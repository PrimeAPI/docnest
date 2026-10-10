from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from django.db import transaction
from django.db.models import QuerySet
from django.http import FileResponse, HttpRequest, HttpResponse
from django.utils import timezone
from ninja import Field, File, Form, Query, Router, Schema, Status
from ninja.errors import HttpError
from ninja.files import UploadedFile

from apps.analysis import ai
from apps.audit.service import audit
from apps.crypto.aead import decrypt_text
from apps.documents import alterations, crypto_fields, files, pages
from apps.documents.intake import (
    IntakeError,
    IntakeRequest,
    receive,
)
from apps.documents.models import Alteration, Document, DocumentTag, ProcessingEvent, Source
from apps.paper import services as paper_services
from apps.paper.models import Location
from apps.processing import pipeline
from apps.processing.models import Job, SystemState
from apps.processing.preferences import get_default_ocr_backend
from apps.processing.schemas import EnhanceSettingsIn
from apps.search import index as search_index
from apps.search.snippets import make_snippet
from apps.storage.backends import StorageAuthError, StorageError
from apps.taxonomy.models import Correspondent, DocumentType, Folder, Series, Tag
from apps.taxonomy.services import (
    find_correspondent,
    folder_paths,
    folder_subtree,
    learn_alias,
    resolve_or_create_tag,
)

router = Router(tags=["documents"])


# --- Schemas ------------------------------------------------------------------


class RefOut(Schema):
    id: int
    name: str


class FolderRefOut(Schema):
    id: int
    name: str
    path: str


class TagRefOut(Schema):
    id: int
    name: str
    color: str
    source: str


class SnippetOut(Schema):
    text: str
    highlights: list[list[int]]


class DocumentListItem(Schema):
    id: UUID
    title: str
    folder: FolderRefOut | None
    document_type: RefOut | None
    correspondent: RefOut | None
    series: RefOut | None
    period_label: str
    tags: list[TagRefOut]
    status: str
    is_important: bool
    is_read: bool
    document_date: date | None
    uploaded_at: datetime
    page_count: int
    processing_state: str
    processing_error: str
    has_series_suggestion: bool
    score: float | None = None
    snippet: SnippetOut | None = None


class DocumentPage(Schema):
    items: list[DocumentListItem]
    total: int
    page: int
    page_size: int


class EventOut(Schema):
    stage: str
    outcome: str
    message: str
    duration_ms: int
    created_at: datetime


class NeighbourOut(Schema):
    id: UUID
    title: str


class PaperPositionOut(Schema):
    index_from_top: int
    count: int
    sheets: int
    sheets_above: int
    sheets_below: int
    total_sheets: int
    capacity: int
    mm_from_top: float
    mm_from_bottom: float
    above: NeighbourOut | None  # the document lying directly on top of this one
    below: NeighbourOut | None


class PaperOut(Schema):
    has_paper: bool
    location_id: int | None
    location_path: str | None
    placed_at: datetime | None
    position: PaperPositionOut | None


class MailDocOut(Schema):
    id: UUID
    title: str
    is_email: bool  # the email itself rather than an attachment


class MailOut(Schema):
    subject: str
    sender: str
    sent_at: datetime | None
    received_at: datetime
    documents: list[MailDocOut]  # everything that arrived with this email


class DocumentDetail(DocumentListItem):
    series_suggestion: RefOut | None
    field_sources: dict[str, object]
    extracted: dict[str, object]
    scanner_metadata: dict[str, object]
    original_filename: str
    received_from: str | None
    size: int
    processing_stage: str
    ocr_backend: str
    stored: bool
    original_page_count: int
    enhanced: bool  # the shown file differs from the original
    hidden_page_count: int  # hidden in Enhanced, still present in the stored archive
    visible_pages: list[int]
    enhancement: dict[str, object]  # settings used and what was changed
    paper: PaperOut
    mail: MailOut | None
    events: list[EventOut]


