"""Export a whole instance into one password-protected ZIP, and restore it on another server.

The archive (AES-256 ZIP, opens with 7-Zip) holds everything that is not in
Proton Drive:

    manifest.json          what was exported: version, applied migrations, settings
    master_key             the instance's master key: all encrypted data depends on it
    database.dump          the whole database (pg_dump custom format)
    data/…                 the data volume: files waiting for Proton Drive, scan sessions,
                           local storage
    proton-session/…       the Proton Drive sign-in (encrypted with the master key)
    proton-cache/…         the Proton Drive CLI's cache

Left out: caches that fill themselves again (view cache, Docling models),
debug logs and the server's own secrets (`./secrets`). Settings from `.env`
are listed in the manifest so an import can say which differ.

An import replaces the database, the data volume and the Proton session, and
makes the exported master key this instance's master key (`docnest/instance_key.py`).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

import pyzipper
from django.conf import settings
from django.db import connection
from django.utils import timezone

from apps.core.debuglog import is_secret
from apps.storage import backup
from apps.transfer import access, accounts
from apps.transfer.state import TransferError, progress, write_status
from docnest import instance_key

logger = logging.getLogger(__name__)

FORMAT = "docnest-export"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
MASTER_KEY = "master_key"
DATABASE = "database.dump"
# Extracted next to the files it replaces (same volume, so the swap is a rename).
STAGING = ".docnest-restore"
SKIPPED_NAMES = {".lock", ".cli.lock", STAGING}
SKIPPED_SUFFIXES = (".partial", ".tmp")
# Image and compose wiring, not settings of the instance.
INTERNAL_ENV = {
    "DOCNEST_DATA_DIR",
    "DOCNEST_FRONTEND_DIR",
    "DOCNEST_WORK_DIR",
    "DOCNEST_TRANSFER_DIR",
    "DOCNEST_DOCLING_ARTIFACTS_PATH",
    "DOCNEST_PROTON_SESSION_DIR",
    "DOCNEST_VERSION",
    "DOCNEST_COMMIT",
    "DOCNEST_BUILD_DATE",
    "DOCNEST_DB_HOST",
    "DOCNEST_DB_PORT",
    "DOCNEST_DB_NAME",
    "DOCNEST_DB_USER",
    "DOCNEST_DEV",
    "DOCNEST_DEBUG",
    "DOCNEST_TESTING",
}
SUPERVISORD_PID = Path("/tmp/supervisord.pid")  # noqa: S108 - set in docker/supervisord.conf
MIGRATE_TIMEOUT_SECONDS = 3600


# --- What belongs to an instance ------------------------------------------------


def roots() -> dict[str, Path]:
    """Directories with instance data, by their name in the archive."""
    found = {"data": Path(settings.DATA_DIR)}
    if not _inside(Path(settings.INTAKE_DIR), Path(settings.DATA_DIR)):
        found["intake"] = Path(settings.INTAKE_DIR)
    if settings.STORAGE_BACKEND == "local" and not _inside(
        Path(settings.STORAGE_LOCAL_DIR), Path(settings.DATA_DIR)
    ):
        found["storage"] = Path(settings.STORAGE_LOCAL_DIR)
    if settings.PROTON_SESSION_DIR:
        found["proton-session"] = Path(settings.PROTON_SESSION_DIR)
    if settings.PROTON_CACHE_DIR:
        found["proton-cache"] = Path(settings.PROTON_CACHE_DIR)
    return found


def _excluded() -> list[Path]:
    """Paths inside the roots that are not part of an export."""
    return [
        Path(settings.TRANSFER_DIR),
        Path(settings.VIEW_CACHE_DIR),
        Path(settings.DEBUG_LOG_DIR),
        Path(settings.WORK_DIR),
        Path(settings.DOCLING_ARTIFACTS_PATH),
        instance_key.path(Path(settings.DATA_DIR)),
    ]


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _files(root: Path, excluded: list[Path]) -> Iterator[tuple[str, Path]]:
    """Regular files below `root` as (relative POSIX path, path); symlinks are skipped."""
    resolved = [p.resolve() for p in excluded]
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in SKIPPED_NAMES and not (here / d).is_symlink() and (here / d).resolve() not in resolved
        )
        for name in sorted(filenames):
            path = here / name
            if (
                name in SKIPPED_NAMES
                or name.endswith(SKIPPED_SUFFIXES)
                or path.is_symlink()
                or path.resolve() in resolved
            ):
                continue
            yield path.relative_to(root).as_posix(), path


def _config() -> dict[str, str]:
    """The settings this instance runs with from `.env` (secrets left out)."""
    return {
        name: value
        for name, value in sorted(os.environ.items())
        if name.startswith("DOCNEST_")
        and name not in INTERNAL_ENV
        and not name.endswith("_FILE")
        and not is_secret(name)
    }


def _manifest() -> dict[str, Any]:
    from django.db.migrations.recorder import MigrationRecorder

    from apps.accounts.models import User
    from apps.documents.models import Document

    migrations: dict[str, list[str]] = {}
    for app, name in sorted(MigrationRecorder(connection).applied_migrations()):
        migrations.setdefault(app, []).append(name)
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "created_at": timezone.now().isoformat(),
        "docnest_version": os.environ.get("DOCNEST_VERSION", "dev"),
        "docnest_commit": os.environ.get("DOCNEST_COMMIT", ""),
        "base_url": settings.BASE_URL,
        "storage_backend": settings.STORAGE_BACKEND,
        "proton_root": settings.PROTON_ROOT,
        "counts": {
            "users": User.objects.count(),
            "documents": Document.objects.filter(deleted_at__isnull=True).count(),
        },
        "migrations": migrations,
        "config": _config(),
    }


# --- Export --------------------------------------------------------------------


def _open_for_writing(path: Path, password: str) -> pyzipper.AESZipFile:
    # Stored, not deflated: the dump is compressed and every other file is encrypted already.
    zf = pyzipper.AESZipFile(path, "w", compression=pyzipper.ZIP_STORED, encryption=pyzipper.WZ_AES)
    zf.setpassword(password.encode())
    zf.setencryption(pyzipper.WZ_AES, nbits=256)
    return zf


def export(dest: Path, password: str) -> dict[str, Any]:
    """Write the instance into a password-protected ZIP at `dest`. Returns the manifest."""
    if not password:
        raise TransferError("A password is required.")
    Path(settings.WORK_DIR).mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = dest.with_name(dest.name + ".partial")
    try:
        with tempfile.TemporaryDirectory(dir=settings.WORK_DIR, prefix="export-") as tmp:
            progress("Saving the database")
            dump = Path(tmp) / DATABASE
            try:
                backup.dump(dump)
            except backup.BackupError as exc:
                raise TransferError(str(exc)) from exc
            manifest = _manifest()
            excluded = _excluded()
            sources = roots()
            files = [
                (f"{name}/{rel}", path)
                for name, root in sources.items()
                for rel, path in _files(root, excluded + [p for p in sources.values() if p != root])
            ]
            manifest["roots"] = sorted(sources)
            manifest["files"] = len(files)

            progress("Packing the database")
            with _open_for_writing(partial, password) as zf:
                zf.writestr(MANIFEST, json.dumps(manifest, indent=2))
                zf.writestr(MASTER_KEY, str(settings.MASTER_KEY))
                zf.write(dump, DATABASE)
                for i, (arcname, path) in enumerate(files, 1):
                    if i == 1 or i % 25 == 0:
                        progress(f"Packing files ({i} of {len(files)})")
                    try:
                        zf.write(path, arcname)
                    except FileNotFoundError:
                        continue  # removed meanwhile (an expired scan session)
        os.replace(partial, dest)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    logger.info("instance exported", extra={"size": dest.stat().st_size, "files": manifest["files"]})
    return manifest


# --- Reading an archive --------------------------------------------------------


def _open_for_reading(path: Path, password: str) -> pyzipper.AESZipFile:
    try:
        zf = pyzipper.AESZipFile(path)
    except (pyzipper.BadZipFile, OSError) as exc:
        raise TransferError("This file is not a DocNest export (not a ZIP file).") from exc
    names = set(zf.namelist())
    if not {MANIFEST, MASTER_KEY, DATABASE} <= names:
        zf.close()
        raise TransferError("This ZIP file is not a DocNest export.")
    if not zf.getinfo(MANIFEST).flag_bits & 0x1:
        zf.close()
        raise TransferError("This export is not password-protected; DocNest only imports its own exports.")
    zf.setpassword(password.encode())
    return zf


def read_manifest(zf: pyzipper.AESZipFile) -> dict[str, Any]:
    try:
        manifest = json.loads(zf.read(MANIFEST))
    except RuntimeError as exc:  # pyzipper: "Bad password for file …"
        raise TransferError("Wrong password.") from exc
    except ValueError as exc:
        raise TransferError("The export is damaged (manifest unreadable).") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise TransferError("This ZIP file is not a DocNest export.")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise TransferError(
            f"This export has format version {manifest.get('format_version')}; "
            f"this DocNest reads version {FORMAT_VERSION}. Update DocNest first."
        )
    return manifest


def _incompatible(manifest: dict[str, Any]) -> list[str]:
    """Migrations the export has applied that this version of DocNest does not know."""
    from django.db.migrations.loader import MigrationLoader

    known = set(MigrationLoader(None, ignore_no_migrations=True).disk_migrations)
    return [
        f"{app}.{name}"
        for app, names in (manifest.get("migrations") or {}).items()
        for name in names
        if (app, name) not in known
    ]


def warnings(manifest: dict[str, Any], *, keep_accounts: bool) -> list[str]:
    """What the user should know before (and after) importing on this server."""
    notes: list[str] = []
    if not keep_accounts and manifest.get("base_url") and manifest["base_url"] != settings.BASE_URL:
        notes.append(
            f"The export comes from {manifest['base_url']}, this server is {settings.BASE_URL}. "
            "Passkeys and security keys only work on the address they were registered for: "
            "sign in with your authenticator app or a recovery code, then register them again."
        )
    if manifest.get("storage_backend") != settings.STORAGE_BACKEND:
        notes.append(
            f"The export stores documents in “{manifest.get('storage_backend')}”, this server in "
            f"“{settings.STORAGE_BACKEND}”: set DOCNEST_STORAGE_BACKEND={manifest.get('storage_backend')} "
            "in .env, or documents cannot be opened."
        )
    elif settings.STORAGE_BACKEND == "proton" and manifest.get("proton_root") != settings.PROTON_ROOT:
        notes.append(
            f"The export keeps its documents in Proton Drive under {manifest.get('proton_root')}, "
            f"this server uses {settings.PROTON_ROOT}: set DOCNEST_PROTON_ROOT={manifest.get('proton_root')} "
            "in .env, or documents cannot be opened."
        )
    current = _config()
    exported = manifest.get("config") or {}
    skip = {"DOCNEST_BASE_URL", "DOCNEST_STORAGE_BACKEND", "DOCNEST_PROTON_ROOT"}
    differing = [
        f"{name}={exported[name]}" + (f" (here: {current[name]})" if name in current else " (not set here)")
        for name in sorted(exported)
        if name not in skip and exported[name] != current.get(name)
    ]
    if differing:
        notes.append(
            "The old server's .env had these settings, which differ here — copy the ones you want "
            "into this server's .env: " + "; ".join(differing)
        )
    return notes


def inspect(path: Path, password: str) -> dict[str, Any]:
    """Check password, format and version; returns the manifest with the warnings for this server."""
    with _open_for_reading(path, password) as zf:
        manifest = read_manifest(zf)
    if unknown := _incompatible(manifest):
        raise TransferError(
            f"This export was made with a newer DocNest ({manifest.get('docnest_version') or 'unknown'}) "
            f"than this server runs ({os.environ.get('DOCNEST_VERSION', 'dev')}). Update this server first "
            f"(unknown database migrations: {', '.join(unknown[:5])}{' …' if len(unknown) > 5 else ''})."
        )
    keep_accounts = not access.instance_empty()
    return {
        **manifest,
        "keeps_accounts": keep_accounts,
        "warnings": warnings(manifest, keep_accounts=keep_accounts),
    }


# --- Import --------------------------------------------------------------------


def restore(path: Path, password: str) -> list[str]:
    """Replace this instance with the export at `path`. Returns notes for the user.

    A server that has users keeps its accounts (apps/transfer/accounts.py); a new one
    takes over the accounts of the export.
    """
    manifest = inspect(path, password)
    notes = list(manifest["warnings"])
    saved = accounts.snapshot() if manifest["keeps_accounts"] else None
    targets = roots()
    Path(settings.WORK_DIR).mkdir(parents=True, exist_ok=True, mode=0o700)
    staged: dict[str, Path] = {}
    try:
        with (
            _open_for_reading(path, password) as zf,
            tempfile.TemporaryDirectory(dir=settings.WORK_DIR, prefix="import-") as tmp,
        ):
            imported_key = zf.read(MASTER_KEY).decode().strip()
            if len(imported_key) < 32:
                raise TransferError("The export is damaged (master key missing).")

            members = [i for i in zf.infolist() if not i.is_dir() and "/" in i.filename]
            skipped = sorted({i.filename.split("/", 1)[0] for i in members} - set(targets))
            if skipped:
                notes.append(f"Not restored (this server has no place for them): {', '.join(skipped)}.")
            for name, root in targets.items():
                staging = root / STAGING
                shutil.rmtree(staging, ignore_errors=True)
                staging.mkdir(parents=True, mode=0o700)
                staged[name] = staging
            for i, info in enumerate(members, 1):
                if i == 1 or i % 25 == 0:
                    progress(f"Unpacking files ({i} of {len(members)})")
                name, rel = info.filename.split("/", 1)
                if name not in staged:
                    continue
                _extract(zf, info, staged[name], rel)

            progress("Unpacking the database")
            dump = Path(tmp) / DATABASE
            _extract(zf, zf.getinfo(DATABASE), Path(tmp), DATABASE)
            check = subprocess.run(
                ["pg_restore", "--list", str(dump)],
                capture_output=True,
                timeout=300,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            if check.returncode != 0:
                raise TransferError("The export is damaged (the database dump cannot be read).")

            progress("Replacing the database")
            _replace_database(dump)
    except BaseException:
        for staging in staged.values():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    # From here on the database is the exported one: finish, even if one step fails.
    progress("Replacing files")
    for name, staging in staged.items():
        _swap(targets[name], staging, others=[p for n, p in targets.items() if n != name])
    instance_key.store(str(settings.LOCAL_MASTER_KEY), imported_key, Path(settings.DATA_DIR))

    progress("Updating the database to this version of DocNest")
    _migrate()

    _reset_runtime_state()
    if saved is not None:
        progress("Restoring the accounts of this server")
        accounts.put_back(saved, imported_key)
        exported_users = (manifest.get("counts") or {}).get("users", 0)
        notes.append(
            "The accounts of this server were kept; "
            + (
                "the account of the export was"
                if exported_users == 1
                else f"the {exported_users} accounts of the export were"
            )
            + " not taken over."
        )
        if saved.had_recovery_codes:
            notes.append(
                "Recovery codes cannot be moved with the data: generate new ones under Settings → Security."
            )
    write_status(accounts_kept=saved is not None)
    logger.info("instance imported", extra={"exported_at": manifest.get("created_at")})
    return notes


def _extract(zf: pyzipper.AESZipFile, info: pyzipper.ZipInfo, base: Path, rel: str) -> None:
    parts = PurePosixPath(rel).parts
    if not parts or any(p in ("", ".", "..") for p in parts) or PurePosixPath(rel).is_absolute():
        raise TransferError(f"The export contains an invalid path: {info.filename}")
    dest = base.joinpath(*parts)
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with zf.open(info) as src, os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
    except RuntimeError as exc:
        raise TransferError("Wrong password.") from exc


def _replace_database(dump: Path) -> None:
    """Drop everything and load the dump, in one transaction: on any error nothing changes."""
    connection.close()
    db = connection.settings_dict
    env = backup.pg_env()
    with tempfile.TemporaryFile() as errors:
        psql = subprocess.Popen(
            [
                "psql",
                "--no-psqlrc",
                "--quiet",
                "--set=ON_ERROR_STOP=1",
                *backup.pg_connection_args(),
                f"--dbname={db['NAME']}",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            env=env,
        )
        assert psql.stdin is not None
        try:
            # One transaction, committed only once pg_restore has written the whole dump: psql
            # would commit whatever it got if pg_restore stopped halfway (--single-transaction).
            # The lock timeout waits for running requests instead of failing on them, but not forever.
            psql.stdin.write(
                b"BEGIN;\nSET lock_timeout = '60s';\nDROP SCHEMA public CASCADE;\nCREATE SCHEMA public;\n"
            )
            psql.stdin.flush()
            restore = subprocess.run(
                ["pg_restore", "--no-owner", "--no-privileges", "--file=-", str(dump)],
                stdout=psql.stdin,
                stderr=errors,
                env=env,
                timeout=settings.BACKUP_TIMEOUT_SECONDS,
                check=False,
            )
            if restore.returncode == 0:
                with contextlib.suppress(BrokenPipeError):  # psql stopped at an error
                    psql.stdin.write(b"COMMIT;\n")
            with contextlib.suppress(BrokenPipeError):
                psql.stdin.close()  # without COMMIT, the transaction is rolled back
            code = psql.wait(timeout=settings.BACKUP_TIMEOUT_SECONDS)
        except BaseException:
            psql.kill()
            psql.wait()
            raise
        if restore.returncode != 0 or code != 0:
            errors.seek(0)
            lines = [line for line in errors.read().decode(errors="replace").splitlines() if line.strip()]
            detail = lines[-1][:300] if lines else f"exit code {restore.returncode or code}"
            raise TransferError(f"The database could not be restored; nothing was changed ({detail}).")


def _swap(root: Path, staging: Path, others: list[Path]) -> None:
    """Replace the contents of `root` with those of `staging` (both on the same volume).

    Kept: what an export leaves out (except the view cache: it belongs to the replaced
    instance) and the other roots, which are swapped on their own.
    """
    keep = [p for p in _excluded() if p != Path(settings.VIEW_CACHE_DIR)] + others
    for child in root.iterdir():
        if child == staging or any(_inside(p, child) for p in keep):
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in staging.iterdir():
        target = root / child.name
        if target.exists():  # an excluded directory of this server, never part of an export
            continue
        os.replace(child, target)
    shutil.rmtree(staging, ignore_errors=True)


def _migrate() -> None:
    """Bring the imported database to this version, in a new process (it reads the imported key)."""
    result = subprocess.run(
        [sys.executable, str(Path(settings.BASE_DIR) / "manage.py"), "migrate", "--noinput"],
        capture_output=True,
        text=True,
        timeout=MIGRATE_TIMEOUT_SECONDS,
        check=False,
        stdin=subprocess.DEVNULL,
        cwd=settings.BASE_DIR,
        # The same database and data volume as this process (tests point both elsewhere).
        env={
            **os.environ,
            "DOCNEST_DB_NAME": str(connection.settings_dict["NAME"]),
            "DOCNEST_DATA_DIR": str(settings.DATA_DIR),
        },
    )
    if result.returncode != 0:
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        raise TransferError(
            "The data was imported, but updating the database failed: "
            f"{lines[-1][:300] if lines else result.returncode}. Run `docnest migrate` and restart DocNest."
        )


def _reset_runtime_state() -> None:
    """Sessions of the old server mean nothing here; status values are refreshed by the worker."""
    from django.contrib.sessions.models import Session

    from apps.accounts.models import UserSession
    from apps.processing import pipeline
    from apps.processing.models import SystemState, WorkerHeartbeat

    Session.objects.all().delete()
    UserSession.objects.all().delete()
    WorkerHeartbeat.objects.all().delete()
    SystemState.objects.filter(key=pipeline.STORAGE_STATE_KEY).delete()


def restart_services() -> bool:
    """Restart web server and worker so they use the imported key (all-in-one container)."""
    import signal

    try:
        pid = int(SUPERVISORD_PID.read_text().strip())
        os.kill(pid, signal.SIGHUP)  # supervisord restarts all programs
    except (OSError, ValueError):
        return False
    return True


def export_name() -> str:
    return f"docnest-export-{timezone.now().strftime('%Y-%m-%d-%H%M%S')}.zip"


def valid_export_name(name: str) -> bool:
    return bool(re.fullmatch(r"docnest-export-\d{4}-\d{2}-\d{2}-\d{6}\.zip", name))
