"""Who may import.

On an instance with users, only a signed-in user who confirmed the password
recently. A new instance has no users yet, so whoever reaches it first could
otherwise import their own data and use the server: there an import also
needs the setup code, which only the server's operator can read
(`docker compose exec app docnest import-code`).
"""

from __future__ import annotations

import base64
import hmac

from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError

from apps.crypto.hashing import keyed_hash


def instance_empty() -> bool:
    from apps.accounts.models import User

    return not User.objects.exists()


def setup_code() -> str:
    digest = bytes.fromhex(keyed_hash(b"docnest/transfer-setup-code/v1"))
    code = base64.b32encode(digest[:10]).decode()
    return "-".join(code[i : i + 4] for i in range(0, len(code), 4))


def valid_setup_code(code: str | None) -> bool:
    normalized = (code or "").replace("-", "").replace(" ", "").upper()
    return bool(normalized) and hmac.compare_digest(normalized, setup_code().replace("-", ""))


def password_problems(password: str) -> list[str]:
    """An export holds the master key and the Proton sign-in: it needs a strong password."""
    try:
        password_validation.validate_password(password)
    except ValidationError as exc:
        return list(exc.messages)
    return []