class DocumentFilters(Schema):
    q: str = ""
    id: list[UUID] = Field(default_factory=list, max_length=100)  # just these documents
    folder: list[int] = Field(default_factory=list)
    subfolders: bool = False  # with `folder`: include documents in subfolders
    unfiled: bool = False  # only documents without a folder
    document_type: list[int] = Field(default_factory=list)
    correspondent: list[int] = Field(default_factory=list)
    tag: list[int] = Field(default_factory=list)
    series: int | None = None
    status: list[Literal["new", "todo", "done"]] = Field(default_factory=list)
    important: bool | None = None
    unread: bool | None = None
    processing: list[Literal["pending", "running", "done", "failed"]] = Field(default_factory=list)
    uploaded_from: date | None = None
    uploaded_to: date | None = None
    date_from: date | None = None
    date_to: date | None = None
    paper_location: int | None = None  # documents put away in this location (or one inside it)
    paper_pending: bool = False  # paper originals not put away yet
    sort: Literal["relevance", "uploaded", "-uploaded", "date", "-date"] = "relevance"
    page: int = Field(1, ge=1)
    page_size: int = Field(25, ge=1, le=100)


class AlterationOrigin(Schema):
    """Which suggestion of the assistant a change came from (shown in the document's history)."""

    task: int
    finding: str = Field("", max_length=20)


class DocumentPatch(Schema):
    title: str | None = None
    document_date: date | None = None
    clear_document_date: bool = False
    document_type_id: int | None = None
    folder_id: int | None = None
    clear_folder: bool = False
    correspondent_id: int | None = None
    correspondent_name: str | None = None
    clear_correspondent: bool = False
    tag_ids: list[int] | None = None
    tag_names: list[str] | None = None
    status: Literal["new", "todo", "done"] | None = None
    is_important: bool | None = None
    series_id: int | None = None
    clear_series: bool = False
    accept_series_suggestion: bool = False
    reject_series_suggestion: bool = False
    has_paper: bool | None = None
    paper_location_id: int | None = None  # put away (on top of that location's stack)
    clear_paper_location: bool = False
    origin: AlterationOrigin | None = None  # applied from an assistant suggestion


ReprocessStep = Literal["enhance", "ocr", "analyze"]
STEP_ORDER: list[str] = ["enhance", "ocr", "analyze"]


class ReprocessIn(Schema):
    # The steps to run again; storage and indexing always follow. Enhancement changes the
    # pages, so it brings text recognition along. Without "enhance", text recognition reads
    # the current (enhanced) version again.
    steps: list[ReprocessStep] | None = None
    # Shorthand used by older clients when `steps` is missing: "ocr" = every step, "analyze" = analysis.
    stage: Literal["ocr", "analyze"] | None = None
    backend: Literal["ocrmypdf", "docling"] | None = None  # text recognition: keep when None
    # One-off scan enhancement settings for this run (merged over the system settings).
    enhancement: EnhanceSettingsIn | None = None
    # Analysis: None = as in Settings, "" = rules only, else an installed Ollama model.
    ai_model: str | None = Field(default=None, max_length=200)

    def resolved_steps(self) -> list[str]:
        chosen = (
            set(self.steps)
            if self.steps is not None
            else set(["analyze"] if self.stage == "analyze" else STEP_ORDER)
        )
        if "enhance" in chosen:
            chosen.add("ocr")
        return [step for step in STEP_ORDER if step in chosen]


class BulkAction(Schema):
    ids: list[UUID]
    action: Literal[
        "mark_read",
        "mark_unread",
        "status_done",
        "status_todo",
        "status_new",
        "important",
        "unimportant",
        "move",
        "reprocess",
        "paper_yes",
        "paper_no",
    ]
    folder_id: int | None = None  # target of "move"; null = unfile
    reprocess: ReprocessIn | None = None  # options of "reprocess"


class TextOut(Schema):
    text: str
    format: str
    backend: str


class StructureOut(Schema):
    backend: str
    data: dict[str, object]


class BulkOut(Schema):
    updated: int


# --- Helpers ------------------------------------------------------------------


def base_queryset() -> QuerySet[Document]:
    return (
        Document.objects.filter(deleted_at__isnull=True)
        .select_related("folder", "document_type", "correspondent", "series")
        .prefetch_related("documenttag_set__tag")
    )


def get_document(doc_id: UUID, *, trashed: bool = False) -> Document:
    """An active document; with `trashed`, one in the trash too (its files are kept)."""
    qs = base_queryset()
    if trashed:
        qs = qs.model.objects.select_related("folder", "document_type", "correspondent", "series")
    document = qs.filter(uuid=doc_id).first()
    if document is None:
        raise HttpError(404, "Document not found")
    return document


def _ref(obj: object) -> RefOut | None:
    if obj is None:
        return None
    return RefOut(id=obj.pk, name=str(obj))  # type: ignore[attr-defined]


