"""Pages into documents — merge, split, remove — without touching an original; the trash; history."""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import pikepdf
import pytest

from apps.assist import page_analysis
from apps.assist.checks import Item
from apps.crypto.aead import encrypt_file
from apps.documents import alterations, crypto_fields, pages
from apps.documents.intake import archive_aad, archive_intake_path_for, ensure_dirs, intake_path_for
from apps.documents.models import Alteration, Document, DocumentPage
from apps.processing.models import Job
from apps.storage.backends import StorageError
from tests.pdfs import text_pdf

pytestmark = pytest.mark.django_db
J = "application/json"


def letter(sender: str, ref: str, page: int, of: int, body: str = "") -> list[str]:
    return [
        sender,
        f"Kundennummer {ref}",
        f"Seite {page} von {of}",
        *(body or f"Inhalt der Seite {page} zum Vertrag {ref} mit vielen Worten {sender} {page}").split(". "),
        f"Betrag {page * 111},{page:02d} EUR am {page:02d}.03.2026",
        "Mit freundlichen Grüßen und weiteren Hinweisen zur Zahlung und zum Widerruf",
    ]


def pdf_of(pages_lines: list[list[str]]) -> bytes:
    return text_pdf(pages_lines[0], extra_pages=pages_lines[1:])


def make(title: str, pages_lines: list[list[str]], *, build: bool = True) -> Document:
    """A processed document whose archive (the pages as shown) is in the intake."""
    ensure_dirs()
    doc = Document(content_hash=uuid.uuid4().hex, processing_state="done", page_count=len(pages_lines))
    crypto_fields.set_title(doc, title)
    doc.save()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "a.pdf"
        path.write_bytes(pdf_of(pages_lines))
        encrypt_file(path, archive_intake_path_for(str(doc.uuid)), aad=archive_aad(str(doc.uuid)))
        if build:
            pages.build(doc, path)
    crypto_fields.set_content(doc, "\n\n".join("\n".join(p) for p in pages_lines))
    return doc


def item(d: Document, n: int) -> Item:
    return Item(
        uuid=str(d.uuid),
        ref=f"D{n}",
        title=crypto_fields.get_title(d),
        sender="",
        type_name="",
        day=None,
        pages=d.page_count,
        uploaded_at=d.uploaded_at,
        text=crypto_fields.get_content(d),
        extracted={},
        series_id=None,
        series_name="",
        tags=[],
        folder="",
    )


def analyse(docs: list[Document]):
    items = [item(d, n) for n, d in enumerate(docs, start=1)]
    findings, covered = page_analysis.analyse(items, {str(d.uuid): d for d in docs})
    return findings, covered


def page_count(path_bytes_doc: Document) -> int:
    from apps.documents import files

    fh = files.fetch(path_bytes_doc, "original")
    try:
        with pikepdf.open(fh.name) as pdf:
            return len(pdf.pages)
    finally:
        fh.close()


# --- Fingerprints and the page analysis -------------------------------------------------------


def test_pages_get_pictures_and_fingerprints():
    d = make("Brief", [letter("Stadtwerke", "4711", 1, 2), letter("Stadtwerke", "4711", 2, 2)])
    assert DocumentPage.objects.filter(document=d).count() == 2
    assert pages.thumbnail(d, 1)[:4] == b"RIFF"  # WebP
    a, b = pages.fingerprints([d])[d.pk]
    assert a.mark == [1, 2] and b.mark == [2, 2]
    assert pages.same_page(a, a) > 0.9
    assert pages.same_page(a, b) == 0  # same letterhead, other page


def test_a_document_scanned_twice_is_found_and_the_edited_one_is_kept():
    lines = [letter("Versicherung", "99", 1, 1)]
    first, second = make("Police", lines), make("Police (Scan)", lines)
    second.set_source("title", "user")
    second.save()
    findings, covered = analyse([first, second])
    assert covered == {str(first.uuid), str(second.uuid)}
    [dup] = [f for f in findings if f.kind == "duplicate"]
    assert dup.documents == [str(second.uuid), str(first.uuid)]  # keep the one the user edited
    assert dup.action == {"type": "compose", "sources": [str(first.uuid)], "outputs": []}
    assert dup.matches == [[str(first.uuid), 1, str(second.uuid), 1]] or dup.matches


