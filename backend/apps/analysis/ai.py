"""Document understanding with a local vision-language model served by Ollama.

The model looks at the first page images and the OCR text together and names
the sender, title, date and type the way a person reading the letter would:
the letterhead rather than a return-address line, never the recipient. It sees
nothing but the one document, so it cannot borrow from other files.

Document data only ever goes to `DOCNEST_OLLAMA_URL` — by default the Ollama
container on the internal Docker network.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

LIST_TIMEOUT = 10
# Shown in Settings as a starting point; any Ollama model works.
MAX_TAGS = 4
MAX_KNOWN_TAGS = 80  # existing tags offered to the model; more only cost time
MAX_ANSWER_TOKENS = 2048  # the answer is ~100 tokens; this only stops a runaway generation
# Shown in Settings as a starting point; any Ollama model works. "-instruct" variants answer
# directly, while reasoning ("thinking") variants spend minutes deliberating first.
SUGGESTED_MODELS = [
    ("qwen3-vl:4b-instruct", "Recommended for 8 GB servers: ≈1 min per document on 8 cores, 3.6 GB RAM"),
    ("qwen3-vl:8b-instruct", "More accurate, half as fast; 6.2 GB RAM, so a server with 12 GB or more"),
    ("qwen2.5vl:7b", "Proven alternative (≈6 GB RAM)"),
    ("gemma3:12b", "Larger and slower (≈9 GB RAM)"),
    ("qwen3-vl:32b-instruct", "Best results, for a big GPU (≈21 GB)"),
]


class ModelUnavailable(Exception):
    """Ollama cannot be reached or the model is not installed: wait and try again later."""


class ModelFailed(Exception):
    """The model answered, but not usefully: fall back to rule-based detection."""


class ModelCrashed(Exception):
    """Ollama failed while running the model (often: not enough memory). Retry; never guess instead."""


@dataclass(frozen=True)
class ModelInfo:
    name: str
    size: int
    parameter_size: str
    vision: bool
    thinking: bool  # reasons before answering: much slower, sometimes cannot be switched off


@dataclass(frozen=True)
class DocumentType:
    slug: str
    name: str


# What the built-in types mean: a bare "Notice" or "Mail" leaves the model guessing.
TYPE_HINTS = {
    "invoice": "bills and invoices, e.g. Rechnung, Beitragsrechnung, Zahlungsaufforderung",
    "statement": "periodic statements, e.g. Kontoauszug, Verdienstabrechnung, Jahresübersicht",
    "notice": "decisions, notices and certificates from authorities, courts or insurers, e.g. Bescheid, "
    "Führungszeugnis, Bescheinigung, Mahnung, Fristverlängerung",
    "contract": "contracts, policies and their changes, e.g. Vertrag, Versicherungsschein, Kündigung",
    "mail": "other letters and information without a bill, statement or decision",
    "other": "anything else, e.g. handwritten notes, manuals, receipts",
}


@dataclass
class ModelFields:
    sender: str | None = None
    recipient: str | None = None
    title: str | None = None
    document_date: date | None = None
    document_type: str | None = None  # a slug from the offered types
    tags: list[str] | None = None  # None: the model was not asked
    model: str = ""


MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/:]{0,199}$")


def valid_model_name(name: str) -> bool:
    return bool(MODEL_NAME.match(name))


def configured() -> bool:
    return bool(settings.OLLAMA_URL)


# --- Ollama API ---------------------------------------------------------------


def _request(path: str, payload: dict[str, Any] | None = None, *, timeout: float) -> dict[str, Any]:
    if not configured():
        raise ModelUnavailable("No Ollama server is configured (DOCNEST_OLLAMA_URL)")
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(  # noqa: S310 - URL comes from the operator's configuration
        settings.OLLAMA_URL + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        if exc.code == 404:
            raise ModelUnavailable(detail or "Model not found in Ollama") from exc
        if exc.code >= 500 and "not found" in detail.lower():
            raise ModelUnavailable(detail) from exc
        if exc.code >= 500:
            raise ModelCrashed(
                f"The AI model stopped while reading ({detail or exc.code}); "
                "a smaller model or more memory for the server helps"
            ) from exc
        raise ModelFailed(f"Ollama error {exc.code}: {detail}") from exc
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ModelUnavailable(f"Ollama is not reachable at {settings.OLLAMA_URL}: {reason}") from exc
    except json.JSONDecodeError as exc:
        raise ModelFailed("Ollama returned invalid JSON") from exc


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(exc.read().decode()).get("error", ""))[:300]
    except Exception:
        return ""


def list_models() -> list[ModelInfo]:
    """Installed models, vision-capable ones first."""
    tags = _request("/api/tags", timeout=LIST_TIMEOUT)
    models = []
    for entry in tags.get("models", []):
        name = str(entry.get("name") or entry.get("model") or "")
        if not name:
            continue
        details = entry.get("details") or {}
        capabilities = _capabilities(name)
        models.append(
            ModelInfo(
                name=name,
                size=int(entry.get("size") or 0),
                parameter_size=str(details.get("parameter_size") or ""),
                vision="vision" in capabilities,
                thinking="thinking" in capabilities,
            )
        )
    return sorted(models, key=lambda m: (not m.vision, m.thinking, m.name))


def _capabilities(name: str) -> set[str]:
    try:
        info = _request("/api/show", {"model": name}, timeout=LIST_TIMEOUT)
    except (ModelUnavailable, ModelFailed):
        return set()
    return {str(c) for c in info.get("capabilities") or []}


def pull(name: str, progress: Any = None) -> None:
    """Download a model into Ollama, reporting (status, completed, total) as it goes."""
    if not configured():
        raise ModelUnavailable("No Ollama server is configured (DOCNEST_OLLAMA_URL)")
    request = urllib.request.Request(  # noqa: S310
        settings.OLLAMA_URL + "/api/pull",
        data=json.dumps({"model": name, "stream": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.AI_TIMEOUT_SECONDS) as response:  # noqa: S310
            for raw in response:
                line = json.loads(raw.decode() or "{}")
                if line.get("error"):
                    raise ModelFailed(str(line["error"])[:300])
                if progress:
                    progress(str(line.get("status", "")), line.get("completed"), line.get("total"))
    except urllib.error.HTTPError as exc:
        raise ModelFailed(f"Ollama error {exc.code}: {_error_detail(exc)}") from exc
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        raise ModelUnavailable(f"Ollama is not reachable at {settings.OLLAMA_URL}") from exc


# --- Document analysis ----------------------------------------------------------

SYSTEM_PROMPT = (
    "You read scanned letters and documents for a private person's document archive. "
    "The documents are mostly German. You answer with JSON only."
)

INSTRUCTIONS = """\
Read this document (page images{text_note}) and determine:

sender: the organisation or person that issued the document, spelled exactly as printed.
  Look at the letterhead, the logo, the page header, the footer (company details,
  bank details, imprint) and the signature.
  The recipient — the person and address in the address window — is NEVER the sender.
  A recipient's town (e.g. in "27755 Delmenhorst") does not make a local authority the sender.
  A small one-line return address above the address window sometimes names a mailing or
  payroll service; when the letterhead or page header names a different organisation,
  that organisation is the sender.
  Use the organisation's name as written out in text — the return address, the footer or the
  signature — not a stylised logo or abbreviation, with its legal form if printed (e.g.
  "Bundesamt für Justiz", "Finanzamt Delmenhorst", "VRK Sachversicherung AG"). The name only,
  without address. null if no sender is recognisable (e.g. handwritten notes).
recipient: the name of the addressee, or null.
german_title: a short German title to file the document under (for a document printed in
  several languages, use its German wording): what the
  document is, plus its subject or period when that distinguishes it — for example
  "Erweitertes Führungszeugnis", "Verdienstabrechnung Juni 2026",
  "Fristverlängerung Umsatzsteuer-Voranmeldung". Do not include the sender, the recipient
  or a full date. At most 8 words.
document_date: the date the document was issued (letter date, invoice date, "Datum"),
  as YYYY-MM-DD. Not a birth date, due date or a period. null if there is none.
document_type: the slug of the type that fits best: {types}
tags: 1 to 4 short German keywords to find the document again by topic (e.g. "Steuer",
  "Versicherung", "Auto", "Gesundheit", "Arbeit", "Wohnung"). Take them from the existing tags
  below whenever one fits, spelled exactly as listed; make up a new one only for a topic none of
  them covers. Not the sender, not the document type, no dates. Existing tags: {tags}
