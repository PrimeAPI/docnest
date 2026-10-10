"""The overnight review: code finds, the model reads and argues, the user gets a report."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.utils import timezone
from freezegun import freeze_time

from apps.analysis import ai
from apps.assist import notes, review, tasks
from apps.assist.models import AssistTask, DocumentNote
from apps.documents import crypto_fields
from apps.documents.models import Document, DocumentPage
from apps.processing import pipeline
from apps.processing.models import Job
from apps.processing.preferences import set_ai_model
from apps.taxonomy.models import Correspondent, Folder
from tests.test_alterations import letter, make

pytestmark = pytest.mark.django_db
J = "application/json"


@pytest.fixture
def model(settings):
    settings.OLLAMA_URL = "http://ollama:11434"
    set_ai_model("qwen3-vl:8b-instruct")


class FakeModel:
    """Answers by the question's schema; the exploration follows a script."""

    def __init__(self, steps: list[dict] | None = None, verdict: str = "keep") -> None:
        self.steps = list(steps or [{"thought": "done", "action": "finish", "memo": ""}])
        self.verdict = verdict
        self.prompts: list[str] = []

    def __call__(self, model, prompt, schema, **kwargs):
        self.prompts.append(prompt)
        keys = set(schema["properties"])
        if "what" in keys:
            return {
                "what": "Eine Rechnung der Telekom",
                "sender": "Telekom",
                "date": "2026-03-01",
                "references": ["555"],
                "complete": "yes",
                "why": "ends with a greeting",
                "title": "Telekom Rechnung",
            }
        if "objections" in keys:
            return {"objections": ["The dates could differ."], "strength": "weak"}
        if "reply" in keys:
            return {"reply": "Same customer number and consecutive page marks."}
        if "verdict" in keys:
            return {"verdict": self.verdict, "reason": "The page marks fit."}
        if "plan" in keys:
            return {"plan": ["Look at the names", "Check the senders"]}
        if "action" in keys:
            return self.steps.pop(0) if self.steps else {"thought": "", "action": "finish", "memo": ""}
        if "summary" in keys:
            return {"summary": "Two scans of one Telekom bill.", "next_steps": ["Merge D1 and D2"]}
        raise AssertionError(f"unexpected question: {keys}")


def start(docs, **options) -> AssistTask:
    return AssistTask.objects.create(
        operation=AssistTask.Operation.REVIEW,
        documents=[str(d.uuid) for d in docs],
        instruction_enc=tasks.encrypt_instruction("Ordne diesen Ordner"),
        options=options,
    )


def bill():
    docs = [
        make("Rechnung Teil 1", [letter("Telekom", "555", 1, 2)]),
        make("Rechnung Teil 2", [letter("Telekom", "555", 2, 2)]),
    ]
    telekom = Correspondent.objects.get_or_create(name="Telekom")[0]
    for d in docs:
        d.correspondent = telekom
        d.document_date = date(2026, 3, 1)
        d.save()
    return docs


def test_review_context_can_use_less_memory_than_the_system_default(monkeypatch, settings):
    settings.AI_CONTEXT_TOKENS = 16384
    requests = []

    def chat(payload, **kwargs):
        requests.append(payload)
        return {}

    monkeypatch.setattr(ai, "_chat", chat)
    ai.ask("small-model", "Question", {}, context=4096)
    ai.ask("small-model", "Question", {})
    assert [r["options"]["num_ctx"] for r in requests] == [4096, 16384]


def test_without_the_model_code_findings_make_the_report():
    task = start(bill(), ai=False)
    review.run(task.pk)
    task.refresh_from_db()
    assert task.state == AssistTask.State.DONE, task.error
    result = tasks.result_of(task)
    assert [f["kind"] for f in result["findings"]] == ["split"]
    assert result["summary"] == ""
    assert any("without the AI model" in j["text"] for j in review.journal_of(task))


