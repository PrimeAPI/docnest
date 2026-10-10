"""Background worker: claims jobs and runs periodic maintenance."""

from __future__ import annotations

import logging
import os
import select
import shutil
import signal
import socket
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import timedelta

import psycopg
from django.conf import settings
from django.db import close_old_connections, connection
from django.db.models import Q
from django.utils import timezone

from apps.analysis import ai, classifier
from apps.documents.intake import ensure_dirs
from apps.documents.models import Document
from apps.mail import inbox
from apps.processing import pipeline, queue
from apps.processing.models import Job, SystemState, WorkerHeartbeat
from apps.processing.preferences import (
    AI_MODEL_PULL_KEY,
    MAX_PROCESSING_CONCURRENCY,
    get_processing_concurrency,
)
from apps.storage import backup
from apps.storage.backends import StorageAuthError, get_backend

logger = logging.getLogger("docnest.worker")

HEALTHCHECK_INTERVAL = 600
TRAIN_INTERVAL = 120
CLEANUP_INTERVAL = 3600
HEARTBEAT_INTERVAL = 30
MAIL_CHECK_INTERVAL = 30  # how often to look whether the email inbox is due (its own interval: Settings)
# Each lane has its own slots. Processing is limited to the configured number of documents;
# intake (seconds per scan) has a small lane of its own, so new scans are taken in meanwhile.
LANES: dict[str, list[str]] = {
    "intake": [Job.Kind.INTAKE_DOCUMENT, Job.Kind.REINDEX_DOCUMENT],
    "process": [Job.Kind.PROCESS_DOCUMENT],
    "download": [Job.Kind.PULL_MODEL],
    # Short jobs the user waits for or that bring in new documents: never behind a batch.
    "assist": [Job.Kind.SUGGEST_FILING, Job.Kind.ASSIST],
    "mail": [Job.Kind.FETCH_MAIL],
    # Hours long: its own lane, so quick requests and new documents never wait for it.
    "review": [Job.Kind.REVIEW],
    "pages": [Job.Kind.PAGES],  # a few seconds per document; the whole archive once
}
INTAKE_SLOTS = 1
LANE_OF = {kind: lane for lane, kinds in LANES.items() for kind in kinds}
CONCURRENT_JOB_KINDS = list(LANE_OF)
MODEL_RETRY_SECONDS = 600
REVIEW_RETENTION_DAYS = 60  # reports of overnight reviews


