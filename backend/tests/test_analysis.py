from datetime import date

import pytest
from django.test import Client

from apps.analysis import docling_fields, extraction
from apps.documents import crypto_fields
from apps.documents.models import Document
from apps.processing.worker import Worker
from apps.taxonomy.models import Correspondent, Series, Tag, TagAlias
from tests.conftest import upload
from tests.pdfs import INVOICE_LINES, payslip_lines, text_pdf

J = "application/json"


def test_extract_invoice_fields():
    e = extraction.extract("\n".join(INVOICE_LINES))
    assert e.document_date == date(2026, 3, 14)
    assert e.sender == "Stadtwerke Musterstadt GmbH"
    assert e.total_amount == "98.40"
    assert e.ibans == ["DE89370400440532013000"]
    assert e.references.get("rechnungnr") == "RE-2026-0042" or "RE-2026-0042" in e.references.values()


def test_extract_dates_formats():
    text = "Berlin, den 3. März 2026\nGeburtsdatum 01.01.1980\nfällig bis 30.04.2026"
    assert extraction.extract(text).document_date == date(2026, 3, 3)
    assert extraction.extract("Invoice date: March 5, 2026").document_date == date(2026, 3, 5)
    assert extraction.extract("Stand 2026-02-01").document_date == date(2026, 2, 1)


def test_invalid_iban_is_ignored():
    assert extraction.find_ibans("IBAN DE89 3704 0044 0532 0130 01") == []


@pytest.mark.django_db(transaction=True)
def test_payslips_form_a_series_with_periods(scanner, api):
    _, token = scanner
    months = [("Januar", 1), ("Februar", 2), ("März", 3), ("April", 4)]
    for name, month in months:
        r = upload(
            Client(), token, text_pdf(payslip_lines(name, month, net=f"2.3{month}5,00")), bucket="private"
        )
        assert r.status_code == 202
        Worker().run_until_empty()

    docs = list(Document.objects.order_by("document_date"))
    assert all(d.processing_state == "done" for d in docs), [d.processing_error for d in docs]
    assert len({d.correspondent_id for d in docs}) == 1
    series_ids = {d.series_id for d in docs}
    assert len(series_ids) == 1 and None not in series_ids
    series = Series.objects.get()
    assert series.cadence == "monthly"
    assert [d.period_label for d in docs] == ["January 2026", "February 2026", "March 2026", "April 2026"]
    assert all(d.document_type.slug == "statement" for d in docs)
    assert "Salary" in {t.name for t in docs[0].tags.all()}

    detail = api.get(f"/api/v1/series/{series.pk}").json()
    assert len(detail["members"]) == 4 and detail["missing_periods"] == []

    # A user-written title becomes the pattern for the next member
    api.patch(f"/api/v1/documents/{docs[-1].uuid}", {"title": "Payslip April 2026"}, content_type=J)
    upload(Client(), token, text_pdf(payslip_lines("Mai", 5)))
    Worker().run_until_empty()
    may = Document.objects.get(document_date=date(2026, 5, 28))
    assert may.series_id == series.pk
    assert crypto_fields.get_title(may) == "Payslip May 2026"


@pytest.mark.django_db(transaction=True)
def test_unrelated_documents_do_not_form_a_series(scanner):
    _, token = scanner
    upload(Client(), token, text_pdf(INVOICE_LINES))
    upload(Client(), token, text_pdf(payslip_lines("Januar", 1)))
    Worker().run_until_empty()
    assert not Document.objects.filter(series__isnull=False).exists()


@pytest.mark.django_db(transaction=True)
def test_existing_german_tag_is_reused_instead_of_new_english_one(scanner):
    _, token = scanner
    Tag.objects.create(name="Fahrzeug")
    from tests.pdfs import INSURANCE_LINES

    upload(Client(), token, text_pdf(INSURANCE_LINES))
    Worker().run_until_empty()
    doc = Document.objects.get()
    names = {t.name for t in doc.tags.all()}
    assert "Fahrzeug" in names and "Vehicle" not in names
    assert not Tag.objects.filter(name__iexact="Vehicle").exists()


