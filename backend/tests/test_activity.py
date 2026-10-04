import pytest
from django.test import Client

from apps.audit.models import AuditLog
from tests.conftest import PASSWORD, totp_code, upload
from tests.pdfs import INVOICE_LINES, text_pdf

pytestmark = pytest.mark.django_db
J = "application/json"


def sign_in(secret: str, offset: int, ua: str) -> Client:
    c = Client(HTTP_USER_AGENT=ua)
    c.post("/api/v1/auth/login", {"username": "alice", "password": PASSWORD}, content_type=J)
    r = c.post("/api/v1/auth/mfa/totp", {"code": totp_code(secret, offset)}, content_type=J)
    assert r.status_code == 200, r.content
    return c


def test_sign_in_history_with_failures_and_session_actions(totp_user, scanner):
    user, secret = totp_user
    first = sign_in(secret, -1, "Mozilla/5.0 (X11; Linux x86_64) Firefox/140.0")

    # actions in the first session
    _, token = scanner
    doc_id = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    first.patch(f"/api/v1/documents/{doc_id}", {"title": "Power bill"}, content_type=J)

    # someone fails twice (wrong password, wrong code)
    Client().post("/api/v1/auth/login", {"username": "alice", "password": "nope"}, content_type=J)
    attacker = Client()
    attacker.post("/api/v1/auth/login", {"username": "alice", "password": PASSWORD}, content_type=J)
    attacker.post("/api/v1/auth/mfa/totp", {"code": "000000"}, content_type=J)

    second = sign_in(secret, 0, "Mozilla/5.0 (Macintosh) Safari/605.1")
    state = second.get("/api/v1/auth/session").json()
    assert state["previous_login"]["user_agent"].endswith("Firefox/140.0")
    assert state["failed_since_previous_login"] == 2

    activity = second.get("/api/v1/account/activity").json()
    outcomes = [a["outcome"] for a in activity]
    assert outcomes == ["success", "wrong_second_factor", "failed", "success"]
    assert activity[0]["current"] is True and activity[0]["active"] is True
    first_session = activity[3]
    assert first_session["method"] == "totp" and first_session["active"] is True
    edits = [a for a in first_session["actions"] if a["action"] == "document.updated"]
    assert edits and edits[0]["target_title"] == "Power bill"

    # logging out ends the session and is recorded in it
    first.post("/api/v1/auth/logout", content_type=J)
    activity = second.get("/api/v1/account/activity").json()
    assert activity[3]["active"] is False
    assert activity[3]["actions"][-1]["action"] == "logout"


def test_activity_only_shows_own_account(totp_user, db):
    user, secret = totp_user
    AuditLog.objects.create(actor_type="anonymous", action="login.failed", subject="mallory")
    c = sign_in(secret, 0, "UA")
    assert all(a["outcome"] == "success" for a in c.get("/api/v1/account/activity").json())


def test_opening_a_document_is_logged(api, scanner):
    from apps.processing.worker import Worker

    _, token = scanner
    doc_id = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    Worker().run_until_empty()
    r = api.get(f"/api/v1/documents/{doc_id}/file")
    b"".join(r.streaming_content)
    entry = AuditLog.objects.get(action="document.opened")
    assert entry.target == doc_id and entry.session_ref.startswith("s")