def _folder_ref(document: Document, paths: dict[int, str] | None) -> FolderRefOut | None:
    folder = document.folder
    if folder is None:
        return None
    paths = paths if paths is not None else folder_paths()
    return FolderRefOut(id=folder.pk, name=folder.name, path=paths.get(folder.pk, folder.name))


def to_list_item(
    document: Document,
    score: float | None = None,
    snippet: object = None,
    paths: dict[int, str] | None = None,
) -> DocumentListItem:
    tags = sorted(
        (
            TagRefOut(id=dt.tag.pk, name=dt.tag.name, color=dt.tag.color, source=dt.source)
            for dt in document.documenttag_set.all()
        ),
        key=lambda t: t.name.lower(),
    )
    return DocumentListItem(
        id=document.uuid,
        title=crypto_fields.get_title(document) or "Processing…",
        folder=_folder_ref(document, paths),
        document_type=_ref(document.document_type),
        correspondent=_ref(document.correspondent),
        series=_ref(document.series),
        period_label=document.period_label,
        tags=tags,
        status=document.status,
        is_important=document.is_important,
        is_read=document.read_at is not None,
        document_date=document.document_date,
        uploaded_at=document.uploaded_at,
        page_count=len(pages.visible_numbers(document)),
        processing_state=document.processing_state,
        processing_error=document.processing_error,
        has_series_suggestion=document.series_suggestion_id is not None,
        score=score,
        snippet=SnippetOut(text=snippet.text, highlights=snippet.highlights) if snippet else None,  # type: ignore[attr-defined]
    )


def to_detail(document: Document) -> DocumentDetail:
    item = to_list_item(document)
    events = ProcessingEvent.objects.filter(document=document).order_by("-created_at")[:30]
    return DocumentDetail(
        **item.dict(),
        series_suggestion=_ref(document.series_suggestion),
        field_sources={k: v for k, v in document.field_sources.items() if k != "tags_removed"},
        extracted=crypto_fields.get_extracted(document),
        scanner_metadata=crypto_fields.get_scanner_metadata(document),
        original_filename=crypto_fields.get_original_filename(document),
        received_from=(
            document.received_from.name
            if document.received_from
            else "Email"
            if document.mail_id
            else "Web upload"
        ),
        size=document.size,
        processing_stage=document.processing_stage,
        ocr_backend=document.ocr_backend or get_default_ocr_backend(),
        stored=bool(document.storage_original),
        original_page_count=document.original_page_count or document.page_count,
        enhanced=_is_enhanced(document),
        hidden_page_count=document.page_count - len(pages.visible_numbers(document)),
        visible_pages=pages.visible_numbers(document),
        enhancement={k: v for k, v in document.enhancement.items() if k in ("settings", "summary")},
        paper=_paper(document),
        mail=_mail(document),
        events=[
            EventOut(
                stage=e.stage,
                outcome=e.outcome,
                message=e.message,
                duration_ms=e.duration_ms,
                created_at=e.created_at,
            )
            for e in events
        ],
    )


def _mail(document: Document) -> MailOut | None:
    mail = document.mail
    if mail is None:
        return None
    siblings = Document.objects.filter(mail=mail, deleted_at__isnull=True).order_by("pk")
    return MailOut(
        subject=decrypt_text(mail.subject_enc),
        sender=decrypt_text(mail.sender_enc),
        sent_at=mail.sent_at,
        received_at=mail.received_at,
        documents=[
            MailDocOut(
                id=d.uuid,
                title=crypto_fields.get_title(d) or crypto_fields.get_original_filename(d) or "Processing…",
                is_email=d.pk == mail.email_document_id,
            )
            for d in siblings
        ],
    )


def _paper(document: Document) -> PaperOut:
    pos = paper_services.position(document)
    path = None
    if document.paper_location_id is not None:
        path = paper_services.location_paths().get(document.paper_location_id)
    return PaperOut(
        has_paper=document.has_paper,
        location_id=document.paper_location_id,
        location_path=path,
        placed_at=document.paper_placed_at,
        position=PaperPositionOut(
            index_from_top=pos.index_from_top,
            count=pos.count,
            sheets=pos.sheets,
            sheets_above=pos.sheets_above,
            sheets_below=pos.sheets_below,
            total_sheets=pos.total_sheets,
            capacity=pos.capacity,
            mm_from_top=pos.mm_from_top,
            mm_from_bottom=pos.mm_from_bottom,
            above=NeighbourOut(id=UUID(pos.above.id), title=pos.above.title) if pos.above else None,
            below=NeighbourOut(id=UUID(pos.below.id), title=pos.below.title) if pos.below else None,
        )
        if pos
        else None,
    )


