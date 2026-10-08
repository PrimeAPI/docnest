from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from django.http import HttpRequest
from django.utils import timezone
from ninja import File, Form, Router, Schema, Status
from ninja.errors import HttpError
from ninja.files import UploadedFile

from apps.audit.service import audit
from apps.core.auth import require_recent_auth
from apps.crypto.hashing import keyed_hash
from apps.documents.intake import IntakeError, IntakeRequest, receive_files
from apps.documents.models import Document
from apps.processing.assemble import COMPRESSIONS, MAX_DPI, MIN_DPI, AssemblyOptions
from apps.scanners import sessions, tokens
from apps.scanners.auth import require_scope
from apps.scanners.models import IdempotencyKey, ScannerClient, ScanPage, ScanSession

upload_router = Router(tags=["upload"])
manage_router = Router(tags=["scanners"])


# --- Upload API (scanner tokens) -------------------------------------------------


class UploadAccepted(Schema):
    id: UUID
    status: str
    duplicate: bool
    status_url: str


class UploadStatus(Schema):
    id: UUID
    status: str
    stage: str
    error: str | None = None


def _status(document: Document) -> str:
    if document.processing_state == Document.State.DONE:
        return "processed"
    if document.processing_state == Document.State.FAILED:
        return "failed"
    return "processing"


def _parse_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _assembly_options(dpi: str, skip_blank_pages: str, compression: str) -> AssemblyOptions:
    resolution: int | None = None
    if dpi.strip():
        try:
            resolution = int(dpi)
        except ValueError as exc:
            raise HttpError(400, "dpi must be an integer") from exc
        if not MIN_DPI <= resolution <= MAX_DPI:
            raise HttpError(400, f"dpi must be between {MIN_DPI} and {MAX_DPI}")
    compression = (compression or "auto").strip().lower()
    if compression not in COMPRESSIONS:
        raise HttpError(400, f"compression must be one of: {', '.join(COMPRESSIONS)}")
    return AssemblyOptions(
        dpi=resolution, skip_blank_pages=_parse_bool(skip_blank_pages), compression=compression
    )


def _metadata(metadata: str) -> dict[str, Any]:
    try:
        meta: dict[str, Any] = json.loads(metadata) if metadata else {}
    except json.JSONDecodeError as exc:
        raise HttpError(400, "metadata must be a JSON object") from exc
    if not isinstance(meta, dict):
        raise HttpError(400, "metadata must be a JSON object")
    return meta


def _intake_request(
    bucket: str, document_type: str, important: str, todo: str, tags: str, metadata: str, filename: str = ""
) -> IntakeRequest:
    return IntakeRequest(
        bucket=bucket,
        document_type=document_type,
        important=_parse_bool(important),
        todo=_parse_bool(todo),
        tags=[t for t in tags.split(",") if t.strip()],
        metadata=_metadata(metadata),
        filename=filename,
    )


def _idempotency_key(request: HttpRequest) -> str:
    return request.headers.get("Idempotency-Key", "").strip()[:200]


