import io
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pikepdf
import pytest
from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.test import Client
from django.utils import timezone
from freezegun import freeze_time

from apps.audit.models import AuditLog
from apps.crypto.aead import encrypt_file
from apps.documents import crypto_fields
from apps.documents.intake import archive_aad, archive_intake_path_for, intake_aad, intake_path_for
from apps.documents.models import Document, DocumentShare
from apps.documents.sharing_api import token_hash
from tests.pdfs import text_pdf

pytestmark = pytest.mark.django_db
J = "application/json"


@pytest.fixture
def document(tmp_path):
    document = Document.objects.create(
        content_hash="share-test",
        page_count=3,
        original_page_count=3,
        page_view={"count": 3, "order": [3, 1]},
        processing_state=Document.State.DONE,
        scanner_metadata_enc=None,
    )
    crypto_fields.set_title(document, "Shared letter")
    document.save()
    crypto_fields.set_content(document, "Recognized letter text")
    original = tmp_path / "original.pdf"
    original.write_bytes(
        text_pdf(["Original page 1"], extra_pages=[["Original page 2"], ["Original page 3"]])
    )
    encrypt_file(original, intake_path_for(str(document.uuid)), aad=intake_aad(str(document.uuid)))
    archive = tmp_path / "archive.pdf"
    archive.write_bytes(text_pdf(["Enhanced page 1"], extra_pages=[["Enhanced page 2"], ["Enhanced page 3"]]))
    encrypt_file(archive, archive_intake_path_for(str(document.uuid)), aad=archive_aad(str(document.uuid)))
    return document


def create(api, document, **options):
    response = api.post(f"/api/v1/documents/{document.uuid}/shares", options, content_type=J)
    assert response.status_code == 201, response.content
    return response.json()


def public(share):
    return f"/api/v1/shares/{share['id']}"


def stream(response):
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Cache-Control"] == "no-store"
    assert response["Referrer-Policy"] == "no-referrer"
    return b"".join(response.streaming_content)


def test_link_only_share_is_read_only_and_returns_enhanced_pdf(api, document):
    share = create(api, document)
    visitor = Client()
    assert share["id"] != str(document.uuid)
    assert share["url"] == f"{settings.BASE_URL}/share/{share['id']}"
    assert share["pdf_url"] == f"{settings.BASE_URL}{public(share)}/file"
    response = visitor.get(public(share))
    assert response.status_code == 200
    assert response.json() == {
        "title": "Shared letter",
        "document_date": None,
        "page_count": 2,
        "allow_original": False,
        "allow_text": True,
    }
    assert visitor.get(public(share) + "/text").json()["text"] == "Recognized letter text"
    pdf = stream(visitor.get(public(share) + "/file"))
    with pikepdf.open(io.BytesIO(pdf)) as shown:
        assert len(shown.pages) == 2
        assert b"Enhanced page 3" in shown.pages[0].Contents.read_bytes()
        assert b"Enhanced page 1" in shown.pages[1].Contents.read_bytes()
    download = visitor.get(public(share) + "/file?download=true")
    assert "attachment" in download["Content-Disposition"]
    stream(download)
    assert visitor.get(public(share) + "/file?variant=original").status_code == 403
    assert visitor.get(public(share) + "/file?variant=../../original").status_code == 422
    for method in ("patch", "delete", "post"):
        assert getattr(visitor, method)(public(share), {}, content_type=J).status_code == 405
    assert visitor.get(f"/api/v1/documents/{document.uuid}").status_code == 401
    assert visitor.get("/api/v1/documents").status_code == 401
    document.refresh_from_db()
    assert document.read_at is None


def test_optional_original_and_text_permissions(api, document):
    share = create(api, document, allow_original=True, allow_text=False)
    visitor = Client()
    with pikepdf.open(io.BytesIO(stream(visitor.get(public(share) + "/file?variant=original")))) as pdf:
        assert len(pdf.pages) == 3
        assert b"Original page 1" in pdf.pages[0].Contents.read_bytes()
    assert visitor.get(public(share) + "/text").status_code == 403


def test_passphrase_is_hashed_and_unlock_is_limited_to_one_share(api, document):
    share = create(api, document, passphrase="A private phrase", allow_original=True)
    other = create(api, document, passphrase="A private phrase")
    stored = DocumentShare.objects.get(token_hash=token_hash(UUID(share["id"])))
    assert share["id"].encode() not in bytes(stored.token_enc)
    assert stored.password_hash != "A private phrase"
    assert check_password("A private phrase", stored.password_hash)
    assert "password_hash" not in share and "passphrase" not in share
    visitor = Client()
    for suffix in ("", "/text"):
        assert visitor.get(public(share) + suffix).status_code == 403
    redirect = visitor.get(public(share) + "/file?variant=original&download=true")
    assert redirect.status_code == 302
    assert redirect["Location"] == f"/share/{share['id']}?pdf=original&download=true"
    assert visitor.post(public(share) + "/unlock", {"passphrase": "wrong"}, content_type=J).status_code == 403
    assert (
        visitor.post(
            public(share) + "/unlock", {"passphrase": "A private phrase"}, content_type=J
        ).status_code
        == 200
    )
    assert visitor.get(public(share)).status_code == 200
    assert visitor.get(public(share) + "/text").status_code == 200
    stream(visitor.get(public(share) + "/file"))
    assert visitor.get(public(other)).status_code == 403
    assert Client().get(public(share)).status_code == 403
    assert visitor.get("/api/v1/documents").status_code == 401
    with freeze_time(timezone.now() + timedelta(hours=13)):
        assert visitor.get(public(share)).status_code == 403
        assert visitor.get(public(share) + "/text").status_code == 403
        assert visitor.get(public(share) + "/file").status_code == 302
    logs = json.dumps(list(AuditLog.objects.values("action", "target", "details")))
    assert share["id"] not in logs
    assert "A private phrase" not in logs


