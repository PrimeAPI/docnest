"""PDF validation, sanitizing, OCR, text extraction and thumbnails."""

from __future__ import annotations

import io
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pikepdf
from django.conf import settings
from PIL import Image

logger = logging.getLogger(__name__)


class PdfRejected(Exception):
    """The file is not an acceptable PDF (permanent failure, no retry)."""


class OcrFailed(Exception):
    pass


@dataclass
class SanitizeResult:
    page_count: int
    removed: list[str]


_DANGEROUS_CATALOG_KEYS = ("/OpenAction", "/AA", "/AcroForm", "/URI")
_DANGEROUS_NAME_TREES = ("/JavaScript", "/EmbeddedFiles")


def sanitize(src: Path, dst: Path) -> SanitizeResult:
    """Open the PDF strictly and strip active content (JavaScript, auto-actions, attachments)."""
    try:
        pdf = pikepdf.open(src)
    except pikepdf.PasswordError as exc:
        raise PdfRejected("Encrypted / password-protected PDFs are not supported") from exc
    except pikepdf.PdfError as exc:
        raise PdfRejected("The file is not a valid PDF") from exc

    removed: list[str] = []
    with pdf:
        if pdf.is_encrypted:
            raise PdfRejected("Encrypted PDFs are not supported")
        pages = len(pdf.pages)
        if pages == 0:
            raise PdfRejected("The PDF has no pages")
        if pages > settings.MAX_PAGES:
            raise PdfRejected(f"Too many pages ({pages} > {settings.MAX_PAGES})")

        root = pdf.Root
        for key in _DANGEROUS_CATALOG_KEYS:
            if key in root:
                del root[key]
                removed.append(key)
        if "/Names" in root:
            names = root.Names
            for key in _DANGEROUS_NAME_TREES:
                if key in names:
                    del names[key]
                    removed.append(key)
        for page in pdf.pages:
            if "/AA" in page.obj:
                del page.obj["/AA"]
                removed.append("/Page/AA")
            for annot in page.obj.get("/Annots", []):
                try:
                    action = annot.get("/A")
                    if action is not None and action.get("/S") in ("/JavaScript", "/Launch", "/SubmitForm"):
                        del annot["/A"]
                        removed.append("/Annot/A")
                except (AttributeError, TypeError, ValueError):
                    continue
        pdf.save(dst, linearize=False, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    if removed:
        logger.info("removed active PDF content", extra={"removed": ",".join(sorted(set(removed)))})
    return SanitizeResult(page_count=pages, removed=removed)


def ocr(src: Path, dst: Path) -> bool:
    """Run OCRmyPDF. Returns False if OCR failed and the source was copied instead."""
    cmd = [
        "ocrmypdf",
        "--skip-text",
        "--rotate-pages",
        "--deskew",
        "--output-type",
        "pdfa",
        "--optimize",
        "1",
        "--jobs",
        str(settings.OCR_JOBS),
        "--language",
        str(settings.OCR_LANGUAGES),
        "--tesseract-timeout",
        "300",
        "--quiet",
        str(src),
        str(dst),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=settings.OCR_TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrFailed("OCR timed out") from exc
    # 0 = ok, 10 = output written but PDF/A conversion failed (still usable)
    if proc.returncode in (0, 10) and dst.exists():
        return True
    tail = (proc.stderr or "").strip().splitlines()[-1:] or [f"exit code {proc.returncode}"]
    # Never log OCR output itself; only the last error line, which describes the failure.
    raise OcrFailed(f"OCR failed: {tail[0][:200]}")


def extract_text(pdf_path: Path) -> str:
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf_path), "-"],
            capture_output=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace").replace("\f", "\n\n")


def thumbnail(pdf_path: Path, *, width: int = 480) -> bytes | None:
    root = pdf_path.with_name(pdf_path.stem + "-thumb")
    png = root.with_suffix(".png")
    try:
        proc = subprocess.run(
            [
                "pdftoppm",
                "-f",
                "1",
                "-l",
                "1",
                "-png",
                "-scale-to",
                str(width),
                "-singlefile",
                str(pdf_path),
                str(root),
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0 or not png.exists():
            return None
        with Image.open(png) as image:
            out = io.BytesIO()
            image.convert("RGB").save(out, format="WEBP", quality=70, method=4)
            return out.getvalue()
    except subprocess.TimeoutExpired:
        return None
    finally:
        png.unlink(missing_ok=True)
