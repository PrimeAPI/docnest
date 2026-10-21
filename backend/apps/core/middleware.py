from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' 'wasm-unsafe-eval'",  # pdf.js image decoders (WebAssembly)
        "style-src 'self' 'unsafe-inline'",  # Tailwind/Radix set inline style attributes
        "img-src 'self' blob: data:",
        "font-src 'self'",
        "connect-src 'self'",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

# An instance export: one large file (apps/transfer/api.py), checked against MAX_IMPORT_BYTES.
IMPORT_UPLOAD_PATH = "/api/v1/transfer/import/upload"

PERMISSIONS_POLICY = (
    "camera=(), microphone=(), geolocation=(), payment=(), usb=(), publickey-credentials-get=(self)"
)


class SecurityHeadersMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        if request.path.startswith(("/share/", "/api/v1/shares/")):
            response["Referrer-Policy"] = "no-referrer"
            response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
            response["Cache-Control"] = "no-store"
        return response


class UploadSizeLimitMiddleware:
    """Reject oversized request bodies before Django parses them."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        from django.conf import settings
        from django.http import JsonResponse

        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if request.path.startswith("/api/upload/"):
            limit = settings.MAX_UPLOAD_BYTES + 1024 * 1024
        elif request.path == IMPORT_UPLOAD_PATH:
            limit = settings.MAX_IMPORT_BYTES
        else:
            limit = 10 * 1024 * 1024
        if length > limit:
            return JsonResponse({"detail": "Request too large"}, status=413)
        return self.get_response(request)
