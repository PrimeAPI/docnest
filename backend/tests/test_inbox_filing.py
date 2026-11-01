from datetime import date, timedelta

import pytest
from django.utils import timezone

from apps.analysis import ai
from apps.documents.models import Document, Source
from apps.processing.models import Job
from apps.processing.preferences import set_ai_model
from apps.taxonomy import inbox_filing
from apps.taxonomy.filing import Tree
from apps.taxonomy.models import FilingSuggestion, Folder
from tests.test_filing import folder, make_doc

pytestmark = pytest.mark.django_db
J = "application/json"
State = FilingSuggestion.State
MONTHS = {1: "Januar", 2: "Februar", 3: "März"}


@pytest.fixture(autouse=True)
def no_ai(settings):
    settings.OLLAMA_URL = ""


@pytest.fixture
def model(settings, monkeypatch):
    """An AI model whose answers the test gives: {first title of a group: path}."""
    settings.OLLAMA_URL = "http://ollama:11434"
    set_ai_model("qwen3-vl:4b-instruct")
    answers: dict[str, str | None] = {}
    seen: list[list[ai.TreeFolder]] = []

    def choose(model_name, *, folders, groups, below="", style=None, allow_new=True):
        seen.append(folders)
        return [answers.get(g.titles[0]) for g in groups]

    monkeypatch.setattr(ai, "choose_folders", choose)
    return answers, seen


def plan() -> None:
    """Run the plan job as the worker would once the batch is processed."""
    inbox_filing.run({"since": (timezone.now() - inbox_filing.MAX_WAIT).isoformat()})


def suggestion(doc: Document) -> FilingSuggestion:
    return FilingSuggestion.objects.get(document=doc)


def target(s: FilingSuggestion) -> str:
    folders = inbox_filing.Folders()
    anchor, new = folders.current(s.anchor_id, s.new)
    return " / ".join([p for p in [folders.path(anchor)] if p] + new)


def filed(title: str, where: Folder, **kwargs) -> Document:
    """A document the user has filed already: out of the inbox."""
    doc = make_doc(title, where, **kwargs)
    Document.objects.filter(pk=doc.pk).update(status=Document.Status.DONE)
    return doc


def insurance_tree() -> Folder:
    insurance = folder("Versicherungen", folder("Privat"))
    filed("Versicherungsschein Hausrat", insurance, sender="VRK", type_slug="contract")
    return insurance


def test_a_new_document_is_suggested_where_its_kind_lies_and_moves_only_when_approved():
    insurance = insurance_tree()
    filed("Beitragsrechnung Hausrat 2025", insurance, sender="VRK", type_slug="invoice")
    new = make_doc("Beitragsrechnung Hausrat 2026", None, sender="VRK", type_slug="invoice")

    plan()

    s = suggestion(new)
    assert s.state == State.PENDING and s.anchor_id == insurance.pk and s.new == []
    new.refresh_from_db()
    assert new.folder_id is None  # nothing moves on its own

    assert inbox_filing.accept([str(new.uuid)]) == (1, 0)
    new.refresh_from_db()
    assert new.folder_id == insurance.pk and new.source_of("folder") == Source.USER
    assert suggestion(new).state == State.ACCEPTED


def test_documents_uploaded_together_get_one_new_folder_together():
    private = folder("Privat")
    filed("Mietvertrag Wohnung", private, sender="Hausverwaltung Kern")
    batch = [
        make_doc(
            f"Kontoauszug {MONTHS[m]} 2026",
            None,
            sender="Sparkasse",
            type_slug="statement",
            day=date(2026, m, 1),
        )
        for m in (1, 2, 3)
    ]

    plan()

    targets = {target(suggestion(d)) for d in batch}
    assert targets == {"Kontoauszug / 2026"}
    # Approving creates the folders once, for all of them.
    assert inbox_filing.accept([str(d.uuid) for d in batch]) == (3, 2)
    assert Folder.objects.filter(name="Kontoauszug").count() == 1


def test_a_later_document_joins_the_folder_proposed_for_earlier_ones():
    first = [
        make_doc(
            f"Beitragsrechnung Kfz {y}", None, sender="HUK-Coburg", type_slug="invoice", day=date(y, 1, 5)
        )
        for y in (2025, 2026)
    ]
    plan()
    assert {target(suggestion(d)) for d in first} == {"Beitragsrechnung"}

    later = make_doc("Beitragsrechnung Kfz Nachtrag", None, sender="HUK-Coburg", type_slug="invoice")
    plan()

    s = suggestion(later)
    assert s.state == State.PENDING and target(s) == "Beitragsrechnung"
    assert "proposed for other new documents" in inbox_filing.reason(s, later)


