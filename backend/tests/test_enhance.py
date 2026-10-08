"""Scan enhancement: orientation, deskew, crop, cleanup and blank pages on synthetic scans."""

import io
from pathlib import Path

import img2pdf
import numpy as np
import pikepdf
import pytest
from PIL import Image, ImageDraw

from apps.processing import enhance
from apps.processing.enhance_settings import EnhanceSettings
from tests.pdfs import INVOICE_LINES, page_image, text_pdf

DPI = 150
LETTER = [
    *INVOICE_LINES,
    "",
    "Sehr geehrte Damen und Herren,",
    *[
        f"Position {i}: Grundpreis und Arbeitspreis gemaess Tarif Musterstrom Klassik {i * 7},50 EUR"
        for i in range(1, 22)
    ],
    "Mit freundlichen Gruessen",
    "Ihre Stadtwerke Musterstadt",
]
ONLY = {
    "rotate": False,
    "deskew": False,
    "crop": False,
    "cleanup": False,
    "remove_blank": False,
}


def letter(mode: str = "L") -> Image.Image:
    return Image.open(io.BytesIO(page_image(LETTER, dpi=DPI, mode=mode))).copy()


def pdf_of(*images: Image.Image, fmt: str = "PNG") -> bytes:
    pages = []
    for image in images:
        buf = io.BytesIO()
        image.save(buf, format=fmt, **({"quality": 92} if fmt == "JPEG" else {}))
        pages.append(buf.getvalue())
    return img2pdf.convert(pages, layout_fun=img2pdf.get_fixed_dpi_layout_fun((DPI, DPI)))


def settings(**changes: object) -> EnhanceSettings:
    return EnhanceSettings.from_json(changes)


def run(tmp_path: Path, pdf: bytes, options: EnhanceSettings) -> tuple[enhance.EnhanceResult, Path]:
    src, dst = tmp_path / "in.pdf", tmp_path / "out.pdf"
    src.write_bytes(pdf)
    return enhance.enhance(src, dst, options, tmp_path), dst


def page_images(path: Path) -> list[Image.Image]:
    with pikepdf.open(path) as pdf:
        return [s.image for p in pdf.pages if (s := enhance.scan_image(p)) is not None]


def ink_angle(image: Image.Image) -> float:
    """Angle of the text lines measured independently of jdeskew (projection profile sharpness)."""
    grey = np.array(image.convert("L"))
    ink = (grey < 128).astype(np.uint8) * 255
    best, best_score = 0.0, -1.0
    for angle in np.arange(-5, 5.01, 0.25):
        rotated = np.array(Image.fromarray(ink).rotate(float(angle), fillcolor=0))
        profile = rotated.sum(axis=1).astype(np.float64)
        score = float(np.var(profile))
        if score > best_score:
            best, best_score = float(angle), score
    return best


# --- Settings -------------------------------------------------------------------------


def test_settings_validate_and_merge():
    base = EnhanceSettings()
    merged = EnhanceSettings.from_json(
        {"deskew": False, "deskew_max_angle": 500, "cleanup_strength": "brutal", "unknown": 1, "crop": "yes"},
        base=base,
    )
    assert merged.deskew is False
    assert merged.deskew_max_angle == 30.0  # clamped
    assert merged.cleanup_strength == "medium"  # invalid value ignored
    assert merged.crop is True  # wrong type ignored
    assert EnhanceSettings.from_json(merged.to_json()) == merged


# --- Page selection ----------------------------------------------------------------------


def test_text_pdf_is_left_untouched(tmp_path):
    result, dst = run(tmp_path, text_pdf(INVOICE_LINES), EnhanceSettings())
    assert result.pages == [None]
    assert not result.changed and not dst.exists()


def test_scanned_page_is_recognised_with_its_resolution():
    with pikepdf.open(io.BytesIO(pdf_of(letter()))) as pdf:
        scan = enhance.scan_image(pdf.pages[0])
    assert scan is not None
    assert scan.dpi == pytest.approx((DPI, DPI), rel=0.01)
    assert scan.lossless


# --- Orientation --------------------------------------------------------------------------


@pytest.mark.ocr
def test_upside_down_page_is_rotated_losslessly(tmp_path):
    upside_down = letter().rotate(180)
    pdf = pdf_of(upside_down, fmt="JPEG")
    result, dst = run(tmp_path, pdf, settings(**{**ONLY, "rotate": True}))
    assert result.pages[0] is not None and result.pages[0].rotated == 180
    with pikepdf.open(dst) as out, pikepdf.open(io.BytesIO(pdf)) as before:
        assert int(out.pages[0].obj.get("/Rotate", 0)) == 180
        # The image itself was not re-encoded.
        (_, original_image), (_, new_image) = (
            next(iter(before.pages[0].get_images(recursive=False).items())),
            next(iter(out.pages[0].get_images(recursive=False).items())),
        )
        assert original_image.read_raw_bytes() == new_image.read_raw_bytes()


