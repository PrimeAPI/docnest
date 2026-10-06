from __future__ import annotations

import os
from datetime import datetime, timedelta

from django.conf import settings
from django.db import connection
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from ninja import Router, Schema

from apps.audit.models import AuditLog
from apps.audit.service import audit
from apps.documents.models import Document
from apps.processing.models import Job, SystemState, WorkerHeartbeat
from apps.processing.preferences import OcrBackend, get_default_ocr_backend, set_default_ocr_backend

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
    storage: StorageStatus
    worker_online: bool
    worker_last_seen: datetime | None
    queued_jobs: int
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
    default_ocr_backend: OcrBackend


class ProcessingSettingsOut(Schema):
    default_ocr_backend: OcrBackend


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
        storage=StorageStatus(
            backend=str(settings.STORAGE_BACKEND),
            ok=value.get("ok"),
            needs_reauth=bool(value.get("needs_reauth")),
            message=value.get("message", ""),
            checked_at=value.get("checked_at"),
        ),
        worker_online=bool(last_seen and timezone.now() - last_seen < timedelta(minutes=2)),
        worker_last_seen=last_seen,
        queued_jobs=Job.objects.filter(state=Job.State.QUEUED).count(),
        failed_jobs=Job.objects.filter(state=Job.State.FAILED).count(),
    )


@router.put("/settings/processing", response=ProcessingSettingsOut)
def update_processing_settings(request: HttpRequest, data: ProcessingSettingsIn) -> ProcessingSettingsOut:
    backend = set_default_ocr_backend(data.default_ocr_backend)
    audit("settings.processing_updated", request=request, default_ocr_backend=backend)
    return ProcessingSettingsOut(default_ocr_backend=backend)


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
