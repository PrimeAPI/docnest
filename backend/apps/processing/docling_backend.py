"""Docling-backed PDF understanding.

Imports are deliberately lazy: OCRmyPDF installations should not pay Docling's
model import/startup cost unless this backend is selected.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

from django.conf import settings

from apps.processing.docling_models import ModelDownloadFailed, ensure_models


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


_field_extractors = threading.local()

COLUMN_GAP = 18.0  # pt; wider than any justified word space at letter font sizes
MIN_SPECK_CONFIDENCE = 0.35
MIN_DIGITAL_CHARS = 20  # fewer characters per page: blank, a stamp, or a scan
SCAN_COVERAGE = 0.5  # an image covering half the page makes it a scan


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


def _split_columns(row: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split a visual row where a wide horizontal gap separates two columns.

    Letters put the sender block, the date and the recipient side by side; joined
    they read as "Landratsamt Musterkreis Musterstadt, den 12.03.2024" and hide the sender.
    """
    segments = [[row[0]]]
    for previous, item in pairwise(row):
        height = max(item["y1"] - item["y0"], previous["y1"] - previous["y0"], 1.0)
        if item["x0"] - previous["x1"] > max(COLUMN_GAP, height * 2.2):
            segments.append([item])
        else:
            segments[-1].append(item)
    return segments


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
        for segment in _split_columns(row):
            confidence = sum(float(item["cell"].confidence) for item in segment) / len(segment)
            text = " ".join(item["text"] for item in segment)
            if confidence < MIN_SPECK_CONFIDENCE and len(text) <= 4:
                continue  # scanner dust, stamps and page edges read as "ee" or "|"
            top_left = bool(segment[0]["top_left"])
            raw_y0 = min(item["y0"] for item in segment)
            raw_y1 = max(item["y1"] for item in segment)
            y0, y1 = (page_height - raw_y1, page_height - raw_y0) if top_left else (raw_y0, raw_y1)
            output.append(
                {
                    "text": text,
                    "x0": min(item["x0"] for item in segment),
                    "y0": y0,
                    "x1": max(item["x1"] for item in segment),
                    "y1": y1,
                    "confidence": round(confidence, 4),
                    "from_ocr": any(bool(item["cell"].from_ocr) for item in segment),
                }
            )
    return output


def has_digital_text(src: Path) -> bool:
    """True when every page has real text and no page-sized (scanned) image.

    Online invoices and statements qualify. Scans do not, including scans that
    carry a scanner's OCR text layer: DocNest's own OCR reads those consistently.
    Any doubt, including unreadable files, answers False and keeps full OCR.
    """
    try:
        import pypdfium2 as pdfium
        import pypdfium2.raw as pdfium_c
        from docling.utils.locks import pypdfium2_lock
    except ImportError:  # pragma: no cover - indicates a broken production image
        return False

    # pdfium is not thread-safe; share Docling's lock with concurrent jobs.
    with pypdfium2_lock:
        try:
            pdf = pdfium.PdfDocument(src)
        except pdfium.PdfiumError:
            return False
        try:
            if len(pdf) == 0:
                return False
            for page in pdf:
                try:
                    width, height = page.get_size()
                    textpage = page.get_textpage()
                    try:
                        if textpage.count_chars() < MIN_DIGITAL_CHARS:
                            return False
                    finally:
                        textpage.close()
                    for image in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]):
                        left, bottom, right, top = image.get_bounds()
                        if (right - left) * (top - bottom) >= SCAN_COVERAGE * width * height:
                            return False
                finally:
                    page.close()
            return True
        except pdfium.PdfiumError:
            return False
        finally:
            pdf.close()


def convert(src: Path) -> DoclingResult:
    """Convert a PDF without imposing a wall-clock timeout.

    Docling supplies layout, reading order, headings, and table structure. Its
    Tesseract CLI adapter keeps DocNest's multilingual ``deu+eng`` behavior;
    ``DocNestPdfPipeline`` re-reads layout regions full-page OCR left empty.
    """
    try:
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
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

        from apps.processing.docling_ocr import DocNestPdfPipeline
    except ImportError as exc:  # pragma: no cover - indicates a broken production image
        raise DoclingFailed("Docling is not installed") from exc

    try:
        artifacts = ensure_models()
    except ModelDownloadFailed as exc:
        raise DoclingFailed(str(exc)) from exc
    options = PdfPipelineOptions(
        do_ocr=True,
        do_table_structure=True,
        table_structure_options=TableStructureOptions(do_cell_matching=True),
        ocr_options=TesseractCliOcrOptions(
            lang=_languages(),
            # Born-digital PDFs already carry exact text; OCR only their images.
            mode=OcrMode.DEFAULT if has_digital_text(src) else OcrMode.FULL_PAGE,
        ),
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
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_cls=DocNestPdfPipeline,
                # docling-parse's own rasterizer blurs embedded scans noticeably;
                # pdfium renders them sharply, which markedly improves OCR of scans.
                backend=PyPdfiumDocumentBackend,
                pipeline_options=options,
            )
        },
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
    try:
        artifacts = ensure_models(include_vlm=True)
        extractor, template = _field_extractor(
            str(artifacts),
            max(1, settings.DOCLING_THREADS),
            settings.DOCLING_DEVICE,
        )
        result = extractor.extract(src, template=template, raises_on_error=False, page_range=(1, 1))
    except ModelDownloadFailed as exc:
        raise DoclingFailed(str(exc)) from exc
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


def _field_extractor(artifacts_path: str, threads: int, device: str) -> tuple[Any, type[Any]]:
    """Build one heavyweight extractor per job thread/configuration.

    Docling extractor instances are stateful, so concurrent jobs must not share
    one. Executor threads are long-lived, which still lets each thread reuse its
    initialized model on later documents.
    """
    key = (artifacts_path, threads, device)
    cache: dict[tuple[str, int, str], tuple[Any, type[Any]]] = getattr(_field_extractors, "cache", {})
    if key not in cache:
        cache[key] = _build_field_extractor(artifacts_path, threads, device)
        _field_extractors.cache = cache
    return cache[key]


def _build_field_extractor(artifacts_path: str, threads: int, device: str) -> tuple[Any, type[Any]]:
    try:
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
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
                backend=PyPdfiumDocumentBackend,
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