def _is_enhanced(document: Document) -> bool:
    summary = document.enhancement.get("summary") or {}
    return pages.visible_numbers(document) != list(range(1, document.page_count + 1)) or any(
        summary.get(key) for key in ("rotated", "deskewed", "cropped", "cleaned", "removed_blank")
    )


def apply_filters(qs: QuerySet[Document], f: DocumentFilters) -> QuerySet[Document]:
    if f.id:
        qs = qs.filter(uuid__in=f.id)
    if f.paper_location is not None:
        qs = qs.filter(paper_location_id__in=paper_services.location_subtree(f.paper_location))
    if f.paper_pending:
        qs = qs.filter(has_paper=True, paper_location__isnull=True)
    if f.unfiled:
        qs = qs.filter(folder__isnull=True)
    elif f.folder:
        qs = qs.filter(folder_id__in=folder_subtree(f.folder) if f.subfolders else f.folder)
    if f.document_type:
        qs = qs.filter(document_type_id__in=f.document_type)
    if f.correspondent:
        qs = qs.filter(correspondent_id__in=f.correspondent)
    for tag_id in f.tag:  # all selected tags must be present
        qs = qs.filter(documenttag__tag_id=tag_id)
    if f.series is not None:
        qs = qs.filter(series_id=f.series)
    if f.status:
        qs = qs.filter(status__in=f.status)
    if f.important is not None:
        qs = qs.filter(is_important=f.important)
    if f.unread is not None:
        qs = qs.filter(read_at__isnull=f.unread)
    if f.processing:
        qs = qs.filter(processing_state__in=f.processing)
    if f.uploaded_from:
        qs = qs.filter(uploaded_at__date__gte=f.uploaded_from)
    if f.uploaded_to:
        qs = qs.filter(uploaded_at__date__lte=f.uploaded_to)
    if f.date_from:
        qs = qs.filter(document_date__gte=f.date_from)
    if f.date_to:
        qs = qs.filter(document_date__lte=f.date_to)
    return qs.distinct() if f.tag else qs


_SORTS = {
    "uploaded": ["uploaded_at", "id"],
    "-uploaded": ["-uploaded_at", "-id"],
    "date": ["document_date", "id"],
    "-date": ["-document_date", "-uploaded_at"],
}


def reindex_quietly(document: Document) -> None:
    if document.processing_state == Document.State.DONE:
        pipeline.reindex(document)


# --- Endpoints ----------------------------------------------------------------


@router.get("", response=DocumentPage)
def list_documents(request: HttpRequest, filters: Query[DocumentFilters]) -> DocumentPage:
    qs = apply_filters(Document.objects.filter(deleted_at__isnull=True), filters)
    offset = (filters.page - 1) * filters.page_size
    query = filters.q.strip()[:200]
    if query:
        hits = search_index.search(query, qs, limit=filters.page_size, offset=offset)
        by_id = {d.pk: d for d in base_queryset().filter(pk__in=hits.ids)}
        ordered = [by_id[i] for i in hits.ids if i in by_id]
        if filters.sort != "relevance":
            reverse = filters.sort.startswith("-")
            key = "uploaded_at" if "uploaded" in filters.sort else "document_date"
            ordered.sort(
                key=lambda d: (getattr(d, key) is not None, getattr(d, key) or date.min), reverse=reverse
            )
        items = []
        paths = folder_paths()
        for d in ordered:
            snippet = make_snippet(crypto_fields.get_content(d), query)
            items.append(to_list_item(d, hits.scores.get(d.pk), snippet, paths))
        return DocumentPage(items=items, total=hits.total, page=filters.page, page_size=filters.page_size)

    order = _SORTS.get(filters.sort, _SORTS["-uploaded"])
    page_qs = base_queryset().filter(pk__in=qs.values("pk")).order_by(*order)
    total = qs.count()
    page_docs = list(page_qs[offset : offset + filters.page_size])
    paths = folder_paths()
    return DocumentPage(
        items=[to_list_item(d, paths=paths) for d in page_docs],
        total=total,
        page=filters.page,
        page_size=filters.page_size,
    )


MAX_SELECTION = 500  # as many as one bulk action takes


class DocumentIds(Schema):
    ids: list[UUID]
    total: int  # all matching documents; at most MAX_SELECTION ids are returned


