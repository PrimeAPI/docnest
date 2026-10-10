import logging
from datetime import date, timedelta

import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.documents import crypto_fields
from apps.documents.models import Document
from apps.processing import queue
from apps.processing.docling_backend import DoclingFailed, DoclingFieldResult, DoclingResult
from apps.processing.models import Job
from apps.processing.worker import Worker
from apps.storage import backends
from tests.conftest import upload
from tests.pdfs import (
    INSURANCE_LINES,
    INVOICE_LINES,
    javascript_pdf,
    scanned_letter_pdf,
    scanned_pdf,
    text_pdf,
)

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


@pytest.mark.ocr
def test_scanned_document_can_be_processed_with_docling(scanner, settings):
    _, token = scanner
    settings.OCR_BACKEND = "docling"
    doc_id = upload(Client(), token, scanned_pdf(INVOICE_LINES)).json()["id"]
    process_all()

    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == "done", doc.processing_error
    assert doc.ocr_backend == "docling"
    assert "Stromlieferung" in crypto_fields.get_content(doc)
    assert doc.correspondent and doc.correspondent.name == "Stadtwerke Musterstadt GmbH"
    assert "Stromlieferung" in crypto_fields.get_title(doc)
    structure = crypto_fields.get_structure(doc)
    assert structure.get("schema_name") == "DoclingDocument"
    assert structure.get("texts")


@pytest.mark.ocr
def test_docling_reads_two_column_letter_with_banded_subject(scanner, settings):
    _, token = scanner
    settings.OCR_BACKEND = "docling"
    doc_id = upload(Client(), token, scanned_letter_pdf()).json()["id"]
    process_all()

    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == "done", doc.processing_error
    extracted = crypto_fields.get_extracted(doc)
    assert doc.correspondent and doc.correspondent.name == "Nordlicht Versicherung"
    assert extracted["subject"] == "Hausratrechnung"
    assert doc.document_date == date(2025, 11, 1)
    assert extracted["amounts"] == ["120.00"]
    assert doc.document_type and doc.document_type.slug == "invoice"


@pytest.mark.ocr
def test_docling_keeps_exact_text_of_born_digital_pdfs(scanner, settings):
    _, token = scanner
    settings.OCR_BACKEND = "docling"
    doc_id = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    process_all()

    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == "done", doc.processing_error
    assert "DE89 3704 0044 0532 0130 00" in crypto_fields.get_content(doc)
    lines = crypto_fields.get_layout(doc)["pages"][0]["lines"]
    assert lines and not any(line["from_ocr"] for line in lines)
    assert doc.correspondent and doc.correspondent.name == "Stadtwerke Musterstadt GmbH"
    assert crypto_fields.get_extracted(doc)["ibans"] == ["DE89370400440532013000"]


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


def test_docling_stores_markdown_and_structure_encrypted(scanner, api, monkeypatch, settings):
    _, token = scanner
    settings.OCR_BACKEND = "docling"
    markdown = "# Electricity bill\n\n| Total | EUR 42.00 |"
    structure = {
        "schema_name": "DoclingDocument",
        "texts": [{"label": "section_header", "text": "Electricity bill"}],
        "tables": [{"cells": [{"text": "EUR 42.00"}]}],
    }
    layout = {
        "pages": [
            {
                "page_no": 1,
                "width": 595.0,
                "height": 842.0,
                "lines": [
                    {
                        "text": "Electricity bill",
                        "x0": 50.0,
                        "y0": 600.0,
                        "x1": 200.0,
                        "y1": 620.0,
                        "confidence": 1.0,
                        "from_ocr": False,
                    }
                ],
            }
        ]
    }

    def fake_convert(_src):
        return DoclingResult(markdown=markdown, structured=structure, layout=layout)

    monkeypatch.setattr("apps.processing.docling_backend.convert", fake_convert)
    doc_id = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    process_all()

    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == "done", doc.processing_error
    assert doc.ocr_backend == "docling"
    assert crypto_fields.get_content(doc) == markdown
    assert crypto_fields.get_structure(doc) == structure
    assert crypto_fields.get_layout(doc) == layout
    assert doc.content.format == "markdown"

    response = api.get(f"/api/v1/documents/{doc_id}/structure")
    assert response.status_code == 200
    assert response.json() == {"backend": "docling", "data": structure}

    with connection.cursor() as cursor:
        cursor.execute("SELECT text_enc, structured_enc, layout_enc FROM documents_documentcontent")
        stored = b"".join(bytes(value) for value in cursor.fetchone())
    assert b"Electricity bill" not in stored
    assert b"EUR 42.00" not in stored


