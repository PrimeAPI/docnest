from __future__ import annotations

import logging
from typing import Any

from django.http import HttpRequest

from apps.audit.models import AuditLog

logger = logging.getLogger("docnest.audit")


def client_ip(request: HttpRequest | None) -> str | None:
    if request is None:
        return None
    # The reverse proxy sets X-Real-IP / X-Forwarded-For; only the right-most
    # (proxy-appended) entry is trustworthy.
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[-1].strip() or None
    return request.META.get("HTTP_X_REAL_IP") or request.META.get("REMOTE_ADDR")


def _session_ref(request: HttpRequest | None) -> str:
    if request is None or not hasattr(request, "session") or not request.session.session_key:
        return ""
    from apps.accounts.models import UserSession

    pk = (
        UserSession.objects.filter(session_key=request.session.session_key)
        .values_list("pk", flat=True)
        .first()
    )
    return f"s{pk}" if pk else ""


def audit(
    action: str,
    *,
    request: HttpRequest | None = None,
    user: Any = None,
    scanner: Any = None,
    target: str = "",
    subject: str = "",
    **details: Any,
) -> None:
    if scanner is not None:
        actor_type, actor_id, label = AuditLog.Actor.SCANNER, str(scanner.pk), scanner.name
    elif user is not None and getattr(user, "is_authenticated", False):
        actor_type, actor_id, label = AuditLog.Actor.USER, str(user.pk), user.get_username()
    elif request is not None and getattr(request, "user", None) and request.user.is_authenticated:
        actor_type, actor_id, label = AuditLog.Actor.USER, str(request.user.pk), request.user.get_username()
    elif request is None:
        actor_type, actor_id, label = AuditLog.Actor.SYSTEM, "", ""
    else:
        actor_type, actor_id, label = AuditLog.Actor.ANONYMOUS, "", ""
    if not subject and actor_type == AuditLog.Actor.USER:
        subject = label
    AuditLog.objects.create(
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=label,
        action=action,
        target=target,
        ip=client_ip(request),
        details=details,
        subject=subject[:150],
        session_ref=_session_ref(request) if actor_type == AuditLog.Actor.USER else "",
        user_agent=(request.META.get("HTTP_USER_AGENT", "")[:300] if request is not None else ""),
    )
    logger.info("audit", extra={"action": action, "actor": actor_type, "target": target})
