"""Changing which pages make up which documents — without touching an original file.

One operation covers merging, splitting, extracting, removing and reordering pages:
**compose** takes the pages of some documents as shown (their archive files) and says
which documents should come out, each a list of (document, page). An output that is a
source document unchanged keeps that document as it is. Every other output becomes a new
document — built from copies of the pages and processed like an upload — and every source
not kept goes to the trash, with its original and archive files untouched. Putting a
document in the trash is the same without outputs.

Every alteration is recorded with its page map, shown in each document's history, and can
be undone while the documents it made still exist: they go to the trash, the sources come
back. Original files and their history are always retained.
"""

from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pikepdf
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.crypto.aead import decrypt_bytes, encrypt_bytes, encrypt_file
from apps.documents import crypto_fields, files
from apps.documents.intake import IntakeRequest, content_hash_of, intake_aad, intake_path_for, register
from apps.documents.models import Alteration, Document, DocumentTag, Source
from apps.processing.models import Job

logger = logging.getLogger(__name__)

DETAIL_AAD = b"alteration-detail"
MAX_OUTPUTS = 50


class AlterationError(ValueError):
    pass


@dataclass
class Output:
    pages: list[tuple[str, int]]  # (document uuid, 1-based page) in the new order
    title: str = ""  # a title the user gave; "" = like the first source, then the analysis decides


@dataclass
class Plan:
    sources: list[Document]
    outputs: list[Output]
    kept: dict[int, Document] = field(default_factory=dict)  # output index -> unchanged source
    retired: list[Document] = field(default_factory=list)


def detail_of(alteration: Alteration) -> dict[str, Any]:
    if not alteration.detail_enc:
        return {}
    return json.loads(decrypt_bytes(bytes(alteration.detail_enc), aad=DETAIL_AAD))


def _save_detail(alteration: Alteration, detail: dict[str, Any]) -> None:
    alteration.detail_enc = encrypt_bytes(json.dumps(detail, ensure_ascii=False).encode(), aad=DETAIL_AAD)


def plan(source_ids: list[str], outputs: list[Output], *, lock: bool = False) -> Plan:
    """Check a composition and work out what it does; raises AlterationError when it cannot be done."""
    if len(outputs) > MAX_OUTPUTS:
        raise AlterationError("Too many documents at once")
    wanted = list(dict.fromkeys(source_ids))
    if not wanted:
        raise AlterationError("Select documents first")
    if any(u not in wanted for o in outputs for u, _ in o.pages):
        raise AlterationError("Every page must belong to a selected source document")
    qs = Document.objects.filter(uuid__in=wanted, deleted_at__isnull=True).order_by("pk")
    if lock:
        qs = qs.select_for_update()
    sources = list(qs)
    by_uuid = {str(d.uuid): d for d in sources}
    if len(by_uuid) != len(wanted):
        raise AlterationError("A document is missing or already in the trash")
    jobs = Job.objects.filter(document__in=sources, state__in=[Job.State.QUEUED, Job.State.RUNNING])
    if lock:
        jobs = jobs.select_for_update()
    if any(j.state == Job.State.RUNNING for j in jobs):
        raise AlterationError("A document is being processed right now. Try again in a moment.")
    busy = [d for d in sources if d.processing_state != Document.State.DONE]
    if outputs and busy:  # its pages are needed as shown: after processing
        raise AlterationError("Wait until the documents are processed")
    seen: set[tuple[str, int]] = set()
    for output in outputs:
        if not output.pages:
            raise AlterationError("A new document needs at least one page")
        for uuid, page in output.pages:
            if not 1 <= page <= by_uuid[uuid].page_count:
                raise AlterationError(f"Page {page} does not exist")
            if (uuid, page) in seen:
                raise AlterationError("A page can only go into one document")
            seen.add((uuid, page))
    result = Plan([by_uuid[u] for u in wanted], outputs)
    for i, output in enumerate(outputs):
        uuids = {u for u, _ in output.pages}
        if len(uuids) == 1:
            d = by_uuid[next(iter(uuids))]
            if [p for _, p in output.pages] == list(range(1, d.page_count + 1)):
                result.kept[i] = d
    kept = {d.pk for d in result.kept.values()}
    result.retired = [d for d in result.sources if d.pk not in kept]
    if not result.retired:
        raise AlterationError("Nothing changes")
    return result