@router.get("/ids", response=DocumentIds)
def document_ids(request: HttpRequest, filters: Query[DocumentFilters]) -> DocumentIds:
    """Every matching document, for "select all" across pages (filters only, no text search)."""
    qs = apply_filters(Document.objects.filter(deleted_at__isnull=True), filters)
    order = _SORTS.get(filters.sort, _SORTS["-uploaded"])
    ids = list(qs.order_by(*order).values_list("uuid", flat=True)[:MAX_SELECTION])
    return DocumentIds(ids=ids, total=qs.count())


class WebUploadOut(Schema):
    id: UUID
    duplicate: bool


@router.post("/upload", response={200: WebUploadOut, 202: WebUploadOut})
def web_upload(
    request: HttpRequest,
    file: File[UploadedFile],
    folder_id: Form[int | None] = None,
    document_type: Form[str] = "auto",
    todo: Form[bool] = False,
    important: Form[bool] = False,
) -> Status:
    """Upload a PDF from the web UI. Uses the same durable intake and pipeline as scanners."""
    if folder_id is not None and not Folder.objects.filter(pk=folder_id).exists():
        raise HttpError(400, "Unknown folder")
    temp = getattr(file, "temporary_file_path", None)
    if temp is None:
        raise HttpError(500, "upload was not streamed to disk")
    req = IntakeRequest(
        folder_id=folder_id,
        document_type=document_type,
        todo=todo,
        important=important,
        filename=Path(file.name or "").name,
    )
    try:
        result = receive(Path(temp()), req)
    except IntakeError as exc:
        raise HttpError(exc.status, str(exc)) from exc
    finally:
        file.close()
    audit(
        "upload.accepted" if result.created else "upload.duplicate",
        request=request,
        target=str(result.document.uuid),
        source="web",
    )
    out = WebUploadOut(id=result.document.uuid, duplicate=not result.created)
    return Status(202 if result.created else 200, out)


@router.post("/reprocess-all", response=BulkOut)
def reprocess_all(request: HttpRequest, data: ReprocessIn) -> dict[str, int]:
    """Queue every document (e.g. overnight, after choosing a new AI model); busy ones are skipped."""
    n = 0
    for document in Document.objects.filter(deleted_at__isnull=True).order_by("uploaded_at", "id").iterator():
        try:
            start_reprocess(request, document, data, quiet=True, background=True)
        except AlreadyProcessing:
            continue
        n += 1
    audit("document.reprocess_all", request=request, steps=data.resolved_steps(), count=n)
    return {"updated": n}


@router.post("/bulk", response=BulkOut)
def bulk_update(request: HttpRequest, data: BulkAction) -> dict[str, int]:
    qs = Document.objects.filter(uuid__in=data.ids[:MAX_SELECTION], deleted_at__isnull=True)
    if data.action == "move":
        if data.folder_id is not None and not Folder.objects.filter(pk=data.folder_id).exists():
            raise HttpError(400, "Unknown folder")
        n = 0
        with transaction.atomic():
            for document in qs.select_for_update():
                before = alterations.snapshot(document)
                document.folder_id = data.folder_id
                document.set_source("folder", Source.USER)
                document.save(update_fields=["folder", "field_sources", "updated_at"])
                alterations.record_edit(document, before)
                n += 1
        audit("document.bulk", request=request, operation="move", count=n, folder=data.folder_id)
        return {"updated": n}
    if data.action == "reprocess":
        options = data.reprocess or ReprocessIn()
        n = 0
        for document in qs:
            try:
                start_reprocess(request, document, options, background=True)
            except AlreadyProcessing:
                continue  # already in the queue: nothing to do
            n += 1
        audit("document.bulk", request=request, operation="reprocess", count=n)
        return {"updated": n}
    updates: dict[str, object] = {
        "mark_read": {"read_at": timezone.now()},
        "mark_unread": {"read_at": None},
        "status_done": {"status": Document.Status.DONE},
        "status_todo": {"status": Document.Status.TODO},
        "status_new": {"status": Document.Status.NEW},
        "important": {"is_important": True},
        "unimportant": {"is_important": False},
        "paper_yes": {"has_paper": True},
        "paper_no": {"has_paper": False, "paper_location": None, "paper_placed_at": None},
    }[data.action]  # type: ignore[assignment]
    n = 0
    with transaction.atomic():
        for document in qs.select_for_update():
            before = alterations.snapshot(document)
            for name, value in updates.items():
                setattr(document, name, value)
                document.set_source(name, Source.USER)
            document.save(update_fields=[*updates, "field_sources", "updated_at"])
            alterations.record_edit(document, before)
            n += 1
    audit("document.bulk", request=request, operation=data.action, count=n)
    return {"updated": n}


