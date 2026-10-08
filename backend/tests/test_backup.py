import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.processing import pipeline
from apps.processing.models import Job
from apps.processing.worker import Worker
from apps.storage import backup
from apps.storage.backends import StorageAuthError

pytestmark = pytest.mark.django_db


def stored_backups(storage: Path) -> list[Path]:
    return sorted((storage / "backups").glob("docnest-*.dump"))


def test_backup_dumps_the_database_into_storage(isolated_dirs, user):
    stored = backup.run()

    files = stored_backups(isolated_dirs / "storage")
    assert [f.name for f in files] == [Path(stored.path).name]
    toc = subprocess.run(["pg_restore", "--list", str(files[0])], capture_output=True, text=True, check=True)
    assert "TABLE DATA public accounts_user" in toc.stdout
    state = backup.get_state()
    assert state["last_error"] == "" and state["last_size"] == files[0].stat().st_size
    assert [b["path"] for b in state["backups"]] == [stored.path]
    assert not list((isolated_dirs / "work").glob("backup-*"))  # plaintext dump removed


def test_old_backups_are_pruned(isolated_dirs, settings, monkeypatch):
    settings.BACKUP_KEEP = 2
    start = timezone.now()
    for i in range(4):
        monkeypatch.setattr(backup, "timezone", SimpleNamespace(now=lambda i=i: start + timedelta(minutes=i)))
        backup.run()

    files = stored_backups(isolated_dirs / "storage")
    assert len(files) == 2
    assert [b["path"] for b in backup.get_state()["backups"]] == [f"backups/{f.name}" for f in files]
    assert files[-1].name == f"docnest-{(start + timedelta(minutes=3)).strftime('%Y-%m-%dT%H%M%SZ')}.dump"


def test_failure_is_recorded(isolated_dirs, monkeypatch):
    def broken(dest):
        raise backup.BackupError("pg_dump failed: connection refused")

    monkeypatch.setattr(backup, "dump", broken)
    with pytest.raises(backup.BackupError):
        backup.run()
    state = backup.get_state()
    assert "connection refused" in state["last_error"]
    assert "last_success_at" not in state
    assert not backup.is_due()  # waits before trying again
    assert backup.is_due(timezone.now() + backup.RETRY_AFTER + timedelta(minutes=1))


def test_due_after_interval(settings):
    assert backup.is_due()
    backup._save_state({"last_success_at": timezone.now().isoformat(), "last_error": ""})
    assert not backup.is_due()
    assert backup.is_due(timezone.now() + timedelta(hours=settings.BACKUP_INTERVAL_HOURS, minutes=1))
    settings.BACKUP_INTERVAL_HOURS = 0
    assert not backup.is_due(timezone.now() + timedelta(days=30))


def test_worker_schedules_and_runs_one_backup(isolated_dirs):
    worker = Worker()
    worker.check_storage = lambda: None  # type: ignore[method-assign]
    worker.periodic()
    worker._last_health = 0
    worker.periodic()  # already queued: no duplicate
    assert Job.objects.filter(kind=Job.Kind.BACKUP_DATABASE).count() == 1

    assert worker.run_until_empty() == 1
    assert Job.objects.get(kind=Job.Kind.BACKUP_DATABASE).state == Job.State.DONE
    assert len(stored_backups(isolated_dirs / "storage")) == 1
    worker._last_health = 0
    worker.periodic()
    assert not Job.objects.filter(state=Job.State.QUEUED).exists()  # not due again yet


def test_worker_defers_backup_while_storage_needs_login(monkeypatch):
    def needs_login(*args):
        raise StorageAuthError("Proton Drive session expired")

    monkeypatch.setattr(backup, "_upload", needs_login)
    with pytest.raises(pipeline.StorageUnavailable):
        Worker().backup_database()
    assert backup.get_state().get("last_error", "") == ""


def test_backup_command(isolated_dirs, capsys):
    call_command("backup")
    assert "Backup stored: backups/docnest-" in capsys.readouterr().out
    assert len(stored_backups(isolated_dirs / "storage")) == 1


def test_api_reports_and_starts_backups(api):
    status = api.get("/api/v1/system").json()["backup"]
    assert status["enabled"] is True and status["count"] == 0 and status["pending"] is False

    r = api.post("/api/v1/system/backup")
    assert r.status_code == 200 and r.json()["pending"] is True
    api.post("/api/v1/system/backup")
    assert Job.objects.filter(kind=Job.Kind.BACKUP_DATABASE).count() == 1

    Worker().run_until_empty()
    status = api.get("/api/v1/system").json()["backup"]
    assert status["count"] == 1 and status["last_success_at"] and status["pending"] is False
