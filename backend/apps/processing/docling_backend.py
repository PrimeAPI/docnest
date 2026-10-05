"""Docling-backed PDF understanding.

Imports are deliberately lazy: OCRmyPDF installations should not pay Docling's
model import/startup cost unless this backend is selected.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from django.conf import settings


class DoclingFailed(Exception):
    pass


@dataclass
class DoclingResult:
    markdown: str
    structured: dict


def _languages() -> list[str]:
    return [part.strip() for part in str(settings.OCR_LANGUAGES).replace(",", "+").split("+") if part.strip()]


def convert(src: Path) -> DoclingResult:
    """Convert a PDF without imposing a wall-clock timeout.

    Docling supplies layout, reading order, headings, and table structure. Its
    Tesseract CLI adapter keeps DocNest's multilingual ``deu+eng`` behavior.
    """
    try:
        from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
        from docling.datamodel.base_models import ConversionStatus, InputFormat
        from docling.datamodel.pipeline_options import (
            HeadingHierarchyOptions,
            OcrMode,
            PdfPipelineOptions,
            TableStructureOptions,
            TesseractCliOcrOptions,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:  # pragma: no cover - indicates a broken production image
        raise DoclingFailed("Docling is not installed") from exc

    artifacts = Path(settings.DOCLING_ARTIFACTS_PATH)
    options = PdfPipelineOptions(
        do_ocr=True,
        do_table_structure=True,
        table_structure_options=TableStructureOptions(do_cell_matching=True),
        ocr_options=TesseractCliOcrOptions(lang=_languages(), mode=OcrMode.FULL_PAGE),
        accelerator_options=AcceleratorOptions(
            num_threads=max(1, settings.DOCLING_THREADS),
            device=AcceleratorDevice(settings.DOCLING_DEVICE),
        ),
        heading_hierarchy_options=HeadingHierarchyOptions(enabled=True),
        generate_parsed_pages=True,
        artifacts_path=artifacts if artifacts.exists() else None,
        enable_remote_services=False,
    )
    converter = DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
    )
    try:
        result = converter.convert(src)
    except Exception as exc:
        raise DoclingFailed(f"Docling conversion failed: {exc}") from exc
    if result.status not in {ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS}:
        raise DoclingFailed(f"Docling conversion finished with status {result.status}")

    try:
        markdown = result.document.export_to_markdown()
        structured = result.document.export_to_dict()
    except Exception as exc:
        raise DoclingFailed(f"Docling export failed: {exc}") from exc
    return DoclingResult(markdown=markdown, structured=structured)
