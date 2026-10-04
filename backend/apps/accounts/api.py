from __future__ import annotations

from datetime import datetime
from typing import Any

from django.contrib.auth import authenticate, logout, password_validation
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.accounts import services, webauthn_service
from apps.accounts.models import TotpDevice, User, UserSession, WebAuthnCredential
from apps.audit.models import AuditLog
from apps.audit.service import audit
from apps.core.auth import enrollment_auth, mfa_auth, require_csrf, require_recent_auth

auth_router = Router(tags=["auth"])
account_router = Router(tags=["account"], auth=mfa_auth)

GENERIC_LOGIN_ERROR = "Invalid username or password"
FAILED_LOGIN_ACTIONS = ["login.failed", "login.mfa_failed", "login.throttled"]
LOGIN_ACTIONS = ["login.success", *FAILED_LOGIN_ACTIONS]


# --- Schemas -----------------------------------------------------------------


class LoginIn(Schema):
    username: str
    password: str


class MfaMethodsOut(Schema):
    webauthn: bool
    totp: bool
    recovery: bool


class PreviousLoginOut(Schema):
    at: datetime
    ip: str | None
    user_agent: str
    method: str


class SessionStateOut(Schema):
    authenticated: bool
    mfa_complete: bool
    needs_enrollment: bool
    pending_mfa: MfaMethodsOut | None = None
    username: str | None = None
    has_recovery_codes: bool = False
    previous_login: PreviousLoginOut | None = None
    failed_since_previous_login: int = 0


class CodeIn(Schema):
    code: str


class CredentialIn(Schema):
    credential: dict[str, Any]
    name: str = ""


class PasswordIn(Schema):
    password: str


class ChangePasswordIn(Schema):
    current_password: str
    new_password: str


class TotpSetupOut(Schema):
    secret: str
    uri: str


class RecoveryCodesOut(Schema):
    codes: list[str]


class DeviceOut(Schema):
    id: int
    kind: str
    name: str
    created_at: datetime
    last_used_at: datetime | None = None


class RevokedOut(Schema):
    revoked: int


class SessionOut(Schema):
    id: int
    current: bool
    created_at: datetime
    last_seen_at: datetime
    ip: str | None
    user_agent: str


# --- Helpers -----------------------------------------------------------------


def _me(request: HttpRequest) -> User:
    return request.user  # type: ignore[return-value]  # endpoints are authenticated


def _login_history(username: str) -> tuple[PreviousLoginOut | None, int]:
    """The sign-in before the current one, and failed attempts since then."""
    logins = list(
        AuditLog.objects.filter(subject__iexact=username, action="login.success").order_by("-id")[:2]
    )
    if len(logins) < 2:
        return None, 0
    current, previous = logins
    failed = AuditLog.objects.filter(
        subject__iexact=username,
        action__in=FAILED_LOGIN_ACTIONS,
        id__gt=previous.id,
        id__lt=current.id,
    ).count()
    return (
        PreviousLoginOut(
            at=previous.created_at,
            ip=previous.ip,
            user_agent=previous.user_agent,
            method=str(previous.details.get("method", "")),
        ),
        failed,
    )


def _state(request: HttpRequest) -> SessionStateOut:
    user = request.user
    if user.is_authenticated:
        mfa_complete = services.is_mfa_authenticated(request)
        previous, failed = _login_history(user.get_username()) if mfa_complete else (None, 0)
        return SessionStateOut(
            authenticated=True,
            mfa_complete=mfa_complete,
            needs_enrollment=not mfa_complete and not user.has_mfa,
            username=user.get_username(),
            has_recovery_codes=user.recovery_codes.filter(used_at__isnull=True).exists(),
            previous_login=previous,
            failed_since_previous_login=failed,
        )
    pending = services.get_pending_user(request)
    if pending is not None:
        m = services.MfaMethods.for_user(pending)
        return SessionStateOut(
            authenticated=False,
            mfa_complete=False,
            needs_enrollment=False,
            pending_mfa=MfaMethodsOut(webauthn=m.webauthn, totp=m.totp, recovery=m.recovery),
        )
    return SessionStateOut(authenticated=False, mfa_complete=False, needs_enrollment=False)


def _pending_or_401(request: HttpRequest) -> User:
    user = services.get_pending_user(request)
    if user is None:
        raise HttpError(401, "Login expired — please sign in again")
    try:
        services.check_throttle(request, user.get_username())
    except services.Throttled as exc:
        raise HttpError(429, "Too many attempts. Try again later.") from exc
    return user


def _mfa_failed(request: HttpRequest, user: User, method: str) -> None:
    services.register_failure(request, user.get_username())
    audit("login.mfa_failed", request=request, subject=user.get_username(), method=method)
    raise HttpError(400, "Verification failed")


