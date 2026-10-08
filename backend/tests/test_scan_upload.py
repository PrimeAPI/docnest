"""Scanner uploads of raw page images and page-by-page scan sessions."""

import io
import shutil
from datetime import timedelta
from pathlib import Path

import pikepdf
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone
from PIL import Image

from apps.documents import crypto_fields, files
from apps.documents.models import Document
from apps.processing import assemble, pdf
from apps.processing.worker import Worker
from apps.scanners import sessions, tokens
from apps.scanners.models import ScannerClient, ScanSession
from tests.pdfs import INSURANCE_LINES, INVOICE_LINES, page_image, text_pdf

pytestmark = pytest.mark.django_db(transaction=True)

A4_POINTS = (595, 842)


@pytest.fixture
def no_ocr(monkeypatch):
    """Skip Tesseract: these tests are about PDF assembly, not text recognition."""
    monkeypatch.setattr(pdf, "ocr", lambda src, dst: bool(shutil.copyfile(src, dst)))


def auth(token: str) -> dict[str, str]:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def upload_files(token: str, *parts: tuple[str, bytes], **fields: str):
    data: dict[str, object] = {"bucket": "private", **fields}
    data["file"] = [SimpleUploadedFile(name, content) for name, content in parts]
    return Client().post("/api/upload/v1/documents", data, **auth(token))


def original_pdf(doc: Document) -> pikepdf.Pdf:
    handle = files.fetch(doc, "original")
    try:
        return pikepdf.open(io.BytesIO(handle.read()))
    finally:
        handle.close()


def is_a4(page: pikepdf.Page) -> bool:
    box = page.mediabox
    width, height = float(box[2]) - float(box[0]), float(box[3]) - float(box[1])
    # Whole pixels at the scan resolution: within ~1 pt of 595 x 842.
    return abs(width - A4_POINTS[0]) < 1.5 and abs(height - A4_POINTS[1]) < 1.5


def image_filters(page: pikepdf.Page) -> list[str]:
    filters = [img.Filter for img in page.get_images().values()]
    return [str(f[0]) if isinstance(f, pikepdf.Array) else str(f) for f in filters]


# --- Assembly ---------------------------------------------------------------------


def _assemble(
    tmp_path: Path, *parts: tuple[str, bytes], **options
) -> tuple[pikepdf.Pdf, assemble.AssemblyResult]:
    paths = []
    for i, (kind, data) in enumerate(parts):
        p = tmp_path / f"part-{i}"
        p.write_bytes(data)
        paths.append((kind, lambda p=p: p))
    dst = tmp_path / "out.pdf"
    result = assemble.assemble(paths, dst, assemble.AssemblyOptions(**options))
    return pikepdf.open(dst), result


def test_png_page_gets_its_physical_size_from_the_resolution(tmp_path):
    out, result = _assemble(tmp_path, ("image", page_image(["Hello"], dpi=150)))
    assert result.page_count == 1
    assert is_a4(out.pages[0])
    assert image_filters(out.pages[0]) == ["/DCTDecode"]


def test_jpeg_is_embedded_without_recompression(tmp_path):
    jpeg = page_image(["Hello"], fmt="JPEG", mode="RGB")
    out, _ = _assemble(tmp_path, ("image", jpeg))
    image = next(iter(out.pages[0].get_images().values()))
    assert image.read_raw_bytes() == jpeg


def test_raw_pnm_without_resolution_uses_dpi_option(tmp_path):
    pbm = page_image(["Hello"], fmt="PPM", mode="1", dpi=150)
    out, _ = _assemble(tmp_path, ("image", pbm), dpi=150)
    assert is_a4(out.pages[0])
    assert image_filters(out.pages[0]) == ["/CCITTFaxDecode"]  # bilevel → G4


def test_sixteen_bit_and_transparent_images_are_normalized(tmp_path):
    grey16 = Image.new("I;16", (300, 400), 40000)
    rgba = Image.new("RGBA", (300, 400), (255, 0, 0, 0))
    parts = []
    for image in (grey16, rgba):
        buf = io.BytesIO()
        image.save(buf, format="PNG", dpi=(100, 100))
        parts.append(("image", buf.getvalue()))
    out, _ = _assemble(tmp_path, *parts, compression="lossless")
    assert len(out.pages) == 2
    assert all(image_filters(p) == ["/FlateDecode"] for p in out.pages)
    # transparent pixels are flattened onto white paper
    flattened = next(iter(out.pages[1].get_images().values()))
    assert str(flattened.ColorSpace) == "/DeviceGray"


def test_multipage_tiff_and_pdf_parts_keep_their_order(tmp_path):
    first = Image.open(io.BytesIO(page_image(["Page one"])))
    second = Image.open(io.BytesIO(page_image(["Page two"])))
    tiff = io.BytesIO()
    first.save(tiff, format="TIFF", save_all=True, append_images=[second], dpi=(150, 150))
    out, result = _assemble(
        tmp_path, ("image", tiff.getvalue()), ("pdf", text_pdf(["Page three"], extra_pages=[["Page four"]]))
    )
    assert result.page_count == 4
    assert all(is_a4(p) for p in out.pages)


