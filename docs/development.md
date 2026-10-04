# Development

Everything backend-related runs in the `dev` Docker image (Python 3.13, OCR toolchain, Proton CLI), so tests behave exactly like production. The frontend uses Node 24 + pnpm locally.

## Backend

```bash
docker compose -f deploy/compose.dev.yml up -d db                     # PostgreSQL on 127.0.0.1:55432
docker compose -f deploy/compose.dev.yml run --rm dev pytest          # all tests, including real OCR
docker compose -f deploy/compose.dev.yml run --rm dev pytest -m "not ocr" -x
docker compose -f deploy/compose.dev.yml run --rm dev ruff check .
docker compose -f deploy/compose.dev.yml run --rm dev ruff format .
docker compose -f deploy/compose.dev.yml run --rm dev mypy apps docnest
```

Run the API + worker with auto-reload (local storage, dev secrets):

```bash
docker compose -f deploy/compose.dev.yml up web worker                # http://localhost:8000
docker compose -f deploy/compose.dev.yml run --rm dev python manage.py createuser dev
```

If your host user is not UID/GID 1000, export `DOCNEST_UID` / `DOCNEST_GID`.

Dependencies are managed with uv (`backend/pyproject.toml`, `backend/uv.lock`). To update the lock file:

```bash
docker run --rm -v "$PWD/backend":/w -w /w -u $(id -u):$(id -g) -e HOME=/tmp python:3.13-slim-trixie \
  sh -c "pip install -q --user uv && /tmp/.local/bin/uv lock --upgrade"
```

## Frontend

```bash
cd frontend
pnpm install
pnpm dev          # http://localhost:5173, proxies /api to localhost:8000
pnpm test
pnpm build        # type-check + production build into dist/
```

With the Vite dev server, set `DOCNEST_DEV_BASE_URL=http://localhost:5173` for the backend so CSRF and passkeys accept that origin.

### API types

The frontend's API client is typed from the backend's OpenAPI schema. After changing a schema:

```bash
docker compose -f deploy/compose.dev.yml run --rm -v "$PWD/frontend:/out" dev \
  python manage.py export_openapi_schema --api docnest.api.web_api --output /out/openapi.json --indent 1
cd frontend && pnpm api:types
```

CI fails if the committed schema or types are out of date.

## End-to-end tests

```bash
./e2e/run-local.sh                 # build the production image, start a throw-away stack on :8099, run Playwright
KEEP=1 ./e2e/run-local.sh          # keep the stack running for manual testing (user e2e / E2E-Test-Password-123)
SKIP_BUILD=1 ./e2e/run-local.sh    # reuse the last image
```

## Test data

Never use real documents. `backend/tests/pdfs.py` generates synthetic letters (text PDFs and image-only "scans"); `e2e/make-fixtures.sh` writes some to `e2e/fixtures/`.

## CI/CD

| Workflow | Trigger | What it does |
|---|---|---|
| `preview.yml` | every push, any branch | builds and publishes `:sha-<commit>`, `:<branch>` and — on `main` — `:edge` (no tests) |
| `release.yml` | tag `vX.Y.Z` | builds and publishes `:X.Y.Z`, `:X.Y`, `:latest` and creates a GitHub release |
| `ci.yml` | pull requests, manual | backend (lint, types, tests incl. OCR) and frontend (types, tests, build) |
| `e2e.yml` | manual | browser end-to-end tests against the production image |
| `security.yml` | push, PR, weekly | gitleaks over the git history, pip-audit, pnpm audit |

Run the tests before tagging a release: `gh workflow run ci.yml` (and `gh workflow run e2e.yml`), or locally as described above.

Images are built for linux/amd64 with SBOM and provenance attestation; a Trivy report is in the job log.

### Releasing

```bash
git tag -a v0.1.0 -m "DocNest 0.1.0"
git push origin v0.1.0
```
