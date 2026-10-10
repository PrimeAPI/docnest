from datetime import date

import pytest
from django.test import Client
from django.utils import timezone

from apps.analysis import ai
from apps.documents import crypto_fields
from apps.documents.models import Document, ProcessingEvent
from apps.processing.models import Job, SystemState
from apps.processing.preferences import set_ai_model
from apps.processing.worker import Worker
from apps.taxonomy.models import Correspondent
from tests.conftest import upload
from tests.pdfs import INVOICE_LINES, text_pdf

pytestmark = pytest.mark.django_db(transaction=True)


def process_all() -> None:
    Worker().run_until_empty()


@pytest.fixture
def model(settings):
    settings.OLLAMA_URL = "http://ollama:11434"
    set_ai_model("qwen3-vl:8b")


def fake_model(monkeypatch, fields: ai.ModelFields | Exception) -> list[dict]:
    calls: list[dict] = []

    def analyze(model, *, images, text, types, tags=None, context=""):
        calls.append({"model": model, "images": images, "text": text, "types": types, "tags": tags})
        if isinstance(fields, Exception):
            raise fields
        return fields

    monkeypatch.setattr("apps.analysis.ai.analyze", analyze)
    return calls


# --- Pipeline ------------------------------------------------------------------


def test_document_is_readable_before_the_analysis_runs(scanner, api):
    _, token = scanner
    doc_id = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]

    assert Worker().run_once()  # only the intake: assemble, validate
    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_stage == Document.Stage.ENHANCE
    assert doc.processing_state == Document.State.PENDING
    assert Job.objects.get(document=doc, kind=Job.Kind.PROCESS_DOCUMENT).state == Job.State.QUEUED
    assert crypto_fields.get_thumbnail(doc)
    r = api.get(f"/api/v1/documents/{doc_id}/file")
    assert r.status_code == 200 and b"".join(r.streaming_content).startswith(b"%PDF")

    process_all()
    doc.refresh_from_db()
    assert doc.processing_state == Document.State.DONE, doc.processing_error


def test_ai_model_reading_wins_over_names_found_in_the_text(scanner, monkeypatch, model):
    _, token = scanner
    # The recipient's town is a known correspondent: the text mentions it, yet it did not send the letter.
    Correspondent.objects.create(name="Stadtwerke Musterstadt GmbH")
    calls = fake_model(
        monkeypatch,
        ai.ModelFields(
            sender="Energie Nord AG",
            title="Stromrechnung Februar 2026",
            document_date=date(2026, 3, 1),
            document_type="invoice",
            model="qwen3-vl:8b",
        ),
    )
    doc_id = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    process_all()

    doc = Document.objects.get(uuid=doc_id)
    assert doc.processing_state == Document.State.DONE, doc.processing_error
    assert len(calls) == 1
    assert calls[0]["images"] and calls[0]["images"][0].startswith(b"\x89PNG")
    assert "Rechnungsbetrag" in calls[0]["text"]
    assert {t.slug for t in calls[0]["types"]} >= {"invoice", "other"}
    assert doc.correspondent and doc.correspondent.name == "Energie Nord AG"
    assert doc.document_type and doc.document_type.slug == "invoice"
    assert doc.document_date == date(2026, 3, 1)
    assert crypto_fields.get_title(doc).startswith("Stromrechnung Februar 2026")
    assert ProcessingEvent.objects.filter(document=doc, message__contains="qwen3-vl:8b").exists()


def test_ai_sender_is_matched_to_an_existing_correspondent(scanner, monkeypatch, model):
    _, token = scanner
    existing = Correspondent.objects.create(name="Energie Nord", aliases=["EN Energie"])
    fake_model(monkeypatch, ai.ModelFields(sender="Energie Nord AG"))
    upload(Client(), token, text_pdf(INVOICE_LINES))
    process_all()
    assert Document.objects.get().correspondent == existing


