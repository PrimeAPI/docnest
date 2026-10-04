"""Recurring document detection (UC06).

Documents from the same correspondent with the same type and a strongly
overlapping vocabulary ("template words" like Gehaltsabrechnung,
Personalnummer) form a series. Similarity is measured on blind-index stem
hashes, weighted by rarity, so no plaintext is needed.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from django.db.models import Count

from apps.documents.models import Document
from apps.search.index import idf_weighted_overlap
from apps.search.models import DocumentStats, SearchTerm
from apps.taxonomy.models import Series

AUTO_ASSIGN = 0.40
SUGGEST = 0.28
NEW_SERIES = 0.45
MEMBERS_TO_COMPARE = 6

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]  # fmt: skip


@dataclass
class SeriesDecision:
    series: Series | None = None
    suggestion: Series | None = None
    created_series: bool = False
    partner_ids: tuple[int, ...] = ()
    score: float = 0.0


def _doc_freq(hashes: set[int]) -> tuple[dict[int, int], int]:
    if not hashes:
        return {}, 0
    rows = (
        SearchTerm.objects.filter(term__in=list(hashes), field=SearchTerm.Field.CONTENT)
        .values("term")
        .annotate(n=Count("document_id", distinct=True))
    )
    return {r["term"]: r["n"] for r in rows}, DocumentStats.objects.count()


def _signatures(doc_ids: list[int]) -> dict[int, set[int]]:
    return {
        d: set(sig)
        for d, sig in DocumentStats.objects.filter(document_id__in=doc_ids).values_list(
            "document_id", "signature"
        )
    }


def decide(document: Document, signature: set[int]) -> SeriesDecision:
    if document.correspondent_id is None or not signature:
        return SeriesDecision()

    peers = Document.objects.filter(
        correspondent_id=document.correspondent_id, deleted_at__isnull=True
    ).exclude(pk=document.pk)
    if document.document_type_id:
        peers = peers.filter(document_type_id=document.document_type_id)
    peer_ids = list(peers.order_by("-uploaded_at").values_list("id", flat=True)[:200])
    if not peer_ids:
        return SeriesDecision()

    sigs = _signatures(peer_ids)
    all_hashes = set(signature)
    for s in sigs.values():
        all_hashes |= s
    doc_freq, n_docs = _doc_freq(all_hashes)
    n_docs = max(n_docs, len(sigs) + 1)

    def sim(other_id: int) -> float:
        return idf_weighted_overlap(signature, sigs.get(other_id, set()), doc_freq, n_docs)

    # 1) Existing series of this correspondent
    best: tuple[float, Series | None] = (0.0, None)
    series_members: dict[int, list[int]] = {}
    for doc_id, series_id in peers.filter(series__isnull=False).values_list("id", "series_id"):
        series_members.setdefault(series_id, []).append(doc_id)
    for series_id, members in series_members.items():
        scores = sorted((sim(m) for m in members[:MEMBERS_TO_COMPARE]), reverse=True)
        score = statistics.mean(scores[:3]) if scores else 0.0
        if score > best[0]:
            best = (score, Series.objects.get(pk=series_id))
    if best[1] is not None and best[0] >= AUTO_ASSIGN:
        return SeriesDecision(series=best[1], score=best[0])
    if best[1] is not None and best[0] >= SUGGEST:
        return SeriesDecision(suggestion=best[1], score=best[0])

    # 2) Unassigned similar documents -> propose a new series
    unassigned = [d for d in peer_ids if d not in {m for ms in series_members.values() for m in ms}]
    partners = [(sim(d), d) for d in unassigned]
    partners = [(s, d) for s, d in partners if s >= NEW_SERIES]
    if partners:
        partners.sort(reverse=True)
        return SeriesDecision(
            created_series=True, partner_ids=tuple(d for _, d in partners), score=partners[0][0]
        )
    return SeriesDecision()


def default_series_name(document: Document) -> str:
    """E.g. "Muster Software GmbH – Statements"."""
    parts = []
    if document.correspondent is not None:
        parts.append(document.correspondent.name)
    if document.document_type is not None and document.document_type.slug not in {"other", "mail"}:
        parts.append(_plural(document.document_type.name))
    return " – ".join(parts) or "Recurring documents"


def _plural(name: str) -> str:
    if name.endswith("s"):
        return name
    if name.endswith("y"):
        return name[:-1] + "ies"
    return name + "s"


def detect_cadence(dates: list[date]) -> str:
    dates = sorted(set(dates))
    if len(dates) < 2:
        return Series.Cadence.IRREGULAR
    gaps = [(b - a).days for a, b in pairwise(dates)]
    median = statistics.median(gaps)
    if 25 <= median <= 35:
        return Series.Cadence.MONTHLY
    if 80 <= median <= 100:
        return Series.Cadence.QUARTERLY
    if 330 <= median <= 400:
        return Series.Cadence.YEARLY
    return Series.Cadence.IRREGULAR


def period_label(d: date | None, cadence: str) -> str:
    if d is None:
        return ""
    if cadence == Series.Cadence.YEARLY:
        return str(d.year)
    if cadence == Series.Cadence.QUARTERLY:
        return f"Q{(d.month - 1) // 3 + 1} {d.year}"
    if cadence == Series.Cadence.MONTHLY:
        return f"{MONTH_NAMES[d.month - 1]} {d.year}"
    return d.isoformat()


def refresh_series(series: Series) -> None:
    """Recompute cadence and period labels of all members."""
    members = list(series.documents.filter(deleted_at__isnull=True))
    cadence = detect_cadence([m.document_date for m in members if m.document_date])
    if cadence != series.cadence:
        series.cadence = cadence
        series.save(update_fields=["cadence"])
    for m in members:
        label = period_label(m.document_date, cadence)
        if m.period_label != label:
            Document.objects.filter(pk=m.pk).update(period_label=label)
