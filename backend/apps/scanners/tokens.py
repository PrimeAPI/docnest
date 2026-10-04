"""Scanner API tokens: `dn_scan_<prefix>_<secret>`.

The prefix is a public lookup id; the full token is stored only as SHA-256
(tokens carry 256 bits of entropy, so a slow hash is unnecessary).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

TOKEN_PREFIX = "dn_scan_"  # noqa: S105 - public prefix, not a secret


def generate() -> tuple[str, str, str]:
    """Return (token, prefix, token_hash)."""
    prefix = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    token = f"{TOKEN_PREFIX}{prefix}_{secret}"
    return token, prefix, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def parse_prefix(token: str) -> str | None:
    if not token.startswith(TOKEN_PREFIX):
        return None
    rest = token[len(TOKEN_PREFIX) :]
    prefix, sep, secret = rest.partition("_")
    if not sep or len(prefix) != 12 or not secret:
        return None
    return prefix


def matches(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), token_hash)
