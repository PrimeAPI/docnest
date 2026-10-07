"""Scan sessions: uploading one document page by page.

A scanner with a document feeder opens a session, uploads every page as soon
as it is scanned (each request small and individually retryable), and then
completes the session. Completion turns the uploaded pages into a regular
multi-part intake (see `apps.documents.intake`), so the worker assembles the
PDF exactly as for a single-request upload.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.crypto.aead import decrypt_bytes, encrypt_bytes, encrypt_file
from apps.crypto.hashing import keyed_hash
from apps.documents.intake import (
    IntakeError,
    IntakeRequest,
    IntakeResult,
    check_limits,
    ensure_dirs,
    inspect_part,
    part_aad,
    part_filename,
    parts_dir_for,
    register,
    scan_hash,
    validate_request,
    write_manifest,
)
from apps.processing.assemble import AssemblyOptions
from apps.scanners.models import ScannerClient, ScanPage, ScanSession

COMPLETED_RETENTION = timedelta(days=7)
ORPHAN_AGE = timedelta(days=1)


def sessions_root() -> Path:
    return settings.INTAKE_DIR / "scans"


def session_dir(session: ScanSession) -> Path:
    return sessions_root() / str(session.uuid)


def _request_aad(session_uuid: UUID) -> bytes:
    return f"scan-session:{session_uuid}".encode()


def _ttl() -> timedelta:
    return timedelta(hours=settings.SCAN_SESSION_HOURS)


@dataclass
class SessionRequest:
    intake: IntakeRequest
    options: AssemblyOptions


def _encode(session_uuid: UUID, req: IntakeRequest, options: AssemblyOptions) -> bytes:
    data = {
        "bucket": req.bucket,
        "document_type": req.document_type,
        "important": req.important,
        "todo": req.todo,
        "tags": req.tags,
        "metadata": req.metadata,
        "options": options.to_json(),
    }
    return encrypt_bytes(json.dumps(data, ensure_ascii=False).encode(), aad=_request_aad(session_uuid))


def decode_request(session: ScanSession) -> SessionRequest:
    data = json.loads(decrypt_bytes(bytes(session.request_enc), aad=_request_aad(session.uuid)))
    return SessionRequest(
        intake=IntakeRequest(
            bucket=data["bucket"],
            document_type=data["document_type"],
            important=data["important"],
            todo=data["todo"],
            tags=data["tags"],
            metadata=data["metadata"],
        ),
        options=AssemblyOptions.from_json(data["options"]),
    )


def _is_expired(session: ScanSession) -> bool:
    return session.state == ScanSession.State.OPEN and session.expires_at <= timezone.now()


def create(
    scanner: ScannerClient, req: IntakeRequest, options: AssemblyOptions, *, idempotency_key: str = ""
) -> tuple[ScanSession, bool]:
    """Open a session. Returns (session, created); an existing session for the same key is reused."""
    key_hash = keyed_hash(idempotency_key.encode()) if idempotency_key else ""
    if key_hash:
        existing = ScanSession.objects.filter(scanner=scanner, idempotency_hash=key_hash).first()
        if existing is not None:
            return existing, False
    validate_request(req)
    open_sessions = ScanSession.objects.filter(
        scanner=scanner, state=ScanSession.State.OPEN, expires_at__gt=timezone.now()
    ).count()
    if open_sessions >= settings.MAX_OPEN_SCAN_SESSIONS:
        raise IntakeError("Too many open scan sessions; complete or delete one first", status=409)
    ensure_dirs()
    session = ScanSession(scanner=scanner, idempotency_hash=key_hash, expires_at=timezone.now() + _ttl())
    session.request_enc = _encode(session.uuid, req, options)
    try:
        with transaction.atomic():
            session.save()
    except IntegrityError:
        existing = ScanSession.objects.filter(scanner=scanner, idempotency_hash=key_hash).first()
        if existing is None:
            raise
        return existing, False
    session_dir(session).mkdir(parents=True, mode=0o700)
    return session, True


def get(scanner: ScannerClient, session_id: UUID) -> ScanSession:
    session = ScanSession.objects.filter(uuid=session_id, scanner=scanner).first()
    if session is None:
        raise IntakeError("Scan session not found", status=404)
    if _is_expired(session):
        raise IntakeError("Scan session expired", status=410)
    return session


def _locked(scanner: ScannerClient, session_id: UUID) -> ScanSession:
    session = ScanSession.objects.select_for_update().filter(uuid=session_id, scanner=scanner).first()
    if session is None:
        raise IntakeError("Scan session not found", status=404)
    if _is_expired(session):
        raise IntakeError("Scan session expired", status=410)
    return session


def add_page(scanner: ScannerClient, session_id: UUID, upload: Path, position: int | None) -> ScanPage:
    """Store one page file. Re-sending a page number replaces that page (safe retries)."""
    part = inspect_part(upload)  # outside the lock: hashing a large file takes a moment
    with transaction.atomic():
        session = _locked(scanner, session_id)
        if session.state != ScanSession.State.OPEN:
            raise IntakeError("Scan session is already completed", status=409)
        pages = session.pages.all()
        if position is None:
            position = (max((p.position for p in pages), default=0)) + 1
        if position < 1:
            raise IntakeError("page must be 1 or greater")
        if position > settings.MAX_PAGES:
            raise IntakeError(f"Too many pages (max {settings.MAX_PAGES})", status=413)
        previous = next((p for p in pages if p.position == position), None)
        others = [p for p in pages if p.position != position]
        check_limits(len(others) + 1, sum(p.size for p in others) + part.size)

        directory = session_dir(session)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A fresh file name per upload: a rolled-back retry never clobbers the stored page.
        name = f"{position:05d}-{secrets.token_hex(6)}.enc"
        encrypt_file(upload, directory / name, aad=part_aad(str(session.uuid), position))
        try:
            ScanPage.objects.filter(session=session, position=position).delete()
            page = ScanPage.objects.create(
                session=session,
                position=position,
                kind=part.kind,
                size=part.size,
                digest=part.digest,
                file_name=name,
            )
            session.expires_at = timezone.now() + _ttl()
            session.save(update_fields=["expires_at", "updated_at"])
        except BaseException:
            (directory / name).unlink(missing_ok=True)
            raise
        if previous is not None:
            transaction.on_commit(lambda: (directory / previous.file_name).unlink(missing_ok=True))
    return page


def complete(
    scanner: ScannerClient, session_id: UUID, *, expected_pages: int | None = None
) -> tuple[ScanSession, IntakeResult]:
    """Turn the uploaded pages into a document (idempotent)."""
    with transaction.atomic():
        session = _locked(scanner, session_id)
        if session.state == ScanSession.State.COMPLETED:
            if session.document is None:
                raise IntakeError("The document of this scan session was deleted", status=410)
            return session, IntakeResult(session.document, created=False)
        pages = list(session.pages.order_by("position"))
        if not pages:
            raise IntakeError("No pages uploaded", status=409)
        missing = sorted(set(range(1, pages[-1].position + 1)) - {p.position for p in pages})
        if missing:
            shown = ", ".join(str(n) for n in missing[:20])
            raise IntakeError(f"Missing page(s): {shown}", status=409)
        if expected_pages is not None and expected_pages != len(pages):
            raise IntakeError(f"Expected {expected_pages} page(s), received {len(pages)}", status=409)

        request = decode_request(session)
        directory = session_dir(session)
        _finalize_files(directory, session, pages, request.options)
        moved: list[Path] = []

        def store(doc_uuid: str) -> None:
            target = parts_dir_for(doc_uuid)
            os.rename(directory, target)
            moved.append(target)

        def discard(doc_uuid: str) -> None:
            target = parts_dir_for(doc_uuid)
            if target.exists():
                os.rename(target, directory)
            moved.clear()

        result = register(
            request.intake,
            digest=scan_hash([(p.kind, p.digest) for p in pages]),
            size=sum(p.size for p in pages),
            scanner=scanner,
            store=store,
            discard=discard,
        )
        try:
            session.state = ScanSession.State.COMPLETED
            session.document = result.document
            session.duplicate = not result.created
            session.save(update_fields=["state", "document", "duplicate", "updated_at"])
        except BaseException:
            for target in moved:
                os.rename(target, directory)
            raise
    if not result.created:
        shutil.rmtree(directory, ignore_errors=True)  # same content already archived
    return session, result


def _finalize_files(
    directory: Path, session: ScanSession, pages: list[ScanPage], options: AssemblyOptions
) -> None:
    """Give the pages their final part names and write the assembly manifest."""
    if not directory.exists():
        raise IntakeError("Scan session files are missing", status=410)
    keep = set()
    for page in pages:
        # Not recorded in the database: if registration rolls back, the next attempt finds
        # the page under its final name.
        final = part_filename(page.position)
        current = directory / page.file_name
        if page.file_name != final and current.exists():
            os.replace(current, directory / final)
        elif not (directory / final).exists():
            raise IntakeError(f"The file of page {page.position} is missing", status=410)
        keep.add(final)
    for stray in directory.iterdir():
        if stray.name not in keep:
            stray.unlink(missing_ok=True)
    write_manifest(directory, str(session.uuid), [(p.position, p.kind) for p in pages], options)


def delete(scanner: ScannerClient, session_id: UUID) -> None:
    with transaction.atomic():
        session = ScanSession.objects.select_for_update().filter(uuid=session_id, scanner=scanner).first()
        if session is None:
            raise IntakeError("Scan session not found", status=404)
        if session.state == ScanSession.State.COMPLETED:
            raise IntakeError("Scan session is already completed", status=409)
        directory = session_dir(session)
        session.delete()
    shutil.rmtree(directory, ignore_errors=True)


def total_size(session: ScanSession) -> int:
    return int(session.pages.aggregate(total=Sum("size"))["total"] or 0)


def cleanup() -> int:
    """Remove expired open sessions, old completed ones and orphaned page files."""
    now = timezone.now()
    removed = 0
    for session in ScanSession.objects.filter(state=ScanSession.State.OPEN, expires_at__lte=now):
        shutil.rmtree(session_dir(session), ignore_errors=True)
        session.delete()
        removed += 1
    ScanSession.objects.filter(
        state=ScanSession.State.COMPLETED, updated_at__lt=now - COMPLETED_RETENTION
    ).delete()
    root = sessions_root()
    if root.exists():
        open_ids = ScanSession.objects.filter(state=ScanSession.State.OPEN).values_list("uuid", flat=True)
        known = {str(u) for u in open_ids}
        cutoff = (now - ORPHAN_AGE).timestamp()
        for entry in root.iterdir():
            if entry.name not in known and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
    return removed
