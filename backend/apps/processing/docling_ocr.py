"""Docling PDF pipeline with a second OCR pass for regions full-page OCR missed.

Full-page Tesseract keeps reading order and ignores decorative backgrounds, but
its page segmentation regularly drops short text on coloured bands (e.g. the
white-on-blue "Ihre Beitragsrechnung" subject band of an insurance letter).
Docling's layout model still finds those regions. Each text-like layout region
that received no OCR words is cropped and OCRed on its own; the result is only
kept when it reads like words, which rejects security microprint and noise.

This module imports Docling at import time; only import it lazily from the
Docling backend.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from typing import Any

from docling.datamodel.pipeline_options import OcrMode, TesseractCliOcrOptions
from docling.models.stages.ocr.tesseract_ocr_cli_model import TesseractOcrCliModel
from docling.models.stages.ocr.tesseract_utils import tesseract_box_to_bounding_rectangle
from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
from docling_core.types.doc import BoundingBox, CoordOrigin, DocItemLabel
from docling_core.types.doc.page import TextCell

logger = logging.getLogger(__name__)

RESCUE_LABELS = frozenset(
    {
        DocItemLabel.TITLE,
        DocItemLabel.SECTION_HEADER,
        DocItemLabel.TEXT,
        DocItemLabel.PAGE_HEADER,
        DocItemLabel.CAPTION,
    }
)
MAX_RESCUES_PER_PAGE = 12
MIN_WORD_CONFIDENCE = 60
MIN_REGION_CONFIDENCE = 75
MIN_REGION_AREA = 150.0  # pt², smaller regions are logos, bullets or stamps
PADDING = 3.0  # pt around a region; Tesseract needs a margin around glyphs

_WORD = re.compile(
    r"(?:[A-ZÄÖÜ]?[a-zäöüß]{1,24}|[A-ZÄÖÜ]{2,12})(?:-(?:[A-ZÄÖÜ]?[a-zäöüß]{1,24}|[A-ZÄÖÜ]{2,12}))*"
)
_NUMBER = re.compile(r"[\d][\d.,/:%-]*")


def reads_like_words(words: list[str]) -> bool:
    """Reject OCR of patterns: microprint, guilloches and barcodes yield letter soup."""
    tokens = [token.strip(".,;:!?()[]\"'«»„“”|*") for token in words]
    tokens = [token for token in tokens if token]
    if not tokens:
        return False
    plausible = sum(1 for token in tokens if _WORD.fullmatch(token) or _NUMBER.fullmatch(token))
    has_word = any(_WORD.fullmatch(token) and len(token) >= 3 for token in tokens)
    return has_word and plausible / len(tokens) >= 0.75


def _center(cell: Any, page_height: float) -> tuple[float, float]:
    box = cell.rect.to_bounding_box().to_top_left_origin(page_height)
    return (box.l + box.r) / 2, (box.t + box.b) / 2


class RegionRescueTesseractOcrModel(TesseractOcrCliModel):
    def post_process_cells(
        self, ocr_cells: list[Any], page: Any, conv_res: Any, priority: Any = None
    ) -> None:
        # Only full-page OCR leaves gaps worth re-reading; the other modes OCR per region already.
        full_page = self.options.mode == OcrMode.FULL_PAGE
        if full_page and self.enabled and page._backend is not None and page.predictions.layout is not None:
            try:
                ocr_cells = [*ocr_cells, *self._rescue(page, ocr_cells)]
            except Exception:
                # The rescue pass is an improvement, never a reason to fail a document.
                logger.warning("Docling OCR region rescue failed", exc_info=True)
        super().post_process_cells(ocr_cells, page, conv_res, priority)

    def _rescue(self, page: Any, ocr_cells: list[Any]) -> list[Any]:
        height = page.size.height
        centers = [_center(cell, height) for cell in ocr_cells]

        rescued: list[Any] = []
        attempted: list[Any] = []
        for cluster in page.predictions.layout.clusters:
            if cluster.label not in RESCUE_LABELS:
                continue
            box = cluster.bbox.to_top_left_origin(height)
            if box.area() < MIN_REGION_AREA:
                continue
            if any(box.l <= x <= box.r and box.t <= y <= box.b for x, y in centers):
                continue
            if any(box.intersection_over_self(seen) > 0.8 for seen in attempted):
                continue
            if len(attempted) >= MAX_RESCUES_PER_PAGE:
                break
            attempted.append(box)
            crop = BoundingBox(
                l=max(0.0, box.l - PADDING),
                t=max(0.0, box.t - PADDING),
                r=min(page.size.width, box.r + PADDING),
                b=min(height, box.b + PADDING),
                coord_origin=CoordOrigin.TOPLEFT,
            )
            cells = self._ocr_region(page, crop)
            # Layout clusters overlap (e.g. a title inside a text block); never read text twice.
            centers.extend(_center(cell, height) for cell in cells)
            rescued.extend(cells)
        return rescued

    def _ocr_region(self, page: Any, crop: Any) -> list[Any]:
        image = page._backend.get_page_image(scale=self.scale, cropbox=crop)
        fd, fname = tempfile.mkstemp(suffix=".png")
        try:
            with os.fdopen(fd, "wb") as handle:
                image.save(handle, format="PNG")
            try:
                rows = self._run_tesseract(fname, None)
            except subprocess.CalledProcessError:
                return []
        finally:
            os.remove(fname)

        words = [
            (str(row["text"]).strip(), float(row["conf"]), row)
            for _, row in rows.iterrows()
            if float(row["conf"]) >= MIN_WORD_CONFIDENCE and str(row["text"]).strip()
        ]
        if not words:
            return []
        if sum(conf for _, conf, _ in words) / len(words) < MIN_REGION_CONFIDENCE:
            return []
        if not reads_like_words([text for text, _, _ in words]):
            return []

        cells = []
        for text, conf, row in words:
            left, top = float(row["left"]), float(row["top"])
            rect = tesseract_box_to_bounding_rectangle(
                BoundingBox(
                    l=left,
                    t=top,
                    r=left + float(row["width"]),
                    b=top + float(row["height"]),
                    coord_origin=CoordOrigin.TOPLEFT,
                ),
                original_offset=crop,
                scale=self.scale,
                orientation=0,
                im_size=image.size,
            )
            cells.append(
                TextCell(index=0, text=text, orig=text, from_ocr=True, confidence=conf / 100.0, rect=rect)
            )
        return cells


class DocNestPdfPipeline(StandardPdfPipeline):
    def _make_ocr_model(self, art_path: Any) -> Any:
        options = self.pipeline_options.ocr_options
        if type(options) is not TesseractCliOcrOptions:
            return super()._make_ocr_model(art_path)
        return RegionRescueTesseractOcrModel(
            enabled=self.pipeline_options.do_ocr,
            artifacts_path=art_path,
            options=options,
            accelerator_options=self.pipeline_options.accelerator_options,
        )