@pytest.mark.ocr
def test_sideways_page_is_turned_upright(tmp_path):
    sideways = letter().rotate(90, expand=True)  # text runs bottom-to-top
    result, dst = run(tmp_path, pdf_of(sideways), settings(**{**ONLY, "rotate": True, "cleanup": True}))
    assert result.pages[0] is not None and result.pages[0].rotated == 90
    (image,) = page_images(dst)
    assert image.width < image.height  # portrait again


# --- Deskew -------------------------------------------------------------------------------


@pytest.mark.parametrize("skew", [2.5, -4.0])
def test_skewed_page_is_straightened(tmp_path, skew):
    skewed = letter().rotate(skew, expand=True, fillcolor=255, resample=Image.Resampling.BICUBIC)
    assert abs(ink_angle(skewed) + skew) <= 0.5  # the measuring helper sees the skew
    result, dst = run(tmp_path, pdf_of(skewed), settings(**{**ONLY, "deskew": True}))
    report = result.pages[0]
    assert report is not None and report.deskewed == pytest.approx(-skew, abs=0.3)
    (image,) = page_images(dst)
    assert abs(ink_angle(image)) <= 0.25


def test_straight_page_is_not_rotated(tmp_path):
    result, dst = run(tmp_path, pdf_of(letter()), settings(**{**ONLY, "deskew": True}))
    assert result.pages[0] is not None and result.pages[0].deskewed == 0
    assert not result.changed and not dst.exists()


# --- Crop ---------------------------------------------------------------------------------


def test_feeder_overshoot_with_dark_backing_is_cut_off(tmp_path):
    page = letter()
    too_long = Image.new("L", (page.width, page.height + 300), 70)  # grey backing plate below the sheet
    too_long.paste(page, (0, 0))
    result, dst = run(tmp_path, pdf_of(too_long), settings(**{**ONLY, "crop": True}))
    assert result.pages[0] is not None and result.pages[0].cropped
    (image,) = page_images(dst)
    assert image.width == page.width
    assert abs(image.height - page.height) <= 4


def test_crooked_page_scanned_too_long_is_cropped_and_straightened(tmp_path):
    page = letter()
    crooked = page.rotate(2.5, expand=True, fillcolor=90)  # backing visible around the crooked sheet
    too_long = Image.new("L", (crooked.width, crooked.height + 300), 90)
    too_long.paste(crooked, (0, 0))
    result, dst = run(tmp_path, pdf_of(too_long), settings(**{**ONLY, "crop": True, "deskew": True}))
    report = result.pages[0]
    assert report is not None and report.cropped and report.deskewed
    (image,) = page_images(dst)
    assert image.height < too_long.height - 200  # the 300 px overshoot is gone (deskew adds ~60 px)
    assert abs(ink_angle(image)) <= 0.25


def test_overshoot_with_light_backing_is_cut_at_the_paper_edge_shadow(tmp_path):
    page = letter()
    too_long = Image.new("L", (page.width, page.height + 260), 252)  # light backing, like the paper
    too_long.paste(page, (0, 0))
    ImageDraw.Draw(too_long).rectangle((0, page.height, page.width, page.height + 3), fill=150)
    result, dst = run(tmp_path, pdf_of(too_long), settings(**{**ONLY, "crop": True}))
    assert result.pages[0] is not None and result.pages[0].cropped
    (image,) = page_images(dst)
    assert abs(image.height - page.height) <= 4


def test_coloured_letterhead_band_is_not_cropped(tmp_path):
    page = letter("RGB")
    draw = ImageDraw.Draw(page)
    draw.rectangle((0, 0, page.width, 120), fill=(20, 60, 140))  # band touching the top edge
    for x in range(40, page.width - 200, 160):
        draw.text((x, 50), "Ihre Beitragsrechnung", fill=(255, 255, 255))
    result, dst = run(tmp_path, pdf_of(page), settings(**{**ONLY, "crop": True}))
    assert result.pages[0] is not None and not result.pages[0].cropped
    assert not dst.exists()


def test_page_without_background_is_not_cropped(tmp_path):
    result, _ = run(tmp_path, pdf_of(letter()), settings(**{**ONLY, "crop": True}))
    assert result.pages[0] is not None and not result.pages[0].cropped


# --- Cleanup ------------------------------------------------------------------------------


