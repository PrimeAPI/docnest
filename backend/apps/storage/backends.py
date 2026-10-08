"""Permanent document storage.

The service only holds documents temporarily; the storage backend is the
permanent home of every file. Backends work with `StoredObject` references
that are saved on the document.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from django.conf import settings

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """A storage operation failed; it can be retried later."""


class StorageAuthError(StorageError):
    """The storage backend needs the operator to log in again."""


class StorageNotFound(StorageError):
    pass


@dataclass(frozen=True)
class StoredObject:
    backend: str
    path: str
    size: int
    sha1: str
    node_uid: str = ""

    def to_json(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, object]) -> StoredObject:
        return cls(**data)  # type: ignore[arg-type]


@dataclass
class HealthStatus:
    ok: bool
    message: str = ""
    needs_reauth: bool = False


class StorageBackend(Protocol):
    name: str

    def put(self, local_path: Path, folder: str, filename: str) -> StoredObject: ...

    def get(self, ref: StoredObject, dest: Path) -> None: ...

    def delete_folder(self, folder: str) -> None: ...

    def delete_file(self, path: str) -> None: ...

    def healthcheck(self) -> HealthStatus: ...


def file_sha1(path: Path) -> str:
    h = hashlib.sha1(usedforsecurity=False)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_relative(folder: str, filename: str) -> PurePosixPath:
    rel = PurePosixPath(folder) / filename
    if rel.is_absolute() or ".." in rel.parts or any(p in {"", "."} for p in rel.parts):
        raise StorageError("invalid storage path")
    return rel


# --- Local filesystem ---------------------------------------------------------


class LocalFilesystemBackend:
    """Stores files under a local directory. Used for development, tests and CI."""

    name = "local"

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or settings.STORAGE_LOCAL_DIR)

    def put(self, local_path: Path, folder: str, filename: str) -> StoredObject:
        rel = _safe_relative(folder, filename)
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = target.with_name(target.name + ".partial")
        shutil.copyfile(local_path, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        sha1 = file_sha1(target)
        if sha1 != file_sha1(local_path):
            raise StorageError("verification failed after upload")
        return StoredObject(self.name, str(rel), target.stat().st_size, sha1)

    def get(self, ref: StoredObject, dest: Path) -> None:
        source = self.root / _safe_relative(*os.path.split(ref.path))
        if not source.exists():
            raise StorageNotFound(ref.path)
        shutil.copyfile(source, dest)

    def delete_folder(self, folder: str) -> None:
        target = self.root / _safe_relative(*os.path.split(folder))
        shutil.rmtree(target, ignore_errors=True)

    def delete_file(self, path: str) -> None:
        (self.root / _safe_relative(*os.path.split(path))).unlink(missing_ok=True)

    def healthcheck(self) -> HealthStatus:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe = self.root / ".healthcheck"
            probe.write_text("ok")
            probe.unlink()
            return HealthStatus(ok=True)
        except OSError as exc:
            return HealthStatus(ok=False, message=f"local storage not writable: {exc.strerror}")


# --- Proton Drive -------------------------------------------------------------

_AUTH_MARKERS = ("need to login", "login first", "session expired", "invalid refresh token", "unauthorized")


class ProtonDriveCliBackend:
    """Wraps the official Proton Drive CLI (see docs/proton-drive.md).

    Every call goes through the `docnest-proton` wrapper, which serializes CLI
    invocations and uses DocNest's encrypted credentials store.
    """

    name = "proton"

    def __init__(self, root: str | None = None, cli: str | None = None, timeout: int | None = None) -> None:
        self.root = PurePosixPath(root or str(settings.PROTON_ROOT))
        self.cli: str = cli or str(settings.PROTON_CLI)
        self.timeout = timeout or settings.PROTON_TIMEOUT_SECONDS
        self._known_folders: set[str] = set()

    # -- low level

    def _run(self, *args: str) -> str:
        cmd = [self.cli, *args, "--json"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise StorageError("Proton Drive CLI not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise StorageError(f"Proton Drive CLI timed out after {self.timeout}s") from exc
        if proc.returncode == 75:  # flock -E 75: another CLI call held the lock for too long
            raise StorageError("Proton Drive CLI is busy (lock timeout); will retry")
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "").strip().splitlines()
            first = message[-1][:300] if message else f"exit code {proc.returncode}"
            lowered = (proc.stderr or "").lower()
            if any(marker in lowered for marker in _AUTH_MARKERS):
                raise StorageAuthError("Proton Drive session expired — run `docnest proton-login`")
            if "node not found" in lowered:
                raise StorageNotFound(first)
            raise StorageError(f"Proton Drive CLI failed: {first}")
        return proc.stdout

    def _json(self, *args: str) -> object:
        out = self._run(*args)
        try:
            return json.loads(out) if out.strip() else None
        except json.JSONDecodeError as exc:
            raise StorageError("unexpected Proton Drive CLI output") from exc

    def _ensure_folder(self, path: PurePosixPath) -> None:
        key = str(path)
        if key in self._known_folders or path == PurePosixPath("/my-files"):
            return
        self._ensure_folder(path.parent)
        try:
            self._json("filesystem", "info", key)
        except StorageNotFound:
            try:
                self._json("filesystem", "create-folder", str(path.parent), path.name)
            except StorageError:
                # Created concurrently or already existing: verify instead of failing.
                self._json("filesystem", "info", key)
        self._known_folders.add(key)

    def _info(self, path: str) -> dict[str, object]:
        data = self._json("filesystem", "info", path)
        if not isinstance(data, dict):
            raise StorageError("unexpected info output")
        return data

    # -- interface

    def put(self, local_path: Path, folder: str, filename: str) -> StoredObject:
        rel = _safe_relative(folder, filename)
        remote_parent = self.root / rel.parent
        self._ensure_folder(remote_parent)
        size = local_path.stat().st_size
        sha1 = file_sha1(local_path)
        # The CLI names the remote file after the local file: upload from a temp
        # directory holding a copy with the desired name.
        with tempfile.TemporaryDirectory(dir=settings.WORK_DIR) as tmpdir:
            staged = Path(tmpdir) / rel.name
            if _same_fs(local_path, staged):
                os.link(local_path, staged)
            else:
                shutil.copyfile(local_path, staged)
            result = self._json(
                "filesystem",
                "upload",
                "--file-conflict-strategy",
                "replace",
                "--skip-thumbnails",
                str(staged),
                str(remote_parent),
            )
        if isinstance(result, dict) and result.get("failedItems"):
            raise StorageError("Proton Drive upload reported failures")
        remote_path = str(remote_parent / rel.name)
        info = self._info(remote_path)
        revision = info.get("activeRevision") or {}
        claimed_size = revision.get("claimedSize") if isinstance(revision, dict) else None
        digests = revision.get("claimedDigests", {}) if isinstance(revision, dict) else {}
        remote_sha1 = digests.get("sha1") if isinstance(digests, dict) else None
        if claimed_size != size or (remote_sha1 and remote_sha1 != sha1):
            raise StorageError("verification failed after upload (size/hash mismatch)")
        return StoredObject(self.name, remote_path, size, sha1, str(info.get("uid", "")))

    def get(self, ref: StoredObject, dest: Path) -> None:
        with tempfile.TemporaryDirectory(dir=settings.WORK_DIR) as tmpdir:
            self._json("filesystem", "download", "--file-conflict-strategy", "remove", ref.path, tmpdir)
            downloaded = Path(tmpdir) / PurePosixPath(ref.path).name
            if not downloaded.exists():
                raise StorageError("download produced no file")
            if file_sha1(downloaded) != ref.sha1:
                raise StorageError("downloaded file does not match the stored checksum")
            shutil.move(downloaded, dest)

    def delete_folder(self, folder: str) -> None:
        rel = _safe_relative(*os.path.split(folder))
        try:
            self._json("filesystem", "trash", str(self.root / rel))
        except StorageNotFound:
            return
        # Trash entries are addressed by name; document folders are UUIDs, so the
        # name is unique and the permanent delete cannot hit anything else.
        try:
            self._json("filesystem", "delete", f"/trash/{rel.name}")
        except StorageError:
            logger.warning("could not permanently delete trashed folder; it remains in Proton Drive trash")

    def delete_file(self, path: str) -> None:
        """Delete a file this backend returned from `put` (its `StoredObject.path`)."""
        remote = PurePosixPath(path)
        if not remote.is_relative_to(self.root) or ".." in remote.parts:
            raise StorageError("invalid storage path")
        try:
            self._json("filesystem", "trash", str(remote))
        except StorageNotFound:
            return
        # Callers only delete uniquely named files (e.g. timestamped backups).
        try:
            self._json("filesystem", "delete", f"/trash/{remote.name}")
        except StorageError:
            logger.warning("could not permanently delete trashed file; it remains in Proton Drive trash")

    def healthcheck(self) -> HealthStatus:
        try:
            self._json("filesystem", "list", "/")
            return HealthStatus(ok=True)
        except StorageAuthError as exc:
            return HealthStatus(ok=False, message=str(exc), needs_reauth=True)
        except StorageError as exc:
            return HealthStatus(ok=False, message=str(exc))


def _same_fs(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_dev == b.parent.stat().st_dev
    except OSError:
        return False


def get_backend() -> StorageBackend:
    if settings.STORAGE_BACKEND == "proton":
        return ProtonDriveCliBackend()
    return LocalFilesystemBackend()
