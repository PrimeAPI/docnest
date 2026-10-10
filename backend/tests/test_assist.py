from datetime import date

import pytest

from apps.analysis import ai
from apps.assist import tasks
from apps.assist.models import AssistTask
from apps.processing.preferences import set_ai_model
from apps.processing.worker import Worker
from tests.test_filing import folder, make_doc

pytestmark = pytest.mark.django_db
J = "application/json"


@pytest.fixture
def model(settings):
    settings.OLLAMA_URL = "http://ollama:11434"
    set_ai_model("qwen3-vl:4b-instruct")


def payslips():
    work = folder("Work")
    titles = ["Abrechnung", "Verdienstabrechnung 25", "Verdienstabrechnungsbeleg", "Gehaltsabrechnung 03/26"]
    days = [date(2025, 1, 28), date(2025, 2, 28), date(2025, 3, 28), date(2026, 3, 28)]
    return [
        make_doc(t, work, sender="ACME GmbH", type_slug="statement", day=d)
        for t, d in zip(titles, days, strict=True)
    ]


def run(operation, docs, instruction=""):
    task = AssistTask.objects.create(
        operation=operation,
        documents=[str(d.uuid) for d in docs],
        instruction_enc=tasks.encrypt_instruction(instruction),
    )
    tasks.run(task.pk)
    task.refresh_from_db()
    return task, tasks.result_of(task)


def changes(result):
    return {
        g["label"]: {i["new"] if isinstance(i["new"], str) else i["new"][0]: i["checked"] for i in g["items"]}
        for g in result["groups"]
    }


# --- Patterns ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wish", "pattern"),
    [
        ("Verdienstabrechnung YYYY/MM bitte", "Verdienstabrechnung YYYY/MM"),
        ("Benenne alle einheitlich als Verdienstabrechnung YYYY/MM", "Verdienstabrechnung YYYY/MM"),
        ("Benenne sie nach dem Muster Gehalt MMMM YYYY", "Gehalt MMMM YYYY"),
        ("Name them Payslip YYYY-MM please", "Payslip YYYY-MM"),
        ('rename to "Lohn MM.YYYY"', "Lohn MM.YYYY"),
        ("alle einheitlich, kurz", None),
        ("YYYY", None),
    ],
)
def test_a_pattern_the_user_wrote_is_found_in_their_words(wish, pattern):
    assert tasks.written_pattern(wish) == pattern


def test_patterns_are_filled_with_the_document_date():
    assert tasks.fill_pattern("Gehalt MMMM YYYY", date(2026, 3, 1)) == "Gehalt März 2026"
    assert tasks.fill_pattern("Beleg DD.MM.YYYY", date(2026, 3, 1)) == "Beleg 01.03.2026"
    assert tasks.fill_pattern("Gehalt YYYY", None) is None
    assert tasks.fill_pattern("Arbeitsvertrag", None) == "Arbeitsvertrag"
    monthly = [date(2025, 1, 28), date(2025, 2, 28)]
    assert tasks.without_day("Verdienstabrechnung YYYY-MM-DD", monthly) == "Verdienstabrechnung YYYY-MM"
    assert tasks.without_day("X YYYY-MM-DD", [date(2025, 1, 1), date(2025, 1, 5)]) == "X YYYY-MM-DD"
    assert tasks.numbered({"a": "X 2026-03", "b": "X 2026-03", "c": "X 2026-04"}) == {
        "a": "X 2026-03 (1)",
        "b": "X 2026-03 (2)",
        "c": "X 2026-04",
    }


# --- Consistent names ---------------------------------------------------------------------------


def test_the_model_gives_one_pattern_per_group_and_lone_documents_stay(model, monkeypatch):
    slips = payslips()
    lone = make_doc("Kantinenplan", folder("Work"), day=date(2026, 1, 1))
    asked = []

    def pattern(model, documents, *, wish=""):
        asked.append([d.title for d in documents])
        return "Verdienstabrechnung YYYY-MM-DD"  # the day is dropped: one document per month

    monkeypatch.setattr(ai, "title_pattern", pattern)
    task, result = run("rename", [*slips, lone])

    assert task.state == "done" and (task.done, task.total) == (1, 1)
    assert len(asked) == 1 and "Kantinenplan" not in asked[0]
    assert changes(result) == {
        "Pattern “Verdienstabrechnung YYYY-MM”": {
            "Verdienstabrechnung 2025-01": True,
            "Verdienstabrechnung 2025-02": True,
            "Verdienstabrechnung 2025-03": True,
            "Verdienstabrechnung 2026-03": True,
        }
    }


def test_a_written_pattern_needs_no_model(model, monkeypatch):
    monkeypatch.setattr(ai, "title_pattern", lambda *a, **k: pytest.fail("no model call"))
    task, result = run("rename", payslips(), "Bitte als Verdienstabrechnung YYYY/MM")
    assert task.total == 0
    assert list(changes(result)["Pattern “Verdienstabrechnung YYYY/MM”"]) == [
        "Verdienstabrechnung 2025/01",
        "Verdienstabrechnung 2025/02",
        "Verdienstabrechnung 2025/03",
        "Verdienstabrechnung 2026/03",
    ]


def test_without_a_model_names_come_from_the_titles():
    work = folder("Work")
    docs = [make_doc(f"Kontoauszug {i}", work, sender="Bank", day=date(2026, i, 1)) for i in (1, 2)]
    _, result = run("rename", docs)
    assert changes(result) == {
        "Pattern “Kontoauszug YYYY-MM”": {"Kontoauszug 2026-01": True, "Kontoauszug 2026-02": True}
    }
    assert "No AI model" in result["note"]


