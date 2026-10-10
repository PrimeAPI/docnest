import uuid
from datetime import date

import pytest

from apps.analysis import ai
from apps.documents import crypto_fields
from apps.documents.models import Document
from apps.processing.preferences import set_ai_model
from apps.processing.worker import Worker
from apps.taxonomy import filing
from apps.taxonomy.models import Correspondent, DocumentType, Folder

pytestmark = pytest.mark.django_db
J = "application/json"


@pytest.fixture(autouse=True)
def no_ai(settings):
    settings.OLLAMA_URL = ""


def make_doc(
    title: str,
    folder: Folder | None,
    *,
    sender: str | None = None,
    type_slug: str | None = None,
    day: date | None = None,
) -> Document:
    doc = Document(
        folder=folder,
        content_hash=uuid.uuid4().hex,
        processing_state="done",
        document_date=day,
        correspondent=Correspondent.objects.get_or_create(name=sender)[0] if sender else None,
        document_type=DocumentType.objects.get(slug=type_slug) if type_slug else None,
    )
    crypto_fields.set_title(doc, title)
    doc.save()
    return doc


def folder(name: str, parent: Folder | None = None) -> Folder:
    return Folder.objects.get_or_create(name=name, parent=parent)[0]


def payslips(work: Folder, year: int, months: range) -> list[Document]:
    names = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli"]
    return [
        make_doc(
            f"Verdienstabrechnung {names[m - 1]} {year}",
            work,
            sender="ACME GmbH",
            type_slug="statement",
            day=date(year, m, 28),
        )
        for m in months
    ]


def suggestion_for(docs: list[Document]) -> filing.Suggestion:
    return filing.suggest([str(d.uuid) for d in docs])


def target(group: filing.Group) -> str:
    anchor = Folder.objects.get(pk=group.anchor_id).name if group.anchor_id else ""
    return "/".join([anchor, *group.new])


def test_loose_documents_go_into_matching_and_new_subfolders():
    work = folder("Work")
    contracts = folder("Arbeitsvertrag", work)
    make_doc("Arbeitsvertrag", contracts, sender="ACME GmbH", type_slug="contract", day=date(2024, 1, 1))
    slips = payslips(work, 2026, range(1, 4))
    change = make_doc(
        "Änderung Arbeitsvertrag", work, sender="ACME GmbH", type_slug="contract", day=date(2026, 3, 1)
    )

    result = suggestion_for([*slips, change])

    targets = {target(g): {d.uuid for d in g.docs} for g in result.groups}
    # Monthly documents get a folder of their own, divided into years.
    assert targets["Work/Verdienstabrechnung/2026"] == {str(d.uuid) for d in slips}
    # A document like those in an existing subfolder goes there, not into a new one.
    assert targets["Arbeitsvertrag"] == {str(change.uuid)}
    assert result.named_by == "rules" and not result.unassigned


def test_documents_follow_existing_year_folders_and_never_move_sideways():
    work = folder("Work")
    slips_folder = folder("Verdienstabrechnungen", work)
    old = payslips(folder("2025", slips_folder), 2025, range(1, 3))
    new = payslips(work, 2026, range(1, 3))
    # A document in another top-level folder may only go deeper below its own folder.
    private = folder("Private")
    elsewhere = make_doc(
        "Verdienstabrechnung Mai 2026",
        private,
        sender="ACME GmbH",
        type_slug="statement",
        day=date(2026, 5, 28),
    )

    result = suggestion_for([*new, elsewhere])

    [group] = [g for g in result.groups if any(d.uuid == str(new[0].uuid) for d in g.docs)]
    assert group.anchor_id == slips_folder.pk and group.new == ["2026"]
    assert {d.uuid for d in group.docs} == {str(d.uuid) for d in new}
    for g in result.groups:
        if any(d.uuid == str(elsewhere.uuid) for d in g.docs):
            assert g.anchor_id is not None
            anchor = Folder.objects.get(pk=g.anchor_id)
            assert anchor == private or anchor.parent == private
    assert all(d.folder_id == slips_folder.children.get(name="2025").pk for d in old)


def test_unfiled_documents_find_their_folder_anywhere_in_the_tree():
    insurance = folder("Versicherungen", folder("Private"))
    make_doc("Versicherungsschein Hausrat", insurance, sender="VRK", type_slug="contract")
    make_doc("Beitragsrechnung Hausrat", insurance, sender="VRK", type_slug="invoice")
    loose = make_doc("Beitragsrechnung Hausrat 2026", None, sender="VRK", type_slug="invoice")

    [group] = suggestion_for([loose]).groups
    assert group.anchor_id == insurance.pk and group.new == []


