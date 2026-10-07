"""Sender and title detection using Docling's reading order and page geometry."""

from __future__ import annotations

import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass

from rapidfuzz import fuzz

from apps.analysis.extraction import ORG_SUFFIX, POSTCODE, RETURN_ADDRESS_SEP, SUBJECT_RE

_TITLE_WORDS = re.compile(
    r"(rechnung|invoice|abrechnung|statement|bescheid|mahnung|vertrag|police|versicherung|"
    r"lieferung|kündigung|bestätigung|mitteilung|angebot|gutschrift|report|notice|certificate|"
    r"zeugnis|bescheinigung|nachweis|auskunft|urkunde|antrag|erklärung|kontoauszug|quittung|"
    r"benachrichtigung|ausweis|zertifikat|befund|attest|lohnsteuer|übersicht)",
    re.I,
)
_FIELD_LINE = re.compile(
    r"^(datum|date|rechnungs?\s*(?:nr|nummer)|invoice\s*(?:no|number)|kunden?\s*(?:nr|nummer)|"
    r"customer\s*(?:no|number|id)|vertrags?\s*(?:nr|nummer)|iban|bic|telefon|tel\.?|fax|e-?mail)\b",
    re.I,
)
_BODY_LINE = re.compile(r"^(sehr geehrt|dear |hiermit |vielen dank|thank you|mit freundlichen)", re.I)
# Sentences rather than headings: "Dieser Bescheid besteht aus", "Einzelheiten – bitte wenden".
_SENTENCE_LINE = re.compile(r"^(dieses?r?|diese|wir|sie|bitte|please)\b|\b(bitte|please)\b", re.I)
_SENDER_LABEL = re.compile(r"^(?:postanschrift|absender|sender)\s*:\s*", re.I)
_POSSESSIVE = re.compile(r"^(?:ihre?|your)\s+(?=\S)", re.I)


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
    height: float = 0.0


def _normal(value: str) -> str:
    return re.sub(r"\W+", " ", value.casefold()).strip()


