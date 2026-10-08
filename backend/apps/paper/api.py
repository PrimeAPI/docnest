"""Paper storage API: physical locations of paper originals and putting documents away."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpRequest
from ninja import Field, Router, Schema
from ninja.errors import HttpError

from apps.audit.service import audit
from apps.documents import crypto_fields
from apps.documents.models import Document
from apps.paper import services
from apps.paper.models import DEFAULT_CAPACITY, Location
from apps.taxonomy.services import folder_paths

router = Router(tags=["paper"])


class LocationOut(Schema):
    id: int
    name: str
    parent_id: int | None
    path: str
    capacity: int
    document_count: int
    sheets: int
    created_at: datetime


class LocationIn(Schema):
    name: str
    parent_id: int | None = None
    capacity: int = Field(DEFAULT_CAPACITY, ge=1, le=100_000)


class LocationPatch(Schema):
    name: str | None = None
    capacity: int | None = Field(None, ge=1, le=100_000)
    parent_id: int | None = None
    move_to_root: bool = False


class PaperDocumentOut(Schema):
    id: UUID
    title: str
    uploaded_at: datetime
    document_date: date | None
    correspondent: str | None
    sheets: int


class PendingItemOut(PaperDocumentOut):
    folder_id: int | None  # the filing folder it was scanned into, to put away one folder at a time
    folder: str | None
    folder_color: str | None


class PendingOut(Schema):
    documents: list[PendingItemOut]
    sheets: int


class StackItemOut(PaperDocumentOut):
    sheets_above: int  # sheets lying on top of this document


class StackOut(Schema):
    location: LocationOut
    documents: list[StackItemOut]  # top of the stack first


class PlaceIn(Schema):
    ids: list[UUID] = Field(..., max_length=2000)


class PlaceOut(Schema):
    placed: int
    location: LocationOut


def _clean_name(value: str) -> str:
    name = " ".join(value.split())[:80]
    if not name:
        raise HttpError(400, "Name cannot be empty")
    if "/" in name or "\\" in name:
        raise HttpError(400, "Location names cannot contain slashes")
    return name


def _sheets_by_location() -> dict[int, tuple[int, int]]:
    """(documents, sheets) per location."""
    totals: dict[int, tuple[int, int]] = {}
    rows = services.active().filter(paper_location__isnull=False)
    for loc, original, pages in rows.values_list("paper_location_id", "original_page_count", "page_count"):
        n, sheets = totals.get(loc, (0, 0))
        totals[loc] = (n + 1, sheets + max(1, original or pages or 1))
    return totals


def _out(location: Location, paths: dict[int, str] | None = None, totals: dict | None = None) -> LocationOut:
    paths = paths if paths is not None else services.location_paths()
    totals = totals if totals is not None else _sheets_by_location()
    n, sheets = totals.get(location.pk, (0, 0))
    return LocationOut(
        id=location.pk,
        name=location.name,
        parent_id=location.parent_id,
        path=paths.get(location.pk, location.name),
        capacity=location.capacity,
        document_count=n,
        sheets=sheets,
        created_at=location.created_at,
    )


def _doc_out(document: Document) -> PaperDocumentOut:
    return PaperDocumentOut(
        id=document.uuid,
        title=crypto_fields.get_title(document)
        or crypto_fields.get_original_filename(document)
        or "Document",
        uploaded_at=document.uploaded_at,
        document_date=document.document_date,
        correspondent=document.correspondent.name if document.correspondent else None,
        sheets=services.sheets_of(document),
    )


def _save(location: Location) -> None:
    try:
        with transaction.atomic():
            location.save()
    except IntegrityError as exc:
        raise HttpError(400, "A location with this name already exists here") from exc


def _get(location_id: int) -> Location:
    location = Location.objects.filter(pk=location_id).first()
    if location is None:
        raise HttpError(404, "Not found")
    return location


@router.get("/locations", response=list[LocationOut])
def list_locations(request: HttpRequest) -> list[LocationOut]:
    paths, totals = services.location_paths(), _sheets_by_location()
    return sorted((_out(loc, paths, totals) for loc in Location.objects.all()), key=lambda o: o.path.lower())


@router.post("/locations", response=LocationOut)
def create_location(request: HttpRequest, data: LocationIn) -> LocationOut:
    if data.parent_id is not None and not Location.objects.filter(pk=data.parent_id).exists():
        raise HttpError(400, "Unknown parent location")
    location = Location(name=_clean_name(data.name), parent_id=data.parent_id, capacity=data.capacity)
    _save(location)
    audit("paper_location.created", request=request, target=location.name)
    return _out(location)


@router.patch("/locations/{location_id}", response=LocationOut)
def update_location(request: HttpRequest, location_id: int, data: LocationPatch) -> LocationOut:
    location = _get(location_id)
    if data.name is not None:
        location.name = _clean_name(data.name)
    if data.capacity is not None:
        location.capacity = data.capacity
    if data.move_to_root:
        location.parent = None
    elif data.parent_id is not None:
        if not Location.objects.filter(pk=data.parent_id).exists():
            raise HttpError(400, "Unknown parent location")
        if data.parent_id in services.location_subtree(location.pk):
            raise HttpError(400, "A location cannot be moved into itself")
        location.parent_id = data.parent_id
    _save(location)
    return _out(location)


@router.delete("/locations/{location_id}")
def delete_location(request: HttpRequest, location_id: int) -> dict[str, bool]:
    location = _get(location_id)
    if services.active().filter(paper_location_id__in=services.location_subtree(location.pk)).exists():
        raise HttpError(400, "The location (or one inside it) still holds documents")
    location.delete()
    audit("paper_location.deleted", request=request, target=location.name)
    return {"ok": True}


@router.get("/pending", response=PendingOut)
def pending(request: HttpRequest) -> PendingOut:
    paths = folder_paths()
    docs = [
        PendingItemOut(
            **_doc_out(d).dict(),
            folder_id=d.folder_id,
            folder=paths.get(d.folder_id) if d.folder_id else None,
            folder_color=d.folder.color if d.folder else None,
        )
        for d in services.pending().select_related("correspondent", "folder")
    ]
    return PendingOut(documents=docs, sheets=sum(d.sheets for d in docs))


@router.get("/locations/{location_id}/stack", response=StackOut)
def location_stack(request: HttpRequest, location_id: int) -> StackOut:
    location = _get(location_id)
    docs = services.stack(location.pk)
    items: list[StackItemOut] = []
    above = 0
    for document in reversed(docs):  # top first
        base = _doc_out(document)
        items.append(StackItemOut(**base.dict(), sheets_above=above))
        above += base.sheets
    return StackOut(location=_out(location), documents=items)


@router.post("/locations/{location_id}/place", response=PlaceOut)
def place(request: HttpRequest, location_id: int, data: PlaceIn) -> PlaceOut:
    """Put the given (not yet placed) documents away in this location, in scan order."""
    location = _get(location_id)
    docs = list(
        services.active()
        .filter(uuid__in=data.ids)
        .filter(Q(paper_location__isnull=True))
        .order_by("uploaded_at", "id")
    )
    placed = services.place(location, docs)
    audit("paper.placed", request=request, target=location.name, count=placed)
    return PlaceOut(placed=placed, location=_out(location))


def pending_count() -> int:
    return services.pending().count()
