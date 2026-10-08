"""Reprocessing chosen steps: enhancement, text recognition (and its processor), analysis (and its model)."""

import shutil

import pytest
from django.test import Client

from apps.analysis import ai
from apps.documents.models import Document, ProcessingEvent
from apps.processing import pdf
from apps.processing.preferences import set_ai_model
from apps.processing.worker import Worker
from tests.conftest import upload
from tests.pdfs import INVOICE_LINES, text_pdf

pytestmark = pytest.mark.django_db(transaction=True)
J = "application/json"


def process_all() -> None:
    Worker().run_until_empty()


@pytest.fixture
def ocr_calls(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_ocr(src, dst, **kwargs):
        calls.append({"src": src.name, **kwargs})
        shutil.copyfile(src, dst)
        return True

    monkeypatch.setattr(pdf, "ocr", fake_ocr)
    return calls


@pytest.fixture
def doc_id(scanner, ocr_calls) -> str:
    _, token = scanner
    uuid = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    process_all()
    ocr_calls.clear()
    return uuid


def ran(document: Document) -> list[str]:
    """Stages that ran in the latest pass, oldest first."""
    events = ProcessingEvent.objects.filter(document=document, outcome="ok").order_by("-id")
    stages: list[str] = []
    for event in events:
        if event.stage in stages:
            break
        stages.append(event.stage)
    return stages[::-1]


def reprocess(api, doc_id: str, **body):
    return api.post(f"/api/v1/documents/{doc_id}/reprocess", body, content_type=J)


def test_analysis_alone_keeps_the_recognised_text(api, doc_id, ocr_calls):
    assert reprocess(api, doc_id, steps=["analyze"]).status_code == 200
    process_all()
    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == "done" and doc.processing_plan == {}
    assert ran(doc) == ["analyze", "store", "index"]
    assert ocr_calls == []


def test_text_recognition_alone_rereads_the_current_version(api, doc_id, ocr_calls):
    assert reprocess(api, doc_id, steps=["ocr"]).status_code == 200
    process_all()
    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == "done", doc.processing_error
    assert ran(doc) == ["ocr", "store", "index"]
    # Not the raw original: the archive, whose old OCR layer is replaced.
    assert [(c["src"], c["redo"]) for c in ocr_calls] == [("current.pdf", True)]


def test_enhancement_brings_text_recognition_along(api, doc_id, ocr_calls):
    assert reprocess(api, doc_id, steps=["enhance"]).status_code == 200
    process_all()
    doc = Document.objects.get(uuid=doc_id)
    assert ran(doc) == ["enhance", "ocr", "store", "index"]
    assert ocr_calls and ocr_calls[0]["redo"] is False


def test_processor_can_be_switched_for_text_recognition(api, doc_id, monkeypatch):
    from apps.processing.docling_backend import DoclingResult

    monkeypatch.setattr(
        "apps.processing.docling_backend.convert",
        lambda src: DoclingResult(markdown="Stromrechnung", structured={"texts": []}, layout={}),
    )
    assert reprocess(api, doc_id, steps=["ocr", "analyze"], backend="docling").status_code == 200
    process_all()
    doc = Document.objects.get(uuid=doc_id)
    assert doc.ocr_backend == "docling"
    assert ran(doc) == ["ocr", "analyze", "store", "index"]


def test_analysis_model_can_be_chosen_per_run(api, doc_id, settings, monkeypatch):
    settings.OLLAMA_URL = "http://ollama:11434"
    set_ai_model("qwen3-vl:8b-instruct")
    used: list[str] = []

    def analyze(model, **_kwargs):
        used.append(model)
        return ai.ModelFields(sender="Energie Nord AG")

    monkeypatch.setattr("apps.analysis.ai.analyze", analyze)
    assert reprocess(api, doc_id, steps=["analyze"], ai_model="gemma3:12b").status_code == 200
    process_all()
    assert reprocess(api, doc_id, steps=["analyze"], ai_model="").status_code == 200  # rules only
    process_all()
    assert used == ["gemma3:12b"]
    assert Document.objects.get(uuid=doc_id).processing_plan == {}


@pytest.mark.parametrize(
    "body",
    [
        {"steps": []},
        {"steps": ["analyze"], "backend": "docling"},
        {"steps": ["ocr"], "enhancement": {"deskew": False}},
        {"steps": ["ocr"], "ai_model": "gemma3:12b"},
        {"steps": ["analyze"], "ai_model": "bad name"},
    ],
)
def test_options_must_match_the_chosen_steps(api, doc_id, body):
    assert reprocess(api, doc_id, **body).status_code == 422


def test_a_failed_document_first_catches_up_on_missing_steps(api, doc_id, ocr_calls):
    Document.objects.filter(uuid=doc_id).update(processing_state="failed", processing_stage="ocr")
    assert reprocess(api, doc_id, steps=["analyze"]).status_code == 200
    process_all()
    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == "done"
    assert ran(doc) == ["ocr", "analyze", "store", "index"]


def test_all_documents_with_chosen_steps(api, scanner, ocr_calls):
    _, token = scanner
    for month in ("Januar", "Februar"):
        upload(Client(), token, text_pdf([*INVOICE_LINES, f"Abrechnung {month}"]))
    process_all()
    ocr_calls.clear()
    r = api.post("/api/v1/documents/reprocess-all", {"steps": ["ocr"]}, content_type=J)
    assert r.status_code == 200 and r.json()["updated"] == 2
    process_all()
    assert len(ocr_calls) == 2 and all(call["redo"] for call in ocr_calls)
