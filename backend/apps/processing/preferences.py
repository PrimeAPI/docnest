from __future__ import annotations

import base64
from typing import Literal, cast
from urllib.parse import urlsplit

from django.conf import settings

from apps.crypto.aead import decrypt_bytes, encrypt_bytes
from apps.processing.enhance_settings import EnhanceSettings
from apps.processing.models import SystemState

DEFAULT_OCR_BACKEND_KEY = "default_ocr_backend"
DOCLING_FIELD_DETECTION_KEY = "docling_field_detection"
PROCESSING_CONCURRENCY_KEY = "processing_concurrency"
SCAN_ENHANCEMENT_KEY = "scan_enhancement"
AI_MODEL_KEY = "ai_model"
AI_MODEL_PULL_KEY = "ai_model_pull"
OLLAMA_URL_KEY = "ollama_url"
OLLAMA_API_KEY_KEY = "ollama_api_key"
OLLAMA_API_KEY_AAD = b"ollama-api-key"
AI_PAGES_KEY = "ai_pages"
OCR_BACKENDS = {"ocrmypdf", "docling"}
DOCLING_FIELD_DETECTION_MODES = {"layout", "vlm", "hybrid"}
OcrBackend = Literal["ocrmypdf", "docling"]
DoclingFieldDetection = Literal["layout", "vlm", "hybrid"]
MIN_PROCESSING_CONCURRENCY = 1
MAX_PROCESSING_CONCURRENCY = 8


def get_default_ocr_backend() -> OcrBackend:
    value = (
        SystemState.objects.filter(key=DEFAULT_OCR_BACKEND_KEY).values_list("value", flat=True).first() or {}
    )
    backend = value.get("backend") if isinstance(value, dict) else None
    if backend in OCR_BACKENDS:
        return cast(OcrBackend, backend)
    return cast(OcrBackend, settings.OCR_BACKEND)


def set_default_ocr_backend(backend: OcrBackend) -> OcrBackend:
    if backend not in OCR_BACKENDS:
        raise ValueError(f"Unknown OCR backend: {backend}")
    SystemState.objects.update_or_create(
        key=DEFAULT_OCR_BACKEND_KEY,
        defaults={"value": {"backend": backend}},
    )
    return backend


def get_docling_field_detection() -> DoclingFieldDetection:
    value = (
        SystemState.objects.filter(key=DOCLING_FIELD_DETECTION_KEY).values_list("value", flat=True).first()
        or {}
    )
    mode = value.get("mode") if isinstance(value, dict) else None
    if mode in DOCLING_FIELD_DETECTION_MODES:
        return cast(DoclingFieldDetection, mode)
    return cast(DoclingFieldDetection, settings.DOCLING_FIELD_DETECTION)


def set_docling_field_detection(mode: DoclingFieldDetection) -> DoclingFieldDetection:
    if mode not in DOCLING_FIELD_DETECTION_MODES:
        raise ValueError(f"Unknown Docling field detection mode: {mode}")
    SystemState.objects.update_or_create(
        key=DOCLING_FIELD_DETECTION_KEY,
        defaults={"value": {"mode": mode}},
    )
    return mode


def get_processing_concurrency() -> int:
    value = (
        SystemState.objects.filter(key=PROCESSING_CONCURRENCY_KEY).values_list("value", flat=True).first()
        or {}
    )
    concurrency = value.get("concurrency") if isinstance(value, dict) else None
    if (
        isinstance(concurrency, int)
        and MIN_PROCESSING_CONCURRENCY <= concurrency <= MAX_PROCESSING_CONCURRENCY
    ):
        return concurrency
    return max(
        MIN_PROCESSING_CONCURRENCY,
        min(MAX_PROCESSING_CONCURRENCY, settings.PROCESSING_CONCURRENCY),
    )


def set_processing_concurrency(concurrency: int) -> int:
    if not MIN_PROCESSING_CONCURRENCY <= concurrency <= MAX_PROCESSING_CONCURRENCY:
        message = f"Processing concurrency must be between {MIN_PROCESSING_CONCURRENCY} and "
        raise ValueError(message + str(MAX_PROCESSING_CONCURRENCY))
    SystemState.objects.update_or_create(
        key=PROCESSING_CONCURRENCY_KEY,
        defaults={"value": {"concurrency": concurrency}},
    )
    return concurrency


