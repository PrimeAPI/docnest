"""Environment-based configuration with support for `*_FILE` secrets.

Every setting can be provided as `NAME` or, preferably for secrets, as
`NAME_FILE` pointing to a file (e.g. a Docker secret). Plain-env secrets are
only accepted when DOCNEST_DEV=true.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import overload


class ConfigError(RuntimeError):
    pass


def is_dev() -> bool:
    return env_bool("DOCNEST_DEV", default=False)


@overload
def env(name: str, default: str) -> str: ...
@overload
def env(name: str, default: None = None) -> str | None: ...


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def secret(name: str, *, required: bool = True, min_length: int = 0) -> str | None:
    """Read a secret from `NAME_FILE` (preferred) or `NAME` (development only)."""
    file_var = f"{name}_FILE"
    path = os.environ.get(file_var)
    value: str | None = None
    if path:
        p = Path(path)
        try:
            value = p.read_text().strip()
        except OSError as exc:
            raise ConfigError(f"Cannot read {file_var} ({path}): {exc.strerror}") from exc
        mode = p.stat().st_mode & 0o777
        if (
            mode & 0o007 and not is_dev()
        ):  # group access is fine (db_password is shared with the DB container)
            raise ConfigError(
                f"{file_var} ({path}) is world-accessible; restrict it to the app user (chmod 400)"
            )
    elif os.environ.get(name):
        if not is_dev():
            raise ConfigError(
                f"{name} must be provided via {file_var} (a file / Docker secret), not as plain env"
            )
        value = os.environ[name]

    if not value:
        if required:
            raise ConfigError(f"Missing required secret {name} (set {file_var})")
        return None
    if len(value) < min_length:
        raise ConfigError(f"Secret {name} is too short (min {min_length} characters)")
    return value
