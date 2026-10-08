"""Scan enhancement: turning a raw scan into a clean, upright, tightly cropped PDF.

Runs after validation and before OCR/Docling. The original is never changed;
this module writes a separate *enhanced* PDF that every later stage reads.

Only pages that consist of exactly one raster image covering the page (what a
scanner or image-to-PDF produces) are touched. Pages with text, vector
graphics, inline images, masks or unusual transforms are copied unchanged, so
born-digital PDFs and already OCRed scans pass through as they are.

Per scanned page, each step can be switched off in the settings:

1. **Orientation** (90/180/270°) — Tesseract OSD; applied only above a
   confidence threshold. A page that only needs this is rotated losslessly via
   the PDF `/Rotate` entry.
2. **Crop to the paper** — removes scanner background beyond the paper edge
   (feeders scan a fixed length; the overshoot shows the backing plate) and a
   paper-edge shadow line followed only by empty background. Bands are only
   cut when they are uniform, so coloured letterhead bands survive. This runs
   before deskewing, while the backing is still aligned with the image edges.
3. **Deskew** (small angles) — jdeskew (Adaptive Radial Projection on the
   Fourier magnitude spectrum, ICIP 2022), applied between a minimum and a
   maximum angle.
4. **Gentle cleanup** — background flattening (yellowed/grey paper and uneven
   lighting become white), a mild contrast stretch, and removal of isolated
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

_TEXT_OPERATORS = {"BT", "ET", "Tj", "TJ", "'", '"', "Tf", "Td", "TD", "Tm", "T*"}
_PAINT_OPERATORS = {"f", "F", "f*", "S", "s", "B", "B*", "b", "b*", "sh", "BI", "ID", "EI", "d0", "d1"}
_ALLOWED_OPERATORS = {"q", "Q", "cm", "Do", "re", "W", "W*", "n", "gs", "w", "i", "ri", "J", "j", "M"}
_MIN_COVERAGE = 0.9  # the image must cover this share of the page
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
    """Where the image is drawn, if the content is just "draw this image over the page"."""
    try:
        instructions = pikepdf.parse_content_stream(page)
    except pikepdf.PdfError:
        return None
    ctm = pikepdf.Matrix()
    stack: list[pikepdf.Matrix] = []
    placed: pikepdf.Matrix | None = None
    for instruction in instructions:
        if isinstance(instruction, pikepdf.ContentStreamInlineImage):
            return None
        operands, op = instruction.operands, str(instruction.operator)
        if op in _TEXT_OPERATORS or op in _PAINT_OPERATORS:
            return None
        if op not in _ALLOWED_OPERATORS:
            return None
        if op == "q":
            stack.append(ctm)
        elif op == "Q":
            ctm = stack.pop() if stack else pikepdf.Matrix()
        elif op == "cm":
            ctm = pikepdf.Matrix(*[float(v) for v in operands]) @ ctm
        elif op == "Do":
            if placed is not None or str(operands[0]) != name:
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
    if overlap_w * overlap_h < _MIN_COVERAGE * page_w * page_h:
        return None
    return x0, y0, x1, y1


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
    lightest = out if out.ndim == 2 else out.min(axis=2)
    out[lightest >= clip] = 255
    return out.astype(np.uint8)


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


def is_blank(image: Image.Image, threshold_percent: float) -> bool:
    return assemble.is_blank_image(image, ink_ratio=threshold_percent / 100)


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

    # Crop before deskewing: the scanner's overshoot is aligned with the scanner, not with a crooked
    # sheet; after rotating, the backing band would be tilted and no longer recognisable.
    if options.crop:
        box = paper_bounds(array, dpi, options.crop_max_fraction)
        if box:
            left, top, right, bottom = box
            array = array[top:bottom, left:right]
            report.cropped = True
            pixels_changed = True

    if options.deskew:
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

    if options.remove_blank and is_blank(result, options.blank_threshold):
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
