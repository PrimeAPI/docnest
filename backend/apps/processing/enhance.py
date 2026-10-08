"""Scan enhancement: turning a raw scan into a clean, upright, tightly cropped PDF.

Runs after validation and before OCR/Docling. The original is never changed;
this module writes a separate *enhanced* PDF that every later stage reads.

Only pages that consist of exactly one raster image covering the page (what a
scanner or image-to-PDF produces) are touched. A hidden OCR text layer (drawn
invisibly or underneath the image, as scanner apps and OCRmyPDF do) is allowed;
it is dropped from rewritten pages, and the OCR stage recognises them again.
Pages with visible text, vector graphics, inline images, masks or unusual
transforms are copied unchanged, so born-digital PDFs pass through as they are.

Per scanned page, each step can be switched off in the settings:

1. **Orientation** (90/180/270°) — Tesseract OSD; applied only above a
   confidence threshold. A page that only needs this is rotated losslessly via
   the PDF `/Rotate` entry.
2. **Cut out the sheet** — when the sheet lies on a visibly darker, neutral
   scanner backing (crooked, smaller than the scan area, or both), it is found
   as a shape: the backing connected to the image edge plus pure padding
   beyond the scan area is outside, the rest's convex hull is the paper. Its
   edges give the angle (up to 30°), refined by the text on it; the page is
   rotated once, cropped to the sheet, and everything outside the sheet
   (wedges, edge shadow, missing corners) is painted in the paper colour.
3. Otherwise **crop + deskew**: uniform backing bands beyond the paper edge
   and a paper-edge shadow line followed only by empty background are cut
   (coloured letterhead bands survive), then jdeskew (Adaptive Radial
   Projection on the Fourier magnitude spectrum, ICIP 2022) straightens the
   text between a minimum and a maximum angle.
4. **Gentle cleanup** — background flattening (yellowed/grey paper, uneven
   lighting and faint show-through become white), a mild contrast stretch, and removal of isolated
   specks. Bilevel pages only get speck removal.
5. **Blank pages** — dropped with the same ink measure the assembly uses,
   evaluated after cropping (so feeder shadows don't count as ink). If every
   page is blank, nothing is dropped.
"""

from __future__ import annotations

import io
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import img2pdf
import numpy as np
import pikepdf
from PIL import Image

from apps.processing import assemble
from apps.processing.enhance_settings import EnhanceSettings, Strength

logger = logging.getLogger(__name__)

_TEXT_SHOW_OPERATORS = {"Tj", "TJ", "'", '"'}
_TEXT_STATE_OPERATORS = {"BT", "ET", "Tf", "Td", "TD", "Tm", "T*", "Tc", "Tw", "Tz", "TL", "Ts", "Tr"}
_PAINT_OPERATORS = {"f", "F", "f*", "S", "s", "B", "B*", "b", "b*", "sh", "BI", "ID", "EI", "d0", "d1"}
_ALLOWED_OPERATORS = {"q", "Q", "cm", "Do", "re", "W", "W*", "n", "gs", "w", "i", "ri", "J", "j", "M"}
_MIN_COVERAGE = 0.9  # the image must cover this share of the page
_HIDING_COVERAGE = 0.99  # ...and nearly all of it when a text layer is hidden underneath
_OSD_TIMEOUT = 60


@dataclass
class PageReport:
    rotated: int = 0  # degrees clockwise
    deskewed: float = 0.0  # degrees
    cropped: bool = False
    cleaned: bool = False
    blank: bool = False


@dataclass
class EnhanceResult:
    page_count: int
    changed: bool
    pages: list[PageReport | None] = field(default_factory=list)  # None = page not a scan
    kept_blank: bool = False  # every page looked blank, so none were removed

    def summary(self) -> dict[str, int]:
        scans = [p for p in self.pages if p is not None]
        return {
            "pages": len(self.pages),
            "scanned_pages": len(scans),
            "rotated": sum(1 for p in scans if p.rotated),
            "deskewed": sum(1 for p in scans if p.deskewed),
            "cropped": sum(1 for p in scans if p.cropped),
            "cleaned": sum(1 for p in scans if p.cleaned),
            "removed_blank": 0 if self.kept_blank else sum(1 for p in scans if p.blank),
        }

    def describe(self) -> str:
        s = self.summary()
        parts = []
        for key, label in (
            ("rotated", "rotated"),
            ("deskewed", "straightened"),
            ("cropped", "cropped"),
            ("cleaned", "cleaned up"),
        ):
            if s[key]:
                parts.append(f"{s[key]} {label}")
        if s["removed_blank"]:
            parts.append(f"{s['removed_blank']} blank page(s) removed")
        if self.kept_blank:
            parts.append("all pages look blank, none removed")
        if not parts:
            return "No changes needed" if s["scanned_pages"] else "No scanned pages to enhance"
        return "Pages " + ", ".join(parts)


# --- Finding the scan image of a page -------------------------------------------


@dataclass
class ScanImage:
    image: Image.Image  # upright as displayed (page /Rotate applied)
    dpi: tuple[float, float]
    lossless: bool
    page_rotate: int