def test_unreachable_ai_model_waits_instead_of_guessing(scanner, monkeypatch, model):
    _, token = scanner
    fake_model(monkeypatch, ai.ModelUnavailable("Ollama is not reachable"))
    upload(Client(), token, text_pdf(INVOICE_LINES))
    process_all()

    doc = Document.objects.get()
    assert doc.processing_state == Document.State.PENDING
    assert doc.processing_error.startswith("Waiting for the AI model")
    job = Job.objects.get(document=doc, kind=Job.Kind.PROCESS_DOCUMENT)
    assert job.state == Job.State.QUEUED and job.attempts == 0
    assert not ProcessingEvent.objects.filter(document=doc, outcome="failed").exists()

    fake_model(monkeypatch, ai.ModelFields(sender="Energie Nord AG"))
    Job.objects.update(run_after=timezone.now())
    process_all()
    doc.refresh_from_db()
    assert doc.processing_state == Document.State.DONE
    assert doc.correspondent and doc.correspondent.name == "Energie Nord AG"


def test_failed_ai_answer_falls_back_to_rules(scanner, monkeypatch, model):
    _, token = scanner
    fake_model(monkeypatch, ai.ModelFailed("The model did not answer with JSON"))
    upload(Client(), token, text_pdf(INVOICE_LINES))
    process_all()

    doc = Document.objects.get()
    assert doc.processing_state == Document.State.DONE
    assert doc.correspondent and "Stadtwerke Musterstadt" in doc.correspondent.name
    assert ProcessingEvent.objects.filter(document=doc, outcome="warning", message__contains="AI").exists()


def test_ai_is_skipped_without_a_chosen_model(scanner, monkeypatch, settings):
    _, token = scanner
    settings.OLLAMA_URL = "http://ollama:11434"
    calls = fake_model(monkeypatch, ai.ModelFields(sender="Energie Nord AG"))
    upload(Client(), token, text_pdf(INVOICE_LINES))
    process_all()
    assert calls == []
    assert Document.objects.get().processing_state == Document.State.DONE


def test_reanalysis_corrects_automatic_but_keeps_user_choices(scanner, api, monkeypatch, model):
    _, token = scanner
    fake_model(monkeypatch, ai.ModelFields(sender="Wrong Sender GmbH", document_type="notice"))
    doc_id = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    process_all()
    mine = api.post("/api/v1/correspondents", {"name": "Chosen by me"}, content_type="application/json")
    assert mine.status_code in (200, 201), mine.content
    r = api.patch(
        f"/api/v1/documents/{doc_id}",
        {"correspondent_id": mine.json()["id"]},
        content_type="application/json",
    )
    assert r.status_code == 200

    fake_model(monkeypatch, ai.ModelFields(sender="Energie Nord AG", document_type="invoice"))
    r = api.post(
        f"/api/v1/documents/{doc_id}/reprocess", {"stage": "analyze"}, content_type="application/json"
    )
    assert r.status_code == 200
    process_all()
    doc = Document.objects.get(uuid=doc_id)
    assert doc.correspondent and doc.correspondent.name == "Chosen by me"
    assert doc.document_type and doc.document_type.slug == "invoice"  # automatic: corrected


def test_classifier_does_not_override_a_sender_printed_on_the_page(scanner, monkeypatch):
    _, token = scanner
    other = Correspondent.objects.create(name="Klinikum Irgendwo GmbH")

    class Prediction:
        label = str(other.pk)
        probability = 0.99

    monkeypatch.setattr(
        "apps.analysis.classifier.predict",
        lambda target, *_a, **_k: Prediction() if target == "correspondent" else None,
    )
    upload(Client(), token, text_pdf(INVOICE_LINES))
    process_all()
    doc = Document.objects.get()
    assert doc.correspondent and "Stadtwerke Musterstadt" in doc.correspondent.name


# --- Model answers ---------------------------------------------------------------


def test_model_answer_is_parsed_and_checked(settings, monkeypatch):
    settings.OLLAMA_URL = "http://ollama:11434"
    sent: list[dict] = []

    def request(path, payload=None, *, timeout):
        sent.append(payload)
        return {
            "message": {
                "content": '{"sender": "Iain Schmidt", "recipient": "Herrn Iain Schmidt", '
                '"title": "  Erweitertes Führungszeugnis ", "document_date": "2024-01-05", '
                '"document_type": "certificate"}'
            }
        }

    monkeypatch.setattr("apps.analysis.ai._request", request)
    fields = ai.analyze(
        "qwen3-vl:8b",
        images=[b"png"],
        text="Bundesamt für Justiz",
        types=[ai.DocumentType("notice", "Notice"), ai.DocumentType("other", "Other")],
    )
    assert fields.sender is None  # the recipient is never the sender
    assert fields.title == "Erweitertes Führungszeugnis"
    assert fields.document_date == date(2024, 1, 5)
    assert fields.document_type is None  # not one of the offered types
    payload = sent[0]
    assert payload["format"]["properties"]["document_type"]["enum"] == ["notice", "other", None]
    assert payload["messages"][1]["images"] == ["cG5n"]
    assert payload["options"]["temperature"] == 0


