"""Structured JSON logging with redaction of secrets.

Document contents must never be logged. This module adds a last line of
defence by redacting values that look like credentials.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

_PATTERNS = [
    (re.compile(r"(?i)(authorization:\s*)(bearer|token|basic)\s+[^\s,;]+"), r"\1\2 [REDACTED]"),
    (re.compile(r"dn_scan_[A-Za-z0-9]+_[A-Za-z0-9_-]+"), "dn_scan_[REDACTED]"),
    (
        re.compile(
            r"(?i)((?:password|passwd|secret|token|session(?:id)?|csrftoken|cookie|api[_-]?key|otp|code)"
            r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',;&]+)"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "[REDACTED KEY]",
    ),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        return True


class JsonFormatter(logging.Formatter):
    _RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)