def test_grey_paper_becomes_white_and_text_stays(tmp_path):
    page = np.array(letter()).astype(np.int16)
    grey_paper = np.clip(page - 40, 0, 255).astype(np.uint8)  # dull, grey-ish scan
    result, dst = run(tmp_path, pdf_of(Image.fromarray(grey_paper)), settings(**{**ONLY, "cleanup": True}))
    assert result.pages[0] is not None and result.pages[0].cleaned
    (image,) = page_images(dst)
    out = np.array(image.convert("L"))
    assert np.median(out) == 255  # paper is white
    assert (out < 100).mean() == pytest.approx((page < 100).mean(), rel=0.25)  # ink kept


def test_isolated_specks_are_removed_but_text_dots_stay():
    page = np.array(letter())
    rng = np.random.default_rng(1)
    specks = 0
    for y, x in zip(rng.integers(10, 60, 40), rng.integers(30, page.shape[1] - 30, 40), strict=True):
        page[y, x] = 0  # 1-pixel specks in the empty top margin
        specks += 1
    out, removed = enhance.despeckle(page, DPI, "medium", 255)
    assert removed >= specks * 0.9
    assert (out[10:60] < 128).sum() == 0
    text_before = (page[60:] < 128).sum()
    assert (out[60:] < 128).sum() >= text_before * 0.995


def test_bilevel_scan_stays_bilevel(tmp_path):
    skewed = letter().rotate(3, expand=True, fillcolor=255).point(lambda v: 255 if v > 128 else 0)
    buf = io.BytesIO()
    skewed.convert("1").save(buf, format="TIFF", compression="group4", dpi=(DPI, DPI))
    pdf = img2pdf.convert(buf.getvalue(), layout_fun=img2pdf.get_fixed_dpi_layout_fun((DPI, DPI)))
    result, dst = run(tmp_path, pdf, settings(**{**ONLY, "deskew": True, "cleanup": True}))
    assert result.changed
    (image,) = page_images(dst)
    assert image.mode == "1"
    with pikepdf.open(dst) as out:
        ((_, xobj),) = out.pages[0].get_images(recursive=False).items()
        assert "/CCITTFaxDecode" in str(xobj.Filter)


# --- Blank pages --------------------------------------------------------------------------


def test_blank_pages_are_removed_from_the_enhanced_version(tmp_path):
    blank = Image.new("L", letter().size, 250)
    result, dst = run(tmp_path, pdf_of(letter(), blank, letter()), settings(**{**ONLY, "remove_blank": True}))
    assert result.page_count == 2 and result.summary()["removed_blank"] == 1
    with pikepdf.open(dst) as out:
        assert len(out.pages) == 2


def test_blank_page_with_feeder_shadow_is_removed_after_cropping(tmp_path):
    blank = Image.new("L", letter().size, 250)
    ImageDraw.Draw(blank).rectangle((0, blank.height - 40, blank.width, blank.height), fill=60)
    result, _ = run(
        tmp_path,
        pdf_of(letter(), blank),
        settings(**{**ONLY, "crop": True, "remove_blank": True}),
    )
    assert result.page_count == 1


def test_all_blank_document_keeps_its_pages(tmp_path):
    blank = Image.new("L", letter().size, 250)
    result, dst = run(tmp_path, pdf_of(blank, blank), settings(**{**ONLY, "remove_blank": True}))
    assert result.kept_blank and result.page_count == 2
    assert not result.changed and not dst.exists()
    assert "none removed" in result.describe()


def test_disabled_enhancement_changes_nothing(tmp_path):
    skewed = letter().rotate(3, expand=True, fillcolor=255)
    result, dst = run(tmp_path, pdf_of(skewed), settings(enabled=False))
    assert not result.changed and not dst.exists()


def test_mixed_document_only_touches_scanned_pages(tmp_path):
    skewed = letter().rotate(3, expand=True, fillcolor=255)
    merged = pikepdf.new()
    with (
        pikepdf.open(io.BytesIO(text_pdf(INVOICE_LINES))) as text,
        pikepdf.open(io.BytesIO(pdf_of(skewed))) as scan,
    ):
        merged.pages.extend(text.pages)
        merged.pages.extend(scan.pages)
        buf = io.BytesIO()
        merged.save(buf)
    result, dst = run(tmp_path, buf.getvalue(), settings(**{**ONLY, "deskew": True}))
    assert result.pages[0] is None and result.pages[1] is not None and result.pages[1].deskewed
    with pikepdf.open(dst) as out:
        assert len(out.pages) == 2
        assert b"BT" in out.pages[0].obj.Contents.read_bytes()  # text page copied as is
