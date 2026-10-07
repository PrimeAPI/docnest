"""Turning raw scanner output (page images and/or PDFs) into one PDF.

This is what scan-to-PDF software normally does on the scanning computer;
DocNest does it server-side so scanner devices can stay dumb. The resulting
PDF is the document's original: every later stage (sanitizing, OCR, Docling,
storage) treats it exactly like an uploaded PDF.

Per page image:
1. Decode (PNG, JPEG, TIFF incl. multi-page, PNM/PBM/PGM/PPM, BMP, GIF, WebP).
2. Apply the EXIF orientation.
3. Normalize the colour model: transparency is flattened onto white, palettes
   are expanded, 16-bit / float greyscale is reduced to 8 bit, exotic modes
   become RGB.
4. Optionally drop blank pages (empty duplex backsides).
5. Compress: JPEG files are embedded unchanged (no generation loss);
   black-and-white pages become CCITT G4; other pages become JPEG (quality 90)
   or, with `compression=lossless`, Flate (PNG).
6. Size the page from the image resolution, so A4 at 300 dpi becomes an A4 page.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import img2pdf
import pikepdf
from PIL import Image, ImageOps, ImageSequence

logger = logging.getLogger(__name__)

DEFAULT_DPI = 300
MIN_DPI = 50
MAX_DPI = 2400
JPEG_QUALITY = 90
COMPRESSIONS = ("auto", "lossless")

# Pillow format names we accept (Pillow's "PPM" covers PBM, PGM, PPM and PNM).
IMAGE_FORMATS = {"PNG", "JPEG", "MPO", "TIFF", "PPM", "BMP", "GIF", "WEBP"}

# Blank-page detection (opt-in): a page is blank when less than 0.01 % of its
# pixels is clearly darker than the paper. A single short line ("Seite 2") is
# about 0.02 %, dust specks and scanner noise stay below it, and bleed-through
# from the other side is not dark enough to count. The outer 2 % of each edge
# (feeder shadows) is ignored.
BLANK_INK_RATIO = 0.0001
BLANK_CONTRAST = 60
BLANK_MARGIN = 0.02
BLANK_MAX_SIDE = 1800  # analyse at roughly 150 dpi


class AssemblyFailed(Exception):
    """The parts cannot be turned into a PDF (permanent failure)."""


@dataclass(frozen=True)
class AssemblyOptions:
    dpi: int | None = None  # overrides the resolution stored in the images
    skip_blank_pages: bool = False
    compression: str = "auto"

    def to_json(self) -> dict[str, object]:
        return {"dpi": self.dpi, "skip_blank_pages": self.skip_blank_pages, "compression": self.compression}

    @classmethod
    def from_json(cls, data: dict[str, object]) -> AssemblyOptions:
        dpi = data.get("dpi")
        return cls(
            dpi=int(dpi) if isinstance(dpi, int) else None,
            skip_blank_pages=bool(data.get("skip_blank_pages")),
            compression=str(data.get("compression") or "auto"),
        )


@dataclass
class AssemblyResult:
    page_count: int
    skipped_blank: int


def assemble(
    parts: list[tuple[str, Callable[[], Path]]], dst: Path, options: AssemblyOptions
) -> AssemblyResult:
    """Build `dst` from `parts` in page order.

    Each part is (kind, materialize) with kind "pdf" or "image"; `materialize()`
    returns a local plaintext file. Image files are deleted as soon as they are
    converted, so only one raw page image occupies the work area at a time.
    """
    out = pikepdf.new()
    sources: list[pikepdf.Pdf] = []  # kept open until saved: appended pages reference them
    skipped = 0
    try:
        for kind, materialize in parts:
            path = materialize()
            if kind == "pdf":
                src = _open_pdf(path)
                sources.append(src)
                out.pages.extend(src.pages)
                continue
            try:
                for page in _image_pages(path):
                    if options.skip_blank_pages and is_blank(page):
                        skipped += 1
                        continue
                    single = pikepdf.open(io.BytesIO(_page_pdf(page, path, options)))
                    sources.append(single)
                    out.pages.extend(single.pages)
            finally:
                path.unlink(missing_ok=True)
        if len(out.pages) == 0 and skipped:
            raise AssemblyFailed("All scanned pages are blank")
        if len(out.pages) == 0:
            raise AssemblyFailed("The scan contains no pages")
        out.save(dst)
        return AssemblyResult(page_count=len(out.pages), skipped_blank=skipped)
    finally:
        out.close()
        for src in sources:
            src.close()


def _open_pdf(path: Path) -> pikepdf.Pdf:
    try:
        pdf = pikepdf.open(path)
    except pikepdf.PasswordError as exc:
        raise AssemblyFailed("Encrypted / password-protected PDFs are not supported") from exc
    except pikepdf.PdfError as exc:
        raise AssemblyFailed("A PDF part is not a valid PDF") from exc
    if pdf.is_encrypted:
        pdf.close()
        raise AssemblyFailed("Encrypted PDFs are not supported")
    return pdf


@dataclass
class _Page:
    image: Image.Image
    jpeg_passthrough: bool  # the source file is a single JPEG that can be embedded unchanged
    source_dpi: tuple[float, float] | None


def _image_pages(path: Path) -> Iterator[_Page]:
    try:
        with Image.open(path) as image:
            fmt = image.format
            if fmt not in IMAGE_FORMATS:
                raise AssemblyFailed(f"Unsupported image format {fmt}")
            frames = ImageSequence.Iterator(image) if fmt == "TIFF" else [image]
            for frame in frames:
                dpi = _dpi_of(frame)
                passthrough = fmt in {"JPEG", "MPO"} and frame.mode in {"L", "RGB", "CMYK"}
                if passthrough:
                    # img2pdf applies the EXIF orientation itself (as /Rotate).
                    yield _Page(frame, True, dpi)
                    continue
                normalized = normalize(ImageOps.exif_transpose(frame) or frame)
                yield _Page(normalized, False, dpi)
    except Image.DecompressionBombError as exc:
        raise AssemblyFailed("Image is too large") from exc
    except (OSError, SyntaxError, ValueError) as exc:  # Pillow's decode errors
        raise AssemblyFailed(f"Cannot decode image: {type(exc).__name__}") from exc


def _dpi_of(image: Image.Image) -> tuple[float, float] | None:
    dpi = image.info.get("dpi")
    if not dpi:
        return None
    try:
        x, y = float(dpi[0]), float(dpi[1])
    except (TypeError, ValueError, IndexError):
        return None
    if not (MIN_DPI <= x <= MAX_DPI and MIN_DPI <= y <= MAX_DPI):
        return None
    return x, y


def normalize(image: Image.Image) -> Image.Image:
    """Bring any decoded image into 1 (bilevel), L (grey) or RGB."""
    mode = image.mode
    if mode in {"1", "L", "RGB"}:
        image.load()
        return image
    if mode in {"I", "I;16", "I;16B", "I;16L", "I;16N"}:
        # 16-bit greyscale (e.g. `scanimage --depth 16`): keep the top 8 bits.
        return image.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    if mode == "F":
        return image.convert("L")
    if mode == "P":
        if "transparency" in image.info:
            return _flatten(image.convert("RGBA"))
        rgb = image.convert("RGB")
        return rgb.convert("L") if _is_grey(rgb) else rgb
    if mode in {"RGBA", "LA", "PA", "RGBa", "La"}:
        return _flatten(image.convert("RGBA"))
    return image.convert("RGB")


def _flatten(image: Image.Image) -> Image.Image:
    background = Image.new("RGB", image.size, (255, 255, 255))
    background.paste(image, mask=image.getchannel("A"))
    return background.convert("L") if _is_grey(background) else background


def _is_grey(image: Image.Image) -> bool:
    small = image.copy()
    small.thumbnail((256, 256))
    r, g, b = small.split()
    return r.tobytes() == g.tobytes() == b.tobytes()


def is_blank(page: _Page) -> bool:
    grey = page.image.convert("L")
    width, height = grey.size
    mx, my = int(width * BLANK_MARGIN), int(height * BLANK_MARGIN)
    grey = grey.crop((mx, my, width - mx, height - my))
    factor = max(grey.size) // BLANK_MAX_SIDE
    if factor > 1:
        grey = grey.reduce(factor)
    histogram = grey.histogram()
    total = sum(histogram)
    if total == 0:
        return True
    # Paper colour = median brightness; ink = pixels clearly darker than the paper.
    running, median = 0, 255
    for value, count in enumerate(histogram):
        running += count
        if running * 2 >= total:
            median = value
            break
    threshold = max(0, median - BLANK_CONTRAST)
    ink = sum(histogram[:threshold])
    return ink / total < BLANK_INK_RATIO


def _page_pdf(page: _Page, path: Path, options: AssemblyOptions) -> bytes:
    if options.dpi is not None:
        dpi: tuple[float, float] = (options.dpi, options.dpi)
    else:
        dpi = page.source_dpi or (DEFAULT_DPI, DEFAULT_DPI)
    layout = img2pdf.get_fixed_dpi_layout_fun(dpi)
    if page.jpeg_passthrough and _is_single_frame_file(path):
        data = path.read_bytes()
    else:
        data = _encode(page.image, options.compression)
    try:
        return img2pdf.convert(data, layout_fun=layout)
    except Exception as exc:  # img2pdf raises a variety of errors for odd inputs
        raise AssemblyFailed(f"Cannot convert page: {type(exc).__name__}") from exc


def _is_single_frame_file(path: Path) -> bool:
    with Image.open(path) as image:
        return getattr(image, "n_frames", 1) == 1


def _encode(image: Image.Image, compression: str) -> bytes:
    buf = io.BytesIO()
    if image.mode == "1":
        image.save(buf, format="TIFF", compression="group4")
    elif compression == "lossless":
        image.save(buf, format="PNG", optimize=False, compress_level=6)
    else:
        image.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()
