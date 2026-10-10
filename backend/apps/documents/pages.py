"""Pages as shown: a small picture of each, and a fingerprint to recognise it elsewhere.

Made when a document is processed (and once for older documents, by a `pages` job). The
page editor shows the pictures; the page analysis compares fingerprints across the whole
archive to find a page scanned twice, a document that is the copy of another, or the
parts of one letter in several scans.

A fingerprint keeps no readable text: hashes of the page's word shingles (a bottom-k
sketch: the smallest hashes estimate how alike two pages are), hashes of the numbers on it
(two payslips share their words, not their numbers), a difference hash of the picture,
and the page mark ("Seite 2 von 3") it shows.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction
from PIL import Image

from apps.crypto.aead import decrypt_bytes, encrypt_bytes
from apps.documents import crypto_fields, files
from apps.documents.models import Document, DocumentPage
from apps.processing import pdf, queue
from apps.processing.models import Job
from apps.search import tokenizer

logger = logging.getLogger(__name__)

THUMB_WIDTH = 240
LARGE_EDGE = 1600
SKETCH = 96  # smallest shingle hashes kept per page
SHINGLE = 4  # words per shingle
MAX_NUMBERS = 300
HASH_SIZE = 16  # difference hash of 16×16 = 256 bits
INK_LEVEL = 160  # grey values below this are ink
EMPTY = 0.002  # share of ink below which a page is empty (dust, punch holes, show-through)
AUTO_BLANK = 0.0001  # automatic hiding must be much more conservative than a suggestion
MIN_PAGE_TEXT = 60  # characters: a page with less is compared by its picture
NUMBER = re.compile(r"\d(?:[\d.,/:-]*\d)?")
MARK = re.compile(
    r"(?<![\w])(?:seite|blatt|page|pg\.?|s\.)\s*(\d{1,3})\s*(?:von|of|/|aus|v\.)\s*(\d{1,3})(?!\d)", re.I
)


@dataclass
class Fingerprint:
    sketch: list[int]  # the smallest shingle hashes
    numbers: list[int]  # hashes of the numbers on the page
    dhash: str  # hex
    chars: int  # characters of text
    mark: list[int] | None  # [page, of] as printed on it
    ink: float  # share of dark pixels: ~0 is an empty page

    @property
    def has_text(self) -> bool:
        return self.chars >= MIN_PAGE_TEXT


def _h(value: str, bits: int = 64) -> int:
    return int.from_bytes(hashlib.blake2b(value.encode(), digest_size=bits // 8).digest(), "big")


def text_fingerprint(text: str) -> tuple[list[int], list[int], int, list[int] | None]:
    words = [tokenizer.fold(w) for w in tokenizer.words(text)]
    shingles = {_h(" ".join(words[i : i + SHINGLE])) for i in range(max(0, len(words) - SHINGLE + 1))}
    numbers = sorted({_h(n.strip(".,:-/"), 32) for n in NUMBER.findall(text)})[:MAX_NUMBERS]
    mark = None
    for m in MARK.finditer(text):
        page, of = int(m.group(1)), int(m.group(2))
        if 1 <= page <= of <= 200:
            mark = [page, of]
            break
    return sorted(shingles)[:SKETCH], numbers, len(text.strip()), mark


def dhash(image: Image.Image) -> tuple[str, float]:
    gray = image.convert("L")
    small = gray.resize((HASH_SIZE + 1, HASH_SIZE), Image.Resampling.LANCZOS)
    pixels = small.tobytes()  # one byte per pixel, row by row
    bits = 0
    for row in range(HASH_SIZE):
        for col in range(HASH_SIZE):
            left = pixels[row * (HASH_SIZE + 1) + col]
            right = pixels[row * (HASH_SIZE + 1) + col + 1]
            bits = (bits << 1) | (left > right)
    histogram = gray.histogram()
    ink = sum(histogram[:INK_LEVEL]) / max(1, sum(histogram))
    return f"{bits:0{HASH_SIZE * HASH_SIZE // 4}x}", round(ink, 4)


# --- Comparing -----------------------------------------------------------------------------------


def text_similarity(a: Fingerprint, b: Fingerprint) -> float:
    """Estimated Jaccard similarity of the pages' shingles (bottom-k)."""
    if not a.sketch or not b.sketch:
        return 0.0
    sa, sb = set(a.sketch), set(b.sketch)
    union = sorted(sa | sb)[:SKETCH]
    both = sa & sb
    return sum(1 for x in union if x in both) / len(union)


def number_similarity(a: Fingerprint, b: Fingerprint) -> float:
    if not a.numbers and not b.numbers:
        return 1.0
    na, nb = set(a.numbers), set(b.numbers)
    return len(na & nb) / max(1, len(na | nb))


