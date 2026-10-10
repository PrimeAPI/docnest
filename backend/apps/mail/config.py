"""Settings of the email import inbox, kept in SystemState; the password is encrypted at rest."""

from __future__ import annotations

import base64
import re
from dataclasses import asdict, dataclass, field
from typing import Literal

from apps.crypto.aead import decrypt_bytes, encrypt_bytes
from apps.processing.models import SystemState

SETTINGS_KEY = "mail_inbox"
STATUS_KEY = "mail_inbox_status"
PASSWORD_AAD = b"mail-inbox-password"
Security = Literal["ssl", "starttls"]
MIN_INTERVAL = 1
MAX_INTERVAL = 1440
MAX_SENDERS = 50
# "name@example.com", or "@example.com" for a whole domain
SENDER = re.compile(r"^([^@\s]+)?@[^@\s]+\.[^@\s]+$")


@dataclass
class MailSettings:
    enabled: bool = False
    host: str = ""
    port: int = 993
    security: Security = "ssl"
    verify_tls: bool = True  # off for a local bridge with a self-signed certificate (Proton Mail Bridge)
    username: str = ""
    folder: str = "INBOX"
    interval_minutes: int = 5
    # Only mail from these addresses is imported. Empty: nothing is imported.
    allowed_senders: list[str] = field(default_factory=list)
    password: str = ""

    def ready(self) -> bool:
        return bool(self.enabled and self.host and self.username and self.password and self.allowed_senders)

    def allows(self, address: str) -> bool:
        address = address.strip().casefold()
        if "@" not in address:
            return False
        domain = "@" + address.rsplit("@", 1)[1]
        return any(entry in (address, domain) for entry in self.allowed_senders)


def clean_sender(value: str) -> str:
    value = value.strip().casefold()
    if not SENDER.match(value):
        raise ValueError(f"Not an email address or @domain: {value[:80]}")
    return value


def load() -> MailSettings:
    value = SystemState.objects.filter(key=SETTINGS_KEY).values_list("value", flat=True).first() or {}
    if not isinstance(value, dict):
        return MailSettings()
    encrypted = value.pop("password_enc", "")
    known = {k: v for k, v in value.items() if k in MailSettings.__dataclass_fields__ and k != "password"}
    settings = MailSettings(**known)
    if encrypted:
        settings.password = decrypt_bytes(base64.b64decode(encrypted), aad=PASSWORD_AAD).decode()
    return settings


def save(settings: MailSettings) -> None:
    value = asdict(settings)
    password = value.pop("password")
    value["password_enc"] = (
        base64.b64encode(encrypt_bytes(password.encode(), aad=PASSWORD_AAD)).decode() if password else ""
    )
    SystemState.objects.update_or_create(key=SETTINGS_KEY, defaults={"value": value})


def status() -> dict[str, object]:
    value = SystemState.objects.filter(key=STATUS_KEY).values_list("value", flat=True).first()
    return value if isinstance(value, dict) else {}


def set_status(**values: object) -> None:
    SystemState.objects.update_or_create(key=STATUS_KEY, defaults={"value": {**status(), **values}})