def test_management_requires_mfa_and_revocation_invalidates_unlocked_visitors(api, document):
    visitor = Client()
    manage = f"/api/v1/documents/{document.uuid}/shares"
    assert visitor.post(manage, {}, content_type=J).status_code == 401
    assert visitor.get(manage).status_code == 401
    share = create(api, document, passphrase="private")
    second = create(api, document)
    assert len(api.get(manage).json()) == 2
    visitor.post(public(share) + "/unlock", {"passphrase": "private"}, content_type=J)
    assert visitor.delete(f"{manage}/{share['id']}").status_code == 401
    unrelated = Document.objects.create(content_hash="other")
    assert api.delete(f"/api/v1/documents/{unrelated.uuid}/shares/{share['id']}").status_code == 404
    assert api.delete(f"{manage}/{share['id']}").status_code == 200
    for suffix in ("", "/text", "/file", "/file?variant=original"):
        assert visitor.get(public(share) + suffix).status_code == 404
    assert (
        visitor.post(public(share) + "/unlock", {"passphrase": "private"}, content_type=J).status_code == 404
    )
    assert visitor.get(public(second)).status_code == 200


def test_unknown_and_trashed_documents_cannot_be_shared_or_accessed(api, document):
    visitor = Client()
    assert visitor.get(f"/api/v1/shares/{document.uuid}").status_code == 404
    assert visitor.get(f"/api/v1/shares/{uuid4()}").status_code == 404
    share = create(api, document)
    document.deleted_at = timezone.now()
    document.save(update_fields=["deleted_at"])
    for suffix in ("", "/text", "/file", "/file?variant=original"):
        assert visitor.get(public(share) + suffix).status_code == 404
    assert api.post(f"/api/v1/documents/{document.uuid}/shares", {}, content_type=J).status_code == 404


def test_unlock_requires_csrf(api, document):
    share = create(api, document, passphrase="private")
    visitor = Client(enforce_csrf_checks=True)
    assert (
        visitor.post(public(share) + "/unlock", {"passphrase": "private"}, content_type=J).status_code == 403
    )
    visitor.get("/api/v1/csrf")
    token = visitor.cookies[settings.CSRF_COOKIE_NAME].value
    assert (
        visitor.post(
            public(share) + "/unlock", {"passphrase": "private"}, content_type=J, HTTP_X_CSRFTOKEN=token
        ).status_code
        == 200
    )


def test_passphrase_attempts_are_throttled(api, document):
    share = create(api, document, passphrase="private")
    visitor = Client()
    for _ in range(5):
        assert (
            visitor.post(public(share) + "/unlock", {"passphrase": "wrong"}, content_type=J).status_code
            == 403
        )
    assert (
        visitor.post(public(share) + "/unlock", {"passphrase": "private"}, content_type=J).status_code == 429
    )
    with freeze_time(timezone.now() + timedelta(minutes=16)):
        assert (
            visitor.post(public(share) + "/unlock", {"passphrase": "private"}, content_type=J).status_code
            == 200
        )


def test_share_validation_and_storage_failure(api, document):
    url = f"/api/v1/documents/{document.uuid}/shares"
    for phrase in ("   ", "x" * 1025):
        assert api.post(url, {"passphrase": phrase}, content_type=J).status_code == 422
    share = create(api, document)
    intake_path_for(str(document.uuid)).unlink()
    archive_intake_path_for(str(document.uuid)).unlink()
    assert Client().get(public(share) + "/file").status_code == 409


def test_public_view_page_has_private_headers_and_csrf_cookie(settings, tmp_path):
    settings.FRONTEND_DIR = tmp_path
    (tmp_path / "index.html").write_text("<html>app</html>")
    response = Client().get(f"/share/{uuid4()}")
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    assert response["Referrer-Policy"] == "no-referrer"
    assert response["X-Robots-Tag"] == "noindex, nofollow, noarchive"
    assert settings.CSRF_COOKIE_NAME in response.cookies


def test_unlock_limit_also_applies_across_different_shares(api, document):
    share = create(api, document, passphrase="private")
    for index in range(20):
        AuditLog.objects.create(
            action="document.share_unlock_failed",
            actor_type=AuditLog.Actor.ANONYMOUS,
            target=f"other-{index}",
            ip="127.0.0.1",
        )
    response = Client().post(public(share) + "/unlock", {"passphrase": "private"}, content_type=J)
    assert response.status_code == 429