def scan_image(page: pikepdf.Page) -> ScanImage | None:
    """The page's single full-page raster image, or None if the page is anything else."""
    try:
        images = dict(page.get_images(recursive=False))
    except (pikepdf.PdfError, AttributeError, KeyError, TypeError, ValueError):
        return None
    if len(images) != 1:
        return None
    name, xobj = next(iter(images.items()))
    if not isinstance(xobj, pikepdf.Stream):
        return None
    placement = _image_placement(page, str(name))
    if placement is None:
        return None
    if any(k in xobj for k in ("/SMask", "/Mask", "/ImageMask")) or "/Decode" in xobj:
        return None
    filters = xobj.get("/Filter")
    filter_names = [str(f) for f in filters] if isinstance(filters, pikepdf.Array) else [str(filters)]
    if any(f in {"/JBIG2Decode", "/JPXDecode"} for f in filter_names):
        return None
    try:
        image = pikepdf.PdfImage(xobj).as_pil_image()
    except Exception:  # unsupported encodings, broken streams
        return None
    image = assemble.normalize(image) if image.mode != "1" else image
    rotate = int(page.obj.get("/Rotate", 0)) % 360
    if rotate % 90:
        return None
    if rotate:
        image = image.rotate(-rotate, expand=True)
    box = page.mediabox
    width_pt, height_pt = float(box[2]) - float(box[0]), float(box[3]) - float(box[1])
    if rotate in (90, 270):
        width_pt, height_pt = height_pt, width_pt
    if width_pt <= 0 or height_pt <= 0:
        return None
    dpi = (image.width / (width_pt / 72), image.height / (height_pt / 72))
    if not all(assemble.MIN_DPI <= d <= assemble.MAX_DPI for d in dpi):
        return None
    lossless = not any(f == "/DCTDecode" for f in filter_names)
    return ScanImage(image=image, dpi=dpi, lossless=lossless, page_rotate=rotate)


def _image_placement(page: pikepdf.Page, name: str) -> tuple[float, float, float, float] | None:
    """Where the image is drawn, if the content is just "draw this image over the page".

    Text is tolerated only when nobody can see it: rendering mode 3 (invisible)
    or drawn before the image that then covers the page.
    """
    try:
        instructions = pikepdf.parse_content_stream(page)
    except pikepdf.PdfError:
        return None
    ctm = pikepdf.Matrix()
    render_mode = 0
    stack: list[tuple[pikepdf.Matrix, int]] = []
    placed: pikepdf.Matrix | None = None
    text_underneath = False
    for instruction in instructions:
        if isinstance(instruction, pikepdf.ContentStreamInlineImage):
            return None
        operands, op = instruction.operands, str(instruction.operator)
        if op in _TEXT_SHOW_OPERATORS:
            if render_mode == 3:
                continue
            if placed is not None:
                return None  # visible text on top of the image
            text_underneath = True
            continue
        if op in _TEXT_STATE_OPERATORS:
            if op == "Tr" and operands:
                render_mode = int(operands[0])
            continue
        if op in _PAINT_OPERATORS or op not in _ALLOWED_OPERATORS:
            return None
        if op == "q":
            stack.append((ctm, render_mode))
        elif op == "Q":
            ctm, render_mode = stack.pop() if stack else (pikepdf.Matrix(), 0)
        elif op == "cm":
            ctm = pikepdf.Matrix(*[float(v) for v in operands]) @ ctm
        elif op == "Do":
            if str(operands[0]) != name:
                if not _invisible_form(page, str(operands[0])):
                    return None
                continue
            if placed is not None:
                return None
            placed = ctm
    if placed is None or abs(placed.b) > 1e-6 or abs(placed.c) > 1e-6 or placed.a <= 0 or placed.d <= 0:
        return None
    box = page.mediabox
    page_w, page_h = float(box[2]) - float(box[0]), float(box[3]) - float(box[1])
    x0, y0 = placed.e - float(box[0]), placed.f - float(box[1])
    x1, y1 = x0 + placed.a, y0 + placed.d
    overlap_w = max(0.0, min(x1, page_w) - max(x0, 0.0))
    overlap_h = max(0.0, min(y1, page_h) - max(y0, 0.0))
    needed = _HIDING_COVERAGE if text_underneath else _MIN_COVERAGE
    if overlap_w * overlap_h < needed * page_w * page_h:
        return None
    return x0, y0, x1, y1


def _invisible_form(page: pikepdf.Page, name: str) -> bool:
    """A form XObject that only holds invisible text (OCRmyPDF's text layer)."""
    try:
        form = page.obj.Resources.XObject[name]
        if form.get("/Subtype") != "/Form":
            return False
        instructions = pikepdf.parse_content_stream(form)
    except (pikepdf.PdfError, AttributeError, KeyError, TypeError, ValueError):
        return False
    render_mode = 0
    stack: list[int] = []
    for instruction in instructions:
        if isinstance(instruction, pikepdf.ContentStreamInlineImage):
            return False
        operands, op = instruction.operands, str(instruction.operator)
        if op in _TEXT_SHOW_OPERATORS:
            if render_mode != 3:
                return False
        elif op == "Tr" and operands:
            render_mode = int(operands[0])
        elif op == "q":
            stack.append(render_mode)
        elif op == "Q":
            render_mode = stack.pop() if stack else 0
        elif op not in _TEXT_STATE_OPERATORS and op not in _ALLOWED_OPERATORS - {"Do"}:
            return False
    return True


