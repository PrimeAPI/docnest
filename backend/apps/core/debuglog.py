"""Debug log files for troubleshooting (DOCNEST_DEBUG_LOG=true).

Every process — web server, worker and the subprocess of each document job —
appends to one file per day in DOCNEST_DEBUG_LOG_DIR (by default on the data
volume), so a whole incident can be read in order. Each process writes its
configuration when it starts. The same rules as for the console log apply: no
document text, and the redaction filter masks anything that looks like a secret.
"""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import re
import socket
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from django.http import HttpRequest, HttpResponse

from apps.core.logging import redact

logger = logging.getLogger("docnest.debug")

FILE_PREFIX = "docnest-debug-"
_role = ""


def set_role(role: str) -> None:
    """Name this process in every line it writes (web, worker, job-12, …)."""
    global _role
    _role = role


def role() -> str:
    if _role:
        return _role
    if len(sys.argv) > 1 and sys.argv[0].endswith("manage.py"):
        return sys.argv[1]
    return Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "python"


class DailyFileHandler(logging.Handler):
    """Appends to <directory>/docnest-debug-YYYY-MM-DD.log; keeps `keep_days` days of files.

    One write() per record on an O_APPEND file, so several processes can share a file
    without interleaving lines. The file is opened on the first record: a missing or
    read-only directory must never stop DocNest from starting.
    """

    def __init__(self, directory: str, keep_days: int = 7) -> None:
        super().__init__()
        self.directory = Path(directory)
        self.keep_days = max(1, keep_days)
        self._day = ""
        self._fd: int | None = None

    def _open(self, day: str) -> int:
        self._close_fd()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / f"{FILE_PREFIX}{day}.log"
        self._fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self._day = day
        self._prune()
        return self._fd

    def _prune(self) -> None:
        cutoff = (datetime.now() - timedelta(days=self.keep_days)).strftime("%Y-%m-%d")
        for old in self.directory.glob(f"{FILE_PREFIX}*.log"):
            if old.stem.removeprefix(FILE_PREFIX) < cutoff:
                old.unlink(missing_ok=True)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record) + "\n"
            day = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d")
            fd = self._fd if self._fd is not None and day == self._day else self._open(day)
            os.write(fd, line.encode("utf-8", "replace"))
        except Exception:
            self.handleError(record)

    def _close_fd(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None

    def close(self) -> None:
        self.acquire()
        try:
            self._close_fd()
        finally:
            self.release()
        super().close()


class TextFormatter(logging.Formatter):
    """One readable line per record: time, level, process, logger, message and its fields."""

    _RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).astimezone()
        when = stamp.strftime("%Y-%m-%d %H:%M:%S.") + f"{int(record.msecs):03d}" + stamp.strftime("%z")
        # Values are redacted one by one: a field named e.g. "exitcode" is no credential.
        fields = " ".join(
            f"{key}={_field(redact(str(value)))}"
            for key, value in record.__dict__.items()
            if key not in self._RESERVED and not key.startswith("_")
        )
        line = (
            f"{when} {record.levelname:<7} [{role()} pid={record.process} {record.threadName}] "
            f"{record.name}: {redact(record.getMessage())}"
        )
        if fields:
            line += " | " + fields
        if record.exc_info:
            line += "\n" + redact(self.formatException(record.exc_info))
        if record.stack_info:
            line += "\n" + redact(self.formatStack(record.stack_info))
        return line


def _field(value: object) -> str:
    text = str(value)
    return repr(text) if not text or any(c.isspace() for c in text) else text


# --- What a process runs with ----------------------------------------------------

_SECRET_NAME = re.compile(r"SECRET|PASSWORD|PASSWD|TOKEN|API_KEY|MASTER_KEY|PRIVATE|_KEY$", re.I)
_SHOWN_ENV = ("DOCNEST_", "OLLAMA_", "HF_", "PROTON_", "OMP_", "TZ", "PYTHON", "DJANGO_")


def _is_secret(name: str) -> bool:
    # *_FILE names a secret's path (shown); *_HASHERS / *_VALIDATORS are password policy.
    if name.upper().endswith(("_FILE", "_HASHERS", "_VALIDATORS")):
        return False
    return bool(_SECRET_NAME.search(name))


def _masked(value: object) -> str:
    # Phrased without "=": the redaction filter would swallow "NAME = <anything>".
    return f"set ({len(str(value))} chars)" if value else "empty"


def safe_value(name: str, value: Any) -> Any:
    """`value` with secrets replaced by a note whether they are set."""
    if _is_secret(name) and not isinstance(value, (dict, list, tuple)):
        return _masked(value)
    if isinstance(value, dict):
        return {k: safe_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_value(name if not isinstance(v, (dict, list, tuple)) else "", v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def log_startup(process_role: str) -> None:
    """Write the configuration this process runs with; once per web or worker process."""
    from django.conf import settings

    if not getattr(settings, "DEBUG_LOG", False):
        return
    set_role(process_role)
    logger.info(
        "===== DocNest %s started =====",
        process_role,
        extra={
            "version": os.environ.get("DOCNEST_VERSION", "dev"),
            "commit": os.environ.get("DOCNEST_COMMIT", ""),
            "build_date": os.environ.get("DOCNEST_BUILD_DATE", ""),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "host": socket.gethostname(),
            "uid": os.getuid(),
            "cwd": os.getcwd(),
            "argv": " ".join(sys.argv),
            "cpus": os.cpu_count(),
        },
    )
    for name in sorted(n for n in dir(settings) if n.isupper()):
        value = getattr(settings, name)
        if _is_secret(name) and not isinstance(value, (dict, list, tuple)):
            logger.info("setting %s is %s, hidden", name, _masked(value))
        else:
            logger.info("setting %s = %r", name, safe_value(name, value))
    for name in sorted(os.environ):
        if name.startswith(_SHOWN_ENV):
            if _is_secret(name):
                logger.info("env %s is %s, hidden", name, _masked(os.environ[name]))
            else:
                logger.info("env %s = %s", name, os.environ[name])
    _log_free_space(settings)


def _log_free_space(settings: Any) -> None:
    import shutil

    for label in ("DATA_DIR", "WORK_DIR", "DOCLING_ARTIFACTS_PATH", "DEBUG_LOG_DIR"):
        path = Path(getattr(settings, label))
        try:
            usage = shutil.disk_usage(path)
            logger.info(
                "disk %s %s: %.1f GB free of %.1f GB",
                label,
                path,
                usage.free / 1e9,
                usage.total / 1e9,
                extra={"writable": os.access(path, os.W_OK)},
            )
        except OSError as exc:
            logger.info("disk %s %s: not available (%s)", label, path, exc.strerror)


# --- Web requests ----------------------------------------------------------------

_QUIET_PATHS = ("/api/v1/health",)  # the container healthcheck, every 30 s


class RequestLogMiddleware:
    """Method, path, status and duration of each request (no query strings: they hold search terms)."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.log = logging.getLogger("docnest.request")

    def __call__(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        try:
            response = self.get_response(request)
        except Exception:
            self.log.exception("%s %s raised", request.method, request.path)
            raise
        if not request.path.startswith(_QUIET_PATHS):
            user = getattr(request, "user", None)
            self.log.debug(
                "%s %s -> %s",
                request.method,
                request.path,
                response.status_code,
                extra={
                    "ms": int((time.monotonic() - started) * 1000),
                    "user": getattr(user, "pk", None) if user is not None else None,
                },
            )
        return response
