"""Database backups: `pg_dump` uploaded to the storage backend.

The worker dumps the whole database in PostgreSQL's custom format (compressed,
restorable with `pg_restore`) and uploads it to `<root>/backups/` next to the
documents. Sensitive values in the database are already encrypted with keys
derived from the master key, so a dump is as protected as the database itself;
the master key is never part of a backup.

Uploaded backups are recorded in `SystemState` so old ones can be removed
without listing the remote folder. Only the newest `BACKUP_KEEP` are kept.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from apps.processing.models import Job, SystemState
from apps.storage.backends import StorageAuthError, StorageError, StoredObject, get_backend

logger = logging.getLogger(__name__)

STATE_KEY = "database_backup"
FOLDER = "backups"
# After a failed backup, wait this long before the worker schedules the next one.
RETRY_AFTER = timedelta(hours=1)


class BackupError(Exception):
    pass


def get_state() -> dict:
    row = SystemState.objects.filter(key=STATE_KEY).first()
    return dict(row.value) if row else {}


def _save_state(state: dict) -> None:
    SystemState.objects.update_or_create(key=STATE_KEY, defaults={"value": state})


def _parse(value: object) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None


def is_due(now: datetime | None = None) -> bool:
    """True if automatic backups are enabled and the last one is older than the interval."""
    if settings.BACKUP_INTERVAL_HOURS <= 0:
        return False
    now = now or timezone.now()
    state = get_state()
    last_success = _parse(state.get("last_success_at"))
    if last_success and now - last_success < timedelta(hours=settings.BACKUP_INTERVAL_HOURS):
        return False
    last_attempt = _parse(state.get("last_attempt_at"))
    return not (state.get("last_error") and last_attempt and now - last_attempt < RETRY_AFTER)


def schedule() -> Job | None:
    """Queue a backup job unless one is already queued or running."""
    from apps.processing import queue

    active = Job.objects.filter(
        kind=Job.Kind.BACKUP_DATABASE, state__in=[Job.State.QUEUED, Job.State.RUNNING]
    ).exists()
    if active:
        return None
    return queue.enqueue(Job.Kind.BACKUP_DATABASE)


def dump(dest: Path) -> None:
    """Write a custom-format dump of the current database to `dest`."""
    db = connection.settings_dict
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PGPASSWORD": str(db.get("PASSWORD") or ""),
        "PGSSLMODE": str((db.get("OPTIONS") or {}).get("sslmode", "prefer")),
        "PGCONNECT_TIMEOUT": "30",
    }
    cmd = [
        "pg_dump",
        "--format=custom",
        "--compress=6",
        "--no-password",
        f"--host={db['HOST']}",
        f"--port={db['PORT'] or 5432}",
        f"--username={db['USER']}",
        f"--file={dest}",
        str(db["NAME"]),
    ]
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=settings.BACKUP_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise BackupError("pg_dump is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError(f"pg_dump timed out after {settings.BACKUP_TIMEOUT_SECONDS}s") from exc
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        raise BackupError(f"pg_dump failed: {lines[-1][:300] if lines else proc.returncode}")
    # A readable table of contents proves the archive is complete and well-formed.
    check = subprocess.run(
        ["pg_restore", "--list", str(dest)],
        capture_output=True,
        timeout=300,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if check.returncode != 0 or not dest.stat().st_size:
        raise BackupError("pg_dump produced an unreadable archive")


def run() -> StoredObject:
    """Dump the database, upload it and prune old backups. Records the outcome."""
    started = timezone.now()
    state = get_state()
    state["last_attempt_at"] = started.isoformat()
    _save_state(state)
    try:
        stored = _upload(started)
    except StorageAuthError:
        raise  # the worker defers the job until Proton Drive is logged in again
    except Exception as exc:
        state = get_state()
        state["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
        _save_state(state)
        raise
    state = get_state()
    backups = [*state.get("backups", []), {**stored.to_json(), "created_at": started.isoformat()}]
    state.update(
        last_success_at=started.isoformat(),
        last_error="",
        last_size=stored.size,
        backups=_prune(backups),
    )
    _save_state(state)
    logger.info("database backup stored", extra={"path": stored.path, "size": stored.size})
    return stored


def _upload(started: datetime) -> StoredObject:
    name = f"docnest-{started.strftime('%Y-%m-%dT%H%M%SZ')}.dump"
    settings.WORK_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=settings.WORK_DIR, prefix="backup-") as tmpdir:
        path = Path(tmpdir) / name
        dump(path)
        return get_backend().put(path, FOLDER, name)


def _prune(backups: list[dict]) -> list[dict]:
    """Delete all but the newest `BACKUP_KEEP` backups; keep entries whose deletion failed."""
    backups.sort(key=lambda b: b.get("created_at", ""))
    excess = len(backups) - settings.BACKUP_KEEP
    if excess <= 0:
        return backups
    backend = get_backend()
    kept: list[dict] = []
    for entry in backups[:excess]:
        try:
            if entry.get("backend") == backend.name:
                backend.delete_file(str(entry["path"]))
        except StorageError:
            logger.warning("could not delete old backup", extra={"path": entry.get("path")})
            kept.append(entry)
    return kept + backups[excess:]
