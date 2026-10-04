"""Blind index: keyed hashes of terms, BM25-style ranking in PostgreSQL.

Only HMAC-SHA256(index_key, kind || term) truncated to 64 bits is stored. A
database dump therefore contains no readable document text; matching works by
hashing the query with the same key.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from django.db import connection, transaction
from django.db.models import QuerySet

from apps.crypto.keys import Purpose, derive
from apps.search import tokenizer
from apps.search.models import DocumentStats, SearchTerm
from apps.search.tokenizer import Term

FIELD_WEIGHTS = {SearchTerm.Field.TITLE: 2.0, SearchTerm.Field.META: 1.5, SearchTerm.Field.CONTENT: 1.0}
BM25_K1 = 1.2
BM25_B = 0.6
SIGNATURE_SIZE = 300


def term_hash(term: Term) -> int:
    digest = hmac.new(derive(Purpose.INDEX), f"{term.kind}\x00{term.value}".encode(), hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@dataclass
class IndexInput:
    title: str = ""
    content: str = ""
    meta: Iterable[str] = ()


def index_document(document_id: int, data: IndexInput) -> None:
    """(Re)build the index rows for one document. Idempotent."""
    rows: list[SearchTerm] = []
    total_words = 0
    stem_counts: Counter[int] = Counter()
    for field, text in (
        (SearchTerm.Field.TITLE, data.title),
        (SearchTerm.Field.CONTENT, data.content),
        (SearchTerm.Field.META, " ".join(data.meta)),
    ):
        if not text:
            continue
        counts, n_words = tokenizer.document_terms(text)
        if field != SearchTerm.Field.META:
            total_words += n_words
        for term, tf in counts.items():
            h = term_hash(term)
            rows.append(SearchTerm(document_id=document_id, term=h, field=field, tf=min(tf, 32767)))
            if term.kind == "S" and field == SearchTerm.Field.CONTENT:
                stem_counts[h] += tf
    signature = [h for h, _ in stem_counts.most_common(SIGNATURE_SIZE)]
    with transaction.atomic():
        SearchTerm.objects.filter(document_id=document_id).delete()
        SearchTerm.objects.bulk_create(rows, batch_size=5000)
        DocumentStats.objects.update_or_create(
            document_id=document_id, defaults={"length": max(total_words, 1), "signature": signature}
        )


def compute_signature(content: str) -> list[int]:
    """Top stem hashes of a text — the same signature `index_document` stores."""
    counts, _ = tokenizer.document_terms(content)
    stem_counts: Counter[int] = Counter()
    for term, tf in counts.items():
        if term.kind == "S":
            stem_counts[term_hash(term)] += tf
    return [h for h, _ in stem_counts.most_common(SIGNATURE_SIZE)]


def remove_document(document_id: int) -> None:
    SearchTerm.objects.filter(document_id=document_id).delete()
    DocumentStats.objects.filter(document_id=document_id).delete()


@dataclass
class SearchHits:
    ids: list[int]
    scores: dict[int, float]
    total: int


def search(query: str, candidates: QuerySet, *, limit: int, offset: int) -> SearchHits:
    """Rank `candidates` (a Document queryset with filters applied) by relevance to `query`.

    All query words must match (AND); each word may match exactly, by stem,
    by prefix or as the tail of a compound word.
    """
    qwords = tokenizer.query_words(query)
    if not qwords:
        return SearchHits([], {}, 0)

    values: list[tuple[int, int, float]] = []
    for qi, word in enumerate(qwords):
        for term, weight in tokenizer.query_terms(word).items():
            values.append((term_hash(term), qi, weight))

    cand_sql, cand_params = candidates.values("id").query.sql_with_params()
    values_sql = ", ".join(["(%s::bigint, %s::int, %s::float8)"] * len(values))
    field_case = " ".join(f"WHEN {int(f)} THEN {w}" for f, w in FIELD_WEIGHTS.items())
    sql = f"""
        WITH q(term, qi, w) AS (VALUES {values_sql}),
        stats AS (
            SELECT COUNT(*)::float8 AS n, COALESCE(AVG(length), 1)::float8 AS avglen FROM search_documentstats
        ),
        m AS (
            SELECT st.document_id, q.qi,
                   MAX(q.w * (CASE st.field {field_case} ELSE 1 END)
                       * (st.tf * ({BM25_K1} + 1))
                       / (st.tf + {BM25_K1} * (1 - {BM25_B} + {BM25_B} * ds.length / stats.avglen))) AS s
            FROM search_searchterm st
            JOIN q ON st.term = q.term
            JOIN search_documentstats ds ON ds.document_id = st.document_id
            CROSS JOIN stats
            WHERE st.document_id IN ({cand_sql})
            GROUP BY st.document_id, q.qi
        ),
        df AS (SELECT qi, COUNT(*)::float8 AS df FROM m GROUP BY qi),
        scored AS (
            SELECT m.document_id,
                   SUM(m.s * LN(1 + (stats.n - df.df + 0.5) / (df.df + 0.5))) AS score
            FROM m JOIN df ON df.qi = m.qi CROSS JOIN stats
            GROUP BY m.document_id
            HAVING COUNT(*) = %s
        )
        SELECT document_id, score, COUNT(*) OVER () AS total
        FROM scored
        ORDER BY score DESC, document_id DESC
        LIMIT %s OFFSET %s
    """
    params: list = []  # type: ignore[type-arg]
    for v in values:
        params.extend(v)
    params.extend(cand_params)
    params.extend([len(qwords), limit, offset])
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    if not rows and offset > 0:
        # Page beyond the end: still report the total.
        total = _count(sql, params, limit, offset)
        return SearchHits([], {}, total)
    total = rows[0][2] if rows else 0
    return SearchHits([r[0] for r in rows], {r[0]: float(r[1]) for r in rows}, int(total))


def _count(sql: str, params: list, limit: int, offset: int) -> int:  # type: ignore[type-arg]
    count_params = [*params[:-2], 1, 0]
    with connection.cursor() as cursor:
        cursor.execute(sql, count_params)
        row = cursor.fetchone()
    return int(row[2]) if row else 0


# --- Similarity (used for series detection) ----------------------------------


def signature_of(document_id: int) -> set[int]:
    stats = DocumentStats.objects.filter(document_id=document_id).only("signature").first()
    return set(stats.signature) if stats else set()


def jaccard(a: set[int], b: set[int]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def idf_weighted_overlap(a: set[int], b: set[int], doc_freq: dict[int, int], n_docs: int) -> float:
    """Similarity emphasising rare shared terms (template words of a series)."""
    if not a or not b:
        return 0.0

    def w(h: int) -> float:
        return math.log(1 + n_docs / (1 + doc_freq.get(h, 0)))

    inter = sum(w(h) for h in a & b)
    union = sum(w(h) for h in a | b)
    return inter / union if union else 0.0
