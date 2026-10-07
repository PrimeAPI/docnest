from types import SimpleNamespace

from apps.processing import docling_backend
from apps.processing.docling_ocr import reads_like_words
from tests.pdfs import INVOICE_LINES, scanned_pdf, text_pdf


def _cell(text: str, x0: float, x1: float, y0: float, y1: float, confidence: float = 0.95):
    rect = SimpleNamespace(
        r_x0=x0, r_x1=x1, r_x2=x1, r_x3=x0, r_y0=y0, r_y1=y0, r_y2=y1, r_y3=y1, coord_origin="BOTTOMLEFT"
    )
    return SimpleNamespace(text=text, rect=rect, confidence=confidence, from_ocr=True)


def test_rescued_region_text_must_read_like_words():
    assert reads_like_words(["Ihre", "Hausrat-Beitragsrechnung"])
    assert reads_like_words(["Keine", "Eintragung", "(No", "record)"])
    assert reads_like_words(["Summe", "120,00", "EUR"])
    # Security microprint and guilloches OCR into letter soup.
    assert not reads_like_words(["MUSTERAMTkrrFURnuMUSTERAMTFUR", "FOOOrMUO", "NunAMTSMUZ"])
    assert not reads_like_words(["AMTZ", "FORoESUUUPMUS", "UNOE", "0URUN2MUS"])
    assert not reads_like_words(["123/456789-X"])


def test_ocr_rows_are_split_into_columns_and_specks_dropped():
    cells = [
        _cell("Landratsamt", 38, 98, 680, 690),
        _cell("Musterkreis", 101, 140, 680, 690),
        _cell("Musterstadt,", 283, 335, 681, 691),
        _cell("den", 338, 354, 681, 691),
        _cell("12.03.2024", 357, 405, 681, 691),
        _cell("ee", 515, 522, 600, 606, confidence=0.2),
    ]

    lines = docling_backend._group_line_cells(cells, page_height=720)

    assert [line["text"] for line in lines] == ["Landratsamt Musterkreis", "Musterstadt, den 12.03.2024"]
    assert lines[0]["x1"] == 140
    assert lines[1]["x0"] == 283


def test_only_born_digital_pdfs_skip_full_page_ocr(tmp_path):
    digital = tmp_path / "digital.pdf"
    digital.write_bytes(text_pdf(INVOICE_LINES))
    scan = tmp_path / "scan.pdf"
    scan.write_bytes(scanned_pdf(INVOICE_LINES))
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7 not really")

    assert docling_backend.has_digital_text(digital)
    assert not docling_backend.has_digital_text(scan)
    assert not docling_backend.has_digital_text(broken)
