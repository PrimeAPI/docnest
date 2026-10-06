from __future__ import annotations

from typing import Literal, cast

from django.conf import settings

from apps.processing.models import SystemState

DEFAULT_OCR_BACKEND_KEY = "default_ocr_backend"
DOCLING_FIELD_DETECTION_KEY = "docling_field_detection"
PROCESSING_CONCURRENCY_KEY = "processing_concurrency"
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
