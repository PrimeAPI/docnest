"""Accessors for a document's encrypted fields.

Ciphertexts are bound to the document (AAD = field + document UUID), so an
encrypted value cannot be copied to another row or field undetected.
"""

from __future__ import annotations

import json

from apps.crypto.aead import decrypt_bytes, encrypt_bytes
from apps.documents.models import Document, DocumentContent, DocumentThumbnail


def _aad(document: Document, field: str) -> bytes:
    return f"{field}:{document.uuid}".encode()


def get_title(document: Document) -> str:
    if not document.title_enc:
        return ""
    return decrypt_bytes(bytes(document.title_enc), aad=_aad(document, "title")).decode()


def set_title(document: Document, value: str) -> None:
    document.title_enc = encrypt_bytes(value.strip()[:200].encode(), aad=_aad(document, "title"))


def get_extracted(document: Document) -> dict:
    if not document.extracted_enc:
        return {}
    return json.loads(decrypt_bytes(bytes(document.extracted_enc), aad=_aad(document, "extracted")))


def set_extracted(document: Document, data: dict) -> None:
    document.extracted_enc = encrypt_bytes(json.dumps(data).encode(), aad=_aad(document, "extracted"))


def get_original_filename(document: Document) -> str:
    if not document.original_filename_enc:
        return ""
    try:
        return decrypt_bytes(bytes(document.original_filename_enc)).decode()
    except Exception:
        return ""


def get_scanner_metadata(document: Document) -> dict:
    if not document.scanner_metadata_enc:
        return {}
    return json.loads(decrypt_bytes(bytes(document.scanner_metadata_enc)))


def get_content(document: Document) -> str:
    row = DocumentContent.objects.filter(document=document).first()
    if row is None:
        return ""
    return decrypt_bytes(bytes(row.text_enc), aad=_aad(document, "content")).decode()


def set_content(document: Document, text: str, language: str = "") -> None:
    DocumentContent.objects.update_or_create(
        document=document,
        defaults={
            "text_enc": encrypt_bytes(text.encode(), aad=_aad(document, "content")),
            "language": language,
            "length": len(text),
        },
    )


def get_thumbnail(document: Document) -> bytes | None:
    row = DocumentThumbnail.objects.filter(document=document).first()
    if row is None:
        return None
    return decrypt_bytes(bytes(row.image_enc), aad=_aad(document, "thumbnail"))


def set_thumbnail(document: Document, image: bytes) -> None:
    DocumentThumbnail.objects.update_or_create(
        document=document, defaults={"image_enc": encrypt_bytes(image, aad=_aad(document, "thumbnail"))}
    )
