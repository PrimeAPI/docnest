from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from apps.core import debuglog
from apps.core.logging import RedactingFilter
from apps.processing import queue
from apps.processing.models import Job


@pytest.fixture
def debug_file(settings, tmp_path: Path):
    """The debug log handler as settings.py installs it, on a test logger."""
    settings.DEBUG_LOG = True
    directory = tmp_path / "debug-logs"
    handler = debuglog.DailyFileHandler(str(directory), keep_days=3)
    handler.setFormatter(debuglog.TextFormatter())
    handler.addFilter(RedactingFilter())
    log = logging.getLogger("docnest.debug")
    log.addHandler(handler)
    previous = log.level
    log.setLevel(logging.DEBUG)
    yield directory
    log.removeHandler(handler)
    log.setLevel(previous)
    handler.close()


def read_log(directory: Path) -> str:
    return "".join(p.read_text() for p in sorted(directory.glob("docnest-debug-*.log")))


def test_lines_carry_process_fields_and_are_redacted(debug_file):
    debuglog.set_role("worker")
    try:
        logging.getLogger("docnest.debug").debug(
            "job claimed", extra={"job": 7, "kind": "process_document", "note": "two words"}
        )
        logging.getLogger("docnest.debug").info("connecting with password=hunter2")
        logging.getLogger("docnest.debug").error(
            "processing was killed", extra={"exitcode": -9, "header": "Authorization: Bearer abc123"}
        )
    finally:
        debuglog.set_role("")
    text = read_log(debug_file)
    assert "DEBUG   [worker pid=" in text
    assert "docnest.debug: job claimed | job=7 kind=process_document note='two words'" in text
    assert "hunter2" not in text
    assert "exitcode=-9" in text
    assert "abc123" not in text
    files = list(debug_file.glob("docnest-debug-*.log"))
    assert len(files) == 1
    assert files[0].stat().st_mode & 0o777 == 0o600


def test_old_files_are_removed(debug_file):
    debug_file.mkdir(parents=True)
    old = debug_file / f"docnest-debug-{(datetime.now() - timedelta(days=10)):%Y-%m-%d}.log"
    recent = debug_file / f"docnest-debug-{(datetime.now() - timedelta(days=1)):%Y-%m-%d}.log"
    old.write_text("old\n")
    recent.write_text("recent\n")
    logging.getLogger("docnest.debug").info("hello")
    assert not old.exists()
    assert recent.exists()


def test_unwritable_directory_never_breaks_logging(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    handler = debuglog.DailyFileHandler(str(blocker / "logs"))
    handler.setFormatter(debuglog.TextFormatter())
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "message", None, None)
    handler.handle(record)  # reports to stderr instead of raising
    handler.close()


def test_secrets_are_masked_but_paths_shown():
    assert debuglog.safe_value("SECRET_KEY", "abcdef") == "set (6 chars)"
    assert debuglog.safe_value("OLLAMA_API_KEY", "") == "empty"
    assert (
        debuglog.safe_value("DOCNEST_MASTER_KEY_FILE", "/run/secrets/master_key") == "/run/secrets/master_key"
    )
    databases = debuglog.safe_value("DATABASES", {"default": {"HOST": "db", "PASSWORD": "pw"}})
    assert databases == {"default": {"HOST": "db", "PASSWORD": "set (2 chars)"}}
    assert debuglog.safe_value("PASSWORD_HASHERS", ["argon2"]) == ["argon2"]


def test_startup_writes_the_configuration_without_secrets(debug_file, settings, monkeypatch):
    settings.OLLAMA_API_KEY = "sk-very-secret-value"
    settings.DEBUG_LOG_DIR = debug_file
    monkeypatch.setenv("DOCNEST_OLLAMA_API_KEY", "sk-very-secret-value")
    monkeypatch.setenv("DOCNEST_OLLAMA_URL", "http://ollama:11434")
    try:
        debuglog.log_startup("worker")
    finally:
        debuglog.set_role("")
    text = read_log(debug_file)
    assert "===== DocNest worker started =====" in text
    assert "setting OCR_BACKEND = " in text
    assert "setting OLLAMA_API_KEY is set (20 chars), hidden" in text
    assert "env DOCNEST_OLLAMA_API_KEY is set (20 chars), hidden" in text
    assert "env DOCNEST_OLLAMA_URL = http://ollama:11434" in text
    assert "sk-very-secret-value" not in text
    assert settings.MASTER_KEY not in text


def test_startup_is_silent_unless_switched_on(settings, tmp_path, caplog):
    settings.DEBUG_LOG = False
    caplog.set_level(logging.DEBUG, logger="docnest.debug")
    debuglog.log_startup("web")
    assert "started" not in caplog.text


@pytest.mark.django_db
def test_queue_snapshot_says_why_a_job_waits():
    job = queue.enqueue(Job.Kind.BACKUP_DATABASE)
    assert job is not None
    Job.objects.filter(pk=job.pk).update(
        run_after=timezone.now() + timedelta(seconds=600), last_error="Ollama is not reachable"
    )
    info = queue.snapshot()
    assert info["queued"] == 1
    assert info["queued_due"] == 0
    jobs = info["jobs"]
    assert isinstance(jobs, list) and len(jobs) == 1
    entry = jobs[0]
    assert entry["job"] == job.pk
    assert str(entry["due"]).startswith("in ")
    assert entry["last_error"] == "Ollama is not reachable"


def test_request_log_omits_query_strings(rf, caplog):
    from django.http import HttpResponse

    caplog.set_level(logging.DEBUG, logger="docnest.request")
    middleware = debuglog.RequestLogMiddleware(lambda request: HttpResponse(status=204))
    middleware(rf.get("/api/v1/documents", {"q": "Steuerbescheid"}))
    middleware(rf.get("/api/v1/health"))
    assert "GET /api/v1/documents -> 204" in caplog.text
    assert "Steuerbescheid" not in caplog.text
    assert "health" not in caplog.text


def test_settings_add_a_file_handler_when_switched_on(monkeypatch, tmp_path):
    import importlib

    import docnest.settings as settings_module

    monkeypatch.setenv("DOCNEST_DEBUG_LOG", "true")
    monkeypatch.setenv("DOCNEST_DEBUG_LOG_DIR", str(tmp_path))
    try:
        reloaded = importlib.reload(settings_module)
        assert reloaded.LOGGING["handlers"]["debug_file"]["directory"] == str(tmp_path)
        assert reloaded.LOGGING["root"]["level"] == "DEBUG"
        assert reloaded.LOGGING["handlers"]["console"]["level"] == reloaded.LOG_LEVEL
        assert reloaded.MIDDLEWARE[0] == "apps.core.debuglog.RequestLogMiddleware"
    finally:
        monkeypatch.delenv("DOCNEST_DEBUG_LOG")
        monkeypatch.delenv("DOCNEST_DEBUG_LOG_DIR")
        importlib.reload(settings_module)
    assert os.environ.get("DOCNEST_DEBUG_LOG") is None