def test_models_without_reasoning_mode_are_retried_without_it(settings, monkeypatch):
    settings.OLLAMA_URL = "http://ollama:11434"
    sent: list[dict] = []

    def request(path, payload=None, *, timeout):
        sent.append(dict(payload))
        if "think" in payload:
            raise ai.ModelFailed('Ollama error 400: "gemma3:12b" does not support thinking')
        return {"message": {"content": '```json\n{"sender": "Finanzamt Delmenhorst"}\n```'}}

    monkeypatch.setattr("apps.analysis.ai._request", request)
    fields = ai.analyze("gemma3:12b", images=[], text="", types=[])
    assert fields.sender == "Finanzamt Delmenhorst"
    assert len(sent) == 2 and "think" not in sent[1]


def test_implausible_dates_are_dropped():
    fields = ai._fields({"document_date": "1890-01-01", "sender": "unknown"}, "m", set())
    assert fields.document_date is None and fields.sender is None


# --- Settings ----------------------------------------------------------------------


def test_ai_settings_explain_a_missing_ollama_server(api, settings):
    settings.OLLAMA_URL = ""
    r = api.get("/api/v1/settings/ai")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False and body["reachable"] is False
    assert "DOCNEST_OLLAMA_URL" in body["error"]
    assert body["suggestions"][0]["name"] == "qwen3-vl:4b-instruct"


def test_ai_model_can_be_chosen_and_downloaded(api, settings, monkeypatch):
    settings.OLLAMA_URL = "http://ollama:11434"
    monkeypatch.setattr(
        "apps.analysis.ai.list_models",
        lambda: [
            ai.ModelInfo(
                name="qwen3-vl:8b-instruct",
                size=6_100_000_000,
                parameter_size="8.8B",
                vision=True,
                thinking=False,
            )
        ],
    )
    r = api.put("/api/v1/settings/ai", {"model": "qwen3-vl:8b-instruct"}, content_type="application/json")
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "qwen3-vl:8b-instruct" and body["reachable"] is True
    assert body["models"][0]["vision"] is True
    assert body["suggestions"][1]["installed"] is True

    assert (
        api.put("/api/v1/settings/ai", {"model": "bad name"}, content_type="application/json").status_code
        == 422
    )

    pulled: list[str] = []

    def pull(name, progress=None):
        progress("pulling manifest", 0, 0)
        pulled.append(name)

    monkeypatch.setattr("apps.analysis.ai.pull", pull)
    r = api.post("/api/v1/settings/ai/pull", {"model": "gemma3:12b"}, content_type="application/json")
    assert r.status_code == 200 and r.json()["pull"]["model"] == "gemma3:12b"
    assert r.json()["pull"]["active"] is True
    r = api.post("/api/v1/settings/ai/pull", {"model": "gemma3:4b"}, content_type="application/json")
    assert r.status_code == 409  # one download at a time

    process_all()
    assert pulled == ["gemma3:12b"]
    state = SystemState.objects.get(key="ai_model_pull").value
    assert state["status"] == "done"
    assert api.get("/api/v1/settings/ai").json()["pull"]["active"] is False

    r = api.put("/api/v1/settings/ai", {"model": ""}, content_type="application/json")
    assert r.json()["model"] == ""  # switched off


def test_thinking_model_that_never_answers_is_explained(settings, monkeypatch):
    settings.OLLAMA_URL = "http://ollama:11434"
    monkeypatch.setattr(
        "apps.analysis.ai._request",
        lambda *_a, **_k: {
            "message": {"content": "", "thinking": "Okay, let's see"},
            "done_reason": "length",
        },
    )
    with pytest.raises(ai.ModelFailed, match="instruct"):
        ai.analyze("qwen3-vl:8b", images=[], text="x", types=[])


