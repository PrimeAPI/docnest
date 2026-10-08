"""Paper storage: locations, putting scanned documents away, and finding a sheet in a binder."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.documents import crypto_fields
from apps.documents.models import Document
from apps.paper.models import Location

pytestmark = pytest.mark.django_db
J = "application/json"


def make_doc(title: str, pages: int, *, minutes: int, has_paper: bool = True) -> Document:
    doc = Document.objects.create(
        content_hash=uuid4().hex,
        page_count=pages,
        original_page_count=pages,
        has_paper=has_paper,
        processing_state=Document.State.DONE,
        processing_stage=Document.Stage.DONE,
        uploaded_at=timezone.now() - timedelta(days=1) + timedelta(minutes=minutes),
    )
    crypto_fields.set_title(doc, title)
    doc.save()
    return doc


def create_location(api, name: str, **extra) -> dict:
    r = api.post("/api/v1/paper/locations", {"name": name, **extra}, content_type=J)
    assert r.status_code == 200, r.content
    return r.json()


def test_locations_nest_and_show_paths(api):
    cabinet = create_location(api, "Cabinet")
    binder = create_location(api, "Binder 1", parent_id=cabinet["id"], capacity=400)
    assert binder["path"] == "Cabinet / Binder 1" and binder["capacity"] == 400
    assert (
        api.post(
            "/api/v1/paper/locations", {"name": "binder 1", "parent_id": cabinet["id"]}, content_type=J
        ).status_code
        == 400
    )
    assert api.post("/api/v1/paper/locations", {"name": "A/B"}, content_type=J).status_code == 400
    r = api.patch(f"/api/v1/paper/locations/{cabinet['id']}", {"parent_id": binder["id"]}, content_type=J)
    assert r.status_code == 400  # not into itself
    paths = [loc["path"] for loc in api.get("/api/v1/paper/locations").json()]
    assert paths == ["Cabinet", "Cabinet / Binder 1"]


def test_putting_pending_documents_away_and_finding_them(api):
    first = make_doc("Stromrechnung Januar", 2, minutes=1)
    second = make_doc("Kfz-Versicherung", 3, minutes=2)
    third = make_doc("Gehaltsabrechnung", 1, minutes=3)
    make_doc("Digital invoice", 1, minutes=4, has_paper=False)
    assert api.get("/api/v1/overview").json()["paper_pending"] == 3

    pending = api.get("/api/v1/paper/pending").json()
    assert [d["title"] for d in pending["documents"]] == [
        "Stromrechnung Januar",
        "Kfz-Versicherung",
        "Gehaltsabrechnung",
    ]
    assert pending["sheets"] == 6

    binder = create_location(api, "Binder 1", capacity=100)
    r = api.post(
        f"/api/v1/paper/locations/{binder['id']}/place",
        {"ids": [str(d.uuid) for d in (first, second, third)]},
        content_type=J,
    )
    assert r.status_code == 200 and r.json()["placed"] == 3
    assert r.json()["location"]["sheets"] == 6 and r.json()["location"]["document_count"] == 3
    assert api.get("/api/v1/paper/pending").json()["documents"] == []

    # Newest scan on top.
    stack = api.get(f"/api/v1/paper/locations/{binder['id']}/stack").json()["documents"]
    assert [d["title"] for d in stack] == ["Gehaltsabrechnung", "Kfz-Versicherung", "Stromrechnung Januar"]
    assert [d["sheets_above"] for d in stack] == [0, 1, 4]

    paper = api.get(f"/api/v1/documents/{second.uuid}").json()["paper"]
    assert paper["location_path"] == "Binder 1"
    pos = paper["position"]
    assert pos["index_from_top"] == 2 and pos["count"] == 3
    assert (pos["sheets_above"], pos["sheets"], pos["sheets_below"]) == (1, 3, 2)
    assert pos["mm_from_top"] == 0.1 and pos["capacity"] == 100
    assert pos["above"]["title"] == "Gehaltsabrechnung"
    assert pos["below"]["title"] == "Stromrechnung Januar"


def test_later_batches_lie_on_top_of_earlier_ones(api):
    binder = create_location(api, "Binder 1")
    old_late_scan = make_doc("Batch 1 newest", 1, minutes=50)
    api.post(
        f"/api/v1/paper/locations/{binder['id']}/place", {"ids": [str(old_late_scan.uuid)]}, content_type=J
    )
    # Scanned earlier, but put away later: it lies on top.
    forgotten = make_doc("Forgotten letter", 1, minutes=10)
    api.post(f"/api/v1/paper/locations/{binder['id']}/place", {"ids": [str(forgotten.uuid)]}, content_type=J)
    stack = api.get(f"/api/v1/paper/locations/{binder['id']}/stack").json()["documents"]
    assert [d["title"] for d in stack] == ["Forgotten letter", "Batch 1 newest"]


def test_placing_ignores_documents_already_put_away(api):
    a = create_location(api, "Binder A")
    b = create_location(api, "Binder B")
    doc = make_doc("Letter", 1, minutes=1)
    api.post(f"/api/v1/paper/locations/{a['id']}/place", {"ids": [str(doc.uuid)]}, content_type=J)
    r = api.post(f"/api/v1/paper/locations/{b['id']}/place", {"ids": [str(doc.uuid)]}, content_type=J)
    assert r.json()["placed"] == 0
    doc.refresh_from_db()
    assert doc.paper_location_id == a["id"]


def test_document_paper_fields_can_be_edited(api):
    binder = create_location(api, "Binder 1")
    web_upload = make_doc("Printed contract", 4, minutes=1, has_paper=False)
    r = api.patch(f"/api/v1/documents/{web_upload.uuid}", {"has_paper": True}, content_type=J)
    assert r.json()["paper"]["has_paper"] is True
    assert api.get("/api/v1/documents", {"paper_pending": "true"}).json()["total"] == 1

    r = api.patch(f"/api/v1/documents/{web_upload.uuid}", {"paper_location_id": binder["id"]}, content_type=J)
    assert r.json()["paper"]["location_id"] == binder["id"]
    assert api.get("/api/v1/documents", {"paper_location": binder["id"]}).json()["total"] == 1

    r = api.patch(f"/api/v1/documents/{web_upload.uuid}", {"has_paper": False}, content_type=J)
    assert r.json()["paper"]["has_paper"] is False and r.json()["paper"]["location_id"] is None


def test_location_with_documents_cannot_be_deleted(api):
    cabinet = create_location(api, "Cabinet")
    binder = create_location(api, "Binder", parent_id=cabinet["id"])
    doc = make_doc("Letter", 1, minutes=1)
    api.post(f"/api/v1/paper/locations/{binder['id']}/place", {"ids": [str(doc.uuid)]}, content_type=J)
    assert api.delete(f"/api/v1/paper/locations/{cabinet['id']}").status_code == 400
    api.patch(f"/api/v1/documents/{doc.uuid}", {"clear_paper_location": True}, content_type=J)
    assert api.delete(f"/api/v1/paper/locations/{cabinet['id']}").status_code == 200
    assert not Location.objects.exists()


def test_bulk_paper_flags(api):
    docs = [make_doc(f"Doc {i}", 1, minutes=i, has_paper=False) for i in range(3)]
    r = api.post(
        "/api/v1/documents/bulk",
        {"ids": [str(d.uuid) for d in docs[:2]], "action": "paper_yes"},
        content_type=J,
    )
    assert r.json() == {"updated": 2}
    assert api.get("/api/v1/paper/pending").json()["sheets"] == 2


def test_scanner_uploads_have_paper_web_uploads_not(scanner, api):
    from django.test import Client

    from tests.conftest import upload
    from tests.pdfs import INVOICE_LINES, text_pdf

    _, token = scanner
    scanned = upload(Client(), token, text_pdf(INVOICE_LINES)).json()["id"]
    assert Document.objects.get(uuid=scanned).has_paper is True
    from django.core.files.uploadedfile import SimpleUploadedFile

    r = api.post(
        "/api/v1/documents/upload",
        {"file": SimpleUploadedFile("x.pdf", text_pdf(["Hello web"]), content_type="application/pdf")},
    )
    assert r.status_code == 202, r.content
    assert Document.objects.get(uuid=r.json()["id"]).has_paper is False
