"""Keep this server's accounts through an import.

Documents, folders and settings belong to the instance, not to a user: the
only data tied to users are the accounts themselves (password, second
factors, sessions). When an export is imported into a server that already has
users, those accounts stay: the user signs in exactly as before the import
and finds the imported archive.

The import switches the master key to the exported one, so TOTP secrets are
encrypted again with it. Recovery codes are stored as keyed hashes only and
cannot be carried over: they are dropped and must be generated again.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.core.management.color import no_style
from django.db import connection, transaction

from apps.crypto import keys
from apps.crypto.aead import decrypt_bytes, encrypt_bytes
from apps.crypto.keys import Purpose


@dataclass
class Snapshot:
    users: list[dict[str, Any]] = field(default_factory=list)
    webauthn: list[dict[str, Any]] = field(default_factory=list)
    totp: list[tuple[dict[str, Any], bytes]] = field(default_factory=list)  # row, plaintext secret
    sessions: list[dict[str, Any]] = field(default_factory=list)
    user_sessions: list[dict[str, Any]] = field(default_factory=list)
    had_recovery_codes: bool = False


def _totp_aad(user_id: int) -> bytes:
    return f"totp:{user_id}".encode()  # as in apps.accounts.services


def snapshot() -> Snapshot:
    """The accounts of this server, with TOTP secrets decrypted (stays in memory)."""
    from django.contrib.sessions.models import Session

    from apps.accounts.models import RecoveryCode, TotpDevice, User, UserSession, WebAuthnCredential

    totp = []
    for row in TotpDevice.objects.values():
        secret = decrypt_bytes(bytes(row["secret_enc"]), purpose=Purpose.TOTP, aad=_totp_aad(row["user_id"]))
        totp.append(({k: v for k, v in row.items() if k != "secret_enc"}, secret))
    return Snapshot(
        users=[dict(row) for row in User.objects.values()],
        webauthn=[
            {k: bytes(v) if isinstance(v, memoryview) else v for k, v in row.items()}
            for row in WebAuthnCredential.objects.values()
        ],
        totp=totp,
        sessions=[dict(row) for row in Session.objects.values()],
        user_sessions=[dict(row) for row in UserSession.objects.values()],
        had_recovery_codes=RecoveryCode.objects.filter(used_at__isnull=True).exists(),
    )


@contextlib.contextmanager
def master_key(key: str) -> Iterator[None]:
    """Encrypt with `key` in this process (the import switched the instance to it)."""
    previous = settings.MASTER_KEY
    settings.MASTER_KEY = key
    keys.derive.cache_clear()
    try:
        yield
    finally:
        settings.MASTER_KEY = previous
        keys.derive.cache_clear()


def put_back(saved: Snapshot, key: str) -> None:
    """Replace the imported accounts with the saved ones of this server."""
    from django.contrib.sessions.models import Session

    from apps.accounts.models import (
        LoginThrottle,
        RecoveryCode,
        TotpDevice,
        User,
        UserSession,
        WebAuthnCredential,
    )

    with transaction.atomic(), master_key(key):
        for model in (RecoveryCode, TotpDevice, WebAuthnCredential, UserSession, LoginThrottle):
            model.objects.all().delete()
        User.objects.all().delete()
        Session.objects.all().delete()

        User.objects.bulk_create(User(**row) for row in saved.users)
        WebAuthnCredential.objects.bulk_create(WebAuthnCredential(**row) for row in saved.webauthn)
        TotpDevice.objects.bulk_create(
            TotpDevice(
                **row,
                secret_enc=encrypt_bytes(secret, purpose=Purpose.TOTP, aad=_totp_aad(row["user_id"])),
            )
            for row, secret in saved.totp
        )
        Session.objects.bulk_create(Session(**row) for row in saved.sessions)
        UserSession.objects.bulk_create(UserSession(**row) for row in saved.user_sessions)
        # Rows were inserted with their ids: let new rows continue after them.
        models = [User, WebAuthnCredential, TotpDevice, UserSession, RecoveryCode, LoginThrottle]
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), models):
                cursor.execute(sql)