def test_the_model_sees_proposed_folders_and_its_choice_survives_approving_another(model):
    answers, seen = model
    insurance = insurance_tree()
    a = make_doc("Versicherungsschein Kfz", None, sender="HUK-Coburg", type_slug="contract")
    answers["Versicherungsschein Kfz"] = "Privat/Versicherungen/Kfz"
    plan()
    sa = suggestion(a)
    assert (sa.anchor_id, sa.new, sa.named_by) == (insurance.pk, ["Kfz"], "ai")

    b = make_doc("Schadenmeldung Auffahrunfall", None, sender="Gutachterbüro Nord", type_slug="mail")
    answers["Schadenmeldung Auffahrunfall"] = "Privat/Versicherungen/Kfz"
    plan()
    listed = {f.path: f for f in seen[-1]}
    assert listed["Privat/Versicherungen/Kfz"].proposed and listed["Privat/Versicherungen/Kfz"].count == 1
    assert (suggestion(b).anchor_id, suggestion(b).new) == (insurance.pk, ["Kfz"])

    inbox_filing.accept([str(a.uuid)])
    kfz = Folder.objects.get(name="Kfz", parent=insurance)
    # The folder exists now: the other suggestion goes into it instead of making it again.
    assert inbox_filing.Folders().current(suggestion(b).anchor_id, suggestion(b).new) == (kfz.pk, [])
    assert inbox_filing.accept([str(b.uuid)]) == (1, 0)


def test_a_document_nothing_fitted_gets_a_second_chance_with_the_next_one():
    one = make_doc("Gutachten Wasserschaden", None, sender="Sachverständigenbüro Ost")
    plan()
    assert suggestion(one).state == State.NONE

    two = make_doc("Gutachten Wasserschaden Nachtrag", None, sender="Sachverständigenbüro Ost")
    plan()
    assert target(suggestion(one)) == target(suggestion(two)) == "Gutachten"


def test_the_plan_waits_for_the_rest_of_its_batch():
    done = make_doc("Kontoauszug Januar 2026", None, sender="Sparkasse", type_slug="statement")
    busy = make_doc("Kontoauszug Februar 2026", None, sender="Sparkasse", type_slug="statement")
    Document.objects.filter(pk=busy.pk).update(processing_state=Document.State.RUNNING)

    inbox_filing.run({"since": timezone.now().isoformat()})
    assert not FilingSuggestion.objects.exists()
    waiting = Job.objects.get(kind=Job.Kind.PLAN_FILING, state=Job.State.QUEUED)
    assert waiting.run_after > timezone.now()

    # Not forever: after MAX_WAIT the documents that are ready are planned.
    plan()
    assert suggestion(done).state in (State.PENDING, State.NONE)


def test_the_model_answer_is_checked_against_the_tree():
    private = folder("Privat")
    insurance = folder("Versicherungen", private)
    statements = folder("Kontoauszüge", private)
    folder("2025", statements)
    tree = Tree(exclude=set())

    assert inbox_filing.resolve("Privat/Versicherungen/Kfz", tree, None) == (insurance.pk, ["Kfz"])
    # A path that starts lower in the tree, spelled differently, with a year: all found.
    assert inbox_filing.resolve("Versicherungen/Kfz", tree, None) == (insurance.pk, ["Kfz"])
    assert inbox_filing.resolve("privat/Kontoauszuege/2026", tree, None) == (statements.pk, [])
    # Too much invented, nothing at all, or new folders where none may be made.
    assert inbox_filing.resolve("Neu/Ganz/Tief/Unten", tree, None) is None
    assert inbox_filing.resolve("2026", tree, None) is None
    assert inbox_filing.resolve("Privat/Steuern", tree, None, allow_new=False) is None
    # Below the folder the user chose; repeating its name is fine.
    assert inbox_filing.resolve("Privat/Kfz", tree, private.pk) == (private.pk, ["Kfz"])


def test_scanner_folders_are_left_but_a_folder_the_user_chose_is_kept():
    insurance = insurance_tree()
    filed("Beitragsrechnung Hausrat 2025", insurance, sender="VRK", type_slug="invoice")
    scans = folder("Scans")
    scanned = make_doc("Beitragsrechnung Hausrat 2026", scans, sender="VRK", type_slug="invoice")
    scanned.set_source("folder", Source.SCANNER)
    scanned.save()
    work = folder("Arbeit")
    chosen = make_doc("Beitragsrechnung Hausrat 2024", work, sender="VRK", type_slug="invoice")
    chosen.set_source("folder", Source.USER)
    chosen.save()

    plan()

    assert suggestion(scanned).anchor_id == insurance.pk
    # Its own folder has no subfolders, and it may not leave it for the insurance folder.
    assert suggestion(chosen).state == State.NONE


