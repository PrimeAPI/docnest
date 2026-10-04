import logging
from datetime import date, timedelta

import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.documents import crypto_fields
from apps.documents.models import Document
from apps.processing import queue
from apps.processing.models import Job
from apps.processing.worker import Worker
from apps.storage import backends
from tests.conftest import upload
from tests.pdfs import INSURANCE_LINES, INVOICE_LINES, javascript_pdf, scanned_pdf, text_pdf

pytestmark = pytest.mark.django_db(transaction=True)


def process_all() -> None:
    Worker().run_until_empty()


@pytest.mark.ocr
def test_scanned_document_is_ocrd_stored_and_searchable(scanner, api, isolated_dirs, caplog):
    _, token = scanner
    caplog.set_level(logging.DEBUG)
    doc_id = upload(Client(), token, scanned_pdf(INSURANCE_LINES)).json()["id"]
    process_all()

    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == "done", doc.processing_error
    assert doc.page_count == 1
    text = crypto_fields.get_content(doc)
    assert "Fahrzeugversicherung" in text

    # stored permanently, intake cleared
    assert doc.storage_original and doc.storage_archive
    assert not list((isolated_dirs / "intake").iterdir())
    stored = list((isolated_dirs / "storage").rglob("*.pdf"))
    assert {p.name for p in stored} == {"original.pdf", "archive.pdf"}
    assert not list((isolated_dirs / "work").glob("doc-*"))  # temp files removed

    # search finds a word that exists only in the scanned image
    r = api.get("/api/v1/documents", {"q": "Fahrzeugversicherung"})
    assert r.status_code == 200 and r.json()["total"] == 1
    assert r.json()["items"][0]["snippet"]["highlights"]

    # analysis
    assert doc.correspondent and "Allsafe" in doc.correspondent.name
    assert doc.document_date == date(2026, 1, 2)
    tags = {t.name for t in doc.tags.all()}
    assert {"Vehicle", "Insurance"} <= tags

    # no OCR text in logs
    assert "Kraftfahrzeug" not in caplog.text
    assert "Versicherungsschein" not in caplog.text

    # file can be viewed
    r = api.get(f"/api/v1/documents/{doc_id}/file")
    assert r.status_code == 200 and b"".join(r.streaming_content).startswith(b"%PDF")
    assert not list((isolated_dirs / "work").glob("dl-*"))


def test_database_contains_no_plaintext_content(scanner):
    _, token = scanner
    upload(Client(), token, text_pdf(INVOICE_LINES))
    process_all()
    doc = Document.objects.get()
    assert doc.processing_state == "done", doc.processing_error
    with connection.cursor() as cursor:
        dump = []
        for table in (
            "documents_document",
            "documents_documentcontent",
            "documents_documentthumbnail",
            "search_searchterm",
            "search_documentstats",
            "analysis_classifiermodel",
            "processing_job",
            "documents_processingevent",
        ):
            cursor.execute(f"SELECT * FROM {table}")
            for row in cursor.fetchall():
                for value in row:
                    if isinstance(value, memoryview):
                        value = bytes(value)
                    dump.append(value.decode("latin-1") if isinstance(value, bytes) else str(value))
    blob = " ".join(dump)
    for secret_word in ("Stromlieferung", "Zaehlerstand", "RE-2026-0042", "DE89", "Beispielstrasse"):
        assert secret_word not in blob, secret_word


def test_javascript_is_stripped(scanner, isolated_dirs):
    _, token = scanner
    upload(Client(), token, javascript_pdf())
    process_all()
    doc = Document.objects.get()
    assert doc.processing_state == "done", doc.processing_error
    original = next((isolated_dirs / "storage").rglob("original.pdf")).read_bytes()
    assert b"/JavaScript" not in original and b"app.alert" not in original


def test_invalid_pdf_fails_visibly_without_losing_the_upload(scanner, api, isolated_dirs):
    _, token = scanner
    broken = b"%PDF-1.7\n" + b"garbage" * 100
    doc_id = upload(Client(), token, broken).json()["id"]
    process_all()
    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == "failed"
    assert "valid PDF" in doc.processing_error
    assert list((isolated_dirs / "intake").iterdir())  # still kept
    overview = api.get("/api/v1/overview").json()
    assert overview["failed"] == 1
    listing = api.get("/api/v1/documents", {"processing": "failed"}).json()
    assert listing["total"] == 1


def test_storage_outage_defers_without_loss(scanner, monkeypatch, isolated_dirs):
    _, token = scanner

    def broken_put(self, *a, **k):
        raise backends.StorageAuthError("Proton Drive session expired")

    monkeypatch.setattr(backends.LocalFilesystemBackend, "put", broken_put)
    upload(Client(), token, text_pdf(INVOICE_LINES))
    process_all()
    doc = Document.objects.get()
    assert doc.processing_state == "pending"
    assert doc.processing_error.startswith("Waiting for storage")
    job = Job.objects.get(document=doc)
    assert job.state == "queued" and job.attempts == 0  # attempt not consumed
    assert list((isolated_dirs / "intake").iterdir())

    monkeypatch.undo()
    Job.objects.update(run_after=timezone.now())
    process_all()
    doc.refresh_from_db()
    assert doc.processing_state == "done"
    assert not list((isolated_dirs / "intake").iterdir())


def test_crashed_worker_job_is_reclaimed_without_duplicates(scanner):
    _, token = scanner
    upload(Client(), token, text_pdf(INVOICE_LINES))
    job = queue.claim("dead-worker")
    assert job is not None
    # simulate a crash: lease expires without completion
    Job.objects.filter(pk=job.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
    process_all()
    assert Document.objects.count() == 1
    assert Document.objects.get().processing_state == "done"
    assert Job.objects.get(pk=job.pk).state == "done"


def test_retry_resumes_and_is_idempotent(scanner, monkeypatch):
    _, token = scanner
    from apps.processing import pipeline

    calls = {"n": 0}
    original = pipeline.stage_index

    def flaky(document, work):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("temporary failure")
        return original(document, work)

    monkeypatch.setitem(pipeline.STAGES, Document.Stage.INDEX, flaky)
    upload(Client(), token, text_pdf(INVOICE_LINES))
    process_all()
    doc = Document.objects.get()
    assert doc.processing_state == "pending" and doc.processing_stage == "index"
    Job.objects.update(run_after=timezone.now())
    process_all()
    doc.refresh_from_db()
    assert doc.processing_state == "done"
    stored = list(backends.LocalFilesystemBackend().root.rglob("*.pdf"))
    assert len(stored) == 2  # store stage did not run twice into new folders


def test_reprocess_keeps_user_edits(scanner, api):
    _, token = scanner
    doc_id = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    process_all()
    r = api.patch(f"/api/v1/documents/{doc_id}", {"title": "My power bill"}, content_type="application/json")
    assert r.status_code == 200
    r = api.post(f"/api/v1/documents/{doc_id}/reprocess", {"stage": "ocr"}, content_type="application/json")
    assert r.status_code == 200
    process_all()
    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == "done", doc.processing_error
    assert crypto_fields.get_title(doc) == "My power bill"
    assert api.get("/api/v1/documents", {"q": "power bill"}).json()["total"] == 1


def test_delete_removes_everything(scanner, api, isolated_dirs):
    _, token = scanner
    doc_id = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    process_all()
    assert api.delete(f"/api/v1/documents/{doc_id}").status_code == 200
    process_all()  # storage deletion job
    assert not Document.objects.exists()
    assert not list((isolated_dirs / "storage").rglob("*.pdf"))
    assert api.get("/api/v1/documents", {"q": "Stromlieferung"}).json()["total"] == 0
