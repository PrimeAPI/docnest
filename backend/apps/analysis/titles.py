"""Title generation without any language model.

Order of preference:
1. Pattern learned from a user-written title in the same series
   (the period in that title is replaced with the new document's period).
2. "<Subject line>" if a meaningful subject was found.
3. "<Type> <Correspondent> <period/date>".
"""

from __future__ import annotations

import re

from apps.analysis.series import MONTH_NAMES
from apps.documents.crypto_fields import get_title
from apps.documents.models import Document, Source

_GERMAN_MONTHS = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]  # fmt: skip


def _period_regex() -> re.Pattern[str]:
    months = "|".join(MONTH_NAMES + _GERMAN_MONTHS)
    return re.compile(
        rf"((?:{months})\s+\d{{4}}|Q[1-4]\s+\d{{4}}|\d{{4}}-\d{{2}}(?:-\d{{2}})?|\b(?:19|20)\d{{2}}\b)"
    )


def from_series_pattern(document: Document) -> str | None:
    if not document.series_id or not document.period_label:
        return None
    sibling = (
        Document.objects.filter(series_id=document.series_id, deleted_at__isnull=True)
        .exclude(pk=document.pk)
        .order_by("-uploaded_at")
    )
    for other in sibling[:10]:
        if other.source_of("title") != Source.USER or not other.title_enc:
            continue
        title = get_title(other)
        if other.period_label and other.period_label in title:
            return title.replace(other.period_label, document.period_label)
        if _period_regex().search(title):
            return _period_regex().sub(document.period_label, title, count=1)
    return None


def generate(document: Document, subject: str | None, *, name_sender: bool = True) -> str:
    """`name_sender`: append the correspondent to a subject (rule-based subjects are often bare)."""
    learned = from_series_pattern(document)
    if learned:
        return learned[:200]
    period = document.period_label or (document.document_date.isoformat() if document.document_date else "")
    if subject and len(subject) >= 6:
        title = subject
        if (
            name_sender
            and document.correspondent
            and document.correspondent.name.lower() not in subject.lower()
        ):
            title = f"{subject} – {document.correspondent.name}"
        return title[:200]
    parts = []
    if document.document_type and document.document_type.slug != "other":
        parts.append(document.document_type.name)
    if document.correspondent:
        parts.append(document.correspondent.name)
    if period:
        parts.append(period)
    return (" ".join(parts) or "Untitled document")[:200]


def without_sender(title: str, *senders: str | None) -> str:
    """Drop the sender from a title the AI model wrote: it has a field of its own."""
    cleaned = title
    for sender in senders:
        if sender and len(sender) >= 3:
            cleaned = re.sub(re.escape(sender), "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -–—:,|/")
    cleaned = re.sub(r"^(?:von|vom|der|des|from)\s+|\s+(?:von|vom|der|des|from)$", "", cleaned, flags=re.I)
    return cleaned if len(cleaned) >= 6 else title
