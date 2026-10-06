"""Automatic document analysis (UC05–UC07).

Combines, in order of strength: user match rules, known correspondents found
in the text, the classifiers trained on the user's own corrections, and the
built-in keyword knowledge. Fields the user (or the scanner) set explicitly are
never overwritten — automatic results are always only a proposal.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from django.db import transaction
from rapidfuzz import fuzz

from apps.analysis import classifier, extraction, series, titles
from apps.analysis.docling_fields import DetectedFields
from apps.analysis.keywords import MIN_KEYWORD_HITS, TAG_KEYWORDS, TYPE_KEYWORDS
from apps.documents import crypto_fields
from apps.documents.models import Document, DocumentTag, Source
from apps.processing.models import SystemState
from apps.search import tokenizer
from apps.search.index import compute_signature
from apps.taxonomy.models import Correspondent, DocumentType, MatchRule, Series, Tag, TagAlias
from apps.taxonomy.services import find_correspondent, find_tag, fold

logger = logging.getLogger(__name__)

DISMISSED_TAGS_KEY = "dismissed_suggested_tags"
HEADER_LINES = 25


@dataclass
class AnalysisResult:
    extracted: extraction.Extracted
    signature: list[int]
    tags_added: list[str] = field(default_factory=list)


def _folded_text(text: str) -> str:
    return " ".join(tokenizer.fold(w) for w in tokenizer.words(text))


def _word_counts(text: str) -> Counter[str]:
    return Counter(tokenizer.fold(w) for w in tokenizer.words(text))


def _keyword_hits(counts: Counter[str], keywords: tuple[str, ...]) -> int:
    hits = 0
    for word, n in counts.items():
        if any(word.startswith(k) for k in keywords):
            hits += n
    return hits


def _can_set(document: Document, name: str) -> bool:
    return document.source_of(name) == Source.AUTO


# --- Correspondent -------------------------------------------------------------


def _match_known_correspondent(text: str) -> Correspondent | None:
    header = _folded_text("\n".join(text.splitlines()[:HEADER_LINES]))
    body = _folded_text(text)
    best: tuple[int, Correspondent] | None = None
    for corr in Correspondent.objects.all():
        for name in [corr.name, *corr.aliases]:
            needle = " ".join(tokenizer.fold(w) for w in tokenizer.words(name))
            if len(needle) < 3:
                continue
            pattern = re.compile(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])")
            score = 0
            if pattern.search(header):
                score = 3
            elif pattern.search(body):
                score = 1
            if score and (
                best is None or score > best[0] or (score == best[0] and len(name) > len(best[1].name))
            ):
                best = (score, corr)
    return best[1] if best else None


def _correspondent_from_sender(sender: str) -> Correspondent:
    existing = find_correspondent(sender)
    if existing:
        return existing
    folded = fold(sender)
    for corr in Correspondent.objects.all():
        if fuzz.ratio(folded, fold(corr.name)) >= 88:
            return corr
    return Correspondent.objects.create(name=sender[:150])


# --- Main ---------------------------------------------------------------------


def analyze(
    document: Document,
    text: str,
    *,
    detected_fields: DetectedFields | None = None,
    context_text: str | None = None,
) -> AnalysisResult:
    extracted = extraction.extract(text)
    if detected_fields:
        if detected_fields.sender and detected_fields.sender_confidence >= 0.72:
            extracted.sender = detected_fields.sender
        if detected_fields.title:
            extracted.subject = detected_fields.title
    analysis_text = context_text or text
    signature = compute_signature(analysis_text)
    counts = _word_counts(analysis_text)
    folded = _folded_text(analysis_text)
    result = AnalysisResult(extracted=extracted, signature=signature)

    with transaction.atomic():
        document = Document.objects.select_for_update().get(pk=document.pk)

        # Rules defined by the user win over everything automatic.
        rule_tags: list[Tag] = []
        for rule in MatchRule.objects.prefetch_related("tags"):
            phrase = " ".join(tokenizer.fold(w) for w in tokenizer.words(rule.phrase))
            if phrase and phrase in folded:
                if rule.correspondent_id and _can_set(document, "correspondent"):
                    document.correspondent_id = rule.correspondent_id
                if rule.document_type_id and _can_set(document, "document_type"):
                    document.document_type_id = rule.document_type_id
                rule_tags.extend(rule.tags.all())

        # Document date
        if _can_set(document, "document_date") and extracted.document_date:
            document.document_date = extracted.document_date

        # Correspondent: known name in text > classifier > extracted sender
        if _can_set(document, "correspondent") and document.correspondent_id is None:
            corr = _match_known_correspondent(analysis_text)
            if corr is None:
                pred = classifier.predict("correspondent", signature, min_probability=0.8)
                if pred:
                    corr = Correspondent.objects.filter(pk=int(pred.label)).first()
            if corr is None and extracted.sender:
                corr = _correspondent_from_sender(extracted.sender)
            document.correspondent = corr

        # Document type: classifier > keywords > "other"
        if _can_set(document, "document_type") and document.document_type_id is None:
            doc_type = None
            pred = classifier.predict("document_type", signature, min_probability=0.7)
            if pred:
                doc_type = DocumentType.objects.filter(pk=int(pred.label)).first()
            if doc_type is None:
                header_counts = _word_counts("\n".join(analysis_text.splitlines()[:40]))
                scored = sorted(
                    (
                        (_keyword_hits(header_counts, kws) * 2 + _keyword_hits(counts, kws), slug)
                        for slug, kws in TYPE_KEYWORDS.items()
                    ),
                    reverse=True,
                )
                if scored and scored[0][0] >= 2:
                    doc_type = DocumentType.objects.filter(slug=scored[0][1]).first()
            if doc_type is None:
                doc_type = DocumentType.objects.filter(slug="other").first()
            document.document_type = doc_type

        document.save()

        # Tags (additive; never removes tags the user or scanner set)
        _apply_tags(document, signature, counts, rule_tags, result)

        # Series
        if _can_set(document, "series"):
            _apply_series(document, set(signature))

        # Title
        if _can_set(document, "title"):
            crypto_fields.set_title(document, titles.generate(document, extracted.subject))

        crypto_fields.set_extracted(document, extracted.to_json())
        document.save()

    SystemState.objects.update_or_create(key="classifier_dirty", defaults={"value": {"dirty": True}})
    return result


def _apply_tags(
    document: Document,
    signature: list[int],
    counts: Counter[str],
    rule_tags: list[Tag],
    result: AnalysisResult,
) -> None:
    existing = set(DocumentTag.objects.filter(document=document).values_list("tag_id", flat=True))
    removed_by_user = set(document.field_sources.get("tags_removed", []))
    candidates: dict[int, tuple[Tag, float]] = {}

    for tag in rule_tags:
        candidates[tag.pk] = (tag, 1.0)
    for pred in classifier.predict_tags(signature):
        predicted = Tag.objects.filter(pk=int(pred.label)).first()
        if predicted:
            candidates.setdefault(predicted.pk, (predicted, pred.probability))

    dismissed = set(
        (
            SystemState.objects.filter(key=DISMISSED_TAGS_KEY).values_list("value", flat=True).first() or {}
        ).get("names", [])
    )
    for name, (aliases, keywords) in TAG_KEYWORDS.items():
        if _keyword_hits(counts, keywords) < MIN_KEYWORD_HITS:
            continue
        found = find_tag(name) or next((t for a in aliases if (t := find_tag(a))), None)
        if found is None:
            if name in dismissed:
                continue
            found = Tag.objects.create(name=name, is_suggested=True)
            for alias in aliases:
                if (
                    not TagAlias.objects.filter(alias__iexact=alias).exists()
                    and not Tag.objects.filter(name__iexact=alias).exists()
                ):
                    TagAlias.objects.create(tag=found, alias=alias)
        candidates.setdefault(found.pk, (found, 0.6))

    for tag_id, (tag, confidence) in list(candidates.items())[:8]:
        if tag_id in existing or tag_id in removed_by_user:
            continue
        DocumentTag.objects.get_or_create(
            document=document, tag=tag, defaults={"source": Source.AUTO, "confidence": round(confidence, 3)}
        )
        result.tags_added.append(tag.name)


def _apply_series(document: Document, signature: set[int]) -> None:
    decision = series.decide(document, signature)
    if decision.series is not None:
        document.series = decision.series
        document.series_suggestion = None
        document.save(update_fields=["series", "series_suggestion"])
        series.refresh_series(decision.series)
        document.refresh_from_db(fields=["period_label"])
    elif decision.suggestion is not None:
        document.series_suggestion = decision.suggestion
        document.save(update_fields=["series_suggestion"])
    elif decision.created_series:
        new_series = Series.objects.create(
            name=series.default_series_name(document),
            correspondent_id=document.correspondent_id,
            document_type_id=document.document_type_id,
            is_suggested=True,
        )
        document.series = new_series
        document.save(update_fields=["series"])
        partner_ids = [
            d.pk
            for d in Document.objects.filter(pk__in=decision.partner_ids, series__isnull=True)
            if d.source_of("series") != Source.USER
        ]
        Document.objects.filter(pk__in=partner_ids).update(series=new_series)
        series.refresh_series(new_series)
        document.refresh_from_db(fields=["period_label"])