# --- Image steps ----------------------------------------------------------------


def detect_orientation(image: Image.Image, dpi: float, work: Path) -> tuple[int, float]:
    """Clockwise rotation (0/90/180/270) that makes the page upright, and Tesseract's confidence."""
    grey = image.convert("L")
    if dpi > 300:
        factor = 300 / dpi
        grey = grey.resize((max(1, int(grey.width * factor)), max(1, int(grey.height * factor))))
        dpi = 300
    path = work / "osd.png"
    grey.save(path)
    try:
        proc = subprocess.run(
            ["tesseract", str(path), "stdout", "--psm", "0", "-l", "osd", "--dpi", str(int(dpi))],
            capture_output=True,
            text=True,
            timeout=_OSD_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 0, 0.0
    finally:
        path.unlink(missing_ok=True)
    rotate = re.search(r"Rotate:\s*(\d+)", proc.stdout)
    confidence = re.search(r"Orientation confidence:\s*([\d.]+)", proc.stdout)
    if proc.returncode != 0 or not rotate or not confidence:
        return 0, 0.0  # usually "too few characters"
    degrees = int(rotate.group(1)) % 360
    return (degrees if degrees in (90, 180, 270) else 0), float(confidence.group(1))


def estimate_skew(grey: np.ndarray, dpi: float, max_angle: float) -> float:
    """Skew in degrees; rotating the image counter-clockwise by this value straightens it."""
    from jdeskew.estimator import get_angle

    factor = min(1.0, 150 / dpi)
    small = grey
    if factor < 1.0:
        small = cv2.resize(grey, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
    try:
        angle = float(get_angle(small, angle_max=max_angle))
    except Exception:  # degenerate images (e.g. completely empty)
        return 0.0
    if not np.isfinite(angle) or abs(angle) > max_angle:
        return 0.0
    return angle


def paper_level(grey: np.ndarray) -> float:
    """Brightness of the paper: a high percentile of the page centre."""
    h, w = grey.shape[:2]
    centre = grey[h // 4 : h - h // 4, w // 4 : w - w // 4]
    return float(np.percentile(centre if centre.size else grey, 90))


def paper_colour(array: np.ndarray) -> tuple[float, ...]:
    """Paper colour per channel (for filling areas that rotation uncovers)."""
    if array.ndim == 2:
        return (paper_level(array),)
    return tuple(paper_level(array[:, :, c]) for c in range(array.shape[2]))


def rotate_by(array: np.ndarray, angle: float, fill: tuple[float, ...]) -> np.ndarray:
    """Rotate counter-clockwise by `angle` degrees, growing the canvas so no content is lost."""
    h, w = array.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_w, new_h = round(h * sin + w * cos), round(h * cos + w * sin)
    matrix[0, 2] += new_w / 2 - w / 2
    matrix[1, 2] += new_h / 2 - h / 2
    border = fill if array.ndim == 3 else fill[0]
    return cv2.warpAffine(
        array,
        matrix,
        (new_w, new_h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=border,
    )


def paper_bounds(array: np.ndarray, dpi: float, max_fraction: float) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) of the paper inside a scan, or None if there is nothing to cut."""
    h, w = array.shape[:2]
    factor = min(1.0, 75 / dpi)
    small = array
    if factor < 1:
        small = cv2.resize(array, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
    chroma = None
    if small.ndim == 3:
        chroma = small.max(axis=2).astype(np.int16) - small.min(axis=2)
        small = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    sdpi = dpi * factor
    paper = paper_level(small)
    sh, sw = small.shape[:2]
    left, top, right, bottom = _background_bounds(small, chroma, paper, sdpi)
    top, bottom, left, right = _edge_line_bounds(small, paper, sdpi, top, bottom, left, right)
    if (left, top, right, bottom) == (0, 0, sw, sh):
        return None
    # Never cut a page down to a fragment: the estimate must leave most of each dimension.
    if (right - left) < sw * (1 - max_fraction) or (bottom - top) < sh * (1 - max_fraction):
        return None
    scale_x, scale_y = w / sw, h / sh
    box = (
        round(left * scale_x),
        round(top * scale_y),
        round(right * scale_x),
        round(bottom * scale_y),
    )
    if box[2] - box[0] < 16 or box[3] - box[1] < 16:
        return None
    return box


def _background_bounds(
    small: np.ndarray, chroma: np.ndarray | None, paper: float, dpi: float
) -> tuple[int, int, int, int]:
    """Cut uniform, clearly-not-paper bands (scanner backing) off each edge.

    Backing plates are neutral grey/black and carry no detail; a letterhead
    band touching the edge is coloured or has text/logos on it, so it stays.
    """
    sh, sw = small.shape[:2]
    like_paper = (np.abs(small.astype(np.int16) - int(paper)) <= 25).astype(np.uint8)
    size = max(3, int(dpi * 10 / 25.4) | 1)  # ~10 mm: text and pictures become "paper"
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (size, size))
    closed = cv2.morphologyEx(like_paper, cv2.MORPH_CLOSE, kernel)
    row_share, col_share = closed.mean(axis=1), closed.mean(axis=0)
    if not (row_share > 0.5).any() or not (col_share > 0.5).any():
        return 0, 0, sw, sh  # no paper found
    # Keep every row/column with a bit of paper: a crooked sheet's corner must not be cut off.
    rows = np.flatnonzero(row_share > 0.05)
    cols = np.flatnonzero(col_share > 0.05)
    top, bottom = int(rows[0]), int(rows[-1]) + 1
    left, right = int(cols[0]), int(cols[-1]) + 1
    min_band = max(1, int(dpi * 1 / 25.4))  # ignore cuts below ~1 mm

    def backing(rows: slice, cols: slice) -> bool:
        band = small[rows, cols]
        if band.size == 0 or float(band.std()) >= 25 or abs(float(band.mean()) - paper) <= 20:
            return False
        detail = (np.abs(band.astype(np.int16) - int(np.median(band))) > 30).mean()
        if detail > 0.003:
            return False
        return chroma is None or float(np.median(chroma[rows, cols])) < 25

    everything = slice(None)
    if top < min_band or not backing(slice(0, top), everything):
        top = 0
    if sh - bottom < min_band or not backing(slice(bottom, sh), everything):
        bottom = sh
    if left < min_band or not backing(slice(top, bottom), slice(0, left)):
        left = 0
    if sw - right < min_band or not backing(slice(top, bottom), slice(right, sw)):
        right = sw
    return left, top, right, bottom


def _edge_line_bounds(
    small: np.ndarray, paper: float, dpi: float, top: int, bottom: int, left: int, right: int
) -> tuple[int, int, int, int]:
    """Cut at a paper-edge shadow: a full-width dark line with only empty background beyond it.

    Feeders with a light backing plate show no dark band, but the paper edge
    still casts a thin shadow line where the sheet ends.
    """
    region = small[top:bottom, left:right].astype(np.int16)
    h, w = region.shape[:2]
    if h < 8 or w < 8:
        return top, bottom, left, right
    reach = int(h * 0.3), int(w * 0.3)
    dark = region < paper - 25
    ink = region < paper - 60
    row_dark, col_dark = dark.mean(axis=1), dark.mean(axis=0)
    row_ink, col_ink = ink.mean(axis=1), ink.mean(axis=0)
    max_line = max(1, int(dpi * 1.5 / 25.4))  # a shadow is at most ~1.5 mm thick

    def cut_from_end(line: np.ndarray, empty: np.ndarray, limit: int) -> int | None:
        """Index after which everything is shadow + empty background, scanning from the end."""
        n = len(line)
        for i in range(n - 1, max(n - 1 - limit, -1), -1):
            if line[i] > 0.8:
                start = i
                while start - 1 >= 0 and line[start - 1] > 0.8 and i - start < max_line:
                    start -= 1
                if i - start >= max_line or not (empty[i + 1 :] < 0.002).all():
                    return None
                return start
            if empty[i] >= 0.002:
                return None
        return None

    cut = cut_from_end(row_dark, row_ink, reach[0])
    if cut is not None:
        bottom = top + cut
    cut = cut_from_end(row_dark[::-1], row_ink[::-1], reach[0])
    if cut is not None:
        top = top + (h - cut)
    cut = cut_from_end(col_dark, col_ink, reach[1])
    if cut is not None:
        right = left + cut
    cut = cut_from_end(col_dark[::-1], col_ink[::-1], reach[1])
    if cut is not None:
        left = left + (w - cut)
    return top, bottom, left, right


_SHEET_DPI = 50  # detection resolution
_NEUTRAL_CHROMA = 45  # backing plates are grey, though often slightly tinted
_SHEET_REFINE_ANGLE = 1.0  # degrees the text may deviate from the sheet's edges
_MAX_SHEET_ANGLE = 30.0  # degrees; a sheet's own edges are trusted further than text-based deskew


@dataclass
class Sheet:
    """The sheet of paper lying on a darker scanner backing."""

    angle: float  # rotating counter-clockwise by this straightens the sheet's edges
    mask: np.ndarray  # uint8, 1 = paper, at detection resolution


def find_sheet(array: np.ndarray, dpi: float) -> Sheet | None:
    """Find the paper as a shape on a visibly darker, neutral scanner backing.

    Works for crooked sheets and sheets smaller than the scan area, where cutting
    straight bands off the edges cannot help. Returns None when no backing is
    visible (white lid, sheet filling the scan, born-digital look).
    """
    factor = min(1.0, _SHEET_DPI / dpi)
    sdpi = dpi * factor
    small = cv2.resize(array, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
    rgb = small if small.ndim == 3 else cv2.cvtColor(small, cv2.COLOR_GRAY2RGB)
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.int16)
    chroma = rgb.max(axis=2).astype(np.int16) - rgb.min(axis=2)
    sh, sw = grey.shape
    if sh < 32 or sw < 32:
        return None
    # Scanners pad beyond their scan area with pure white or black: whole rows/columns along an edge.
    padding = _scan_padding(rgb, sdpi)
    if padding.mean() > 0.9:
        return None

    width = max(2, int(sdpi * 3 / 25.4))  # ~3 mm ring along the image edge
    ring = np.zeros(grey.shape, bool)
    ring[:width] = ring[-width:] = True
    ring[:, :width] = ring[:, -width:] = True
    ring &= padding == 0
    paper = float(np.percentile(grey[padding == 0], 95))
    values = grey[ring]
    darker = values < paper - 30
    if values.size < 20 or darker.mean() < 0.03:
        return None
    backing_level = float(np.median(values[darker]))
    if float(np.median(chroma[ring][darker])) >= _NEUTRAL_CHROMA:
        return None  # coloured, so a letterhead band or a photo rather than a backing plate
    split = (backing_level + paper) / 2

    # Backing: smooth, close to the backing level, connected to the image edge.
    backing = (
        (np.abs(grey - backing_level) < split - backing_level)
        & (_texture(grey) < 12)
        & (chroma < _NEUTRAL_CHROMA)
    ).astype(np.uint8) | padding
    # The scan area's own edge is a dark, noisy line next to the padding; it belongs outside as well.
    reach = max(3, int(sdpi * 3 / 25.4) | 1)
    near_padding = cv2.dilate(padding, np.ones((reach, reach), np.uint8))
    backing |= ((near_padding > 0) & (grey < split)).astype(np.uint8)
    backing = cv2.morphologyEx(backing, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    _, labels = cv2.connectedComponents(backing, connectivity=4)
    edge_labels = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    outside = np.isin(labels, edge_labels[edge_labels > 0])
    # The sheet's edge casts a shadow onto the backing: dark, but not smooth. Grow into it up to ~3 mm.
    shadow = grey < split
    for _ in range(reach // 2):
        outside |= cv2.dilate(outside.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool) & shadow
    if (outside & (padding == 0)).mean() < 0.005:
        return None  # hardly any backing visible: nothing to straighten or cut by

    candidate = cv2.morphologyEx((~outside).astype(np.uint8), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(candidate, connectivity=4)
    if count <= 1:
        return None
    biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    mask = (labels == biggest).astype(np.uint8)
    others = int(stats[1:, cv2.CC_STAT_AREA].sum()) - int(mask.sum())
    if others > 0.01 * sh * sw:
        return None  # several separate things on the backing: unclear which one is the sheet
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    outline = max(contours, key=cv2.contourArea)
    hull = cv2.convexHull(outline)
    hull_area = cv2.contourArea(hull)
    if hull_area < 0.15 * sh * sw or mask.sum() < 0.85 * hull_area:
        return None  # too small, or not a sheet-like (convex) shape

    # Angle from the sheet's own edges, ignoring stretches along the image border or padding.
    points = outline[:, 0, :]
    xs, ys = points[:, 0], points[:, 1]
    real = (xs > 1) & (xs < sw - 2) & (ys > 1) & (ys < sh - 2) & (near_padding[ys, xs] == 0)
    edge = points[real].astype(np.float32)
    if len(edge) < 0.1 * (sh + sw):
        return None
    angle = _edge_angle(edge)
    if angle is None:
        return None
    # Paper is convex: its hull gives straight edges where shading near the edge looked like backing.
    outline_mask = np.zeros_like(mask)
    cv2.drawContours(outline_mask, [hull], -1, 1, thickness=cv2.FILLED)
    return Sheet(angle=angle, mask=outline_mask)


def _texture(grey: np.ndarray) -> np.ndarray:
    """Local standard deviation over 3x3 pixels."""
    as_float = grey.astype(np.float32)
    mean = cv2.blur(as_float, (3, 3))
    return np.sqrt(np.maximum(cv2.blur(as_float * as_float, (3, 3)) - mean * mean, 0))


def _scan_padding(rgb: np.ndarray, dpi: float) -> np.ndarray:
    """Mask of the rows/columns at the image edges that lie outside the scan area.

    Scanners pad there with pure white or black. A stripe only counts when the
    scan next to it shows backing or the scan area's dark edge; a clean white
    page margin is pure white as well, but borders on paper.
    """
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    pure = (rgb.min(axis=2) >= 253) | (rgb.max(axis=2) <= 2)
    dark = (grey < 225) & (_texture(grey) < 12)  # backing or the scan edge, not text
    band = max(2, int(dpi * 3 / 25.4))  # ~3 mm of scan next to the stripe
    mask = np.zeros(pure.shape, np.uint8)

    def stripe(share: np.ndarray) -> int:
        full = share >= 0.98
        return len(full) if full.all() else int(np.argmin(full))

    rows, cols = pure.mean(axis=1), pure.mean(axis=0)
    h, w = pure.shape

    def outside_scan(next_line: np.ndarray, next_band: np.ndarray) -> bool:
        # The scan's last line is its dark edge or backing; a page margin ends in nearly white paper.
        return float(next_line.mean()) < 235 or float(next_band.mean()) >= 0.05

    top, bottom = stripe(rows), stripe(rows[::-1])
    left, right = stripe(cols), stripe(cols[::-1])
    if 0 < top < h and outside_scan(grey[top], dark[top : top + band]):
        mask[:top] = 1
    if 0 < bottom < h and outside_scan(grey[h - bottom - 1], dark[h - bottom - band : h - bottom]):
        mask[h - bottom :] = 1
    if 0 < left < w and outside_scan(grey[:, left], dark[:, left : left + band]):
        mask[:, :left] = 1
    if 0 < right < w and outside_scan(grey[:, w - right - 1], dark[:, w - right - band : w - right]):
        mask[:, w - right :] = 1
    return mask


def _edge_angle(points: np.ndarray) -> float | None:
    """Rotation that aligns the most edge points with straight horizontal/vertical lines (a tiny Hough)."""
    best_score, best_angle = 0, 0.0
    for angle in np.arange(-_MAX_SHEET_ANGLE, _MAX_SHEET_ANGLE + 1e-6, 0.1):
        t = np.radians(angle)
        score = 0
        for nx, ny in ((-np.sin(t), np.cos(t)), (np.cos(t), np.sin(t))):  # horizontal and vertical edges
            distance = np.round(points[:, 0] * nx + points[:, 1] * ny).astype(np.int64)
            counts = np.bincount(distance - distance.min())
            score += int(np.sort(counts)[-2:].sum())  # the two parallel sides
        if score > best_score:
            best_score, best_angle = score, float(angle)
    if best_score < 0.3 * len(points):
        return None  # edges are not straight lines (torn, folded, or not a sheet at all)
    return round(best_angle, 2)


def sheet_angle(array: np.ndarray, dpi: float, sheet: Sheet) -> float:
    """The sheet's edge angle, refined by the text on it (print is often a little off the paper edge)."""
    factor = min(1.0, 150 / dpi)
    small = cv2.resize(array, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
    straightened = cut_out_sheet(small, dpi * factor, sheet, sheet.angle)
    grey = straightened if straightened.ndim == 2 else cv2.cvtColor(straightened, cv2.COLOR_RGB2GRAY)
    residual = estimate_skew(grey, dpi * factor, _SHEET_REFINE_ANGLE)
    return round(sheet.angle + residual, 2)


def cut_out_sheet(array: np.ndarray, dpi: float, sheet: Sheet, angle: float) -> np.ndarray:
    """Rotate by `angle`, crop to the sheet and paint anything that is not paper in the paper colour.

    Corners that lay outside the scan area are filled rather than cut, so no content is lost.
    """
    h, w = array.shape[:2]
    mask = cv2.resize(sheet.mask * 255, (w, h), interpolation=cv2.INTER_LINEAR)
    inside = array[mask >= 128]
    fill = tuple(float(v) for v in np.percentile(inside.reshape(len(inside), -1), 90, axis=0))
    if angle:
        array = rotate_by(array, angle, fill)
        mask = rotate_by(mask, angle, (0.0,))
    paper = mask >= 128
    rows, cols = paper.sum(axis=1), paper.sum(axis=0)
    rows = np.flatnonzero(rows > 0.05 * np.median(rows[rows > 0]))
    cols = np.flatnonzero(cols > 0.05 * np.median(cols[cols > 0]))
    top, bottom = int(rows[0]), int(rows[-1]) + 1
    left, right = int(cols[0]), int(cols[-1]) + 1
    inset = max(1, round(dpi * 2 / 25.4))  # the sheet's edge casts a shadow: paint ~2 mm over
    out = array[top:bottom, left:right].copy()
    keep = cv2.erode(
        paper[top:bottom, left:right].astype(np.uint8),
        np.ones((2 * inset + 1,) * 2, np.uint8),
        borderType=cv2.BORDER_CONSTANT,
        borderValue=0,  # the outermost rows/columns are the paper edge itself
    )
    out[keep == 0] = fill if out.ndim == 3 else fill[0]
    return out


_KNEE = 25.0  # levels below the white clip that fade into white

_STRENGTH = {
    # blend of the flattened background, white clip level, max black point lift, speck size at 300 dpi
    "low": (0.5, 248, 30, 4),
    "medium": (0.8, 240, 50, 8),
    "high": (1.0, 230, 70, 14),
}


def flatten_background(array: np.ndarray, dpi: float, strength: Strength) -> np.ndarray:
    """Divide by the estimated paper illumination so the paper becomes white."""
    blend, clip, _, _ = _STRENGTH[strength]
    h, w = array.shape[:2]
    factor = min(1.0, 50 / dpi)
    small = cv2.resize(array, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
    size = max(3, int(dpi * factor * 25 / 25.4) | 1)  # ~25 mm: wider than text and most logos
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    background = cv2.dilate(small, kernel)
    background = cv2.GaussianBlur(background, (0, 0), size / 3)
    grey_small = small if small.ndim == 2 else cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    floor = paper_level(grey_small) * 0.7
    background = np.maximum(background.astype(np.float32), floor)
    background = cv2.resize(background, (w, h), interpolation=cv2.INTER_LINEAR)
    source = array.astype(np.float32)
    flat = np.clip(source * 255.0 / np.maximum(background, 1.0), 0, 255)
    out = source * (1 - blend) + flat * blend
    # Near-white becomes white with a soft knee: a hard threshold leaves show-through and paper
    # grain as a mottled pattern of white and almost-white pixels.
    lightest = out if out.ndim == 2 else out.min(axis=2)
    lift = np.clip((lightest - (clip - _KNEE)) / _KNEE, 0.0, 1.0)
    lift = lift * lift * (3 - 2 * lift)  # smoothstep
    if out.ndim == 3:
        lift = lift[:, :, None]
    out = out + (255.0 - out) * lift
    return np.clip(out, 0, 255).astype(np.uint8)


def stretch_contrast(array: np.ndarray, strength: Strength) -> np.ndarray:
    """Pull the darkest ink to black (gently), leaving white as white."""
    _, _, max_lift, _ = _STRENGTH[strength]
    grey = array if array.ndim == 2 else cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
    low = float(np.percentile(grey, 0.5))
    if low <= 2 or low > max_lift:
        return array  # already black enough, or no real ink to stretch
    lut = np.clip((np.arange(256, dtype=np.float32) - low) * 255.0 / (255.0 - low), 0, 255).astype(np.uint8)
    return cv2.LUT(array, lut)


def despeckle(array: np.ndarray, dpi: float, strength: Strength, paper: float) -> tuple[np.ndarray, int]:
    """Whiten tiny isolated dark specks (dust, toner); dots near text (i, ä, punctuation) stay."""
    _, _, _, speck = _STRENGTH[strength]
    grey = array if array.ndim == 2 else cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
    ink = (grey < paper - 80).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    if count <= 1:
        return array, 0
    max_area = max(1, round(speck * (dpi / 300) ** 2))
    areas = stats[:, cv2.CC_STAT_AREA]
    small = areas <= max_area
    small[0] = False
    if not small.any():
        return array, 0
    big_mask = (~small[labels] & (labels > 0)).astype(np.uint8)
    radius = max(1, int(dpi * 1.0 / 25.4))  # ~1 mm
    near_big = cv2.dilate(big_mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1,) * 2))
    touching = np.bincount(labels[near_big > 0].ravel().astype(np.int64), minlength=count) > 0
    remove = small & ~touching
    if not remove.any():
        return array, 0
    mask = remove[labels]
    out = array.copy()
    out[mask] = 255
    return out, int(remove.sum())


_BLANK_DPI = 100  # analysis resolution
_BLANK_EDGE_MM = 8.0  # fold marks, edge shadows, staple and clip marks live here
_PUNCH_HOLE_MM = (4.0, 8.5)  # diameter range of filing holes (ISO 838: 6 mm)
_PUNCH_REACH_MM = 25.0  # how far from an edge a hole may lie
_SPECK_MM2 = 0.1  # smaller ink blobs are dust, not a full stop
# Print, handwriting and stamps have a dark core; show-through, fold creases and the shadow
# of a crease stay grey even at full resolution.
_FAINT = 140  # a mark whose darkest pixel is within this much of the paper is not content
_CREASE_MM = (1.0, 5.0)  # thinner than this and longer than that: a fold line or crease
_CREASE_FAINT = 200  # creases are excused up to this darkness; printed rules are black


def is_blank(image: Image.Image, threshold_percent: float, dpi: float) -> bool:
    """True if the page holds no content: the back of a sheet, an empty separator page."""
    return content_share(image, dpi) < threshold_percent / 100


def content_share(image: Image.Image, dpi: float) -> float:
    """Share of the page (inside the edge band) covered by ink that counts as content.

    Ink is anything clearly darker than the paper. Not counted: a band along
    the edges, punch holes (round blobs of filing-hole size near an edge), dust
    specks, and faint marks — show-through from the other side and fold creases
    never get as dark as print, handwriting or a stamp.
    """
    full = np.array(image.convert("L"))
    grey = full
    factor = min(1.0, _BLANK_DPI / dpi)
    if factor < 1.0:
        grey = cv2.resize(full, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
    sdpi = dpi * factor
    h, w = grey.shape
    edge = int(sdpi * _BLANK_EDGE_MM / 25.4)
    if h <= 2 * edge or w <= 2 * edge:
        edge = 0
    paper = float(np.median(grey[edge : h - edge, edge : w - edge]))
    ink = (grey < paper - assemble.BLANK_CONTRAST).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    mm = 25.4 / sdpi
    reach = _PUNCH_REACH_MM / mm
    content = 0
    for i in range(1, count):
        x, y, bw, bh, area = (int(v) for v in stats[i])
        if area * mm * mm < _SPECK_MM2:
            continue
        if x >= w - edge or y >= h - edge or x + bw <= edge or y + bh <= edge:
            continue  # entirely within the edge band
        diameter = max(bw, bh) * mm
        near_edge = min(x, y, w - (x + bw), h - (y + bh)) < reach
        if (
            near_edge
            and _PUNCH_HOLE_MM[0] <= diameter <= _PUNCH_HOLE_MM[1]
            and 0.75 <= bw / max(bh, 1) <= 1.33
        ):
            # A hole is often only partly dark (a ring or crescent, the backing shows through):
            # judge the hull of the blob, not its ink.
            points = cv2.findNonZero((labels[y : y + bh, x : x + bw] == i).astype(np.uint8))
            if points is not None and cv2.contourArea(cv2.convexHull(points)) >= 0.5 * bw * bh:  # disc: 0.785
                continue
        # Darkness is judged at full resolution: thin pen strokes turn grey when scaled down.
        darkest = int(
            full[
                int(y / factor) : int((y + bh) / factor) + 1, int(x / factor) : int((x + bw) / factor) + 1
            ].min()
        )
        if darkest >= paper - _FAINT:
            continue  # show-through or a crease shadow
        thin, long = min(bw, bh) * mm, max(bw, bh) * mm
        if thin <= _CREASE_MM[0] and long >= _CREASE_MM[1] and darkest >= paper - _CREASE_FAINT:
            continue  # a fold line
        inner = labels[max(y, edge) : min(y + bh, h - edge), max(x, edge) : min(x + bw, w - edge)] == i
        content += int(inner.sum())
    return content / float((h - 2 * edge) * (w - 2 * edge))


def enhance_image(
    scan: ScanImage, options: EnhanceSettings, work: Path
) -> tuple[Image.Image, PageReport, bool]:
    """Apply the enabled steps. Returns (image, report, pixels_changed)."""
    report = PageReport()
    image = scan.image
    bilevel = image.mode == "1"
    dpi = (scan.dpi[0] + scan.dpi[1]) / 2
    pixels_changed = False

    if options.rotate:
        degrees, confidence = detect_orientation(image, dpi, work)
        if degrees and confidence >= options.rotate_min_confidence:
            report.rotated = degrees
            image = image.rotate(-degrees, expand=True)

    array = np.array(image.convert("L") if bilevel else image)
    grey = array if array.ndim == 2 else cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)

    # A sheet on a visible backing: its own edges give the angle and the outline to cut along.
    sheet = find_sheet(array, dpi) if options.crop and not bilevel else None
    if sheet is not None:
        angle = sheet_angle(array, dpi, sheet) if options.deskew else 0.0
        if abs(angle) < max(options.deskew_min_angle, 0.01):
            angle = 0.0
        array = cut_out_sheet(array, dpi, sheet, angle)
        report.cropped = True
        report.deskewed = angle
        pixels_changed = True

    # Otherwise crop before deskewing: the scanner's overshoot is aligned with the scanner, not with a
    # crooked sheet; after rotating, the backing band would be tilted and no longer recognisable.
    if options.crop and sheet is None:
        box = paper_bounds(array, dpi, options.crop_max_fraction)
        if box:
            left, top, right, bottom = box
            array = array[top:bottom, left:right]
            report.cropped = True
            pixels_changed = True

    if options.deskew and sheet is None:
        grey = array if array.ndim == 2 else cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
        angle = estimate_skew(grey, dpi, options.deskew_max_angle)
        if abs(angle) >= max(options.deskew_min_angle, 0.01):
            array = rotate_by(array, angle, paper_colour(array))
            report.deskewed = round(angle, 2)
            pixels_changed = True

    if options.cleanup:
        strength = options.cleanup_strength
        before = array
        if not bilevel and options.cleanup_background:
            array = flatten_background(array, dpi, strength)
        if not bilevel and options.cleanup_contrast:
            array = stretch_contrast(array, strength)
        if options.cleanup_despeckle:
            grey = array if array.ndim == 2 else cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
            array, _ = despeckle(array, dpi, strength, paper_level(grey))
        if array is not before and not np.array_equal(array, before):
            report.cleaned = True
            pixels_changed = True

    if bilevel:
        result = Image.fromarray(np.where(array >= 128, 255, 0).astype(np.uint8)).convert("1")
    else:
        result = Image.fromarray(array)

    if options.remove_blank and is_blank(result, options.blank_threshold, dpi):
        report.blank = True
    return result, report, pixels_changed


# --- Whole document ---------------------------------------------------------------


def enhance(src: Path, dst: Path, options: EnhanceSettings, work: Path) -> EnhanceResult:
    """Write the enhanced version of `src` to `dst` (only if anything changed)."""
    reports: list[PageReport | None] = []
    rendered: list[tuple[pikepdf.Page, bytes | None, int]] = []  # page, new single-page PDF, extra rotate
    with pikepdf.open(src) as pdf:
        for page in pdf.pages:
            scan = scan_image(page) if options.enabled else None
            if scan is None:
                reports.append(None)
                rendered.append((page, None, 0))
                continue
            image, report, pixels_changed = enhance_image(scan, options, work)
            reports.append(report)
            if pixels_changed:
                rendered.append((page, _page_pdf(image, scan), 0))
            else:
                # Orientation alone (or nothing): keep the original image bytes, adjust /Rotate.
                rendered.append((page, None, report.rotated))

        blank = [r is not None and r.blank for r in reports]
        kept_blank = all(blank) and bool(blank)
        drop = [b and not kept_blank for b in blank]
        changed = any(drop) or any(data is not None or rot for _, data, rot in rendered)
        result = EnhanceResult(
            page_count=len(reports) - sum(drop), changed=changed, pages=reports, kept_blank=kept_blank
        )
        if not changed:
            return result

        out = pikepdf.new()
        singles: list[pikepdf.Pdf] = []
        try:
            for (page, data, extra_rotate), dropped in zip(rendered, drop, strict=True):
                if dropped:
                    continue
                if data is None:
                    out.pages.append(page)
                    if extra_rotate:
                        new = out.pages[-1]
                        new.obj["/Rotate"] = (int(new.obj.get("/Rotate", 0)) + extra_rotate) % 360
                    continue
                single = pikepdf.open(io.BytesIO(data))
                singles.append(single)
                out.pages.extend(single.pages)
            out.save(dst)
        finally:
            out.close()
            for single in singles:
                single.close()
    return result


def _page_pdf(image: Image.Image, scan: ScanImage) -> bytes:
    """Encode one enhanced page like the assembly does (G4 / JPEG 90 / lossless)."""
    dpi = (scan.dpi[0] + scan.dpi[1]) / 2
    data = assemble.encode_image(image, "lossless" if scan.lossless else "auto")
    layout = img2pdf.get_fixed_dpi_layout_fun((dpi, dpi))
    return img2pdf.convert(data, layout_fun=layout)
