"""Supervise isolated document jobs, including native OCR and all its descendants.

Spawn (never fork a Django/PyTorch thread) and keep leases in the supervisor.
Only the supervisor settles cancellation, after the process group is gone.
"""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import signal
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import connection
from django.utils import timezone

POLL_SECONDS = 0.25
DOCUMENT_LOCK_NAMESPACE = 39826


def check_job(job: Any) -> None:
    from apps.processing import pipeline
    from apps.processing.models import Job

    current = (
        Job.objects.filter(
            pk=job.pk,
            state=Job.State.RUNNING,
            locked_by=job.locked_by,
            lease_token=job.lease_token,
            locked_until__gt=timezone.now(),
        )
        .values("cancel_requested")
        .first()
    )
    if current is None:
        raise pipeline.LeaseLost()
    if current["cancel_requested"]:
        raise pipeline.Cancelled()


def _child(job_id: int, lease_token: str, config: dict[str, Any], root: str, sender: Any) -> None:
    # setsid must happen before starting any libraries or subprocesses.
    os.setsid()
    import django

    for name, value in config.items():
        setattr(settings, name, value)
    settings.WORK_DIR = Path(root)
    native_tmp = Path(root) / "tmp"
    native_tmp.mkdir(mode=0o700)
    os.environ["TMPDIR"] = str(native_tmp)
    tempfile.tempdir = str(native_tmp)
    django.setup()

    from django.db import close_old_connections

    from apps.documents.models import Document
    from apps.processing import pipeline
    from apps.processing.models import Job

    stopped = threading.Event()
    parent = multiprocessing.parent_process()

    def guard() -> None:
        # An OOM-killed supervisor must not leave an orphan writing to the archive.
        while not stopped.wait(POLL_SECONDS):
            try:
                if parent is None or not parent.is_alive():
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                close_old_connections()
                if not Job.objects.filter(
                    pk=job_id,
                    lease_token=lease_token,
                    state=Job.State.RUNNING,
                    locked_until__gt=timezone.now(),
                ).exists():
                    os.killpg(os.getpgrp(), signal.SIGKILL)
            except Exception:
                # Fail closed: a disconnected process must not keep mutating files.
                os.killpg(os.getpgrp(), signal.SIGKILL)
            finally:
                close_old_connections()

    threading.Thread(target=guard, name="processing-guard", daemon=True).start()
    try:
        job = Job.objects.get(pk=job_id, lease_token=lease_token)
        if job.document_id is None:
            raise pipeline.PermanentError("Document processing job has no document")
        check_job(job)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [DOCUMENT_LOCK_NAMESPACE, job.document_id])
            if not cursor.fetchone()[0]:
                raise RuntimeError("Previous processing attempt is still stopping")
        if job.kind == Job.Kind.REINDEX_DOCUMENT:
            pipeline.reindex(Document.objects.get(pk=job.document_id))
        else:
            pipeline.run(
                job.document_id,
                intake_only=job.kind == Job.Kind.INTAKE_DOCUMENT,
                check=lambda: check_job(job),
            )
        sender.send(("ok", ""))
    except pipeline.Cancelled:
        sender.send(("cancelled", ""))
    except pipeline.LeaseLost:
        sender.send(("lost", ""))
    except pipeline.StorageUnavailable as exc:
        sender.send(("storage", str(exc)[:500]))
    except pipeline.AnalysisModelUnavailable as exc:
        sender.send(("analysis", str(exc)[:500]))
    except pipeline.PermanentError as exc:
        sender.send(("permanent", str(exc)[:500]))
    except Exception as exc:
        sender.send(("error", f"{type(exc).__name__}: {exc}"[:500]))
    finally:
        stopped.set()
        connection.close()  # releases the per-document advisory lock
        sender.close()


def _stop(process: Any) -> None:
    if process.pid is None:
        return
    with contextlib.suppress(ProcessLookupError):
        # Also kill OCR/Tesseract/Proton grandchildren, even if the Python child already exited.
        os.killpg(process.pid, signal.SIGKILL)
    if process.is_alive():
        process.kill()  # covers cancellation during spawn, before setsid
    process.join(timeout=5)


def run(job: Any) -> None:
    from apps.processing import pipeline
    from apps.processing.models import Job

    check_job(job)
    # Settings are copied in memory; secrets never go on a command line or into a temp file.
    config = {name: getattr(settings, name) for name in dir(settings) if name.isupper()}
    config["DATABASES"] = {"default": dict(connection.settings_dict)}
    settings.WORK_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    with tempfile.TemporaryDirectory(prefix=f"job-{job.pk}-", dir=settings.WORK_DIR) as root:
        process = ctx.Process(target=_child, args=(job.pk, str(job.lease_token), config, root, sender))
        stage = None
        started = time.monotonic()
        result = None
        try:
            process.start()
            sender.close()
            while True:
                check_job(job)
                current_stage = Job.objects.values_list("document__processing_stage", flat=True).get(
                    pk=job.pk
                )
                if current_stage != stage:
                    stage, started = current_stage, time.monotonic()
                limit = settings.PROCESSING_STAGE_TIMEOUT_SECONDS
                if stage == pipeline.Stage.ANALYZE:
                    limit = max(limit, settings.AI_TIMEOUT_SECONDS + 120)
                if time.monotonic() - started > limit:
                    raise TimeoutError(f"Processing stage {stage} exceeded {limit}s")
                if result is None and receiver.poll(POLL_SECONDS):
                    with contextlib.suppress(EOFError):
                        result = receiver.recv()
                if not process.is_alive():
                    break
                time.sleep(POLL_SECONDS)
            process.join(timeout=5)
            check_job(job)
            if process.exitcode != 0 or result is None:
                raise RuntimeError(
                    f"Processing subprocess stopped (exit {process.exitcode}); original preserved"
                )
            outcome, message = result
            exceptions = {
                "cancelled": pipeline.Cancelled,
                "lost": pipeline.LeaseLost,
                "storage": pipeline.StorageUnavailable,
                "analysis": pipeline.AnalysisModelUnavailable,
                "permanent": pipeline.PermanentError,
                "error": RuntimeError,
            }
            if outcome != "ok":
                raise exceptions[outcome](message)
        finally:
            _stop(process)
            sender.close()
            receiver.close()
            process.close()
