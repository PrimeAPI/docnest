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

MAX_TEXT_CHARS = 12000
LIST_TIMEOUT = 10
# Shown in Settings as a starting point; any Ollama model works.
MAX_ANSWER_TOKENS = 2048  # the answer is ~100 tokens; this only stops a runaway generation
# Shown in Settings as a starting point; any Ollama model works. "-instruct" variants answer
# directly, while reasoning ("thinking") variants spend minutes deliberating first.
SUGGESTED_MODELS = [
    ("qwen3-vl:8b-instruct", "Recommended: reads German letters very well (≈6 GB download, 8 GB RAM)"),
    ("qwen3-vl:4b-instruct", "Smaller and faster, a little less accurate (≈3 GB, 5 GB RAM)"),
    ("qwen2.5vl:7b", "Proven alternative (≈6 GB, 8 GB RAM)"),
    ("gemma3:12b", "Larger and slower (≈8 GB, 12 GB RAM)"),
    ("qwen3-vl:32b-instruct", "Best results, for a big GPU or a lot of patience (≈21 GB)"),
]


class ModelUnavailable(Exception):
    """Ollama cannot be reached or the model is not installed: wait and try again later."""


class ModelFailed(Exception):
    """The model answered, but not usefully: fall back to rule-based detection."""


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


@dataclass
class ModelFields:
    sender: str | None = None
    recipient: str | None = None
    title: str | None = None
    document_date: date | None = None
    document_type: str | None = None  # a slug from the offered types
    model: str = ""


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
  Use the organisation's full name with its legal form if printed (e.g. "Bundesamt für Justiz",
  "Finanzamt Delmenhorst", "VRK Sachversicherung AG"). null if no sender is recognisable
  (e.g. handwritten notes).
recipient: the name of the addressee, or null.
title: a short title to file the document under, in the document's language: what the
  document is, plus its subject or period when that distinguishes it — for example
  "Erweitertes Führungszeugnis", "Verdienstabrechnung Juni 2026",
  "Fristverlängerung Umsatzsteuer-Voranmeldung". Do not include the sender, the recipient
  or a full date. At most 8 words.
document_date: the date the document was issued (letter date, invoice date, "Datum"),
  as YYYY-MM-DD. Not a birth date, due date or a period. null if there is none.
document_type: the slug of the type that fits best: {types}
"""

TEXT_NOTE = " and the OCR text below; the OCR text may contain recognition errors, so trust the images"


def _schema(type_slugs: list[str]) -> dict[str, Any]:
    nullable = {"type": ["string", "null"]}
    return {
        "type": "object",
        "properties": {
            "sender": nullable,
            "recipient": nullable,
            "title": nullable,
            "document_date": nullable,
            "document_type": {"type": ["string", "null"], "enum": [*type_slugs, None]}
            if type_slugs
            else nullable,
        },
        "required": ["sender", "recipient", "title", "document_date", "document_type"],
    }


def analyze(
    model: str,
    *,
    images: list[bytes],
    text: str,
    types: list[DocumentType],
) -> ModelFields:
    """Ask `model` about one document. No timeout beyond DOCNEST_AI_TIMEOUT_SECONDS: slow is fine."""
    listing = ", ".join(f'"{t.slug}" ({t.name})' for t in types) or "null"
    prompt = INSTRUCTIONS.format(text_note=TEXT_NOTE if text.strip() else "", types=listing)
    if text.strip():
        prompt += "\nOCR text:\n<<<\n" + text.strip()[:MAX_TEXT_CHARS] + "\n>>>\n"
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
    return _fields(data, model, {t.slug for t in types})


def _strip_fences(content: str) -> str:
    content = content.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.S)
    return match.group(1) if match else content


def _fields(data: dict[str, Any], model: str, slugs: set[str]) -> ModelFields:
    sender = clean(data.get("sender"))
    recipient = clean(data.get("recipient"))
    if sender and recipient and _same_party(sender, recipient):
        sender = None  # the model confused the addressee with the sender
    doc_type = clean(data.get("document_type"))
    return ModelFields(
        sender=sender,
        recipient=recipient,
        title=clean(data.get("title"), limit=120),
        document_date=_date(data.get("document_date")),
        document_type=doc_type if doc_type in slugs else None,
        model=model,
    )


def clean(value: object, *, limit: int = 150) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip(" -–—:;,\"'")
    if not cleaned or cleaned.casefold() in {"none", "null", "n/a", "unknown", "unbekannt", "-"}:
        return None
    return cleaned[:limit]


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