@upload_router.post("/documents", response={200: UploadAccepted, 202: UploadAccepted})
def upload_document(
    request: HttpRequest,
    file: File[list[UploadedFile]],
    bucket: Form[str] = "",
    document_type: Form[str] = "auto",
    important: Form[str] = "false",
    todo: Form[str] = "false",
    tags: Form[str] = "",
    metadata: Form[str] = "",
    dpi: Form[str] = "",
    skip_blank_pages: Form[str] = "false",
    compression: Form[str] = "auto",
) -> Status:
    """Upload one scan: a PDF, page images, or several files (repeat `file`) in page order.

    Images and multiple files are assembled into one PDF by DocNest.
    `bucket` is the folder path to file the document in (e.g. `Private/Taxes`);
    missing folders are created, an empty value leaves the document unfiled.
    `tags` is a comma-separated list; `metadata` an optional JSON object.
    Send an `Idempotency-Key` header to make retries safe.
    """
    scanner = require_scope(request, ScannerClient.Scope.UPLOAD)
    try:
        idem = _idempotency_key(request)
        if idem:
            existing = IdempotencyKey.objects.filter(
                scanner=scanner, key_hash=keyed_hash(idem.encode())
            ).first()
            if existing:
                return Status(200, _accepted(existing.document, duplicate=True))

        req = _intake_request(
            bucket, document_type, important, todo, tags, metadata, filename=Path(file[0].name or "").name
        )
        options = _assembly_options(dpi, skip_blank_pages, compression)
        paths = [_upload_path(f) for f in file]
        try:
            result = receive_files(paths, req, scanner=scanner, options=options)
        except IntakeError as exc:
            audit("upload.rejected", request=request, scanner=scanner, reason=str(exc))
            raise HttpError(exc.status, str(exc)) from exc
    finally:
        for f in file:
            f.close()

    if idem:
        IdempotencyKey.objects.get_or_create(
            scanner=scanner, key_hash=keyed_hash(idem.encode()), defaults={"document": result.document}
        )
    audit(
        "upload.accepted" if result.created else "upload.duplicate",
        request=request,
        scanner=scanner,
        target=str(result.document.uuid),
    )
    if result.created:
        return Status(202, _accepted(result.document, duplicate=False))
    return Status(200, _accepted(result.document, duplicate=True))


def _upload_path(file: UploadedFile) -> Path:
    temp = getattr(file, "temporary_file_path", None)
    if temp is None:
        raise HttpError(500, "upload was not streamed to disk")
    return Path(temp())


def _accepted(document: Document, *, duplicate: bool) -> UploadAccepted:
    return UploadAccepted(
        id=document.uuid,
        status=_status(document),
        duplicate=duplicate,
        status_url=f"/api/upload/v1/documents/{document.uuid}",
    )


@upload_router.get("/documents/{doc_id}", response=UploadStatus)
def upload_status(request: HttpRequest, doc_id: UUID) -> UploadStatus:
    scanner = require_scope(request, ScannerClient.Scope.UPLOAD_STATUS)
    document = Document.objects.filter(uuid=doc_id, received_from=scanner).first()
    if document is None:
        raise HttpError(404, "Not found")
    return UploadStatus(
        id=document.uuid,
        status=_status(document),
        stage=document.processing_stage,
        error="Processing failed" if document.processing_state == Document.State.FAILED else None,
    )


@upload_router.get("/ping")
def ping(request: HttpRequest) -> dict[str, str]:
    scanner: ScannerClient = request.auth  # type: ignore[attr-defined]
    return {"status": "ok", "scanner": scanner.name}


# --- Scan sessions: page-by-page upload of long scans -------------------------------


class ScanSessionOut(Schema):
    id: UUID
    status: str  # open | completed
    pages: list[int]
    page_count: int
    size: int
    expires_at: datetime | None
    document_id: UUID | None = None
    duplicate: bool = False
    status_url: str | None = None


class ScanPageOut(Schema):
    page: int
    kind: str
    size: int
    page_count: int


def _session_out(session: ScanSession) -> ScanSessionOut:
    positions = list(session.pages.order_by("position").values_list("position", flat=True))
    completed = session.state == ScanSession.State.COMPLETED
    document = session.document
    return ScanSessionOut(
        id=session.uuid,
        status=session.state,
        pages=positions,
        page_count=len(positions),
        size=sessions.total_size(session),
        expires_at=None if completed else session.expires_at,
        document_id=document.uuid if document else None,
        duplicate=session.duplicate,
        status_url=f"/api/upload/v1/documents/{document.uuid}" if document else None,
    )


def _intake_error(request: HttpRequest, scanner: ScannerClient, exc: IntakeError) -> HttpError:
    if exc.status in (400, 413, 415):
        audit("upload.rejected", request=request, scanner=scanner, reason=str(exc))
    return HttpError(exc.status, str(exc))