def test_every_document_can_be_reprocessed_at_once(scanner, api, monkeypatch, model):
    _, token = scanner
    fake_model(monkeypatch, ai.ModelFields(sender="Wrong Sender GmbH"))
    for month in ("Januar", "Februar"):
        upload(Client(), token, text_pdf([*INVOICE_LINES, f"Abrechnung {month}"]))
    process_all()

    fake_model(monkeypatch, ai.ModelFields(sender="Energie Nord AG"))
    r = api.post("/api/v1/documents/reprocess-all", {"steps": ["analyze"]}, content_type="application/json")
    assert r.status_code == 200 and r.json()["updated"] == 2
    r = api.post("/api/v1/documents/reprocess-all", {"steps": ["analyze"]}, content_type="application/json")
    assert r.json()["updated"] == 0  # already queued
    process_all()
    assert {d.correspondent.name for d in Document.objects.all()} == {"Energie Nord AG"}


@pytest.mark.parametrize(
    ("answer", "sender"),
    [
        ("Versicherer im Raum der Kirchen, Doktorweg 2-4, 32756 Detmold", "Versicherer im Raum der Kirchen"),
        ("RME GmbH · Brüggeweg 54 · 28309 Bremen", "RME GmbH"),
        (
            "Johanniter-Unfall-Hilfe e.V., Ortsverband Delmenhorst",
            "Johanniter-Unfall-Hilfe e.V., Ortsverband Delmenhorst",
        ),
        ("Bundesamt für Justiz", "Bundesamt für Justiz"),
    ],
)
def test_sender_address_is_dropped(answer, sender):
    assert ai._fields({"sender": answer}, "m", set()).sender == sender


def test_a_correction_teaches_how_the_sender_is_spelled(scanner, api, monkeypatch, model):
    _, token = scanner
    fake_model(monkeypatch, ai.ModelFields(sender="RWM GmbH"))  # the payroll service's return address
    first = upload(Client(), token, text_pdf([*INVOICE_LINES, "Juni"])).json()["id"]
    process_all()
    rme = Correspondent.objects.create(name="RME GmbH")
    r = api.patch(f"/api/v1/documents/{first}", {"correspondent_id": rme.pk}, content_type="application/json")
    assert r.status_code == 200
    rme.refresh_from_db()
    assert rme.aliases == ["RWM GmbH"]
    assert Document.objects.get(uuid=first).correspondent == rme  # the corrected document stays
    assert not Correspondent.objects.filter(name="RWM GmbH").exists()  # the misreading's leftover

    upload(Client(), token, text_pdf([*INVOICE_LINES, "Juli"]))
    process_all()
    latest = Document.objects.order_by("-id").first()
    assert latest and latest.correspondent == rme


def test_a_different_sender_is_not_learned_as_a_spelling(scanner, api, monkeypatch, model):
    _, token = scanner
    fake_model(monkeypatch, ai.ModelFields(sender="Stadt Delmenhorst"))
    ids = [upload(Client(), token, text_pdf([*INVOICE_LINES, m])).json()["id"] for m in ("Juni", "Juli")]
    process_all()
    justiz = Correspondent.objects.create(name="Bundesamt für Justiz")
    api.patch(f"/api/v1/documents/{ids[0]}", {"correspondent_id": justiz.pk}, content_type="application/json")
    justiz.refresh_from_db()
    # "Stadt Delmenhorst" still has a document of its own: a real, different sender.
    assert justiz.aliases == []


def test_a_bare_logo_as_sender_yields_to_the_known_letterhead(scanner, monkeypatch, model):
    _, token = scanner
    known = Correspondent.objects.create(name="Stadtwerke Musterstadt GmbH")
    fake_model(monkeypatch, ai.ModelFields(sender="swm"))  # what a small model reads off a logo
    upload(Client(), token, text_pdf(INVOICE_LINES))
    process_all()
    assert Document.objects.get().correspondent == known


def test_title_is_asked_for_in_german(settings, monkeypatch):
    settings.OLLAMA_URL = "http://ollama:11434"
    sent: list[dict] = []

    def request(path, payload=None, *, timeout):
        sent.append(payload)
        return {"message": {"content": '{"german_title": "Erweitertes Führungszeugnis"}'}}

    monkeypatch.setattr("apps.analysis.ai._request", request)
    fields = ai.analyze("m", images=[], text="Enhanced Certificate of Conduct", types=[])
    assert fields.title == "Erweitertes Führungszeugnis"
    assert "german_title" in sent[0]["format"]["required"]