def picture_distance(a: Fingerprint, b: Fingerprint) -> int:
    return bin(int(a.dhash, 16) ^ int(b.dhash, 16)).count("1")


SAME_TEXT = 0.85
SAME_NUMBERS = 1.0  # different amounts, dates or references must not count as duplicate pages
SAME_PICTURE = 20  # of 256 bits: the same scan, or the same sheet scanned again
SAME_PICTURE_NO_TEXT = 10


def same_page(a: Fingerprint, b: Fingerprint) -> float:
    """How sure it is the same page (0: not; up to 1)."""
    if a.ink < EMPTY or b.ink < EMPTY:
        return 0.0  # empty pages are all alike
    if a.has_text and b.has_text:
        text = text_similarity(a, b)
        if text < SAME_TEXT or number_similarity(a, b) < SAME_NUMBERS:
            return 0.0
        return min(1.0, (text + number_similarity(a, b)) / 2)
    if a.has_text != b.has_text:
        return 0.0
    distance = picture_distance(a, b)
    return 0.0 if distance > SAME_PICTURE_NO_TEXT else 1 - distance / 256


# --- Making them --------------------------------------------------------------------------------


def _aad(document: Document, number: int, what: str) -> bytes:
    return f"page:{document.uuid}:{number}:{what}".encode()


def page_texts(pdf_path: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf_path), "-"],
            capture_output=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return []
    if proc.returncode != 0:
        return []
    return proc.stdout.decode("utf-8", errors="replace").split("\f")


def recognized_texts(document: Document) -> list[str]:
    """Reuse separately stored, page-scoped OCR (Docling), without recognizing anything again."""
    result = [""] * document.page_count
    for page in crypto_fields.get_layout(document).get("pages", []):
        n = page.get("page_no", 0)
        if isinstance(n, int) and 1 <= n <= len(result):
            result[n - 1] = "\n".join(line.get("text", "") for line in page.get("lines", []))
    structured = crypto_fields.get_structure(document)
    if structured.get("schema_name") == "DoclingDocument" and any(not text.strip() for text in result):
        try:
            from docling_core.types.doc import DoclingDocument

            parsed = DoclingDocument.model_validate(structured)
            result = [
                text
                if text.strip()
                else parsed.export_to_markdown(
                    page_no=n,
                    image_placeholder="",
                    include_picture_classification=False,
                )
                for n, text in enumerate(result, start=1)
            ]
        except (ValueError, TypeError, KeyError):
            logger.warning("could not reuse structured page text", extra={"document": str(document.uuid)})
    if document.page_count == 1 and not result[0].strip():
        result[0] = crypto_fields.get_content(document)
    return result


def render(pdf_path: Path, out_dir: Path) -> list[Path]:
    proc = subprocess.run(
        ["pdftoppm", "-png", "-scale-to", str(THUMB_WIDTH * 2), str(pdf_path), str(out_dir / "p")],
        capture_output=True,
        timeout=600,
        check=False,
    )
    if proc.returncode:
        raise RuntimeError("Could not render the document's pages")
    return sorted(out_dir.glob("p-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))


def build(document: Document, pdf_path: Path) -> int:
    """Pictures and fingerprints of every page of `pdf_path` (the archive as shown)."""
    version = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    texts = page_texts(pdf_path)
    recognized = recognized_texts(document)
    texts = [
        texts[n] if n < len(texts) and texts[n].strip() else recognized[n] for n in range(document.page_count)
    ]
    with tempfile.TemporaryDirectory(dir=settings.WORK_DIR) as tmp:
        images = render(pdf_path, Path(tmp))
        if len(images) != document.page_count:
            raise RuntimeError("The rendered page count does not match the document")
        rows = []
        blank = []
        for number, png in enumerate(images, start=1):
            with Image.open(png) as image:
                picture, ink = dhash(image)
                thumb = image.convert("RGB")
                thumb.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 2))
                out = io.BytesIO()
                thumb.save(out, format="WEBP", quality=60, method=4)
            sketch, numbers, chars, mark = text_fingerprint(texts[number - 1] if number <= len(texts) else "")
            fp = Fingerprint(sketch, numbers, picture, chars, mark, ink)
            if fp.ink < AUTO_BLANK and fp.chars == 0:
                blank.append(number)
            rows.append(
                DocumentPage(
                    document=document,
                    number=number,
                    thumbnail_enc=encrypt_bytes(out.getvalue(), aad=_aad(document, number, "thumb")),
                    fingerprint_enc=encrypt_bytes(
                        json.dumps(asdict(fp)).encode(), aad=_aad(document, number, "fingerprint")
                    ),
                    version=version,
                )
            )
    with transaction.atomic():
        current = Document.objects.select_for_update().get(pk=document.pk).page_view
        DocumentPage.objects.filter(document=document).delete()
        DocumentPage.objects.bulk_create(rows)
        state: dict[str, Any] = {
            "version": version,
            "count": document.page_count,
            "blank": blank,
        }
        if current.get("count") == document.page_count and "order" in current:
            state["order"] = current["order"]
        document.page_view = state
        document.save(update_fields=["page_view"])
    return len(rows)


