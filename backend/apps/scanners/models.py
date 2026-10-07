from __future__ import annotations

import uuid

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


class ScanSession(models.Model):
    """A scan uploaded page by page (`/api/upload/v1/scans`), assembled into one document on completion.

    Page files live encrypted in `INTAKE_DIR/scans/<uuid>/` until the session is
    completed (they then become the document's intake parts) or expires.
    """

    class State(models.TextChoices):
        OPEN = "open"
        COMPLETED = "completed"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    scanner = models.ForeignKey(ScannerClient, on_delete=models.CASCADE, related_name="scan_sessions")
    state = models.CharField(max_length=20, choices=State.choices, default=State.OPEN)
    request_enc = models.BinaryField()  # JSON: bucket, type, flags, tags, metadata, assembly options
    idempotency_hash = models.CharField(max_length=64, blank=True)
    document = models.ForeignKey("documents.Document", null=True, blank=True, on_delete=models.SET_NULL)
    duplicate = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["scanner", "idempotency_hash"],
                condition=~models.Q(idempotency_hash=""),
                name="scan_session_idempotency_unique",
            )
        ]


class ScanPage(models.Model):
    """One uploaded file of a scan session (an image, a multi-page TIFF or a PDF)."""

    session = models.ForeignKey(ScanSession, on_delete=models.CASCADE, related_name="pages")
    position = models.PositiveIntegerField()
    kind = models.CharField(max_length=10)  # pdf | image
    size = models.BigIntegerField()
    digest = models.CharField(max_length=64)  # keyed hash of the content (dedupe)
    file_name = models.CharField(max_length=40)  # encrypted file in the session directory
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["position"]
        constraints = [models.UniqueConstraint(fields=["session", "position"], name="scan_page_unique")]