@router.get("/{doc_id}", response=DocumentDetail)
def document_detail(request: HttpRequest, doc_id: UUID) -> DocumentDetail:
    document = get_document(doc_id)
    if document.read_at is None:
        document.read_at = timezone.now()
        Document.objects.filter(pk=document.pk).update(read_at=document.read_at)
    return to_detail(document)


@router.patch("/{doc_id}", response=DocumentDetail)
def update_document(request: HttpRequest, doc_id: UUID, data: DocumentPatch) -> DocumentDetail:
    with transaction.atomic():
        document = Document.objects.select_for_update().get(pk=get_document(doc_id).pk)
        before = alterations.snapshot(document)
        changed: list[str] = []
        series_to_refresh: set[int] = set()

        if data.title is not None:
            title = data.title.strip()
            if not title:
                raise HttpError(400, "Title cannot be empty")
            crypto_fields.set_title(document, title)
            document.set_source("title", Source.USER)
            changed.append("title")
        if data.clear_document_date:
            document.document_date = None
            document.set_source("document_date", Source.USER)
            changed.append("document_date")
        elif data.document_date is not None:
            document.document_date = data.document_date
            document.set_source("document_date", Source.USER)
            changed.append("document_date")
        if data.document_type_id is not None:
            if not DocumentType.objects.filter(pk=data.document_type_id).exists():
                raise HttpError(400, "Unknown document type")
            document.document_type_id = data.document_type_id
            document.set_source("document_type", Source.USER)
            changed.append("document_type")
        if data.clear_folder:
            document.folder = None
            document.set_source("folder", Source.USER)
            changed.append("folder")
        elif data.folder_id is not None:
            if not Folder.objects.filter(pk=data.folder_id).exists():
                raise HttpError(400, "Unknown folder")
            document.folder_id = data.folder_id
            document.set_source("folder", Source.USER)
            changed.append("folder")
        if data.clear_correspondent:
            document.correspondent = None
            document.set_source("correspondent", Source.USER)
            changed.append("correspondent")
        elif data.correspondent_id is not None:
            if not Correspondent.objects.filter(pk=data.correspondent_id).exists():
                raise HttpError(400, "Unknown correspondent")
            document.correspondent_id = data.correspondent_id
            document.set_source("correspondent", Source.USER)
            changed.append("correspondent")
        elif data.correspondent_name:
            name = data.correspondent_name.strip()[:150]
            document.correspondent = find_correspondent(name) or Correspondent.objects.create(name=name)
            document.set_source("correspondent", Source.USER)
            changed.append("correspondent")
        if "correspondent" in changed and document.correspondent_id:
            # Learn the spelling on the letter, so the next one from this sender is filed alike.
            read = crypto_fields.get_extracted(document).get("sender")
            corr = Correspondent.objects.get(pk=document.correspondent_id)
            learn_alias(corr, read if isinstance(read, str) else None, document_id=document.pk)
        if data.status is not None:
            document.status = data.status
            document.set_source("status", Source.USER)
            changed.append("status")
        if data.is_important is not None:
            document.is_important = data.is_important
            changed.append("is_important")

        if data.clear_series:
            if document.series_id:
                series_to_refresh.add(document.series_id)
            document.series = None
            document.period_label = ""
            document.set_source("series", Source.USER)
            changed.append("series")
        elif data.series_id is not None:
            if not Series.objects.filter(pk=data.series_id).exists():
                raise HttpError(400, "Unknown series")
            if document.series_id:
                series_to_refresh.add(document.series_id)
            document.series_id = data.series_id
            document.series_suggestion = None
            series_to_refresh.add(data.series_id)
            document.set_source("series", Source.USER)
            changed.append("series")
        elif data.accept_series_suggestion and document.series_suggestion_id:
            document.series_id = document.series_suggestion_id
            document.series_suggestion = None
            series_to_refresh.add(document.series_id)
            document.set_source("series", Source.USER)
            changed.append("series")
        elif data.reject_series_suggestion:
            document.series_suggestion = None
            document.set_source("series", Source.USER)
            changed.append("series")

        if data.has_paper is not None:
            document.has_paper = data.has_paper
            if not data.has_paper:
                document.paper_location = None
                document.paper_placed_at = None
            changed.append("has_paper")
        if data.clear_paper_location:
            document.paper_location = None
            document.paper_placed_at = None
            changed.append("paper_location")
        elif data.paper_location_id is not None:
            if not Location.objects.filter(pk=data.paper_location_id).exists():
                raise HttpError(400, "Unknown location")
            if document.paper_location_id != data.paper_location_id:
                document.paper_location_id = data.paper_location_id
                document.paper_placed_at = timezone.now()
                document.has_paper = True
            changed.append("paper_location")

        document.save()

        if data.tag_ids is not None or data.tag_names is not None:
            wanted: set[int] = set(data.tag_ids or [])
            if data.tag_ids and Tag.objects.filter(pk__in=wanted).count() != len(wanted):
                raise HttpError(400, "Unknown tag")
            for name in data.tag_names or []:
                if name.strip():
                    wanted.add(resolve_or_create_tag(name, suggested=False).pk)
            current = dict(DocumentTag.objects.filter(document=document).values_list("tag_id", "source"))
            removed = [t for t in current if t not in wanted]
            DocumentTag.objects.filter(document=document, tag_id__in=removed).delete()
            for tag_id in wanted - set(current):
                DocumentTag.objects.create(document=document, tag_id=tag_id, source=Source.USER)
            DocumentTag.objects.filter(document=document, tag_id__in=wanted).update(source=Source.USER)
            Tag.objects.filter(pk__in=wanted, is_suggested=True).update(is_suggested=False)
            # Remember removals so automatic tagging does not re-add them.
            prev = set(document.field_sources.get("tags_removed", []))
            document.field_sources = {
                **document.field_sources,
                "tags": Source.USER,
                "tags_removed": sorted((prev | set(removed)) - wanted),
            }
            document.save(update_fields=["field_sources"])
            changed.append("tags")

    from apps.analysis.series import refresh_series

    for series_id in series_to_refresh:
        s = Series.objects.filter(pk=series_id).first()
        if s:
            if s.is_suggested and "series" in changed:
                s.is_suggested = False
                s.save(update_fields=["is_suggested"])
            refresh_series(s)
    if changed:
        if {"title", "correspondent", "document_type", "tags", "series"} & set(changed):
            reindex_quietly(document)
        if {"document_type", "correspondent", "tags"} & set(changed):
            SystemState.objects.update_or_create(key="classifier_dirty", defaults={"value": {"dirty": True}})
        audit("document.updated", request=request, target=str(document.uuid), fields=changed)
        assistant = data.origin is not None
        alterations.record_edit(
            document,
            before,
            actor=Alteration.Actor.ASSISTANT if assistant else Alteration.Actor.USER,
            origin=data.origin.model_dump() if data.origin else None,
        )
    return to_detail(get_document(doc_id))


