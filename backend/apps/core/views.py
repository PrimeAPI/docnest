from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseNotFound
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET


@require_GET
@ensure_csrf_cookie
def spa(request: HttpRequest) -> HttpResponse:
    """Serve the single-page app's index.html for all client-side routes."""
    index = settings.FRONTEND_DIR / "index.html"
    if not index.exists():
        return HttpResponseNotFound("Frontend not built. Run `pnpm build` in frontend/.")
    response = HttpResponse(index.read_bytes(), content_type="text/html; charset=utf-8")
    response["Cache-Control"] = "no-cache"
    return response
