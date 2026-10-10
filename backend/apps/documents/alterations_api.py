"""Pages, the page editor's changes, the trash and every document's history."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from django.db.models import Count
from django.http import HttpRequest, HttpResponse
from ninja import Field, Router, Schema
from ninja.errors import HttpError

from apps.audit.service import audit
from apps.documents import alterations, crypto_fields, pages
from apps.documents.api import AlterationOrigin
from apps.documents.models import Alteration, Document, DocumentPage
from apps.storage.backends import StorageError

router = Router(tags=["alterations"])


class PageOut(Schema):
    number: int
    mark: str  # "2/3": the page number printed on it, if any
    blank: bool


class PagesOut(Schema):
    id: UUID
    title: str
    page_count: int
    pages: list[PageOut]
    visible_pages: list[int]  # physical archive page numbers, in Enhanced order


class PageRef(Schema):
    document: UUID
    page: int = Field(..., ge=1)


class OutputIn(Schema):
    pages: list[PageRef] = Field(..., max_length=2000)
    title: str = Field("", max_length=200)


class ComposeIn(Schema):
    # Documents to take pages from; any of them not kept unchanged goes to the trash.
    sources: list[UUID] = Field(..., max_length=200)
    outputs: list[OutputIn] = Field(..., max_length=alterations.MAX_OUTPUTS)
    origin: AlterationOrigin | None = None
    expected_views: dict[str, list[int]] = Field(default_factory=dict)


class IdsIn(Schema):
    ids: list[UUID] = Field(..., max_length=500)
    origin: AlterationOrigin | None = None


class DocRef(Schema):
    id: UUID
    title: str
    trashed: bool


class ChangeOut(Schema):
    field: str
    old: str
    new: str


class AlterationOut(Schema):
    id: int
    kind: str
    actor: str
    summary: str
    created_at: datetime
    undone_at: datetime | None
    can_undo: bool
    sources: list[DocRef]
    results: list[DocRef]
    changes: list[ChangeOut]
    task: int | None  # the assistant run it came from
    finding: str


class HistoryOut(Schema):
    id: UUID
    title: str
    trashed: bool
    deleted_at: datetime | None
    replaced_by: list[DocRef]  # in the trash because these were made from it
    entries: list[AlterationOut]


class TrashItem(Schema):
    id: UUID
    title: str
    page_count: int
    deleted_at: datetime
    replaced_by: list[DocRef]


class PagesStatus(Schema):
    documents: int
    ready: int
    queued: int


def _refs(uuids: list[str], cache: dict[str, Document]) -> list[DocRef]:
    out = []
    for u in uuids:
        d = cache.get(u)
        if d is not None:
            out.append(
                DocRef(
                    id=d.uuid,
                    title=crypto_fields.get_title(d) or "Untitled",
                    trashed=d.deleted_at is not None,
                )
            )
    return out


def _out(alteration: Alteration, cache: dict[str, Document] | None = None) -> AlterationOut:
    wanted = set(alteration.sources) | set(alteration.results)
    if cache is None or not wanted <= set(cache):
        cache = {**(cache or {}), **{str(d.uuid): d for d in Document.objects.filter(uuid__in=wanted)}}
    detail = alterations.detail_of(alteration)
    made = [u for u in alteration.results if u not in alteration.sources] or (
        alteration.results if alteration.kind == Alteration.Kind.RESTORE else []
    )
    can_undo = (
        alteration.kind != Alteration.Kind.EDIT
        and alteration.undone_at is None
        and all(u in cache and cache[u].deleted_at is None for u in made)
        and all(u in cache and cache[u].deleted_at is not None for u in alteration.retired)
        and all(
            u in cache and cache[u].deleted_at is None and pages.view_state(cache[u]) == change["after"]
            for u, change in detail.get("views", {}).items()
        )
    )
    return AlterationOut(
        id=alteration.pk,
        kind=alteration.kind,
        actor=alteration.actor,
        summary=str(detail.get("summary", "")),
        created_at=alteration.created_at,
        undone_at=alteration.undone_at,
        can_undo=can_undo,
        sources=_refs(alteration.sources, cache),
        results=_refs(alteration.results, cache),
        changes=[ChangeOut(**c) for c in detail.get("changes", [])],
        task=detail.get("task"),
        finding=str(detail.get("finding", "")),
    )


def _origin(origin: AlterationOrigin | None) -> tuple[str, dict[str, object] | None]:
    if origin is None:
        return Alteration.Actor.USER, None
    return Alteration.Actor.ASSISTANT, origin.dict()


def _document(doc_id: UUID, *, trashed: bool = False) -> Document:
    qs = Document.objects.filter(uuid=doc_id)
    if not trashed:
        qs = qs.filter(deleted_at__isnull=True)
    document = qs.first()
    if document is None:
        raise HttpError(404, "Document not found")
    return document


@router.get("/pages/status", response=PagesStatus)
def pages_status(request: HttpRequest) -> PagesStatus:
    from apps.processing.models import Job

    documents = Document.objects.filter(
        deleted_at__isnull=True, processing_state=Document.State.DONE, page_count__gt=0
    )
    counts = dict(
        DocumentPage.objects.filter(document__in=documents).values_list("document_id").annotate(n=Count("id"))
    )
    ready = sum(1 for pk, n in documents.values_list("pk", "page_count") if counts.get(pk) == n and n)
    queued = Job.objects.filter(kind=Job.Kind.PAGES, state__in=[Job.State.QUEUED, Job.State.RUNNING]).count()
    return PagesStatus(documents=documents.count(), ready=ready, queued=queued)


@router.post("/pages/prepare", response=PagesStatus)
def prepare_pages(request: HttpRequest) -> PagesStatus:
    """Make page pictures and fingerprints for every document that has none yet."""
    n = pages.schedule_missing()
    audit("pages.prepare", request=request, count=n)
    return pages_status(request)


@router.get("/pages/{doc_id}", response=PagesOut)
def document_pages(request: HttpRequest, doc_id: UUID) -> PagesOut:
    document = _document(doc_id, trashed=True)
    if document.deleted_at is None:
        try:
            pages.ensure(document)
        except (FileNotFoundError, StorageError) as exc:
            raise HttpError(409, "The pages are not available yet") from exc
    prints = pages.fingerprints([document]).get(document.pk, [])
    return PagesOut(
        id=document.uuid,
        title=crypto_fields.get_title(document),
        page_count=document.page_count,
        visible_pages=pages.visible_numbers(document),
        pages=[
            PageOut(
                number=n, mark=f"{fp.mark[0]}/{fp.mark[1]}" if fp.mark else "", blank=fp.ink < pages.EMPTY
            )
            for n, fp in enumerate(prints, start=1)
        ],
    )


@router.get("/pages/{doc_id}/{number}")
def page_thumbnail(request: HttpRequest, doc_id: UUID, number: int, large: bool = False) -> HttpResponse:
    """A page's picture; `large` renders it big enough to read (slower: from the file)."""
    document = _document(doc_id, trashed=True)
    try:
        image = pages.large(document, number) if large else pages.thumbnail(document, number)
    except (FileNotFoundError, StorageError) as exc:
        raise HttpError(409, "The file is not available") from exc
    if image is None:
        raise HttpError(404, "No picture of this page")
    response = HttpResponse(image, content_type="image/webp")
    response["Cache-Control"] = "private, max-age=600"
    return response