def test_a_page_scanned_twice_inside_a_document_is_removed():
    p1, p2 = letter("Bank", "1", 1, 2), letter("Bank", "1", 2, 2)
    d = make("Kontoauszug", [p1, p2, p2])
    findings, _ = analyse([d])
    [twice] = [f for f in findings if f.kind == "duplicate_pages"]
    assert twice.action["outputs"] == [{"pages": [[str(d.uuid), 1], [str(d.uuid), 2]], "title": ""}]


def test_a_letter_in_two_scans_is_merged_and_one_scan_with_two_letters_is_split():
    part1 = make("Rechnung Teil 1", [letter("Telekom", "555", 1, 3), letter("Telekom", "555", 2, 3)])
    part2 = make("Rechnung Teil 2", [letter("Telekom", "555", 3, 3)])
    mixed = make(
        "Stapel",
        [letter("Amt", "7", 1, 2), letter("Amt", "7", 2, 2), letter("Kasse", "8", 1, 1)],
    )
    findings, _ = analyse([part1, part2, mixed])
    [merge] = [f for f in findings if f.kind == "split"]
    assert merge.action["outputs"][0]["pages"] == [
        [str(part1.uuid), 1],
        [str(part1.uuid), 2],
        [str(part2.uuid), 1],
    ]
    [split] = [f for f in findings if f.kind == "split_inside"]
    assert [len(o["pages"]) for o in split.action["outputs"]] == [2, 1]


def test_pages_that_are_nowhere_are_reported():
    d = make("Vertrag", [letter("Verlag", "3", 1, 3), letter("Verlag", "3", 3, 3)])
    findings, _ = analyse([d])
    [missing] = [f for f in findings if f.kind == "missing_pages"]
    assert "page 2" in missing.text and missing.action is None


# --- Composing: never touching an original ------------------------------------------------------


def test_merging_makes_a_new_document_and_keeps_the_sources_in_the_trash(api):
    a = make("Teil 1", [letter("Telekom", "555", 1, 2)])
    b = make("Teil 2", [letter("Telekom", "555", 2, 2)])
    a.set_source("title", "user")
    a.save()
    r = api.post(
        "/api/v1/alterations/compose",
        {
            "sources": [str(a.uuid), str(b.uuid)],
            "outputs": [
                {"pages": [{"document": str(a.uuid), "page": 1}, {"document": str(b.uuid), "page": 1}]}
            ],
        },
        content_type=J,
    )
    assert r.status_code == 200, r.content
    out = r.json()
    assert out["summary"].startswith("Merged 2 documents")
    [made] = out["results"]
    merged = Document.objects.get(uuid=made["id"])
    assert crypto_fields.get_title(merged) == "Teil 1" and merged.source_of("title") == "user"
    assert merged.processing_plan == {"steps": ["ocr", "analyze"]}
    assert intake_path_for(str(merged.uuid)).exists()
    assert page_count(merged) == 2
    assert Job.objects.filter(document=merged, kind=Job.Kind.INTAKE_DOCUMENT).exists()
    for source in (a, b):
        source.refresh_from_db()
        assert source.deleted_at is not None  # in the trash …
        assert archive_intake_path_for(str(source.uuid)).exists()  # … with its files

    history = api.get(f"/api/v1/alterations/document/{a.uuid}").json()
    assert history["trashed"] and history["replaced_by"][0]["id"] == str(merged.uuid)
    assert history["entries"][0]["can_undo"]

    # Undo: the merged document goes to the trash, the parts come back.
    Job.objects.all().delete()
    r = api.post(f"/api/v1/alterations/{out['id']}/undo")
    assert r.status_code == 200, r.content
    a.refresh_from_db()
    merged.refresh_from_db()
    assert a.deleted_at is None and merged.deleted_at is not None
    assert not r.json()["can_undo"]


def test_removing_a_page_and_validation():
    d = make("Auszug", [letter("Bank", "1", 1, 2), letter("Bank", "1", 2, 2), letter("Bank", "1", 2, 2)])
    uid = str(d.uuid)
    with pytest.raises(alterations.AlterationError, match="Nothing changes"):
        alterations.compose([uid], [alterations.Output([(uid, 1), (uid, 2), (uid, 3)])])
    with pytest.raises(alterations.AlterationError, match="only go into one"):
        alterations.compose([uid], [alterations.Output([(uid, 1), (uid, 1)])])
    with pytest.raises(alterations.AlterationError, match="does not exist"):
        alterations.compose([uid], [alterations.Output([(uid, 9)])])
    a = alterations.compose([uid], [alterations.Output([(uid, 1), (uid, 2)])])
    assert alterations.detail_of(a)["summary"] == "Removed page 3 of “Auszug”"