def test_blank_pages_are_skipped_on_request(tmp_path):
    blank = io.BytesIO()
    Image.new("L", (1240, 1754), 250).save(blank, format="PNG", dpi=(150, 150))
    parts = [("image", page_image(INVOICE_LINES)), ("image", blank.getvalue())]
    out, result = _assemble(tmp_path, *parts, skip_blank_pages=True)
    assert result.page_count == 1 and result.skipped_blank == 1
    out, result = _assemble(tmp_path, *parts)
    assert result.page_count == 2
    with pytest.raises(assemble.AssemblyFailed, match="blank"):
        _assemble(tmp_path, ("image", blank.getvalue()), skip_blank_pages=True)


# --- Single-request upload of images ------------------------------------------------


def test_page_images_are_stored_encrypted_and_assembled_into_the_original(scanner, isolated_dirs, no_ocr):
    _, token = scanner
    pages = [page_image(INVOICE_LINES), page_image(["Seite 2"], fmt="JPEG", mode="RGB")]
    r = upload_files(token, ("p1.png", pages[0]), ("p2.jpg", pages[1]), todo="true", skip_blank_pages="true")
    assert r.status_code == 202, r.content
    doc = Document.objects.get(uuid=r.json()["id"])

    parts_dir = isolated_dirs / "intake" / f"{doc.uuid}.parts"
    stored = sorted(p.name for p in parts_dir.iterdir())
    assert stored == ["00001.enc", "00002.enc", "manifest.json"]
    for name in stored[:2]:
        raw = (parts_dir / name).read_bytes()
        assert b"PNG" not in raw[:16] and b"JFIF" not in raw[:16]  # encrypted at rest

    Worker().run_until_empty()
    doc.refresh_from_db()
    assert doc.processing_state == "done", doc.processing_error
    assert doc.page_count == 2
    assert not parts_dir.exists()
    assert not list((isolated_dirs / "intake").iterdir())
    original = original_pdf(doc)
    assert all(is_a4(p) for p in original.pages)
    assert crypto_fields.get_original_filename(doc) == "p1.png"


def test_same_images_are_detected_as_duplicate(scanner):
    _, token = scanner
    png = page_image(["Duplicate"])
    first = upload_files(token, ("a.png", png))
    second = upload_files(token, ("b.png", png))
    assert first.status_code == 202 and second.status_code == 200
    assert second.json() == {**first.json(), "duplicate": True, "status": second.json()["status"]}


def test_unsupported_files_and_options_are_rejected(scanner):
    _, token = scanner
    r = upload_files(token, ("scan.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>"))
    assert r.status_code == 415 and "PNG" in r.json()["detail"]
    png = page_image(["x"])
    assert upload_files(token, ("a.png", png), dpi="abc").status_code == 400
    assert upload_files(token, ("a.png", png), dpi="10").status_code == 400
    assert upload_files(token, ("a.png", png), compression="zip").status_code == 400
    assert Document.objects.count() == 0


def test_too_many_files_are_rejected(scanner, settings):
    _, token = scanner
    settings.MAX_PAGES = 2
    png = page_image(["x"])
    r = upload_files(token, ("a.png", png), ("b.png", png), ("c.png", png))
    assert r.status_code == 413


@pytest.mark.ocr
def test_uploaded_page_image_becomes_searchable(scanner, api):
    _, token = scanner
    r = upload_files(token, ("scan.png", page_image(INSURANCE_LINES, dpi=200)))
    assert r.status_code == 202
    Worker().run_until_empty()
    doc = Document.objects.get(uuid=r.json()["id"])
    assert doc.processing_state == "done", doc.processing_error
    assert "Fahrzeugversicherung" in crypto_fields.get_content(doc)


# --- Scan sessions -------------------------------------------------------------------


def open_scan(token: str, **fields: str):
    return Client().post("/api/upload/v1/scans", {"bucket": "private", **fields}, **auth(token))


def add_page(token: str, scan_id: str, data: bytes, page: int | None = None, name: str = "page.png"):
    body: dict[str, object] = {"file": SimpleUploadedFile(name, data)}
    if page is not None:
        body["page"] = str(page)
    return Client().post(f"/api/upload/v1/scans/{scan_id}/pages", body, **auth(token))


def complete(token: str, scan_id: str, **fields: str):
    return Client().post(f"/api/upload/v1/scans/{scan_id}/complete", fields, **auth(token))


