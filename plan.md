# DocNest – Implementation Plan

This plan describes how the system specified in `vision.md` will be built end to end. It turns the vision into concrete architecture decisions, a data model, a processing pipeline, a security concept, and a sequence of milestones that can each be implemented, tested, and reviewed on their own.

All code, comments, documentation, commit messages, the web UI, and this plan are in English.

- **Repository:** `github.com/primeapi/docnest` (default branch `main`)
- **Image:** `ghcr.io/primeapi/docnest`
- **License:** Apache-2.0 — free for anyone to use, modify, and redistribute (also commercially), but redistributions must keep the copyright and attribution notices (`LICENSE` + `NOTICE`), so nobody can pass the project off as their own work. Contributions are explicitly welcome (`CONTRIBUTING.md`).

## Implementation status (2026-10-04)

All milestones M0–M7 are implemented. Verification:
- 67 backend tests (unit, integration, security, resilience; real OCR in the dev image)
- 4 frontend unit tests
- 4 Playwright browser tests against the production image (login + MFA enrollment, scanner token, upload → OCR → search → view/edit, tags/senders)
- The Proton Drive adapter was tested against a real account

Deviations from the original plan (the original plan below is kept unchanged for reference):

| Plan | Implemented | Why |
|---|---|---|
| scikit-learn classifier (2.6) | Small in-house Naive Bayes over blind-index hashes | No heavy dependencies; the trained model holds only keyed hashes, so it needs no extra encryption |
| Soft delete | Hard delete (DB rows + Proton folder) | Deleted content should really be gone |
| Multi-arch image (amd64 + arm64) | amd64 only | The Proton Drive CLI checksum was only verified for linux-x64 |
| Keyboard shortcuts `/ j k d i` | `/` (focus search) only | Row actions are one click; more shortcuts can follow |
| Postgres app role | Separate superuser (`postgres`) + non-superuser `docnest` role owning the DB | Least privilege (SEC05) |
| Proton login | In-container `docnest proton-login` with an encrypted `pass` shim (see `docs/proton-drive.md`) | Verified in spike S1 |

Resolved: the Proton Drive CLI is MIT-licensed (github.com/ProtonDriveApps/sdk), so bundling it in the public image is allowed; its license text ships in the image.

---

## 1. Guiding Constraints (from the vision)

| Area | Constraint |
|---|---|
| Architecture | Modular monolith, `frontend/` + `backend/` in one repo, one Docker image |
| Stack | React + TS + Vite, Tailwind + shadcn/ui, React Router, TanStack Query, PDF.js · Python + Django + Django Ninja, PostgreSQL, Django ORM · Python worker with PostgreSQL job queue · OCRmyPDF + Tesseract, pikepdf |
| Storage | Proton Drive via the official Proton Drive CLI is the permanent document store. The service holds files only temporarily (processing, viewing, download). |
| Analysis | **No LLMs at all** — neither remote APIs nor local models. All analysis is rules, heuristics, and lightweight classical ML running in-process. |
| Users | Single user / small circle. No multi-tenancy, no public sharing. |
| Security | Security by design, secure by default, no plaintext OCR text in the DB, MFA mandatory, scanner credentials strictly separated from user access. |
| Reliability | A confirmed upload must never be lost. Processing is idempotent and retryable. Failures are visible. |
| Ops | `docker compose up -d` after providing secrets. Images on GHCR (`latest` + semver), built by GitHub Actions, gated by tests. |

---

## 2. Key Architecture Decisions

These are the decisions the vision explicitly leaves open (SEC01, UC05–UC07, job queue, Proton integration). Each is the recommended default; alternatives are noted where they matter.

### 2.1 Encrypted content + blind-index search (SEC01)

**Problem:** OCR text must not be stored as plaintext in PostgreSQL, but full-text search over it must be fast.

**Solution:**

1. **Encryption at rest (application level).** OCR text, the document title, extracted free-text fields, and thumbnails are encrypted with AES-256-GCM before being written to the DB. Each record stores `key_version` + nonce, allowing key rotation.
2. **Blind index for search.** During indexing, text is normalized (lowercase, Unicode NFKC, umlaut folding `ä→ae` etc.), tokenized, and stemmed (Snowball, German + English). Each resulting term is turned into `HMAC-SHA256(index_key, term)` (truncated to 16 bytes). Only these hashes are stored in a `search_term` table together with document id, field (title/content/sender), and term frequency.
3. **Prefix search.** Additionally, edge n-grams (length 3–10) of each term are HMAC'd and stored, so `Fahrzeugvers` finds `Fahrzeugversicherung`. Compound words in German are a real issue: the edge n-gram approach covers prefixes; infix matches (`versicherung` inside `Fahrzeugversicherung`) are covered by also indexing a decompounded form (dictionary-based splitter, e.g. `compound-split` / CharSplit) during indexing.
4. **Querying.** The query string goes through the same pipeline; the DB matches hashes, ranks by a BM25-style score computed in SQL from stored term frequencies and document lengths, and combines this with the plaintext filters (bucket, type, tags, dates, status). Snippets/highlights are produced in the application by decrypting only the OCR text of the result page (≤ 25 documents).
5. **Keys.** One master key from a secret file (`DOCNEST_MASTER_KEY_FILE`). Sub-keys derived via HKDF: `enc-key-v{n}`, `index-key-v{n}`, `token-pepper`. The master key never touches the DB or logs.

