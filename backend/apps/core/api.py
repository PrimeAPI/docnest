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

from apps.audit.models import AuditLog
from apps.audit.service import audit
from apps.documents import crypto_fields
from apps.documents.models import Document
from apps.processing.models import Job, SystemState, WorkerHeartbeat
from apps.processing.preferences import (
    DoclingFieldDetection,
    OcrBackend,
    get_default_ocr_backend,
    get_docling_field_detection,
    get_enhance_settings,
    get_processing_concurrency,
    set_default_ocr_backend,
    set_docling_field_detection,
    set_enhance_settings,
    set_processing_concurrency,
)
from apps.processing.schemas import EnhanceSettingsIn, EnhanceSettingsOut

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


class StorageStatus(Schema):
    backend: str
    ok: bool | None
    needs_reauth: bool
    message: str
    checked_at: str | None


class SystemStatus(Schema):
    version: str
    default_ocr_backend: OcrBackend
    docling_field_detection: DoclingFieldDetection
    storage: StorageStatus
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
        worker_online=bool(last_seen and timezone.now() - last_seen < timedelta(minutes=2)),
        worker_last_seen=last_seen,
        processing_concurrency=get_processing_concurrency(),
        scan_enhancement=EnhanceSettingsOut(**get_enhance_settings().to_json()),
        queued_jobs=Job.objects.filter(state=Job.State.QUEUED).count(),
        running_jobs=Job.objects.filter(state=Job.State.RUNNING).count(),
        failed_jobs=Job.objects.filter(state=Job.State.FAILED).count(),
    )


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
        kind__in=[Job.Kind.PROCESS_DOCUMENT, Job.Kind.REINDEX_DOCUMENT],
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