@upload_router.post("/scans", response={200: ScanSessionOut, 201: ScanSessionOut})
def create_scan(
    request: HttpRequest,
    bucket: Form[str] = "",
    document_type: Form[str] = "auto",
    important: Form[str] = "false",
    todo: Form[str] = "false",
    tags: Form[str] = "",
    metadata: Form[str] = "",
    dpi: Form[str] = "",
    skip_blank_pages: Form[str] = "false",
    compression: Form[str] = "auto",
) -> Status:
    """Open a scan session to upload a document page by page.

    Takes the same fields as `POST /documents` (without `file`). Send an
    `Idempotency-Key` header to make retries safe.
    """
    scanner = require_scope(request, ScannerClient.Scope.UPLOAD)
    req = _intake_request(bucket, document_type, important, todo, tags, metadata)
    options = _assembly_options(dpi, skip_blank_pages, compression)
    try:
        session, created = sessions.create(scanner, req, options, idempotency_key=_idempotency_key(request))
    except IntakeError as exc:
        raise _intake_error(request, scanner, exc) from exc
    if created:
        audit("scan.opened", request=request, scanner=scanner, target=str(session.uuid))
    return Status(201 if created else 200, _session_out(session))


@upload_router.get("/scans/{scan_id}", response=ScanSessionOut)
def get_scan(request: HttpRequest, scan_id: UUID) -> ScanSessionOut:
    scanner = require_scope(request, ScannerClient.Scope.UPLOAD)
    try:
        return _session_out(sessions.get(scanner, scan_id))
    except IntakeError as exc:
        raise HttpError(exc.status, str(exc)) from exc


@upload_router.post("/scans/{scan_id}/pages", response={201: ScanPageOut})
def add_scan_page(
    request: HttpRequest,
    scan_id: UUID,
    file: File[UploadedFile],
    page: Form[int | None] = None,
) -> Status:
    """Upload one page file (image, multi-page TIFF or PDF).

    `page` is the 1-based position; omit it to append. Re-sending a position
    replaces that page, so retries are safe.
    """
    scanner = require_scope(request, ScannerClient.Scope.UPLOAD)
    try:
        stored = sessions.add_page(scanner, scan_id, _upload_path(file), page)
    except IntakeError as exc:
        raise _intake_error(request, scanner, exc) from exc
    finally:
        file.close()
    count = ScanPage.objects.filter(session_id=stored.session_id).count()
    return Status(
        201, ScanPageOut(page=stored.position, kind=stored.kind, size=stored.size, page_count=count)
    )


@upload_router.post("/scans/{scan_id}/complete", response={200: UploadAccepted, 202: UploadAccepted})
def complete_scan(request: HttpRequest, scan_id: UUID, expected_pages: Form[int | None] = None) -> Status:
    """Finish the scan: DocNest assembles the pages into one PDF and processes it.

    `expected_pages` (optional) makes completion fail with 409 unless exactly
    that many pages were received. Calling this again returns the same document.
    """
    scanner = require_scope(request, ScannerClient.Scope.UPLOAD)
    try:
        already = ScanSession.objects.filter(
            uuid=scan_id, scanner=scanner, state=ScanSession.State.COMPLETED
        ).exists()
        _, result = sessions.complete(scanner, scan_id, expected_pages=expected_pages)
    except IntakeError as exc:
        raise _intake_error(request, scanner, exc) from exc
    if not already:
        audit(
            "upload.accepted" if result.created else "upload.duplicate",
            request=request,
            scanner=scanner,
            target=str(result.document.uuid),
        )
    if result.created:
        return Status(202, _accepted(result.document, duplicate=False))
    return Status(200, _accepted(result.document, duplicate=True))


@upload_router.delete("/scans/{scan_id}", response={204: None})
def delete_scan(request: HttpRequest, scan_id: UUID) -> Status:
    """Abandon an open scan session and delete its pages."""
    scanner = require_scope(request, ScannerClient.Scope.UPLOAD)
    try:
        sessions.delete(scanner, scan_id)
    except IntakeError as exc:
        raise HttpError(exc.status, str(exc)) from exc
    return Status(204, None)


# --- Management (web users) ---------------------------------------------------------