def test_the_model_reads_argues_explores_and_reports(model, monkeypatch):
    fake = FakeModel(
        steps=[
            {"thought": "Read the first part", "action": "read", "doc": "D1", "memo": "D1 is a bill"},
            {
                "thought": "The title should say it is from the Telekom",
                "action": "propose",
                "kind": "rename",
                "documents": ["D2"],
                "value": "Telekom Rechnung März 2026",
                "reason": "The title does not name the sender",
                "memo": "proposed a title",
            },
            {"thought": "Nothing else", "action": "finish", "memo": ""},
        ]
    )
    monkeypatch.setattr(ai, "ask", fake)
    monkeypatch.setattr(ai, "title_pattern", lambda *a, **k: None)
    docs = bill()
    task = start(docs)
    review.run(task.pk)
    task.refresh_from_db()
    assert task.state == AssistTask.State.DONE, task.error
    result = tasks.result_of(task)
    split, renamed = result["findings"]
    assert split["kind"] == "split" and split["verdict"] == "keep"
    assert [d["role"] for d in split["debate"]] == ["against", "for", "verdict"]
    assert renamed["source"] == "model" and renamed["verdict"] == "keep"
    assert renamed["action"]["groups"][0]["items"][0]["new"] == "Telekom Rechnung März 2026"
    assert result["summary"] == "Two scans of one Telekom bill."
    assert result["next_steps"] == ["Merge “Rechnung Teil 1” and “Rechnung Teil 2”"]  # refs made readable
    # The notes stay for the next run, which does not read the documents again.
    assert DocumentNote.objects.count() == 2
    assert notes.load(docs[0], "x") is None  # another text: read again
    reads = sum("Read this document closely" in p for p in fake.prompts)
    task2 = start(docs, explore=False)
    review.run(task2.pk)
    assert sum("Read this document closely" in p for p in fake.prompts) == reads


def test_a_dropped_finding_stays_visible_with_its_arguments(model, monkeypatch):
    monkeypatch.setattr(ai, "ask", FakeModel(verdict="drop"))
    monkeypatch.setattr(ai, "title_pattern", lambda *a, **k: None)
    task = start(bill(), explore=False)
    review.run(task.pk)
    task.refresh_from_db()
    [split] = tasks.result_of(task)["findings"]
    assert split["verdict"] == "drop" and split["debate"]


def test_a_model_away_defers_and_the_run_resumes_from_its_checkpoint(model, monkeypatch):
    calls = {"n": 0}
    fake = FakeModel()

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ai.ModelUnavailable("restarting")
        return fake(*args, **kwargs)

    monkeypatch.setattr(ai, "ask", flaky)
    monkeypatch.setattr(ai, "title_pattern", lambda *a, **k: None)
    task = start(bill(), explore=False)
    with pytest.raises(pipeline.AnalysisModelUnavailable):
        review.run(task.pk)
    task.refresh_from_db()
    assert task.state == AssistTask.State.RUNNING
    review.run(task.pk)  # the worker tries again later
    task.refresh_from_db()
    assert task.state == AssistTask.State.DONE
    journal = [j["text"] for j in review.journal_of(task)]
    assert sum(t.startswith("Compared the documents page by page") for t in journal) == 1  # not again


def test_time_up_or_stop_writes_the_report_with_what_there_is(model, monkeypatch):
    monkeypatch.setattr(ai, "ask", FakeModel())
    task = start(bill(), until=(timezone.now() - timedelta(minutes=1)).isoformat())
    review.run(task.pk)
    task.refresh_from_db()
    result = tasks.result_of(task)
    assert task.state == AssistTask.State.DONE
    assert result["stopped"].startswith("Time is up") and result["findings"]


def test_reviews_through_the_api(api, model, monkeypatch):
    docs = bill()
    later = timezone.now() + timedelta(hours=3)
    r = api.post(
        "/api/v1/assist/reviews",
        {
            "ids": [str(d.uuid) for d in docs],
            "instruction": "Ordne das",
            "think": True,
            "start": later.isoformat(),
            "until": (later + timedelta(hours=6)).isoformat(),
        },
        content_type=J,
    )
    assert r.status_code == 200, r.content
    summary = r.json()
    assert summary["state"] == "pending" and summary["think"] and summary["documents"] == 2
    job = Job.objects.get(kind=Job.Kind.REVIEW)
    assert job.run_after > timezone.now() + timedelta(hours=2)

    monkeypatch.setattr(ai, "ask", FakeModel())
    monkeypatch.setattr(ai, "title_pattern", lambda *a, **k: None)
    review.run(summary["id"])
    report = api.get(f"/api/v1/assist/reviews/{summary['id']}").json()
    [finding] = report["findings"]
    assert finding["compose"]["outputs"][0]["pages"] == [
        {"document": str(docs[0].uuid), "page": 1},
        {"document": str(docs[1].uuid), "page": 1},
    ]
    assert finding["documents"][0]["what"] == "Eine Rechnung der Telekom"
    assert report["open_findings"] == 1 and report["journal"]

    r = api.post(
        f"/api/v1/assist/reviews/{summary['id']}/findings/F1", {"decision": "applied"}, content_type=J
    )
    assert r.json()["open_findings"] == 0
    assert api.post(f"/api/v1/assist/reviews/{summary['id']}/read").json()["read"]
    assert [x["id"] for x in api.get("/api/v1/assist/reviews").json()] == [summary["id"]]