def test_filing_by_hand_settles_the_suggestion_and_a_dismissed_one_stays_dismissed(api):
    insurance = insurance_tree()
    filed("Beitragsrechnung Hausrat 2025", insurance, sender="VRK", type_slug="invoice")
    a = make_doc("Beitragsrechnung Hausrat 2026", None, sender="VRK", type_slug="invoice")
    b = make_doc("Beitragsrechnung Hausrat 2027", None, sender="VRK", type_slug="invoice")
    c = make_doc("Beitragsrechnung Hausrat 2028", None, sender="VRK", type_slug="invoice")
    plan()

    api.patch(f"/api/v1/documents/{a.uuid}", {"folder_id": insurance.pk}, content_type=J)
    other = folder("Sonstiges")
    api.post(
        "/api/v1/documents/bulk",
        {"ids": [str(b.uuid)], "action": "move", "folder_id": other.pk},
        content_type=J,
    )
    assert suggestion(a).state == State.ACCEPTED  # where it was suggested
    assert suggestion(b).state == State.DISMISSED  # somewhere else

    r = api.post("/api/v1/filing/inbox/decide", {"ids": [str(c.uuid)], "action": "dismiss"}, content_type=J)
    assert r.json()["dismissed"] == 1
    inbox_filing.request(c.pk)  # reprocessed later
    assert suggestion(c).state == State.DISMISSED


def test_the_inbox_shows_suggestions_by_target_and_approves_them_together(api):
    batch = [
        make_doc(
            f"Kontoauszug {MONTHS[m]} 2026",
            None,
            sender="Sparkasse",
            type_slug="statement",
            day=date(2026, m, 1),
        )
        for m in (1, 2, 3)
    ]
    plan()

    failed = make_doc("Unlesbarer Scan", None)
    Document.objects.filter(pk=failed.pk).update(processing_state=Document.State.FAILED)

    data = api.get("/api/v1/filing/inbox").json()
    assert data["enabled"] and data["waiting"] == 0  # a failed document is not waiting for a plan
    [group] = data["groups"]
    assert group["anchor_id"] is None and group["new"] == ["Kontoauszug", "2026"]
    assert len(group["documents"]) == 3 and group["reasons"]
    detail = api.get(f"/api/v1/documents/{batch[0].uuid}").json()
    assert detail["filing_suggestion"]["new"] == ["Kontoauszug", "2026"]

    # Approving somewhere else than suggested.
    elsewhere = folder("Bank")
    r = api.post(
        "/api/v1/filing/inbox/decide",
        {
            "ids": [str(d.uuid) for d in batch],
            "action": "accept",
            "target": {"anchor_id": elsewhere.pk, "new": ["2026"]},
        },
        content_type=J,
    )
    assert r.json() == {"moved": 3, "created": 1, "dismissed": 0}
    assert {d.folder.name for d in Document.objects.filter(pk__in=[d.pk for d in batch])} == {"2026"}
    assert api.get("/api/v1/filing/inbox").json()["groups"] == []
    assert api.get(f"/api/v1/documents/{batch[0].uuid}").json()["filing_suggestion"] is None


def test_switched_off_nothing_is_planned(api):
    api.put("/api/v1/filing/inbox/settings", {"enabled": False}, content_type=J)
    doc = make_doc("Kontoauszug Januar 2026", None, sender="Sparkasse", type_slug="statement")
    inbox_filing.request(doc.pk)
    plan()
    assert not FilingSuggestion.objects.exists()
    assert not Job.objects.filter(kind=Job.Kind.PLAN_FILING).exists()

    api.put("/api/v1/filing/inbox/settings", {"enabled": True}, content_type=J)
    assert Job.objects.filter(kind=Job.Kind.PLAN_FILING, state=Job.State.QUEUED).exists()


def test_a_finished_document_asks_for_a_plan_and_a_reprocessed_one_is_planned_anew():
    doc = make_doc("Kontoauszug Januar 2026", None, sender="Sparkasse", type_slug="statement")
    inbox_filing.request(doc.pk)
    assert Job.objects.filter(kind=Job.Kind.PLAN_FILING, state=Job.State.QUEUED).count() == 1
    inbox_filing.request(doc.pk)  # a plan is waiting already
    assert Job.objects.filter(kind=Job.Kind.PLAN_FILING).count() == 1

    FilingSuggestion.objects.create(document=doc, state=State.PENDING, new=["Alt"])
    inbox_filing.request(doc.pk)
    assert not FilingSuggestion.objects.filter(document=doc).exists()

    old = make_doc("Kontoauszug Februar 2026", None, sender="Sparkasse", type_slug="statement")
    Document.objects.filter(pk=old.pk).update(uploaded_at=timezone.now() - timedelta(days=1))
    assert inbox_filing._awaiting()[0].pk == old.pk  # oldest first
