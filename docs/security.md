# Security model

DocNest assumes every document is sensitive (contracts, tax notices, bank statements, health letters). This page describes what is protected, how, and where the limits are.

## Threat model

| Attacker has… | Protected? |
|---|---|
| A copy of the database or a DB backup | **Yes.** Document text, titles, extracted values (amounts, IBANs, reference numbers), original file names, scanner metadata and thumbnails are encrypted. The search index contains only keyed hashes. |
| The `data` volume (intake) or `proton` volume | **Yes.** Intake files and the Proton session are encrypted with keys derived from the master key, which is not stored in either volume. |
| A stolen scanner / its token | **Limited.** The token can upload documents and read the processing status of its *own* uploads. It cannot list, search, read or download anything. Revoke it in Settings → Scanners. |
| A user's password | **No access** without the second factor. |
| The running container (code execution as the app user) | **No.** The app must be able to read documents to work. Keep the host and image updated. |
| Database **and** master key | **No.** Keep the master key separate from backups. |

## Cryptography

- **Master key** — a Docker secret (`secrets/master_key`, ≥ 32 random bytes). Never stored in the database, image or logs.
- **Sub-keys** — derived with HKDF-SHA256 per purpose (`content`, `files`, `index`, `tokens`, `totp`), with a key version for future rotation.
- **Values** — AES-256-GCM with random nonces. The associated data binds each ciphertext to its document and field (`title:<uuid>`), so values cannot be swapped between rows.
- **Files** — chunked AES-256-GCM (1 MiB chunks) with a per-file key (HKDF with a random salt); chunk index and a final-chunk flag are authenticated, so truncation and reordering are detected.
- **Dedupe** — uploads are compared by an HMAC of the file, not a plain SHA-256.

## Searchable without readable text (SEC01)

Each word is normalized (case folding, umlauts `ä→ae`), stemmed (German and English Snowball) and expanded into prefixes (3–15 characters) and compound-word tails. Every variant is stored only as `HMAC-SHA256(index_key, kind ‖ term)` truncated to 64 bits, together with the term frequency. A query is hashed the same way; ranking is a BM25-style score computed in PostgreSQL. Result snippets are created in memory from the decrypted text of the current result page only.

**Known leakage:** someone with only the database sees how many (hashed) terms each document has and which documents share terms. They cannot read the terms. This trade-off is what makes fast server-side search possible.

The classifier that learns from your corrections uses the same hashed terms as features, so the trained model contains no plaintext either.

## Authentication (SEC02, SEC03)

- Passwords are hashed with **Argon2id**; policy: ≥ 12 characters, three character classes or a 20+ character passphrase, common and numeric passwords rejected.
- A **second factor is mandatory**: passkeys / security keys (WebAuthn, phishing-resistant, recommended) or TOTP. SMS is not supported. Accounts without a second factor can only enroll one.
- **Recovery codes** (10, single use, stored as keyed hashes).
- **Brute-force protection** — per account and per IP, persistent in the database; lockout after repeated failures (default 5 per account / 20 per IP, 15 minutes). TOTP codes cannot be replayed.
- **Sessions** — server-side, `__Host-` cookie, `Secure`, `HttpOnly`, `SameSite=Strict`; a signed-in browser stays signed in while it is used (the cookie survives closing the browser) and is signed out after one week without use or five weeks after signing in, whichever comes first — both enforced server-side; sensitive actions (password, second factors, recovery codes) ask for the password again regardless; the session ID is rotated at login; logout and “sign out other sessions” invalidate server-side. Changing the password ends all other sessions.
- **Re-authentication** — creating scanner tokens, managing second factors and similar actions require the password again if the last sign-in is older than 10 minutes.
- **CSRF** protection on every state-changing request.

## Scanner separation (SEC04)

The upload API (`/api/upload/v1/`) and the web API (`/api/v1/`) use different authentication classes: scanner bearer tokens are never accepted on the web API, and session cookies are never accepted on the upload API. Tokens are 256-bit random secrets stored as SHA-256 hashes, scoped (`upload`, `upload:status`), revocable, optionally IP-restricted, and failed token attempts are rate-limited per IP.

## Least privilege (SEC05)

- The container runs as UID 10001 with a read-only root filesystem, no Linux capabilities and `no-new-privileges`.
- PostgreSQL is reachable only on an internal Docker network.
- The optional Ollama container (AI analysis) publishes no ports; document page images and text are sent to it only over the Docker network, and it keeps nothing but the downloaded models. A remote `DOCNEST_OLLAMA_URL` would receive document contents unencrypted — keep it local.
- The application database user is not a superuser.
- Interactive API docs are disabled in production.

## Transport (SEC06)

DocNest expects TLS termination at your reverse proxy and refuses `http://` base URLs except for `localhost`. Cookies are `Secure`; HSTS is sent in production. Proton Drive traffic uses HTTPS (and Proton's end-to-end encryption).

## Temporary files (SEC07)

Uploaded files go straight to the encrypted intake. Plaintext copies needed for OCR or for viewing/downloading exist only in per-job directories under `/tmp`, which is a RAM-backed `tmpfs` with mode 0700, and are deleted when the job or response ends. A sweeper removes leftovers after crashes.

## Logs (SEC08)

Logs are structured JSON. Document text is never passed to the logger (enforced by tests that run the full pipeline and search the captured logs for document words). A redaction filter additionally masks anything that looks like a password, token, cookie or key. Gunicorn access logs are disabled because URLs contain search queries. The Proton CLI log level is `WARNING`.

## Secure defaults (SEC09)

No default users or passwords; secrets only via files (`*_FILE`), and startup fails if a secret file is world-readable or too short; debug mode cannot be enabled in production; strict Content-Security-Policy (`default-src 'self'`, no inline scripts), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` for API responses; uploaded PDFs are sanitized (JavaScript, auto-actions, embedded files and launch actions removed) and served with a sandboxing CSP.

## Audit log and sign-in activity

Sign-ins (successful and failed, with IP, device and method), second-factor and password changes, scanner token changes, uploads, opened and downloaded documents, edits and deletions are recorded. Every action is linked to the sign-in session it happened in.

- **Settings → Security → Sign-in activity** lists your sign-ins and failed attempts; expand a session to see what was done in it. Active sessions are marked and can be signed out below.
- **After every sign-in** DocNest shows when and from where you last signed in, and warns about failed attempts since then.
- **Settings → System → Security log** shows the raw log for all actors (users, scanners, system).

IP addresses are shown as received from the reverse proxy; no external geolocation service is used. Entries are kept for `DOCNEST_AUDIT_RETENTION_DAYS` (default 365).
