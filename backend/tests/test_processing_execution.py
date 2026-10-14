"""Exercise the actual spawned supervisor, not just cooperative stage boundaries."""

from __future__ import annotations

import multiprocessing
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.db import close_old_connections, connection, connections
from django.test import Client
from django.utils import timezone

from apps.documents import files
from apps.documents.models import Document
from apps.processing import execution, pipeline, queue
from apps.processing.models import Job
from apps.processing.worker import Worker
from tests.conftest import upload
from tests.pdfs import INVOICE_LINES, text_pdf
from tests.processing_children import (
    _blocking_child,
    _crashing_child,
    _quick_child,
    _supervisor_child,
)

pytestmark = pytest.mark.django_db(transaction=True)
J = "application/json"


def _execute(worker, job):
    close_old_connections()
    try:
        worker.execute(job)
    finally:
        connections.close_all()


def _wait_started(directory: Path) -> int:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if (directory / "started").exists():
            return int((directory / "started").read_text())
        time.sleep(0.05)
    pytest.fail("Isolated processing never started")


def _assert_stopped(pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = Path(f"/proc/{pid}/stat")
        if not status.exists() or status.read_text().split()[2] == "Z":
            return
        time.sleep(0.05)
    pytest.fail("OCR grandchild survived cancellation")


@pytest.mark.parametrize("stage", pipeline.PROCESSING_STAGES)
def test_cancel_kills_running_stage_and_descendants_and_keeps_original(
    scanner, api, isolated_dirs, monkeypatch, stage
):
    original = text_pdf(INVOICE_LINES)
    doc_id = upload(Client(), scanner[1], original).json()["id"]
    Worker().run_once()
    job = Job.objects.get(kind=Job.Kind.PROCESS_DOCUMENT)
    job.payload = {"block_stage": stage}
    job.save(update_fields=["payload"])
    job = queue.claim("supervisor")
    monkeypatch.setattr(execution, "_child", _blocking_child)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_execute, Worker(isolate_documents=True), job)
        pid = _wait_started(isolated_dirs)
        response = api.post("/api/v1/processing/queue/cancel", {"job_ids": [job.pk]}, content_type=J)
        assert response.json() == {"cancelled": 1}
        future.result(timeout=10)
    _assert_stopped(pid)
    job.refresh_from_db()
    assert job.state == Job.State.FAILED and job.last_error == "Cancelled"
    assert job.attempts == 1
    doc = Document.objects.get(uuid=doc_id)
    with_file = files.fetch(doc, "original")
    try:
        assert with_file.read() == original
    finally:
        with_file.close()
    assert not list((isolated_dirs / "work").glob("job-*"))
    monkeypatch.undo()
    assert api.post(f"/api/v1/documents/{doc_id}/reprocess", {}, content_type=J).status_code == 200
    Worker().run_until_empty()
    assert Document.objects.get(uuid=doc_id).processing_state == "done"


def test_hung_stage_times_out_and_releases_its_slot(scanner, isolated_dirs, settings, monkeypatch):
    upload(Client(), scanner[1], text_pdf(INVOICE_LINES))
    Worker().run_once()
    Job.objects.filter(kind=Job.Kind.PROCESS_DOCUMENT).update(payload={"block_stage": "ocr"}, max_attempts=1)
    job = queue.claim("supervisor")
    settings.PROCESSING_STAGE_TIMEOUT_SECONDS = 30  # allow interpreter startup under load
    monkeypatch.setattr(execution, "_child", _blocking_child)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_execute, Worker(isolate_documents=True), job)
        pid = _wait_started(isolated_dirs)
        settings.PROCESSING_STAGE_TIMEOUT_SECONDS = 2  # now bound the deliberately hung OCR stage
        future.result(timeout=10)
    _assert_stopped(pid)
    job.refresh_from_db()
    assert job.state == Job.State.FAILED
    assert "exceeded 2s" in job.last_error
    assert Document.objects.get().processing_state == "failed"


