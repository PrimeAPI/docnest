from __future__ import annotations

import ipaddress
from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.utils import timezone
from ninja.errors import HttpError
from ninja.security import HttpBearer

from apps.accounts.services import Throttled, check_key_throttle, register_key_failure
from apps.audit.service import client_ip
from apps.scanners import tokens
from apps.scanners.models import ScannerClient


def _ip_allowed(client: ScannerClient, ip: str | None) -> bool:
    if not client.allowed_ips:
        return True
    if ip is None:
        return False
    addr = ipaddress.ip_address(ip)
    for entry in client.allowed_ips:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


class ScannerTokenAuth(HttpBearer):
    """Bearer token auth for scanner devices. Never accepts session cookies."""

    def authenticate(self, request: HttpRequest, token: str) -> Any:
        ip = client_ip(request)
        throttle_key = f"scanner-ip:{ip}"
        try:
            check_key_throttle(throttle_key)
        except Throttled as exc:
            raise HttpError(429, "Too many failed attempts") from exc
        prefix = tokens.parse_prefix(token)
        client = ScannerClient.objects.filter(token_prefix=prefix).first() if prefix else None
        if client is None or not tokens.matches(token, client.token_hash) or not client.is_active:
            register_key_failure(throttle_key, settings.LOGIN_RATE_LIMIT_PER_IP)
            return None
        if not _ip_allowed(client, ip):
            return None
        now = timezone.now()
        if client.last_used_at is None or (now - client.last_used_at).total_seconds() > 60:
            ScannerClient.objects.filter(pk=client.pk).update(last_used_at=now, last_used_ip=ip)
        request.scanner = client  # type: ignore[attr-defined]
        return client


def require_scope(request: HttpRequest, scope: str) -> ScannerClient:
    client: ScannerClient = request.auth  # type: ignore[attr-defined]
    if scope not in client.scopes:
        raise HttpError(403, f"Token lacks scope '{scope}'")
    return client


scanner_auth = ScannerTokenAuth()