@router.post("/{doc_id}/unread")
def mark_unread(request: HttpRequest, doc_id: UUID) -> dict[str, bool]:
    Document.objects.filter(pk=get_document(doc_id).pk).update(read_at=None)
    return {"ok": True}


@router.get("/{doc_id}/file")
def document_file(
    request: HttpRequest,
    doc_id: UUID,
    variant: Literal["archive", "original"] = "archive",
    download: bool = False,
) -> HttpResponse | FileResponse:
    document = get_document(doc_id, trashed=True)
    try:
        fh = files.fetch(document, variant)
    except FileNotFoundError as exc:
        raise HttpError(409, "The file is not available yet") from exc
    except StorageAuthError as exc:
        raise HttpError(503, "Storage needs to be re-authenticated by the administrator") from exc
    except StorageError as exc:
        raise HttpError(502, "Could not fetch the file from storage. Please try again.") from exc
    title = crypto_fields.get_title(document) or "document"
    safe = "".join(c if c.isalnum() or c in " -_." else "_" for c in title)[:100].strip() or "document"
    response = FileResponse(
        fh, content_type="application/pdf", as_attachment=download, filename=f"{safe}.pdf"
    )  # type: ignore[arg-type]
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    audit(
        "document.downloaded" if download else "document.opened",
        request=request,
        target=str(document.uuid),
        variant=variant,
    )
    return response


