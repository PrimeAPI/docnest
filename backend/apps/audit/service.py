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


def audit(
    action: str,
    *,
    request: HttpRequest | None = None,
    user: Any = None,
    scanner: Any = None,
    target: str = "",
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
    AuditLog.objects.create(
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=label,
        action=action,
        target=target,
        ip=client_ip(request),
        details=details,
    )
    logger.info("audit", extra={"action": action, "actor": actor_type, "target": target})