def get_enhance_settings() -> EnhanceSettings:
    value = SystemState.objects.filter(key=SCAN_ENHANCEMENT_KEY).values_list("value", flat=True).first()
    return EnhanceSettings.from_json(value or {})


def set_enhance_settings(data: dict[str, object]) -> EnhanceSettings:
    """Merge `data` into the current settings (invalid values keep the current value)."""
    merged = EnhanceSettings.from_json(data, base=get_enhance_settings())
    SystemState.objects.update_or_create(key=SCAN_ENHANCEMENT_KEY, defaults={"value": merged.to_json()})
    return merged


def get_ai_model() -> str:
    """The Ollama model used for AI analysis; "" means AI analysis is off."""
    value = SystemState.objects.filter(key=AI_MODEL_KEY).values_list("value", flat=True).first()
    if isinstance(value, dict) and isinstance(value.get("model"), str):
        return value["model"]
    return str(settings.AI_MODEL or "")


def set_ai_model(model: str) -> str:
    model = model.strip()
    if len(model) > 200 or any(c.isspace() for c in model):
        raise ValueError("Invalid model name")
    SystemState.objects.update_or_create(key=AI_MODEL_KEY, defaults={"value": {"model": model}})
    return model


def get_ollama_url() -> str:
    value = SystemState.objects.filter(key=OLLAMA_URL_KEY).values_list("value", flat=True).first()
    if isinstance(value, dict) and isinstance(value.get("url"), str):
        return value["url"]
    return str(settings.OLLAMA_URL or "")


def get_ollama_api_key() -> str:
    value = SystemState.objects.filter(key=OLLAMA_API_KEY_KEY).values_list("value", flat=True).first()
    if isinstance(value, dict) and "api_key_enc" in value:
        encrypted = value["api_key_enc"]
        return (
            decrypt_bytes(base64.b64decode(encrypted), aad=OLLAMA_API_KEY_AAD).decode() if encrypted else ""
        )
    return str(settings.OLLAMA_API_KEY or "").strip()


def validate_ollama_api_key(api_key: str) -> str:
    api_key = api_key.strip()
    if len(api_key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in api_key):
        raise ValueError("Enter an API key without spaces or control characters")
    return api_key


def set_ollama_api_key(api_key: str) -> None:
    api_key = validate_ollama_api_key(api_key)
    encrypted = (
        base64.b64encode(encrypt_bytes(api_key.encode(), aad=OLLAMA_API_KEY_AAD)).decode() if api_key else ""
    )
    SystemState.objects.update_or_create(
        key=OLLAMA_API_KEY_KEY, defaults={"value": {"api_key_enc": encrypted}}
    )


def validate_ollama_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        port = parts.port
        valid = (
            parts.scheme in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and not parts.query
            and not parts.fragment
            and (port is None or port > 0)
            and not any(c.isspace() or ord(c) < 32 for c in url)
            and "\\" not in url
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Enter an HTTP or HTTPS Ollama URL without credentials, query or fragment")
    return url


def set_ollama_url(url: str) -> str:
    url = validate_ollama_url(url)
    SystemState.objects.update_or_create(key=OLLAMA_URL_KEY, defaults={"value": {"url": url}})
    return url


def get_ai_pages() -> int:
    value = SystemState.objects.filter(key=AI_PAGES_KEY).values_list("value", flat=True).first()
    pages = value.get("pages") if isinstance(value, dict) else None
    if isinstance(pages, int) and not isinstance(pages, bool) and 1 <= pages <= settings.MAX_PAGES:
        return pages
    return max(1, min(settings.MAX_PAGES, settings.AI_PAGES))


def set_ai_pages(pages: int) -> int:
    if isinstance(pages, bool) or not 1 <= pages <= settings.MAX_PAGES:
        raise ValueError(f"AI page limit must be between 1 and {settings.MAX_PAGES}")
    SystemState.objects.update_or_create(key=AI_PAGES_KEY, defaults={"value": {"pages": pages}})
    return pages
