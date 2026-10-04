from __future__ import annotations

import time
from collections.abc import Callable

from django.conf import settings
from django.contrib.auth import logout
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from apps.accounts.models import UserSession

TOUCH_INTERVAL = 60


class SessionTimeoutMiddleware:
    """Enforces the idle timeout and invalidates sessions that were revoked."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        session = request.session
        if request.user.is_authenticated:
            now = time.time()
            last = session.get("last_activity", now)
            tracked = session.get("tracked")
            if now - last > settings.SESSION_IDLE_TIMEOUT:
                logout(request)
            elif tracked and not UserSession.objects.filter(session_key=session.session_key).exists():
                logout(request)  # revoked from another session
            elif now - last > TOUCH_INTERVAL or "last_activity" not in session:
                session["last_activity"] = now
                if tracked:
                    UserSession.objects.filter(session_key=session.session_key).update(
                        last_seen_at=timezone.now()
                    )
        return self.get_response(request)