"""

CONTEXT_NOTE = (
    "\nThe owner forwarded this document by email. The email can tell what the document is "
    "and who sent it originally; the owner who forwarded it is never the sender:\n"
)
MAX_CONTEXT_CHARS = 1200
TEXT_NOTE = " and the OCR text below; the OCR text may contain recognition errors, so trust the images"


def _schema(type_slugs: list[str]) -> dict[str, Any]:
    nullable = {"type": ["string", "null"]}
    return {
        "type": "object",
        "properties": {
            "sender": nullable,
            "recipient": nullable,
            "german_title": nullable,
            "document_date": nullable,
            "document_type": {"type": ["string", "null"], "enum": [*type_slugs, None]}
            if type_slugs
            else nullable,
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_TAGS},
        },
        "required": ["sender", "recipient", "german_title", "document_date", "document_type", "tags"],
    }


def analyze(
    model: str,
    *,
    images: list[bytes],
    text: str,
    types: list[DocumentType],
    tags: list[str] | None = None,
    context: str = "",
) -> ModelFields:
    """Ask `model` about one document. No timeout beyond DOCNEST_AI_TIMEOUT_SECONDS: slow is fine.

    `context`: how the document arrived, e.g. the email it was attached to.
    """
    listing = (
        "".join(
            f'\n    "{t.slug}": {t.name}' + (f" — {TYPE_HINTS[t.slug]}" if t.slug in TYPE_HINTS else "")
            for t in types
        )
        or "null"
    )
    known = ", ".join(f'"{t}"' for t in (tags or [])[:MAX_KNOWN_TAGS]) or "none yet"
    prompt = INSTRUCTIONS.format(text_note=TEXT_NOTE if text.strip() else "", types=listing, tags=known)
    if text.strip():
        prompt += "\nOCR text:\n<<<\n" + text.strip()[: settings.AI_TEXT_CHARS] + "\n>>>\n"
    if context.strip():
        prompt += CONTEXT_NOTE + "<<<\n" + context.strip()[:MAX_CONTEXT_CHARS] + "\n>>>\n"
    message: dict[str, Any] = {"role": "user", "content": prompt}
    if images:
        message["images"] = [base64.b64encode(image).decode() for image in images]
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, message],
        "format": _schema([t.slug for t in types]),
        "stream": False,
        "think": False,
        "keep_alive": "10m",
        "options": {
            "temperature": 0,
            "num_ctx": settings.AI_CONTEXT_TOKENS,
            "num_predict": MAX_ANSWER_TOKENS,
        },
    }
    return _fields(_chat(payload), model, {t.slug for t in types})


def _chat(payload: dict[str, Any]) -> dict[str, Any]:
    """Run a chat request that must answer with a JSON object."""
    try:
        response = _request("/api/chat", payload, timeout=settings.AI_TIMEOUT_SECONDS)
    except ModelFailed as exc:
        if "think" not in str(exc).lower():
            raise
        # Models without a reasoning mode reject the switch instead of ignoring it.
        payload.pop("think")
        response = _request("/api/chat", payload, timeout=settings.AI_TIMEOUT_SECONDS)
    content = str((response.get("message") or {}).get("content") or "")
    if not content.strip() and response.get("done_reason") == "length":
        raise ModelFailed(
            "The model used up its answer length before answering. Reasoning models do that; "
            'choose an "-instruct" variant'
        )
    try:
        data = json.loads(_strip_fences(content))
    except json.JSONDecodeError as exc:
        raise ModelFailed("The model did not answer with JSON") from exc
    if not isinstance(data, dict):
        raise ModelFailed("The model did not answer with a JSON object")
    return data


def _strip_fences(content: str) -> str:
    content = content.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.S)
    return match.group(1) if match else content


def _fields(data: dict[str, Any], model: str, slugs: set[str]) -> ModelFields:
    sender = _without_address(clean(data.get("sender")))
    recipient = clean(data.get("recipient"))
    if sender and recipient and _same_party(sender, recipient):
        sender = None  # the model confused the addressee with the sender
    doc_type = clean(data.get("document_type"))
    return ModelFields(
        sender=sender,
        recipient=recipient,
        title=clean(data.get("german_title") or data.get("title"), limit=120),
        document_date=_date(data.get("document_date")),
        document_type=doc_type if doc_type in slugs else None,
        tags=_tags(data.get("tags")),
        model=model,
    )


def clean(value: object, *, limit: int = 150) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip(" -–—:;,\"'")
    if not cleaned or cleaned.casefold() in {"none", "null", "n/a", "unknown", "unbekannt", "-"}:
        return None
    return cleaned[:limit]


def _tags(value: object) -> list[str]:
    tags: list[str] = []
    for item in value if isinstance(value, list) else []:
        tag = clean(item, limit=40)
        if tag and tag.casefold() not in {t.casefold() for t in tags}:
            tags.append(tag)
    return tags[:MAX_TAGS]


def _without_address(sender: str | None) -> str | None:
    """Cut an appended address: "Versicherer im Raum der Kirchen, Doktorweg 2-4, 32756 Detmold"."""
    if not sender:
        return sender
    parts = [part.strip() for part in re.split(r",|\s[·•|]\s", sender)]
    for index, part in enumerate(parts[1:], start=1):
        if re.search(r"\d", part):  # a street number, postcode or PO box: the address begins
            return ", ".join(parts[:index]) or None
    return sender


def _same_party(a: str, b: str) -> bool:
    return fuzz.token_set_ratio(a.casefold(), b.casefold()) >= 90


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None
    if not date(1950, 1, 1) <= parsed <= date.today() + timedelta(days=400):
        return None
    return parsed


# --- Folder names for filing suggestions -----------------------------------------------

FOLDER_SYSTEM_PROMPT = (
    "You organise a private person's document archive into folders. "
    "The documents are mostly German. You answer with JSON only."
)

FOLDER_INSTRUCTIONS = """\
Documents in the folder "{parent}" should be sorted into its subfolders.
Existing subfolders: {existing}