class Worker:
    def __init__(self) -> None:
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}"
        self.stopping = False
        self._last_health = 0.0
        self._last_train = 0.0
        self._last_heartbeat = 0.0
        self._last_cleanup = 0.0
        self._last_mail = 0.0
        self._listen_conn: psycopg.Connection | None = None

    # -- lifecycle

    def stop(self, *_args: object) -> None:
        logger.info("worker stopping")
        self.stopping = True

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        ensure_dirs()
        self.sweep_workspace()
        logger.info("worker started", extra={"worker": self.worker_id})
        active: dict[str, set[Future[None]]] = {lane: set() for lane in LANES}
        executor = ThreadPoolExecutor(
            max_workers=MAX_PROCESSING_CONCURRENCY * 2 + 1,
            thread_name_prefix="docnest-job",
        )
        try:
            while not self.stopping:
                close_old_connections()
                self.periodic()
                active = {lane: self._reap(futures) for lane, futures in active.items()}
                concurrency = get_processing_concurrency()
                claimed = False

                # Start the oldest job first whenever no document work is active.
                # Maintenance jobs remain exclusive because classifier/storage
                # operations were designed for serial execution.
                if not any(active.values()):
                    job = queue.claim(self.worker_id)
                    if job is not None:
                        if job.kind in LANE_OF:
                            active[LANE_OF[job.kind]].add(executor.submit(self._execute_in_thread, job))
                        else:
                            self.execute(job)
                        claimed = True

                for lane, kinds in LANES.items():
                    limit = concurrency if lane == "process" else INTAKE_SLOTS if lane == "intake" else 1
                    while not self.stopping and len(active[lane]) < limit:
                        job = queue.claim(self.worker_id, kinds=kinds)
                        if job is None:
                            break
                        active[lane].add(executor.submit(self._execute_in_thread, job))
                        claimed = True
                if self.stopping or claimed:
                    continue
                running = set().union(*active.values())
                if running:
                    # Re-read the live concurrency setting promptly and fill slots as jobs finish.
                    wait(running, timeout=1, return_when=FIRST_COMPLETED)
                else:
                    self.wait_for_jobs(settings.WORKER_POLL_SECONDS)
        finally:
            # Deployments may wait a long time here intentionally: active Docling jobs are not aborted.
            executor.shutdown(wait=True, cancel_futures=False)
            if self._listen_conn:
                self._listen_conn.close()

    def _execute_in_thread(self, job: Job) -> None:
        close_old_connections()
        try:
            self.execute(job)
        finally:
            close_old_connections()

    def _reap(self, active: set[Future[None]]) -> set[Future[None]]:
        remaining: set[Future[None]] = set()
        for future in active:
            if not future.done():
                remaining.add(future)
                continue
            try:
                future.result()
            except Exception:
                # execute() handles job failures; this catches worker implementation failures.
                logger.exception("job thread crashed")
        return remaining

    def run_once(self) -> bool:
        job = queue.claim(self.worker_id)
        if job is None:
            return False
        self.execute(job)
        return True

    def run_until_empty(self, max_jobs: int = 1000) -> int:
        """Process jobs synchronously (used by tests and `docnest process`)."""
        n = 0
        while n < max_jobs and self.run_once():
            n += 1
        return n

    # -- jobs

    def execute(self, job: Job) -> None:
        logger.info("job started", extra={"job": job.pk, "kind": job.kind, "attempt": job.attempts})
        try:
            with self.maintain_lease(job):
                if job.kind == Job.Kind.INTAKE_DOCUMENT:
                    pipeline.run(job.document_id, intake_only=True)  # type: ignore[arg-type]
                elif job.kind == Job.Kind.PROCESS_DOCUMENT:
                    pipeline.run(job.document_id)  # type: ignore[arg-type]
                elif job.kind == Job.Kind.PULL_MODEL:
                    self.pull_model(job)
                elif job.kind == Job.Kind.DELETE_STORAGE:
                    self.delete_storage(job)
                elif job.kind == Job.Kind.TRAIN_CLASSIFIER:
                    classifier.train_all()
                elif job.kind == Job.Kind.REINDEX_DOCUMENT:
                    pipeline.reindex(Document.objects.get(pk=job.document_id or 0))
                elif job.kind == Job.Kind.BACKUP_DATABASE:
                    self.backup_database()
                elif job.kind == Job.Kind.SUGGEST_FILING:
                    from apps.taxonomy import filing

                    filing.run_proposal(int(job.payload.get("proposal", 0)))
                elif job.kind == Job.Kind.ASSIST:
                    from apps.assist import tasks

                    tasks.run(int(job.payload.get("task", 0)))
                elif job.kind == Job.Kind.PAGES:
                    from apps.documents import pages

                    pages.run(job.document_id or 0)
                elif job.kind == Job.Kind.REVIEW:
                    from apps.assist import review

                    review.run(int(job.payload.get("task", 0)))
                elif job.kind == Job.Kind.FETCH_MAIL:
                    inbox.fetch()
                else:
                    raise pipeline.PermanentError(f"unknown job kind {job.kind}")
        except pipeline.StorageUnavailable as exc:
            if not queue.defer(job, 600, str(exc)):
                logger.error("job ownership lost before deferral", extra={"job": job.pk})
                return
            self._mark_document(job, Document.State.PENDING, "Waiting for storage: " + str(exc))
            logger.warning("storage unavailable, job deferred", extra={"job": job.pk})
            return
        except pipeline.AnalysisModelUnavailable as exc:
            if not queue.defer(job, MODEL_RETRY_SECONDS, str(exc)):
                logger.error("job ownership lost before deferral", extra={"job": job.pk})
                return
            self._mark_document(job, Document.State.PENDING, "Waiting for the AI model: " + str(exc))
            logger.warning("AI model unavailable, job deferred", extra={"job": job.pk})
            return
        except pipeline.PermanentError as exc:
            if queue.retry_or_fail(job, str(exc), retryable=False) is None:
                logger.error("job ownership lost before failure", extra={"job": job.pk})
                return
            self._mark_document(job, Document.State.FAILED, str(exc))
            logger.warning("job failed permanently", extra={"job": job.pk, "error": str(exc)[:200]})
            return
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            failed = queue.retry_or_fail(job, message)
            if failed is None:
                logger.error("job ownership lost before retry", extra={"job": job.pk})
                return
            state = Document.State.FAILED if failed else Document.State.PENDING
            self._mark_document(job, state, message if failed else f"Retrying: {message}")
            logger.exception("job error", extra={"job": job.pk, "final": failed})
            return
        if not queue.complete(job):
            logger.error("job ownership lost before completion", extra={"job": job.pk})
            return
        logger.info("job done", extra={"job": job.pk})

    @contextmanager
    def maintain_lease(self, job: Job):
        """Renew the queue lease and worker heartbeat during long model inference."""
        stopped = threading.Event()
        interval = max(1.0, min(30.0, settings.JOB_LEASE_SECONDS / 3))

        def heartbeat() -> None:
            while not stopped.wait(interval):
                try:
                    close_old_connections()
                    if not queue.renew(job):
                        logger.error("job lease was lost", extra={"job": job.pk})
                        return
                    WorkerHeartbeat.objects.update_or_create(
                        worker_id=self.worker_id,
                        defaults={"last_seen_at": timezone.now()},
                    )
                except Exception:
                    logger.exception("could not renew job lease", extra={"job": job.pk})
                finally:
                    close_old_connections()

        thread = threading.Thread(target=heartbeat, name=f"lease-{job.pk}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=interval + 1)

    def _mark_document(self, job: Job, state: str, message: str) -> None:
        if job.kind in pipeline.DOCUMENT_JOB_KINDS and job.document_id:
            Document.objects.filter(pk=job.document_id).update(
                processing_state=state, processing_error=message[:500]
            )

    def delete_storage(self, job: Job) -> None:
        folder = job.payload.get("folder")
        if folder:
            get_backend().delete_folder(folder)

    def pull_model(self, job: Job) -> None:
        """Download an AI model into Ollama; progress is shown under Settings."""
        name = str(job.payload.get("model", ""))
        state: dict[str, object] = {"model": name, "status": "starting", "completed": 0, "total": 0}
        last_saved = 0.0

        def save(**changes: object) -> None:
            state.update(changes)
            SystemState.objects.update_or_create(key=AI_MODEL_PULL_KEY, defaults={"value": dict(state)})

        def progress(status: str, completed: object, total: object) -> None:
            nonlocal last_saved
            if time.monotonic() - last_saved < 1:
                return
            last_saved = time.monotonic()
            save(status=status, completed=completed or 0, total=total or 0)

        save()
        try:
            ai.pull(name, progress)
        except (ai.ModelUnavailable, ai.ModelFailed) as exc:
            save(status="failed", error=str(exc))
            raise pipeline.PermanentError(str(exc)) from exc
        save(status="done")

    def backup_database(self) -> None:
        try:
            backup.run()
        except StorageAuthError as exc:
            raise pipeline.StorageUnavailable(str(exc)) from exc

    # -- periodic maintenance

    def periodic(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat > HEARTBEAT_INTERVAL:
            WorkerHeartbeat.objects.update_or_create(
                worker_id=self.worker_id, defaults={"last_seen_at": timezone.now()}
            )
            WorkerHeartbeat.objects.filter(last_seen_at__lt=timezone.now() - timedelta(hours=1)).delete()
            self._last_heartbeat = now
            if released := queue.release_orphans():
                logger.warning("released jobs of a worker that stopped", extra={"jobs": released})
        if now - self._last_health > HEALTHCHECK_INTERVAL:
            self._last_health = now
            self.check_storage()
            if backup.is_due():
                backup.schedule()
        if now - self._last_mail > MAIL_CHECK_INTERVAL:
            self._last_mail = now
            if inbox.is_due():
                inbox.schedule()
        if now - self._last_cleanup > CLEANUP_INTERVAL:
            self._last_cleanup = now
            self.cleanup()
        if now - self._last_train > TRAIN_INTERVAL:
            self._last_train = now
            dirty = SystemState.objects.filter(key="classifier_dirty", value__dirty=True).exists()
            if dirty:
                SystemState.objects.filter(key="classifier_dirty").update(value={"dirty": False})
                queue.enqueue(Job.Kind.TRAIN_CLASSIFIER)

    def cleanup(self) -> None:
        """Retention: audit entries, login throttles, finished jobs, stale scan sessions, proposals."""
        from apps.accounts.models import LoginThrottle
        from apps.assist.models import AssistTask
        from apps.audit.models import AuditLog
        from apps.scanners import sessions
        from apps.taxonomy.models import FilingProposal

        sessions.cleanup()

        now = timezone.now()
        AuditLog.objects.filter(created_at__lt=now - timedelta(days=settings.AUDIT_RETENTION_DAYS)).delete()
        LoginThrottle.objects.filter(window_started_at__lt=now - timedelta(days=1)).delete()
        FilingProposal.objects.filter(created_at__lt=now - timedelta(days=1)).delete()
        AssistTask.objects.exclude(operation=AssistTask.Operation.REVIEW).filter(
            state__in=[AssistTask.State.DONE, AssistTask.State.FAILED, AssistTask.State.CANCELLED],
            finished_at__lt=now - timedelta(days=1),
        ).delete()
        AssistTask.objects.filter(
            operation=AssistTask.Operation.REVIEW,
            state__in=[AssistTask.State.DONE, AssistTask.State.FAILED, AssistTask.State.CANCELLED],
            finished_at__lt=now - timedelta(days=REVIEW_RETENTION_DAYS),
        ).delete()
        history_cutoff = now - timedelta(hours=settings.JOB_HISTORY_HOURS)
        Job.objects.filter(
            state__in=[Job.State.DONE, Job.State.FAILED],
        ).filter(
            Q(finished_at__lt=history_cutoff) | Q(finished_at__isnull=True, updated_at__lt=history_cutoff)
        ).delete()

    def check_storage(self) -> None:
        """Also keeps the Proton session fresh (token refresh on use)."""
        status = get_backend().healthcheck()
        previous = SystemState.objects.filter(key=pipeline.STORAGE_STATE_KEY).first()
        SystemState.objects.update_or_create(
            key=pipeline.STORAGE_STATE_KEY,
            defaults={
                "value": {
                    "ok": status.ok,
                    "needs_reauth": status.needs_reauth,
                    "message": status.message,
                    "checked_at": timezone.now().isoformat(),
                }
            },
        )
        if status.ok and previous and not previous.value.get("ok", True):
            # Storage is back: run deferred jobs now instead of waiting.
            Job.objects.filter(state=Job.State.QUEUED, last_error__icontains="proton").update(
                run_after=timezone.now()
            )

    def sweep_workspace(self) -> None:
        """Remove leftovers of crashed jobs (plaintext temp files must not linger)."""
        work = settings.WORK_DIR
        if not work.exists():
            return
        now = time.time()
        for child in work.iterdir():
            if child.name == "uploads":
                for f in child.iterdir():
                    if now - f.stat().st_mtime > 3600:
                        f.unlink(missing_ok=True)
                continue
            # doc-* workspaces belong to this (single) worker and are stale at startup;
            # anything else (e.g. web downloads) only once it is clearly abandoned.
            if not child.name.startswith("doc-") and now - child.stat().st_mtime < 3600:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    # -- notifications

    def wait_for_jobs(self, timeout: float) -> None:
        try:
            conn = self._listen()
            ready, _, _ = select.select([conn.fileno()], [], [], timeout)
            if ready:
                for _ in conn.notifies(timeout=0):
                    pass
        except (psycopg.Error, OSError, ValueError):
            logger.warning("LISTEN connection failed; falling back to polling")
            self._listen_conn = None
            time.sleep(min(timeout, 5))

    def _listen(self) -> psycopg.Connection:
        if self._listen_conn is None or self._listen_conn.closed:
            params = connection.get_connection_params()
            params.pop("cursor_factory", None)
            params.pop("context", None)
            params.pop("prepare_threshold", None)
            conn = psycopg.connect(**params, autocommit=True)
            conn.execute(f"LISTEN {queue.CHANNEL}")
            self._listen_conn = conn
        return self._listen_conn
