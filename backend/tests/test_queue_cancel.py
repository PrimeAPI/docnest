"""Cancellation preserves originals and protects intake."""

import pytest
from django.test import Client

from apps.documents.models import Document
from apps.processing import pipeline, queue
from apps.processing.models import Job
from apps.processing.worker import Worker
from tests.conftest import upload
from tests.pdfs import INVOICE_LINES, text_pdf

pytestmark = pytest.mark.django_db(transaction=True)
J = "application/json"


def process_all() -> None:
    Worker().run_until_empty()


def test_a_new_scan_can_be_cancelled_and_finished_later(scanner, api):
    _, token = scanner
    doc_id = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    Worker().run_once()  # intake cannot be cancelled; first preserve and validate the original
    job = Job.objects.get(kind=Job.Kind.PROCESS_DOCUMENT)
    queue_view = api.get("/api/v1/processing/queue").json()
    assert queue_view["queued_total"] == 1

    r = api.post("/api/v1/processing/queue/cancel", {"job_ids": [job.pk]}, content_type=J)
    assert r.json() == {"cancelled": 1}
    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == "failed" and doc.processing_error.startswith("Cancelled")
    assert Job.objects.get(pk=job.pk).state == "failed"
    process_all()  # nothing runs any more
    assert Document.objects.get(uuid=doc_id).processing_state == "failed"

    assert api.post(f"/api/v1/documents/{doc_id}/reprocess", {}, content_type=J).status_code == 200
    process_all()
    assert Document.objects.get(uuid=doc_id).processing_state == "done"


def test_cancelling_a_queued_reanalysis_leaves_the_document_as_it_was(scanner, api):
    _, token = scanner
    ids = [upload(Client(), token, text_pdf([*INVOICE_LINES, m])).json()["id"] for m in ("Januar", "Februar")]
    process_all()
    api.post("/api/v1/documents/reprocess-all", {"steps": ["analyze"]}, content_type=J)
    assert api.post("/api/v1/processing/queue/cancel", {}, content_type=J).json() == {"cancelled": 2}
    for doc_id in ids:
        doc = Document.objects.get(uuid=doc_id)
        assert (doc.processing_state, doc.processing_stage, doc.processing_error) == ("done", "done", "")
        assert doc.processing_plan == {}
    assert api.get("/api/v1/documents", {"q": "Januar"}).json()["total"] == 1  # still searchable


def test_running_jobs_stop_when_cancellation_is_acknowledged(scanner, api):
    _, token = scanner
    upload(Client(), token, text_pdf(INVOICE_LINES))
    Worker().run_once()
    running = queue.claim("worker")
    assert running is not None
    assert api.post("/api/v1/processing/queue/cancel", {}, content_type=J).json() == {"cancelled": 1}
    assert Job.objects.get(pk=running.pk).state == "running"
    assert api.get("/api/v1/processing/queue").json()["running"][0]["cancel_requested"]
    assert api.post("/api/v1/processing/queue/cancel", {}, content_type=J).json() == {"cancelled": 0}
    Worker().execute(running)
    assert Job.objects.get(pk=running.pk).last_error == "Cancelled"
    assert Document.objects.get().processing_state == "failed"


@pytest.mark.parametrize("running", [False, True])
def test_intake_is_never_cancellable(scanner, api, running):
    upload(Client(), scanner[1], text_pdf(INVOICE_LINES))
    if running:
        queue.claim("worker")
    item = api.get("/api/v1/processing/queue").json()["running" if running else "queued"][0]
    assert item["cancellable"] is False
    assert api.post("/api/v1/processing/queue/cancel", {"job_ids": [item["id"]]}, content_type=J).json() == {
        "cancelled": 0
    }
    assert not Job.objects.get(pk=item["id"]).cancel_requested


def test_cancel_before_indexing_does_not_claim_the_document_is_complete(scanner, api):
    upload(Client(), scanner[1], text_pdf(INVOICE_LINES))
    process_all()
    doc = Document.objects.get()
    pipeline.restart_from(doc, Document.Stage.INDEX)
    pipeline.enqueue(doc)
    assert api.post("/api/v1/processing/queue/cancel", {}, content_type=J).json() == {"cancelled": 1}
    doc.refresh_from_db()
    assert doc.processing_state == "failed"
    assert doc.processing_stage == "index"
    assert doc.storage_original  # verified original is still available