def _build_pdf(outputs: Output, archives: dict[str, Path], target: Path) -> None:
    out = pikepdf.new()
    opened: dict[str, pikepdf.Pdf] = {}
    try:
        for uuid, page in outputs.pages:
            if uuid not in opened:
                opened[uuid] = pikepdf.open(archives[uuid])
            out.pages.append(opened[uuid].pages[page - 1])
        out.save(target)
    finally:
        out.close()
        for pdf in opened.values():
            pdf.close()


def _new_document(output: Output, pdf_path: Path, by_uuid: dict[str, Document]) -> Document:
    """Register the composed pages as a document, like an upload, with the first source's details."""
    first = by_uuid[output.pages[0][0]]
    with pdf_path.open("rb") as fh:
        digest, size = content_hash_of(fh)
    tags = [dt.tag.name for dt in DocumentTag.objects.filter(document=first).select_related("tag")]

    def store(doc_uuid: str) -> None:
        encrypt_file(pdf_path, intake_path_for(doc_uuid), aad=intake_aad(doc_uuid))

    def discard(doc_uuid: str) -> None:
        intake_path_for(doc_uuid).unlink(missing_ok=True)

    req = IntakeRequest(
        folder_id=first.folder_id,
        important=first.is_important,
        todo=first.status == Document.Status.TODO,
        tags=tags,
        filename=output.title or crypto_fields.get_title(first),
    )
    result = register(req, digest=digest, size=size, scanner=None, store=store, discard=discard)
    document = result.document
    if not result.created:
        raise AlterationError(
            "An output already exists in the archive. Review that document before rearranging."
        )
    # The pages are enhanced already: recognise and analyse them again, nothing else.
    document.processing_plan = {"steps": ["ocr", "analyze"]}
    document.ocr_backend = first.ocr_backend or document.ocr_backend
    document.has_paper = first.has_paper
    document.paper_location_id = first.paper_location_id
    document.paper_placed_at = first.paper_placed_at
    document.mail_id = first.mail_id
    document.received_from_id = first.received_from_id
    sources = {"folder": Source.USER}
    for name, value in (
        ("document_type", first.document_type_id),
        ("correspondent", first.correspondent_id),
        ("document_date", first.document_date),
        ("series", first.series_id),
    ):
        setattr(document, name + ("_id" if name not in ("document_date",) else ""), value)
        if first.source_of(name) == Source.USER:
            sources[name] = Source.USER  # what the user set stays; the analysis fills in the rest
    title = output.title or crypto_fields.get_title(first)
    if title:
        crypto_fields.set_title(document, title)
        if output.title or first.source_of("title") == Source.USER:
            sources["title"] = Source.USER
    document.field_sources = {**document.field_sources, **sources}
    document.save()
    return document


@transaction.atomic
def compose(
    source_ids: list[str],
    outputs: list[Output],
    *,
    actor: str = Alteration.Actor.USER,
    summary: str = "",
    origin: dict[str, Any] | None = None,
) -> Alteration:
    p = plan(source_ids, outputs, lock=True)
    by_uuid = {str(d.uuid): d for d in p.sources}
    needed = {u for i, o in enumerate(outputs) if i not in p.kept for u, _ in o.pages}
    settings.WORK_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=settings.WORK_DIR) as tmp:
        work = Path(tmp)
        archives: dict[str, Path] = {}
        handles = []
        try:
            for uuid in needed:
                fh = files.fetch(by_uuid[uuid], "archive")
                handles.append(fh)
                archives[uuid] = Path(fh.name)
            built: dict[int, Path] = {}
            for i, output in enumerate(outputs):
                if i in p.kept:
                    continue
                built[i] = work / f"out-{i}.pdf"
                _build_pdf(output, archives, built[i])
            created: list[Document] = []
            try:
                results: dict[int, Document] = dict(p.kept)
                for i, path in built.items():
                    results[i] = _new_document(outputs[i], path, by_uuid)
                    created.append(results[i])
                now = timezone.now()
                Document.objects.filter(pk__in=[d.pk for d in p.retired]).update(deleted_at=now)
                alteration = Alteration(
                    kind=Alteration.Kind.COMPOSE if outputs else Alteration.Kind.TRASH,
                    actor=actor,
                    sources=[str(d.uuid) for d in p.sources],
                    results=[str(results[i].uuid) for i in sorted(results)],
                    retired=[str(d.uuid) for d in p.retired],
                )
                _save_detail(
                    alteration,
                    {
                        "summary": summary or describe(p, results),
                        "titles": {str(d.uuid): crypto_fields.get_title(d) for d in p.sources},
                        "outputs": [
                            {
                                "document": str(results[i].uuid),
                                "title": outputs[i].title or crypto_fields.get_title(results[i]),
                                "kept": i in p.kept,
                                "pages": [[u, n] for u, n in outputs[i].pages],
                            }
                            for i in sorted(results)
                        ],
                        **(origin or {}),
                    },
                )
                alteration.save()
            except BaseException:
                for document in created:
                    intake_path_for(str(document.uuid)).unlink(missing_ok=True)
                raise
        finally:
            for fh in handles:
                fh.close()
    return alteration


