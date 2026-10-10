from __future__ import annotations

from typing import Literal

from django.http import HttpRequest
from ninja import Field, Router, Schema
from ninja.errors import HttpError

from apps.audit.service import audit
from apps.core.auth import require_recent_auth
from apps.mail import config, inbox

router = Router(tags=["mail"])


class MailStatusOut(Schema):
    checked_at: str | None = None
    ok: bool | None = None
    message: str = ""
    ignored: int = 0  # messages from senders not on the list, left in the inbox
    imported_total: int = 0
    last_import_at: str | None = None
    last_documents: int = 0


class MailSettingsOut(Schema):
    enabled: bool
    host: str
    port: int
    security: Literal["ssl", "starttls"]
    verify_tls: bool
    username: str
    has_password: bool
    folder: str
    interval_minutes: int
    allowed_senders: list[str]
    status: MailStatusOut


class MailSettingsIn(Schema):
    enabled: bool
    host: str = Field("", max_length=253)
    port: int = Field(993, ge=1, le=65535)
    security: Literal["ssl", "starttls"] = "ssl"
    verify_tls: bool = True
    username: str = Field("", max_length=320)
    password: str | None = Field(None, max_length=500)  # null: keep the saved one
    folder: str = Field("INBOX", max_length=200)
    interval_minutes: int = Field(5, ge=config.MIN_INTERVAL, le=config.MAX_INTERVAL)
    allowed_senders: list[str] = Field(default_factory=list, max_length=config.MAX_SENDERS)


class MailTestOut(Schema):
    ok: bool
    message: str
    waiting: int = 0


def _out(cfg: config.MailSettings) -> MailSettingsOut:
    status = config.status()
    return MailSettingsOut(
        enabled=cfg.enabled,
        host=cfg.host,
        port=cfg.port,
        security=cfg.security,
        verify_tls=cfg.verify_tls,
        username=cfg.username,
        has_password=bool(cfg.password),
        folder=cfg.folder,
        interval_minutes=cfg.interval_minutes,
        allowed_senders=cfg.allowed_senders,
        status=MailStatusOut.model_validate(
            {k: v for k, v in status.items() if k in MailStatusOut.model_fields}
        ),
    )


def _settings_from(data: MailSettingsIn) -> config.MailSettings:
    saved = config.load()
    try:
        senders = sorted({config.clean_sender(s) for s in data.allowed_senders if s.strip()})
    except ValueError as exc:
        raise HttpError(400, str(exc)) from exc
    cfg = config.MailSettings(
        enabled=data.enabled,
        host=data.host.strip(),
        port=data.port,
        security=data.security,
        verify_tls=data.verify_tls,
        username=data.username.strip(),
        folder=data.folder.strip() or "INBOX",
        interval_minutes=data.interval_minutes,
        allowed_senders=senders,
        password=saved.password if data.password is None else data.password,
    )
    return cfg


@router.get("/settings", response=MailSettingsOut)
def get_settings(request: HttpRequest) -> MailSettingsOut:
    return _out(config.load())


@router.put("/settings", response=MailSettingsOut)
def put_settings(request: HttpRequest, data: MailSettingsIn) -> MailSettingsOut:
    """Save the inbox. It brings documents in, so it asks for the password again like other credentials."""
    require_recent_auth(request)
    cfg = _settings_from(data)
    if cfg.enabled:
        if not (cfg.host and cfg.username and cfg.password):
            raise HttpError(400, "Server, user name and password are needed to turn the inbox on")
        if not cfg.allowed_senders:
            raise HttpError(400, "Add at least one allowed sender: mail from anyone else is never imported")
    config.save(cfg)
    audit("mail.settings", request=request, enabled=cfg.enabled, senders=len(cfg.allowed_senders))
    return _out(cfg)


@router.post("/test", response=MailTestOut)
def test_connection(request: HttpRequest, data: MailSettingsIn) -> MailTestOut:
    """Try the given settings (the saved password if none is given) without saving them."""
    require_recent_auth(request)
    cfg = _settings_from(data)
    if not (cfg.host and cfg.username and cfg.password):
        raise HttpError(400, "Enter server, user name and password first")
    try:
        waiting = inbox.test(cfg)
    except inbox.MailError as exc:
        return MailTestOut(ok=False, message=str(exc))
    return MailTestOut(ok=True, message="Connected", waiting=waiting)


@router.post("/check", response=MailSettingsOut)
def check_now(request: HttpRequest) -> MailSettingsOut:
    cfg = config.load()
    if not cfg.ready():
        raise HttpError(400, "Turn the inbox on and save it first")
    inbox.schedule()
    return _out(cfg)
