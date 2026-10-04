from django.urls import path, re_path

from apps.core.views import spa
from docnest.api import upload_api, web_api

urlpatterns = [
    path("api/v1/", web_api.urls),
    path("api/upload/v1/", upload_api.urls),
    re_path(r"^(?!api/|assets/).*$", spa),
]
