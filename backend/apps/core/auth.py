"""Authentication classes for the two API surfaces.

- Web API: Django session cookie + CSRF + completed MFA.
- Upload API: scanner bearer tokens only. Session cookies are never accepted there,
  and scanner tokens are never accepted on the web API.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import HttpRequest
from ninja.errors import HttpError
from ninja.security import APIKeyCookie
from ninja.utils import check_csrf

from apps.accounts.services import is_mfa_authenticated, recently_authenticated


class MfaSessionAuth(APIKeyCookie):
    """Fully authenticated user: password + second factor."""

    param_name = settings.SESSION_COOKIE_NAME

    def authenticate(self, request: HttpRequest, key: str | None) -> Any:
        if is_mfa_authenticated(request):
            return request.user
        return None


class EnrollmentSessionAuth(APIKeyCookie):
    """Password-authenticated user who may still need to enroll a second factor."""

    param_name = settings.SESSION_COOKIE_NAME

    def authenticate(self, request: HttpRequest, key: str | None) -> Any:
        if request.user.is_authenticated:
            return request.user
        return None


def require_csrf(request: HttpRequest) -> None:
    """CSRF check for endpoints that run before a session exists (login)."""
    if check_csrf(request):
        raise HttpError(403, "CSRF check failed")


def require_recent_auth(request: HttpRequest) -> None:
    if not recently_authenticated(request):
        raise HttpError(403, "reauthentication_required")


mfa_auth = MfaSessionAuth()
enrollment_auth = EnrollmentSessionAuth()