def test_a_review_of_everything_and_stopping_one_not_started(api, model):
    bill()
    r = api.post("/api/v1/assist/reviews", {"scope": "all", "ai": False}, content_type=J)
    assert r.json()["documents"] == 2
    later = (timezone.now() + timedelta(hours=1)).isoformat()
    r = api.post("/api/v1/assist/reviews", {"scope": "all", "start": later}, content_type=J)
    stopped = api.post(f"/api/v1/assist/reviews/{r.json()['id']}/stop").json()
    assert stopped["state"] == "cancelled"


def test_folder_review_snapshots_every_document_beyond_preview_and_bulk_limits(api):
    work = Folder.objects.create(name="Large folder")
    docs = Document.objects.bulk_create(
        [Document(folder=work, content_hash=f"folder-{n}", processing_state="done") for n in range(551)]
    )
    outsider = Document.objects.create(content_hash="outside")
    trashed = Document.objects.create(folder=work, content_hash="trashed", deleted_at=timezone.now())
    response = api.post(
        "/api/v1/assist/reviews", {"scope": "folder", "folder_id": work.pk, "ai": False}, content_type=J
    )
    assert response.status_code == 200, response.content
    assert response.json()["documents"] == 551 and response.json()["until"] is None
    task = AssistTask.objects.get(pk=response.json()["id"])
    assert set(task.documents) == {str(d.uuid) for d in docs}
    assert str(outsider.uuid) not in task.documents and str(trashed.uuid) not in task.documents
    Document.objects.create(folder=work, content_hash="uploaded-after-scheduling")
    review.run(task.pk)
    task.refresh_from_db()
    assert task.state == "done", task.error
    assert tasks.result_of(task)["documents"] == 551


@pytest.mark.parametrize("subfolders", [False, True])
def test_folder_scope_honors_subfolders_and_exclusions(api, subfolders):
    parent = Folder.objects.create(name="Parent")
    child = Folder.objects.create(name="Child", parent=parent)
    grandchild = Folder.objects.create(name="Grandchild", parent=child)
    direct = Document.objects.create(folder=parent, content_hash="direct")
    omitted = Document.objects.create(folder=parent, content_hash="omitted")
    nested = Document.objects.create(folder=grandchild, content_hash="nested")
    response = api.post(
        "/api/v1/assist/reviews",
        {
            "scope": "folder",
            "folder_id": parent.pk,
            "subfolders": subfolders,
            "excluded_ids": [str(omitted.uuid)],
            "ai": False,
        },
        content_type=J,
    )
    assert response.status_code == 200, response.content
    task = AssistTask.objects.get(pk=response.json()["id"])
    assert set(task.documents) == ({str(direct.uuid), str(nested.uuid)} if subfolders else {str(direct.uuid)})


def test_archive_and_selection_reviews_no_longer_truncate_at_5000(api, monkeypatch):
    docs = Document.objects.bulk_create([Document(content_hash=f"archive-{n}") for n in range(5001)])
    monkeypatch.setattr(notes, "load", lambda *args: None)  # no OCR or cached notes in this scope test
    for scope in ("all", "selection"):
        payload = {"scope": scope, "ai": False}
        if scope == "selection":
            payload["ids"] = [str(d.uuid) for d in docs]
        response = api.post("/api/v1/assist/reviews", payload, content_type=J)
        assert response.status_code == 200, response.content
        assert response.json()["documents"] == 5001
        task = AssistTask.objects.get(pk=response.json()["id"])
        run = review.Run(task, "")
        review.load(run)
        assert len(run.items) == 5001 and len(run.documents) == 5001


