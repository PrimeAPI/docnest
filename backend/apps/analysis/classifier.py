"""Naive Bayes classifiers trained on the user's own documents.

Features are the document's blind-index stem hashes (its "signature"), so a
trained model contains only keyed hashes and counts — never plaintext words.
Models are retrained in the background whenever classifications change.
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.utils import timezone

from apps.analysis.models import ClassifierModel

logger = logging.getLogger(__name__)

ALPHA = 0.5
MIN_CLASS_SAMPLES = 2
MIN_TOTAL_SAMPLES = 4

SINGLE_TARGETS = ("document_type", "correspondent", "bucket")
TAG_TARGET = "tags"


@dataclass
class Prediction:
    label: str
    probability: float


class NaiveBayes:
    """Multinomial NB over binary features with Laplace smoothing."""

    def __init__(self, data: dict | None = None) -> None:
        data = data or {}
        self.classes: dict[str, dict] = data.get("classes", {})
        self.vocab_size: int = data.get("vocab_size", 0)
        self.n_docs: int = data.get("n_docs", 0)

    def fit(self, samples: Iterable[tuple[Iterable[int], str]]) -> NaiveBayes:
        counts: dict[str, Counter[str]] = defaultdict(Counter)
        docs: Counter[str] = Counter()
        vocab: set[str] = set()
        for features, label in samples:
            feats = {str(f) for f in features}
            counts[label].update(feats)
            docs[label] += 1
            vocab |= feats
        self.classes = {
            label: {"docs": docs[label], "total": sum(c.values()), "counts": dict(c)}
            for label, c in counts.items()
            if docs[label] >= MIN_CLASS_SAMPLES
        }
        self.vocab_size = len(vocab)
        self.n_docs = sum(docs.values())
        return self

    def to_json(self) -> dict:
        return {"classes": self.classes, "vocab_size": self.vocab_size, "n_docs": self.n_docs}

    def predict(self, features: Iterable[int]) -> list[Prediction]:
        if len(self.classes) < 2 or self.n_docs < MIN_TOTAL_SAMPLES:
            return []
        feats = [str(f) for f in features]
        total_docs = sum(c["docs"] for c in self.classes.values())
        logp: dict[str, float] = {}
        for label, c in self.classes.items():
            denom = c["total"] + ALPHA * (self.vocab_size + 1)
            counts = c["counts"]
            score = math.log(c["docs"] / total_docs)
            for f in feats:
                score += math.log((counts.get(f, 0) + ALPHA) / denom)
            logp[label] = score
        peak = max(logp.values())
        exp = {k: math.exp(v - peak) for k, v in logp.items()}
        norm = sum(exp.values())
        ranked = sorted(((k, v / norm) for k, v in exp.items()), key=lambda kv: kv[1], reverse=True)
        return [Prediction(k, p) for k, p in ranked]


def _training_rows() -> list[tuple[list[int], dict[str, Any]]]:
    from apps.documents.models import Document
    from apps.search.models import DocumentStats

    sigs = dict(DocumentStats.objects.values_list("document_id", "signature"))
    rows: list[tuple[list[int], dict[str, Any]]] = []
    docs = (
        Document.objects.filter(deleted_at__isnull=True, processing_state=Document.State.DONE)
        .prefetch_related("documenttag_set")
        .only("id", "document_type_id", "correspondent_id", "bucket_id", "field_sources")
    )
    for doc in docs:
        sig = sigs.get(doc.pk)
        if not sig:
            continue
        rows.append(
            (
                sig,
                {
                    "document_type": doc.document_type_id,
                    "correspondent": doc.correspondent_id,
                    "bucket": doc.bucket_id,
                    "tags": [dt.tag_id for dt in doc.documenttag_set.all()],
                },
            )
        )
    return rows


def train_all() -> dict[str, int]:
    rows = _training_rows()
    result = {}
    for target in SINGLE_TARGETS:
        samples = [(sig, str(labels[target])) for sig, labels in rows if labels[target] is not None]
        model = NaiveBayes().fit(samples)
        _save(target, model.to_json(), len(samples))
        result[target] = len(samples)

    # Tags: one binary model per tag ("1" = has tag, "0" = has not)
    tag_counts: Counter[int] = Counter(t for _, labels in rows for t in labels["tags"])
    tag_models: dict[str, dict] = {}
    for tag_id, n in tag_counts.items():
        if n < MIN_CLASS_SAMPLES:
            continue
        samples = [(sig, "1" if tag_id in labels["tags"] else "0") for sig, labels in rows]
        tag_models[str(tag_id)] = NaiveBayes().fit(samples).to_json()
    _save(TAG_TARGET, {"tags": tag_models}, len(rows))
    result[TAG_TARGET] = len(tag_models)
    logger.info("classifiers trained", extra={k: v for k, v in result.items()})
    return result


def _save(target: str, data: dict, n: int) -> None:
    ClassifierModel.objects.update_or_create(
        target=target, defaults={"model": data, "sample_count": n, "trained_at": timezone.now()}
    )


def predict(target: str, features: Iterable[int], *, min_probability: float = 0.6) -> Prediction | None:
    row = ClassifierModel.objects.filter(target=target).first()
    if row is None:
        return None
    ranked = NaiveBayes(row.model).predict(features)
    if ranked and ranked[0].probability >= min_probability:
        return ranked[0]
    return None


def predict_tags(
    features: Iterable[int], *, min_probability: float = 0.75, limit: int = 5
) -> list[Prediction]:
    row = ClassifierModel.objects.filter(target=TAG_TARGET).first()
    if row is None:
        return []
    feats = list(features)
    out = []
    for tag_id, data in row.model.get("tags", {}).items():
        ranked = NaiveBayes(data).predict(feats)
        p = next((r.probability for r in ranked if r.label == "1"), 0.0)
        if p >= min_probability:
            out.append(Prediction(tag_id, p))
    out.sort(key=lambda p: p.probability, reverse=True)
    return out[:limit]