def test_docling_vlm_mode_supplies_sender_and_title(scanner, monkeypatch, settings):
    _, token = scanner
    settings.OCR_BACKEND = "docling"
    settings.DOCLING_FIELD_DETECTION = "vlm"
    markdown = "Max Mustermann\nAcme Energy GmbH\nAnnual energy statement"
    layout = {
        "pages": [
            {
                "height": 842,
                "lines": [
                    {"text": "Max Mustermann", "y1": 720},
                    {"text": "Acme Energy GmbH", "y1": 600},
                    {"text": "Annual energy statement", "y1": 500},
                ],
            }
        ]
    }
    monkeypatch.setattr(
        "apps.processing.docling_backend.convert",
        lambda _src: DoclingResult(markdown=markdown, structured={"texts": []}, layout=layout),
    )
    calls = []

    def fake_extract(src):
        calls.append(src)
        return DoclingFieldResult(sender="Acme Energy GmbH", title="Annual energy statement")

    monkeypatch.setattr("apps.processing.docling_backend.extract_fields", fake_extract)
    doc_id = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    process_all()

    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == "done", doc.processing_error
    assert len(calls) == 1
    assert doc.correspondent and doc.correspondent.name == "Acme Energy GmbH"
    assert crypto_fields.get_title(doc).startswith("Annual energy statement")


def test_docling_hybrid_skips_vlm_for_confident_layout(scanner, monkeypatch, settings):
    _, token = scanner
    settings.OCR_BACKEND = "docling"
    settings.DOCLING_FIELD_DETECTION = "hybrid"
    sender = "Acme Energy GmbH - Energieweg 1 - 12345 Berlin"
    title = "Annual energy statement"
    structure = {
        "texts": [
            {"label": "page_header", "text": sender},
            {"label": "section_header", "text": title},
        ]
    }
    layout = {
        "pages": [
            {
                "height": 842,
                "lines": [
                    {"text": sender, "y1": 810},
                    {"text": title, "y1": 500},
                ],
            }
        ]
    }
    monkeypatch.setattr(
        "apps.processing.docling_backend.convert",
        lambda _src: DoclingResult(markdown=title, structured=structure, layout=layout),
    )

    def unexpected_vlm(_src):
        raise AssertionError("confident hybrid detection must not invoke the VLM")

    monkeypatch.setattr("apps.processing.docling_backend.extract_fields", unexpected_vlm)
    doc_id = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    process_all()

    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == "done", doc.processing_error
    assert doc.correspondent and doc.correspondent.name == "Acme Energy GmbH"
    assert crypto_fields.get_title(doc).startswith(title)


def test_docling_vlm_failure_falls_back_without_failing_document(scanner, monkeypatch, settings):
    _, token = scanner
    settings.OCR_BACKEND = "docling"
    settings.DOCLING_FIELD_DETECTION = "vlm"
    sender = "Fallback Energy GmbH - Weg 1 - 12345 Berlin"
    title = "Fallback annual statement"
    structure = {
        "texts": [
            {"label": "page_header", "text": sender},
            {"label": "section_header", "text": title},
        ]
    }
    layout = {
        "pages": [
            {
                "height": 842,
                "lines": [
                    {"text": sender, "y1": 810},
                    {"text": title, "y1": 500},
                ],
            }
        ]
    }
    monkeypatch.setattr(
        "apps.processing.docling_backend.convert",
        lambda _src: DoclingResult(markdown=title, structured=structure, layout=layout),
    )

    def failed_vlm(_src):
        raise DoclingFailed("model unavailable")

    monkeypatch.setattr("apps.processing.docling_backend.extract_fields", failed_vlm)
    doc_id = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    process_all()

    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == "done", doc.processing_error
    assert doc.correspondent and doc.correspondent.name == "Fallback Energy GmbH"
    assert doc.events.filter(stage="analyze", outcome="warning").exists()