**What this protects against:** a stolen DB dump or backup, DB-level access by another process, accidental exposure via DB tooling. **What it does not protect against:** an attacker with both the DB and the master key, or full control of the running app container. Blind indexes leak term frequency / co-occurrence patterns to someone who has only the DB — acceptable for this threat model and documented in `docs/security.md`.

**Plaintext by design** (needed for filtering, low sensitivity): bucket, document type, tag names, correspondent names, series names, dates, status, flags. This list is reviewed explicitly in M3 and can be tightened later (e.g. correspondent names could also be encrypted and matched via blind index).

Alternatives considered and rejected: plaintext PostgreSQL FTS with disk encryption only (does not meet SEC01), an encrypted external search engine (Tantivy/Meilisearch index files encrypted at rest — more moving parts, still plaintext in memory and on disk while running), fully client-side search (does not scale to thousands of OCR'd documents in a browser).

### 2.2 Durable intake before Proton Drive (REL01, SEC07)

The vision says Proton Drive is the permanent store and the service holds files only temporarily. To satisfy "no loss after confirmed upload", the upload endpoint writes the PDF **encrypted (AES-GCM, streaming) to a persistent intake volume** and commits the DB row + first job in the same transaction **before** returning `202 Accepted`. The intake copy is deleted only after Proton Drive storage has been verified (upload + size/hash check via the storage adapter). If Proton is unreachable, documents wait in the intake volume and the UI shows "Pending storage".

Temporary working files (OCR, view, download) live in a dedicated directory on a `tmpfs` mount, mode `0700`, created per job via a context manager that deletes on exit, plus a periodic sweeper for orphans after crashes.

### 2.3 Storage abstraction

```python
class StorageBackend(Protocol):
    def put(self, local_path: Path, key: StorageKey) -> StoredObject: ...
    def get(self, ref: StoredObject, dest: Path) -> None: ...
    def exists(self, ref: StoredObject) -> bool: ...
    def delete(self, ref: StoredObject) -> None: ...
    def healthcheck(self) -> HealthStatus: ...
```

Implementations:
- `ProtonDriveCliBackend` – wraps the official [Proton Drive CLI](https://proton.me/support/drive-cli) (`proton-drive`) via `subprocess` (no shell, argument lists only, always with `--json` so output is parsed, never scraped; stdout/stderr scrubbed before logging). Commands used: `fs upload -f skip -t`, `fs info` (verification via size + SHA-1), `fs download -f remove`, `fs create-folder`, `fs trash` + `fs delete`. Exit code 1 + stderr on errors.
- `LocalFilesystemBackend` – for development, tests, CI, and as a future option (vision §9).
- An S3 backend can be added later without core changes.

Remote layout: `/my-files/DocNest/<bucket>/<yyyy>/<document-uuid>/original.pdf` and `.../archive.pdf` (OCR'd PDF/A). Names use UUIDs, not titles, so no metadata leaks into file names. Both original and OCR version are stored; the original is the source of truth.

**Proton authentication model (verified in S1, see `docs/proton-drive.md`).** The CLI keeps its session (tokens + the key password that unlocks the account keys) in a pluggable credentials store. DocNest sets `PROTON_DRIVE_CREDENTIALS_STORE=pass` and ships its own `pass` shim, which stores the session AES-256-GCM-encrypted in the `proton` volume, with the key derived from the DocNest master key. The plaintext session never touches disk, and a stolen volume is useless without the master key.

- Login is a one-time operator step, done **inside the container** (no host install): `docker compose run --rm app proton-drive auth login` prints a link that can be opened on any device.
- All CLI calls are serialized via `flock` (the CLI rotates refresh tokens, and concurrent refreshes could invalidate the session). CLI log level is `WARNING`, and its cache stays encrypted.
- The binary is downloaded during the image build from Proton's URL, pinned by version + SHA-256.
- The app checks the session on startup and periodically (`healthcheck()` → `fs info` on the DocNest root). An expired session puts storage into `needs_reauth`: uploads keep being accepted into the encrypted intake (no loss), storage jobs pause, and the UI shows a banner with the re-login command.
- Measured latency is ~3–4 s per CLI call. This is fine for the worker. For viewing, the optional encrypted view cache below becomes more valuable.

Optional, configurable: a small **encrypted local view cache** (LRU, size-capped, TTL) so repeatedly opening the same PDF does not re-download it each time. Off by default to honor "temporary only".

### 2.4 Job queue

A small purpose-built queue on PostgreSQL rather than an extra library:

- Table `job(id, kind, document_id, state, attempts, max_attempts, run_after, locked_by, locked_until, last_error, created_at, updated_at)`.
- Workers claim jobs with `SELECT … FOR UPDATE SKIP LOCKED`, use a lease (`locked_until`) so crashed workers' jobs are picked up again, and use `LISTEN/NOTIFY` to wake up immediately on new jobs.
- Exponential backoff, `max_attempts`, then state `failed` → visible in UI with "Retry" action.
- Unique constraint on `(kind, document_id)` for active jobs prevents duplicate work.

(Alternative: `procrastinate` – mature and Postgres-based. Reconsider if the custom queue grows beyond ~300 lines.)

### 2.5 Processing pipeline (UC01, UC05–UC07, REL02)

Each document has a `processing_stage` and each stage is idempotent (it checks its own output before redoing work and writes results in a transaction):

```
received ─► validated ─► ocr ─► extract ─► classify ─► tag ─► series ─► store ─► index ─► done
                                                                                   │
                                                          any stage ─► failed (retryable, visible)
```

| Stage | What happens |
|---|---|
| validate | Magic bytes, size limit, `pikepdf` open, page count limit, reject encrypted PDFs, strip JavaScript/embedded files/auto-actions, compute SHA-256 (dedupe) |
| ocr | `ocrmypdf --output-type pdfa --skip-text -l deu+eng` (skip pages that already have text), extract text per page, generate thumbnail of page 1 |
| extract | Dates (regex + `dateparser`, picking the most plausible "document date"), amounts, IBANs, invoice/customer/contract numbers, sender block heuristics |
| classify | Document type, correspondent, bucket suggestion (scanner-provided bucket/type win unless "auto") |
| tag | Tag suggestions constrained to existing vocabulary (see 2.6) |
| series | Assign to an existing series or propose a new one (see 2.7) |
| store | Upload original + archive PDF to storage backend, verify, delete intake copy |
| index | Build blind index terms, encrypt and save content |

**Human override:** every auto-filled field records its provenance (`auto` / `scanner` / `user`). Re-processing never overwrites fields set by the user.

### 2.6 Analysis & classification strategy (UC05, UC07)

Layered, so the system works fully offline and gets better with use:

1. **Rules & heuristics** (always on): extraction above, plus user-defined match rules on correspondents/tags (e.g. "contains `Allianz` → correspondent Allianz, tag Insurance"), evaluated in the application on decrypted text.
2. **Learned classifier** (always on once ≥ N documents exist): scikit-learn, TF-IDF + linear models (one per target: type, correspondent, tags multi-label, series). Retrained in the background after user corrections. The model file contains vocabulary derived from content, so it is **stored encrypted** with the content key.
3. **Title generation** without an LLM: templates per document type + correspondent (e.g. `{type} {correspondent} {period}` → "Invoice Telekom March 2026"), learned from titles the user has written for similar documents (most frequent title pattern within the same correspondent/series), falling back to the first meaningful line of the subject block.

**No LLMs** are used anywhere — document text never leaves the process, and the system runs on modest hardware (target: 2 vCPU / 2–4 GB RAM including OCR).

**Tag hygiene (UC07):** tags have aliases (`KFZ`, `Auto`, `PKW` → `Vehicle`). Suggested tags are normalized and matched against names + aliases (exact, then fuzzy via `rapidfuzz`). New tags created automatically are flagged `suggested` and listed in the Tags view for confirm/merge. The Tags view supports merge (re-points all documents, keeps the old name as alias).

### 2.7 Recurring documents / series (UC06)

Model: `Series(name, correspondent, document_type, cadence: monthly|quarterly|yearly|irregular, period_label_format)`. A document belongs to at most one series and gets a `period_label` (e.g. "January 2026") derived from the document date and cadence.

Detection: candidate series are those with the same correspondent and type. Similarity is measured on the blind-index term sets (MinHash/Jaccard on the "template" terms that recur across members, like `Gehaltsabrechnung`, `Personalnummer`) plus the learned classifier. Above a high threshold → auto-assign; middle band → suggestion shown in the Inbox ("Belongs to series *Payslips*?"); new series are proposed when ≥ 2 similar unassigned documents from the same correspondent appear. Users can assign, move, and remove documents from series.

### 2.8 Authentication & authorization (UC02, SEC02–SEC04)

**Users (web):**
- Django auth with **Argon2id** hashing; password policy: min. 12 chars, zxcvbn-style strength check, breached-password check against a bundled k-anonymity-free list (no external calls by default).
- **MFA mandatory.** WebAuthn/passkeys/security keys via `py_webauthn` (preferred), TOTP via `pyotp` as alternative, plus one-time recovery codes (hashed). No SMS. A user without a second factor can only reach the MFA enrollment page.
- Login is a two-step state machine (password → second factor) stored in the session; the session is only "fully authenticated" after step 2. Session ID rotated on each privilege change.
- Brute-force protection: per-account and per-IP rate limits with progressive delay and temporary lockout (DB-backed, so it survives restarts), audit log entries for failures.
- Sessions: DB-backed, `Secure`, `HttpOnly`, `SameSite=Strict`, `__Host-` prefix, idle timeout (default 30 min) + absolute timeout (default 12 h), logout invalidates server-side, "log out all other sessions" in settings.
- CSRF protection on all state-changing endpoints (Django CSRF with Ninja's `django_auth` + CSRF).
- Re-authentication (password or WebAuthn) required for sensitive actions: managing MFA devices, scanner tokens, password change.
- Bootstrap: `docnest createuser` management command (no default credentials ever); first login forces MFA enrollment.

**Scanners (upload API):**
- Separate model `ScannerClient` with **scoped API tokens** (`dn_scan_<id>_<secret>`, 256-bit secret, stored as SHA-256 hash, shown once). Scopes: `upload`, `upload:status`. Revocable, optional expiry, `last_used_at`, optional IP allow-list.
- Upload API lives under its own router `/api/upload/v1/` with its own auth class; user session cookies are **not** accepted there, and scanner tokens are **not** accepted anywhere else.
- A scanner can only query the status of documents it uploaded itself, and the status response contains no content or metadata beyond processing state.

**Authorization in general:** single-user focus, but every query is still scoped through a central `documents_for(user)` service, so a future second user does not require a rewrite.

### 2.9 HTTP & platform hardening (SEC05–SEC09)

- Strict security headers: CSP (`default-src 'self'`, no inline scripts; PDF.js worker served from self), HSTS, `X-Content-Type-Options`, `Referrer-Policy: no-referrer`, `frame-ancestors 'none'`, `Permissions-Policy`.
- `SECURE_PROXY_SSL_HEADER` + configurable trusted proxies; the app refuses to start in production mode without `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS` and with `DEBUG=True`.
- PDFs are served with `Content-Disposition` and `Content-Type: application/pdf`, from the same origin, with `Cache-Control: no-store`.
- Container runs as a non-root user, read-only root filesystem, `tmpfs` for `/tmp` and the work dir, `no-new-privileges`, all capabilities dropped. Postgres not exposed to the host network.
- DB access: the app uses a role without superuser rights; migrations run with the same role (owner of the schema) — no runtime superuser.
- **Logging:** structured JSON logs; a logging filter redacts known secret patterns (tokens, `Authorization`, cookies, passwords); document content is never passed to loggers (enforced by code review + a test that runs the pipeline and greps captured logs for OCR text fragments).
- **Audit log** (DB table): logins, MFA changes, token creation/revocation, downloads, deletions, settings changes.
- **Secrets:** every secret setting supports `NAME_FILE` (read from file, e.g. Docker secret) with `NAME` as fallback only in development. Startup check fails if a required secret is missing, too short, or file permissions are too open.

---

## 3. Data Model (first version)

```
User (Django)                 — + mfa_enrolled, last_login_ip
WebAuthnCredential            — user, credential_id, public_key, sign_count, name, created/last_used
TotpDevice                    — user, encrypted secret, confirmed
RecoveryCode                  — user, code_hash, used_at
ScannerClient                 — name, token_hash, token_prefix, scopes, allowed_ips, expires_at, revoked_at, last_used_at

Bucket                        — name, slug, color (seeded: Private, Business, Studies)
DocumentType                  — name, slug (seeded: Mail, Contract, Invoice, Notice, Statement, Other)
Tag                           — name, slug, color, is_suggested
TagAlias                      — tag, alias
Correspondent                 — name, aliases, match rules
Series                        — name, correspondent, document_type, cadence

Document
  id (UUID), bucket, document_type, correspondent, series, period_label
  title_enc, document_date, uploaded_at, received_from (ScannerClient)
  status: new | todo | done
  is_important, read_at
  sha256_original (unique per bucket → dedupe), page_count, size
  processing_stage, processing_state: pending | running | done | failed, processing_error
  storage_original_ref, storage_archive_ref, intake_path
  field_provenance (JSON: field → auto|scanner|user)
  extracted (encrypted JSON: amounts, IBAN, reference numbers…)
DocumentTag                   — document, tag, source (auto|scanner|user), confidence
DocumentContent               — document, content_enc, key_version, language, length
DocumentThumbnail             — document, image_enc, key_version
SearchTerm                    — document, field, term_hash (bytea 16), tf     [index on term_hash]
DocumentStats                 — document, field lengths for BM25

Job                           — see 2.4
ProcessingEvent               — document, stage, outcome, duration, error (sanitized)
AuditLog                      — actor (user|scanner|system), action, target, ip, timestamp
ClassifierModel               — target, model_enc, trained_at, sample_count
```

"Markings" from the vision map to `status` (Todo) and `is_important` (Important); "new/unread" maps to `read_at IS NULL`. If more free markings are needed later they become a `Marking` table — not in v1.

---

## 4. API Surface

Base: same origin, `/api/…`, OpenAPI generated by Django Ninja; TypeScript client generated from it (`openapi-typescript` + a thin fetch wrapper) as part of the build so frontend and backend can't drift.

**Upload API** (`/api/upload/v1/`, scanner token):
- `POST /documents` – multipart: `file`, `bucket`, `document_type` (or `auto`), optional `important`, `todo`, `tags[]`, `metadata` (JSON). Header `Idempotency-Key` supported. → `202 { id, status_url }`. Duplicate SHA-256 → `200` with existing id (no second document).
- `GET /documents/{id}/status` – processing state only.
- `GET /health` – liveness for scanner devices.

**Web API** (`/api/v1/`, session + CSRF + MFA-complete):
- Auth: `POST /auth/login`, `POST /auth/mfa/webauthn/begin|complete`, `POST /auth/mfa/totp`, `POST /auth/logout`, `GET /auth/me`, enrollment endpoints, recovery codes, sessions list/revoke.
- Documents: `GET /documents` (search query + filters + sort + cursor pagination), `GET /documents/{id}`, `PATCH /documents/{id}`, `POST /documents/{id}/read`, `DELETE /documents/{id}` (soft delete, then storage delete), `GET /documents/{id}/file?variant=archive|original` (streams from storage), `GET /documents/{id}/thumbnail`, `POST /documents/{id}/reprocess`.
- Inbox counters: `GET /overview` (new, unread, todo, important, failed, pending storage).
- Taxonomy: CRUD for buckets, types, tags (+ merge, aliases), correspondents, series.
- Admin/settings: scanner clients CRUD + token rotation, processing failures list + retry, audit log, system health (DB, worker heartbeat, storage, OCR).

---

## 5. Frontend

Routes (matching QR01):

| Route | Purpose |
|---|---|
| `/login`, `/login/mfa`, `/setup/mfa` | Auth flow, enrollment |
| `/inbox` (default) | New/unread documents, series suggestions, failed processing — "work through incoming mail" |
| `/documents` | List/grid with search bar + filter panel (bucket, type, tags, status, important, upload date range, document date range, correspondent, series); filter state in URL so searches are bookmarkable |
| `/documents/:id` | PDF.js viewer + metadata side panel (edit title, date, type, bucket, tags, correspondent, series, status, important); provenance hints ("auto-detected") |
| `/todos` | Documents with status Todo, sorted by date/importance, one-click "Done" |
| `/series`, `/series/:id` | Series overview and timeline (e.g. payslips by month, gaps highlighted) |
| `/tags` | Tag management: rename, merge, aliases, confirm suggested tags |
| `/settings` | Account, password, MFA devices, sessions, scanner clients & tokens, processing/health, audit log |

UX details: keyboard shortcuts (`/` search, `j/k` navigate, `d` done, `i` important), optimistic updates with TanStack Query, responsive layout (sidebar collapses to bottom nav on mobile), dark mode. UI language is English; strings live in one catalog (`react-i18next`) so translations could be added later without refactoring. Dates/amounts are formatted per browser locale.

Security in the frontend: no tokens in `localStorage`; cookies only. PDFs rendered via PDF.js from an in-memory blob (no third-party viewer). No external fonts/CDNs (self-hosted assets, CSP-compatible).

---

## 6. Repository Layout

```
DocNest/
├── backend/
│   ├── pyproject.toml            # uv-managed; ruff, mypy, pytest config
│   ├── docnest/                  # Django project (settings split: base/dev/prod/test)
│   ├── apps/
│   │   ├── accounts/             # users, MFA, sessions, rate limiting
│   │   ├── scanners/             # scanner clients, tokens, upload API
│   │   ├── documents/            # models, services, web API
│   │   ├── taxonomy/             # buckets, types, tags, correspondents, series
│   │   ├── processing/           # job queue, worker, pipeline stages
│   │   ├── analysis/             # extraction, rules, classifier, title templates
│   │   ├── search/               # tokenizer, blind index, query engine
│   │   ├── storage/              # backend interface, Proton CLI, local FS
│   │   ├── crypto/               # key derivation, AEAD helpers, streaming encryption
│   │   └── audit/
│   └── tests/
├── frontend/
│   ├── package.json              # pnpm; vite, vitest, eslint, prettier
│   └── src/ (app/, routes/, components/, features/, api/, locales/)
├── e2e/                          # Playwright tests against the compose stack
├── deploy/
│   ├── compose.yml               # copy-paste ready
│   ├── compose.dev.yml
│   ├── .env.example
│   └── secrets/README.md         # how to generate each secret
├── docker/
│   ├── Dockerfile                # multi-stage
│   └── supervisord.conf (or s6 services)
├── docs/ (architecture.md, security.md, operations.md, scanner-api.md, development.md)
├── .github/workflows/ (ci.yml, release.yml)
├── vision.md
└── plan.md
```

---

## 7. Deployment

**Dockerfile (multi-stage):**
1. `frontend-build` – Node LTS, `pnpm install --frozen-lockfile`, generate API types, `vite build`.
2. `python-deps` – build wheels with `uv`.
3. `runtime` – `python:3.13-slim` + `ocrmypdf`, `tesseract-ocr` (`deu`, `eng`), `ghostscript`, `qpdf`, Proton Drive CLI (pinned version + checksum verification, subject to the S1 licensing check), app wheels, frontend `dist/` served by Django via WhiteNoise with SPA fallback. Non-root user. `HEALTHCHECK`.

**Process model:** one `app` container (vision §12.4). The entrypoint runs gunicorn (uvicorn workers) and the worker as separate processes under a process manager (supervisord). Sub-commands `web`, `worker`, `migrate`, `manage …` exist for debugging and maintenance. Migrations run automatically at startup behind an advisory lock.

**compose.yml:** `app` + `db` (postgres:17, healthcheck, internal network only), named volumes for DB data and the encrypted intake, `tmpfs` for work dir, Docker secrets for `master_key`, `db_password`, `django_secret_key`; a `proton-session` volume (or host bind mount) for the Proton CLI session (see 2.3). Ports: app binds to `127.0.0.1:8000` by default (behind the existing reverse proxy). Example reverse-proxy snippets (Caddy, Traefik, nginx) in `docs/operations.md`, including the upload body size limit.

**Backups:** documented: PostgreSQL dump (contains only encrypted content + metadata) + the master key stored separately offline. Without the master key, backups are unreadable — this is stated loudly in the docs. Documents themselves live in Proton Drive.

---

## 8. CI/CD

- `ci.yml` (push/PR): backend lint (ruff), types (mypy), tests (pytest with a PostgreSQL service container, real OCRmyPDF on small fixture PDFs), frontend lint/typecheck/vitest, OpenAPI-types drift check, Docker build (no push), Trivy image scan, `pip-audit` + `pnpm audit`, gitleaks secret scan.
- `release.yml` (tag `v*.*.*`): runs the full CI job first (`needs:`), then builds multi-arch images (amd64 + arm64) with Buildx and pushes to `ghcr.io/primeapi/docnest:<version>`, `:<major>.<minor>`, `:latest`. SBOM + provenance attestation. Failing tests block the push.
- Dependabot for pip, npm, GitHub Actions, Docker base images.

---

## 9. Testing Strategy

| Level | Tooling | Focus |
|---|---|---|
| Unit | pytest | crypto helpers, tokenizer/stemming/n-grams, extraction regexes, rate limiter, tag normalization, series detection |
| Integration | pytest-django + Postgres | upload API, job queue claim/lease/retry, full pipeline on fixture PDFs with `LocalFilesystemBackend`, auth flows incl. WebAuthn (via soft authenticator), search ranking |
| Security tests | pytest | scanner token cannot access web API and vice versa, session cookie flags, CSRF enforced, MFA-incomplete sessions blocked, no OCR text / secrets in logs, no plaintext content in DB (scan DB dump for fixture phrases) |
| Resilience | pytest | kill worker mid-stage → job re-claimed, no duplicate documents; Proton unavailable → document stays in intake, retried |
| Frontend | Vitest + Testing Library | components, filter/URL state, forms |
| E2E | Playwright against compose stack | login with TOTP, upload via API, document appears in inbox, search finds OCR'd word, edit metadata, mark done |

Test fixtures: a set of synthetic German/English PDFs (invoices, payslips, letters) generated by a script — no real personal documents in the repo.

---

## 10. Milestones

Each milestone ends in a working, tested state on its own branch/PR. Rough size estimates are in working sessions with me.

### M0 – Foundations & spikes
- Repo skeleton (layout from §6), `.gitignore` (incl. `.idea/`), pre-commit, `LICENSE` (Apache-2.0) + `NOTICE`, `CONTRIBUTING.md`, README, default branch `main`.
- Django project with split settings, `*_FILE` secret loading, startup security checks, JSON logging with redaction.
- Vite/React/Tailwind/shadcn scaffold, API type generation pipeline.
- Dev compose (Postgres + app with hot reload).
- CI skeleton (lint + test).
- ~~**Spike S1 – Proton Drive CLI**~~ **done (2026-10-04):** credentials store analysed, `pass` shim + hardened spike container in `docker/proton/`, upload/info/download/trash/delete verified against a real account. Findings in `docs/proton-drive.md`. Remaining: licensing of bundling the binary.
- **Spike S2 – Blind index:** prototype tokenizer + HMAC index on ~1,000 synthetic documents; measure query latency and index size; validate German compound handling.

**Done when:** `docker compose -f deploy/compose.dev.yml up` serves a hello page and API docs; CI green; spikes documented.

### M1 – Accounts & security core (UC02, SEC02, SEC03)
- `crypto` app (HKDF, AES-GCM, streaming file encryption, key versions).
- User model, Argon2id, password policy, `createuser` command.
- Two-step login, TOTP, WebAuthn, recovery codes, forced enrollment, rate limiting/lockout, session hardening, re-auth for sensitive actions, logout-all.
- Audit log.
- Frontend: login, MFA, enrollment, minimal app shell.

**Done when:** a user can only reach the app shell after password + second factor; security tests from §9 for auth pass.

### M2 – Upload API & durable intake (UC01 steps 1–6, SEC04, REL01)
- `ScannerClient` + tokens + scopes; management UI in settings (create, show token once, revoke).
- Upload endpoint with validation, PDF sanitizing, idempotency, dedupe, encrypted intake write, transactional job creation.
- Job queue + worker process + `LISTEN/NOTIFY` + lease recovery.
- Status endpoint.
- `docs/scanner-api.md` with curl example.

**Done when:** `curl` upload returns 202, the document is in the DB with encrypted intake file, a job is queued, scanner tokens are rejected on the web API.

### M3 – Processing pipeline, storage & encrypted search (UC01 7–14, UC03 backend, SEC01, SEC07, REL02, REL03)
- Pipeline stages validate → ocr → extract → store → index (classification stubs for now).
- Storage backends (local + Proton Drive CLI), session health / `needs_reauth` handling, verification, intake cleanup, temp-file handling + sweeper.
- Encrypted content and thumbnails, blind index, BM25 query, filters, snippets.
- Failure handling, retry endpoint, processing events.
- Review of the "plaintext by design" field list.

**Done when:** an uploaded scan is OCR'd, stored in Proton Drive (or local backend in CI), searchable by a word appearing only in the scan; the DB contains no plaintext OCR text (automated check); killing the worker mid-way does not create duplicates.

### M4 – Web UI: inbox, documents, viewer, editing (UC03 UI, UC04, UC08, UC09, QR01)
- App shell with navigation (Inbox, Documents, Todos, Search, Tags, Settings).
- Inbox with counters, documents list with search + combined filters, document detail with PDF.js, metadata editing with provenance, status/important/read handling, Todos view, download, delete, reprocess.
- Taxonomy management (buckets, types, correspondents).

**Done when:** the full "scan → find → edit → mark done" loop works in the browser, E2E test green.

### M5 – Automatic analysis & tags (UC05, UC07)
- Extraction heuristics (dates, amounts, IBANs, reference numbers, sender block).
- Correspondent matching + user rules.
- Learned classifier (type, correspondent, tags) with encrypted model storage and background retraining on corrections.
- Tag aliases, normalization, fuzzy matching, suggested tags, merge UI.
- Title generation from templates and learned title patterns.

**Done when:** on the synthetic corpus, after a few corrections, type/correspondent/tag suggestions reach an agreed accuracy (target: ≥ 80 % on the held-out fixture set), and no new near-duplicate tags appear for alias'd concepts.

### M6 – Series detection (UC06)
- Series model, period labels, similarity-based assignment & proposals, inbox suggestions, series views with timeline and gap detection, manual assign/move/remove.

**Done when:** a sequence of fixture payslips/phone bills is grouped into series automatically and displayed as in the vision's example tree.

### M7 – Production readiness (UC10–UC13, SEC05–SEC09)
- Production Dockerfile, process manager, non-root/read-only hardening, healthchecks.
- Copy-paste `deploy/compose.yml`, secrets README with generation commands, `.env.example`, reverse proxy examples.
- Release workflow to GHCR (multi-arch, semver tags, SBOM), Dependabot, image scanning.
- Docs: architecture, security model & threat model, operations (backup/restore, key rotation, upgrades), scanner integration.
- Final security review pass (OWASP ASVS L2 checklist as guide) and fixes.

**Done when:** on a fresh machine, following `docs/operations.md`, `docker compose up -d` brings up a secure instance; tagging `v0.1.0` publishes `ghcr.io/primeapi/docnest:0.1.0` and `:latest`.

### Later (explicitly out of v1, architecture prepared)
S3 storage backend, e-mail import, mobile upload, webhooks, additional OCR engines, multi-user permissions, key rotation tooling UI, UI translations. (LLM-based analysis is deliberately excluded.)

---

## 11. How I (Claude) will execute this

- **One milestone at a time**, on a feature branch, ending with: tests passing locally, a short summary of what was built, and any deviations from this plan. I'll update `plan.md` when decisions change, so it stays the source of truth.
- **Test-first for security-critical code** (crypto, auth, scanner/web separation, blind index): tests are written alongside the code and must pass before moving on.
- **Everything runnable without Proton Drive** via the local storage backend, so development and CI never depend on your Proton account. The Proton adapter is tested against a real account only when you run it.
- **No real documents or secrets** in the repo; synthetic fixtures only. I will never write secrets into files that are committed.
- I run commands (tests, builds, compose) locally to verify my work and report results honestly, including failures.

### What I need from you

| When | What |
|---|---|
| Now | Log in once inside the spike container (`docs/proton-drive.md` → "One-time login") |
| M2 | Details of your scanner device (what HTTP client it uses, whether it can send headers/multipart, TLS capabilities) |
| M7 | Push access / a first release tag on `github.com/primeapi/docnest` |

### Resolved decisions

| # | Topic | Decision |
|---|---|---|
| 1 | Name | DocNest — `github.com/primeapi/docnest`, `ghcr.io/primeapi/docnest` |
| 2 | License | Apache-2.0 (free for everyone, attribution required, contributions welcome) |
| 3 | Storage | Official Proton Drive CLI; browser login done once by the operator, container reuses the session (encrypted `pass` shim, details in 2.3 and `docs/proton-drive.md`) |
| 4 | LLM | None — no remote APIs, no local models |
| 5 | UI language | English |
| 6 | Process model | One `app` container with web + worker under a process manager |
| 7 | Branch | `main` |

### Remaining open points

- Scanner device capabilities (needed for M2).
- Whether Proton's terms allow bundling the CLI binary in a public image.

---

## 12. Feature plan 2026-10

Requested on 2026-10-08. Worked on branch `feature/scan-quality-learning-storage`, one commit per feature.

### 12.1 Date format `dd.mm.yyyy`

- [x] All dates in the UI (display and the date input) use `dd.mm.yyyy` instead of `dd/mm/yyyy`; the input also accepts `/` and `-` as separators.

### 12.2 Scan enhancement (stage `enhance`)

Goal: crooked, rotated, too long scans and blank pages are fixed before OCR/Docling, while the untouched original is always kept.

Research and library choice:

| Step | Choice | Why / alternatives |
|---|---|---|
| Fine skew | `jdeskew` (MIT) — Adaptive Radial Projection on the Fourier spectrum | Best on DISE 2021 (≈0.07° mean deviation); needs only numpy + OpenCV (already installed). `deskew` (Hough) adds scikit-image; unpaper only handles small angles |
| 90/180/270° | Tesseract OSD (`--psm 0`) with a confidence threshold | `osd.traineddata` already in the image (same as OCRmyPDF `--rotate-pages`). PP-LCNet ONNX (`docorient`, PaddleOCR) would need onnxruntime + a model download — possible later |
| Paper too long / scanner background | Own OpenCV paper detection (background colour from the edges, crop to the paper) | unpaper (in the image) only paints borders white and targets b/w book pages |
| Gentle cleanup | Own: background flattening (paper → white), despeckle, mild contrast stretch | Off-the-shelf tools (unpaper, scantailor) are tuned for b/w text and damage colour letters, stamps, signatures |
| Blank pages | Existing `assemble.is_blank()`, run after cropping | Cropped borders no longer count as ink |

Decisions (from the user):
- Runs for **all** uploads; only pages that are a single raster image (scans) are touched, pages with real text/vector content stay unchanged.
- Cropped pages keep their **cropped size** (no snapping to A4).
- Blank pages are **always** removed from the enhanced version (the original keeps them). `skip_blank_pages` in the scanner API no longer removes pages from the original.
- Gentle cleanup is included.
- Everything is configurable under Settings → System (master switch + each step + its parameters), and can be overridden once per document when reprocessing from the original.
- Viewer toggle **Enhanced / Original** on the document and review page; both downloadable.
- Existing documents are never changed automatically; they can be reprocessed (single or as a **bulk action**).

Implementation:
- [x] Pipeline: `assemble → validate → enhance → ocr → analyze → store → index`. `enhance` writes `enhanced.pdf` (encrypted in the intake); OCR/Docling/VLM/thumbnail/page count use it. The archive (`archive.pdf`) is built from it, so storage keeps two files: untouched `original.pdf` and processed `archive.pdf`.
- [x] `apps/processing/enhance.py`: per page extract the embedded image at native resolution, orientation → deskew → crop → cleanup → blank check, re-encode (G4 for bilevel, JPEG 90 otherwise; untouched pages are copied as-is).
- [x] Settings stored in `SystemState` (`scan_enhancement`), API + Settings → System UI.
- [x] Reprocess "from original" with one-off overrides (stored on the job/document for that run only); bulk "reprocess" action.
- [x] Processing event with a summary ("rotated 1, deskewed 3, cropped 2, removed 1 blank page").
- [x] Tests with synthetic skewed / rotated / too long / blank scans; docs (`architecture.md`, `scanner-api.md`).

### 12.3 Physical storage locations

Goal: know where the paper original of a document lies.

Decisions (from the user):
- Separate from the digital filing folders: **storage locations** (e.g. cabinet → binder), nested.
- Action "file everything not yet placed here": takes all scanner uploads without a location, plus manual uploads marked "paper original exists"; preview list where single documents can be deselected before confirming.
- Documents are placed in scan (upload) order, **newest on top**.
- Thickness: **simplex**, one sheet per page of the original.
- Each location has a capacity in sheets (default 500 ≈ 8 cm binder) used for the fill level.
- Document page: location path + a binder pictogram with a marker at the document's height, computed from the sheets of the documents above and below it.

Implementation:
- [ ] Models `StorageLocation` (name, parent, capacity, created) and on `Document`: `storage_location`, `storage_batch`/`placed_at`, `has_paper` (default: true for scanner uploads, false for manual uploads).
- [ ] API: CRUD, "place pending" preview + confirm, position of a document, move/unplace.
- [ ] UI: locations page (tree, fill level), place-pending dialog, location card with pictogram on the document page, filter by location.
- [ ] Tests + docs.

### 12.4 Learning from corrections (exploration only)

The user wants the existing models to learn better from corrections of title, sender, labels — **no** replacement rules ("word a → word b"). If that is not feasible, nothing is built yet; only explore and propose.

- [ ] Explore and write the findings + proposal below (no implementation).