class ScannerOut(Schema):
    id: int
    name: str
    token_prefix: str
    scopes: list[str]
    allowed_ips: list[str]
    created_at: datetime
    last_used_at: datetime | None
    last_used_ip: str | None
    expires_at: datetime | None
    revoked_at: datetime | None
    active: bool
    document_count: int


class ScannerIn(Schema):
    name: str
    allow_status: bool = True
    allowed_ips: list[str] = []
    expires_at: datetime | None = None


class ScannerCreated(Schema):
    scanner: ScannerOut
    token: str


def _out(c: ScannerClient) -> ScannerOut:
    return ScannerOut(
        id=c.pk,
        name=c.name,
        token_prefix=f"{tokens.TOKEN_PREFIX}{c.token_prefix}_…",
        scopes=c.scopes,
        allowed_ips=c.allowed_ips,
        created_at=c.created_at,
        last_used_at=c.last_used_at,
        last_used_ip=c.last_used_ip,
        expires_at=c.expires_at,
        revoked_at=c.revoked_at,
        active=c.is_active,
        document_count=c.documents.count(),
    )


def _validate_ips(values: list[str]) -> list[str]:
    import ipaddress

    out = []
    for v in values:
        v = v.strip()
        if not v:
            continue
        try:
            ipaddress.ip_network(v, strict=False)
        except ValueError as exc:
            raise HttpError(400, f"Invalid IP or network: {v[:50]}") from exc
        out.append(v)
    return out


@manage_router.get("", response=list[ScannerOut])
def list_scanners(request: HttpRequest) -> list[ScannerOut]:
    return [_out(c) for c in ScannerClient.objects.order_by("-created_at")]


@manage_router.post("", response=ScannerCreated)
def create_scanner(request: HttpRequest, data: ScannerIn) -> ScannerCreated:
    require_recent_auth(request)
    name = data.name.strip()[:100]
    if not name:
        raise HttpError(400, "Name is required")
    if ScannerClient.objects.filter(name__iexact=name).exists():
        raise HttpError(400, "A scanner with this name already exists")
    token, prefix, token_hash = tokens.generate()
    scopes = [ScannerClient.Scope.UPLOAD] + ([ScannerClient.Scope.UPLOAD_STATUS] if data.allow_status else [])
    client = ScannerClient.objects.create(
        name=name,
        token_prefix=prefix,
        token_hash=token_hash,
        scopes=scopes,
        allowed_ips=_validate_ips(data.allowed_ips),
        expires_at=data.expires_at,
    )
    audit("scanner.created", request=request, target=name)
    return ScannerCreated(scanner=_out(client), token=token)


@manage_router.post("/{scanner_id}/rotate", response=ScannerCreated)
def rotate_token(request: HttpRequest, scanner_id: int) -> ScannerCreated:
    require_recent_auth(request)
    client = ScannerClient.objects.filter(pk=scanner_id).first()
    if client is None:
        raise HttpError(404, "Not found")
    token, prefix, token_hash = tokens.generate()
    client.token_prefix, client.token_hash, client.revoked_at = prefix, token_hash, None
    client.save(update_fields=["token_prefix", "token_hash", "revoked_at"])
    audit("scanner.token_rotated", request=request, target=client.name)
    return ScannerCreated(scanner=_out(client), token=token)


@manage_router.post("/{scanner_id}/revoke", response=ScannerOut)
def revoke_scanner(request: HttpRequest, scanner_id: int) -> ScannerOut:
    client = ScannerClient.objects.filter(pk=scanner_id).first()
    if client is None:
        raise HttpError(404, "Not found")
    client.revoked_at = timezone.now()
    client.save(update_fields=["revoked_at"])
    audit("scanner.revoked", request=request, target=client.name)
    return _out(client)


@manage_router.delete("/{scanner_id}")
def delete_scanner(request: HttpRequest, scanner_id: int) -> dict[str, bool]:
    require_recent_auth(request)
    client = ScannerClient.objects.filter(pk=scanner_id).first()
    if client is None:
        raise HttpError(404, "Not found")
    name = client.name
    client.delete()
    audit("scanner.deleted", request=request, target=name)
    return {"ok": True}
