"""Document processing pipeline.

Stages run in order and each is idempotent. `Document.processing_stage` is the
next stage to run, so a retry resumes where the previous attempt stopped.
Until the storage stage succeeded, the (encrypted) files live in the intake
volume; after it, the storage backend holds them and the intake is cleared.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.analysis import docling_fields
from apps.analysis.analyze import analyze
from apps.crypto.aead import decrypt_file, encrypt_file
from apps.documents import crypto_fields
from apps.documents.intake import (
    archive_aad,
    archive_intake_path_for,
    enhanced_aad,
    enhanced_intake_path_for,
    intake_aad,
    intake_path_for,
    part_aad,
    part_filename,
    parts_dir_for,
    read_manifest,
    remove_parts,
)
from apps.documents.models import Document, ProcessingEvent
from apps.processing import assemble, docling_backend, pdf
from apps.processing.enhance_settings import EnhanceSettings
from apps.processing.models import SystemState
from apps.processing.preferences import (
    get_default_ocr_backend,
    get_docling_field_detection,
    get_enhance_settings,
)
from apps.search.index import IndexInput, index_document
from apps.storage.backends import StorageAuthError, StoredObject, get_backend

logger = logging.getLogger(__name__)

Stage = Document.Stage
ORDER: list[str] = [
    Stage.RECEIVED,
    Stage.ASSEMBLE,
    Stage.VALIDATE,
    Stage.ENHANCE,
    Stage.OCR,
    Stage.ANALYZE,
    Stage.STORE,
    Stage.INDEX,
    Stage.DONE,
]
STORAGE_STATE_KEY = "storage_status"


class PermanentError(Exception):
    """Processing cannot succeed by retrying (e.g. not a valid PDF)."""


class StorageUnavailable(Exception):
    """Storage needs operator action (re-login); retry later without using attempts."""


@contextmanager
def workspace(document: Document) -> Iterator[Path]:
    root = settings.WORK_DIR / f"doc-{document.uuid}"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, mode=0o700)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def archive_intake_path(document: Document) -> Path:
    return archive_intake_path_for(str(document.uuid))


def _archive_aad(document: Document) -> bytes:
    return archive_aad(str(document.uuid))


def _event(document: Document, stage: str, outcome: str, started: float, message: str = "") -> None:
    ProcessingEvent.objects.create(
        document=document,
        stage=stage,
        outcome=outcome,
        message=message[:500],
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _advance(document: Document, stage: str) -> None:
    document.processing_stage = stage
    document.save(update_fields=["processing_stage", "updated_at"])


def _storage_folder(document: Document) -> str:
    year = (document.uploaded_at or timezone.now()).year
    # Independent of the filing folder, so moving a document never moves files in storage.
    return f"{year}/{document.uuid}"


def _original_local(document: Document, work: Path) -> Path:
    """Plaintext original in the workspace (from intake, or from storage on reprocessing)."""
    target = work / "original.pdf"
    if target.exists():
        return target
    intake = intake_path_for(str(document.uuid))
    if intake.exists():
        decrypt_file(intake, target, aad=intake_aad(str(document.uuid)))
    elif document.storage_original:
        _download(document, StoredObject.from_json(document.storage_original), target)
    else:
        raise PermanentError("Original file is missing")
    return target


def _archive_local(document: Document, work: Path) -> Path:
    target = work / "archive.pdf"
    if target.exists():
        return target
    intake = archive_intake_path(document)
    if intake.exists():
        decrypt_file(intake, target, aad=_archive_aad(document))
    elif document.storage_archive:
        _download(document, StoredObject.from_json(document.storage_archive), target)
    else:
        raise PermanentError("Archive file is missing")
    return target


def _enhanced_local(document: Document, work: Path) -> Path | None:
    """Plaintext enhanced version, if the enhance stage produced one."""
    target = work / "enhanced.pdf"
    if target.exists():
        return target
    intake = enhanced_intake_path_for(str(document.uuid))
    if intake.exists():
        decrypt_file(intake, target, aad=enhanced_aad(str(document.uuid)))
        return target
    return None


def _ocr_source(document: Document, work: Path) -> Path:
    """Input for OCR / Docling: the enhanced version, else the original."""
    return _enhanced_local(document, work) or _original_local(document, work)


def _processed_local(document: Document, work: Path) -> Path:
    """The processed document as shown to the user (for later stages such as the VLM)."""
    enhanced = _enhanced_local(document, work)
    if enhanced:
        return enhanced
    if (work / "archive.pdf").exists() or archive_intake_path(document).exists() or document.storage_archive:
        return _archive_local(document, work)
    return _original_local(document, work)


def _download(document: Document, ref: StoredObject, target: Path) -> None:
    try:
        get_backend().get(ref, target)
    except StorageAuthError as exc:
        raise StorageUnavailable(str(exc)) from exc


# --- Stages -------------------------------------------------------------------


def stage_assemble(document: Document, work: Path) -> None:
    """Build the original PDF from uploaded scanner parts (page images / several files)."""
    doc_uuid = str(document.uuid)
    directory = parts_dir_for(doc_uuid)
    if not directory.exists():
        return  # uploaded as a single PDF: nothing to assemble
    manifest = read_manifest(directory)
    batch = str(manifest["batch"])

    def materializer(position: int) -> Callable[[], Path]:
        def materialize() -> Path:
            target = work / f"part-{position:05d}"
            decrypt_file(directory / part_filename(position), target, aad=part_aad(batch, position))
            return target

        return materialize

    parts = [(str(e["kind"]), materializer(int(e["position"]))) for e in manifest["parts"]]
    original = work / "original.pdf"
    try:
        result = assemble.assemble(parts, original, assemble.AssemblyOptions.from_json(manifest["options"]))
    except assemble.AssemblyFailed as exc:
        raise PermanentError(str(exc)) from exc
    finally:
        for leftover in work.glob("part-*"):
            leftover.unlink(missing_ok=True)
    # The assembled PDF is the document's original from here on; the raw parts can go.
    encrypt_file(original, intake_path_for(doc_uuid), aad=intake_aad(doc_uuid))
    remove_parts(doc_uuid)
    document.size = original.stat().st_size
    fields = ["size"]
    if manifest["options"].get("skip_blank_pages"):
        document.enhancement = {**document.enhancement, "scanner_remove_blank": True}
        fields.append("enhancement")
    document.save(update_fields=fields)
    logger.debug("assembled scan", extra={"document": doc_uuid, "pages": result.page_count})


def stage_validate(document: Document, work: Path) -> None:
    src = _original_local(document, work)
    sanitized = work / "sanitized.pdf"
    try:
        result = pdf.sanitize(src, sanitized)
    except pdf.PdfRejected as exc:
        raise PermanentError(str(exc)) from exc
    # The sanitized file becomes the stored original.
    sanitized.replace(src)
    if not document.storage_original:
        encrypt_file(src, intake_path_for(str(document.uuid)), aad=intake_aad(str(document.uuid)))
    document.page_count = result.page_count
    document.original_page_count = result.page_count
    document.save(update_fields=["page_count", "original_page_count"])


def enhancement_settings(document: Document) -> EnhanceSettings:
    """Settings for the next enhance run: a one-off override, else the system settings."""
    override = document.enhancement.get("override")
    options = EnhanceSettings.from_json(override, base=get_enhance_settings()) if override else None
    options = options or get_enhance_settings()
    if document.enhancement.get("scanner_remove_blank") and not options.remove_blank:
        options = EnhanceSettings.from_json({"remove_blank": True}, base=options)
    return options


def stage_enhance(document: Document, work: Path) -> None:
    from apps.processing import enhance  # image libraries are only needed in the worker

    uuid = str(document.uuid)
    src = _original_local(document, work)
    target = work / "enhanced.pdf"
    target.unlink(missing_ok=True)
    enhanced_intake_path_for(uuid).unlink(missing_ok=True)
    options = enhancement_settings(document)
    if options.enabled:
        result = enhance.enhance(src, target, options, work)
        summary: dict[str, object] = dict(result.summary())
        message = result.describe()
        if result.changed:
            encrypt_file(target, enhanced_intake_path_for(uuid), aad=enhanced_aad(uuid))
        page_count = result.page_count
    else:
        summary, message = {}, "Scan enhancement is switched off"
        page_count = document.original_page_count or document.page_count
    state = {k: v for k, v in document.enhancement.items() if k != "override"}
    document.enhancement = {**state, "settings": options.to_json(), "summary": summary}
    document.page_count = page_count
    document.save(update_fields=["enhancement", "page_count"])
    ProcessingEvent.objects.create(document=document, stage=Stage.ENHANCE, outcome="info", message=message)


def stage_ocr(document: Document, work: Path) -> None:
    src = _ocr_source(document, work)
    archive = work / "archive.pdf"
    archive.unlink(missing_ok=True)
    backend = document.ocr_backend or get_default_ocr_backend()
    if backend == Document.OcrBackend.DOCLING:
        result = docling_backend.convert(src)
        # Docling produces a structured document rather than a searchable PDF.
        # Keep the (enhanced) input as the archive and expose its richer output separately.
        shutil.copyfile(src, archive)
        crypto_fields.set_content(
            document,
            result.markdown,
            structured=result.structured,
            layout=result.layout,
            content_format="markdown",
        )
        document.ocr_backend = backend
        document.save(update_fields=["ocr_backend"])
        thumb = pdf.thumbnail(archive)
        if thumb:
            crypto_fields.set_thumbnail(document, thumb)
        encrypt_file(archive, archive_intake_path(document), aad=_archive_aad(document))
        return
    if backend != Document.OcrBackend.OCRMYPDF:
        raise PermanentError(f"Unknown OCR backend: {backend}")

    message = ""
    # OCRmyPDF straightens and rotates on its own only where DocNest's enhancement did not.
    used = EnhanceSettings.from_json(document.enhancement.get("settings") or {"enabled": False})
    try:
        pdf.ocr(
            src, archive, deskew=not (used.enabled and used.deskew), rotate=not (used.enabled and used.rotate)
        )
    except pdf.OcrFailed as exc:
        # Keep the document usable: archive = original, text from any existing text layer.
        logger.warning("OCR failed, continuing without text layer", extra={"document": str(document.uuid)})
        shutil.copyfile(src, archive)
        message = str(exc)
    text = pdf.extract_text(archive)
    crypto_fields.set_content(document, text, content_format="text")
    document.ocr_backend = backend
    document.save(update_fields=["ocr_backend"])
    thumb = pdf.thumbnail(archive)
    if thumb:
        crypto_fields.set_thumbnail(document, thumb)
    encrypt_file(archive, archive_intake_path(document), aad=_archive_aad(document))
    if message:
        ProcessingEvent.objects.create(document=document, stage=Stage.OCR, outcome="warning", message=message)


def stage_analyze(document: Document, work: Path) -> None:
    text = crypto_fields.get_content(document)
    if document.ocr_backend != Document.OcrBackend.DOCLING:
        analyze(document, text)
        return

    structure = crypto_fields.get_structure(document)
    layout = crypto_fields.get_layout(document)
    detected = docling_fields.detect(structure, layout)
    evidence_lines = [
        line.get("text", "")
        for page in layout.get("pages", [])
        if isinstance(page, dict)
        for line in page.get("lines", [])
        if isinstance(line, dict) and isinstance(line.get("text"), str)
    ]
    evidence = "\n".join([*evidence_lines, text])
    mode = get_docling_field_detection()
    if mode == "vlm" or (mode == "hybrid" and detected.low_confidence):
        try:
            vlm = docling_backend.extract_fields(_processed_local(document, work))
            detected = docling_fields.merge_vlm(detected, vlm.sender, vlm.title, evidence)
        except docling_backend.DoclingFailed:
            logger.warning(
                "Docling VLM field extraction failed; using layout detection",
                extra={"document": str(document.uuid)},
            )
            ProcessingEvent.objects.create(
                document=document,
                stage=Stage.ANALYZE,
                outcome="warning",
                message="VLM field extraction failed; layout detection was used",
            )
    analyze(document, text, detected_fields=detected, context_text=evidence)


def stage_store(document: Document, work: Path) -> None:
    backend = get_backend()
    folder = _storage_folder(document)
    try:
        if not document.storage_original or archive_intake_path(document).exists():
            original = _original_local(document, work)
            archive = _archive_local(document, work)
            ref_original = backend.put(original, folder, "original.pdf")
            ref_archive = backend.put(archive, folder, "archive.pdf")
            document.storage_original = ref_original.to_json()
            document.storage_archive = ref_archive.to_json()
            document.save(update_fields=["storage_original", "storage_archive"])
    except StorageAuthError as exc:
        SystemState.objects.update_or_create(
            key=STORAGE_STATE_KEY,
            defaults={"value": {"ok": False, "needs_reauth": True, "message": str(exc)}},
        )
        raise StorageUnavailable(str(exc)) from exc
    # Verified in permanent storage: the intake copies can go.
    intake_path_for(str(document.uuid)).unlink(missing_ok=True)
    archive_intake_path(document).unlink(missing_ok=True)
    enhanced_intake_path_for(str(document.uuid)).unlink(missing_ok=True)
    document.intake_path = ""
    document.save(update_fields=["intake_path"])


def stage_index(document: Document, work: Path) -> None:
    reindex(document)


def reindex(document: Document) -> None:
    document = Document.objects.select_related("correspondent", "document_type", "series").get(pk=document.pk)
    meta = [t.name for t in document.tags.all()]
    meta += [a.alias for t in document.tags.all() for a in t.aliases.all()]
    if document.correspondent:
        meta.append(document.correspondent.name)
    if document.document_type:
        meta.append(document.document_type.name)
    if document.series:
        meta.append(document.series.name)
    meta.append(crypto_fields.get_original_filename(document))
    index_document(
        document.pk,
        IndexInput(
            title=crypto_fields.get_title(document),
            content=crypto_fields.get_content(document),
            meta=[m for m in meta if m],
        ),
    )


STAGES: dict[str, Callable[[Document, Path], None]] = {
    Stage.ASSEMBLE: stage_assemble,
    Stage.VALIDATE: stage_validate,
    Stage.ENHANCE: stage_enhance,
    Stage.OCR: stage_ocr,
    Stage.ANALYZE: stage_analyze,
    Stage.STORE: stage_store,
    Stage.INDEX: stage_index,
}


def run(document_id: int) -> None:
    """Run all outstanding stages of a document."""
    document = Document.objects.get(pk=document_id)
    if document.deleted_at is not None:
        return
    if document.processing_stage == Stage.RECEIVED:
        _advance(document, Stage.ASSEMBLE)
    Document.objects.filter(pk=document.pk).update(
        processing_state=Document.State.RUNNING, processing_error=""
    )

    with workspace(document) as work:
        while document.processing_stage != Stage.DONE:
            stage = document.processing_stage
            started = time.monotonic()
            try:
                STAGES[stage](document, work)
            except Exception as exc:
                _event(document, stage, "failed", started, _describe(exc))
                raise
            _event(document, stage, "ok", started)
            document.refresh_from_db()
            _advance(document, ORDER[ORDER.index(stage) + 1])

    with transaction.atomic():
        Document.objects.filter(pk=document.pk).update(
            processing_state=Document.State.DONE, processing_error="", updated_at=timezone.now()
        )


def restart_from(document: Document, stage: str) -> None:
    """Prepare a document for reprocessing from `stage` (used by the reprocess action)."""
    document.processing_stage = stage
    document.processing_state = Document.State.PENDING
    document.processing_error = ""
    document.save(update_fields=["processing_stage", "processing_state", "processing_error"])


def _describe(exc: Exception) -> str:
    # Messages are written by us or by tools describing failures — never document text.
    return f"{type(exc).__name__}: {exc}"[:500]
