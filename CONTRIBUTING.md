# Contributing to DocNest

Contributions are very welcome — bug reports, ideas, documentation, translations and code.

## Ground rules

- **Security first.** DocNest stores highly sensitive documents. Changes must keep the guarantees in
  [docs/security.md](docs/security.md): no document content in logs, no plaintext content in the database,
  scanner tokens never reach the archive, MFA is never optional.
- **No real personal documents** in issues, tests or fixtures. Use the synthetic generators in
  `backend/tests/pdfs.py`.
- Found a vulnerability? Please report it privately (see [SECURITY.md](SECURITY.md)), not in a public issue.

## Development setup

See [docs/development.md](docs/development.md). In short:

```bash
docker compose -f deploy/compose.dev.yml up -d db
docker compose -f deploy/compose.dev.yml run --rm dev pytest      # backend tests (real OCR)
cd frontend && pnpm install && pnpm dev                             # UI on http://localhost:5173
./e2e/run-local.sh                                                  # full browser test against the image
```

## Pull requests

1. Fork, create a branch from `main`.
2. Keep changes focused; add or update tests.
3. Make sure `ruff check`, `ruff format --check`, `mypy`, `pytest`, `pnpm test` and `pnpm build` pass
   (CI runs them, plus the end-to-end tests).
4. If you change API schemas, regenerate `frontend/openapi.json` and `frontend/src/api/schema.d.ts`
   (see docs/development.md).
5. Describe *why* in the PR description.

By contributing, you agree that your contributions are licensed under the Apache License 2.0.