def _finish_mfa(request: HttpRequest, user: User, method: str) -> SessionStateOut:
    services.complete_login(request, user, mfa_ok=True)
    audit("login.success", request=request, user=user, method=method)
    return _state(request)


# --- Login flow (no session required) ---------------------------------------


@auth_router.get("/session", auth=None, response=SessionStateOut)
def session_state(request: HttpRequest) -> SessionStateOut:
    return _state(request)


@auth_router.post("/login", auth=None, response=SessionStateOut)
def login_password(request: HttpRequest, data: LoginIn) -> SessionStateOut:
    require_csrf(request)
    username = data.username.strip()[:150]
    try:
        services.check_throttle(request, username)
    except services.Throttled as exc:
        audit("login.throttled", request=request, subject=username)
        raise HttpError(429, "Too many attempts. Try again later.") from exc

    user = authenticate(request, username=username, password=data.password)
    if user is None:
        services.register_failure(request, username)
        audit("login.failed", request=request, subject=username)
        raise HttpError(400, GENERIC_LOGIN_ERROR)

    if user.has_mfa:
        services.set_pending_user(request, user)
        audit("login.password_ok", request=request, subject=username)
    else:
        # No second factor yet: partial session that can only enroll one.
        services.complete_login(request, user, mfa_ok=False)
        audit("login.enrollment_required", request=request, user=user)
    return _state(request)


@auth_router.post("/mfa/totp", auth=None, response=SessionStateOut)
def login_totp(request: HttpRequest, data: CodeIn) -> SessionStateOut:
    require_csrf(request)
    user = _pending_or_401(request)
    if not services.verify_any_totp(user, data.code):
        _mfa_failed(request, user, "totp")
    return _finish_mfa(request, user, "totp")


@auth_router.post("/mfa/recovery", auth=None, response=SessionStateOut)
def login_recovery(request: HttpRequest, data: CodeIn) -> SessionStateOut:
    require_csrf(request)
    user = _pending_or_401(request)
    if not services.use_recovery_code(user, data.code):
        _mfa_failed(request, user, "recovery")
    audit("login.recovery_code_used", request=request, user=user)
    return _finish_mfa(request, user, "recovery")


@auth_router.post("/mfa/webauthn/options", auth=None)
def login_webauthn_options(request: HttpRequest) -> dict[str, Any]:
    require_csrf(request)
    user = _pending_or_401(request)
    return webauthn_service.authentication_options(request, user)


@auth_router.post("/mfa/webauthn/verify", auth=None, response=SessionStateOut)
def login_webauthn_verify(request: HttpRequest, data: CredentialIn) -> SessionStateOut:
    require_csrf(request)
    user = _pending_or_401(request)
    try:
        webauthn_service.authenticate(request, user, data.credential)
    except webauthn_service.WebAuthnError:
        _mfa_failed(request, user, "webauthn")
    return _finish_mfa(request, user, "webauthn")


@auth_router.post("/logout", auth=None, response=SessionStateOut)
def logout_view(request: HttpRequest) -> SessionStateOut:
    require_csrf(request)
    if request.user.is_authenticated:
        audit("logout", request=request)
        UserSession.objects.filter(session_key=request.session.session_key).delete()
    logout(request)
    return _state(request)


@auth_router.post("/reauth", auth=enrollment_auth)
def reauthenticate(request: HttpRequest, data: PasswordIn) -> dict[str, bool]:
    user = request.user
    try:
        services.check_throttle(request, user.get_username())
    except services.Throttled as exc:
        raise HttpError(429, "Too many attempts. Try again later.") from exc
    if not user.check_password(data.password):
        services.register_failure(request, user.get_username())
        audit("reauth.failed", request=request)
        raise HttpError(400, "Wrong password")
    services.mark_reauthenticated(request)
    return {"ok": True}


# --- Enrollment (allowed before MFA is complete, only if user has no MFA) -----


def _enrollment_allowed(request: HttpRequest) -> User:
    user: User = request.user  # type: ignore[assignment]
    if services.is_mfa_authenticated(request):
        require_recent_auth(request)
        return user
    if user.has_mfa:
        raise HttpError(403, "Second factor required")
    return user


@account_router.post("/totp/setup", auth=enrollment_auth, response=TotpSetupOut)
def totp_setup(request: HttpRequest) -> TotpSetupOut:
    user = _enrollment_allowed(request)
    _device, secret = services.create_totp_device(user)
    return TotpSetupOut(secret=secret, uri=services.totp_uri(user, secret))