def describe(p: Plan, results: dict[int, Document]) -> str:
    titles = {str(d.uuid): crypto_fields.get_title(d) or "Untitled" for d in p.sources}
    new = [i for i in results if i not in p.kept]
    if not p.outputs:
        return "Put in the trash: " + ", ".join(f"“{titles[str(d.uuid)]}”" for d in p.retired)
    if len(new) == 1 and len(p.retired) > 1 and len(p.outputs) - len(p.kept) == 1:
        return f"Merged {len(p.retired)} documents: " + " + ".join(
            f"“{titles[str(d.uuid)]}”" for d in p.retired
        )
    if len(p.retired) == 1 and len(new) > 1:
        return f"Split “{titles[str(p.retired[0].uuid)]}” into {len(new)} documents"
    if len(p.retired) == 1 and len(new) == 1:
        source = p.retired[0]
        pages = p.outputs[new[0]].pages
        if {u for u, _ in pages} == {str(source.uuid)}:
            kept = [n for _, n in pages]
            if len(kept) < source.page_count:
                dropped = sorted(set(range(1, source.page_count + 1)) - set(kept))
                return f"Removed page {', '.join(map(str, dropped))} of “{titles[str(source.uuid)]}”"
            return f"Changed the page order of “{titles[str(source.uuid)]}”"
    return f"Rearranged the pages of {len(p.sources)} document(s) into {len(p.outputs)}"


def trash(
    document_ids: list[str], *, actor: str = Alteration.Actor.USER, origin: dict[str, Any] | None = None
) -> Alteration:
    return compose(document_ids, [], actor=actor, origin=origin)


class Conflict(AlterationError):
    pass


def _restore(documents: list[Document]) -> None:
    if len({d.content_hash for d in documents}) != len(documents):
        raise Conflict("These originals contain the same file. Restore one at a time.")
    for d in documents:
        if (
            Document.objects.filter(content_hash=d.content_hash, deleted_at__isnull=True)
            .exclude(pk=d.pk)
            .exists()
        ):
            raise Conflict(f"“{crypto_fields.get_title(d)}” cannot come back: the same file is there already")
    try:
        with transaction.atomic():
            Document.objects.filter(pk__in=[d.pk for d in documents]).update(deleted_at=None)
    except IntegrityError as exc:
        raise Conflict("The same file was restored or uploaded meanwhile. Refresh and try again.") from exc
    from apps.processing import pipeline

    for document in documents:
        if document.processing_state == Document.State.PENDING:
            pipeline.enqueue(document)


@transaction.atomic
def restore(document_ids: list[str]) -> Alteration:
    """Take documents out of the trash."""
    documents = list(
        Document.objects.select_for_update()
        .filter(uuid__in=document_ids, deleted_at__isnull=False)
        .order_by("pk")
    )
    if not documents:
        raise AlterationError("Nothing to restore")
    with transaction.atomic():
        _restore(documents)
        alteration = Alteration(
            kind=Alteration.Kind.RESTORE,
            sources=[str(d.uuid) for d in documents],
            results=[str(d.uuid) for d in documents],
        )
        _save_detail(
            alteration,
            {
                "summary": "Restored from the trash: "
                + ", ".join(f"“{crypto_fields.get_title(d)}”" for d in documents),
            },
        )
        alteration.save()
    return alteration


