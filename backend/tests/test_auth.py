import pytest
from django.test import Client

from apps.accounts.models import RecoveryCode, TotpDevice, UserSession
from apps.audit.models import AuditLog
from tests.conftest import PASSWORD, totp_code

pytestmark = pytest.mark.django_db
J = "application/json"


def login(client: Client, username: str, password: str = PASSWORD):
    return client.post("/api/v1/auth/login", {"username": username, "password": password}, content_type=J)


def test_passwords_are_hashed_with_argon2(user):
    assert user.password.startswith("argon2")
    assert PASSWORD not in user.password


def test_password_login_without_mfa_only_allows_enrollment(user):
    c = Client()
    r = login(c, "alice")
    assert r.status_code == 200
    assert r.json()["needs_enrollment"] is True and r.json()["mfa_complete"] is False
    assert c.get("/api/v1/documents").status_code == 401
    assert c.get("/api/v1/overview").status_code == 401
    # enrollment works
    setup = c.post("/api/v1/account/totp/setup", content_type=J).json()
    assert setup["uri"].startswith("otpauth://")
    r = c.post("/api/v1/account/totp/confirm", {"code": totp_code(setup["secret"])}, content_type=J)
    assert r.status_code == 200 and r.json()["mfa_complete"] is True
    assert c.get("/api/v1/documents").status_code == 200
    assert UserSession.objects.filter(user=user).count() == 1


def test_mfa_required_when_enrolled(totp_user):
    user, secret = totp_user
    c = Client()
    r = login(c, "alice")
    body = r.json()
    assert body["authenticated"] is False and body["pending_mfa"]["totp"] is True
    assert c.get("/api/v1/documents").status_code == 401
    assert c.post("/api/v1/auth/mfa/totp", {"code": "000000"}, content_type=J).status_code == 400
    r = c.post("/api/v1/auth/mfa/totp", {"code": totp_code(secret)}, content_type=J)
    assert r.status_code == 200 and r.json()["mfa_complete"] is True
    assert c.get("/api/v1/documents").status_code == 200


def test_totp_code_cannot_be_replayed(totp_user):
    user, secret = totp_user
    code = totp_code(secret)
    c1 = Client()
    login(c1, "alice")
    assert c1.post("/api/v1/auth/mfa/totp", {"code": code}, content_type=J).status_code == 200
    c2 = Client()
    login(c2, "alice")
    assert c2.post("/api/v1/auth/mfa/totp", {"code": code}, content_type=J).status_code == 400


def test_wrong_password_is_generic_and_throttled(user, settings):
    settings.LOGIN_RATE_LIMIT_PER_ACCOUNT = 3
    c = Client()
    for _ in range(3):
        r = login(c, "alice", "wrong")
        assert r.status_code == 400
        assert r.json()["detail"] == "Invalid username or password"
    # now even the right password is refused for the lockout period
    assert login(c, "alice").status_code == 429
    assert login(c, "nobody", "x").status_code == 400  # unknown user: same generic error
    assert AuditLog.objects.filter(action="login.failed").count() >= 3


def test_recovery_code_login_single_use(totp_user):
    user, _ = totp_user
    from apps.accounts.services import generate_recovery_codes

    codes = generate_recovery_codes(user)
    assert RecoveryCode.objects.filter(user=user).count() == 10
    assert all(codes[0] not in rc.code_hash for rc in RecoveryCode.objects.all())
    c = Client()
    login(c, "alice")
    assert c.post("/api/v1/auth/mfa/recovery", {"code": codes[0].upper()}, content_type=J).status_code == 200
    c2 = Client()
    login(c2, "alice")
    assert c2.post("/api/v1/auth/mfa/recovery", {"code": codes[0]}, content_type=J).status_code == 400


def test_csrf_is_enforced_on_login(user):
    c = Client(enforce_csrf_checks=True)
    assert login(c, "alice").status_code == 403
    c.get("/api/v1/csrf")
    from django.conf import settings

    token = c.cookies[settings.CSRF_COOKIE_NAME].value
    r = c.post(
        "/api/v1/auth/login",
        {"username": "alice", "password": PASSWORD},
        content_type=J,
        HTTP_X_CSRFTOKEN=token,
    )
    assert r.status_code == 200