@account_router.post("/totp/confirm", auth=enrollment_auth, response=SessionStateOut)
def totp_confirm(request: HttpRequest, data: CodeIn) -> SessionStateOut:
    user = _enrollment_allowed(request)
    device = TotpDevice.objects.filter(user=user, confirmed=False).order_by("-created_at").first()
    if device is None:
        raise HttpError(400, "Start the setup first")
    if not services.verify_totp(device, data.code):
        raise HttpError(400, "Code is not valid — check the time on your device and try again")
    device.confirmed = True
    device.save(update_fields=["confirmed"])
    audit("mfa.totp_added", request=request)
    if not services.is_mfa_authenticated(request):
        services.promote_to_mfa(request, user)
        audit("login.success", request=request, user=user, method="enrollment")
    return _state(request)


@account_router.post("/webauthn/register/options", auth=enrollment_auth)
def webauthn_register_options(request: HttpRequest) -> dict[str, Any]:
    user = _enrollment_allowed(request)
    return webauthn_service.registration_options(request, user)


@account_router.post("/webauthn/register/verify", auth=enrollment_auth, response=SessionStateOut)
def webauthn_register_verify(request: HttpRequest, data: CredentialIn) -> SessionStateOut:
    user = _enrollment_allowed(request)
    try:
        webauthn_service.register(request, user, data.credential, data.name)
    except webauthn_service.WebAuthnError as exc:
        raise HttpError(400, str(exc)) from exc
    audit("mfa.webauthn_added", request=request)
    if not services.is_mfa_authenticated(request):
        services.promote_to_mfa(request, user)
        audit("login.success", request=request, user=user, method="enrollment")
    return _state(request)


@account_router.post("/recovery-codes", response=RecoveryCodesOut)
def regenerate_recovery_codes(request: HttpRequest) -> RecoveryCodesOut:
    user: User = request.user  # type: ignore[assignment]
    # Right after the first enrollment no re-auth is needed; afterwards it is.
    if user.recovery_codes.exists():
        require_recent_auth(request)
    codes = services.generate_recovery_codes(user)
    audit("mfa.recovery_codes_generated", request=request)
    return RecoveryCodesOut(codes=codes)


@account_router.get("/devices", response=list[DeviceOut])
def list_devices(request: HttpRequest) -> list[DeviceOut]:
    user: User = request.user  # type: ignore[assignment]
    out = [
        DeviceOut(id=c.pk, kind="webauthn", name=c.name, created_at=c.created_at, last_used_at=c.last_used_at)
        for c in user.webauthn_credentials.order_by("created_at")
    ]
    out += [
        DeviceOut(id=d.pk, kind="totp", name=d.name, created_at=d.created_at, last_used_at=d.last_used_at)
        for d in user.totp_devices.filter(confirmed=True).order_by("created_at")
    ]
    return out


@account_router.delete("/devices/{kind}/{device_id}")
def delete_device(request: HttpRequest, kind: str, device_id: int) -> dict[str, bool]:
    require_recent_auth(request)
    user: User = request.user  # type: ignore[assignment]
    total = user.webauthn_credentials.count() + user.totp_devices.filter(confirmed=True).count()
    if total <= 1:
        raise HttpError(400, "You cannot remove your last second factor")
    model = WebAuthnCredential if kind == "webauthn" else TotpDevice if kind == "totp" else None
    if model is None:
        raise HttpError(404, "Not found")
    deleted, _ = model.objects.filter(user=user, pk=device_id).delete()
    if not deleted:
        raise HttpError(404, "Not found")
    audit("mfa.device_removed", request=request, kind=kind)
    return {"ok": True}


@account_router.post("/password")
def change_password(request: HttpRequest, data: ChangePasswordIn) -> dict[str, bool]:
    user: User = request.user  # type: ignore[assignment]
    if not user.check_password(data.current_password):
        services.register_failure(request, user.get_username())
        raise HttpError(400, "Current password is wrong")
    try:
        password_validation.validate_password(data.new_password, user)
    except ValidationError as exc:
        raise HttpError(400, " ".join(exc.messages)) from exc
    user.set_password(data.new_password)
    user.save(update_fields=["password"])
    # Keep this session, end all others.
    current = request.session.session_key
    others = UserSession.objects.filter(user=user).exclude(session_key=current)
    Session.objects.filter(session_key__in=others.values_list("session_key", flat=True)).delete()
    others.delete()
    from django.contrib.auth import update_session_auth_hash

    update_session_auth_hash(request, user)
    UserSession.objects.filter(session_key=current).update(session_key=request.session.session_key)
    audit("account.password_changed", request=request)
    return {"ok": True}


