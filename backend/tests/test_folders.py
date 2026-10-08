import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.documents.models import Document
from apps.taxonomy.models import Folder
from tests.conftest import upload
from tests.pdfs import text_pdf

pytestmark = pytest.mark.django_db
J = "application/json"


def make_doc(folder: Folder | None = None) -> Document:
    return Document.objects.create(folder=folder, content_hash=uuid.uuid4().hex, processing_state="done")


def folder_of(response) -> Folder | None:
    return Document.objects.get(uuid=response.json()["id"]).folder


def test_scanner_bucket_files_into_folder_path_and_creates_missing_folders(scanner):
    _, token = scanner
    private = Folder.objects.get(name="Private")
    assert folder_of(upload(Client(), token, text_pdf(["a"]), bucket="private")) == private

    nested = folder_of(upload(Client(), token, text_pdf(["b"]), bucket="Private/Steuern/2024"))
    assert nested is not None and nested.name == "2024"
    assert nested.parent.name == "Steuern" and nested.parent.parent == private

    # existing path is reused case-insensitively, new top-level folder is created by name
    assert folder_of(upload(Client(), token, text_pdf(["c"]), bucket=" private / STEUERN /2024")) == nested
    vereine = folder_of(upload(Client(), token, text_pdf(["d"]), bucket="Vereine"))
    assert vereine.name == "Vereine" and vereine.parent is None
    assert Folder.objects.filter(name__iexact="steuern").count() == 1

    unfiled = upload(Client(), token, text_pdf(["e"]), bucket="")
    assert unfiled.status_code == 202 and folder_of(unfiled) is None
    r = Client().post(  # no bucket field at all
        "/api/upload/v1/documents",
        {"file": SimpleUploadedFile("scan.pdf", text_pdf(["f"]), content_type="application/pdf")},
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert r.status_code == 202 and folder_of(r) is None


def test_folder_crud_and_tree_rules(api):
    root = api.post("/api/v1/folders", {"name": "Haus"}, content_type=J).json()
    child = api.post("/api/v1/folders", {"name": "Strom", "parent_id": root["id"]}, content_type=J).json()
    assert child["parent_id"] == root["id"] and child["path"] == "Haus / Strom"

    # same name next to each other is rejected, under another parent it is fine
    assert api.post("/api/v1/folders", {"name": "haus"}, content_type=J).status_code == 400
    assert (
        api.post("/api/v1/folders", {"name": "Haus", "parent_id": root["id"]}, content_type=J).status_code
        == 200
    )
    assert api.post("/api/v1/folders", {"name": "a/b"}, content_type=J).status_code == 400

    # a folder cannot be moved into itself or its descendants
    r = api.patch(f"/api/v1/folders/{root['id']}", {"parent_id": child["id"]}, content_type=J)
    assert r.status_code == 400
    r = api.patch(f"/api/v1/folders/{child['id']}", {"move_to_root": True, "name": "Energie"}, content_type=J)
    assert r.json()["parent_id"] is None and r.json()["path"] == "Energie"

    # deleting a folder with documents in its subtree is blocked
    r = api.patch(f"/api/v1/folders/{child['id']}", {"parent_id": root["id"]}, content_type=J)
    make_doc(Folder.objects.get(pk=child["id"]))
    assert api.delete(f"/api/v1/folders/{root['id']}").status_code == 400
    Document.objects.update(deleted_at="2026-01-01T00:00:00Z")  # trashed documents do not block
    assert api.delete(f"/api/v1/folders/{root['id']}").status_code == 200
    assert not Folder.objects.filter(pk__in=[root["id"], child["id"]]).exists()
    assert Document.objects.get().folder is None

    listed = {f["path"]: f for f in api.get("/api/v1/folders").json()}
    assert "Private" in listed and listed["Private"]["document_count"] == 0


def test_filter_move_and_unfile_documents(api):
    private = Folder.objects.get(name="Private")
    taxes = Folder.objects.create(name="Taxes", parent=private)
    a, b, c = make_doc(private), make_doc(taxes), make_doc()

    def listed(**params) -> set[str]:
        return {i["id"] for i in api.get("/api/v1/documents", params).json()["items"]}

    assert listed(folder=private.pk) == {str(a.uuid)}
    assert listed(folder=private.pk, subfolders="true") == {str(a.uuid), str(b.uuid)}
    assert listed(unfiled="true") == {str(c.uuid)}

    item = api.get(f"/api/v1/documents/{b.uuid}").json()
    assert item["folder"] == {"id": taxes.pk, "name": "Taxes", "path": "Private / Taxes"}

    r = api.post(
        "/api/v1/documents/bulk",
        {"ids": [str(a.uuid), str(c.uuid)], "action": "move", "folder_id": taxes.pk},
        content_type=J,
    )
    assert r.json() == {"updated": 2}
    assert listed(folder=taxes.pk) == {str(a.uuid), str(b.uuid), str(c.uuid)}
    assert Document.objects.get(pk=c.pk).field_sources["folder"] == "user"

    api.post("/api/v1/documents/bulk", {"ids": [str(a.uuid)], "action": "move"}, content_type=J)
    r = api.patch(f"/api/v1/documents/{b.uuid}", {"clear_folder": True}, content_type=J)
    assert r.json()["folder"] is None
    assert listed(unfiled="true") == {str(a.uuid), str(b.uuid)}
    r = api.patch(f"/api/v1/documents/{b.uuid}", {"folder_id": private.pk}, content_type=J)
    assert r.json()["folder"]["id"] == private.pk
