from __future__ import annotations

from django.conf import settings
from ninja import NinjaAPI

from apps.accounts.api import account_router, auth_router
from apps.assist.api import router as assist_router
from apps.core.api import router as core_router
from apps.core.auth import mfa_auth
from apps.documents.alterations_api import router as alterations_router
from apps.documents.api import router as documents_router
from apps.mail.api import router as mail_router
from apps.paper.api import router as paper_router
from apps.scanners.api import manage_router as scanners_manage_router
from apps.scanners.api import upload_router
from apps.scanners.auth import scanner_auth
from apps.taxonomy.api import router as taxonomy_router

_docs = settings.DEV  # interactive API docs only in development

web_api = NinjaAPI(
    title="DocNest Web API",
    version="1",
    urls_namespace="web",
    auth=mfa_auth,
    docs_url="/docs" if _docs else None,
    openapi_url="/openapi.json",
)
web_api.add_router("/", core_router)
web_api.add_router("/auth", auth_router)
web_api.add_router("/account", account_router)
web_api.add_router("/documents", documents_router)
web_api.add_router("/", taxonomy_router)
web_api.add_router("/paper", paper_router)
web_api.add_router("/scanners", scanners_manage_router)
web_api.add_router("/mail", mail_router)
web_api.add_router("/assist", assist_router)
web_api.add_router("/alterations", alterations_router)

upload_api = NinjaAPI(
    title="DocNest Upload API",
    version="1",
    urls_namespace="upload",
    auth=scanner_auth,
    docs_url="/docs" if _docs else None,
    openapi_url="/openapi.json",
)
upload_api.add_router("/", upload_router)
