from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet
from django.http import FileResponse, HttpRequest, HttpResponse
from django.utils import timezone
from ninja import Field, File, Form, Query, Router, Schema, Status
from ninja.errors import HttpError
from ninja.files import UploadedFile

from apps.audit.service import audit
from apps.documents import crypto_fields, files
from apps.documents.intake import IntakeError, IntakeRequest, archive_intake_path_for, receive, remove_intake
from apps.documents.models import Document, DocumentTag, ProcessingEvent, Source
from apps.processing import pipeline, queue
from apps.processing.models import Job, SystemState
from apps.search import index as search_index
from apps.search.snippets import make_snippet
from apps.storage.backends import StorageAuthError, StorageError
from apps.taxonomy.models import Bucket, Correspondent, DocumentType, Series, Tag
from apps.taxonomy.services import find_correspondent, resolve_or_create_tag

router = Router(tags=["documents"])


# --- Schemas ------------------------------------------------------------------


class RefOut(Schema):
    id: int
    name: str


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
    bucket: RefOut
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
    events: list[EventOut]


class DocumentFilters(Schema):
    q: str = ""
    bucket: list[int] = Field(default_factory=list)
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
    sort: Literal["relevance", "uploaded", "-uploaded", "date", "-date"] = "relevance"
    page: int = Field(1, ge=1)
    page_size: int = Field(25, ge=1, le=100)


class DocumentPatch(Schema):
    title: str | None = None
    document_date: date | None = None
    clear_document_date: bool = False
    document_type_id: int | None = None
    bucket_id: int | None = None
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


class BulkAction(Schema):
    ids: list[UUID]
    action: Literal[
        "mark_read", "mark_unread", "status_done", "status_todo", "status_new", "important", "unimportant"
    ]


class TextOut(Schema):
    text: str
    format: str
    backend: str


class StructureOut(Schema):
    backend: str
    data: dict[str, object]


class BulkOut(Schema):
    updated: int


class ReprocessIn(Schema):
    stage: Literal["ocr", "analyze"] = "ocr"
    backend: Literal["ocrmypdf", "docling"] | None = None


# --- Helpers ------------------------------------------------------------------


def base_queryset() -> QuerySet[Document]:
    return (
        Document.objects.filter(deleted_at__isnull=True)
        .select_related("bucket", "document_type", "correspondent", "series")
        .prefetch_related("documenttag_set__tag")
    )


def get_document(doc_id: UUID) -> Document:
    document = base_queryset().filter(uuid=doc_id).first()
    if document is None:
        raise HttpError(404, "Document not found")
    return document


def _ref(obj: object) -> RefOut | None:
    if obj is None:
        return None
    return RefOut(id=obj.pk, name=str(obj))  # type: ignore[attr-defined]


def to_list_item(document: Document, score: float | None = None, snippet: object = None) -> DocumentListItem:
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
        bucket=_ref(document.bucket),  # type: ignore[arg-type]
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
        page_count=document.page_count,
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
        received_from=document.received_from.name if document.received_from else "Web upload",
        size=document.size,
        processing_stage=document.processing_stage,
        ocr_backend=document.ocr_backend or str(settings.OCR_BACKEND),
        stored=bool(document.storage_original),
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


def apply_filters(qs: QuerySet[Document], f: DocumentFilters) -> QuerySet[Document]:
    if f.bucket:
        qs = qs.filter(bucket_id__in=f.bucket)
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
        for d in ordered:
            snippet = make_snippet(crypto_fields.get_content(d), query)
            items.append(to_list_item(d, hits.scores.get(d.pk), snippet))
        return DocumentPage(items=items, total=hits.total, page=filters.page, page_size=filters.page_size)

    order = _SORTS.get(filters.sort, _SORTS["-uploaded"])
    page_qs = base_queryset().filter(pk__in=qs.values("pk")).order_by(*order)
    total = qs.count()
    page_docs = list(page_qs[offset : offset + filters.page_size])
    return DocumentPage(
        items=[to_list_item(d) for d in page_docs],
        total=total,
        page=filters.page,
        page_size=filters.page_size,
    )


class WebUploadOut(Schema):
    id: UUID
    duplicate: bool


