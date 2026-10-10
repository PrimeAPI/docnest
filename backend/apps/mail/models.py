from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone


class MailMessage(models.Model):
    """An email forwarded to the import inbox: the context its documents arrived with.

    Subject, sender and text are encrypted like every other document content.
    """

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    # HMAC of the Message-ID: a message whose deletion failed is not imported twice.
    message_key = models.CharField(max_length=64, unique=True)
    subject_enc = models.BinaryField(null=True)
    sender_enc = models.BinaryField(null=True)
    text_enc = models.BinaryField(null=True)  # the email's text, forwarded headers included
    sent_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    imported_at = models.DateTimeField(null=True, blank=True)  # set once every file is in the intake
    # The email itself, rendered as a PDF document; its attachments point here via Document.mail.
    email_document = models.ForeignKey(
        "documents.Document", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-received_at"]