def view_state(document: Document) -> dict[str, Any]:
    """Stored presentation metadata, with a read-only fallback for pre-upgrade fingerprints."""
    if document.page_view and document.page_view.get("count") == document.page_count:
        return document.page_view
    state = {"version": "", "count": document.page_count, "blank": []}
    rows = list(DocumentPage.objects.filter(document=document).only("number", "version", "fingerprint_enc"))
    if len(rows) != document.page_count:
        return state
    blank = []
    for row in rows:
        if row.fingerprint_enc:
            fp = Fingerprint(
                **json.loads(
                    decrypt_bytes(bytes(row.fingerprint_enc), aad=_aad(document, row.number, "fingerprint"))
                )
            )
            if fp.ink < AUTO_BLANK and fp.chars == 0:
                blank.append(row.number)
    return {**state, "version": rows[0].version if rows else "", "blank": blank}


def visible_numbers(document: Document) -> list[int]:
    """Physical archive pages shown in Enhanced; Original never uses this presentation."""
    state = view_state(document)
    if "order" in state:
        return [n for n in state["order"] if 1 <= n <= document.page_count]
    blank = set(state.get("blank", []))
    shown = [n for n in range(1, document.page_count + 1) if n not in blank]
    # A PDF cannot have zero pages; do not silently hide an entirely blank scan.
    return shown or list(range(1, document.page_count + 1))


def build_from_storage(document: Document) -> int:
    fh = files.fetch(document, "archive", apply_view=False)
    try:
        return build(document, Path(fh.name))
    finally:
        fh.close()


def ensure(document: Document) -> None:
    """Make the pages now if they are missing (the page editor needs them)."""
    if document.page_count and DocumentPage.objects.filter(document=document).count() == document.page_count:
        return
    build_from_storage(document)


def invalidate(document: Document) -> None:
    DocumentPage.objects.filter(document=document).delete()


def run(document_id: int) -> None:
    """The `pages` job."""
    document = Document.objects.filter(pk=document_id, deleted_at__isnull=True).first()
    if document is None or document.processing_state != Document.State.DONE:
        return
    ensure(document)


def schedule_missing() -> int:
    """Queue the pages job for every finished document without (all) its pages."""
    n = 0
    documents = Document.objects.filter(
        deleted_at__isnull=True, processing_state=Document.State.DONE, page_count__gt=0
    )
    for document in documents.iterator():
        missing = DocumentPage.objects.filter(document=document).count() != document.page_count
        if missing and queue.enqueue(Job.Kind.PAGES, document=document, priority=-5):
            n += 1
    return n


def large(document: Document, number: int) -> bytes | None:
    """A page big enough to read (for a closer look in the page editor); rendered on request."""
    if not 1 <= number <= document.page_count:
        return None
    fh = files.fetch(document, "archive", apply_view=False)
    try:
        png = pdf.render_page(Path(fh.name), number, long_edge=LARGE_EDGE)
    finally:
        fh.close()
    if png is None:
        return None
    with Image.open(io.BytesIO(png)) as image:
        out = io.BytesIO()
        image.convert("RGB").save(out, format="WEBP", quality=75, method=4)
        return out.getvalue()


def thumbnail(document: Document, number: int) -> bytes | None:
    row = DocumentPage.objects.filter(document=document, number=number).first()
    if row is None or not row.thumbnail_enc:
        return None
    return decrypt_bytes(bytes(row.thumbnail_enc), aad=_aad(document, number, "thumb"))


def fingerprints(documents: list[Document]) -> dict[int, list[Fingerprint]]:
    """document pk -> its pages' fingerprints, in page order (documents without pages: absent)."""
    by_pk = {d.pk: d for d in documents}
    out: dict[int, list[Fingerprint]] = {}
    for row in DocumentPage.objects.filter(document_id__in=by_pk).order_by("document_id", "number"):
        if not row.fingerprint_enc:
            continue
        d = by_pk[row.document_id]
        data = json.loads(decrypt_bytes(bytes(row.fingerprint_enc), aad=_aad(d, row.number, "fingerprint")))
        out.setdefault(d.pk, []).append(Fingerprint(**data))
    return {pk: fps for pk, fps in out.items() if len(fps) == by_pk[pk].page_count}
