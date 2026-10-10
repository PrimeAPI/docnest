from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

from apps.taxonomy.models import Correspondent, DocumentType, Folder, Series, Tag


class Source(models.TextChoices):
    AUTO = "auto"
    SCANNER = "scanner"
    USER = "user"


class Document(models.Model):
    class OcrBackend(models.TextChoices):
        OCRMYPDF = "ocrmypdf", "OCRmyPDF / Tesseract"
        DOCLING = "docling", "Docling"

    class Status(models.TextChoices):
        NEW = "new"
        TODO = "todo"
        DONE = "done"

    class Stage(models.TextChoices):
        RECEIVED = "received"
        ASSEMBLE = "assemble"
        VALIDATE = "validate"
        ENHANCE = "enhance"
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
    folder = models.ForeignKey(
        Folder, null=True, blank=True, on_delete=models.SET_NULL, related_name="documents"
    )
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
    mail = models.ForeignKey(
        "mail.MailMessage", null=True, blank=True, on_delete=models.SET_NULL, related_name="documents"
    )  # arrived by email: the email itself and its attachments

    # Inbox state
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NEW, db_index=True)
    is_important = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)

    # File & processing
    content_hash = models.CharField(max_length=64)  # HMAC-SHA256 of the uploaded file (dedupe)
    original_filename_enc = models.BinaryField(null=True)
    size = models.BigIntegerField(default=0)
    page_count = models.PositiveIntegerField(default=0)  # physical pages of the stored archive
    original_page_count = models.PositiveIntegerField(default=0)  # of the untouched original
    # A lightweight presentation of the stored archive, never a replacement file.
    # {version, count, blank: [physical page numbers], order?: [visible physical page numbers]}
    page_view = models.JSONField(default=dict, blank=True)
    processing_stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.RECEIVED)
    processing_state = models.CharField(
        max_length=20, choices=State.choices, default=State.PENDING, db_index=True
    )
    processing_error = models.CharField(max_length=500, blank=True)
    ocr_backend = models.CharField(max_length=20, choices=OcrBackend.choices, blank=True)
    # Scan enhancement: {"settings": used settings, "summary": counts, "override": one-off settings
    # for the next run, "scanner_remove_blank": scanner asked to drop blank pages}
    enhancement = models.JSONField(default=dict, blank=True)
    # A reprocessing run limited to some steps: {"steps": ["ocr", "analyze"], "ai_model": "…"}.
    # Empty: every stage runs, with the system settings. Cleared when the run finishes.
    processing_plan = models.JSONField(default=dict, blank=True)
    intake_path = models.CharField(max_length=300, blank=True)
    storage_original = models.JSONField(null=True, blank=True)
    storage_archive = models.JSONField(null=True, blank=True)
    field_sources = models.JSONField(default=dict, blank=True)  # field -> auto|scanner|user
    scanner_metadata_enc = models.BinaryField(null=True)

    # Paper original: whether one exists and where it was put away
    has_paper = models.BooleanField(default=False)
    paper_location = models.ForeignKey(
        "paper.Location", null=True, blank=True, on_delete=models.SET_NULL, related_name="documents"
    )
    paper_placed_at = models.DateTimeField(null=True, blank=True)  # the batch it was put away with

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
    structured_enc = models.BinaryField(null=True)
    layout_enc = models.BinaryField(null=True)
    format = models.CharField(max_length=20, default="text")
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


class DocumentPage(models.Model):
    """A page as shown (of the archive): a small picture and a fingerprint to find it again.

    The fingerprint finds the same page in other documents — a page scanned twice, a letter
    in two scans — without keeping the page's text: shingle and number hashes of it, and a
    difference hash of the picture. Both are encrypted like the document's other content.
    """

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="page_prints")
    number = models.PositiveIntegerField()  # 1-based, in the archive
    thumbnail_enc = models.BinaryField(null=True)  # WebP, ~240 px wide
    fingerprint_enc = models.BinaryField(null=True)  # see apps.documents.pages.Fingerprint
    version = models.CharField(max_length=64)  # of the archive it was made from: stale when it changes

    class Meta:
        ordering = ["document", "number"]
        constraints = [models.UniqueConstraint(fields=["document", "number"], name="document_page_unique")]


class Alteration(models.Model):
    """A change to which pages make up which documents — merging, splitting, removing pages,
    putting a document in the trash — kept so it can be shown and undone.

    Original files are never changed. Page visibility/order is a same-document presentation;
    only genuinely separate outputs require new documents. Superseded sources are retained.
    """

    class Kind(models.TextChoices):
        COMPOSE = "compose"  # merge / split / extract, reusing processed pages and text
        TRASH = "trash"
        RESTORE = "restore"
        EDIT = "edit"  # details changed (title, sender, tags …): old and new values
        VIEW = "view"  # hide / reorder archive pages in the same document, without OCR

    class Actor(models.TextChoices):
        USER = "user"  # done by hand
        ASSISTANT = "assistant"  # a suggestion of the assistant, applied by the user

    kind = models.CharField(max_length=10, choices=Kind.choices)
    actor = models.CharField(max_length=10, choices=Actor.choices, default=Actor.USER)
    sources = models.JSONField(default=list)  # uuids of the documents it took pages from
    results = models.JSONField(default=list)  # uuids of the documents it made
    retired = models.JSONField(default=list)  # uuids it put in the trash
    # {"summary": "…", "outputs": [{"document": uuid, "pages": [[uuid, page], …]}], "task", "finding"}
    detail_enc = models.BinaryField(null=True)
    created_at = models.DateTimeField(default=timezone.now)
    undone_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
