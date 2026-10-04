from __future__ import annotations

from django.core.exceptions import ValidationError


class CharacterClassValidator:
    """Require at least three of: lowercase, uppercase, digits, symbols — or a long passphrase (20+)."""

    def validate(self, password: str, user: object = None) -> None:
        if len(password) >= 20:
            return
        classes = sum(
            [
                any(c.islower() for c in password),
                any(c.isupper() for c in password),
                any(c.isdigit() for c in password),
                any(not c.isalnum() for c in password),
            ]
        )
        if classes < 3:
            raise ValidationError(
                "Use at least three of: lowercase, uppercase, digits, symbols — "
                "or a passphrase of 20+ characters.",
                code="password_too_simple",
            )

    def get_help_text(self) -> str:
        return "At least three character classes, or a passphrase of 20+ characters."
