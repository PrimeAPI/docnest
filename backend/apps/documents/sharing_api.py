"""Authenticated share management and a deliberately small, read-only public API."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.http import FileResponse, HttpRequest, HttpResponseRedirect
from django.utils import timezone
from ninja import Field, Router, Schema, Status
from ninja.errors import HttpError

from apps.audit.models import AuditLog
from apps.audit.service import audit, client_ip
from apps.core.auth import require_csrf
from apps.crypto.aead import decrypt_text, encrypt_text
from apps.documents import crypto_fields, pages
from apps.documents.api import TextOut, get_document, serve_document_file
from apps.documents.models import DocumentShare

manage_router = Router(tags=["sharing"])
public_router = Router(tags=["shared documents"], auth=None)
GRANTS_KEY = "document_share_grants"
GRANT_SECONDS = 12 * 60 * 60


class ShareIn(Schema):
    passphrase: str = Field("", max_length=1024)
    allow_original: bool = False
    allow_text: bool = True


class ShareOut(Schema):
    id: UUID
    protected: bool
    allow_original: bool
    allow_text: bool
    created_at: datetime
    url: str
    pdf_url: str


class SharedDocumentOut(Schema):
    title: str
    document_date: date | None
    page_count: int
    allow_original: bool
    allow_text: bool


class UnlockIn(Schema):
    passphrase: str = Field(min_length=1, max_length=1024)


def token_hash(token: UUID) -> str:
    return hashlib.sha256(str(token).encode()).hexdigest()


def token_aad(document_id: UUID, digest: str) -> bytes:
    return f"document-share:{document_id}:{digest}".encode()


def share_out(share: DocumentShare) -> ShareOut:
    token = UUID(decrypt_text(bytes(share.token_enc), aad=token_aad(share.document.uuid, share.token_hash)))
    return ShareOut(
        id=token,
        protected=bool(share.password_hash),
        allow_original=share.allow_original,
        allow_text=share.allow_text,
        created_at=share.created_at,
        url=f"{settings.BASE_URL}/share/{token}",
        pdf_url=f"{settings.BASE_URL}/api/v1/shares/{token}/file",
    )


@manage_router.get("/{doc_id}/shares", response=list[ShareOut])
def list_shares(request: HttpRequest, doc_id: UUID) -> list[ShareOut]:
    return [share_out(s) for s in get_document(doc_id).shares.select_related("document")]


@manage_router.post("/{doc_id}/shares", response={201: ShareOut})
def create_share(request: HttpRequest, doc_id: UUID, data: ShareIn) -> Status[ShareOut]:
    document = get_document(doc_id)
    if data.passphrase and not data.passphrase.strip():
        raise HttpError(422, "Passphrase cannot contain only spaces")
    token = uuid4()
    digest = token_hash(token)
    share = DocumentShare.objects.create(
        token_hash=digest,
        token_enc=encrypt_text(str(token), aad=token_aad(document.uuid, digest)),
        document=document,
        password_hash=make_password(data.passphrase) if data.passphrase else "",
        allow_original=data.allow_original,
        allow_text=data.allow_text,
    )
    audit("document.share_created", request=request, target=str(document.uuid), share=share.pk)
    return Status(201, share_out(share))


@manage_router.delete("/{doc_id}/shares/{share_id}")
def revoke_share(request: HttpRequest, doc_id: UUID, share_id: UUID) -> dict[str, bool]:
    document = get_document(doc_id)
    share = document.shares.filter(token_hash=token_hash(share_id)).first()
    if share is None:
        raise HttpError(404, "Share not found")
    share_pk = share.pk
    share.delete()
    audit("document.share_revoked", request=request, target=str(document.uuid), share=share_pk)
    return {"ok": True}


def get_share(share_id: UUID) -> DocumentShare:
    share = (
        DocumentShare.objects.select_related("document")
        .filter(token_hash=token_hash(share_id), document__deleted_at__isnull=True)
        .first()
    )
    if share is None:
        raise HttpError(404, "This share is unavailable or has been revoked")
    return share


def is_unlocked(request: HttpRequest, share: DocumentShare) -> bool:
    if not share.password_hash:
        return True
    granted = request.session.get(GRANTS_KEY, {}).get(str(share.pk), 0)
    return timezone.now().timestamp() - granted < GRANT_SECONDS


def require_unlocked(request: HttpRequest, share: DocumentShare) -> None:
    if not is_unlocked(request, share):
        raise HttpError(403, "passphrase_required")


@public_router.post("/{share_id}/unlock")
def unlock_share(request: HttpRequest, share_id: UUID, data: UnlockIn) -> dict[str, bool]:
    require_csrf(request)
    share = get_share(share_id)
    if not share.password_hash:
        return {"ok": True}
    failures = AuditLog.objects.filter(
        action="document.share_unlock_failed",
        ip=client_ip(request),
        created_at__gte=timezone.now() - timedelta(minutes=15),
    )
    if failures.count() >= 20 or failures.filter(target=str(share.pk)).count() >= 5:
        raise HttpError(429, "Too many attempts. Try again in 15 minutes.")
    if not check_password(data.passphrase, share.password_hash):
        audit("document.share_unlock_failed", request=request, target=str(share.pk))
        raise HttpError(403, "Incorrect passphrase")
    now = timezone.now().timestamp()
    grants = {k: v for k, v in request.session.get(GRANTS_KEY, {}).items() if now - v < GRANT_SECONDS}
    # Bound session growth when one visitor opens many different shares.
    grants = dict(sorted(grants.items(), key=lambda item: item[1])[-49:])
    grants[str(share.pk)] = now
    request.session[GRANTS_KEY] = grants
    audit("document.share_unlocked", request=request, target=str(share.document.uuid), share=share.pk)
    return {"ok": True}


@public_router.get("/{share_id}", response=SharedDocumentOut)
def shared_document(request: HttpRequest, share_id: UUID) -> SharedDocumentOut:
    share = get_share(share_id)
    require_unlocked(request, share)
    document = share.document
    return SharedDocumentOut(
        title=crypto_fields.get_title(document) or "Document",
        document_date=document.document_date,
        page_count=len(pages.visible_numbers(document)),
        allow_original=share.allow_original,
        allow_text=share.allow_text,
    )


@public_router.get("/{share_id}/file")
def shared_file(
    request: HttpRequest,
    share_id: UUID,
    variant: Literal["archive", "original"] = "archive",
    download: bool = False,
) -> FileResponse | HttpResponseRedirect:
    share = get_share(share_id)
    if not is_unlocked(request, share):
        # Raw protected PDF links take a browser through the passphrase form first.
        return HttpResponseRedirect(
            f"/share/{share_id}?pdf={variant}&download={'true' if download else 'false'}"
        )
    if variant == "original" and not share.allow_original:
        raise HttpError(403, "The original was not shared")
    return serve_document_file(request, share.document, variant, download)


@public_router.get("/{share_id}/text", response=TextOut)
def shared_text(request: HttpRequest, share_id: UUID) -> dict[str, str]:
    share = get_share(share_id)
    require_unlocked(request, share)
    if not share.allow_text:
        raise HttpError(403, "OCR text was not shared")
    document = share.document
    content = getattr(document, "content", None)
    return {
        "text": crypto_fields.get_content(document),
        "format": content.format if content else "text",
        "backend": document.ocr_backend,
    }
