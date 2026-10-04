"""Rule-based metadata extraction from OCR text (German and English documents).

Everything here runs in-process on decrypted text held in memory; results that
are stored go into encrypted columns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

MONTHS = {
    # German
    "januar": 1, "jänner": 1, "jan": 1, "februar": 2, "feb": 2, "märz": 3, "maerz": 3, "mär": 3, "mrz": 3,
    "april": 4, "apr": 4, "mai": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "oktober": 10, "okt": 10, "november": 11, "nov": 11,
    "dezember": 12, "dez": 12,
    # English
    "january": 1, "february": 2, "march": 3, "mar": 3, "may": 5, "june": 6, "july": 7, "october": 10,
    "oct": 10, "december": 12, "dec": 12,
}  # fmt: skip

_MONTH_NAMES = "|".join(sorted((re.escape(m) for m in MONTHS), key=len, reverse=True))

DATE_PATTERNS = [
    # 12.03.2026 / 12.03.26 / 12/03/2026 / 12-03-2026
    (re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})\b"), "dmy"),
    # 2026-03-12
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "ymd"),
    # 12. März 2026 / 12 March 2026
    (re.compile(rf"\b(\d{{1,2}})\.?\s+({_MONTH_NAMES})\.?\s+(\d{{4}})\b", re.I), "d_month_y"),
    # March 12, 2026
    (re.compile(rf"\b({_MONTH_NAMES})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.I), "month_d_y"),
]

DATE_HINTS = re.compile(
    r"(datum|date|vom|dated|rechnungsdatum|invoice date|ausstellungsdatum|bescheid vom|erstellt am|stand)",
    re.I,
)
PERIOD_HINTS = re.compile(r"(zeitraum|abrechnungszeitraum|leistungszeitraum|period|monat)", re.I)

ORG_SUFFIX = re.compile(
    r"\b(GmbH|AG|SE|KG|KGaA|OHG|e\.\s?V\.|eG|mbH|UG|GbR|Inc\.?|Ltd\.?|LLC|plc|S\.A\.|B\.V\.|"
    r"Versicherung(?:en)?|Bank|Sparkasse|Volksbank|Stadtwerke|Finanzamt|Krankenkasse|Universität|"
    r"Hochschule|Landratsamt|Rathaus|Stadt|Gemeinde|Bundesagentur|Deutsche Rentenversicherung|AOK|"
    r"Techniker|Barmer|Telekom|Vodafone|Amt)\b"
)
RETURN_ADDRESS_SEP = re.compile(r"\s+[·•|]\s+|\s+-\s+|\s{3,}")
POSTCODE = re.compile(r"\b\d{5}\s+[A-ZÄÖÜ][a-zäöüß]+")

SUBJECT_RE = re.compile(r"^\s*(?:betreff|betr\.|subject|re|ihr zeichen)\s*:\s*(.{4,120})$", re.I | re.M)

AMOUNT_RE = re.compile(
    r"(?:(?:EUR|€|USD|\$|CHF)\s*)?(-?\d{1,3}(?:[.\s']\d{3})*(?:[.,]\d{2})|-?\d+[.,]\d{2})\s*(?:EUR|€|USD|\$|CHF)?"
)
TOTAL_HINTS = re.compile(
    r"(gesamtbetrag|rechnungsbetrag|endbetrag|zu zahlen|zahlbetrag|summe|gesamt|total|amount due|"
    r"auszahlungsbetrag|netto-?verdienst|überweisung)",
    re.I,
)

IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?)\b")

REFERENCE_RE = re.compile(
    r"(?P<label>rechnungs?-?\s?(?:nr|nummer)|invoice\s?(?:no|number|#)|kunden-?\s?(?:nr|nummer)|"
    r"customer\s?(?:no|number|id)|vertrags-?\s?(?:nr|nummer)|contract\s?(?:no|number)|"
    r"versicherungs(?:schein)?-?\s?(?:nr|nummer)|policy\s?(?:no|number)|steuer-?\s?(?:nr|nummer)|"
    r"aktenzeichen|personal-?\s?(?:nr|nummer)|kennzeichen|matrikel-?\s?(?:nr|nummer))"
    r"\.?\s*[:#]?\s*(?P<value>(?:[A-Z]{1,4} )?[A-Z0-9][A-Z0-9\-/]{2,30})",
    re.I,
)

LICENSE_PLATE_RE = re.compile(r"\b([A-ZÄÖÜ]{1,3}-[A-Z]{1,2}\s?\d{1,4}[EH]?)\b")


@dataclass
class Extracted:
    document_date: date | None = None
    dates: list[str] = field(default_factory=list)
    sender: str | None = None
    subject: str | None = None
    total_amount: str | None = None
    amounts: list[str] = field(default_factory=list)
    ibans: list[str] = field(default_factory=list)
    references: dict[str, str] = field(default_factory=dict)
    license_plates: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "document_date": self.document_date.isoformat() if self.document_date else None,
            "sender": self.sender,
            "subject": self.subject,
            "total_amount": self.total_amount,
            "amounts": self.amounts[:10],
            "ibans": self.ibans[:5],
            "references": self.references,
            "license_plates": self.license_plates[:5],
        }


def _to_date(kind: str, m: re.Match[str]) -> date | None:
    try:
        if kind == "dmy":
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100:
                y += 2000 if y < 70 else 1900
            return date(y, mo, d)
        if kind == "ymd":
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if kind == "d_month_y":
            return date(int(m.group(3)), MONTHS[m.group(2).lower().rstrip(".")], int(m.group(1)))
        if kind == "month_d_y":
            return date(int(m.group(3)), MONTHS[m.group(1).lower().rstrip(".")], int(m.group(2)))
    except (ValueError, KeyError):
        return None
    return None


def find_dates(text: str) -> list[tuple[date, int]]:
    """All plausible dates with their character offset."""
    out: list[tuple[date, int]] = []
    today = date.today()
    for pattern, kind in DATE_PATTERNS:
        for m in pattern.finditer(text):
            d = _to_date(kind, m)
            if d and date(1950, 1, 1) <= d <= today + timedelta(days=400):
                out.append((d, m.start()))
    out.sort(key=lambda x: x[1])
    return out


def pick_document_date(text: str, dates: list[tuple[date, int]]) -> date | None:
    if not dates:
        return None
    length = max(len(text), 1)
    today = date.today()
    best: tuple[float, date] | None = None
    for d, pos in dates:
        score = 0.0
        line_start = text.rfind("\n", 0, pos) + 1
        line = text[line_start : text.find("\n", pos) if text.find("\n", pos) != -1 else len(text)]
        before = text[max(0, pos - 40) : pos]
        if DATE_HINTS.search(before) or DATE_HINTS.search(line):
            score += 3
        if PERIOD_HINTS.search(before):
            score -= 1
        if re.search(r"(geb\.|geboren|birth|fällig|due|bis zum|zahlbar)", before, re.I):
            score -= 2
        if pos < length * 0.25:
            score += 2
        if re.search(r"[A-ZÄÖÜ][a-zäöüß]+,\s*(den\s*)?$", text[line_start:pos]):  # "Berlin, 12.03.2026"
            score += 2
        if d > today + timedelta(days=30):
            score -= 3
        score -= pos / length  # earlier is better
        if best is None or score > best[0]:
            best = (score, d)
    return best[1] if best else None


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def find_sender(text: str) -> str | None:
    lines = _lines(text)[:30]
    # 1) Window-envelope return address line: "Firma GmbH · Straße 1 · 12345 Stadt"
    for ln in lines:
        if POSTCODE.search(ln) and RETURN_ADDRESS_SEP.search(ln):
            first = RETURN_ADDRESS_SEP.split(ln)[0].strip(" ,")
            if 3 <= len(first) <= 80 and not first[0].isdigit():
                return first
    # 2) First line with an organisation marker
    for ln in lines[:20]:
        m = ORG_SUFFIX.search(ln)
        if m and len(ln) <= 90:
            candidate = RETURN_ADDRESS_SEP.split(ln)[0].strip(" ,")
            if len(candidate) >= 3 and not re.search(r"\d{5}", candidate):
                return candidate
    return None


def find_subject(text: str) -> str | None:
    m = SUBJECT_RE.search(text)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()[:120]
    # Heuristic: a short line in the upper part with a document keyword
    for ln in _lines(text)[:40]:
        if 6 <= len(ln) <= 90 and re.match(
            r"^(rechnung|invoice|gehaltsabrechnung|entgeltabrechnung|lohnabrechnung|kontoauszug|"
            r"beitragsrechnung|bescheid|mahnung|kündigung|vertrag|police|versicherungsschein|"
            r"jahresabrechnung|abrechnung|statement|bestätigung|mitteilung)\b",
            ln,
            re.I,
        ):
            return re.sub(r"\s+", " ", ln)[:120]
    return None


def _parse_amount(raw: str) -> Decimal | None:
    s = raw.replace(" ", "").replace("'", "")
    if re.search(r",\d{2}$", s):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def find_amounts(text: str) -> tuple[str | None, list[str]]:
    amounts: list[str] = []
    total: Decimal | None = None
    for ln in text.splitlines():
        if not re.search(r"(€|EUR|USD|\$|CHF)", ln) and not TOTAL_HINTS.search(ln):
            continue
        for m in AMOUNT_RE.finditer(ln):
            value = _parse_amount(m.group(1))
            if value is None:
                continue
            formatted = f"{value:.2f}"
            amounts.append(formatted)
            if TOTAL_HINTS.search(ln) and (total is None or value > total):
                total = value
    if total is None and amounts:
        total = max(Decimal(a) for a in amounts)
    return (f"{total:.2f}" if total is not None else None), list(dict.fromkeys(amounts))


def _iban_valid(iban: str) -> bool:
    iban = iban.replace(" ", "")
    if not 15 <= len(iban) <= 34:
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


def find_ibans(text: str) -> list[str]:
    out = []
    for m in IBAN_RE.finditer(text.upper()):
        iban = m.group(1).replace(" ", "")
        if _iban_valid(iban):
            out.append(iban)
    return list(dict.fromkeys(out))


def find_references(text: str) -> dict[str, str]:
    refs: dict[str, str] = {}
    for m in REFERENCE_RE.finditer(text):
        label = re.sub(r"[\s.-]", "", m.group("label").lower())
        refs.setdefault(label, m.group("value").strip())
    return dict(list(refs.items())[:10])


def extract(text: str) -> Extracted:
    dates = find_dates(text)
    total, amounts = find_amounts(text)
    return Extracted(
        document_date=pick_document_date(text, dates),
        dates=[d.isoformat() for d, _ in dates[:20]],
        sender=find_sender(text),
        subject=find_subject(text),
        total_amount=total,
        amounts=amounts,
        ibans=find_ibans(text),
        references=find_references(text),
        license_plates=list(dict.fromkeys(LICENSE_PLATE_RE.findall(text)))[:5],
    )
