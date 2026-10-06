"""Sender and title detection using Docling's reading order and page geometry."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from apps.analysis.extraction import ORG_SUFFIX, POSTCODE, RETURN_ADDRESS_SEP, SUBJECT_RE

_TITLE_WORDS = re.compile(
    r"(rechnung|invoice|abrechnung|statement|bescheid|mahnung|vertrag|police|versicherung|"
    r"lieferung|kündigung|bestätigung|mitteilung|angebot|gutschrift|report|notice|certificate)",
    re.I,
)
_FIELD_LINE = re.compile(
    r"^(datum|date|rechnungs?\s*(?:nr|nummer)|invoice\s*(?:no|number)|kunden?\s*(?:nr|nummer)|"
    r"customer\s*(?:no|number|id)|vertrags?\s*(?:nr|nummer)|iban|bic|telefon|tel\.?|fax|e-?mail)\b",
    re.I,
)
_BODY_LINE = re.compile(r"^(sehr geehrt|dear |hiermit |vielen dank|thank you|mit freundlichen)", re.I)


@dataclass(frozen=True)
class DetectedFields:
    sender: str | None = None
    title: str | None = None
    sender_confidence: float = 0.0
    title_confidence: float = 0.0

    @property
    def low_confidence(self) -> bool:
        return self.sender_confidence < 0.72 or self.title_confidence < 0.72


@dataclass(frozen=True)
class _Line:
    text: str
    top: float
    label: str = ""


def _normal(value: str) -> str:
    return re.sub(r"\W+", " ", value.casefold()).strip()


def _structured_labels(structured: dict) -> dict[str, str]:
    labels: dict[str, str] = {}
    for item in structured.get("texts", []):
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        label = item.get("label")
        if isinstance(text, str) and isinstance(label, str):
            labels[_normal(text)] = label
    return labels


def _first_page_lines(structured: dict, layout: dict) -> list[_Line]:
    pages = layout.get("pages", []) if isinstance(layout, dict) else []
    if pages and isinstance(pages[0], dict):
        page = pages[0]
        height = float(page.get("height") or 1)
        labels = _structured_labels(structured)
        lines = []
        for raw in page.get("lines", []):
            if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
                continue
            text = " ".join(raw["text"].split())
            if text:
                lines.append(
                    _Line(
                        text=text,
                        top=max(0.0, min(1.0, float(raw.get("y1") or 0) / height)),
                        label=labels.get(_normal(text), ""),
                    )
                )
        return sorted(lines, key=lambda line: -line.top)

    # Older records do not have parsed lines. Structured text still preserves
    # semantic labels and is a useful, if less precise, fallback.
    lines = []
    for item in structured.get("texts", []):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        prov = item.get("prov") or []
        page_no = prov[0].get("page_no") if prov and isinstance(prov[0], dict) else 1
        if page_no not in {0, 1}:
            continue
        for text in item["text"].splitlines():
            text = " ".join(text.split())
            if text:
                lines.append(_Line(text=text, top=0.5, label=str(item.get("label") or "")))
    return lines


def detect(structured: dict, layout: dict) -> DetectedFields:
    lines = _first_page_lines(structured, layout)
    sender, sender_score = _sender(lines)
    title, title_score = _title(lines, sender)
    return DetectedFields(
        sender=sender,
        title=title,
        sender_confidence=round(min(sender_score, 1.0), 3),
        title_confidence=round(min(title_score, 1.0), 3),
    )


def _sender(lines: list[_Line]) -> tuple[str | None, float]:
    best: tuple[float, str] = (0.0, "")
    for line in lines[:35]:
        text = line.text.strip(" ,")
        if not 3 <= len(text) <= 140 or _FIELD_LINE.search(text):
            continue
        candidate = RETURN_ADDRESS_SEP.split(text)[0].strip(" ,")
        score = 0.0
        if ORG_SUFFIX.search(candidate):
            score += 0.58
        if POSTCODE.search(text) and RETURN_ADDRESS_SEP.search(text):
            score += 0.22
        if line.label in {"page_header", "title"}:
            score += 0.22
        if line.top >= 0.7:
            score += 0.12
        if re.search(r"(@|www\.|https?://)", text, re.I):
            score -= 0.25
        if len(candidate) > 90 or candidate[:1].isdigit():
            score -= 0.3
        if score > best[0]:
            best = (score, candidate)
    return (best[1], best[0]) if best[0] >= 0.55 else (None, best[0])


def _title(lines: list[_Line], sender: str | None) -> tuple[str | None, float]:
    best: tuple[float, str] = (0.0, "")
    for line in lines[:60]:
        text = line.text.strip(" -–—:;,.")
        if not 6 <= len(text) <= 140 or text == sender:
            continue
        score = 0.0
        explicit = SUBJECT_RE.fullmatch(text)
        if explicit:
            text = re.sub(r"\s+", " ", explicit.group(1)).strip()
            score += 0.82
        if line.label == "title":
            score += 0.72
        elif line.label == "section_header":
            score += 0.52
        if _TITLE_WORDS.search(text):
            score += 0.46
        if 0.3 <= line.top <= 0.85:
            score += 0.12
        if _FIELD_LINE.search(text) or POSTCODE.search(text) or _BODY_LINE.search(text):
            score -= 0.65
        if re.search(r"\b(?:IBAN|BIC|EUR|€)\b", text, re.I):
            score -= 0.35
        if score > best[0]:
            best = (score, text)
    return (best[1][:120], best[0]) if best[0] >= 0.55 else (None, best[0])


def merge_vlm(layout: DetectedFields, sender: str | None, title: str | None, evidence: str) -> DetectedFields:
    """Prefer supported VLM fields while rejecting unrelated generations."""
    selected_sender = layout.sender
    sender_confidence = layout.sender_confidence
    if sender and _supported(sender, evidence, threshold=68):
        selected_sender = sender
        sender_confidence = 0.94

    selected_title = layout.title
    title_confidence = layout.title_confidence
    if title and _supported(title, evidence, threshold=52):
        selected_title = title
        title_confidence = 0.92

    return DetectedFields(selected_sender, selected_title, sender_confidence, title_confidence)


def _supported(value: str, evidence: str, threshold: int) -> bool:
    value = _normal(value)
    evidence = _normal(evidence)
    return bool(value and evidence) and (
        value in evidence or fuzz.partial_ratio(value, evidence) >= threshold
    )
