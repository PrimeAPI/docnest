"""Durable document intake.

A scanner upload is acknowledged only after the file has been encrypted to the
intake volume (fsync'd) and the document row + processing job are committed.
From that point on the document cannot be lost: it stays in the intake until
the storage backend has verifiably stored it.

Uploads are either a single PDF (stored as the original right away) or raw
scanner output — page images and/or several files — stored as encrypted parts
in `<uuid>.parts/` and turned into the original PDF by the worker.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from django.conf import settings
from django.db import IntegrityError, transaction
from PIL import Image, UnidentifiedImageError

from apps.crypto.aead import encrypt_bytes, encrypt_file, encrypt_text
from apps.crypto.keys import Purpose, derive
from apps.documents.models import Document, DocumentTag, Source
from apps.processing.assemble import IMAGE_FORMATS, AssemblyOptions
from apps.processing.models import Job
from apps.processing.preferences import get_default_ocr_backend
from apps.processing.queue import enqueue
from apps.taxonomy.models import DocumentType, Folder
from apps.taxonomy.services import ensure_folder_path, resolve_or_create_tag, split_folder_path

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"
MAX_METADATA_BYTES = 8 * 1024
PART_PDF = "pdf"
PART_IMAGE = "image"
ACCEPTED_TYPES = "PDF, PNG, JPEG, TIFF, PNM/PBM/PGM/PPM, BMP, GIF, WebP"
MANIFEST = "manifest.json"


class IntakeError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class IntakeRequest:
    bucket: str = ""  # folder path, e.g. "Private/Taxes"; missing folders are created
    folder_id: int | None = None  # takes precedence over `bucket` (web upload)
    document_type: str | None = None  # None or "auto" = classify automatically
    important: bool = False
    todo: bool = False
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)
    filename: str = ""
    mail_id: int | None = None  # the email the file arrived with


@dataclass
class Part:
    path: Path
    kind: str  # PART_PDF | PART_IMAGE
    size: int
    digest: str


@dataclass
class IntakeResult:
    document: Document
    created: bool


def _check_folder_path(value: str) -> None:
    try:
        split_folder_path(value)
    except ValueError as exc:
        raise IntakeError(str(exc)) from exc


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


def enhanced_intake_path_for(doc_uuid: str) -> Path:
    return settings.INTAKE_DIR / f"{doc_uuid}.enhanced.pdf.enc"


def enhanced_aad(doc_uuid: str) -> bytes:
    return f"intake-enhanced:{doc_uuid}".encode()


def ensure_dirs() -> None:
    for path in (settings.INTAKE_DIR, settings.WORK_DIR, Path(settings.FILE_UPLOAD_TEMP_DIR)):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)


def sniff(path: Path) -> str:
    """Detect a part's type from its content (never from the file name or Content-Type)."""
    with open(path, "rb") as f:
        if PDF_MAGIC in f.read(1024):
            return PART_PDF
    try:
        with Image.open(path) as image:
            fmt = image.format
    except Image.DecompressionBombError as exc:
        raise IntakeError("Image dimensions are too large", status=413) from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise IntakeError(f"Unsupported file type; accepted: {ACCEPTED_TYPES}", status=415) from exc
    if fmt not in IMAGE_FORMATS:
        raise IntakeError(f"Unsupported file type; accepted: {ACCEPTED_TYPES}", status=415)
    return PART_IMAGE


def inspect_part(path: Path) -> Part:
    size = path.stat().st_size
    if size == 0:
        raise IntakeError("Empty file")
    if size > settings.MAX_UPLOAD_BYTES:
        raise IntakeError("File too large", status=413)
    kind = sniff(path)
    with open(path, "rb") as f:
        digest, _ = content_hash_of(f)
    return Part(path=path, kind=kind, size=size, digest=digest)


def scan_hash(parts: list[tuple[str, str]]) -> str:
    """Dedupe hash of a multi-part scan from its (kind, digest) parts.

    A single PDF keeps its plain content hash, so uploading the same PDF as a
    file or as a one-part scan is recognized as the same document.
    """
    if len(parts) == 1 and parts[0][0] == PART_PDF:
        return parts[0][1]
    mac = hmac.new(derive(Purpose.TOKENS), digestmod=hashlib.sha256)
    mac.update(b"docnest-scan-parts-v1")
    for kind, digest in parts:
        mac.update(f"|{kind}:{digest}".encode())
    return mac.hexdigest()


def check_limits(part_count: int, total_size: int) -> None:
    if part_count > settings.MAX_PAGES:
        raise IntakeError(f"Too many files ({part_count} > {settings.MAX_PAGES})", status=413)
    if total_size > settings.MAX_SCAN_BYTES:
        raise IntakeError("Scan too large", status=413)


# --- Multi-part intake: page images and PDFs that are assembled by the worker ----


def parts_dir_for(doc_uuid: str) -> Path:
    return settings.INTAKE_DIR / f"{doc_uuid}.parts"


def part_filename(position: int) -> str:
    return f"{position:05d}.enc"


def part_aad(batch_id: str, position: int) -> bytes:
    return f"intake-part:{batch_id}:{position}".encode()


def write_manifest(
    directory: Path, batch_id: str, parts: list[tuple[int, str]], options: AssemblyOptions
) -> None:
    """Describe the parts of a scan; contains no document content."""
    manifest = {
        "version": 1,
        "batch": batch_id,
        "parts": [{"position": pos, "kind": kind} for pos, kind in parts],
        "options": options.to_json(),
    }
    tmp = directory / f"{MANIFEST}.tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, directory / MANIFEST)


def read_manifest(directory: Path) -> dict[str, Any]:
    with open(directory / MANIFEST) as f:
        data: dict[str, Any] = json.load(f)
    return data


