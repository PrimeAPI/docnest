# Operations

## Requirements

- Linux host (x86-64) with Docker Engine and **Docker Compose ≥ 2.23** (for inline `configs`)
- A reverse proxy that terminates HTTPS (Caddy, Traefik, nginx, …)
- A Proton account with Proton Drive
- ~2 GB RAM (OCR of large scans needs more; see `DOCNEST_TMP_SIZE`; VLM extraction needs several GB more)
- Internet access for the first use of each Docling mode; downloaded models are retained in the `models` volume

## Install

```bash
mkdir docnest && cd docnest
curl -fsSLO https://raw.githubusercontent.com/PrimeAPI/docnest/main/deploy/compose.yml
curl -fsSL https://raw.githubusercontent.com/PrimeAPI/docnest/main/deploy/.env.example -o .env
curl -fsSLO https://raw.githubusercontent.com/PrimeAPI/docnest/main/deploy/init-secrets.sh && chmod +x init-secrets.sh
```

1. Edit `.env` and set `DOCNEST_BASE_URL` to the public HTTPS URL.
2. `./init-secrets.sh` — creates `secrets/master_key`, `django_secret_key`, `db_password`, `db_admin_password` and hands them to the container user (UID 10001; uses `sudo`).
   **Copy `secrets/master_key` to a safe offline place now** (password manager). Without it, the encrypted data cannot be recovered.
3. `docker compose up -d` — first start runs the database migrations.
4. `docker compose exec app docnest createuser <name>` — prints a generated password (shown once). Sign in at your URL, enroll a passkey/security key or authenticator app, and change the password under Settings → Security if you like.
5. `docker compose exec app docnest proton-login` — prints a sign-in link; open it on any device, sign in to Proton and keep the terminal open until it reports *Authentication successful*. See [proton-drive.md](proton-drive.md).
6. In the UI: **Settings → Scanners → Add scanner** and configure your scanner ([scanner-api.md](scanner-api.md)).

