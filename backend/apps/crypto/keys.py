"""Key management.

A single master key (Docker secret) is the root of all application-level
cryptography. Purpose-specific sub-keys are derived with HKDF-SHA256 so a key
is never used for two purposes. `KEY_VERSION` allows future rotation: every
ciphertext records the version it was written with.
"""

from __future__ import annotations

from functools import cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings

KEY_VERSION = 1


class Purpose:
    CONTENT = "content"  # OCR text, titles, extracted fields, thumbnails
    FILES = "files"  # intake files, view cache
    INDEX = "index"  # blind index HMAC
    TOKENS = "tokens"  # misc keyed hashing (e.g. idempotency keys)
    TOTP = "totp"  # TOTP secrets at rest


def _master_key() -> bytes:
    key = settings.MASTER_KEY
    if not key or len(key) < 32:
        raise RuntimeError("DOCNEST_MASTER_KEY is missing or too short")
    return key.encode()


@cache
def derive(purpose: str, version: int = KEY_VERSION) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=f"docnest/{purpose}/v{version}".encode(),
    ).derive(_master_key())