def test_csrf_is_enforced_on_authenticated_writes(api):
    api.handler.enforce_csrf_checks = True
    assert api.post("/api/v1/tags", {"name": "X"}, content_type=J).status_code == 403


def test_logout_invalidates_session(api):
    assert api.get("/api/v1/overview").status_code == 200
    api.post("/api/v1/auth/logout", content_type=J)
    assert api.get("/api/v1/overview").status_code == 401


def test_revoked_session_is_logged_out(totp_user):
    user, secret = totp_user
    a, b = Client(), Client()
    for c, offset in ((a, 0), (b, 1)):
        login(c, "alice")
        assert (
            c.post("/api/v1/auth/mfa/totp", {"code": totp_code(secret, offset)}, content_type=J).status_code
            == 200
        )
    assert a.post("/api/v1/account/sessions/revoke-others", content_type=J).json()["revoked"] == 1
    assert b.get("/api/v1/overview").status_code == 401
    assert a.get("/api/v1/overview").status_code == 200


def test_cannot_remove_last_factor(api):
    device = TotpDevice.objects.get()
    r = api.delete(f"/api/v1/account/devices/totp/{device.pk}")
    assert r.status_code == 400


def test_sensitive_actions_require_recent_auth(api, settings):
    settings.REAUTH_MAX_AGE_SECONDS = -1
    r = api.post("/api/v1/scanners", {"name": "S"}, content_type=J)
    assert r.status_code == 403 and r.json()["detail"] == "reauthentication_required"
    settings.REAUTH_MAX_AGE_SECONDS = 600
    assert api.post("/api/v1/auth/reauth", {"password": PASSWORD}, content_type=J).status_code == 200
    assert api.post("/api/v1/scanners", {"name": "S"}, content_type=J).status_code == 200


def test_security_headers(api):
    r = api.get("/api/v1/overview")
    assert "default-src 'self'" in r["Content-Security-Policy"]
    assert r["Cache-Control"] == "no-store"
    assert r["X-Frame-Options"] == "DENY"
    assert r["Referrer-Policy"] == "no-referrer"


def test_password_change_policy(api):
    r = api.post(
        "/api/v1/account/password", {"current_password": PASSWORD, "new_password": "short"}, content_type=J
    )
    assert r.status_code == 400
    r = api.post(
        "/api/v1/account/password",
        {"current_password": PASSWORD, "new_password": "Another-Long-Passw0rd"},
        content_type=J,
    )
    assert r.status_code == 200
    assert api.get("/api/v1/overview").status_code == 200


def _age_session(client, *, idle_days: float = 0, since_login_days: float = 0) -> None:
    import time as _time

    session = client.session
    now = _time.time()
    session["last_activity"] = now - idle_days * 86400
    session["login_at"] = now - since_login_days * 86400
    session.save()


def test_session_survives_six_quiet_days(api):
    _age_session(api, idle_days=6, since_login_days=20)
    assert api.get("/api/v1/documents").status_code == 200


def test_session_ends_after_a_week_without_use(api):
    _age_session(api, idle_days=8, since_login_days=8)
    assert api.get("/api/v1/documents").status_code == 401


def test_session_ends_five_weeks_after_sign_in_even_when_used_daily(api):
    _age_session(api, idle_days=0.5, since_login_days=36)
    assert api.get("/api/v1/documents").status_code == 401


def test_session_cookie_outlives_the_browser(totp_user, settings):
    from django.test import Client as _Client

    user, secret = totp_user
    client = _Client()
    client.post(
        "/api/v1/auth/login",
        {"username": user.username, "password": PASSWORD},
        content_type="application/json",
    )
    r = client.post("/api/v1/auth/mfa/totp", {"code": totp_code(secret)}, content_type="application/json")
    cookie = r.cookies[settings.SESSION_COOKIE_NAME]
    assert int(cookie["max-age"]) == 35 * 24 * 3600
    assert cookie["httponly"] and cookie["samesite"] == "Strict"
