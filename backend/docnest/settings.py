"""Django settings for DocNest.

All configuration comes from environment variables (see `deploy/.env.example`).
Secrets are read from files via `*_FILE` variables (Docker secrets).
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from urllib.parse import urlparse

from docnest.config import ConfigError, env, env_bool, env_int, is_dev, secret

BASE_DIR = Path(__file__).resolve().parent.parent
TESTING = "pytest" in sys.modules or env_bool("DOCNEST_TESTING", default=False)
DEV = is_dev() or TESTING

# --- Core -------------------------------------------------------------------

DEBUG = DEV and env_bool("DOCNEST_DEBUG", default=False)

SECRET_KEY = secret("DOCNEST_SECRET_KEY", required=not DEV, min_length=50) or (
    "insecure-dev-only-secret-key-do-not-use-in-production-0123456789"
)

BASE_URL = (env("DOCNEST_BASE_URL") or ("http://localhost:8000" if DEV else "")).rstrip("/")
if not BASE_URL:
    raise ConfigError("DOCNEST_BASE_URL is required (e.g. https://docs.example.com)")
_parsed_base = urlparse(BASE_URL)
if _parsed_base.scheme not in {"http", "https"} or not _parsed_base.hostname:
    raise ConfigError("DOCNEST_BASE_URL must be an absolute http(s) URL")
if _parsed_base.scheme != "https" and _parsed_base.hostname not in {"localhost", "127.0.0.1"} and not DEV:
    raise ConfigError("DOCNEST_BASE_URL must use https (http is only allowed for localhost)")

ALLOWED_HOSTS = [_parsed_base.hostname, "localhost", "127.0.0.1"]
if TESTING:
    ALLOWED_HOSTS.append("testserver")
CSRF_TRUSTED_ORIGINS = [BASE_URL]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "ninja",
    "apps.core",
    "apps.accounts",
    "apps.audit",
    "apps.taxonomy",
    "apps.documents",
    "apps.scanners",
    "apps.processing",
    "apps.search",
    "apps.analysis",
    "apps.storage",
]

MIDDLEWARE = [
    "apps.core.middleware.SecurityHeadersMiddleware",
    "apps.core.middleware.UploadSizeLimitMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.accounts.middleware.SessionTimeoutMiddleware",
]

ROOT_URLCONF = "docnest.urls"
WSGI_APPLICATION = "docnest.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]

# --- Database ---------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": env("DOCNEST_DB_HOST", "db"),
        "PORT": env("DOCNEST_DB_PORT", "5432"),
        "NAME": env("DOCNEST_DB_NAME", "docnest"),
        "USER": env("DOCNEST_DB_USER", "docnest"),
        "PASSWORD": secret("DOCNEST_DB_PASSWORD", required=not DEV) or env("DOCNEST_DB_PASSWORD", "docnest"),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {"sslmode": env("DOCNEST_DB_SSLMODE", "prefer")},
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Auth -------------------------------------------------------------------

AUTH_USER_MODEL = "accounts.User"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.Argon2PasswordHasher"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
    {"NAME": "apps.accounts.validators.CharacterClassValidator"},
]

SECURE_COOKIES = _parsed_base.scheme == "https" or not DEV
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = "__Host-docnest_session" if SECURE_COOKIES else "docnest_session"
SESSION_COOKIE_SECURE = SECURE_COOKIES
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
SESSION_COOKIE_AGE = env_int("DOCNEST_SESSION_ABSOLUTE_TIMEOUT_MINUTES", 12 * 60) * 60
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_IDLE_TIMEOUT = env_int("DOCNEST_SESSION_IDLE_TIMEOUT_MINUTES", 30) * 60
SESSION_SAVE_EVERY_REQUEST = False

CSRF_COOKIE_NAME = "__Host-docnest_csrf" if SECURE_COOKIES else "docnest_csrf"
CSRF_COOKIE_SECURE = SECURE_COOKIES
CSRF_COOKIE_HTTPONLY = False  # the SPA reads it to send X-CSRFToken
CSRF_COOKIE_SAMESITE = "Strict"
CSRF_HEADER_NAME = "HTTP_X_CSRFTOKEN"

WEBAUTHN_RP_ID = _parsed_base.hostname
WEBAUTHN_RP_NAME = "DocNest"
WEBAUTHN_ORIGIN = BASE_URL

LOGIN_RATE_LIMIT_PER_ACCOUNT = env_int("DOCNEST_LOGIN_MAX_FAILURES", 5)
LOGIN_RATE_LIMIT_PER_IP = env_int("DOCNEST_LOGIN_MAX_FAILURES_PER_IP", 20)
LOGIN_LOCKOUT_SECONDS = env_int("DOCNEST_LOGIN_LOCKOUT_SECONDS", 15 * 60)
REAUTH_MAX_AGE_SECONDS = 10 * 60

# --- HTTP security ----------------------------------------------------------

if env_bool("DOCNEST_BEHIND_PROXY", default=True):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = False
SECURE_HSTS_SECONDS = 0 if DEV else 63072000
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "no-referrer"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_SSL_REDIRECT = False  # TLS is terminated by the reverse proxy
SILENCED_SYSTEM_CHECKS = [
    "security.W002",  # X-Frame-Options is set by SecurityHeadersMiddleware
    "security.W005",  # HSTS subdomains: the operator's domain policy, not ours to decide
    "security.W008",  # HTTPS redirect happens at the reverse proxy
    "security.W021",  # HSTS preload is an explicit opt-in by the operator
]

MAX_UPLOAD_BYTES = env_int("DOCNEST_MAX_UPLOAD_MB", 100) * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 0  # always stream uploads to a temp file
DATA_UPLOAD_MAX_NUMBER_FIELDS = 200

# --- Paths & storage --------------------------------------------------------

DATA_DIR = Path(env("DOCNEST_DATA_DIR", str(BASE_DIR / ".data")))
INTAKE_DIR = Path(env("DOCNEST_INTAKE_DIR", str(DATA_DIR / "intake")))
WORK_DIR = Path(env("DOCNEST_WORK_DIR", str(DATA_DIR / "work")))
VIEW_CACHE_DIR = Path(env("DOCNEST_VIEW_CACHE_DIR", str(DATA_DIR / "view-cache")))
FILE_UPLOAD_TEMP_DIR = str(WORK_DIR / "uploads")
for _dir in (DATA_DIR, INTAKE_DIR, WORK_DIR, Path(FILE_UPLOAD_TEMP_DIR)):
    with contextlib.suppress(OSError):  # reported by Django's system checks
        _dir.mkdir(parents=True, exist_ok=True, mode=0o700)
FILE_UPLOAD_PERMISSIONS = 0o600

STORAGE_BACKEND = env("DOCNEST_STORAGE_BACKEND", "local" if DEV else "proton")
STORAGE_LOCAL_DIR = Path(env("DOCNEST_STORAGE_LOCAL_DIR", str(DATA_DIR / "storage")))
PROTON_ROOT = env("DOCNEST_PROTON_ROOT", "/my-files/DocNest")
PROTON_CLI = env("DOCNEST_PROTON_CLI", "docnest-proton")
PROTON_TIMEOUT_SECONDS = env_int("DOCNEST_PROTON_TIMEOUT_SECONDS", 600)
VIEW_CACHE_MB = env_int("DOCNEST_VIEW_CACHE_MB", 0)  # 0 = disabled

# --- Crypto -----------------------------------------------------------------

MASTER_KEY = secret("DOCNEST_MASTER_KEY", required=not DEV, min_length=32) or (
    "insecure-dev-only-master-key-0123456789abcdef" if DEV else None
)

# --- Processing -------------------------------------------------------------

OCR_BACKEND = env("DOCNEST_OCR_BACKEND", "ocrmypdf").lower()
if OCR_BACKEND not in {"ocrmypdf", "docling"}:
    raise ConfigError("DOCNEST_OCR_BACKEND must be 'ocrmypdf' or 'docling'")
OCR_LANGUAGES = env("DOCNEST_OCR_LANGUAGES", "deu+eng")
OCR_TIMEOUT_SECONDS = env_int("DOCNEST_OCR_TIMEOUT_SECONDS", 900)
OCR_JOBS = env_int("DOCNEST_OCR_JOBS", 2)
DOCLING_THREADS = env_int("DOCNEST_DOCLING_THREADS", 2)
DOCLING_DEVICE = env("DOCNEST_DOCLING_DEVICE", "cpu").lower()
DOCLING_ARTIFACTS_PATH = Path(env("DOCNEST_DOCLING_ARTIFACTS_PATH", "/var/lib/docnest/models"))
DOCLING_FIELD_DETECTION = env("DOCNEST_DOCLING_FIELD_DETECTION", "layout").lower()
if DOCLING_FIELD_DETECTION not in {"layout", "vlm", "hybrid"}:
    raise ConfigError("DOCNEST_DOCLING_FIELD_DETECTION must be 'layout', 'vlm', or 'hybrid'")
MAX_PAGES = env_int("DOCNEST_MAX_PAGES", 500)
JOB_LEASE_SECONDS = env_int("DOCNEST_JOB_LEASE_SECONDS", 1800)
JOB_MAX_ATTEMPTS = env_int("DOCNEST_JOB_MAX_ATTEMPTS", 5)
WORKER_POLL_SECONDS = env_int("DOCNEST_WORKER_POLL_SECONDS", 30)
PROCESSING_CONCURRENCY = env_int("DOCNEST_PROCESSING_CONCURRENCY", 1)
JOB_HISTORY_HOURS = env_int("DOCNEST_JOB_HISTORY_HOURS", 24)
AUDIT_RETENTION_DAYS = env_int("DOCNEST_AUDIT_RETENTION_DAYS", 365)

# --- Static / SPA -----------------------------------------------------------

STATIC_URL = "/static/"
FRONTEND_DIR = Path(env("DOCNEST_FRONTEND_DIR", str(BASE_DIR.parent / "frontend" / "dist")))
WHITENOISE_ROOT = FRONTEND_DIR if FRONTEND_DIR.exists() else None
WHITENOISE_INDEX_FILE = False
WHITENOISE_MAX_AGE = 3600

# --- I18N -------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("DOCNEST_TIME_ZONE", "Europe/Berlin")
USE_I18N = False
USE_TZ = True

# --- Logging ----------------------------------------------------------------

LOG_LEVEL = env("DOCNEST_LOG_LEVEL", "INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"redact": {"()": "apps.core.logging.RedactingFilter"}},
    "formatters": {"json": {"()": "apps.core.logging.JsonFormatter"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "json",
            "filters": ["redact"],
        }
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.security.DisallowedHost": {"level": "ERROR"},
        "ocrmypdf": {"level": "WARNING"},
        "pikepdf": {"level": "WARNING"},
        "PIL": {"level": "WARNING"},
    },
}
