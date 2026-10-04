"""Authentication building blocks: throttling, TOTP, recovery codes, sessions."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from datetime import timedelta

import pyotp
from django.conf import settings
from django.contrib.auth import login
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone

from apps.accounts.models import LoginThrottle, RecoveryCode, TotpDevice, User, UserSession
from apps.audit.service import client_ip
from apps.crypto.aead import decrypt_bytes, encrypt_bytes
from apps.crypto.hashing import keyed_hash
from apps.crypto.keys import Purpose

PENDING_USER_KEY = "pending_user_id"
PENDING_AT_KEY = "pending_at"
PENDING_TTL_SECONDS = 300
MFA_OK_KEY = "mfa_ok"
REAUTH_AT_KEY = "reauth_at"


# --- Throttling --------------------------------------------------------------


class Throttled(Exception):
    pass


def _throttle_keys(request: HttpRequest, username: str) -> list[tuple[str, int]]:
    keys = [(f"user:{username.strip().lower()}", settings.LOGIN_RATE_LIMIT_PER_ACCOUNT)]
    ip = client_ip(request)
    if ip:
        keys.append((f"ip:{ip}", settings.LOGIN_RATE_LIMIT_PER_IP))
    return keys


def check_throttle(request: HttpRequest, username: str) -> None:
    now = timezone.now()
    for key, _limit in _throttle_keys(request, username):
        row = LoginThrottle.objects.filter(key=key).first()
        if row and row.locked_until and row.locked_until > now:
            raise Throttled()


def register_failure(request: HttpRequest, username: str) -> None:
    now = timezone.now()
    window = timedelta(seconds=settings.LOGIN_LOCKOUT_SECONDS)
    with transaction.atomic():
        for key, limit in _throttle_keys(request, username):
            row, _ = LoginThrottle.objects.select_for_update().get_or_create(key=key)
            if now - row.window_started_at > window:
                row.failures = 0
                row.window_started_at = now
            row.failures += 1
            if row.failures >= limit:
                row.locked_until = now + window
            row.save()


def check_key_throttle(key: str) -> None:
    row = LoginThrottle.objects.filter(key=key).first()
    if row and row.locked_until and row.locked_until > timezone.now():
        raise Throttled()


def register_key_failure(key: str, limit: int) -> None:
    now = timezone.now()
    window = timedelta(seconds=settings.LOGIN_LOCKOUT_SECONDS)
    with transaction.atomic():
        row, _ = LoginThrottle.objects.select_for_update().get_or_create(key=key)
        if now - row.window_started_at > window:
            row.failures = 0
            row.window_started_at = now
        row.failures += 1
        if row.failures >= limit:
            row.locked_until = now + window
        row.save()


def clear_failures(username: str) -> None:
    LoginThrottle.objects.filter(key=f"user:{username.strip().lower()}").delete()


# --- Pending (password ok, second factor outstanding) ------------------------


def set_pending_user(request: HttpRequest, user: User) -> None:
    request.session.cycle_key()
    request.session[PENDING_USER_KEY] = user.pk
    request.session[PENDING_AT_KEY] = time.time()


def get_pending_user(request: HttpRequest) -> User | None:
    user_id = request.session.get(PENDING_USER_KEY)
    started = request.session.get(PENDING_AT_KEY, 0)
    if not user_id or time.time() - started > PENDING_TTL_SECONDS:
        return None
    return User.objects.filter(pk=user_id, is_active=True).first()


def complete_login(request: HttpRequest, user: User, *, mfa_ok: bool) -> None:
    """Log the user in (rotates the session key) and start session tracking."""
    request.session.pop(PENDING_USER_KEY, None)
    request.session.pop(PENDING_AT_KEY, None)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session[MFA_OK_KEY] = mfa_ok
    request.session["last_activity"] = time.time()
    if mfa_ok:
        request.session[REAUTH_AT_KEY] = time.time()
        track_session(request, user)
    clear_failures(user.get_username())


def promote_to_mfa(request: HttpRequest, user: User) -> None:
    """After first MFA enrollment: upgrade the partially authenticated session."""
    request.session.cycle_key()
    request.session[MFA_OK_KEY] = True
    request.session[REAUTH_AT_KEY] = time.time()
    track_session(request, user)


def track_session(request: HttpRequest, user: User) -> None:
    request.session.save()
    UserSession.objects.update_or_create(
        session_key=request.session.session_key,
        defaults={
            "user": user,
            "ip": client_ip(request),
            "user_agent": request.META.get("HTTP_USER_AGENT", "")[:300],
        },
    )
    request.session["tracked"] = True


def is_mfa_authenticated(request: HttpRequest) -> bool:
    return bool(request.user.is_authenticated and request.session.get(MFA_OK_KEY))


def mark_reauthenticated(request: HttpRequest) -> None:
    request.session[REAUTH_AT_KEY] = time.time()


def recently_authenticated(request: HttpRequest) -> bool:
    return time.time() - request.session.get(REAUTH_AT_KEY, 0) <= settings.REAUTH_MAX_AGE_SECONDS


# --- TOTP --------------------------------------------------------------------


def _totp_aad(user_id: int) -> bytes:
    return f"totp:{user_id}".encode()


def create_totp_device(user: User) -> tuple[TotpDevice, str]:
    TotpDevice.objects.filter(user=user, confirmed=False).delete()
    secret = pyotp.random_base32(length=32)
    device = TotpDevice.objects.create(
        user=user, secret_enc=encrypt_bytes(secret.encode(), purpose=Purpose.TOTP, aad=_totp_aad(user.pk))
    )
    return device, secret


def totp_uri(user: User, secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=user.get_username(), issuer_name="DocNest")


def verify_totp(device: TotpDevice, code: str) -> bool:
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6:
        return False
    secret = decrypt_bytes(
        bytes(device.secret_enc), purpose=Purpose.TOTP, aad=_totp_aad(device.user_id)
    ).decode()
    totp = pyotp.TOTP(secret)
    now = time.time()
    for offset in (-1, 0, 1):
        step = int(now // totp.interval) + offset
        if step <= device.last_used_step:
            continue  # replay protection: each step can be used once
        if secrets.compare_digest(totp.at(step * totp.interval), code):
            device.last_used_step = step
            device.last_used_at = timezone.now()
            device.save(update_fields=["last_used_step", "last_used_at"])
            return True
    return False


def verify_any_totp(user: User, code: str) -> bool:
    return any(verify_totp(d, code) for d in user.totp_devices.filter(confirmed=True))


# --- Recovery codes ----------------------------------------------------------

RECOVERY_CODE_COUNT = 10
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def _normalize_recovery(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch.isalnum())


def generate_recovery_codes(user: User) -> list[str]:
    codes = [
        "-".join("".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3)) for _ in range(10)
    ]
    with transaction.atomic():
        RecoveryCode.objects.filter(user=user).delete()
        RecoveryCode.objects.bulk_create(
            RecoveryCode(user=user, code_hash=keyed_hash(_normalize_recovery(c).encode())) for c in codes
        )
    return codes


def use_recovery_code(user: User, code: str) -> bool:
    digest = keyed_hash(_normalize_recovery(code).encode())
    updated = RecoveryCode.objects.filter(user=user, code_hash=digest, used_at__isnull=True).update(
        used_at=timezone.now()
    )
    return updated == 1


@dataclass
class MfaMethods:
    webauthn: bool
    totp: bool
    recovery: bool

    @classmethod
    def for_user(cls, user: User) -> MfaMethods:
        return cls(
            webauthn=user.webauthn_credentials.exists(),
            totp=user.totp_devices.filter(confirmed=True).exists(),
            recovery=user.recovery_codes.filter(used_at__isnull=True).exists(),
        )
