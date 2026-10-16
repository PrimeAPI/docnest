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
        logger.debug(
            "job not queued: an equivalent job is already active",
            extra={"kind": kind, "document": getattr(document, "pk", None)},
        )
        return None
    logger.debug(
        "job queued",
        extra={
            "job": job.pk,
            "kind": kind,
            "document": getattr(document, "pk", None),
            "priority": priority,
            "delay": delay,
        },
    )
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
                        logger.debug(
                            "expired job skipped: its previous attempt still holds the document",
                            extra={"job": job.pk, "document": job.document_id},
                        )
                        jobs = jobs.exclude(pk=job.pk)
                        continue
            if job.cancel_requested:
                from apps.processing.pipeline import finish_cancellation

                logger.debug("cancelled job settled instead of claimed", extra={"job": job.pk})
                finish_cancellation(job)
                continue
            if job.attempts >= job.max_attempts:
                message = "Worker stopped repeatedly; retry limit reached — use Reprocess to try again"
                logger.warning(
                    "job failed: retry limit reached when claiming",
                    extra={"job": job.pk, "kind": job.kind, "attempts": job.attempts},
                )
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
        logger.debug(
            "job claimed",
            extra={
                "job": job.pk,
                "kind": job.kind,
                "document": job.document_id,
                "attempt": job.attempts,
                "max_attempts": job.max_attempts,
                "priority": job.priority,
                "waited_s": int((now - job.created_at).total_seconds()),
                "reclaimed": bool(job.last_error) or job.attempts > 1,
            },
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
    released = (
        Job.objects.filter(state=Job.State.RUNNING, locked_until__gt=now)
        .exclude(locked_by__in=alive.values("worker_id"))
        .update(locked_until=now, updated_at=now)
    )
    return released


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
    logger.debug("job marked done", extra={"job": job.pk, "owned": updated == 1})
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
    if updated != 1:
        logger.debug("job lease could not be renewed", extra={"job": job.pk})
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
        logger.info(
            "job queued again for a retry",
            extra={
                "job": job.pk,
                "kind": job.kind,
                "attempt": job.attempts,
                "max_attempts": job.max_attempts,
                "retry_in_s": backoff,
                "owned": updated == 1,
                "error": error,
            },
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
    logger.info(
        "job failed",
        extra={
            "job": job.pk,
            "kind": job.kind,
            "attempt": job.attempts,
            "retryable": retryable,
            "owned": updated == 1,
            "error": error,
        },
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
    logger.info(
        "job deferred without using an attempt",
        extra={
            "job": job.pk,
            "kind": job.kind,
            "retry_in_s": seconds,
            "owned": updated == 1,
            "reason": reason,
        },
    )
    return updated == 1


def snapshot(limit: int = 20) -> dict[str, object]:
    """What is queued and running, and why queued jobs wait (for the debug log)."""
    now = timezone.now()
    active = Job.objects.filter(state__in=[Job.State.QUEUED, Job.State.RUNNING])
    jobs = []
    for job in active.order_by("state", "-priority", "run_after", "id")[:limit]:
        entry: dict[str, object] = {
            "job": job.pk,
            "kind": job.kind,
            "state": job.state,
            "document": job.document_id,
            "attempts": f"{job.attempts}/{job.max_attempts}",
            "priority": job.priority,
        }
        if job.state == Job.State.QUEUED:
            wait = (job.run_after - now).total_seconds()
            entry["due"] = "now" if wait <= 0 else f"in {int(wait)}s"
        else:
            entry["worker"] = job.locked_by
            entry["lease_left_s"] = (
                int((job.locked_until - now).total_seconds()) if job.locked_until else None
            )
        if job.cancel_requested:
            entry["cancel_requested"] = True
        if job.last_error:
            entry["last_error"] = job.last_error[:200]
        jobs.append(entry)
    return {
        "queued": active.filter(state=Job.State.QUEUED).count(),
        "queued_due": active.filter(state=Job.State.QUEUED, run_after__lte=now).count(),
        "running": active.filter(state=Job.State.RUNNING).count(),
        "jobs": jobs,
    }
