"""Web API for exporting and importing the whole instance (Settings → System, and the login page)."""

from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, HttpRequest
from ninja import Field, Router, Schema
from ninja.errors import HttpError

from apps.accounts.services import is_mfa_authenticated
from apps.audit.service import audit
from apps.core.auth import mfa_auth, require_csrf, require_recent_auth
from apps.transfer import access, archive, state

router = Router(tags=["transfer"])

IMPORT_FILE = "import.zip"
SETUP_CODE_HEADER = "HTTP_X_DOCNEST_SETUP_CODE"
UPLOAD_CHUNK = 1024 * 1024


class ExportFileOut(Schema):
    name: str
    size: int


class TransferStatusOut(Schema):
    instance_empty: bool  # a new instance without users: importing needs the setup code
    running: bool
    operation: str  # "", "export" or "import"
    state: str  # "", "running", "done" or "failed"
    step: str
    message: str
    warnings: list[str]
    started_at: datetime | None
    finished_at: datetime | None
    restarting: bool  # an import finished and DocNest restarts
    accounts_kept: bool  # the import kept this server's accounts (signed-in users stay signed in)
    export: ExportFileOut | None  # a finished export waiting to be downloaded
    import_uploaded: bool  # an uploaded export waits to be imported


class ExportIn(Schema):
    password: str = Field(..., max_length=1024, json_schema_extra={"writeOnly": True})


class ImportIn(Schema):
    password: str = Field(..., max_length=1024, json_schema_extra={"writeOnly": True})
    setup_code: str | None = Field(None, max_length=64)


class UploadOut(Schema):
    size: int


class ImportSummaryOut(Schema):
    created_at: datetime | None
    docnest_version: str
    base_url: str
    documents: int
    users: int
    size: int
    keeps_accounts: bool  # this server has users: they stay, the export's accounts are not taken over
    warnings: list[str]


def _import_path() -> Path:
    return state.directory() / IMPORT_FILE


def _authorize_import(request: HttpRequest, setup_code: str | None, *, recent: bool = True) -> None:
    require_csrf(request)
    if is_mfa_authenticated(request):
        if recent:
            require_recent_auth(request)
        return
    if access.instance_empty():
        if access.valid_setup_code(setup_code):
            return
        raise HttpError(403, "Wrong setup code. Show it with: docker compose exec app docnest import-code")
    raise HttpError(401, "Sign in to import an export")


def _spawn(args: list[str], password: str) -> None:
    """Run a management command in its own process; it outlives this request (and a restart)."""
    proc = subprocess.Popen(
        [sys.executable, str(Path(settings.BASE_DIR) / "manage.py"), *args, "--password-stdin"],
        stdin=subprocess.PIPE,
        cwd=settings.BASE_DIR,
        start_new_session=True,
    )
    assert proc.stdin is not None
    proc.stdin.write(password.encode() + b"\n")
    proc.stdin.close()
    threading.Thread(target=proc.wait, daemon=True).start()  # reap it


def _export_file(status: dict) -> ExportFileOut | None:
    export = status.get("export")
    if not isinstance(export, dict) or not archive.valid_export_name(str(export.get("name", ""))):
        return None
    path = state.directory() / export["name"]
    if not path.is_file():
        return None
    return ExportFileOut(name=export["name"], size=path.stat().st_size)


@router.get("/status", response=TransferStatusOut, auth=None)
def transfer_status(request: HttpRequest) -> TransferStatusOut:
    """Readable without a session: an import signs everyone out, and a new instance has no users."""
    status = state.read_status()
    importing = state.paused() == "import"
    # While an import replaces the database, queries would wait for it: answer from the status file.
    empty = False if importing else access.instance_empty()
    authorized = not importing and (empty or is_mfa_authenticated(request))
    operation = str(status.get("operation", ""))
    # Details of an export are for its owner; those of an import are needed after it signed everyone out.
    detailed = authorized or operation == "import"
    return TransferStatusOut(
        instance_empty=empty,
        running=state.running(),
        operation=operation,
        state=str(status.get("state", "")),
        step=str(status.get("step", "")),
        message=str(status.get("message", "")) if detailed else "",
        warnings=[str(w) for w in status.get("warnings") or []] if detailed else [],
        started_at=status.get("started_at"),
        finished_at=status.get("finished_at"),
        restarting=bool(status.get("restarting")),
        accounts_kept=bool(status.get("accounts_kept")),
        export=_export_file(status) if authorized else None,
        import_uploaded=authorized and _import_path().is_file(),
    )


