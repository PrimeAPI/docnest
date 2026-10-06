"""Docling-backed PDF understanding.

Imports are deliberately lazy: OCRmyPDF installations should not pay Docling's
model import/startup cost unless this backend is selected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from django.conf import settings


class DoclingFailed(Exception):
    pass


@dataclass
class DoclingResult:
    markdown: str
    structured: dict
    layout: dict = field(default_factory=dict)


@dataclass
class DoclingFieldResult:
    sender: str | None = None
    title: str | None = None


def _languages() -> list[str]:
    return [part.strip() for part in str(settings.OCR_LANGUAGES).replace(",", "+").split("+") if part.strip()]


def _layout_pages(pages: list[Any], limit: int = 2) -> dict:
    """Keep a compact, encrypted copy of line geometry for field detection."""
    output: list[dict[str, Any]] = []
    for page in pages[:limit]:
        parsed = page.parsed_page
        if parsed is None:
            continue
        width = float(page.size.width) if page.size else float(parsed.dimension.width)
        height = float(page.size.height) if page.size else float(parsed.dimension.height)
        lines = _group_line_cells(parsed.textline_cells, height)
        output.append({"page_no": page.page_no, "width": width, "height": height, "lines": lines})
    return {"pages": output}


def _group_line_cells(cells: list[Any], page_height: float) -> list[dict[str, Any]]:
    """Docling OCR may expose one ``textline_cell`` per word; rebuild rows."""
    measured = []
    for cell in cells:
        text = " ".join(cell.text.split()).strip()
        if not text:
            continue
        rect = cell.rect
        xs = [rect.r_x0, rect.r_x1, rect.r_x2, rect.r_x3]
        ys = [rect.r_y0, rect.r_y1, rect.r_y2, rect.r_y3]
        origin = getattr(rect.coord_origin, "value", str(rect.coord_origin))
        measured.append(
            {
                "cell": cell,
                "text": text,
                "x0": min(xs),
                "x1": max(xs),
                "y0": min(ys),
                "y1": max(ys),
                "center": (min(ys) + max(ys)) / 2,
                "top_left": origin == "TOPLEFT",
            }
        )
    measured.sort(key=lambda item: (item["center"], item["x0"]))

    rows: list[list[dict[str, Any]]] = []
    for item in measured:
        if rows:
            center = sum(part["center"] for part in rows[-1]) / len(rows[-1])
            tolerance = max(2.5, (item["y1"] - item["y0"]) * 0.6)
            if abs(item["center"] - center) <= tolerance:
                rows[-1].append(item)
                continue
        rows.append([item])

    output = []
    for row in rows:
        row.sort(key=lambda item: item["x0"])
        top_left = bool(row[0]["top_left"])
        raw_y0 = min(item["y0"] for item in row)
        raw_y1 = max(item["y1"] for item in row)
        y0, y1 = (page_height - raw_y1, page_height - raw_y0) if top_left else (raw_y0, raw_y1)
        output.append(
            {
                "text": " ".join(item["text"] for item in row),
                "x0": min(item["x0"] for item in row),
                "y0": y0,
                "x1": max(item["x1"] for item in row),
                "y1": y1,
                "confidence": round(sum(float(item["cell"].confidence) for item in row) / len(row), 4),
                "from_ocr": any(bool(item["cell"].from_ocr) for item in row),
            }
        )
    return output


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
    return DoclingResult(markdown=markdown, structured=structured, layout=_layout_pages(result.pages))


def extract_fields(src: Path) -> DoclingFieldResult:
    """Extract sender and title from page one using Docling's local VLM.

    No document timeout is set. The caller intentionally treats failures as a
    signal to use deterministic layout detection instead.
    """
    artifacts = Path(settings.DOCLING_ARTIFACTS_PATH)
    try:
        extractor, template = _field_extractor(
            str(artifacts) if artifacts.exists() else "",
            max(1, settings.DOCLING_THREADS),
            settings.DOCLING_DEVICE,
        )
        result = extractor.extract(src, template=template, raises_on_error=False, page_range=(1, 1))
    except DoclingFailed:
        raise
    except Exception as exc:
        raise DoclingFailed(f"Docling VLM extraction failed: {exc}") from exc

    for page in result.pages:
        data = page.extracted_data or {}
        sender = _clean_field(data.get("sender"))
        title = _clean_field(data.get("title"))
        if sender or title:
            return DoclingFieldResult(sender=sender, title=title)
    raise DoclingFailed("Docling VLM extraction returned no fields")


@lru_cache(maxsize=4)
def _field_extractor(artifacts_path: str, threads: int, device: str) -> tuple[Any, type[Any]]:
    """Build the heavyweight extractor once per worker/configuration."""
    try:
        from docling.backend.docling_parse_v4_backend import ThreadedDoclingParseDocumentBackend
        from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import VlmExtractionPipelineOptions
        from docling.document_extractor import DocumentExtractor, ExtractionFormatOption
        from docling.pipeline.extraction_vlm_pipeline import ExtractionVlmPipeline
        from pydantic import BaseModel, Field
    except ImportError as exc:  # pragma: no cover - indicates a broken production image
        raise DoclingFailed("Docling VLM extraction is not installed") from exc

    class Fields(BaseModel):
        sender: str | None = Field(
            default=None,
            description="The organization or person that issued and sent the document, not its recipient",
        )
        title: str | None = Field(
            default=None,
            description="The concise subject or heading of the document, without sender, recipient, or date",
        )

    options = VlmExtractionPipelineOptions(
        document_timeout=None,
        artifacts_path=Path(artifacts_path) if artifacts_path else None,
        accelerator_options=AcceleratorOptions(
            num_threads=threads,
            device=AcceleratorDevice(device),
        ),
        enable_remote_services=False,
    )
    return DocumentExtractor(
        allowed_formats=[InputFormat.PDF],
        extraction_format_options={
            InputFormat.PDF: ExtractionFormatOption(
                pipeline_cls=ExtractionVlmPipeline,
                backend=ThreadedDoclingParseDocumentBackend,
                pipeline_options=options,
            )
        },
    ), Fields


def _clean_field(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip(" -–—:;,.")
    if not cleaned or cleaned.lower() in {"none", "null", "n/a", "unknown"}:
        return None
    return cleaned[:200]