def test_javascript_is_stripped_from_the_archive_and_the_uploaded_original_is_preserved(
    scanner, isolated_dirs
):
    _, token = scanner
    uploaded = javascript_pdf()
    upload(Client(), token, uploaded)
    process_all()
    doc = Document.objects.get()
    assert doc.processing_state == "done", doc.processing_error
    original = next((isolated_dirs / "storage").rglob("original.pdf")).read_bytes()
    archive = next((isolated_dirs / "storage").rglob("archive.pdf")).read_bytes()
    assert original == uploaded
    assert b"/JavaScript" not in archive and b"app.alert" not in archive


def test_reprocessing_does_not_rewrite_the_stored_original(scanner, api, isolated_dirs, monkeypatch):
    _, token = scanner
    uploaded = text_pdf(INVOICE_LINES)
    doc_id = upload(Client(), token, uploaded).json()["id"]
    process_all()
    original_path = next((isolated_dirs / "storage").rglob("original.pdf"))
    assert original_path.read_bytes() == uploaded
    backend = backends.get_backend()
    put = backend.put
    written = []

    def record_put(source, folder, filename):
        written.append(filename)
        return put(source, folder, filename)

    monkeypatch.setattr(backend, "put", record_put)
    monkeypatch.setattr("apps.processing.pipeline.get_backend", lambda: backend)
    r = api.post(f"/api/v1/documents/{doc_id}/reprocess", {}, content_type="application/json")
    assert r.status_code == 200, r.content
    process_all()
    assert original_path.read_bytes() == uploaded
    assert written == ["archive.pdf"]


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
    job = Job.objects.get(document=doc, kind=Job.Kind.PROCESS_DOCUMENT)
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


def test_running_job_lease_can_be_renewed(scanner, settings):
    _, token = scanner
    settings.JOB_LEASE_SECONDS = 60
    upload(Client(), token, text_pdf(INVOICE_LINES))
    job = queue.claim("slow-worker")
    assert job is not None
    previous = job.locked_until
    assert queue.renew(job)
    job.refresh_from_db()
    assert job.locked_until and previous and job.locked_until > previous


def test_stale_worker_cannot_finalize_job_owned_by_another_worker(scanner):
    _, token = scanner
    upload(Client(), token, text_pdf(INVOICE_LINES))
    stale = queue.claim("first-worker")
    assert stale is not None
    Job.objects.filter(pk=stale.pk).update(locked_by="replacement-worker")

    assert queue.retry_or_fail(stale, "late failure") is None
    assert not queue.complete(stale)
    job = Job.objects.get(pk=stale.pk)
    assert job.state == Job.State.RUNNING
    assert job.locked_by == "replacement-worker"


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


def test_delete_keeps_originals_permanently_and_restore_makes_them_searchable(scanner, api, isolated_dirs):
    _, token = scanner
    doc_id = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    process_all()
    assert api.delete(f"/api/v1/documents/{doc_id}").status_code == 200
    process_all()
    assert list((isolated_dirs / "storage").rglob("*.pdf"))  # in the trash: files untouched
    assert api.get("/api/v1/documents", {"q": "Stromlieferung"}).json()["total"] == 0
    assert api.delete(f"/api/v1/alterations/trash/{doc_id}?confirm=delete").status_code == 404
    assert Document.objects.exists()
    assert list((isolated_dirs / "storage").rglob("*.pdf"))
    restored = api.post("/api/v1/alterations/restore", {"ids": [doc_id]}, content_type="application/json")
    assert restored.status_code == 200, restored.content
    assert api.get("/api/v1/documents", {"q": "Stromlieferung"}).json()["total"] == 1


def test_jobs_of_a_dead_worker_are_released_at_once(scanner):
    from apps.processing.models import WorkerHeartbeat

    _, token = scanner
    upload(Client(), token, text_pdf(INVOICE_LINES))
    job = queue.claim("crashed-worker")  # lease valid for JOB_LEASE_SECONDS
    assert job is not None
    WorkerHeartbeat.objects.create(
        worker_id="crashed-worker", last_seen_at=timezone.now() - timedelta(minutes=5)
    )
    WorkerHeartbeat.objects.create(worker_id="busy-worker", last_seen_at=timezone.now())
    other = Job.objects.create(
        kind=Job.Kind.REINDEX_DOCUMENT,
        document=job.document,
        state="running",
        locked_by="busy-worker",
        locked_until=timezone.now() + timedelta(minutes=30),
    )

    assert queue.release_orphans() == 1
    assert Job.objects.get(pk=other.pk).locked_until > timezone.now()  # a live worker keeps its job
    process_all()  # the released job is taken over and finished
    assert Document.objects.get().processing_state == "done"
