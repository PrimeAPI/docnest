import pytest
from django.test import Client

from apps.documents.models import Document
from apps.processing.models import Job
from apps.scanners.models import ScannerClient
from tests.conftest import upload
from tests.pdfs import INVOICE_LINES, text_pdf

pytestmark = pytest.mark.django_db


def test_upload_is_accepted_and_durable(scanner, isolated_dirs):
    _, token = scanner
    pdf = text_pdf(INVOICE_LINES)
    r = upload(Client(), token, pdf, todo="true", important="1", tags="Energie, Haushalt")
    assert r.status_code == 202, r.content
    body = r.json()
    doc = Document.objects.get(uuid=body["id"])
    assert doc.status == "todo" and doc.is_important
    assert Job.objects.filter(document=doc, kind="process_document").count() == 1
    intake = list((isolated_dirs / "intake").iterdir())
    assert len(intake) == 1
    raw = intake[0].read_bytes()
    assert b"%PDF" not in raw and b"Stadtwerke" not in raw  # encrypted at rest
    assert sorted(t.name for t in doc.tags.all()) == ["Energie", "Haushalt"]


def test_duplicate_upload_returns_existing(scanner):
    _, token = scanner
    pdf = text_pdf(INVOICE_LINES)
    first = upload(Client(), token, pdf).json()
    r = upload(Client(), token, pdf)
    assert r.status_code == 200 and r.json()["duplicate"] is True and r.json()["id"] == first["id"]
    assert Document.objects.count() == 1


def test_idempotency_key(scanner):
    _, token = scanner
    a = upload(Client(), token, text_pdf(["one"]), idempotency_key="abc")
    b = upload(Client(), token, text_pdf(["two"]), idempotency_key="abc")
    assert b.status_code == 200 and a.json()["id"] == b.json()["id"]


def test_rejects_non_pdf_and_unknown_bucket(scanner):
    _, token = scanner
    from django.core.files.uploadedfile import SimpleUploadedFile

    r = Client().post(
        "/api/upload/v1/documents",
        {"bucket": "private", "file": SimpleUploadedFile("x.pdf", b"MZ\x90\x00 not a pdf")},
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert r.status_code == 415
    assert upload(Client(), token, text_pdf(["x"]), bucket="nope").status_code == 400
    assert upload(Client(), token, text_pdf(["x"]), document_type="nope").status_code == 400


def test_invalid_or_revoked_token(scanner):
    client, token = scanner
    assert upload(Client(), "dn_scan_000000000000_wrong", text_pdf(["x"])).status_code == 401
    assert upload(Client(), token + "x", text_pdf(["x"])).status_code == 401
    from django.utils import timezone

    client.revoked_at = timezone.now()
    client.save()
    assert upload(Client(), token, text_pdf(["x"])).status_code == 401


def test_scanner_token_cannot_access_web_api(scanner):
    _, token = scanner
    c = Client()
    for path in ("/api/v1/documents", "/api/v1/overview", "/api/v1/tags", "/api/v1/scanners"):
        assert c.get(path, HTTP_AUTHORIZATION=f"Bearer {token}").status_code == 401


def test_user_session_cannot_use_upload_api(api):
    r = upload(api, "", text_pdf(["x"]))
    assert r.status_code == 401
    assert api.get("/api/upload/v1/ping").status_code == 401


def test_scope_and_status_isolation(scanner, db):
    client, token = scanner
    doc_id = upload(Client(), token, text_pdf(["status test"])).json()["id"]
    r = Client().get(f"/api/upload/v1/documents/{doc_id}", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert r.status_code == 200 and set(r.json()) == {"id", "status", "stage", "error"}

    from apps.scanners import tokens

    other_token, prefix, h = tokens.generate()
    ScannerClient.objects.create(name="Other", token_prefix=prefix, token_hash=h, scopes=["upload"])
    r = Client().get(f"/api/upload/v1/documents/{doc_id}", HTTP_AUTHORIZATION=f"Bearer {other_token}")
    assert r.status_code == 403  # lacks status scope
    ScannerClient.objects.filter(name="Other").update(scopes=["upload", "upload:status"])
    r = Client().get(f"/api/upload/v1/documents/{doc_id}", HTTP_AUTHORIZATION=f"Bearer {other_token}")
    assert r.status_code == 404  # not its document


def test_ip_allow_list(scanner):
    client, token = scanner
    client.allowed_ips = ["10.0.0.0/8"]
    client.save()
    assert upload(Client(), token, text_pdf(["x"])).status_code == 401
    client.allowed_ips = ["127.0.0.1"]
    client.save()
    assert upload(Client(), token, text_pdf(["x"])).status_code == 202


def test_scanner_management_shows_token_once(api):
    r = api.post("/api/v1/scanners", {"name": "Office"}, content_type="application/json")
    assert r.status_code == 200
    token = r.json()["token"]
    assert token.startswith("dn_scan_")
    listing = api.get("/api/v1/scanners").json()
    assert token not in str(listing)
    stored = ScannerClient.objects.get(name="Office")
    assert token not in stored.token_hash
    assert upload(Client(), token, text_pdf(["managed"])).status_code == 202