@pytest.mark.django_db(transaction=True)
def test_learns_from_user_corrections(scanner, api):
    from apps.analysis import classifier

    _, token = scanner
    letters = [
        [
            "Turnverein Grünwald",
            "Sehr geehrtes Mitglied",
            "Einladung zur Jahreshauptversammlung des Vereins",
            f"Tagesordnung Punkt {i}",
            "Mitgliedsbeitrag Satzung Vorstand Kassenbericht",
        ]
        for i in range(4)
    ]
    others = [
        ["Pizzeria Roma", f"Bestellung {i}", "Margherita Salami Lieferung", "Guten Appetit wünscht Ihr Team"]
        for i in range(4)
    ]
    for lines in letters + others:
        upload(Client(), token, text_pdf(lines))
    Worker().run_until_empty()
    club = Correspondent.objects.create(name="TV Grünwald")
    tag = api.post("/api/v1/tags", {"name": "Verein"}, content_type=J).json()
    for doc in Document.objects.all():
        title = crypto_fields.get_content(doc)
        if "Turnverein" in title:
            r = api.patch(
                f"/api/v1/documents/{doc.uuid}",
                {"correspondent_id": club.pk, "tag_ids": [tag["id"]]},
                content_type=J,
            )
            assert r.status_code == 200
    classifier.train_all()

    upload(
        Client(),
        token,
        text_pdf(
            [
                "Turnverein Grünwald",
                "Einladung zur Jahreshauptversammlung",
                "Tagesordnung Vorstand Satzung Kassenbericht",
            ]
        ),
    )
    Worker().run_until_empty()
    new = Document.objects.order_by("-uploaded_at").first()
    assert "Verein" in {t.name for t in new.tags.all()}


@pytest.mark.django_db
def test_tag_merge_and_alias(api):
    a = api.post("/api/v1/tags", {"name": "KFZ"}, content_type=J).json()
    b = api.post("/api/v1/tags", {"name": "Fahrzeug"}, content_type=J).json()
    r = api.post(f"/api/v1/tags/{a['id']}/merge", {"target_id": b["id"]}, content_type=J)
    assert r.status_code == 200 and "KFZ" in r.json()["aliases"]
    assert api.post("/api/v1/tags", {"name": "kfz"}, content_type=J).status_code == 400
    from apps.taxonomy.services import find_tag

    assert find_tag("Kfz").name == "Fahrzeug"
    assert TagAlias.objects.filter(alias="KFZ").exists()


def test_reference_numbers_do_not_swallow_following_words():
    refs = extraction.find_references("Personalnummer 004711   Steuerklasse 1\nKundennummer: KD 778899")
    assert refs["personalnummer"] == "004711"
    assert refs["kundennummer"] == "KD 778899"


def test_docling_layout_detects_sender_and_title_not_recipient():
    structure = {
        "texts": [
            {
                "label": "page_header",
                "text": "Stadtwerke Musterstadt GmbH - Energieweg 1 - 12345 Musterstadt",
            },
            {"label": "text", "text": "Max Mustermann"},
            {
                "label": "text",
                "text": "Rechnung Nr. RE-2026-0042 Stromlieferung für Ihre Verbrauchsstelle",
            },
        ]
    }
    layout = {
        "pages": [
            {
                "height": 842,
                "lines": [
                    {
                        "text": "Stadtwerke Musterstadt GmbH - Energieweg 1 - 12345 Musterstadt",
                        "y1": 808,
                    },
                    {"text": "Max Mustermann", "y1": 720},
                    {"text": "Beispielstrasse 12", "y1": 700},
                    {"text": "Rechnung Nr. RE-2026-0042", "y1": 560},
                    {"text": "Stromlieferung für Ihre Verbrauchsstelle", "y1": 520},
                ],
            }
        ]
    }

    detected = docling_fields.detect(structure, layout)

    assert detected.sender == "Stadtwerke Musterstadt GmbH"
    assert detected.title == "Stromlieferung für Ihre Verbrauchsstelle"
    assert detected.sender_confidence >= 0.72
    assert detected.title_confidence >= 0.55


def test_docling_vlm_fields_need_document_evidence():
    layout = docling_fields.DetectedFields(
        sender="Existing GmbH",
        title="Existing invoice",
        sender_confidence=0.8,
        title_confidence=0.8,
    )
    merged = docling_fields.merge_vlm(
        layout,
        sender="Hallucinated AG",
        title="Completely unrelated subject",
        evidence="Existing GmbH\nExisting invoice for March",
    )
    assert merged.sender == "Existing GmbH"
    assert merged.title == "Existing invoice"