Check **Settings → System**: storage should show *Connected* and the worker *Running*.

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `DOCNEST_BASE_URL` | – (required) | Public URL, e.g. `https://docs.example.com`. Used for allowed hosts, CSRF and passkeys. |
| `DOCNEST_VERSION` | `latest` | Image tag: a release (`0.1.0`, `latest`) or a preview (`edge` = newest `main`, `<branch>`, `sha-<commit>`). |
| `DOCNEST_BIND` / `DOCNEST_PORT` | `127.0.0.1` / `8000` | Where the container port is published. |
| `DOCNEST_STORAGE_BACKEND` | `proton` | `proton`, or `local` (files stay in the `data` volume — testing only). |
| `DOCNEST_PROTON_ROOT` | `/my-files/DocNest` | Folder in Proton Drive. |
| `DOCNEST_OCR_BACKEND` | `ocrmypdf` | Initial default processor for new documents: `ocrmypdf` or `docling`. It can be changed under Settings → System; either backend can also be selected when reprocessing one document. |
| `DOCNEST_OCR_LANGUAGES` | `deu+eng` | Tesseract languages used by OCRmyPDF and Docling (the image contains `deu` and `eng`). |
| `DOCNEST_DOCLING_THREADS` | `2` | CPU threads used by Docling. Set to `1` on a very small server. |
| `DOCNEST_DOCLING_FIELD_DETECTION` | `layout` | Initial sender/title detector: `layout`, `vlm`, or `hybrid`. It can be changed under Settings → System. |
| `DOCNEST_OLLAMA_URL` | `http://ollama:11434` | Ollama server for AI analysis (see below). Empty switches the feature off. |
| `DOCNEST_AI_MODEL` | *(empty)* | Initial AI model, e.g. `qwen3-vl:4b-instruct`. Normally chosen under Settings → System instead. |
| `DOCNEST_PROCESSING_CONCURRENCY` | `1` | Initial maximum number of documents processed at once (1–8). It can be changed live under Settings → System. Keep this low for Docling because each parallel job uses additional CPU and memory. |
| `DOCNEST_TIME_ZONE` | `Europe/Berlin` | |
| `DOCNEST_VIEW_CACHE_MB` | `0` | Encrypted cache of recently viewed PDFs (faster viewing; 0 = off). |
| `DOCNEST_BACKUP_INTERVAL_HOURS` | `24` | Hours between automatic database backups to Proton Drive (0 = off). See [Backup and restore](#backup-and-restore). |
| `DOCNEST_BACKUP_KEEP` | `14` | Number of database backups kept in Proton Drive; older ones are deleted. |
| `DOCNEST_TMP_SIZE` | `2g` | RAM-backed work area for OCR and downloads. |
| `DOCNEST_MAX_UPLOAD_MB` | `100` | Maximum size of one uploaded file (a PDF or one page image). |
| `DOCNEST_MAX_SCAN_MB` | `2000` | Maximum size of all files of one scanner upload together (several page images or a [scan session](scanner-api.md#upload-page-by-page--scan-sessions)). |

Advanced variables (set under `environment:` in `compose.yml` if needed): `DOCNEST_SESSION_IDLE_TIMEOUT_MINUTES` (10080 = one week without use), `DOCNEST_SESSION_ABSOLUTE_TIMEOUT_MINUTES` (50400 = five weeks after signing in), `DOCNEST_LOGIN_MAX_FAILURES` (5), `DOCNEST_LOGIN_LOCKOUT_SECONDS` (900), `DOCNEST_OCR_JOBS` (2), `DOCNEST_OCR_TIMEOUT_SECONDS` (900; OCRmyPDF only—Docling has no wall-clock timeout), `DOCNEST_DOCLING_DEVICE` (`cpu`), `DOCNEST_DOCLING_ARTIFACTS_PATH` (`/var/lib/docnest/models`), `DOCNEST_JOB_LEASE_SECONDS` (1800; renewed automatically while a job runs), `DOCNEST_JOB_HISTORY_HOURS` (24; completed queue entries are transient), `DOCNEST_WORKER_STOP_GRACE_PERIOD` (`24h`), `DOCNEST_MAX_PAGES` (500), `DOCNEST_SCAN_SESSION_HOURS` (24; scan sessions expire this long after their last page), `DOCNEST_MAX_OPEN_SCAN_SESSIONS` (10 per scanner token), `DOCNEST_WEB_WORKERS` (2), `DOCNEST_LOG_LEVEL` (INFO), `DOCNEST_AUDIT_RETENTION_DAYS` (365), `DOCNEST_BACKUP_TIMEOUT_SECONDS` (3600; for `pg_dump`), `DOCNEST_AI_TIMEOUT_SECONDS` (3600; per document), `DOCNEST_AI_PAGES` (1; page images the AI model looks at), `DOCNEST_AI_IMAGE_SIZE` (1280; pixels along the longer side), `DOCNEST_AI_TEXT_CHARS` (3000; OCR text from the start of the document), `DOCNEST_AI_CONTEXT_TOKENS` (4096; the input is about 2700 tokens).

Docling extracts reading order, headings, tables, Markdown, and its lossless JSON document model. Both outputs and the line geometry used for field detection are encrypted in PostgreSQL; the JSON is available from the authenticated document structure API. Layout detection is fast and deterministic. VLM detection always runs the local NuExtract model on page one, while hybrid detection invokes it only when layout confidence is low. The model needs several GB of free memory. VLM work has no wall-clock timeout and falls back to layout detection if it fails. Docling does not generate a searchable PDF/A, so its archive file is the sanitized original. OCRmyPDF remains the choice when a searchable PDF/A is required.

Models are not part of the application image. The worker downloads the layout/table models compatible with the installed Docling version on the first Docling document and the revision-pinned VLM model only when VLM or hybrid fallback needs it. A filesystem lock prevents duplicate downloads, interrupted downloads resume, and the named `models` volume keeps the cache across container upgrades. To prefetch everything before enabling Docling, run:

```bash
docker compose exec app docnest models
```

Use `docnest models --layout-only` to fetch only the standard models or `docnest models --force` to repair/refresh the pinned cache. The cache can be deleted and recreated; it contains no documents and does not need to be backed up.

## AI analysis

Rules and the small Docling model misjudge many real letters: they take the recipient's town for the sender, pick the payroll service's return address over the employer's letterhead, and the classifier copies senders from documents with similar words. With AI analysis switched on, a vision-language model running on your own server looks at the first pages and the OCR text of each document and names the sender, a title, the document date and the type. Its sender wins over the classifier and over names merely mentioned in the text; fields you set yourself are never changed.

1. Start the bundled Ollama container: `docker compose --profile ai up -d`
2. Under Settings → System → *AI analysis*, download a model and select it. On a server with 8 GB RAM take `qwen3-vl:4b-instruct` (3.6 GB in memory); `qwen3-vl:8b-instruct` reads a little better but needs 6.2 GB, and a model that does not fit next to DocNest makes the server swap, which slows every document down to many minutes.
3. To have existing documents read again, select them in the document list and choose *Reprocess*.

Processing is split in two so that a slow model never delays reading: as soon as a scan is assembled, validated and enhanced (seconds), the enhanced PDF and its thumbnail are available. OCR, AI analysis, storage and indexing then run in a separate worker lane, one document after another; new scans keep becoming readable meanwhile. The model looks at the first page (1280 pixels) and the first 3000 characters of the OCR text. Measured on 8 CPU cores: about 40 seconds per document with the 4B model and 70 seconds with the 8B model, plus text recognition; slower server CPUs take up to about twice as long; an NVIDIA GPU (see the commented `deploy` block in `compose.yml`) brings that down to seconds. Prefer `-instruct` models: reasoning ("thinking") variants deliberate for minutes before answering.

If Ollama is unreachable or the chosen model is not installed, documents wait (shown as *Waiting for the AI model*) and are retried every ten minutes instead of falling back to guesses; switch AI analysis off to finish them with the rules. If the model answers with something unusable, the document is analysed with the rules and the history notes it.

Document images and text go only to `DOCNEST_OLLAMA_URL` — by default the Ollama container on the same host, which publishes no ports. Pointing it at another machine sends document contents there over plain HTTP; only do that inside a network you trust.

## Queue and statistics

The processing queue (bottom left) shows running, waiting and recent jobs. Waiting jobs can be cancelled one by one or all at once; a running job finishes its current step. A cancelled reanalysis leaves the document as it was; a cancelled new scan is marked *Cancelled* and can be finished later with *Reprocess*.

Settings → Statistics shows how many documents, pages and senders the archive holds, new documents per month and per week, the share of each document type, sender and scanner, and how long processing takes: the median time from upload until a scan is readable and until it is fully processed (queue waiting included), the average per step, and the AI model's average time.

## Reverse proxy

DocNest must be served over HTTPS on its own (sub)domain. Forward `X-Forwarded-Proto`, allow uploads up to `DOCNEST_MAX_UPLOAD_MB` (or more if scanners send many page images in one request — scan sessions only need one page per request), and allow long requests (OCR'd documents are fetched from Proton Drive when viewed).

**Caddy**

```caddy
docs.example.com {
    request_body {
        max_size 110MB
    }
    reverse_proxy 127.0.0.1:8000 {
        transport http {
            read_timeout 300s
        }
    }
}
```

**nginx**

```nginx
server {
    listen 443 ssl http2;
    server_name docs.example.com;
    # ssl_certificate …; ssl_certificate_key …;
    client_max_body_size 110m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;
        proxy_request_buffering off;
    }
}
```

**Traefik** (labels on the `app` service)

```yaml
labels:
  - traefik.enable=true
  - traefik.http.routers.docnest.rule=Host(`docs.example.com`)
  - traefik.http.routers.docnest.entrypoints=websecure
  - traefik.http.routers.docnest.tls.certresolver=letsencrypt
  - traefik.http.services.docnest.loadbalancer.server.port=8000
```

## Day-to-day commands

```bash
docker compose exec app docnest createuser <name>       # new user with a generated password —
                                                        # or, if the user exists: new generated password
                                                        # (signs out all sessions, keeps second factors)
docker compose exec app docnest resetmfa <name>         # lost all second factors (ends all sessions)
docker compose exec app docnest proton-login            # renew the Proton session
docker compose exec app docnest proton fs list /my-files/DocNest
docker compose exec app docnest backup                  # back up the database to Proton Drive now
docker compose logs -f app
```

## Backup and restore

What to back up:

| What | Why | How |
|---|---|---|
| `secrets/master_key` (+ other secrets) | Decrypts the database contents and the Proton session | Once, offline (password manager). Keep it **separate** from database backups. |
| PostgreSQL database | Metadata, encrypted text, search index, users | **Automatic**: the worker uploads a dump to Proton Drive every day (see below). Manually: `docker compose exec -T db pg_dump -U postgres -Fc docnest > docnest-$(date +%F).dump` |
| `data` volume | Documents not yet stored in Proton Drive (normally empty) | Only needed if the overview shows documents *waiting for storage*. |
| Proton Drive | The documents themselves | Managed by Proton; DocNest never deletes there except when you delete a document. |

### Automatic database backups

Every `DOCNEST_BACKUP_INTERVAL_HOURS` (default 24) the worker runs `pg_dump` and uploads the dump to `<DOCNEST_PROTON_ROOT>/backups/docnest-<UTC time>.dump` (e.g. `/my-files/DocNest/backups/docnest-2026-10-08T031500Z.dump`). The first backup runs shortly after the worker starts. The dump is PostgreSQL's compressed custom format; DocNest checks that it is readable before uploading and verifies size and SHA-1 after the upload. The plaintext dump only exists in the RAM-backed `/tmp` while it is uploaded.

Only the newest `DOCNEST_BACKUP_KEEP` (default 14) backups are kept; DocNest deletes older ones it uploaded itself. Files you put into the folder by hand are never touched.

**Settings → System** shows the last backup and has a *Back up now* button; a failed backup is also shown in the warning banner. A failed backup is retried with backoff and, after that, at most once an hour. While Proton Drive needs a new login, backups wait like document uploads.

The dump contains the same data as the database: document text, titles and other sensitive values stay encrypted with keys derived from the master key (see [security.md](security.md)), and Proton Drive encrypts the file end-to-end. The master key is **not** in the backup — keep your offline copy.

### Restore

Download the dump you want from Proton Drive (web or desktop app), then:

```bash
docker compose up -d db
docker compose exec -T db pg_restore -U postgres -d docnest --clean --if-exists < docnest-2026-10-04.dump
docker compose up -d
```

Use the same `secrets/master_key` as the backup — a different key cannot decrypt the data.

The list of backups to prune is stored in the database itself, so after restoring an older dump DocNest no longer knows about backups uploaded after it. Delete those by hand in Proton Drive if you want to clean them up.

## Upgrades

```bash
docker compose pull
docker compose up -d        # migrations run automatically on start
```

Read the release notes first; pin `DOCNEST_VERSION` if you want to control upgrades. Settings → System → *Version* shows the running version, the commit it was built from (linked to GitHub) and the build time, so you can check that an upgrade took effect.

To try an unreleased version, set `DOCNEST_VERSION=edge` (newest tested `main`) or `DOCNEST_VERSION=sha-<commit>` and run the two commands above. Switch back to a release tag the same way. Migrations only move forward, so back up the database before trying previews on real data.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Banner “Proton Drive needs a new login” | Run `docker compose exec app docnest proton-login`. Uploads keep being accepted and stored encrypted in the meantime; they are moved to Proton Drive automatically afterwards. |
| “The processing worker is not running” | `docker compose logs app` — look for errors from the `worker` process. |
| Document shows *Processing failed* | Open it → History tab shows the stage and error. Use *Reprocess*. The original file is never lost. |
| Upload returns 413 | Raise `DOCNEST_MAX_UPLOAD_MB` and your proxy's body size limit. |
| Forgot the password | `docker compose exec app docnest createuser <name>` generates a new one. |
| Login says “Too many attempts” | Wait 15 minutes (lockout) or check the security log for attacks. |
| Passkeys don't work | The browser URL must exactly match `DOCNEST_BASE_URL` (same host, HTTPS). |
| `Missing required secret …` at start | The file in `secrets/` is missing, empty or not readable by UID 10001 — run `./init-secrets.sh` again. |
| `… is world-accessible` at start | `chmod 400 secrets/<file>` (`db_password`: `chmod 440`). |