def test_scan_session_upload_page_by_page(scanner, isolated_dirs, no_ocr):
    _, token = scanner
    r = open_scan(token, tags="Post", dpi="150")
    assert r.status_code == 201, r.content
    scan = r.json()
    assert scan["status"] == "open" and scan["pages"] == []

    p1, p2, p3 = (page_image([f"Seite {n}"], dpi=None, fmt="PPM") for n in (1, 2, 3))
    assert add_page(token, scan["id"], p2, page=2).status_code == 201  # out of order is fine
    r = add_page(token, scan["id"], p1, page=1)
    assert r.json() == {"page": 1, "kind": "image", "size": len(p1), "page_count": 2}
    assert add_page(token, scan["id"], p1, page=1).status_code == 201  # retry replaces
    assert add_page(token, scan["id"], p3).json()["page"] == 3  # no number: appended

    status = Client().get(f"/api/upload/v1/scans/{scan['id']}", **auth(token)).json()
    assert status["pages"] == [1, 2, 3] and status["size"] == len(p1) + len(p2) + len(p3)
    session_files = list((isolated_dirs / "intake" / "scans" / scan["id"]).iterdir())
    assert len(session_files) == 3

    assert complete(token, scan["id"], expected_pages="4").status_code == 409
    r = complete(token, scan["id"], expected_pages="3")
    assert r.status_code == 202, r.content
    doc_id = r.json()["id"]
    again = complete(token, scan["id"])
    assert again.status_code == 200 and again.json()["id"] == doc_id
    assert add_page(token, scan["id"], p1).status_code == 409
    assert not (isolated_dirs / "intake" / "scans" / scan["id"]).exists()

    Worker().run_until_empty()
    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == "done", doc.processing_error
    assert doc.page_count == 3
    assert [t.name for t in doc.tags.all()] == ["Post"]
    original = original_pdf(doc)
    assert len(original.pages) == 3 and all(is_a4(p) for p in original.pages)  # size from dpi=150


def test_scan_session_rejects_gaps_and_empty_completion(scanner):
    _, token = scanner
    scan_id = open_scan(token).json()["id"]
    assert complete(token, scan_id).status_code == 409
    add_page(token, scan_id, page_image(["a"]), page=1)
    add_page(token, scan_id, page_image(["c"]), page=3)
    r = complete(token, scan_id)
    assert r.status_code == 409 and "2" in r.json()["detail"]
    assert add_page(token, scan_id, page_image(["a"]), page=0).status_code == 400
    assert add_page(token, scan_id, b"not an image").status_code == 415


def test_scan_session_is_idempotent_private_and_deletable(scanner, isolated_dirs):
    _, token = scanner
    first = Client().post(
        "/api/upload/v1/scans", {"bucket": "private"}, HTTP_IDEMPOTENCY_KEY="scan-1", **auth(token)
    )
    again = Client().post(
        "/api/upload/v1/scans", {"bucket": "private"}, HTTP_IDEMPOTENCY_KEY="scan-1", **auth(token)
    )
    assert first.status_code == 201 and again.status_code == 200
    assert first.json()["id"] == again.json()["id"]
    assert open_scan(token, document_type="nope").status_code == 400

    other_token, prefix, token_hash = tokens.generate()
    ScannerClient.objects.create(
        name="Other", token_prefix=prefix, token_hash=token_hash, scopes=[ScannerClient.Scope.UPLOAD]
    )
    scan_id = first.json()["id"]
    assert Client().get(f"/api/upload/v1/scans/{scan_id}", **auth(other_token)).status_code == 404

    add_page(token, scan_id, page_image(["a"]))
    assert Client().delete(f"/api/upload/v1/scans/{scan_id}", **auth(token)).status_code == 204
    assert not (isolated_dirs / "intake" / "scans" / scan_id).exists()
    assert Client().get(f"/api/upload/v1/scans/{scan_id}", **auth(token)).status_code == 404


def test_expired_scan_sessions_are_rejected_and_cleaned_up(scanner, isolated_dirs, settings):
    _, token = scanner
    settings.MAX_OPEN_SCAN_SESSIONS = 1
    scan_id = open_scan(token).json()["id"]
    add_page(token, scan_id, page_image(["a"]))
    assert open_scan(token).status_code == 409  # limit of open sessions
    ScanSession.objects.filter(uuid=scan_id).update(expires_at=timezone.now() - timedelta(minutes=1))
    assert add_page(token, scan_id, page_image(["b"])).status_code == 410
    assert complete(token, scan_id).status_code == 410
    assert sessions.cleanup() == 1
    assert not (isolated_dirs / "intake" / "scans" / scan_id).exists()
    assert open_scan(token).status_code == 201


def test_scan_session_with_known_content_returns_existing_document(scanner):
    _, token = scanner
    pdf_bytes = text_pdf(["Already archived"])
    existing = upload_files(token, ("letter.pdf", pdf_bytes)).json()["id"]
    scan_id = open_scan(token).json()["id"]
    add_page(token, scan_id, pdf_bytes, name="letter.pdf")
    r = complete(token, scan_id)
    assert r.status_code == 200 and r.json()["id"] == existing and r.json()["duplicate"] is True
    assert Document.objects.count() == 1