Groups of documents that are not sorted yet:
{groups}

For each group give the subfolder it belongs in:
- the name of an existing subfolder when the group fits it, spelled exactly as listed;
- otherwise a new short German folder name: a plural noun for what the documents are,
  e.g. "Verdienstabrechnungen", "Kontoauszüge", "Versicherungen", "Arbeitsverträge".
  No sender name unless needed to tell folders apart, no year, at most 3 words;
- the same name for groups that belong together;
- null when a group does not fit any folder and is too unlike the others for a new one.
"""

MAX_FOLDER_NAME = 60


@dataclass(frozen=True)
class FolderGroup:
    titles: list[str]
    sender: str | None
    doc_type: str | None
    count: int


def name_folders(
    model: str, *, parent: str, existing: list[str], groups: list[FolderGroup]
) -> list[str | None]:
    """Ask `model` for a subfolder name per group: an existing one, a new one, or None."""
    lines = []
    for number, group in enumerate(groups, start=1):
        about = ", ".join(
            part
            for part in (
                f"{group.count} document{'s' if group.count != 1 else ''}",
                f"sender {group.sender}" if group.sender else "",
                f"type {group.doc_type}" if group.doc_type else "",
            )
            if part
        )
        titles = "; ".join(f'"{t}"' for t in group.titles[:4])
        lines.append(f"{number}. {about}: {titles}")
    prompt = FOLDER_INSTRUCTIONS.format(
        parent=parent or "(top level)",
        existing=", ".join(f'"{name}"' for name in existing[:40]) or "none",
        groups="\n".join(lines),
    )
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": FOLDER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "format": {
            "type": "object",
            "properties": {
                "groups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"group": {"type": "integer"}, "folder": {"type": ["string", "null"]}},
                        "required": ["group", "folder"],
                    },
                }
            },
            "required": ["groups"],
        },
        "stream": False,
        "think": False,
        "keep_alive": "10m",
        "options": {
            "temperature": 0,
            "num_ctx": settings.AI_CONTEXT_TOKENS,
            "num_predict": MAX_ANSWER_TOKENS,
        },
    }
    data = _chat(payload)
    names: list[str | None] = [None] * len(groups)
    answers = data.get("groups")
    for item in answers if isinstance(answers, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("group"), int):
            continue
        index = item["group"] - 1
        name = clean(item.get("folder"), limit=MAX_FOLDER_NAME)
        if 0 <= index < len(groups) and name:
            names[index] = name.replace("/", "-").replace("\\", "-")
    return names
