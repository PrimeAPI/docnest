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


def test_pending_documents_carry_their_filing_folder(api):
    from apps.taxonomy.services import ensure_folder_path

    filed = make_doc("Filed", 1, minutes=1)
    filed.folder = ensure_folder_path("Bucket 1/Taxes")
    filed.save()
    make_doc("Unfiled", 1, minutes=2)
    docs = api.get("/api/v1/paper/pending").json()["documents"]
    assert [(d["title"], d["folder"]) for d in docs] == [("Filed", "Bucket 1 / Taxes"), ("Unfiled", None)]
    assert docs[0]["folder_id"] == filed.folder_id and docs[1]["folder_id"] is None
    assert docs[0]["folder_color"] == "slate" and docs[1]["folder_color"] is None


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


# --- Filing the paper original at upload time ---------------------------------------


def scan(token: str, *lines: str, **fields: str):
    from django.test import Client

    from tests.conftest import upload
    from tests.pdfs import text_pdf

    return upload(Client(), token, text_pdf(list(lines) or ["scan"]), **fields)


def test_scanner_files_the_paper_original_on_upload(scanner, api):
    """The scanner says where it put the sheet, so the document is never pending."""
    _, token = scanner
    r = scan(token, "Stromrechnung", paper_location="Cabinet/Binder 3")
    assert r.status_code == 202, r.content
    doc = Document.objects.get(uuid=r.json()["id"])

    assert doc.has_paper is True
    assert doc.paper_location is not None and doc.paper_placed_at is not None
    assert doc.paper_location.name == "Binder 3" and doc.paper_location.parent.name == "Cabinet"
    assert doc.field_sources["paper_location"] == "scanner"
    assert api.get("/api/v1/paper/pending").json()["documents"] == []
    assert api.get("/api/v1/overview").json()["paper_pending"] == 0

    detail = api.get(f"/api/v1/documents/{doc.uuid}").json()["paper"]
    assert detail["location_path"] == "Cabinet / Binder 3" and detail["position"]["index_from_top"] == 1
    stack = api.get(f"/api/v1/paper/locations/{doc.paper_location_id}/stack").json()
    assert [d["id"] for d in stack["documents"]] == [str(doc.uuid)]


def test_upload_reuses_an_existing_location_case_insensitively(scanner, api):
    _, token = scanner
    cabinet = create_location(api, "Cabinet")
    binder = create_location(api, "Binder 3", parent_id=cabinet["id"])
    doc = Document.objects.get(uuid=scan(token, "a", paper_location="cabinet / binder 3").json()["id"])
    assert doc.paper_location_id == binder["id"]
    assert Location.objects.count() == 2  # nothing new was created

    # A second scan lands on top of the first one in the same binder.
    other = Document.objects.get(uuid=scan(token, "b", paper_location="Cabinet/Binder 3").json()["id"])
    stack = api.get(f"/api/v1/paper/locations/{binder['id']}/stack").json()
    assert [d["id"] for d in stack["documents"]] == [str(other.uuid), str(doc.uuid)]  # top first


def test_upload_without_a_location_leaves_the_document_pending(scanner, api):
    _, token = scanner
    doc = Document.objects.get(uuid=scan(token, "no location given").json()["id"])
    assert doc.has_paper is True and doc.paper_location_id is None and doc.paper_placed_at is None
    assert Location.objects.count() == 0
    assert len(api.get("/api/v1/paper/pending").json()["documents"]) == 1


def test_scanner_can_say_there_is_no_paper_original(scanner, api):
    """ "I threw the sheet out": nothing to put away, so it stays out of the queue."""
    _, token = scanner
    doc = Document.objects.get(uuid=scan(token, "thrown out", no_paper="true").json()["id"])
    assert doc.has_paper is False and doc.paper_location_id is None and doc.paper_placed_at is None
    assert api.get("/api/v1/paper/pending").json()["documents"] == []
    assert api.get(f"/api/v1/documents/{doc.uuid}").json()["paper"]["has_paper"] is False


def test_a_location_and_no_paper_contradict_each_other(scanner, isolated_dirs):
    _, token = scanner
    r = scan(token, "both", paper_location="Cabinet", no_paper="true")
    assert r.status_code == 400 and "having none" in r.json()["detail"]
    assert Document.objects.count() == 0 and Location.objects.count() == 0
    assert list((isolated_dirs / "intake").iterdir()) == []  # the rejected scan left nothing behind


def test_location_path_depth_is_limited(scanner):
    _, token = scanner
    assert scan(token, "deep", paper_location="/".join("abcdefg")).status_code == 400
    assert Document.objects.count() == 0 and Location.objects.count() == 0
    assert scan(token, "ok", paper_location="/".join("abcdef")).status_code == 202


def test_pending_filter_and_location_filter_see_the_upload(scanner, api):
    _, token = scanner
    filed = Document.objects.get(uuid=scan(token, "filed", paper_location="Shelf").json()["id"])
    pending_doc = Document.objects.get(uuid=scan(token, "pending").json()["id"])

    def ids(query: str) -> list[str]:
        return [d["id"] for d in api.get(f"/api/v1/documents?{query}").json()["items"]]

    assert ids("paper_pending=true") == [str(pending_doc.uuid)]
    assert ids(f"paper_location={filed.paper_location_id}") == [str(filed.uuid)]


def test_web_upload_can_file_the_paper_original_or_disclaim_it(api):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from tests.pdfs import text_pdf

    shelf = create_location(api, "Shelf")

    def web_upload(text: str, **fields):
        return api.post(
            "/api/v1/documents/upload",
            {
                "file": SimpleUploadedFile(f"{text}.pdf", text_pdf([text]), content_type="application/pdf"),
                **fields,
            },
        )

    r = web_upload("filed by hand", paper_location_id=shelf["id"])
    assert r.status_code == 202, r.content
    doc = Document.objects.get(uuid=r.json()["id"])
    assert doc.has_paper is True and doc.paper_location_id == shelf["id"]
    assert doc.paper_placed_at is not None and doc.field_sources["paper_location"] == "user"

    # A web upload has no paper original unless one is filed, so `no_paper` only confirms that.
    other = Document.objects.get(uuid=web_upload("digital", no_paper="true").json()["id"])
    assert other.has_paper is False

    assert web_upload("bad location", paper_location_id=99999).status_code == 400
    assert web_upload("both", paper_location_id=shelf["id"], no_paper="true").status_code == 400


def test_scan_session_carries_the_filing_to_the_document(scanner):
    from apps.documents.intake import IntakeRequest
    from apps.processing.assemble import AssemblyOptions
    from apps.scanners import sessions

    client, _ = scanner
    session, created = sessions.create(
        client, IntakeRequest(bucket="private", paper_location="Cabinet/Binder 3"), AssemblyOptions()
    )
    assert created
    decoded = sessions.decode_request(session).intake
    assert decoded.paper_location == "Cabinet/Binder 3" and decoded.no_paper is False
    assert decoded.paper_filed is True
