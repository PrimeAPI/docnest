from __future__ import annotations

import os
from datetime import datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.db import connection
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from ninja import Field, Router, Schema
from ninja.errors import HttpError

from apps.analysis import ai
from apps.audit.models import AuditLog
from apps.audit.service import audit
from apps.documents import crypto_fields
from apps.documents.models import Document
from apps.paper.services import pending as pending_paper
from apps.processing import queue
from apps.processing.models import Job, SystemState, WorkerHeartbeat
from apps.processing.preferences import (
    AI_MODEL_PULL_KEY,
    DoclingFieldDetection,
    OcrBackend,
    get_ai_model,
    get_default_ocr_backend,
    get_docling_field_detection,
    get_enhance_settings,
    get_processing_concurrency,
    set_ai_model,
    set_default_ocr_backend,
    set_docling_field_detection,
    set_enhance_settings,
    set_processing_concurrency,
)
from apps.processing.schemas import EnhanceSettingsIn, EnhanceSettingsOut
from apps.storage import backup

router = Router(tags=["system"])


class OverviewOut(Schema):
    total: int
    new: int
    unread: int
    todo: int
    important: int
    failed: int
    processing: int
    waiting_for_storage: int
    series_suggestions: int
    suggested_tags: int
    paper_pending: int  # paper originals not put away yet


class StorageStatus(Schema):
    backend: str
    ok: bool | None
    needs_reauth: bool
    message: str
    checked_at: str | None


class BackupStatus(Schema):
    enabled: bool
    interval_hours: int
    keep: int
    last_success_at: datetime | None
    last_attempt_at: datetime | None
    last_error: str
    last_size: int | None
    count: int  # backups currently kept in storage
    pending: bool  # a backup job is queued or running


class SystemStatus(Schema):
    version: str
    default_ocr_backend: OcrBackend
    docling_field_detection: DoclingFieldDetection
    storage: StorageStatus
    backup: BackupStatus
    worker_online: bool
    worker_last_seen: datetime | None
    processing_concurrency: int
    scan_enhancement: EnhanceSettingsOut
    queued_jobs: int
    running_jobs: int
    failed_jobs: int


class AuditOut(Schema):
    id: int
    created_at: datetime
    actor_type: str
    actor_label: str
    action: str
    target: str
    ip: str | None
    details: dict


class ProcessingSettingsIn(Schema):
    default_ocr_backend: OcrBackend | None = None
    docling_field_detection: DoclingFieldDetection | None = None
    processing_concurrency: int | None = Field(None, ge=1, le=8)


class ProcessingSettingsOut(Schema):
    default_ocr_backend: OcrBackend
    docling_field_detection: DoclingFieldDetection
    processing_concurrency: int


class AiModelOut(Schema):
    name: str
    size: int
    parameter_size: str
    vision: bool
    thinking: bool


class AiSuggestionOut(Schema):
    name: str
    description: str
    installed: bool


class AiPullOut(Schema):
    model: str
    status: str
    completed: int
    total: int
    error: str
    active: bool


class AiSettingsOut(Schema):
    configured: bool  # DOCNEST_OLLAMA_URL is set
    url: str
    reachable: bool
    error: str
    model: str  # "" = AI analysis off
    models: list[AiModelOut]
    suggestions: list[AiSuggestionOut]
    pull: AiPullOut | None


class AiSettingsIn(Schema):
    model: str = Field(..., max_length=200)


class AiPullIn(Schema):
    model: str = Field(..., max_length=200)


class ProcessingQueueItem(Schema):
    id: int
    document_id: UUID
    title: str
    state: str
    stage: str
    backend: str
    attempts: int
    error: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None


class ProcessingQueueOut(Schema):
    concurrency: int
    queued: list[ProcessingQueueItem]
    running: list[ProcessingQueueItem]
    history: list[ProcessingQueueItem]


