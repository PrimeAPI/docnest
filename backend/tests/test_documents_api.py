import pytest
from django.test import Client

from apps.documents.models import Document
from apps.processing.worker import Worker
from apps.taxonomy.models import Bucket, DocumentType
from tests.conftest import upload
from tests.pdfs import INSURANCE_LINES, INVOICE_LINES, text_pdf

pytestmark = pytest.mark.django_db(transaction=True)
J = "application/json"


@pytest.fixture
def docs(scanner):
    _, token = scanner
    a = upload(Client(), token, text_pdf(INVOICE_LINES), bucket="private", todo="true").json()["id"]
    b = upload(Client(), token, text_pdf(INSURANCE_LINES), bucket="business", important="true").json()["id"]
    Worker().run_until_empty()
    return a, b


def test_combined_filters_and_search(api, docs):
    a, b = docs
    business = Bucket.objects.get(slug="business").pk
    r = api.get("/api/v1/documents", {"bucket": business}).json()
    assert [i["id"] for i in r["items"]] == [b]
    r = api.get("/api/v1/documents", {"status": "todo"}).json()
    assert [i["id"] for i in r["items"]] == [a]
    r = api.get("/api/v1/documents", {"important": "true", "q": "Kennzeichen"}).json()
    assert [i["id"] for i in r["items"]] == [b]
    r = api.get("/api/v1/documents", {"date_from": "2026-03-01", "date_to": "2026-03-31"}).json()
    assert [i["id"] for i in r["items"]] == [a]
    invoice = DocumentType.objects.get(slug="invoice").pk
    r = api.get("/api/v1/documents", {"document_type": invoice, "q": "Strom"}).json()
    assert r["total"] == 1


def test_inbox_overview_and_read_state(api, docs):
    a, b = docs
    o = api.get("/api/v1/overview").json()
    assert o["total"] == 2 and o["unread"] == 2 and o["todo"] == 1 and o["important"] == 1
    api.get(f"/api/v1/documents/{a}")  # opening marks as read
    assert api.get("/api/v1/overview").json()["unread"] == 1
    api.post(f"/api/v1/documents/{a}/unread", content_type=J)
    assert api.get("/api/v1/overview").json()["unread"] == 2
    api.post("/api/v1/documents/bulk", {"ids": [a, b], "action": "status_done"}, content_type=J)
    assert api.get("/api/v1/overview").json()["todo"] == 0


def test_manual_corrections_are_recorded_as_user_source(api, docs):
    a, _ = docs
    r = api.patch(
        f"/api/v1/documents/{a}",
        {
            "title": "Power February",
            "document_date": "2026-02-28",
            "correspondent_name": "Stadtwerke",
            "tag_names": ["Utilities"],
            "status": "done",
            "is_important": True,
        },
        content_type=J,
    )
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["title"] == "Power February"
    assert body["correspondent"]["name"] == "Stadtwerke"
    assert [t["name"] for t in body["tags"]] == ["Utilities"]
    assert body["field_sources"]["title"] == "user"
    assert body["field_sources"]["tags"] == "user"
    # removed automatic tags are not re-added on reprocessing
    api.post(f"/api/v1/documents/{a}/reprocess", {"stage": "analyze"}, content_type=J)
    Worker().run_until_empty()
    d = api.get(f"/api/v1/documents/{a}").json()
    assert [t["name"] for t in d["tags"]] == ["Utilities"]
    assert d["title"] == "Power February" and d["status"] == "done"
    # search finds the new title
    assert api.get("/api/v1/documents", {"q": "Power February"}).json()["total"] == 1


def test_thumbnail_and_text(api, docs):
    a, _ = docs
    r = api.get(f"/api/v1/documents/{a}/thumbnail")
    assert r.status_code == 200 and r["Content-Type"] == "image/webp"
    assert "Stromlieferung" in api.get(f"/api/v1/documents/{a}/text").json()["text"]


def test_download_is_audited(api, docs):
    from apps.audit.models import AuditLog

    a, _ = docs
    r = api.get(f"/api/v1/documents/{a}/file", {"variant": "original", "download": "true"})
    assert r.status_code == 200 and "attachment" in r["Content-Disposition"]
    b"".join(r.streaming_content)
    assert AuditLog.objects.filter(action="document.downloaded").exists()


def test_unknown_document_404(api, db):
    import uuid

    assert api.get(f"/api/v1/documents/{uuid.uuid4()}").status_code == 404


def test_spa_fallback_and_health(db, settings, tmp_path):
    settings.FRONTEND_DIR = tmp_path
    (tmp_path / "index.html").write_text("<html>app</html>")
    c = Client()
    r = c.get("/inbox")
    assert r.status_code == 200 and b"app" in r.content
    assert "csrf" in "".join(r.cookies.keys())
    assert c.get("/api/v1/health").json() == {"status": "ok"}
    assert Document.objects.count() == 0


def test_web_upload_goes_through_the_pipeline(api, isolated_dirs):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from tests.pdfs import INVOICE_LINES as LINES

    business = Bucket.objects.get(slug="business").pk
    pdf = text_pdf(LINES)
    r = api.post(
        "/api/v1/documents/upload",
        {
            "file": SimpleUploadedFile("brief.pdf", pdf, content_type="application/pdf"),
            "bucket_id": business,
            "todo": "true",
        },
    )
    assert r.status_code == 202, r.content
    doc_id = r.json()["id"]
    assert list((isolated_dirs / "intake").iterdir())  # durable, encrypted intake
    Worker().run_until_empty()
    d = api.get(f"/api/v1/documents/{doc_id}").json()
    assert d["processing_state"] == "done" and d["bucket"]["id"] == business and d["status"] == "todo"
    assert d["received_from"] == "Web upload" and d["original_filename"] == "brief.pdf"
    # same file again -> duplicate, no new document
    r = api.post("/api/v1/documents/upload", {"file": SimpleUploadedFile("again.pdf", pdf)})
    assert r.status_code == 200 and r.json() == {"id": doc_id, "duplicate": True}


def test_web_upload_requires_login_and_csrf(api, scanner):
    from django.core.files.uploadedfile import SimpleUploadedFile

    _, token = scanner
    f = lambda: SimpleUploadedFile("x.pdf", text_pdf(["x"]))  # noqa: E731
    assert Client().post("/api/v1/documents/upload", {"file": f()}).status_code == 401
    assert (
        Client()
        .post("/api/v1/documents/upload", {"file": f()}, HTTP_AUTHORIZATION=f"Bearer {token}")
        .status_code
        == 401
    )
    api.handler.enforce_csrf_checks = True
    assert api.post("/api/v1/documents/upload", {"file": f()}).status_code == 403
    api.handler.enforce_csrf_checks = False
    r = api.post("/api/v1/documents/upload", {"file": SimpleUploadedFile("x.txt", b"hello")})
    assert r.status_code == 415
