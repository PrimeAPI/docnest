"""Deployment safety checks: the app refuses insecure production configuration."""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=False)
def production_safety(app_configs, **kwargs):  # type: ignore[no-untyped-def]
    errors: list[Error] = []
    if settings.DEV:
        return errors
    if settings.DEBUG:
        errors.append(Error("DEBUG must be off in production", id="docnest.E001"))
    if not settings.BASE_URL.startswith("https://") and "localhost" not in settings.BASE_URL:
        errors.append(Error("DOCNEST_BASE_URL must use https", id="docnest.E002"))
    if not settings.SESSION_COOKIE_SECURE:
        errors.append(Error("Session cookies must be Secure", id="docnest.E003"))
    if settings.STORAGE_BACKEND not in {"proton", "local"}:
        errors.append(Error("DOCNEST_STORAGE_BACKEND must be 'proton' or 'local'", id="docnest.E004"))
    return errors
