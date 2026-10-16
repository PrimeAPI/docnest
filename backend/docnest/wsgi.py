import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "docnest.settings")
application = get_wsgi_application()

from apps.core.debuglog import log_startup  # noqa: E402  (needs the app registry)

log_startup("web")
