#!/bin/sh
# Builds the image, starts a throw-away DocNest stack on http://localhost:8099
# (local storage, fresh database) and runs the browser end-to-end tests.
#   ./e2e/run-local.sh            build + test
#   KEEP=1 ./e2e/run-local.sh     keep the stack running afterwards for manual testing
set -eu
cd "$(dirname "$0")"
ROOT=$(cd .. && pwd)
export COMPOSE_PROJECT_NAME=docnest-e2e
export E2E_SECRETS="$PWD/.secrets"
STACK="docker compose -f $ROOT/deploy/compose.yml -f compose.e2e.yml --env-file e2e.env"

if [ "${SKIP_BUILD:-0}" != 1 ]; then
  docker build -f "$ROOT/docker/Dockerfile" --target runtime --build-arg VERSION=e2e -t ghcr.io/primeapi/docnest:e2e "$ROOT"
fi

mkdir -p .secrets
for spec in master_key:48 django_secret_key:64 db_password:32 db_admin_password:32; do
  name=${spec%%:*}; bytes=${spec##*:}
  [ -s ".secrets/$name" ] || openssl rand -base64 "$bytes" | tr -d '\n' > ".secrets/$name"
done
# the container runs as UID 10001 (uses docker instead of sudo to hand over the throw-away secrets)
docker run --rm -v "$PWD/.secrets:/s" alpine sh -c 'chown 10001:10001 /s/* && chmod 400 /s/* && chown 10001:70 /s/db_password && chmod 440 /s/db_password'

$STACK down -v --remove-orphans >/dev/null 2>&1 || true
$STACK up -d
echo "waiting for DocNest…"
for _ in $(seq 1 60); do
  curl -fs http://localhost:8099/api/v1/health >/dev/null 2>&1 && break
  sleep 2
done
echo "E2E-Test-Password-123" | $STACK exec -T app docnest createuser e2e --password-stdin

[ -f fixtures/crooked-page.png ] || ./make-fixtures.sh
[ -d node_modules ] || pnpm install
DOCNEST_URL=http://localhost:8099 npx playwright test || status=$?

if [ "${KEEP:-0}" = 1 ]; then
  echo "Stack still running at http://localhost:8099 (user e2e / E2E-Test-Password-123)."
  echo "Stop it with: $STACK down -v"
else
  $STACK down -v >/dev/null
fi
exit "${status:-0}"
