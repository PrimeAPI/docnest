# Architecture

DocNest is a modular monolith: a Django backend and a React frontend in one repository, shipped as one container image.

```
 Scanner ──HTTPS──► /api/upload/v1  (scanner token)                        ┌──────────────┐
                         │                                                 │ Proton Drive │
 Browser ──HTTPS──► /api/v1 + SPA   (session + MFA)                        └──────▲───────┘
                         │                                                        │ Proton Drive CLI
               ┌─────────▼──────────┐   jobs (SKIP LOCKED, NOTIFY)   ┌────────────┴─────────┐
               │  web (gunicorn)    │ ─────────────────────────────► │ worker               │
               │  Django + Ninja    │                                │ validate → OCR →     │
               └─────────┬──────────┘                                │ analyze → store →    │
                         │                                           │ index                │
                         ▼                                           └────────────┬─────────┘
                 PostgreSQL (metadata, encrypted text, blind index, jobs) ◄────────┘
```

## Components

| Path | Responsibility |
|---|---|
| `backend/docnest/` | Settings (`*_FILE` secrets, startup checks), URL routing, API wiring, gunicorn config |
| `apps/core` | Security headers, logging with redaction, SPA serving, health/overview/system API, management commands |
| `apps/crypto` | HKDF key derivation, AES-GCM values, chunked file encryption |
| `apps/accounts` | Users, Argon2, TOTP, WebAuthn, recovery codes, throttling, sessions, auth API |
| `apps/scanners` | Scanner clients, token auth, upload API, scanner management API |
| `apps/documents` | Document model, encrypted field accessors, durable intake, file serving, document API |
| `apps/taxonomy` | Buckets, types, tags + aliases, correspondents, series, rules; their API |
| `apps/processing` | Job queue, worker, pipeline stages, PDF sanitizing/OCR/text/thumbnail tools |
| `apps/analysis` | Metadata extraction, keyword knowledge, Naive Bayes classifiers, series detection, titles |
| `apps/search` | Tokenizer, blind index, BM25 ranking, snippets |
| `apps/storage` | Storage interface; Proton Drive CLI and local filesystem backends |
| `apps/audit` | Audit log |
| `frontend/` | React + TypeScript + Vite, Tailwind, Radix primitives, TanStack Query, PDF.js; API types generated from OpenAPI |
| `docker/` | Multi-stage Dockerfile, entrypoint, supervisord, Proton CLI wrapper and encrypted `pass` shim |
| `deploy/` | Production `compose.yml`, `.env.example`, `init-secrets.sh`, development compose |
| `e2e/` | Playwright browser tests against the production image |

## Document lifecycle

1. **Intake** (web process — from the scanner API or a drag & drop upload in the UI): validate type/size, HMAC for dedupe, encrypt to the intake volume (fsync), create the `Document` and a `process_document` job in one transaction → `202`.
2. **validate**: pikepdf opens the file strictly, rejects encrypted/oversized PDFs, strips active content; the sanitized file replaces the intake copy.
3. **ocr**: selectable per document. OCRmyPDF (`--skip-text`, PDF/A, deskew, rotation) produces a searchable archive and extracts text with `pdftotext`. Docling extracts Markdown plus its lossless layout/table JSON; the sanitized original remains the archive because Docling does not emit searchable PDF/A. Born-digital PDFs keep their embedded text (only their images are OCRed); scans are rendered with pdfium and OCRed per page, and layout regions that page OCR left empty (e.g. white text on a coloured band) get a second, filtered OCR pass. Text, structured output, and thumbnails are encrypted; the archive goes to the intake encrypted. The initial default comes from `DOCNEST_OCR_BACKEND` and can be changed under Settings → System.
4. **analyze**: rules → known senders in the text → classifiers → keyword knowledge; date, amounts, IBANs, references; tags; series; title. Docling documents can use layout-aware sender/title rules, local VLM extraction, or a confidence-based hybrid of both. User-set fields are never changed.
5. **store**: upload original + archive to `<root>/<bucket>/<year>/<uuid>/`, verify size and SHA-1, then delete the intake copies.
6. **index**: blind-index title, content and metadata.

Each stage is idempotent; `processing_stage` records where to resume. Failures retry with exponential backoff; permanent failures (invalid PDF) are shown in the UI. If Proton Drive needs a new login, storage jobs are deferred without consuming retries. A background heartbeat renews every active job lease throughout long OCR/model inference, so slow Docling work is not mistaken for a crashed worker. The worker runs up to the live concurrency limit from Settings; the bottom-left queue popup shows waiting, active and recent jobs with elapsed time. Finished queue history is operational data and expires after 24 hours by default.

## Design decisions

- **Blind index instead of plaintext full-text search** — see [security.md](security.md).
- **In-house job queue** on PostgreSQL (`SELECT … FOR UPDATE SKIP LOCKED`, leases, `LISTEN/NOTIFY`) — no extra broker.
- **Local-only optional VLM** — deterministic analysis remains the default for small hardware; the version-pinned Docling extraction model is downloaded into a persistent cache on demand and never receives document data over the network.
- **Naive Bayes over hashed features** (instead of scikit-learn) — small, dependency-free, and the model contains no plaintext.
- **Hard delete** — deleting a document removes its database rows (text, index, thumbnail) and its Proton Drive folder.
- **One container** with web and worker under supervisord, as requested in the vision; the image can also run `web` or `worker` alone.