def test_the_ai_model_names_new_folders_and_may_merge_groups(settings, monkeypatch):
    settings.OLLAMA_URL = "http://ollama:11434"
    set_ai_model("qwen3-vl:4b-instruct")
    work = folder("Work")
    slips = payslips(work, 2026, range(1, 3))
    tax = [
        make_doc(
            f"Lohnsteuerbescheinigung {y}", work, sender="ACME GmbH", type_slug="notice", day=date(y, 2, 1)
        )
        for y in (2024, 2025)
    ]
    asked = {}

    def fake(model, *, parent, existing, groups):
        asked.update(parent=parent, groups=groups)
        return ["Lohn und Gehalt"] * len(groups)

    monkeypatch.setattr(ai, "name_folders", fake)
    result = suggestion_for([*slips, *tax])

    assert asked["parent"] == "Work" and len(asked["groups"]) == 2
    assert result.named_by == "ai"
    targets = sorted(target(g) for g in result.groups)
    assert targets == ["Work/Lohn und Gehalt/2024", "Work/Lohn und Gehalt/2025", "Work/Lohn und Gehalt/2026"]


def test_without_a_reachable_model_names_come_from_the_titles(settings, monkeypatch):
    settings.OLLAMA_URL = "http://ollama:11434"
    set_ai_model("qwen3-vl:4b-instruct")

    def down(*args, **kwargs):
        raise ai.ModelUnavailable("connection refused")

    monkeypatch.setattr(ai, "name_folders", down)
    slips = payslips(folder("Work"), 2026, range(1, 4))
    result = suggestion_for(slips)
    assert [target(g) for g in result.groups] == ["Work/Verdienstabrechnung/2026"]
    assert result.named_by == "rules" and "could not name" in result.note


def test_single_documents_without_a_fitting_folder_stay_where_they_are():
    work = folder("Work")
    odd = make_doc("Kantinenplan", work)
    result = suggestion_for([odd])
    assert result.groups == [] and [d.uuid for d in result.unassigned] == [str(odd.uuid)]


def test_apply_moves_only_downwards_and_creates_folders():
    work = folder("Work")
    private = folder("Private")
    doc = make_doc("Verdienstabrechnung Januar 2026", work)

    moved, created = filing.apply([filing.Move([str(doc.uuid)], work.pk, ["Verdienstabrechnungen", "2026"])])
    doc.refresh_from_db()
    assert (moved, created) == (1, 2)
    assert doc.folder.name == "2026" and doc.folder.parent.parent == work
    assert doc.source_of("folder") == "user"

    with pytest.raises(filing.ApplyError):
        filing.apply([filing.Move([str(doc.uuid)], private.pk, ["Lohn"])])
    with pytest.raises(filing.ApplyError):
        filing.apply([filing.Move([str(doc.uuid)], None, [])])
    with pytest.raises(filing.ApplyError):
        filing.apply([filing.Move([str(doc.uuid)], doc.folder_id, ["a/b"])])


def test_suggest_and_apply_through_the_api(api):
    work = folder("Work")
    slips = payslips(work, 2026, range(1, 4))
    r = api.post("/api/v1/filing/suggestions", {"ids": [str(d.uuid) for d in slips]}, content_type=J)
    assert r.status_code == 200 and r.json()["state"] == "pending"
    proposal = r.json()["id"]

    Worker().run_until_empty()

    body = api.get(f"/api/v1/filing/suggestions/{proposal}").json()
    assert body["state"] == "done"
    [group] = body["groups"]
    assert group["anchor_path"] == "Work" and group["new"] == ["Verdienstabrechnung", "2026"]
    assert {d["title"] for d in group["documents"]} == {
        "Verdienstabrechnung Januar 2026",
        "Verdienstabrechnung Februar 2026",
        "Verdienstabrechnung März 2026",
    }

    # The user renames the new folder before applying.
    move = {
        "ids": [d["id"] for d in group["documents"]],
        "anchor_id": group["anchor_id"],
        "new": ["Gehalt", "2026"],
    }
    r = api.post("/api/v1/filing/apply", {"moves": [move]}, content_type=J)
    assert r.status_code == 200 and r.json() == {"moved": 3, "created": 2}
    assert {d.folder.parent.name for d in Document.objects.filter(pk__in=[s.pk for s in slips])} == {"Gehalt"}

    bad = {"ids": [str(slips[0].uuid)], "anchor_id": folder("Private").pk, "new": []}
    assert api.post("/api/v1/filing/apply", {"moves": [bad]}, content_type=J).status_code == 400


def test_select_all_takes_the_folder_but_not_its_subfolders(api):
    work = folder("Work")
    direct = [make_doc(f"Brief {i}", work) for i in range(30)]  # more than one page
    make_doc("Tiefer", folder("Archiv", work))
    body = api.get(f"/api/v1/documents/ids?folder={work.pk}").json()
    assert body["total"] == 30 and set(body["ids"]) == {str(d.uuid) for d in direct}
