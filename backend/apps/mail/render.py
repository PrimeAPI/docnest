"""Turn an email into text (for context) and into a PDF (to keep it as a document).

HTML is never rendered: it is reduced to its text, so nothing in a message can
load remote content or run code.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

MAX_TEXT = 100_000
FONT_DIRS = [Path("/usr/share/fonts/truetype/dejavu")]


@dataclass
class EmailView:
    subject: str
    sender: str
    to: str
    sent_at: datetime | None
    text: str
    attachments: list[str] = field(default_factory=list)  # names, with a note when not imported


class _TextExtractor(HTMLParser):
    BLOCKS = frozenset(
        "p div br tr li h1 h2 h3 h4 h5 h6 table blockquote pre hr section article header footer".split()  # noqa: SIM905
    )
    SKIP = frozenset({"script", "style", "head", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")
        elif tag == "td":
            self.parts.append("  ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return tidy("".join(parser.parts))


def tidy(text: str) -> str:
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.replace("\r", "").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:MAX_TEXT]


_font: tuple[str, str] | None = None


def _fonts() -> tuple[str, str]:
    """DejaVu covers umlauts and most scripts; Helvetica is the fallback."""
    global _font
    if _font is None:
        _font = ("Helvetica", "Helvetica-Bold")
        for directory in FONT_DIRS:
            regular, bold = directory / "DejaVuSans.ttf", directory / "DejaVuSans-Bold.ttf"
            if regular.exists() and bold.exists():
                pdfmetrics.registerFont(TTFont("DejaVuSans", str(regular)))
                pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(bold)))
                _font = ("DejaVuSans", "DejaVuSans-Bold")
                break
    return _font


def _escape(text: str) -> str:
    return html.escape(text).replace("\n", "<br/>")


def to_pdf(view: EmailView, target: Path) -> None:
    regular, bold = _fonts()
    body = ParagraphStyle("body", fontName=regular, fontSize=10, leading=13.5)
    label = ParagraphStyle("label", parent=body, fontName=bold, textColor="#555555")
    heading = ParagraphStyle("heading", parent=body, fontName=bold, fontSize=14, leading=18)
    rows = [
        ("From", view.sender),
        ("To", view.to),
        ("Date", view.sent_at.strftime("%d.%m.%Y %H:%M") if view.sent_at else ""),
        ("Attachments", "\n".join(view.attachments)),
    ]
    table = Table(
        [[Paragraph(k, label), Paragraph(_escape(v), body)] for k, v in rows if v],
        colWidths=[28 * mm, None],
    )
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story = [
        Paragraph(_escape(view.subject or "(no subject)"), heading),
        Spacer(1, 4 * mm),
        table,
        Spacer(1, 3 * mm),
        HRFlowable(width="100%", color="#cccccc"),
        Spacer(1, 3 * mm),
    ]
    for paragraph in re.split(r"\n\s*\n", view.text or ""):
        if paragraph.strip():
            story.append(Paragraph(_escape(paragraph.strip()), body))
            story.append(Spacer(1, 2.5 * mm))
    doc = SimpleDocTemplate(
        str(target),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=view.subject[:200],
        author=view.sender[:200],
        creator="DocNest",
        invariant=1,  # the same email gives the same file
    )
    doc.build(story)