def test_delete_puts_in_the_trash_and_originals_cannot_be_purged(api):
    d = make("Alt", [letter("X", "1", 1, 1)], build=False)
    assert api.delete(f"/api/v1/documents/{d.uuid}").status_code == 200
    d.refresh_from_db()
    assert d.deleted_at is not None
    assert api.get(f"/api/v1/documents/{d.uuid}").status_code == 404
    assert [t["id"] for t in api.get("/api/v1/alterations/trash").json()] == [str(d.uuid)]

    assert api.post("/api/v1/alterations/restore", {"ids": [str(d.uuid)]}, content_type=J).status_code == 200
    d.refresh_from_db()
    assert d.deleted_at is None

    api.delete(f"/api/v1/documents/{d.uuid}")
    assert api.delete(f"/api/v1/alterations/trash/{d.uuid}?confirm=delete").status_code == 404
    assert Document.objects.filter(pk=d.pk).exists()
    assert archive_intake_path_for(str(d.uuid)).exists()


def test_edits_are_recorded_with_old_and_new_values(api):
    d = make("Alt", [letter("X", "1", 1, 1)], build=False)
    r = api.patch(
        f"/api/v1/documents/{d.uuid}",
        {"title": "Neu", "origin": {"task": 3, "finding": "F2"}},
        content_type=J,
    )
    assert r.status_code == 200, r.content
    [entry] = api.get(f"/api/v1/alterations/document/{d.uuid}").json()["entries"]
    assert entry["kind"] == "edit" and entry["actor"] == "assistant"
    assert entry["changes"] == [{"field": "Title", "old": "Alt", "new": "Neu"}]
    assert entry["task"] == 3 and entry["finding"] == "F2" and not entry["can_undo"]
    assert Alteration.objects.count() == 1


def test_the_page_editor_gets_the_pages(api):
    d = make("Brief", [letter("A", "1", 1, 2), letter("A", "1", 2, 2)], build=False)
    r = api.get(f"/api/v1/alterations/pages/{d.uuid}")  # made on demand
    assert r.status_code == 200, r.content
    assert [p["mark"] for p in r.json()["pages"]] == ["1/2", "2/2"]
    image = api.get(f"/api/v1/alterations/pages/{d.uuid}/2")
    assert image.status_code == 200 and image["Content-Type"] == "image/webp"


def test_similar_bills_with_different_amounts_are_not_duplicate_pages():
    body = " ".join(["Weitere Angaben zum Vertrag und zur Zahlung."] * 20)
    a = make("Rechnung 1", [letter("Telekom", "555", 1, 1, body + " Rechnungsbetrag 123,45 EUR")])
    b = make("Rechnung 2", [letter("Telekom", "555", 1, 1, body + " Rechnungsbetrag 124,45 EUR")])
    findings, _ = analyse([a, b])
    assert not [f for f in findings if f.kind in ("duplicate", "duplicate_pages")]


def test_merge_orders_interleaved_scans_by_printed_page_numbers():
    a = make("Ungerade", [letter("Amt", "777", 1, 3), letter("Amt", "777", 3, 3)])
    b = make("Gerade", [letter("Amt", "777", 2, 3)])
    findings, _ = analyse([a, b])
    [split] = [f for f in findings if f.kind == "split"]
    assert split.action["outputs"][0]["pages"] == [[str(a.uuid), 1], [str(b.uuid), 1], [str(a.uuid), 2]]


def test_a_missing_ocr_page_mark_does_not_mean_a_page_is_missing():
    d = make("Brief", [letter("Amt", "1", 1, 2), ["Zweite Seite ohne erkannte Seitenzahl."]])
    findings, _ = analyse([d])
    assert not [f for f in findings if f.kind == "missing_pages"]


def test_failed_rendering_keeps_previously_prepared_pages(monkeypatch):
    d = make("Brief", [letter("Amt", "1", 1, 1)])
    before = list(DocumentPage.objects.filter(document=d).values_list("fingerprint_enc", flat=True))
    monkeypatch.setattr(pages, "render", lambda *a: [])
    with pytest.raises(RuntimeError, match="page count"):
        pages.build_from_storage(d)
    assert list(DocumentPage.objects.filter(document=d).values_list("fingerprint_enc", flat=True)) == before


