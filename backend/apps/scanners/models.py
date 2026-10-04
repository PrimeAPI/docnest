from __future__ import annotations

from django.db import models
from django.utils import timezone


class ScannerClient(models.Model):
    """An automated device allowed to use the upload API — never the web API."""

    class Scope(models.TextChoices):
        UPLOAD = "upload"
        UPLOAD_STATUS = "upload:status"

    name = models.CharField(max_length=100, unique=True)
    token_prefix = models.CharField(max_length=16, unique=True)  # public part, used for lookup
    token_hash = models.CharField(max_length=64)  # SHA-256 of the full token
    scopes = models.JSONField(default=list)
    allowed_ips = models.JSONField(default=list, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_used_ip = models.GenericIPAddressField(null=True, blank=True)

    @property
    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        return not (self.expires_at is not None and self.expires_at <= timezone.now())


class IdempotencyKey(models.Model):
    scanner = models.ForeignKey(ScannerClient, on_delete=models.CASCADE)
    key_hash = models.CharField(max_length=64)
    document = models.ForeignKey("documents.Document", on_delete=models.CASCADE)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["scanner", "key_hash"], name="idempotency_unique")]
