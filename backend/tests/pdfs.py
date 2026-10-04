"""Synthetic test documents (no real personal data)."""

from __future__ import annotations

import io
import subprocess
import tempfile
from pathlib import Path

from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def text_pdf(lines: list[str], *, extra_pages: list[list[str]] | None = None) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for page in [lines, *(extra_pages or [])]:
        y = 800
        c.setFont("Helvetica", 11)
        for line in page:
            c.drawString(60, y, line)
            y -= 16
        c.showPage()
    c.save()
    return buf.getvalue()


def scanned_pdf(lines: list[str]) -> bytes:
    """An image-only PDF (like a scanner produces): text exists only as pixels."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.pdf"
        src.write_bytes(text_pdf(lines))
        subprocess.run(
            ["pdftoppm", "-r", "200", "-gray", "-png", "-singlefile", str(src), str(Path(tmp) / "page")],
            check=True,
        )
        image = Image.open(Path(tmp) / "page.png").convert("L")
        out = io.BytesIO()
        image.save(out, format="PDF", resolution=200)
        return out.getvalue()


def javascript_pdf() -> bytes:
    import pikepdf

    pdf = pikepdf.open(io.BytesIO(text_pdf(["Harmless looking letter"])))
    js = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert('x')"))
    pdf.Root.OpenAction = pdf.make_indirect(js)
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


INVOICE_LINES = [
    "Stadtwerke Musterstadt GmbH · Energieweg 1 · 12345 Musterstadt",
    "",
    "Max Mustermann",
    "Beispielstrasse 5",
    "54321 Beispielort",
    "",
    "Musterstadt, 14.03.2026",
    "Rechnung Nr. RE-2026-0042",
    "Kundennummer: KD 778899",
    "Stromlieferung fuer den Zeitraum 01.02.2026 bis 28.02.2026",
    "Verbrauch 312 kWh, Zaehlerstand 45123",
    "Rechnungsbetrag: 98,40 EUR",
    "Bitte ueberweisen Sie den Betrag auf IBAN DE89 3704 0044 0532 0130 00",
]

INSURANCE_LINES = [
    "Allsafe Versicherung AG · Policenweg 9 · 10115 Berlin",
    "",
    "Berlin, 02.01.2026",
    "Beitragsrechnung zu Ihrer Fahrzeugversicherung",
    "Versicherungsschein-Nr.: VS-445566",
    "Kennzeichen: B-XY 1234",
    "Fahrzeug: Kraftfahrzeug Typ Kombi",
    "Jahresbeitrag Kfz-Haftpflicht und Teilkasko: 612,00 EUR",
]


def payslip_lines(month_name: str, month: int, year: int = 2026, net: str = "2.345,67") -> list[str]:
    return [
        "Muster Software GmbH · Hauptstrasse 10 · 80331 Muenchen",
        "",
        f"Muenchen, 28.{month:02d}.{year}",
        f"Gehaltsabrechnung {month_name} {year}",
        "Personalnummer 004711   Steuerklasse 1",
        "Bruttolohn 4.100,00 EUR",
        "Lohnsteuer 612,33 EUR  Solidaritaetszuschlag 0,00 EUR",
        "Krankenversicherung 340,10 EUR  Rentenversicherung 381,30 EUR",
        f"Nettoverdienst {net} EUR",
        "Auszahlungsbetrag wird ueberwiesen auf das Girokonto",
    ]
