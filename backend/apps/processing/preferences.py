from __future__ import annotations

from typing import Literal, cast

from django.conf import settings

from apps.processing.models import SystemState

DEFAULT_OCR_BACKEND_KEY = "default_ocr_backend"
OCR_BACKENDS = {"ocrmypdf", "docling"}
OcrBackend = Literal["ocrmypdf", "docling"]


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
