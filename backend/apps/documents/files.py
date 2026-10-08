"""Serving document files: fetched from storage into a private temp file,
streamed to the client, and deleted as soon as the response is closed."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path

from django.conf import settings

from apps.crypto.aead import decrypt_file, encrypt_file
from apps.documents.intake import (
    archive_aad,
    archive_intake_path_for,
    enhanced_aad,
    enhanced_intake_path_for,
    intake_aad,
    intake_path_for,
)
from apps.documents.models import Document
from apps.storage.backends import StoredObject, get_backend


class SelfDeletingFile:
    """File object that removes its directory on close (used by FileResponse)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = open(path, "rb")  # noqa: SIM115

    def read(self, size: int = -1) -> bytes:
        return self._fh.read(size)

    def seek(self, *args: int) -> int:
        return self._fh.seek(*args)

    def tell(self) -> int:
        return self._fh.tell()

    @property
    def name(self) -> str:
        return str(self.path)

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            shutil.rmtree(self.path.parent, ignore_errors=True)


def _view_cache_path(document: Document, variant: str) -> Path:
    name = hashlib.sha256(f"{document.uuid}:{variant}".encode()).hexdigest()
    return settings.VIEW_CACHE_DIR / f"{name}.enc"


def _cache_aad(document: Document, variant: str) -> bytes:
    return f"view-cache:{document.uuid}:{variant}".encode()


def fetch(document: Document, variant: str) -> SelfDeletingFile:
    """Return a readable, self-deleting plaintext copy of the requested file."""
    settings.WORK_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmpdir = Path(tempfile.mkdtemp(prefix="dl-", dir=settings.WORK_DIR))
    target = tmpdir / f"{variant}.pdf"
    try:
        _materialize(document, variant, target)
    except BaseException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    return SelfDeletingFile(target)


def _materialize(document: Document, variant: str, target: Path) -> None:
    uuid = str(document.uuid)
    # 1) Still in the intake (not yet in permanent storage)
    if variant == "original" and intake_path_for(uuid).exists():
        decrypt_file(intake_path_for(uuid), target, aad=intake_aad(uuid))
        return
    if variant == "archive" and archive_intake_path_for(uuid).exists():
        decrypt_file(archive_intake_path_for(uuid), target, aad=archive_aad(uuid))
        return
    # Enhanced but not yet recognised: readable while the analysis is still running.
    if variant == "archive" and enhanced_intake_path_for(uuid).exists():
        decrypt_file(enhanced_intake_path_for(uuid), target, aad=enhanced_aad(uuid))
        return
    ref_json = document.storage_archive if variant == "archive" else document.storage_original
    if not ref_json:
        if variant == "archive":
            return _materialize(document, "original", target)
        raise FileNotFoundError("file not available yet")
    # 2) Encrypted view cache (optional)
    cache = _view_cache_path(document, variant)
    if settings.VIEW_CACHE_MB > 0 and cache.exists():
        try:
            decrypt_file(cache, target, aad=_cache_aad(document, variant))
            os.utime(cache)
            return
        except Exception:
            cache.unlink(missing_ok=True)
    # 3) Permanent storage
    get_backend().get(StoredObject.from_json(ref_json), target)
    if settings.VIEW_CACHE_MB > 0:
        _store_in_cache(document, variant, target)


def _store_in_cache(document: Document, variant: str, source: Path) -> None:
    settings.VIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    encrypt_file(source, _view_cache_path(document, variant), aad=_cache_aad(document, variant))
    limit = settings.VIEW_CACHE_MB * 1024 * 1024
    entries = sorted(settings.VIEW_CACHE_DIR.glob("*.enc"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in entries)
    cutoff = time.time() - 7 * 86400
    for p in entries:
        if total <= limit and p.stat().st_mtime > cutoff:
            break
        total -= p.stat().st_size
        p.unlink(missing_ok=True)


def evict(document: Document) -> None:
    for variant in ("original", "archive"):
        _view_cache_path(document, variant).unlink(missing_ok=True)