@router.post("/upload", response={200: WebUploadOut, 202: WebUploadOut})
def web_upload(
    request: HttpRequest,
    file: File[UploadedFile],
    bucket_id: Form[int | None] = None,
    document_type: Form[str] = "auto",
    todo: Form[bool] = False,
    important: Form[bool] = False,
) -> Status:
    """Upload a PDF from the web UI. Uses the same durable intake and pipeline as scanners."""
    bucket = Bucket.objects.filter(pk=bucket_id).first() if bucket_id else None
    bucket = bucket or Bucket.objects.filter(slug="private").first() or Bucket.objects.order_by("pk").first()
    if bucket is None:
        raise HttpError(400, "Create a bucket first")
    temp = getattr(file, "temporary_file_path", None)
    if temp is None:
        raise HttpError(500, "upload was not streamed to disk")
    req = IntakeRequest(
        bucket=bucket.slug,
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


@router.post("/bulk", response=BulkOut)
def bulk_update(request: HttpRequest, data: BulkAction) -> dict[str, int]:
    qs = Document.objects.filter(uuid__in=data.ids[:500], deleted_at__isnull=True)
    updates: dict[str, object] = {
        "mark_read": {"read_at": timezone.now()},
        "mark_unread": {"read_at": None},
        "status_done": {"status": Document.Status.DONE},
        "status_todo": {"status": Document.Status.TODO},
        "status_new": {"status": Document.Status.NEW},
        "important": {"is_important": True},
        "unimportant": {"is_important": False},
    }[data.action]  # type: ignore[assignment]
    n = qs.update(**updates)  # type: ignore[arg-type]
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
        if data.bucket_id is not None:
            if not Bucket.objects.filter(pk=data.bucket_id).exists():
                raise HttpError(400, "Unknown bucket")
            document.bucket_id = data.bucket_id
            document.set_source("bucket", Source.USER)
            changed.append("bucket")
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
        if {"document_type", "correspondent", "bucket", "tags"} & set(changed):
            SystemState.objects.update_or_create(key="classifier_dirty", defaults={"value": {"dirty": True}})
        audit("document.updated", request=request, target=str(document.uuid), fields=changed)
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
    document = get_document(doc_id)
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
    image = crypto_fields.get_thumbnail(document)
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
        "backend": document.ocr_backend or str(settings.OCR_BACKEND),
    }


@router.get("/{doc_id}/structure", response=StructureOut)
def document_structure(request: HttpRequest, doc_id: UUID) -> dict[str, object]:
    document = get_document(doc_id)
    return {
        "backend": document.ocr_backend or str(settings.OCR_BACKEND),
        "data": crypto_fields.get_structure(document),
    }


@router.post("/{doc_id}/reprocess", response=DocumentDetail)
def reprocess(request: HttpRequest, doc_id: UUID, data: ReprocessIn) -> DocumentDetail:
    document = get_document(doc_id)
    if Job.objects.filter(
        document=document, kind=Job.Kind.PROCESS_DOCUMENT, state__in=[Job.State.QUEUED, Job.State.RUNNING]
    ).exists():
        raise HttpError(409, "Document is already being processed")
    stage: str = Document.Stage.OCR if data.stage == "ocr" else Document.Stage.ANALYZE
    if data.backend is not None:
        if data.stage != "ocr":
            raise HttpError(422, "An OCR backend can only be selected when reprocessing OCR")
        document.ocr_backend = data.backend
        document.save(update_fields=["ocr_backend"])
    if document.processing_state == Document.State.FAILED and document.processing_stage in (
        Document.Stage.VALIDATE,
        Document.Stage.OCR,
        Document.Stage.STORE,
        Document.Stage.INDEX,
    ):
        stage = document.processing_stage  # retry where it failed
    pipeline.restart_from(document, stage)
    queue.enqueue(Job.Kind.PROCESS_DOCUMENT, document=document)
    audit("document.reprocess", request=request, target=str(document.uuid), stage=stage)
    return to_detail(get_document(doc_id))


@router.delete("/{doc_id}")
def delete_document(request: HttpRequest, doc_id: UUID) -> dict[str, bool]:
    document = get_document(doc_id)
    if Job.objects.filter(document=document, state=Job.State.RUNNING).exists():
        raise HttpError(409, "The document is being processed right now. Try again in a moment.")
    folder = None
    if document.storage_original:
        ref_path = str(document.storage_original.get("path", ""))
        if "/" in ref_path:
            folder = ref_path.rsplit("/", 1)[0]
            if document.storage_original.get("backend") == "proton":
                root = str(settings.PROTON_ROOT).rstrip("/") + "/"
                folder = folder[len(root) :] if folder.startswith(root) else folder
    uuid = str(document.uuid)
    remove_intake(document)
    archive_intake_path_for(uuid).unlink(missing_ok=True)
    files.evict(document)
    with transaction.atomic():
        # Hard delete: content, thumbnail, index terms, tags and events cascade.
        document.delete()
        if folder:
            queue.enqueue(Job.Kind.DELETE_STORAGE, payload={"folder": folder})
    audit("document.deleted", request=request, target=uuid)
    return {"ok": True}
