"""Cancelling waiting jobs from the processing queue."""

import pytest
from django.test import Client

from apps.documents.models import Document
from apps.processing import queue
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


def test_running_jobs_are_not_cancelled(scanner, api):
    _, token = scanner
    upload(Client(), token, text_pdf(INVOICE_LINES))
    running = queue.claim("worker")
    assert running is not None
    assert api.post("/api/v1/processing/queue/cancel", {}, content_type=J).json() == {"cancelled": 0}
    assert Job.objects.get(pk=running.pk).state == "running"