@router.get("/health", auth=None, include_in_schema=False)
def health(request: HttpRequest) -> HttpResponse:
    """Liveness/readiness: database reachable. No information disclosure."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "error"}, status=503)
    return JsonResponse({"status": "ok"})


@router.get("/csrf", auth=None)
def csrf(request: HttpRequest) -> dict[str, bool]:
    """Ensures the CSRF cookie is set (the SPA calls this on startup)."""
    get_token(request)
    return {"ok": True}


@router.get("/overview", response=OverviewOut)
def overview(request: HttpRequest) -> OverviewOut:
    from apps.taxonomy.models import Tag

    agg = Document.objects.filter(deleted_at__isnull=True).aggregate(
        total=Count("id"),
        new=Count("id", filter=Q(status=Document.Status.NEW)),
        unread=Count("id", filter=Q(read_at__isnull=True)),
        todo=Count("id", filter=Q(status=Document.Status.TODO)),
        important=Count("id", filter=Q(is_important=True) & ~Q(status=Document.Status.DONE)),
        failed=Count("id", filter=Q(processing_state=Document.State.FAILED)),
        processing=Count(
            "id", filter=Q(processing_state__in=[Document.State.PENDING, Document.State.RUNNING])
        ),
        waiting=Count("id", filter=Q(processing_error__startswith="Waiting for storage")),
        suggestions=Count("id", filter=Q(series_suggestion__isnull=False)),
    )
    return OverviewOut(
        total=agg["total"],
        new=agg["new"],
        unread=agg["unread"],
        todo=agg["todo"],
        important=agg["important"],
        failed=agg["failed"],
        processing=agg["processing"],
        waiting_for_storage=agg["waiting"],
        series_suggestions=agg["suggestions"],
        suggested_tags=Tag.objects.filter(is_suggested=True).count(),
        paper_pending=pending_paper().count(),
    )


@router.get("/system", response=SystemStatus)
def system_status(request: HttpRequest) -> SystemStatus:
    storage = SystemState.objects.filter(key="storage_status").first()
    value = storage.value if storage else {}
    heartbeat = WorkerHeartbeat.objects.order_by("-last_seen_at").first()
    last_seen = heartbeat.last_seen_at if heartbeat else None
    return SystemStatus(
        version=os.environ.get("DOCNEST_VERSION", "dev"),
        default_ocr_backend=get_default_ocr_backend(),
        docling_field_detection=get_docling_field_detection(),
        storage=StorageStatus(
            backend=str(settings.STORAGE_BACKEND),
            ok=value.get("ok"),
            needs_reauth=bool(value.get("needs_reauth")),
            message=value.get("message", ""),
            checked_at=value.get("checked_at"),
        ),
        backup=_backup_status(),
        worker_online=bool(last_seen and timezone.now() - last_seen < timedelta(minutes=2)),
        worker_last_seen=last_seen,
        processing_concurrency=get_processing_concurrency(),
        scan_enhancement=EnhanceSettingsOut(**get_enhance_settings().to_json()),
        queued_jobs=Job.objects.filter(state=Job.State.QUEUED).count(),
        running_jobs=Job.objects.filter(state=Job.State.RUNNING).count(),
        failed_jobs=Job.objects.filter(state=Job.State.FAILED).count(),
    )


def _backup_status() -> BackupStatus:
    state = backup.get_state()
    return BackupStatus(
        enabled=settings.BACKUP_INTERVAL_HOURS > 0,
        interval_hours=settings.BACKUP_INTERVAL_HOURS,
        keep=settings.BACKUP_KEEP,
        last_success_at=state.get("last_success_at") or None,
        last_attempt_at=state.get("last_attempt_at") or None,
        last_error=state.get("last_error", ""),
        last_size=state.get("last_size"),
        count=len(state.get("backups", [])),
        pending=Job.objects.filter(
            kind=Job.Kind.BACKUP_DATABASE, state__in=[Job.State.QUEUED, Job.State.RUNNING]
        ).exists(),
    )


@router.post("/system/backup", response=BackupStatus)
def start_backup(request: HttpRequest) -> BackupStatus:
    """Queue a database backup now (the worker uploads it to storage)."""
    if backup.schedule() is not None:
        audit("system.backup_requested", request=request)
    return _backup_status()


@router.put("/settings/processing", response=ProcessingSettingsOut)
def update_processing_settings(request: HttpRequest, data: ProcessingSettingsIn) -> ProcessingSettingsOut:
    backend = (
        set_default_ocr_backend(data.default_ocr_backend)
        if data.default_ocr_backend is not None
        else get_default_ocr_backend()
    )
    detection = (
        set_docling_field_detection(data.docling_field_detection)
        if data.docling_field_detection is not None
        else get_docling_field_detection()
    )
    concurrency = (
        set_processing_concurrency(data.processing_concurrency)
        if data.processing_concurrency is not None
        else get_processing_concurrency()
    )
    audit(
        "settings.processing_updated",
        request=request,
        default_ocr_backend=backend,
        docling_field_detection=detection,
        processing_concurrency=concurrency,
    )
    return ProcessingSettingsOut(
        default_ocr_backend=backend,
        docling_field_detection=detection,
        processing_concurrency=concurrency,
    )


@router.put("/settings/enhancement", response=EnhanceSettingsOut)
def update_enhancement_settings(request: HttpRequest, data: EnhanceSettingsIn) -> EnhanceSettingsOut:
    changes = data.changes()
    updated = set_enhance_settings(changes)
    audit("settings.enhancement_updated", request=request, changes=changes)
    return EnhanceSettingsOut(**updated.to_json())


def _ai_settings() -> AiSettingsOut:
    models: list[ai.ModelInfo] = []
    reachable, error = False, ""
    if ai.configured():
        try:
            models = ai.list_models()
            reachable = True
        except (ai.ModelUnavailable, ai.ModelFailed) as exc:
            error = str(exc)
    else:
        error = "No Ollama server is configured. Set DOCNEST_OLLAMA_URL (see docs/operations.md)."
    installed = {m.name for m in models}
    pull_state = SystemState.objects.filter(key=AI_MODEL_PULL_KEY).values_list("value", flat=True).first()
    pull = None
    if isinstance(pull_state, dict) and pull_state.get("model"):
        active = Job.objects.filter(kind=Job.Kind.PULL_MODEL, state__in=[Job.State.QUEUED, Job.State.RUNNING])
        pull = AiPullOut(
            model=str(pull_state["model"]),
            status=str(pull_state.get("status", "")),
            completed=int(pull_state.get("completed") or 0),
            total=int(pull_state.get("total") or 0),
            error=str(pull_state.get("error", "")),
            active=active.exists(),
        )
    return AiSettingsOut(
        configured=ai.configured(),
        url=str(settings.OLLAMA_URL),
        reachable=reachable,
        error=error,
        model=get_ai_model(),
        models=[AiModelOut(**vars(m)) for m in models],
        suggestions=[
            AiSuggestionOut(name=name, description=description, installed=_installed(name, installed))
            for name, description in ai.SUGGESTED_MODELS
        ],
        pull=pull,
    )


def _installed(name: str, installed: set[str]) -> bool:
    return name in installed or (":" not in name and f"{name}:latest" in installed)


@router.get("/settings/ai", response=AiSettingsOut)
def ai_settings(request: HttpRequest) -> AiSettingsOut:
    return _ai_settings()


@router.put("/settings/ai", response=AiSettingsOut)
def update_ai_settings(request: HttpRequest, data: AiSettingsIn) -> AiSettingsOut:
    model = data.model.strip()
    if model and not ai.valid_model_name(model):
        raise HttpError(422, "Invalid model name")
    set_ai_model(model)
    audit("settings.ai_updated", request=request, model=model)
    return _ai_settings()


@router.post("/settings/ai/pull", response=AiSettingsOut)
def pull_ai_model(request: HttpRequest, data: AiPullIn) -> AiSettingsOut:
    model = data.model.strip()
    if not ai.valid_model_name(model):
        raise HttpError(422, "Invalid model name")
    if not ai.configured():
        raise HttpError(409, "No Ollama server is configured")
    if Job.objects.filter(kind=Job.Kind.PULL_MODEL, state__in=[Job.State.QUEUED, Job.State.RUNNING]).exists():
        raise HttpError(409, "A model is already being downloaded")
    SystemState.objects.update_or_create(
        key=AI_MODEL_PULL_KEY, defaults={"value": {"model": model, "status": "queued"}}
    )
    queue.enqueue(Job.Kind.PULL_MODEL, payload={"model": model})
    audit("settings.ai_model_pull", request=request, model=model)
    return _ai_settings()


def _queue_item(job: Job, now: datetime) -> ProcessingQueueItem:
    document = job.document
    if document is None:
        raise ValueError("Document processing jobs must reference a document")
    title = crypto_fields.get_title(document) or crypto_fields.get_original_filename(document) or "Document"
    end = job.finished_at or (now if job.state == Job.State.RUNNING else None)
    duration = (end - job.started_at).total_seconds() if job.started_at and end else None
    return ProcessingQueueItem(
        id=job.pk,
        document_id=document.uuid,
        title=title,
        state=job.state,
        stage=document.processing_stage,
        backend=document.ocr_backend or get_default_ocr_backend(),
        attempts=job.attempts,
        error=job.last_error,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        duration_seconds=round(duration, 1) if duration is not None else None,
    )


@router.get("/processing/queue", response=ProcessingQueueOut)
def processing_queue(request: HttpRequest) -> ProcessingQueueOut:
    document_jobs = Job.objects.filter(
        kind__in=[Job.Kind.PROCESS_DOCUMENT, Job.Kind.ANALYZE_DOCUMENT, Job.Kind.REINDEX_DOCUMENT],
        document__isnull=False,
    ).select_related("document")
    now = timezone.now()
    queued = document_jobs.filter(state=Job.State.QUEUED).order_by("run_after", "id")[:100]
    running = document_jobs.filter(state=Job.State.RUNNING).order_by("started_at", "id")[:100]
    history = document_jobs.filter(state__in=[Job.State.DONE, Job.State.FAILED]).order_by(
        "-finished_at", "-id"
    )[:50]
    return ProcessingQueueOut(
        concurrency=get_processing_concurrency(),
        queued=[_queue_item(job, now) for job in queued],
        running=[_queue_item(job, now) for job in running],
        history=[_queue_item(job, now) for job in history],
    )


@router.get("/audit", response=list[AuditOut])
def audit_log(request: HttpRequest, limit: int = 100, before: int | None = None) -> list[AuditOut]:
    qs = AuditLog.objects.all()
    if before:
        qs = qs.filter(pk__lt=before)
    return [
        AuditOut(
            id=a.pk,
            created_at=a.created_at,
            actor_type=a.actor_type,
            actor_label=a.actor_label,
            action=a.action,
            target=a.target,
            ip=a.ip,
            details=a.details,
        )
        for a in qs.order_by("-id")[: min(limit, 500)]
    ]