def remove_parts(doc_uuid: str) -> None:
    shutil.rmtree(parts_dir_for(doc_uuid), ignore_errors=True)


def receive(upload_path: Path, req: IntakeRequest, *, scanner: object = None) -> IntakeResult:
    """Validate, deduplicate, encrypt and register a single uploaded file (PDF or image)."""
    return receive_files([upload_path], req, scanner=scanner)


def receive_files(
    paths: list[Path],
    req: IntakeRequest,
    *,
    scanner: object = None,
    options: AssemblyOptions | None = None,
) -> IntakeResult:
    """Register one scan made of one or more files, in page order.

    A single PDF is stored as the original right away. Anything else (images,
    several files) is stored as encrypted parts; the worker's `assemble` stage
    builds the original PDF from them before any other processing.
    """
    ensure_dirs()
    if not paths:
        raise IntakeError("No file uploaded")
    check_limits(len(paths), 0)
    parts = [inspect_part(p) for p in paths]
    total = sum(p.size for p in parts)
    check_limits(len(parts), total)
    digest = scan_hash([(p.kind, p.digest) for p in parts])

    if len(parts) == 1 and parts[0].kind == PART_PDF:
        source = parts[0].path

        def store(doc_uuid: str) -> None:
            encrypt_file(source, intake_path_for(doc_uuid), aad=intake_aad(doc_uuid))

        def discard(doc_uuid: str) -> None:
            intake_path_for(doc_uuid).unlink(missing_ok=True)

        return register(req, digest=digest, size=total, scanner=scanner, store=store, discard=discard)

    opts = options or AssemblyOptions()

    def store_parts(doc_uuid: str) -> None:
        directory = parts_dir_for(doc_uuid)
        directory.mkdir(mode=0o700)
        for position, part in enumerate(parts, start=1):
            encrypt_file(part.path, directory / part_filename(position), aad=part_aad(doc_uuid, position))
        write_manifest(directory, doc_uuid, [(i, p.kind) for i, p in enumerate(parts, start=1)], opts)

    return register(req, digest=digest, size=total, scanner=scanner, store=store_parts, discard=remove_parts)


def validate_request(req: IntakeRequest) -> None:
    """Fail early on request fields that would be rejected at registration."""
    _check_folder_path(req.bucket)
    _resolve_type(req.document_type)
    if len(json.dumps(req.metadata or {}, ensure_ascii=False).encode()) > MAX_METADATA_BYTES:
        raise IntakeError("Metadata too large")


def register(
    req: IntakeRequest,
    *,
    digest: str,
    size: int,
    scanner: object,
    store: Callable[[str], None],
    discard: Callable[[str], None],
) -> IntakeResult:
    """Create the document for already-validated content.

    `store(doc_uuid)` must durably place the encrypted file(s) in the intake;
    `discard(doc_uuid)` undoes it if the document cannot be committed.
    """
    existing = Document.objects.filter(content_hash=digest, deleted_at__isnull=True).first()
    if existing is not None:
        return IntakeResult(existing, created=False)

    _check_folder_path(req.bucket)
    doc_type = _resolve_type(req.document_type)
    metadata_json = json.dumps(req.metadata or {}, ensure_ascii=False)
    if len(metadata_json.encode()) > MAX_METADATA_BYTES:
        raise IntakeError("Metadata too large")
    tags = [t.strip()[:80] for t in req.tags if t and t.strip()][:20]

    document = Document(
        document_type=doc_type,
        content_hash=digest,
        size=size,
        status=Document.Status.TODO if req.todo else Document.Status.NEW,
        is_important=req.important,
        ocr_backend=get_default_ocr_backend(),
        received_from=scanner,  # type: ignore[misc]
        mail_id=req.mail_id,
        has_paper=scanner is not None,  # scanned = there is a sheet of paper; web uploads are often digital
        scanner_metadata_enc=encrypt_bytes(metadata_json.encode()) if req.metadata else None,
        original_filename_enc=encrypt_text(req.filename[:200]) if req.filename else None,
    )
    sources: dict[str, str] = {}
    if doc_type is not None:
        sources["document_type"] = Source.SCANNER
    if req.todo or req.important:
        sources["status"] = Source.SCANNER
    document.field_sources = sources

    doc_uuid = str(document.uuid)
    store(doc_uuid)
    try:
        with transaction.atomic():
            if req.folder_id is not None:
                document.folder = Folder.objects.filter(pk=req.folder_id).first()
            else:
                document.folder = ensure_folder_path(req.bucket)
            if document.folder is not None:
                document.set_source("folder", Source.SCANNER if scanner else Source.USER)
            document.intake_path = intake_path_for(doc_uuid).name
            document.save()
            for name in tags:
                tag = resolve_or_create_tag(name, suggested=False)
                DocumentTag.objects.get_or_create(
                    document=document, tag=tag, defaults={"source": Source.SCANNER}
                )
            enqueue(Job.Kind.INTAKE_DOCUMENT, document=document)
    except IntegrityError:
        # Concurrent upload of the same file: keep the winner, drop our copy.
        discard(doc_uuid)
        existing = Document.objects.filter(content_hash=digest, deleted_at__isnull=True).first()
        if existing is None:
            raise
        return IntakeResult(existing, created=False)
    except BaseException:
        discard(doc_uuid)
        raise
    logger.info("document received", extra={"document": doc_uuid, "size": size})
    return IntakeResult(document, created=True)


def remove_intake(document: Document) -> None:
    remove_parts(str(document.uuid))
    enhanced_intake_path_for(str(document.uuid)).unlink(missing_ok=True)
    if document.intake_path:
        path = settings.INTAKE_DIR / Path(document.intake_path).name
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