def test_ai_picks_existing_tags_and_may_suggest_new_ones(scanner, api, monkeypatch, model):
    from apps.documents.models import DocumentTag
    from apps.taxonomy.models import Tag

    _, token = scanner
    Tag.objects.create(name="Energie")
    calls = fake_model(monkeypatch, ai.ModelFields(sender="Energie Nord AG", tags=["energie", "Haushalt"]))
    doc_id = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    process_all()

    assert "Energie" in calls[0]["tags"]
    doc = Document.objects.get(uuid=doc_id)
    assert {t.name for t in doc.tags.all()} == {"Energie", "Haushalt"}  # the existing spelling is reused
    assert Tag.objects.get(name="Haushalt").is_suggested  # new: a suggestion to confirm

    # The user removes one; reanalysis replaces the automatic tags but never brings that one back.
    energie = Tag.objects.get(name="Energie")
    api.patch(f"/api/v1/documents/{doc_id}", {"tag_ids": [energie.pk]}, content_type="application/json")
    fake_model(monkeypatch, ai.ModelFields(sender="Energie Nord AG", tags=["Haushalt", "Strom"]))
    api.post(f"/api/v1/documents/{doc_id}/reprocess", {"steps": ["analyze"]}, content_type="application/json")
    process_all()
    assert {t.name for t in doc.tags.all()} == {"Energie", "Strom"}
    assert DocumentTag.objects.get(document=doc, tag=energie).source == "user"


def test_deleted_tags_are_not_recreated_by_the_ai(scanner, api, monkeypatch, model):
    from apps.taxonomy.models import Tag

    _, token = scanner
    unwanted = Tag.objects.create(name="Sonstiges")
    api.delete(f"/api/v1/tags/{unwanted.pk}")
    fake_model(monkeypatch, ai.ModelFields(sender="Energie Nord AG", tags=["Sonstiges"]))
    upload(Client(), token, text_pdf(INVOICE_LINES))
    process_all()
    assert not Tag.objects.filter(name="Sonstiges").exists()
    assert not Document.objects.get().tags.exists()


def test_model_tags_are_cleaned():
    fields = ai._fields(
        {"tags": ["Steuer", " steuer ", "", None, "A" * 80, "Auto", "Haus", "Bank"]}, "m", set()
    )
    assert fields.tags == ["Steuer", "A" * 40, "Auto", "Haus"]


def test_a_crashed_model_is_retried_instead_of_guessing_with_rules(scanner, monkeypatch, model):
    _, token = scanner
    fake_model(monkeypatch, ai.ModelCrashed("The AI model stopped while reading (unexpected EOF)"))
    upload(Client(), token, text_pdf(INVOICE_LINES))
    process_all()

    doc = Document.objects.get()
    assert doc.processing_state == Document.State.PENDING  # retried later, not analysed with rules
    assert "stopped while reading" in doc.processing_error
    assert doc.correspondent is None

    fake_model(monkeypatch, ai.ModelFields(sender="Energie Nord AG", title="Stromrechnung Februar 2026"))
    Job.objects.update(run_after=timezone.now())
    process_all()
    doc.refresh_from_db()
    assert doc.processing_state == Document.State.DONE
    assert crypto_fields.get_extracted(doc)["analysis"] == {"by": "ai", "model": "qwen3-vl:8b"}


def test_how_a_document_was_read_is_kept(scanner, monkeypatch, model):
    _, token = scanner
    fake_model(monkeypatch, ai.ModelFailed("The model did not answer with JSON"))
    upload(Client(), token, text_pdf(INVOICE_LINES))
    process_all()
    how = crypto_fields.get_extracted(Document.objects.get())["analysis"]
    assert how == {"by": "rules", "model": "qwen3-vl:8b", "problem": "The model did not answer with JSON"}


def test_ai_titles_leave_the_sender_to_its_own_field(scanner, monkeypatch, model):
    _, token = scanner
    fake_model(
        monkeypatch,
        ai.ModelFields(
            sender="RME GmbH", title="Verdienstabrechnung RME GmbH Juni 2026", document_date=date(2026, 6, 25)
        ),
    )
    upload(Client(), token, text_pdf(INVOICE_LINES))
    process_all()
    doc = Document.objects.get()
    assert crypto_fields.get_title(doc) == "Verdienstabrechnung Juni 2026"
    assert doc.correspondent and doc.correspondent.name == "RME GmbH"