def test_compose_rejects_undeclared_sources_and_empty_requests():
    a = make("A", [letter("Amt", "1", 1, 1)])
    b = make("B", [letter("Amt", "2", 1, 1)])
    with pytest.raises(alterations.AlterationError, match="selected source"):
        alterations.compose([str(a.uuid)], [alterations.Output([(str(b.uuid), 1)])])
    with pytest.raises(alterations.AlterationError, match="Select documents"):
        alterations.trash([])
    assert not Document.objects.filter(deleted_at__isnull=False).exists()


def test_existing_output_is_never_adopted_or_trashed_by_compose(monkeypatch):
    from apps.documents.intake import IntakeResult

    a = make("Source", [letter("Amt", "1", 1, 2), letter("Amt", "1", 2, 2)])
    existing = make("Existing output", [letter("Amt", "1", 1, 1)])
    monkeypatch.setattr(alterations, "register", lambda *a, **k: IntakeResult(existing, created=False))
    with pytest.raises(alterations.AlterationError, match="already exists"):
        alterations.compose([str(a.uuid)], [alterations.Output([(str(a.uuid), 1)])])
    assert not Document.objects.filter(deleted_at__isnull=False).exists()
    assert not Alteration.objects.exists()


def test_failed_composition_rolls_back_documents_and_generated_intake_files(monkeypatch, settings):
    d = make("Source", [letter("Amt", "1", 1, 2), letter("Amt", "1", 2, 2)])
    uid = str(d.uuid)
    before = set(settings.INTAKE_DIR.iterdir())
    create = alterations._new_document
    calls = []

    def fail_second(*args):
        calls.append(1)
        if len(calls) == 2:
            raise StorageError("unavailable")
        return create(*args)

    monkeypatch.setattr(alterations, "_new_document", fail_second)
    with pytest.raises(StorageError):
        alterations.compose([uid], [alterations.Output([(uid, 1)]), alterations.Output([(uid, 2)])])
    assert Document.objects.count() == 1 and not Alteration.objects.exists()
    assert set(settings.INTAKE_DIR.iterdir()) == before
    d.refresh_from_db()
    assert d.deleted_at is None


def test_undo_refuses_independently_restored_sources_and_does_not_change_results():
    a = make("A", [letter("Amt", "1", 1, 2)])
    b = make("B", [letter("Amt", "1", 2, 2)])
    au, bu = str(a.uuid), str(b.uuid)
    alteration = alterations.compose([au, bu], [alterations.Output([(au, 1), (bu, 1)])])
    alterations.restore([au])
    with pytest.raises(alterations.AlterationError, match="restored since"):
        alterations.undo(alteration)
    merged = Document.objects.get(uuid=alteration.results[0])
    assert merged.deleted_at is None
    alteration.refresh_from_db()
    assert alteration.undone_at is None


def test_bulk_metadata_changes_are_visible_in_document_history(api):
    d = make("Brief", [letter("Amt", "1", 1, 1)])
    r = api.post("/api/v1/documents/bulk", {"ids": [str(d.uuid)], "action": "important"}, content_type=J)
    assert r.status_code == 200, r.content
    [entry] = api.get(f"/api/v1/alterations/document/{d.uuid}").json()["entries"]
    assert entry["changes"] == [{"field": "Important", "old": "no", "new": "yes"}]


def test_moves_between_same_named_folders_record_full_paths(api):
    from apps.taxonomy.services import ensure_folder_path

    d = make("Brief", [letter("Amt", "1", 1, 1)])
    d.folder = ensure_folder_path("Private/Taxes")
    d.save(update_fields=["folder"])
    target = ensure_folder_path("Work/Taxes")
    assert target is not None
    r = api.post(
        "/api/v1/documents/bulk",
        {"ids": [str(d.uuid)], "action": "move", "folder_id": target.pk},
        content_type=J,
    )
    assert r.status_code == 200, r.content
    [entry] = api.get(f"/api/v1/alterations/document/{d.uuid}").json()["entries"]
    assert entry["changes"] == [{"field": "Folder", "old": "Private / Taxes", "new": "Work / Taxes"}]


def test_restoring_a_pending_document_requeues_work_skipped_while_in_the_trash():
    from apps.processing import pipeline
    from apps.processing.worker import Worker

    d = Document.objects.create(content_hash=uuid.uuid4().hex)
    pipeline.enqueue(d)
    alterations.trash([str(d.uuid)])
    assert Worker().run_once()  # the job skips the trashed document
    assert not Job.objects.filter(state=Job.State.QUEUED).exists()
    alterations.restore([str(d.uuid)])
    assert Job.objects.filter(document=d, kind=Job.Kind.INTAKE_DOCUMENT, state=Job.State.QUEUED).exists()
