from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

from apps.taxonomy.models import Bucket, Correspondent, DocumentType, Series, Tag


class Source(models.TextChoices):
    AUTO = "auto"
    SCANNER = "scanner"
    USER = "user"


class Document(models.Model):
    class Status(models.TextChoices):
        NEW = "new"
        TODO = "todo"
        DONE = "done"

    class Stage(models.TextChoices):
        RECEIVED = "received"
        VALIDATE = "validate"
        OCR = "ocr"
        ANALYZE = "analyze"
        STORE = "store"
        INDEX = "index"
        DONE = "done"

    class State(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        DONE = "done"
        FAILED = "failed"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    # Classification (plaintext by design: needed for filtering, low sensitivity)
    bucket = models.ForeignKey(Bucket, on_delete=models.PROTECT, related_name="documents")
    document_type = models.ForeignKey(DocumentType, null=True, blank=True, on_delete=models.SET_NULL)
    correspondent = models.ForeignKey(Correspondent, null=True, blank=True, on_delete=models.SET_NULL)
    series = models.ForeignKey(
        Series, null=True, blank=True, on_delete=models.SET_NULL, related_name="documents"
    )
    series_suggestion = models.ForeignKey(
        Series, null=True, blank=True, on_delete=models.SET_NULL, related_name="suggested_documents"
    )
    period_label = models.CharField(max_length=40, blank=True)
    tags = models.ManyToManyField(Tag, through="DocumentTag", related_name="documents")

    # Sensitive values: encrypted at rest
    title_enc = models.BinaryField(null=True)
    extracted_enc = models.BinaryField(null=True)  # JSON: amounts, IBANs, reference numbers, ...

    document_date = models.DateField(null=True, blank=True)
    uploaded_at = models.DateTimeField(default=timezone.now, db_index=True)
    received_from = models.ForeignKey(
        "scanners.ScannerClient", null=True, blank=True, on_delete=models.SET_NULL, related_name="documents"
    )

    # Inbox state
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NEW, db_index=True)
    is_important = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)

    # File & processing
    content_hash = models.CharField(max_length=64)  # HMAC-SHA256 of the uploaded file (dedupe)
    original_filename_enc = models.BinaryField(null=True)
    size = models.BigIntegerField(default=0)
    page_count = models.PositiveIntegerField(default=0)
    processing_stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.RECEIVED)
    processing_state = models.CharField(
        max_length=20, choices=State.choices, default=State.PENDING, db_index=True
    )
    processing_error = models.CharField(max_length=500, blank=True)
    intake_path = models.CharField(max_length=300, blank=True)
    storage_original = models.JSONField(null=True, blank=True)
    storage_archive = models.JSONField(null=True, blank=True)
    field_sources = models.JSONField(default=dict, blank=True)  # field -> auto|scanner|user
    scanner_metadata_enc = models.BinaryField(null=True)

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-uploaded_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["content_hash"],
                condition=models.Q(deleted_at__isnull=True),
                name="document_hash_unique_active",
            )
        ]
        indexes = [models.Index(fields=["document_date"]), models.Index(fields=["status", "uploaded_at"])]

    def source_of(self, field: str) -> str:
        return self.field_sources.get(field, Source.AUTO)

    def set_source(self, field: str, source: str) -> None:
        self.field_sources = {**self.field_sources, field: source}


class DocumentTag(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE)
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.AUTO)
    confidence = models.FloatField(default=1.0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["document", "tag"], name="document_tag_unique")]


class DocumentContent(models.Model):
    document = models.OneToOneField(
        Document, on_delete=models.CASCADE, related_name="content", primary_key=True
    )
    text_enc = models.BinaryField()
    language = models.CharField(max_length=10, blank=True)
    length = models.PositiveIntegerField(default=0)


class DocumentThumbnail(models.Model):
    document = models.OneToOneField(
        Document, on_delete=models.CASCADE, related_name="thumbnail", primary_key=True
    )
    image_enc = models.BinaryField()


class ProcessingEvent(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="events")
    stage = models.CharField(max_length=20)
    outcome = models.CharField(max_length=20)  # ok | failed | retry
    message = models.CharField(max_length=500, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at"]
