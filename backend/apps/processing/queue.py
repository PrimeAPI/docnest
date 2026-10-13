"""PostgreSQL-backed job queue.

Workers claim jobs with `SELECT ... FOR UPDATE SKIP LOCKED` and hold a lease
(`locked_until`). If a worker crashes, the lease expires and another worker
picks the job up again. New jobs trigger a NOTIFY so idle workers wake up
immediately.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.processing.models import Job

logger = logging.getLogger(__name__)
CHANNEL = "docnest_jobs"


BACKGROUND = -10  # priority of batch work (reprocessing many documents at once)


def enqueue(
    kind: str, *, document: object = None, payload: dict | None = None, delay: int = 0, priority: int = 0
) -> Job | None:
    """Queue a job. Returns None if an equivalent job is already active."""
    try:
        with transaction.atomic():
            job = Job.objects.create(
                kind=kind,
                document=document,  # type: ignore[misc]
                payload=payload or {},
                priority=priority,
                max_attempts=settings.JOB_MAX_ATTEMPTS,
                run_after=timezone.now() + timedelta(seconds=delay),
            )
    except IntegrityError:
        return None
    transaction.on_commit(_notify)
    return job


def _notify() -> None:
    with connection.cursor() as cursor:
        cursor.execute(f"NOTIFY {CHANNEL}")


def claim(worker_id: str, *, kinds: Sequence[str] | None = None) -> Job | None:
    now = timezone.now()
    with transaction.atomic():
        jobs = Job.objects.select_for_update(skip_locked=True).filter(
            Q(state=Job.State.QUEUED, run_after__lte=now)
            | Q(state=Job.State.RUNNING, locked_until__lt=now)  # expired lease: worker died
            | Q(state=Job.State.RUNNING, locked_until__isnull=True)
        )
        if kinds is not None:
            jobs = jobs.filter(kind__in=kinds)
        while True:
            job = jobs.order_by("-priority", "run_after", "id").first()
            if job is None:
                return None
            if job.state == Job.State.RUNNING and job.document_id:
                from apps.processing.execution import DOCUMENT_LOCK_NAMESPACE

                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_try_advisory_xact_lock(%s, %s)",
                        [DOCUMENT_LOCK_NAMESPACE, job.document_id],
                    )
                    if not cursor.fetchone()[0]:
                        jobs = jobs.exclude(pk=job.pk)
                        continue
            if job.cancel_requested:
                from apps.processing.pipeline import finish_cancellation

                finish_cancellation(job)
                continue
            if job.attempts >= job.max_attempts:
                message = "Worker stopped repeatedly; retry limit reached — use Reprocess to try again"
                Job.objects.filter(pk=job.pk).update(
                    state=Job.State.FAILED,
                    last_error=message,
                    finished_at=now,
                    locked_by="",
                    locked_until=None,
                    updated_at=now,
                )
                if job.document_id and job.kind in (Job.Kind.INTAKE_DOCUMENT, Job.Kind.PROCESS_DOCUMENT):
                    from apps.documents.models import Document

                    Document.objects.filter(pk=job.document_id).update(
                        processing_state=Document.State.FAILED,
                        processing_error=message,
                    )
                continue
            break
        job.state = Job.State.RUNNING
        job.attempts += 1
        job.started_at = now
        job.finished_at = None
        job.locked_by = worker_id
        job.lease_token = uuid.uuid4()
        job.locked_until = now + timedelta(seconds=settings.JOB_LEASE_SECONDS)
        job.save(
            update_fields=[
                "state",
                "attempts",
                "started_at",
                "finished_at",
                "locked_by",
                "lease_token",
                "locked_until",
                "updated_at",
            ]
        )
        return job


WORKER_SILENT_SECONDS = 120  # workers report every 30 s, also while a job runs


def release_orphans() -> int:
    """Make jobs claimable again whose worker died (e.g. killed for lack of memory).

    Their lease would hold them for up to JOB_LEASE_SECONDS, shown as running although
    nobody works on them; a worker that stopped reporting is not coming back for them.
    """
    from apps.processing.models import WorkerHeartbeat

    now = timezone.now()
    alive = WorkerHeartbeat.objects.filter(last_seen_at__gte=now - timedelta(seconds=WORKER_SILENT_SECONDS))
    return (
        Job.objects.filter(state=Job.State.RUNNING, locked_until__gt=now)
        .exclude(locked_by__in=alive.values("worker_id"))
        .update(locked_until=now, updated_at=now)
    )


def complete(job: Job) -> bool:
    now = timezone.now()
    updated = Job.objects.filter(
        pk=job.pk,
        state=Job.State.RUNNING,
        locked_by=job.locked_by,
        lease_token=job.lease_token,
        cancel_requested=False,
    ).update(
        state=Job.State.DONE,
        locked_by="",
        locked_until=None,
        last_error="",
        finished_at=now,
        updated_at=now,
    )
    return updated == 1


def renew(job: Job) -> bool:
    """Extend a running job's lease, provided this worker still owns it."""
    updated = Job.objects.filter(
        pk=job.pk,
        state=Job.State.RUNNING,
        locked_by=job.locked_by,
        lease_token=job.lease_token,
        locked_until__gt=timezone.now(),
    ).update(
        locked_until=timezone.now() + timedelta(seconds=settings.JOB_LEASE_SECONDS),
        updated_at=timezone.now(),
    )
    return updated == 1


def retry_or_fail(job: Job, error: str, *, retryable: bool = True, delay: int | None = None) -> bool | None:
    """Schedule a retry; return final-failure, or None if this worker lost ownership."""
    error = error[:500]
    if retryable and job.attempts < job.max_attempts:
        backoff = delay if delay is not None else min(3600, 30 * 2 ** (job.attempts - 1))
        updated = Job.objects.filter(
            pk=job.pk,
            state=Job.State.RUNNING,
            locked_by=job.locked_by,
            lease_token=job.lease_token,
            cancel_requested=False,
        ).update(
            state=Job.State.QUEUED,
            run_after=timezone.now() + timedelta(seconds=backoff),
            locked_by="",
            locked_until=None,
            started_at=None,
            finished_at=None,
            last_error=error,
            updated_at=timezone.now(),
        )
        return False if updated == 1 else None
    now = timezone.now()
    updated = Job.objects.filter(
        pk=job.pk,
        state=Job.State.RUNNING,
        locked_by=job.locked_by,
        lease_token=job.lease_token,
        cancel_requested=False,
    ).update(
        state=Job.State.FAILED,
        locked_by="",
        locked_until=None,
        last_error=error,
        finished_at=now,
        updated_at=now,
    )
    return True if updated == 1 else None


def defer(job: Job, seconds: int, reason: str) -> bool:
    """Re-queue without consuming an attempt (e.g. storage temporarily needs re-auth)."""
    updated = Job.objects.filter(
        pk=job.pk,
        state=Job.State.RUNNING,
        locked_by=job.locked_by,
        lease_token=job.lease_token,
        cancel_requested=False,
    ).update(
        state=Job.State.QUEUED,
        attempts=max(0, job.attempts - 1),
        run_after=timezone.now() + timedelta(seconds=seconds),
        locked_by="",
        locked_until=None,
        started_at=None,
        finished_at=None,
        last_error=reason[:500],
        updated_at=timezone.now(),
    )
    return updated == 1