def _ascii(value: str) -> str:
    value = value.replace("ß", "ss")
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()


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
                y0, y1 = raw.get("y0"), raw.get("y1")
                line_height = float(y1) - float(y0) if y0 is not None and y1 is not None else 0.0
                lines.append(
                    _Line(
                        text=text,
                        top=max(0.0, min(1.0, float(y1 or 0) / height)),
                        label=labels.get(_normal(text), ""),
                        height=max(0.0, line_height),
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


def _evidence_texts(structured: dict, layout: dict) -> list[str]:
    """Every text line Docling saw, on all stored pages, for repetition checks."""
    texts: list[str] = []
    for page in layout.get("pages", []) if isinstance(layout, dict) else []:
        if isinstance(page, dict):
            texts.extend(
                line["text"]
                for line in page.get("lines", [])
                if isinstance(line, dict) and isinstance(line.get("text"), str)
            )
    if not texts:
        texts = [
            item["text"]
            for item in structured.get("texts", [])
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
    return texts


def detect(structured: dict, layout: dict) -> DetectedFields:
    lines = _first_page_lines(structured, layout)
    evidence = _evidence_texts(structured, layout)
    sender, sender_score = _sender(lines, evidence)
    title, title_score = _title(lines, sender)
    return DetectedFields(
        sender=restore_diacritics(sender, evidence) if sender else None,
        title=restore_diacritics(title, evidence) if title else None,
        sender_confidence=round(min(sender_score, 1.0), 3),
        title_confidence=round(min(title_score, 1.0), 3),
    )


def _sender_candidate(text: str) -> tuple[str, bool]:
    """Strip "Postanschrift:" labels and the trailing ", 12345 City" of address lines."""
    labelled = bool(_SENDER_LABEL.match(text))
    text = _SENDER_LABEL.sub("", text)
    candidate = RETURN_ADDRESS_SEP.split(text)[0].strip(" ,")
    if POSTCODE.search(candidate) and "," in candidate:
        candidate = candidate.split(",")[0].strip()
    return candidate, labelled


def _repeated(candidate: str, evidence: list[str]) -> bool:
    """The sender's name recurs (return address, footer, signature, payee)."""
    needle = _normal(_ascii(candidate))
    if len(needle) < 6:
        return False
    hits = 0
    for text in evidence:
        haystack = _normal(_ascii(text))
        similar = len(haystack) >= len(needle) and fuzz.partial_ratio(needle, haystack) >= 92
        if needle in haystack or similar:
            hits += 1
            if hits >= 2:
                return True
    return False


def _sender(lines: list[_Line], evidence: list[str]) -> tuple[str | None, float]:
    best: tuple[float, str] = (0.0, "")
    for line in lines[:35]:
        text = line.text.strip(" ,")
        if not 3 <= len(text) <= 140 or _FIELD_LINE.search(text) or _BODY_LINE.search(text):
            continue
        candidate, labelled = _sender_candidate(text)
        if len(candidate) < 3:
            continue
        score = 0.0
        if ORG_SUFFIX.search(candidate):
            score += 0.58
        if POSTCODE.search(text) and (RETURN_ADDRESS_SEP.search(text) or candidate != text):
            score += 0.22
        if labelled:
            score += 0.25
        if line.label in {"page_header", "title"}:
            score += 0.22
        elif line.label == "section_header":
            score += 0.12
        if line.top >= 0.7:
            score += 0.12
        if score > 0 and _repeated(candidate, evidence):
            score += 0.12
        if re.search(r"(@|www\.|https?://)", text, re.I):
            score -= 0.25
        if len(candidate) > 90 or candidate[:1].isdigit() or len(candidate.split()) > 9:
            score -= 0.3
        if score > best[0]:
            best = (score, candidate)
    return (best[1], best[0]) if best[0] >= 0.55 else (None, best[0])


def _title(lines: list[_Line], sender: str | None) -> tuple[str | None, float]:
    heights = [line.height for line in lines if line.height > 0]
    typical_height = statistics.median(heights) if heights else 0.0
    best: tuple[float, str] = (0.0, "")
    for line in lines[:60]:
        text = line.text.strip(" -–—:;,.|")
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
        if typical_height and line.height >= typical_height * 1.25 and len(text.split()) <= 8:
            score += 0.15
        if _FIELD_LINE.search(text) or POSTCODE.search(text) or _BODY_LINE.search(text):
            score -= 0.65
        if not explicit and (_SENTENCE_LINE.search(text) or len(text.split()) > 10):
            score -= 0.3
        if re.search(r"\b(?:IBAN|BIC|EUR|€)\b", text, re.I):
            score -= 0.35
        if score > best[0]:
            best = (score, text)
    if best[0] < 0.55:
        return None, best[0]
    return _clean_title(best[1]), best[0]


def _clean_title(title: str) -> str:
    """Drop the possessive: "Ihre Beitragsrechnung" names the document "Beitragsrechnung"."""
    stripped = _POSSESSIVE.sub("", title)
    if len(stripped) >= 6:
        title = stripped[:1].upper() + stripped[1:]
    return title[:120]


def restore_diacritics(value: str, evidence: list[str]) -> str:
    """Undo OCR-dropped umlauts when the document spells the same word with them.

    Tesseract regularly reads "für" as "fur" in one place and correctly elsewhere;
    the accented spelling wins when the same document uses it more often.
    """
    spellings: dict[str, Counter[str]] = {}
    for text in evidence:
        for word in re.findall(r"\w+", text):
            spellings.setdefault(_ascii(word).casefold(), Counter())[word.lower()] += 1

    def replace(match: re.Match[str]) -> str:
        word = match.group(0)
        options = spellings.get(word.casefold())
        if not word.isascii() or not options:
            return word
        accented = options.most_common(1)[0][0]
        if accented.isascii():
            return word
        if word.isupper() and len(word) > 1:
            return accented.upper()
        if word[:1].isupper():
            return accented[:1].upper() + accented[1:]
        return accented

    return re.sub(r"\w+", replace, value)


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
