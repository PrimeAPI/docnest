"""Shared state of an export or import: a status file, a lock and the pause marker.

Exports and imports run in their own process (started by the web API or run
from the command line), one at a time. While one runs, the pause marker tells
the worker to start no jobs and the web API to refuse changes (an import:
every request), so the archive is a consistent snapshot and nothing writes
into a database that is being replaced.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from django.conf import settings
from django.utils import timezone

Operation = Literal["export", "import"]

STATUS_FILE = "status.json"
LOCK_FILE = ".lock"
PAUSE_FILE = "pause"
# How long a running job may keep an export or import waiting before it gives up.
JOB_WAIT_SECONDS = 120
STARTUP_GRACE = timedelta(seconds=60)


class TransferError(Exception):
    """An export or import cannot be done; the message is shown to the user."""


class Busy(TransferError):
    pass


def directory() -> Path:
    path = Path(settings.TRANSFER_DIR)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


# --- Status --------------------------------------------------------------------


def read_status() -> dict[str, Any]:
    try:
        status = json.loads((directory() / STATUS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    if status.get("state") == "running" and not running() and not _just_started(status):
        # The process ended without a word (killed, container restarted).
        status.update(state="failed", message="The process stopped unexpectedly. Please try again.")
    return status if isinstance(status, dict) else {}


def _just_started(status: dict[str, Any]) -> bool:
    """The process that takes the lock may not have started yet."""
    try:
        started = datetime.fromisoformat(str(status.get("started_at")))
    except ValueError:
        return False
    return timezone.now() - started < STARTUP_GRACE


def write_status(**changes: Any) -> dict[str, Any]:
    path = directory() / STATUS_FILE
    try:
        status = json.loads(path.read_text())
    except (OSError, ValueError):
        status = {}
    status.update(changes)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(status, f)
    os.replace(tmp, path)
    return status


def start_status(operation: Operation) -> None:
    """Reset the status for a new run (before the process that does the work is started)."""
    path = directory() / STATUS_FILE
    path.unlink(missing_ok=True)
    write_status(
        operation=operation,
        state="running",
        step="Starting",
        message="",
        warnings=[],
        started_at=timezone.now().isoformat(),
        finished_at=None,
    )


def progress(step: str) -> None:
    write_status(step=step)


# --- Lock & pause --------------------------------------------------------------


def running() -> bool:
    """True while an export or import holds the lock."""
    path = directory() / LOCK_FILE
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def paused() -> Operation | None:
    """The operation the instance is paused for, or None."""
    path = Path(settings.TRANSFER_DIR) / PAUSE_FILE
    if not path.exists():
        return None
    if not running():
        path.unlink(missing_ok=True)  # left behind by a process that was killed
        return None
    with contextlib.suppress(OSError):
        value = path.read_text().strip()
        if value in ("export", "import"):
            return value  # type: ignore[return-value]
    return "import"


@contextlib.contextmanager
def exclusive(operation: Operation) -> Iterator[None]:
    """Run an export or import: hold the lock, pause the instance and record the outcome."""
    fd = os.open(directory() / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        for _ in range(50):  # running() takes the lock for a moment to test it
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.1)
        else:
            raise Busy("An export or import is already running.")
        if read_status().get("state") != "running" or read_status().get("operation") != operation:
            start_status(operation)
        pause = directory() / PAUSE_FILE
        pause.write_text(operation)
        try:
            _wait_for_jobs()
            yield
        except Exception as exc:
            message = str(exc) if isinstance(exc, TransferError) else f"{type(exc).__name__}: {exc}"
            write_status(state="failed", message=message[:2000], finished_at=timezone.now().isoformat())
            raise
        else:
            write_status(state="done", step="Done", finished_at=timezone.now().isoformat())
        finally:
            pause.unlink(missing_ok=True)
    finally:
        os.close(fd)  # releases the lock


def _wait_for_jobs() -> None:
    """Running jobs change files and the database: let them finish first (no new ones start)."""
    from apps.processing.models import Job

    deadline = time.monotonic() + JOB_WAIT_SECONDS
    while True:
        running_jobs = Job.objects.filter(state=Job.State.RUNNING, locked_until__gt=timezone.now()).count()
        if not running_jobs:
            return
        if time.monotonic() > deadline:
            raise TransferError(
                f"{running_jobs} job{'s are' if running_jobs != 1 else ' is'} still running. "
                "Wait until processing has finished (Settings → Processing), or cancel it, and try again."
            )
        progress(f"Waiting for {running_jobs} running job{'s' if running_jobs != 1 else ''} to finish")
        time.sleep(2)


def cleanup_exports(max_age: timedelta = timedelta(hours=6)) -> None:
    """Exports are downloaded right away; old ones should not linger on the server."""
    cutoff = time.time() - max_age.total_seconds()
    for path in directory().glob("docnest-export-*.zip*"):
        with contextlib.suppress(OSError):
            if path.stat().st_mtime < cutoff:
                path.unlink()