# --- Custom ------------------------------------------------------------------------------------


def test_rules_tick_the_documents_the_wish_names(model, monkeypatch):
    slips = payslips()
    power = make_doc("Jahresabrechnung Strom", None, sender="Stadtwerke Delmenhorst", type_slug="invoice")
    menu = make_doc("Kantinenplan", None, sender="ACME GmbH")
    rules = [
        ai.EditRule("tag", "Energie"),
        ai.EditRule("tag", "Gehalt"),
        ai.EditRule("document_type", "Rechnung"),
    ]
    monkeypatch.setattr(ai, "read_edit_rules", lambda model, wish: rules)

    task, result = run(
        "custom",
        [*slips, power, menu],
        "Stadtwerke bekommt das Tag Energie und die Gehaltsabrechnungen das Tag Gehalt, Typ Rechnung",
    )

    assert task.state == "done" and task.total == 1
    ticked = {g["label"]: sorted(i["document"] for i in g["items"] if i["checked"]) for g in result["groups"]}
    assert ticked["Add tag “Energie”"] == [str(power.uuid)]
    # "Gehaltsabrechnungen" names one payslip, and so all that belong with it.
    assert ticked["Add tag “Gehalt”"] == sorted(str(d.uuid) for d in slips)
    # Every selected document is listed, to be ticked by hand.
    assert all(len(g["items"]) == 6 for g in result["groups"])
    assert "no document type “Rechnung”" in result["note"]


def test_unclear_documents_are_left_unticked_and_all_means_all(model, monkeypatch):
    slips = payslips()
    monkeypatch.setattr(ai, "read_edit_rules", lambda model, wish: [ai.EditRule("sender", "ACME Payroll")])
    _, result = run("custom", slips, "Absender der Lohnzettel ist ACME Payroll")
    [group] = result["groups"]
    assert not any(i["checked"] for i in group["items"]) and "unclear" in result["note"]

    monkeypatch.setattr(ai, "read_edit_rules", lambda model, wish: [ai.EditRule("tag", "Archiv")])
    _, result = run("custom", slips, "Alle bekommen das Tag Archiv")
    assert all(i["checked"] for i in result["groups"][0]["items"])


def test_custom_needs_a_model():
    task, result = run("custom", payslips(), "Tag Gehalt")
    assert task.state == "done" and result["groups"] == [] and "need an AI model" in result["note"]


def test_edit_rules_only_keep_what_the_wish_says(monkeypatch):
    answer = {
        "rules": [
            {"change": "document_type", "value": "Tag Energie"},  # the word before says: a tag
            {"change": "tag", "value": "Telefon"},  # not in the wish
            {"change": "sender", "value": "ACME GmbH"},
            {"change": "sender", "value": "ACME GmbH"},  # twice
            {"change": "nonsense", "value": "x"},
        ]
    }
    monkeypatch.setattr(ai, "_chat", lambda payload: answer)
    rules = ai.read_edit_rules("m", "Stadtwerke bekommt das Tag Energie, Absender ACME GmbH")
    assert rules == [ai.EditRule("tag", "Energie"), ai.EditRule("sender", "ACME GmbH")]


# --- API ---------------------------------------------------------------------------------------


def test_a_task_through_the_api_proposes_but_changes_nothing(api, model, monkeypatch):
    slips = payslips()
    monkeypatch.setattr(ai, "title_pattern", lambda *a, **k: "Verdienstabrechnung YYYY-MM")
    ids = [str(d.uuid) for d in slips]
    r = api.post(
        "/api/v1/assist/tasks", {"ids": ids, "operation": "rename", "instruction": "Lohn"}, content_type=J
    )
    assert r.status_code == 200 and r.json()["state"] == "pending"
    task_id = r.json()["id"]
    assert b"Lohn" not in bytes(AssistTask.objects.get().instruction_enc)  # encrypted

    Worker().run_until_empty()

    body = api.get(f"/api/v1/assist/tasks/{task_id}").json()
    assert body["state"] == "done"
    [group] = body["groups"]
    assert group["field"] == "title" and len(group["items"]) == 4
    item = group["items"][0]
    assert item["old"] == "Abrechnung" and item["new"] == "Verdienstabrechnung 2025-01" and item["checked"]
    assert item["document"]["title"] == "Abrechnung"  # nothing renamed yet

    assert (
        api.post("/api/v1/assist/tasks", {"ids": ids, "operation": "custom"}, content_type=J).status_code
        == 400
    )


def test_a_type_change_comes_with_the_type_to_set(api, model, monkeypatch):
    menu = make_doc("Kantinenplan", None, sender="ACME GmbH")
    monkeypatch.setattr(ai, "read_edit_rules", lambda model, wish: [ai.EditRule("document_type", "other")])
    r = api.post(
        "/api/v1/assist/tasks",
        {"ids": [str(menu.uuid)], "operation": "custom", "instruction": "Typ other"},
        content_type=J,
    )
    Worker().run_until_empty()
    [group] = api.get(f"/api/v1/assist/tasks/{r.json()['id']}").json()["groups"]
    assert group["field"] == "document_type" and group["document_type_id"] is not None
    assert group["items"][0]["new"] == "Other"


def test_a_running_task_can_be_cancelled(api, model, monkeypatch):
    slips = payslips()
    task = AssistTask.objects.create(operation="rename", documents=[str(d.uuid) for d in slips])

    def pattern(*args, **kwargs):
        api.post(f"/api/v1/assist/tasks/{task.pk}/cancel")
        return "X YYYY"

    monkeypatch.setattr(ai, "title_pattern", pattern)
    tasks.run(task.pk)
    task.refresh_from_db()
    assert task.state == "cancelled" and task.result_enc is None
