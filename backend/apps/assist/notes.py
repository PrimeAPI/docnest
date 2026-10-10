"""What the AI model understood reading a document, kept for later runs.

Reading is the slow part of a review (one request per document). A note stays valid while
the document's text is the same, so the next review — or a nightly one over everything —
starts from what was understood before and spends its time on the connections instead.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from django.utils import timezone

from apps.assist.models import DocumentNote
from apps.crypto.aead import decrypt_bytes, encrypt_bytes
from apps.documents.models import Document

FIELDS = ("what", "sender", "recipient", "date", "period", "references", "complete", "why", "title")


def _aad(document: Document) -> bytes:
    return b"assist-note:" + str(document.uuid).encode()


def load(document: Document, text: str, model: str = "") -> dict[str, Any] | None:
    """The note for this version of the text; None when there is none or the text changed."""
    row = DocumentNote.objects.filter(document=document).first()
    if row is None or row.text_length != len(text) or (model and row.model != model):
        return None
    data = json.loads(decrypt_bytes(bytes(row.note_enc), aad=_aad(document)))
    if data.pop("text_hash", None) != hashlib.sha256(text.encode()).hexdigest():
        return None
    return data


def save(document: Document, note: dict[str, Any], text: str, model: str) -> None:
    data = {k: note.get(k) for k in FIELDS if note.get(k) not in (None, "", [])}
    data["text_hash"] = hashlib.sha256(text.encode()).hexdigest()
    DocumentNote.objects.update_or_create(
        document=document,
        defaults={
            "note_enc": encrypt_bytes(json.dumps(data, ensure_ascii=False).encode(), aad=_aad(document)),
            "text_length": len(text),
            "model": model,
            "created_at": timezone.now(),
        },
    )
