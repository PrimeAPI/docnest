"""Scan enhancement in the pipeline and its API: settings, one-off overrides, bulk reprocess."""

import io
import shutil

import pikepdf
import pytest
from django.test import Client
from PIL import Image

from apps.documents import files
from apps.documents.models import Document
from apps.processing import pdf
from apps.processing.worker import Worker
from tests.conftest import upload
from tests.pdfs import INSURANCE_LINES, INVOICE_LINES, page_image, text_pdf

pytestmark = pytest.mark.django_db(transaction=True)
J = "application/json"


@pytest.fixture
def no_ocr(monkeypatch):
    monkeypatch.setattr(pdf, "ocr", lambda src, dst, **_: bool(shutil.copyfile(src, dst)))


def skewed_scan(lines: list[str]) -> bytes:
    image = Image.open(io.BytesIO(page_image(lines, dpi=150)))
    skewed = image.rotate(3, expand=True, fillcolor=255)
    out = io.BytesIO()
    skewed.save(out, format="PDF", resolution=150)
    return out.getvalue()


def fetch(doc: Document, variant: str) -> bytes:
    handle = files.fetch(doc, variant)
    try:
        return handle.read()
    finally:
        handle.close()


def test_settings_are_shown_and_partially_updated(api):
    system = api.get("/api/v1/system").json()
    assert system["scan_enhancement"]["enabled"] is True
    assert system["scan_enhancement"]["cleanup_strength"] == "medium"
    r = api.put("/api/v1/settings/enhancement", {"deskew": False, "cleanup_strength": "low"}, content_type=J)
    assert r.status_code == 200
    assert r.json()["deskew"] is False and r.json()["cleanup_strength"] == "low"
    assert r.json()["crop"] is True  # untouched
    r = api.put("/api/v1/settings/enhancement", {"deskew_max_angle": 99}, content_type=J)
    assert r.status_code == 422
    assert api.get("/api/v1/system").json()["scan_enhancement"]["deskew"] is False


def test_enhanced_version_is_shown_and_original_kept(scanner, api, no_ocr):
    _, token = scanner
    doc_id = upload(Client(), token, skewed_scan(INVOICE_LINES)).json()["id"]
    Worker().run_until_empty()
    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == "done", doc.processing_error
    detail = api.get(f"/api/v1/documents/{doc_id}").json()
    assert detail["enhanced"] is True
    assert detail["enhancement"]["summary"]["deskewed"] == 1
    assert detail["enhancement"]["settings"]["deskew"] is True
    assert any(e["stage"] == "enhance" for e in detail["events"])
    original, archive = fetch(doc, "original"), fetch(doc, "archive")
    assert original != archive
    with pikepdf.open(io.BytesIO(original)) as a, pikepdf.open(io.BytesIO(archive)) as b:
        assert len(a.pages) == len(b.pages) == 1
    r = api.get(f"/api/v1/documents/{doc_id}/file", {"variant": "original", "download": "true"})
    assert r.status_code == 200 and b"".join(r.streaming_content) == original


def test_born_digital_pdf_is_not_enhanced(scanner, api, no_ocr):
    _, token = scanner
    doc_id = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    Worker().run_until_empty()
    detail = api.get(f"/api/v1/documents/{doc_id}").json()
    assert detail["enhanced"] is False
    assert detail["enhancement"]["summary"]["scanned_pages"] == 0


def test_reprocess_from_original_with_one_off_settings(scanner, api, no_ocr, tmp_path):
    _, token = scanner
    uploaded = skewed_scan(INVOICE_LINES)
    doc_id = upload(Client(), token, uploaded).json()["id"]
    Worker().run_until_empty()
    r = api.post(
        f"/api/v1/documents/{doc_id}/reprocess",
        {"stage": "ocr", "enhancement": {"enabled": False}},
        content_type=J,
    )
    assert r.status_code == 200, r.content
    Worker().run_until_empty()
    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == "done", doc.processing_error
    assert doc.enhancement["settings"]["enabled"] is False
    assert "override" not in doc.enhancement  # only for that one run
    assert fetch(doc, "original") == uploaded
    # Processing sanitizes the PDF container even when enhancement is disabled.
    # The rendered page must still be exactly the unenhanced uploaded scan.
    original_path, archive_path = tmp_path / "original.pdf", tmp_path / "archive.pdf"
    original_path.write_bytes(uploaded)
    archive_path.write_bytes(fetch(doc, "archive"))
    original_page = pdf.render_page(original_path, 1)
    assert original_page is not None
    assert pdf.render_page(archive_path, 1) == original_page
    # The system settings were not changed.
    assert api.get("/api/v1/system").json()["scan_enhancement"]["enabled"] is True

    # Reprocessing again uses the system settings.
    api.post(f"/api/v1/documents/{doc_id}/reprocess", {"stage": "ocr"}, content_type=J)
    Worker().run_until_empty()
    doc.refresh_from_db()
    assert doc.enhancement["settings"]["enabled"] is True
    assert fetch(doc, "original") == uploaded
    assert fetch(doc, "original") != fetch(doc, "archive")


def test_enhancement_options_require_reprocessing_from_the_original(scanner, api, no_ocr):
    _, token = scanner
    doc_id = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    Worker().run_until_empty()
    r = api.post(
        f"/api/v1/documents/{doc_id}/reprocess",
        {"stage": "analyze", "enhancement": {"deskew": False}},
        content_type=J,
    )
    assert r.status_code == 422


def test_bulk_reprocess(scanner, api, no_ocr):
    _, token = scanner
    ids = [
        upload(Client(), token, skewed_scan(INVOICE_LINES)).json()["id"],
        upload(Client(), token, skewed_scan(INSURANCE_LINES)).json()["id"],
    ]
    Worker().run_until_empty()
    r = api.post(
        "/api/v1/documents/bulk",
        {"ids": ids, "action": "reprocess", "reprocess": {"stage": "ocr", "enhancement": {"deskew": False}}},
        content_type=J,
    )
    assert r.status_code == 200 and r.json() == {"updated": 2}
    # Already queued documents are skipped instead of failing the whole action.
    again = api.post("/api/v1/documents/bulk", {"ids": ids, "action": "reprocess"}, content_type=J)
    assert again.json() == {"updated": 0}
    Worker().run_until_empty()
    for doc in Document.objects.filter(uuid__in=ids):
        assert doc.processing_state == "done", doc.processing_error
        assert doc.enhancement["settings"]["deskew"] is False
        assert doc.enhancement["summary"]["deskewed"] == 0


def test_reprocessed_archive_is_not_served_from_the_view_cache(scanner, api, no_ocr, settings):
    settings.VIEW_CACHE_MB = 50
    _, token = scanner
    doc_id = upload(Client(), token, skewed_scan(INVOICE_LINES)).json()["id"]
    Worker().run_until_empty()
    doc = Document.objects.get(uuid=doc_id)
    enhanced = fetch(doc, "archive")  # now cached
    api.post(
        f"/api/v1/documents/{doc_id}/reprocess",
        {"stage": "ocr", "enhancement": {"enabled": False}},
        content_type=J,
    )
    Worker().run_until_empty()
    doc.refresh_from_db()
    assert fetch(doc, "archive") != enhanced
