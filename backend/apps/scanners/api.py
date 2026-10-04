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
from apps.documents.intake import IntakeError, IntakeRequest, receive
from apps.documents.models import Document
from apps.scanners import tokens
from apps.scanners.auth import require_scope
from apps.scanners.models import IdempotencyKey, ScannerClient

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


@upload_router.post("/documents", response={200: UploadAccepted, 202: UploadAccepted})
def upload_document(
    request: HttpRequest,
    file: File[UploadedFile],
    bucket: Form[str],
    document_type: Form[str] = "auto",
    important: Form[str] = "false",
    todo: Form[str] = "false",
    tags: Form[str] = "",
    metadata: Form[str] = "",
) -> Status:
    """Upload a scanned PDF.

    `tags` is a comma-separated list; `metadata` an optional JSON object.
    Send an `Idempotency-Key` header to make retries safe.
    """
    scanner = require_scope(request, ScannerClient.Scope.UPLOAD)
    idem = request.headers.get("Idempotency-Key", "").strip()[:200]
    if idem:
        existing = IdempotencyKey.objects.filter(scanner=scanner, key_hash=keyed_hash(idem.encode())).first()
        if existing:
            return Status(200, _accepted(existing.document, duplicate=True))

    try:
        meta: dict[str, Any] = json.loads(metadata) if metadata else {}
    except json.JSONDecodeError as exc:
        raise HttpError(400, "metadata must be a JSON object") from exc
    if not isinstance(meta, dict):
        raise HttpError(400, "metadata must be a JSON object")

    path = _upload_path(file)
    req = IntakeRequest(
        bucket=bucket,
        document_type=document_type,
        important=_parse_bool(important),
        todo=_parse_bool(todo),
        tags=[t for t in tags.split(",") if t.strip()],
        metadata=meta,
        filename=Path(file.name or "").name,
    )
    try:
        result = receive(path, req, scanner=scanner)
    except IntakeError as exc:
        audit("upload.rejected", request=request, scanner=scanner, reason=str(exc))
        raise HttpError(exc.status, str(exc)) from exc
    finally:
        file.close()

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
