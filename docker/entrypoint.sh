#!/bin/sh
# DocNest container entrypoint.
#   all            web server + worker under supervisord (default)
#   web | worker   a single process
#   migrate        apply database migrations
#   createuser U   create a user (prompts for the password)
#   proton-login   one-time Proton Drive login (opens a sign-in link)
#   models         pre-download all Docling models into the persistent cache
#   proton <args>  run the Proton Drive CLI with DocNest's settings
#   manage <args>  any Django management command
set -eu

cd /app/backend
umask 077

wait_for_db() {
  python - <<'PY'
import os, sys, time
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "docnest.settings")
django.setup()
from django.db import connection
for attempt in range(60):
    try:
        connection.ensure_connection()
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001
        print(f"waiting for database ({exc.__class__.__name__})", file=sys.stderr)
        time.sleep(2)
sys.exit("database not reachable")
PY
}

migrate() {
  wait_for_db
  python manage.py migrate --noinput
}

cmd="${1:-all}"
[ $# -gt 0 ] && shift

mkdir -p "${DOCNEST_WORK_DIR:-/tmp/docnest-work}/uploads"
chmod 700 "${DOCNEST_WORK_DIR:-/tmp/docnest-work}"

case "$cmd" in
  all)
    python manage.py check --deploy --fail-level ERROR >/dev/null
    migrate
    exec supervisord -c /etc/docnest/supervisord.conf
    ;;
  web)
    migrate
    exec gunicorn docnest.wsgi:application -c /app/backend/docnest/gunicorn.py
    ;;
  worker)
    wait_for_db
    exec python manage.py worker
    ;;
  migrate)
    migrate
    ;;
  createuser)
    wait_for_db
    exec python manage.py createuser "$@"
    ;;
  resetmfa)
    exec python manage.py resetmfa "$@"
    ;;
  proton-login)
    echo "Open the link below in a browser on any device and sign in to Proton."
    echo "Keep this terminal open until it reports success."
    exec docnest-proton auth login
    ;;
  proton)
    exec docnest-proton "$@"
    ;;
  models)
    exec python manage.py download_docling_models "$@"
    ;;
  manage)
    exec python manage.py "$@"
    ;;
  *)
    exec "$cmd" "$@"
    ;;
esac
