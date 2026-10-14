"""Supervise isolated document jobs, including native OCR and all its descendants.

Spawn (never fork a Django/PyTorch thread) and keep leases in the supervisor.
Only the supervisor settles cancellation, after the process group is gone.
"""

from __future__ import annotations

import contextlib
import logging
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

logger = logging.getLogger(__name__)

POLL_SECONDS = 0.25
# Cancellation, lease and stage checks hit the database; once per second per running job is
# responsive enough and keeps a full queue of supervisors off the database.
DB_POLL_SECONDS = 1.0
# The child verifies its own lease less often than the supervisor polls: once per second is
# enough to stop an orphan, and it keeps the database load of a full queue low.
GUARD_POLL_SECONDS = 1.0
GUARD_MAX_FAILURES = 10  # ~10 s of failed lease checks before a process stops itself
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


def _oom_kills() -> int | None:
    """How often this container's memory limit has killed a process, as the kernel counts it.

    None when the counter cannot be read, so "no kill recorded" is never claimed on a guess.
    """
    for path in (Path("/sys/fs/cgroup/memory.events"), Path("/sys/fs/cgroup/memory/memory.oom_control")):
        try:
            for line in path.read_text().splitlines():
                name, _, value = line.partition(" ")
                if name == "oom_kill":
                    return int(value)
        except (OSError, ValueError):
            continue
    return None


def _peak_memory(pid: int) -> int:
    """The highest resident memory this process has reached, in bytes (0 if it cannot be read)."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return 0
    return 0


def _memory_limit() -> int:
    """The container's memory limit in bytes, or 0 when it is not limited or unreadable."""
    try:
        value = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        return 0 if value == "max" else int(value)
    except (OSError, ValueError):
        return 0


def _size(value: int) -> str:
    return f"{value / 1024**3:.1f} GB" if value >= 1024**3 else f"{round(value / 1024**2)} MB"


def _why_it_stopped(exitcode: int | None, stage: str | None, peak: int, oom_before: int | None) -> str:
    """Describe an attempt that ended without reporting anything, in the terms of the run.

    A killed process leaves no exception behind, so this is the only account of it the
    document will ever get: which step, how it died, how much memory it had taken, and
    whether the kernel says the container ran out.
    """
    from apps.processing import pipeline

    step = pipeline.stage_label(stage)
    if exitcode is not None and exitcode < 0:
        try:
            how = signal.Signals(-exitcode).name
        except ValueError:
            how = f"signal {-exitcode}"
        first = f"{step} was killed ({how})"
    else:
        first = f"{step} stopped unexpectedly (exit {exitcode})"
    if peak:  # 0 means the step ended before its memory could be read, not that it used none
        first += f" after reaching {_size(peak)}"
    parts = [first]
    oom_after = _oom_kills()
    if oom_before is not None and oom_after is not None and oom_after > oom_before:
        limit = _memory_limit()
        parts.append(
            f"the container ran out of memory (limit {_size(limit)})"
            if limit
            else "the container ran out of memory"
        )
        parts.append("give it more memory, or lower DOCNEST_PROCESSING_CONCURRENCY and DOCNEST_TMP_SIZE")
    elif exitcode == -signal.SIGKILL:
        parts.append("the container's memory limit recorded no kill, so look for it on the host (dmesg)")
    return f"{'; '.join(parts)}. The original is preserved — use Reprocess to try again."[:500]


def _record_failure(job: Any, stage: str | None, message: str) -> None:
    """Leave the account in the document's processing history, where a killed step leaves none."""
    from apps.documents.models import ProcessingEvent

    if not job.document_id:
        return
    with contextlib.suppress(Exception):  # diagnosing a failure must not create another one
        ProcessingEvent.objects.create(
            document_id=job.document_id, stage=stage or "", outcome="failed", message=message[:500]
        )


def _stage_limit(stage: str | None, pages: int | None) -> float:
    """How long a stage may run before the attempt is considered hung.

    Enhancement and text recognition work page by page, so a long document needs
    proportionally longer; the limit must bound a hung process, not a thick letter.
    """
    from apps.processing import pipeline

    limit = float(settings.PROCESSING_STAGE_TIMEOUT_SECONDS)
    if stage == pipeline.Stage.ANALYZE:
        return max(limit, settings.AI_TIMEOUT_SECONDS + 120)
    if stage in (pipeline.Stage.ENHANCE, pipeline.Stage.OCR):
        return limit + settings.PROCESSING_PAGE_TIMEOUT_SECONDS * max(0, (pages or 0) - 1)
    return limit


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
        failures = 0
        while not stopped.wait(GUARD_POLL_SECONDS):
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
                failures = 0
            except Exception:
                # Fail closed, but not on a hiccup: a database restart or a short network
                # outage must not kill a healthy job. Only a process that cannot confirm
                # its lease for a while must stop mutating files.
                failures += 1
                if failures >= GUARD_MAX_FAILURES:
                    os.killpg(os.getpgrp(), signal.SIGKILL)
            finally:
                # The guard itself must survive what it survives the job for.
                with contextlib.suppress(Exception):
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
        checked = 0.0
        peak = 0  # the child's own high-water mark, read while it is still alive
        oom_before = _oom_kills()
        try:
            process.start()
            sender.close()
            while True:
                # Ownership and progress come from the database; a full queue of supervisors
                # must not poll it four times a second each.
                if time.monotonic() - checked >= DB_POLL_SECONDS:
                    checked = time.monotonic()
                    if process.pid is not None:
                        peak = max(peak, _peak_memory(process.pid))
                    check_job(job)
                    current_stage, pages = Job.objects.values_list(
                        "document__processing_stage", "document__page_count"
                    ).get(pk=job.pk)
                    if current_stage != stage:
                        stage, started = current_stage, time.monotonic()
                    limit = _stage_limit(stage, pages)
                    if time.monotonic() - started > limit:
                        waited = f"{limit / 60:.0f} min" if limit >= 90 else f"{limit:g} s"
                        raise pipeline.Stopped(
                            f"{pipeline.stage_label(stage)} was still running after {waited} and was "
                            "stopped. The original is preserved — use Reprocess to try again."
                        )
                if result is None:
                    if receiver.poll(POLL_SECONDS):
                        with contextlib.suppress(EOFError):
                            result = receiver.recv()
                else:
                    time.sleep(POLL_SECONDS)
                if not process.is_alive():
                    break
            # The child may have sent its result in the moment it exited: read it before
            # judging the attempt, or a finished document would be reprocessed needlessly.
            if result is None and receiver.poll(POLL_SECONDS):
                with contextlib.suppress(EOFError):
                    result = receiver.recv()
            process.join(timeout=5)
            check_job(job)
            if result is None:
                # Nobody reported anything: the process was killed mid-step. Say which step,
                # and what the system knows about why, or the document fails without a reason.
                message = _why_it_stopped(process.exitcode, stage, peak, oom_before)
                logger.error(
                    "processing was killed",
                    extra={
                        "job": job.pk,
                        "document": job.document_id,
                        "stage": stage,
                        "exitcode": process.exitcode,
                        "peak_bytes": peak,
                    },
                )
                _record_failure(job, stage, message)
                raise pipeline.Stopped(message)
            if process.exitcode != 0:
                raise pipeline.Stopped(
                    f"{pipeline.stage_label(stage)} reported its result but then stopped "
                    f"(exit {process.exitcode}); the original is preserved"
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