@account_router.get("/sessions", response=list[SessionOut])
def list_sessions(request: HttpRequest) -> list[SessionOut]:
    rows = UserSession.objects.filter(user=_me(request)).order_by("-last_seen_at")
    valid = set(
        Session.objects.filter(
            session_key__in=[r.session_key for r in rows], expire_date__gt=timezone.now()
        ).values_list("session_key", flat=True)
    )
    return [
        SessionOut(
            id=r.pk,
            current=r.session_key == request.session.session_key,
            created_at=r.created_at,
            last_seen_at=r.last_seen_at,
            ip=r.ip,
            user_agent=r.user_agent,
        )
        for r in rows
        if r.session_key in valid
    ]


@account_router.post("/sessions/revoke-others", response=RevokedOut)
def revoke_other_sessions(request: HttpRequest) -> dict[str, int]:
    others = UserSession.objects.filter(user=_me(request)).exclude(session_key=request.session.session_key)
    keys = list(others.values_list("session_key", flat=True))
    Session.objects.filter(session_key__in=keys).delete()
    others.delete()
    audit("account.sessions_revoked", request=request, count=len(keys))
    return {"revoked": len(keys)}


@account_router.delete("/sessions/{session_id}")
def revoke_session(request: HttpRequest, session_id: int) -> dict[str, bool]:
    row = UserSession.objects.filter(user=_me(request), pk=session_id).first()
    if row is None:
        raise HttpError(404, "Not found")
    Session.objects.filter(session_key=row.session_key).delete()
    row.delete()
    audit("account.session_revoked", request=request)
    return {"ok": True}


# --- Sign-in activity ------------------------------------------------------------


class ActivityActionOut(Schema):
    at: datetime
    action: str
    target: str
    target_title: str | None = None
    details: dict[str, Any]


class SignInOut(Schema):
    id: int
    at: datetime
    outcome: str  # success | failed | wrong_second_factor | blocked
    method: str
    ip: str | None
    user_agent: str
    current: bool
    active: bool
    last_activity_at: datetime | None
    actions: list[ActivityActionOut]


_OUTCOMES = {
    "login.success": "success",
    "login.failed": "failed",
    "login.mfa_failed": "wrong_second_factor",
    "login.throttled": "blocked",
}


@account_router.get("/activity", response=list[SignInOut])
def sign_in_activity(request: HttpRequest, limit: int = 50) -> list[SignInOut]:
    """Sign-ins (successful and failed) of the current user and what happened in each session."""
    from apps.documents.crypto_fields import get_title
    from apps.documents.models import Document

    me = _me(request)
    events = list(
        AuditLog.objects.filter(subject__iexact=me.get_username(), action__in=LOGIN_ACTIONS).order_by("-id")[
            : min(limit, 200)
        ]
    )
    refs = [e.session_ref for e in events if e.session_ref]
    actions_by_ref: dict[str, list[AuditLog]] = {}
    for a in (
        AuditLog.objects.filter(session_ref__in=refs)
        .exclude(action__in=LOGIN_ACTIONS)
        .exclude(action="login.password_ok")
        .order_by("id")
    ):
        actions_by_ref.setdefault(a.session_ref, []).append(a)

    doc_ids = {a.target for acts in actions_by_ref.values() for a in acts if a.action.startswith("document.")}
    titles: dict[str, str] = {}
    for doc in Document.objects.filter(uuid__in=[t for t in doc_ids if len(t) == 36]):
        try:
            titles[str(doc.uuid)] = get_title(doc)
        except Exception:  # noqa: S112 - a broken value must not break the log view
            continue

    active_keys = set(
        UserSession.objects.filter(user=me)
        .filter(session_key__in=Session.objects.filter(expire_date__gt=timezone.now()).values("session_key"))
        .values_list("pk", flat=True)
    )
    current_ref = ""
    current_pk = (
        UserSession.objects.filter(session_key=request.session.session_key)
        .values_list("pk", flat=True)
        .first()
    )
    if current_pk:
        current_ref = f"s{current_pk}"

    out = []
    for e in events:
        acts = actions_by_ref.get(e.session_ref, []) if e.session_ref else []
        out.append(
            SignInOut(
                id=e.pk,
                at=e.created_at,
                outcome=_OUTCOMES.get(e.action, e.action),
                method=str(e.details.get("method", "password")),
                ip=e.ip,
                user_agent=e.user_agent,
                current=bool(e.session_ref) and e.session_ref == current_ref,
                active=bool(e.session_ref)
                and e.session_ref.startswith("s")
                and int(e.session_ref[1:]) in active_keys,
                last_activity_at=acts[-1].created_at if acts else None,
                actions=[
                    ActivityActionOut(
                        at=a.created_at,
                        action=a.action,
                        target=a.target,
                        target_title=titles.get(a.target),
                        details=a.details,
                    )
                    for a in acts[-200:]
                ],
            )
        )
    return out
