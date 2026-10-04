from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    """A human user of the web UI. MFA is mandatory before any data access."""

    first_name = None  # type: ignore[assignment]
    last_name = None  # type: ignore[assignment]

    @property
    def has_mfa(self) -> bool:
        return self.webauthn_credentials.exists() or self.totp_devices.filter(confirmed=True).exists()


class WebAuthnCredential(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="webauthn_credentials")
    name = models.CharField(max_length=100)
    credential_id = models.BinaryField(unique=True)
    public_key = models.BinaryField()
    sign_count = models.BigIntegerField(default=0)
    transports = models.JSONField(default=list)
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)


class TotpDevice(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="totp_devices")
    name = models.CharField(max_length=100, default="Authenticator app")
    secret_enc = models.BinaryField()  # encrypted with the TOTP purpose key
    confirmed = models.BooleanField(default=False)
    last_used_step = models.BigIntegerField(default=0)  # prevents code replay
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)


class RecoveryCode(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="recovery_codes")
    code_hash = models.CharField(max_length=64, unique=True)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)


class LoginThrottle(models.Model):
    """Failure counter per account or per IP (DB-backed so it survives restarts)."""

    key = models.CharField(max_length=200, unique=True)
    failures = models.PositiveIntegerField(default=0)
    window_started_at = models.DateTimeField(default=timezone.now)
    locked_until = models.DateTimeField(null=True, blank=True)


class UserSession(models.Model):
    """Tracks Django sessions per user so they can be listed and revoked."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="tracked_sessions")
    session_key = models.CharField(max_length=40, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
