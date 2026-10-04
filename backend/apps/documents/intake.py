"""Durable document intake.

A scanner upload is acknowledged only after the file has been encrypted to the
intake volume (fsync'd) and the document row + processing job are committed.
From that point on the document cannot be lost: it stays in the intake until
the storage backend has verifiably stored it.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.crypto.aead import encrypt_bytes, encrypt_file, encrypt_text
from apps.crypto.keys import Purpose, derive
from apps.documents.models import Document, DocumentTag, Source
from apps.processing.models import Job
from apps.processing.queue import enqueue
from apps.taxonomy.models import Bucket, DocumentType
from apps.taxonomy.services import resolve_or_create_tag

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"
MAX_METADATA_BYTES = 8 * 1024


class IntakeError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class IntakeRequest:
    bucket: str
    document_type: str | None = None  # None or "auto" = classify automatically
    important: bool = False
    todo: bool = False
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)
    filename: str = ""


@dataclass
class IntakeResult:
    document: Document
    created: bool


def _resolve_bucket(value: str) -> Bucket:
    value = value.strip()
    bucket = (
        Bucket.objects.filter(slug__iexact=value).first() or Bucket.objects.filter(name__iexact=value).first()
    )
    if bucket is None:
        raise IntakeError(f"Unknown bucket '{value[:50]}'")
    return bucket


def _resolve_type(value: str | None) -> DocumentType | None:
    if not value or value.strip().lower() == "auto":
        return None
    value = value.strip()
    doc_type = (
        DocumentType.objects.filter(slug__iexact=value).first()
        or DocumentType.objects.filter(name__iexact=value).first()
    )
    if doc_type is None:
        raise IntakeError(f"Unknown document type '{value[:50]}'")
    return doc_type


def content_hash_of(fileobj: BinaryIO) -> tuple[str, int]:
    """Keyed hash of the file (dedupe without revealing a plain SHA-256)."""
    mac = hmac.new(derive(Purpose.TOKENS), digestmod=hashlib.sha256)
    size = 0
    fileobj.seek(0)
    for chunk in iter(lambda: fileobj.read(1024 * 1024), b""):
        mac.update(chunk)
        size += len(chunk)
    fileobj.seek(0)
    return mac.hexdigest(), size


def intake_path_for(doc_uuid: str) -> Path:
    return settings.INTAKE_DIR / f"{doc_uuid}.pdf.enc"


def intake_aad(doc_uuid: str) -> bytes:
    return f"intake:{doc_uuid}".encode()


def archive_intake_path_for(doc_uuid: str) -> Path:
    return settings.INTAKE_DIR / f"{doc_uuid}.archive.pdf.enc"


def archive_aad(doc_uuid: str) -> bytes:
    return f"intake-archive:{doc_uuid}".encode()


def ensure_dirs() -> None:
    for path in (settings.INTAKE_DIR, settings.WORK_DIR, Path(settings.FILE_UPLOAD_TEMP_DIR)):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)


def receive(upload_path: Path, req: IntakeRequest, *, scanner: object = None) -> IntakeResult:
    """Validate, deduplicate, encrypt and register an uploaded PDF."""
    ensure_dirs()
    size = upload_path.stat().st_size
    if size == 0:
        raise IntakeError("Empty file")
    if size > settings.MAX_UPLOAD_BYTES:
        raise IntakeError("File too large", status=413)
    with open(upload_path, "rb") as f:
        head = f.read(1024)
        if PDF_MAGIC not in head:
            raise IntakeError("Only PDF documents are accepted", status=415)
        digest, _ = content_hash_of(f)

    existing = Document.objects.filter(content_hash=digest, deleted_at__isnull=True).first()
    if existing is not None:
        return IntakeResult(existing, created=False)

    bucket = _resolve_bucket(req.bucket)
    doc_type = _resolve_type(req.document_type)
    metadata_json = json.dumps(req.metadata or {}, ensure_ascii=False)
    if len(metadata_json.encode()) > MAX_METADATA_BYTES:
        raise IntakeError("Metadata too large")
    tags = [t.strip()[:80] for t in req.tags if t and t.strip()][:20]

    document = Document(
        bucket=bucket,
        document_type=doc_type,
        content_hash=digest,
        size=size,
        status=Document.Status.TODO if req.todo else Document.Status.NEW,
        is_important=req.important,
        received_from=scanner,  # type: ignore[misc]
        scanner_metadata_enc=encrypt_bytes(metadata_json.encode()) if req.metadata else None,
        original_filename_enc=encrypt_text(req.filename[:200]) if req.filename else None,
    )
    sources = {"bucket": Source.SCANNER}
    if doc_type is not None:
        sources["document_type"] = Source.SCANNER
    if req.todo or req.important:
        sources["status"] = Source.SCANNER
    document.field_sources = sources

    target = intake_path_for(str(document.uuid))
    encrypt_file(upload_path, target, aad=intake_aad(str(document.uuid)))
    try:
        with transaction.atomic():
            document.intake_path = target.name
            document.save()
            for name in tags:
                tag = resolve_or_create_tag(name, suggested=False)
                DocumentTag.objects.get_or_create(
                    document=document, tag=tag, defaults={"source": Source.SCANNER}
                )
            enqueue(Job.Kind.PROCESS_DOCUMENT, document=document)
    except IntegrityError:
        # Concurrent upload of the same file: keep the winner, drop our copy.
        target.unlink(missing_ok=True)
        existing = Document.objects.filter(content_hash=digest, deleted_at__isnull=True).first()
        if existing is None:
            raise
        return IntakeResult(existing, created=False)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    logger.info("document received", extra={"document": str(document.uuid), "size": size})
    return IntakeResult(document, created=True)


def remove_intake(document: Document) -> None:
    if document.intake_path:
        path = settings.INTAKE_DIR / Path(document.intake_path).name
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