@pytest.mark.parametrize(
    "payload",
    [
        {"scope": "folder"},
        {"scope": "folder", "folder_id": 987654},
        {"scope": "selection", "folder_id": 987654},
        {"scope": "all", "folder_id": 987654},
    ],
)
def test_invalid_folder_scope_never_falls_back_to_reviewing_everything(api, payload):
    Document.objects.create(content_hash="not-selected")
    response = api.post("/api/v1/assist/reviews", {**payload, "ai": False}, content_type=J)
    assert response.status_code == 400
    assert not AssistTask.objects.exists()


def test_reviews_prepare_existing_documents_without_a_separate_request():
    docs = bill()
    DocumentPage.objects.all().delete()
    task = start(docs, ai=False)
    review.run(task.pk)
    task.refresh_from_db()
    assert task.state == AssistTask.State.DONE, task.error
    assert DocumentPage.objects.count() == 2
    assert any(f["kind"] == "split" for f in tasks.result_of(task)["findings"])


def test_notes_are_invalidated_by_same_length_text_changes_and_model_changes():
    [doc, _] = bill()
    original = crypto_fields.get_content(doc)
    changed = original.replace("Telekom", "Vodafon")
    assert len(original) == len(changed) and original != changed
    notes.save(doc, {"what": "Telekom bill"}, original, "small-model")
    assert notes.load(doc, original, "small-model") == {"what": "Telekom bill"}
    assert notes.load(doc, changed, "small-model") is None
    assert notes.load(doc, original, "larger-model") is None


def test_model_failure_keeps_findings_and_exposes_the_partial_report(api, model, monkeypatch):
    def crash(model, prompt, schema, **kwargs):
        raise ai.ModelCrashed("not enough memory")

    monkeypatch.setattr(ai, "ask", crash)
    monkeypatch.setattr(ai, "title_pattern", lambda *a, **k: None)
    task = start(bill(), explore=False)
    review.run(task.pk)
    task.refresh_from_db()
    assert task.state == AssistTask.State.FAILED
    report = api.get(f"/api/v1/assist/reviews/{task.pk}").json()
    assert report["findings"] and report["stopped"] == "Review failed"
    assert "not enough memory" in report["error"]


def test_deadline_limits_requests_and_writes_a_report_instead_of_deferring(model, monkeypatch):
    calls = []

    def timeout(model, prompt, schema, **kwargs):
        calls.append(kwargs["timeout"])
        frozen.tick(timedelta(seconds=31))
        raise ai.ModelUnavailable("timed out")

    monkeypatch.setattr(ai, "ask", timeout)
    with freeze_time() as frozen:
        task = start(bill(), until=(timezone.now() + timedelta(seconds=30)).isoformat())
        review.run(task.pk)
    task.refresh_from_db()
    assert task.state == AssistTask.State.DONE, task.error
    assert 0 < calls[0] <= 30
    assert tasks.result_of(task)["stopped"].startswith("Time is up")


def test_review_rejects_missing_documents_naive_times_and_unknown_findings(api):
    import uuid

    assert (
        api.post(
            "/api/v1/assist/reviews", {"ids": [str(uuid.uuid4())], "ai": False}, content_type=J
        ).status_code
        == 400
    )
    doc = bill()[0]
    assert (
        api.post(
            "/api/v1/assist/reviews",
            {"ids": [str(doc.uuid)], "ai": False, "start": "2030-01-01T01:00:00"},
            content_type=J,
        ).status_code
        == 422
    )
    task = start([doc], ai=False)
    assert (
        api.post(
            f"/api/v1/assist/reviews/{task.pk}/findings/F999", {"decision": "applied"}, content_type=J
        ).status_code
        == 404
    )


def test_resumed_review_keeps_document_refs_after_metadata_changes():
    docs = bill()
    task = start(docs, ai=False)
    first = review.Run(task, "")
    review.load(first)
    original_refs = {i.uuid: i.ref for i in first.items}
    Document.objects.filter(pk=docs[0].pk).update(document_date=date(2030, 1, 1))
    task.refresh_from_db()
    resumed = review.Run(task, "")
    review.load(resumed)
    assert {i.uuid: i.ref for i in resumed.items} == original_refs