def test_real_spawned_pipeline_completes(scanner, api, isolated_dirs):
    doc_id = upload(Client(), scanner[1], text_pdf(INVOICE_LINES)).json()["id"]
    assert Worker(isolate_documents=True).run_until_empty() == 2
    assert Document.objects.get(uuid=doc_id).processing_state == "done"
    assert api.get("/api/v1/documents", {"q": "Stromlieferung"}).json()["total"] == 1
    assert not list((isolated_dirs / "work").glob("job-*"))


def test_crash_reclaims_are_bounded_and_other_jobs_continue(scanner):
    upload(Client(), scanner[1], text_pdf(INVOICE_LINES))
    job = queue.claim("crasher")
    for _ in range(job.max_attempts - 1):
        Job.objects.filter(pk=job.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
        job = queue.claim("crasher")
    Job.objects.filter(pk=job.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
    other = queue.enqueue(Job.Kind.TRAIN_CLASSIFIER)
    assert queue.claim("healthy").pk == other.pk
    job.refresh_from_db()
    assert job.attempts == job.max_attempts
    assert job.state == Job.State.FAILED
    assert "retry limit" in job.last_error
    assert Document.objects.get().processing_state == "failed"


def test_reclaimed_attempt_has_a_new_token_even_on_same_worker(scanner):
    upload(Client(), scanner[1], text_pdf(INVOICE_LINES))
    stale = queue.claim("same-worker")
    Job.objects.filter(pk=stale.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
    current = queue.claim("same-worker")
    assert stale.lease_token != current.lease_token
    assert not queue.renew(stale)
    assert not queue.complete(stale)
    assert queue.retry_or_fail(stale, "stale error") is None


def test_cancel_reserves_job_until_process_has_stopped(scanner, api, isolated_dirs, monkeypatch):
    doc_id = upload(Client(), scanner[1], text_pdf(INVOICE_LINES)).json()["id"]
    Worker().run_once()
    Job.objects.filter(kind=Job.Kind.PROCESS_DOCUMENT).update(payload={"block_stage": "ocr"})
    job = queue.claim("supervisor")
    monkeypatch.setattr(execution, "_child", _blocking_child)
    stopping, release = threading.Event(), threading.Event()
    stop = execution._stop

    def delayed_stop(process):
        stopping.set()
        try:
            assert release.wait(timeout=10)
        finally:
            stop(process)

    monkeypatch.setattr(execution, "_stop", delayed_stop)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_execute, Worker(isolate_documents=True), job)
        _wait_started(isolated_dirs)
        try:
            api.post("/api/v1/processing/queue/cancel", {"job_ids": [job.pk]}, content_type=J)
            assert stopping.wait(timeout=5)
            assert Job.objects.get(pk=job.pk).state == Job.State.RUNNING
            assert queue.enqueue(Job.Kind.PROCESS_DOCUMENT, document=job.document) is None
            assert api.post(f"/api/v1/documents/{doc_id}/reprocess", {}, content_type=J).status_code == 409
        finally:
            release.set()
        future.result(timeout=10)


def test_native_crashes_retry_then_fail_without_leaking_workspaces(scanner, isolated_dirs, monkeypatch):
    upload(Client(), scanner[1], text_pdf(INVOICE_LINES))
    Worker().run_once()
    Job.objects.filter(kind=Job.Kind.PROCESS_DOCUMENT).update(max_attempts=2)
    monkeypatch.setattr(execution, "_child", _crashing_child)
    worker = Worker(isolate_documents=True)
    assert worker.run_once()
    job = Job.objects.get(kind=Job.Kind.PROCESS_DOCUMENT)
    assert job.state == Job.State.QUEUED and job.attempts == 1
    Job.objects.filter(pk=job.pk).update(run_after=timezone.now())
    assert worker.run_once()
    job.refresh_from_db()
    assert job.state == Job.State.FAILED and job.attempts == 2
    assert "exit -9" in job.last_error
    assert not worker.run_once()
    assert not list((isolated_dirs / "work").glob("job-*"))


def test_lease_loss_stops_stale_process_without_overwriting_replacement(scanner, isolated_dirs, monkeypatch):
    upload(Client(), scanner[1], text_pdf(INVOICE_LINES))
    Worker().run_once()
    Job.objects.filter(kind=Job.Kind.PROCESS_DOCUMENT).update(payload={"block_stage": "ocr"})
    job = queue.claim("supervisor")
    monkeypatch.setattr(execution, "_child", _blocking_child)
    new_token = uuid.uuid4()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_execute, Worker(isolate_documents=True), job)
        pid = _wait_started(isolated_dirs)
        Job.objects.filter(pk=job.pk).update(lease_token=new_token, locked_by="replacement")
        future.result(timeout=10)
    _assert_stopped(pid)
    job.refresh_from_db()
    assert job.lease_token == new_token and job.state == Job.State.RUNNING
    assert job.last_error == ""


def test_supervisor_death_stops_children_and_workspace_recovery_is_safe(scanner, isolated_dirs):
    upload(Client(), scanner[1], text_pdf(INVOICE_LINES))
    Worker().run_once()
    Job.objects.filter(kind=Job.Kind.PROCESS_DOCUMENT).update(payload={"block_stage": "ocr"})
    job = queue.claim("doomed-supervisor")
    config = {name: getattr(django_settings, name) for name in dir(django_settings) if name.isupper()}
    config["DATABASES"] = {"default": dict(connection.settings_dict)}
    supervisor = multiprocessing.get_context("spawn").Process(target=_supervisor_child, args=(job.pk, config))
    supervisor.start()
    try:
        pid = _wait_started(isolated_dirs)
        runner_pid = int((isolated_dirs / "runner-pid").read_text())
        root = next((isolated_dirs / "work").glob("job-*"))
        Worker().sweep_workspace()  # another worker must not delete an active workspace
        assert root.exists()
        supervisor.kill()
        supervisor.join(timeout=5)
        _assert_stopped(runner_pid)
        _assert_stopped(pid)
        queue.release_orphans()
        Worker().sweep_workspace()
        assert not root.exists()
        assert queue.claim("replacement").pk == job.pk
    finally:
        if supervisor.is_alive():
            supervisor.kill()
        supervisor.join(timeout=5)
        supervisor.close()


def test_a_process_that_finishes_at_once_is_not_taken_for_a_crash(scanner, isolated_dirs, monkeypatch):
    """The supervisor reads the result even when the child sends it and exits in the same breath.

    Without draining the pipe after the process ends, such an attempt looks like a crashed
    subprocess, and a document that is already stored gets processed all over again.
    """
    upload(Client(), scanner[1], text_pdf(INVOICE_LINES))
    Worker().run_once()
    monkeypatch.setattr(execution, "_child", _quick_child)
    monkeypatch.setattr(execution, "POLL_SECONDS", 0)  # worst case: never wait on the pipe
    for _ in range(8):
        job = queue.claim("supervisor")
        assert job is not None
        execution.run(job)  # a lost result would raise "Processing subprocess stopped"
        Job.objects.filter(pk=job.pk).update(
            state=Job.State.QUEUED, locked_until=None, run_after=timezone.now(), attempts=0
        )


def test_page_by_page_stages_may_take_longer_for_a_long_document(settings):
    settings.PROCESSING_STAGE_TIMEOUT_SECONDS = 100
    settings.PROCESSING_PAGE_TIMEOUT_SECONDS = 10
    assert execution._stage_limit(pipeline.Stage.OCR, 1) == 100
    assert execution._stage_limit(pipeline.Stage.ENHANCE, 50) == 590
    assert execution._stage_limit(pipeline.Stage.ENHANCE, None) == 100
    # Stages that do not work page by page keep the flat limit; analysis waits for the model.
    assert execution._stage_limit(pipeline.Stage.STORE, 500) == 100
    assert execution._stage_limit(pipeline.Stage.ANALYZE, 1) == settings.AI_TIMEOUT_SECONDS + 120