@transaction.atomic
def undo(alteration: Alteration) -> None:
    """The documents it made go to the trash; the ones it put there come back."""
    locked = Alteration.objects.select_for_update().get(pk=alteration.pk)
    if locked.undone_at:
        raise AlterationError("This was undone already")
    if alteration.kind == Alteration.Kind.EDIT:
        raise AlterationError("Edit the document's details to change them back")
    made = [u for u in alteration.results if u not in alteration.sources]
    if alteration.kind == Alteration.Kind.RESTORE:
        made, back = alteration.results, []
    else:
        back = alteration.retired
    involved = list(Document.objects.select_for_update().filter(uuid__in=made + back).order_by("pk"))
    active_made = [d for d in involved if str(d.uuid) in made and d.deleted_at is None]
    if len(active_made) != len(made):
        raise AlterationError("A document it made was changed or removed since; undo that first")
    if any(str(d.uuid) in back and d.deleted_at is None for d in involved):
        raise AlterationError("An original was restored since; undo that restoration first")
    if Job.objects.select_for_update().filter(document__in=involved, state=Job.State.RUNNING).exists():
        raise AlterationError("A document is being processed right now. Try again in a moment.")
    with transaction.atomic():
        Document.objects.filter(pk__in=[d.pk for d in active_made]).update(deleted_at=timezone.now())
        _restore(list(Document.objects.filter(uuid__in=back)))
        alteration.undone_at = timezone.now()
        alteration.save(update_fields=["undone_at"])
        origin = detail_of(alteration)
        if origin.get("task") and origin.get("finding"):
            from apps.assist.models import AssistTask

            task = AssistTask.objects.select_for_update().filter(pk=origin["task"]).first()
            if task:
                task.decisions = {k: v for k, v in task.decisions.items() if k != origin["finding"]}
                task.save(update_fields=["decisions"])


def history(uuid: str) -> list[Alteration]:
    from django.db.models import Q

    return list(
        Alteration.objects.filter(
            Q(sources__contains=[uuid]) | Q(results__contains=[uuid]) | Q(retired__contains=[uuid])
        )
    )


# --- Edits of the details --------------------------------------------------------------------

FIELD_NAMES = {
    "title": "Title",
    "document_date": "Date",
    "document_type": "Type",
    "folder": "Folder",
    "correspondent": "Sender",
    "status": "Status",
    "is_important": "Important",
    "series": "Series",
    "tags": "Tags",
    "has_paper": "Paper original",
    "paper_location": "Paper location",
    "read_at": "Read",
}


def snapshot(document: Document) -> dict[str, str]:
    """The details as the user reads them."""
    from apps.paper.services import location_paths
    from apps.taxonomy.services import folder_paths

    document = Document.objects.select_related(
        "document_type", "folder", "correspondent", "series", "paper_location"
    ).get(pk=document.pk)
    tags = sorted(dt.tag.name for dt in DocumentTag.objects.filter(document=document).select_related("tag"))
    return {
        "title": crypto_fields.get_title(document),
        "document_date": document.document_date.isoformat() if document.document_date else "",
        "document_type": document.document_type.name if document.document_type else "",
        "folder": folder_paths().get(document.folder_id, "") if document.folder_id else "",
        "correspondent": document.correspondent.name if document.correspondent else "",
        "status": document.status,
        "is_important": "yes" if document.is_important else "no",
        "series": document.series.name if document.series else "",
        "tags": ", ".join(tags),
        "has_paper": "yes" if document.has_paper else "no",
        "paper_location": location_paths().get(document.paper_location_id, "")
        if document.paper_location_id
        else "",
        "read_at": "yes" if document.read_at else "no",
    }


def record_edit(
    document: Document,
    before: dict[str, str],
    *,
    actor: str = Alteration.Actor.USER,
    origin: dict[str, Any] | None = None,
) -> Alteration | None:
    after = snapshot(document)
    changes = [
        {"field": FIELD_NAMES[k], "old": before.get(k, ""), "new": v}
        for k, v in after.items()
        if before.get(k, "") != v
    ]
    if not changes:
        return None
    uuid = str(document.uuid)
    alteration = Alteration(kind=Alteration.Kind.EDIT, actor=actor, sources=[uuid], results=[uuid])
    _save_detail(
        alteration,
        {
            "summary": "Changed " + ", ".join(c["field"].lower() for c in changes),
            "changes": changes,
            **(origin or {}),
        },
    )
    alteration.save()
    return alteration
