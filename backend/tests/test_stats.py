import pytest
from django.test import Client

from apps.analysis import ai
from apps.processing.preferences import set_ai_model
from apps.processing.worker import Worker
from tests.conftest import upload
from tests.pdfs import INVOICE_LINES, text_pdf

pytestmark = pytest.mark.django_db(transaction=True)


def test_statistics_describe_the_archive_and_its_processing(scanner, api, settings, monkeypatch):
    _, token = scanner
    settings.OLLAMA_URL = "http://ollama:11434"
    set_ai_model("qwen3-vl:4b-instruct")
    monkeypatch.setattr(
        "apps.analysis.ai.analyze",
        lambda *_a, **_k: ai.ModelFields(sender="Energie Nord AG", document_type="invoice"),
    )
    for month in ("Januar", "Februar", "März"):
        upload(Client(), token, text_pdf([*INVOICE_LINES, f"Abrechnung {month}"]))
    Worker().run_until_empty()

    r = api.get("/api/v1/stats")
    assert r.status_code == 200
    s = r.json()
    assert s["documents"] == 3 and s["pages"] == 3 and s["bytes"] > 0
    assert s["per_week"] == 0.2  # 3 in the last 12 weeks
    assert len(s["months"]) == 12 and s["months"][-1]["documents"] == 3
    assert s["types"] == [{"name": "Invoice", "count": 3}]
    assert s["correspondents_top"] == [{"name": "Energie Nord AG", "count": 3}]
    assert s["sources"] == [{"name": "Desk scanner", "count": 3}]
    p = s["processing"]
    assert p["sample"] == 3 and p["readable_median_seconds"] is not None
    assert p["readable_median_seconds"] <= p["done_median_seconds"]
    assert {t["stage"] for t in p["stages"]} >= {"validate", "enhance", "ocr", "analyze"}
    assert p["ai_documents"] == 3 and p["ai_average_seconds"] is not None


def test_statistics_of_an_empty_archive(api):
    s = api.get("/api/v1/stats").json()
    assert s["documents"] == 0 and s["processing"]["readable_median_seconds"] is None
    assert all(m["documents"] == 0 for m in s["months"])