@router.get("/{doc_id}/thumbnail")
def document_thumbnail(request: HttpRequest, doc_id: UUID) -> HttpResponse:
    document = get_document(doc_id)
    shown = pages.visible_numbers(document)
    image = pages.thumbnail(document, shown[0]) if shown and shown[0] != 1 else None
    image = image or crypto_fields.get_thumbnail(document)
    if image is None:
        raise HttpError(404, "No thumbnail")
    response = HttpResponse(image, content_type="image/webp")
    response["Cache-Control"] = "private, max-age=300"
    return response


@router.get("/{doc_id}/text", response=TextOut)
def document_text(request: HttpRequest, doc_id: UUID) -> dict[str, str]:
    document = get_document(doc_id)
    content = getattr(document, "content", None)
    return {
        "text": crypto_fields.get_content(document),
        "format": content.format if content else "text",
        "backend": document.ocr_backend or get_default_ocr_backend(),
    }


@router.get("/{doc_id}/structure", response=StructureOut)
def document_structure(request: HttpRequest, doc_id: UUID) -> dict[str, object]:
    document = get_document(doc_id)
    return {
        "backend": document.ocr_backend or get_default_ocr_backend(),
        "data": crypto_fields.get_structure(document),
    }


class AlreadyProcessing(Exception):
    pass


def start_reprocess(
    request: HttpRequest,
    document: Document,
    data: ReprocessIn,
    *,
    quiet: bool = False,
    background: bool = False,
) -> str:
    """Queue a document for reprocessing; returns the stage it restarts from."""
    if Job.objects.filter(
        document=document,
        kind__in=pipeline.DOCUMENT_JOB_KINDS,
        state__in=[Job.State.QUEUED, Job.State.RUNNING],
    ).exists():
        raise AlreadyProcessing
    steps = data.resolved_steps()
    if not steps:
        raise HttpError(422, "Choose at least one step")
    if data.backend is not None and "ocr" not in steps:
        raise HttpError(422, "A processor can only be chosen when text recognition runs")
    if data.enhancement is not None and "enhance" not in steps:
        raise HttpError(422, "Enhancement settings can only be chosen when the scan enhancement runs")
    if data.ai_model is not None and "analyze" not in steps:
        raise HttpError(422, "An AI model can only be chosen when the analysis runs")
    if data.ai_model and not ai.valid_model_name(data.ai_model):
        raise HttpError(422, "Invalid model name")
    fields: list[str] = []
    if data.backend is not None:
        document.ocr_backend = data.backend
        fields.append("ocr_backend")
    state = {k: v for k, v in document.enhancement.items() if k != "override"}
    if data.enhancement is not None:
        state["override"] = data.enhancement.changes()
    if state != document.enhancement:
        document.enhancement = state
        fields.append("enhancement")
    if fields:
        document.save(update_fields=fields)
    stage: str = pipeline.STEP_STAGES[steps[0]]
    if document.processing_state == Document.State.FAILED:
        failed = document.processing_stage
        order = pipeline.ORDER
        if failed in order and order.index(failed) < order.index(stage):
            # It never got this far: everything from where it failed has to run first.
            steps = [
                s
                for s in STEP_ORDER
                if s in steps or order.index(pipeline.STEP_STAGES[s]) >= order.index(failed)
            ]
            stage = failed
    plan: dict[str, object] = {} if steps == STEP_ORDER else {"steps": steps}
    if data.ai_model is not None:
        plan["ai_model"] = data.ai_model
    if background:
        plan["background"] = True  # a batch: new scans go first
    pipeline.restart_from(document, stage, plan)
    pipeline.enqueue(document)
    if not quiet:
        audit("document.reprocess", request=request, target=str(document.uuid), stage=stage, steps=steps)
    return stage


@router.post("/{doc_id}/reprocess", response=DocumentDetail)
def reprocess(request: HttpRequest, doc_id: UUID, data: ReprocessIn) -> DocumentDetail:
    document = get_document(doc_id)
    try:
        start_reprocess(request, document, data)
    except AlreadyProcessing as exc:
        raise HttpError(409, "Document is already being processed") from exc
    return to_detail(get_document(doc_id))


@router.delete("/{doc_id}")
def delete_document(request: HttpRequest, doc_id: UUID) -> dict[str, bool]:
    """Put a document in the trash: it can be restored, its files stay. See /alterations/trash."""
    document = get_document(doc_id)
    try:
        alterations.trash([str(document.uuid)])
    except alterations.AlterationError as exc:
        raise HttpError(409, str(exc)) from exc
    audit("document.trashed", request=request, target=str(document.uuid))
    return {"ok": True}