@router.post("/export", response=TransferStatusOut, auth=mfa_auth)
def start_export(request: HttpRequest, data: ExportIn) -> TransferStatusOut:
    require_recent_auth(request)
    if problems := access.password_problems(data.password):
        raise HttpError(422, " ".join(problems))
    if state.running():
        raise HttpError(409, "An export or import is already running")
    _remove_export()
    state.cleanup_exports()
    state.start_status("export")
    dest = state.directory() / archive.export_name()
    _spawn(["export_instance", "--output", str(dest)], data.password)
    audit("system.export_started", request=request)
    return transfer_status(request)


@router.get("/export/download", auth=mfa_auth)
def download_export(request: HttpRequest) -> FileResponse:
    export = _export_file(state.read_status())
    if export is None:
        raise HttpError(404, "No export is ready")
    audit("system.export_downloaded", request=request, size=export.size)
    return FileResponse(
        (state.directory() / export.name).open("rb"),
        as_attachment=True,
        filename=export.name,
        content_type="application/zip",
    )


def _remove_export() -> None:
    if export := _export_file(state.read_status()):
        (state.directory() / export.name).unlink(missing_ok=True)


@router.delete("/export", response=TransferStatusOut, auth=mfa_auth)
def delete_export(request: HttpRequest) -> TransferStatusOut:
    if state.running():
        raise HttpError(409, "An export or import is running")
    _remove_export()
    return transfer_status(request)


@router.post("/import/upload", response=UploadOut, auth=None)
def upload_import(request: HttpRequest) -> UploadOut:
    """The export as the raw request body (application/octet-stream), stored on the data volume."""
    _authorize_import(request, request.META.get(SETUP_CODE_HEADER), recent=False)
    if state.running():
        raise HttpError(409, "An export or import is running")
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        raise HttpError(411, "Send the export as the request body")
    if length > settings.MAX_IMPORT_BYTES:
        raise HttpError(413, "The export is larger than DOCNEST_MAX_IMPORT_MB allows")
    dest = _import_path()
    partial = dest.with_name(dest.name + ".partial")
    received = 0
    try:
        with partial.open("wb") as out:
            while chunk := request.read(min(UPLOAD_CHUNK, length - received)):
                received += len(chunk)
                out.write(chunk)
                if received >= length:
                    break
        if received != length:
            raise HttpError(400, "The upload was interrupted")
        partial.replace(dest)
    finally:
        partial.unlink(missing_ok=True)
    return UploadOut(size=received)


@router.post("/import/inspect", response=ImportSummaryOut, auth=None)
def inspect_import(request: HttpRequest, data: ImportIn) -> ImportSummaryOut:
    _authorize_import(request, data.setup_code)
    path = _import_path()
    if not path.is_file():
        raise HttpError(404, "Upload the export first")
    try:
        manifest = archive.inspect(path, data.password)
    except state.TransferError as exc:
        raise HttpError(422, str(exc)) from exc
    counts = manifest.get("counts") or {}
    return ImportSummaryOut(
        created_at=manifest.get("created_at"),
        docnest_version=str(manifest.get("docnest_version", "")),
        base_url=str(manifest.get("base_url", "")),
        documents=int(counts.get("documents") or 0),
        users=int(counts.get("users") or 0),
        size=path.stat().st_size,
        keeps_accounts=manifest["keeps_accounts"],
        warnings=manifest["warnings"],
    )


@router.post("/import/start", response=TransferStatusOut, auth=None)
def start_import(request: HttpRequest, data: ImportIn) -> TransferStatusOut:
    _authorize_import(request, data.setup_code)
    path = _import_path()
    if not path.is_file():
        raise HttpError(404, "Upload the export first")
    if state.running():
        raise HttpError(409, "An export or import is already running")
    try:
        archive.inspect(path, data.password)
    except state.TransferError as exc:
        raise HttpError(422, str(exc)) from exc
    audit("system.import_started", request=request)
    state.start_status("import")
    _spawn(["import_instance", str(path), "--yes"], data.password)
    return transfer_status(request)


@router.delete("/import", response=TransferStatusOut, auth=None)
def discard_import(request: HttpRequest, setup_code: str | None = None) -> TransferStatusOut:
    _authorize_import(request, setup_code or request.META.get(SETUP_CODE_HEADER), recent=False)
    if state.running():
        raise HttpError(409, "An export or import is running")
    _import_path().unlink(missing_ok=True)
    return transfer_status(request)