@router.post("/compose", response=AlterationOut)
def compose(request: HttpRequest, data: ComposeIn) -> AlterationOut:
    """Make documents from pages: merge, split, extract, remove or reorder. Originals stay untouched."""
    actor, origin = _origin(data.origin)
    outputs = [
        alterations.Output([(str(p.document), p.page) for p in o.pages], " ".join(o.title.split()))
        for o in data.outputs
    ]
    try:
        alteration = alterations.compose(
            [str(u) for u in data.sources],
            outputs,
            actor=actor,
            origin=origin,
            expected_views=data.expected_views,
        )
    except alterations.AlterationError as exc:
        raise HttpError(400, str(exc)) from exc
    except (FileNotFoundError, StorageError) as exc:
        raise HttpError(502, "Could not fetch a document's file. Please try again.") from exc
    audit("document.composed", request=request, alteration=alteration.pk, sources=len(data.sources))
    return _out(alteration)


@router.post("/trash", response=AlterationOut)
def trash(request: HttpRequest, data: IdsIn) -> AlterationOut:
    actor, origin = _origin(data.origin)
    try:
        alteration = alterations.trash([str(u) for u in data.ids], actor=actor, origin=origin)
    except alterations.AlterationError as exc:
        raise HttpError(400, str(exc)) from exc
    audit("document.trashed", request=request, count=len(data.ids))
    return _out(alteration)


@router.post("/restore", response=AlterationOut)
def restore(request: HttpRequest, data: IdsIn) -> AlterationOut:
    try:
        alteration = alterations.restore([str(u) for u in data.ids])
    except alterations.AlterationError as exc:
        raise HttpError(400, str(exc)) from exc
    audit("document.restored", request=request, count=len(data.ids))
    return _out(alteration)


@router.post("/{alteration_id}/undo", response=AlterationOut)
def undo(request: HttpRequest, alteration_id: int) -> AlterationOut:
    alteration = Alteration.objects.filter(pk=alteration_id).first()
    if alteration is None:
        raise HttpError(404, "Not found")
    try:
        alterations.undo(alteration)
    except alterations.AlterationError as exc:
        raise HttpError(400, str(exc)) from exc
    audit("document.undone", request=request, alteration=alteration.pk)
    return _out(alteration)


def _replaced_by(document: Document, cache: dict[str, Document]) -> list[DocRef]:
    """The documents made from it, when an alteration (not undone) put it in the trash."""
    uuid = str(document.uuid)
    for a in Alteration.objects.filter(retired__contains=[uuid], undone_at__isnull=True)[:1]:
        made = [u for u in a.results if u not in a.sources]
        missing = set(made) - set(cache)
        cache.update({str(d.uuid): d for d in Document.objects.filter(uuid__in=missing)})
        return _refs(made, cache)
    return []


@router.get("/document/{doc_id}", response=HistoryOut)
def document_history(request: HttpRequest, doc_id: UUID) -> HistoryOut:
    document = _document(doc_id, trashed=True)
    entries = alterations.history(str(document.uuid))
    wanted = {u for a in entries for u in a.sources + a.results}
    cache = {str(d.uuid): d for d in Document.objects.filter(uuid__in=wanted)}
    return HistoryOut(
        id=document.uuid,
        title=crypto_fields.get_title(document),
        trashed=document.deleted_at is not None,
        deleted_at=document.deleted_at,
        replaced_by=_replaced_by(document, cache) if document.deleted_at else [],
        entries=[_out(a, cache) for a in entries],
    )


@router.get("/trash", response=list[TrashItem])
def trash_list(request: HttpRequest) -> list[TrashItem]:
    cache: dict[str, Document] = {}
    return [
        TrashItem(
            id=d.uuid,
            title=crypto_fields.get_title(d) or "Untitled",
            page_count=d.page_count,
            deleted_at=d.deleted_at,  # type: ignore[arg-type]
            replaced_by=_replaced_by(d, cache),
        )
        for d in Document.objects.filter(deleted_at__isnull=False).order_by("-deleted_at")[:500]
    ]
