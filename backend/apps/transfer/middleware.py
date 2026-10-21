from __future__ import annotations

from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse

from apps.transfer import state

TRANSFER_API = "/api/v1/transfer/"
ALWAYS_ALLOWED = (TRANSFER_API, "/api/v1/health", "/api/v1/csrf")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class TransferPauseMiddleware:
    """While an import replaces the instance, the API answers nothing else; during an export, no changes."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path.startswith(ALWAYS_ALLOWED) and state.paused() == "import":
            # The session table is being replaced: without the cookie the session is not looked
            # up — a lookup would find nothing and Django would delete the browser's cookie.
            request.COOKIES.pop(settings.SESSION_COOKIE_NAME, None)
        elif request.path.startswith("/api/") and not request.path.startswith(ALWAYS_ALLOWED):
            operation = state.paused()
            if operation == "import":
                return JsonResponse(
                    {"detail": "DocNest is being replaced by an imported export. Please wait."},
                    status=503,
                    headers={"Retry-After": "10"},
                )
            if operation == "export" and request.method not in SAFE_METHODS:
                return JsonResponse(
                    {"detail": "An export is running; changes are possible again in a moment."},
                    status=503,
                    headers={"Retry-After": "10"},
                )
        return self.get_response(request)
