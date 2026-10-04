from __future__ import annotations

import hashlib
import hmac

from apps.crypto.keys import Purpose, derive


def keyed_hash(value: bytes, *, purpose: str = Purpose.TOKENS) -> str:
    return hmac.new(derive(purpose), value, hashlib.sha256).hexdigest()


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
