# DocNest

[![Build](https://github.com/PrimeAPI/docnest/actions/workflows/preview.yml/badge.svg)](https://github.com/PrimeAPI/docnest/actions/workflows/preview.yml)
[![Security](https://github.com/PrimeAPI/docnest/actions/workflows/security.yml/badge.svg)](https://github.com/PrimeAPI/docnest/actions/workflows/security.yml)

**A self-hosted, security-first document archive for scanned mail.** A scanner uploads PDFs; DocNest runs OCR, recognizes sender, date, type and tags, groups recurring documents into series, stores the files in **Proton Drive** and makes everything searchable — without ever keeping readable document text in its database.

- **Inbox workflow** — new, unread, todo and important documents at a glance; one click to mark done.
- **Upload from anywhere** — scanners use the upload API; in the browser just drag & drop PDFs onto DocNest.
- **Full-text search over scans** — finds words that exist only as pixels in a scanned letter, with prefix and German compound-word matching (`versicherung` finds `Fahrzeugversicherung`).
- **Automatic analysis, no AI service** — rules, heuristics and a small classifier that learns from your corrections. Nothing leaves your server.
- **Series** — monthly payslips, phone bills or bank statements are grouped and labelled (“March 2026”), with gaps highlighted.
- **Human override** — every automatic value can be corrected; your edits are never overwritten.
- **Proton Drive storage** — documents are stored end-to-end encrypted in your Proton Drive; DocNest holds files only temporarily.

## Security at a glance

| | |
|---|---|
| Database contents | OCR text, titles, extracted data and thumbnails are AES-256-GCM encrypted. Search uses a *blind index* (keyed hashes of words), so a database dump contains no readable text. |
| Login | Argon2id passwords + **mandatory** second factor (passkeys / security keys or TOTP), recovery codes, rate limiting, short-lived hardened sessions. |
| Scanners | Separate, scoped upload tokens. A scanner can upload — never read, search or download. |
| Files at rest | Uploads wait encrypted until Proton Drive has verifiably stored them; plaintext work files live only in memory (tmpfs). |
| Container | Non-root, read-only filesystem, all capabilities dropped, database on an internal network. |

Details and limits: [docs/security.md](docs/security.md).

## Quick start

Requirements: Docker with Compose, a reverse proxy with HTTPS, a Proton account.

```bash
mkdir docnest && cd docnest
curl -fsSLO https://raw.githubusercontent.com/PrimeAPI/docnest/main/deploy/compose.yml
curl -fsSL https://raw.githubusercontent.com/PrimeAPI/docnest/main/deploy/.env.example -o .env
curl -fsSLO https://raw.githubusercontent.com/PrimeAPI/docnest/main/deploy/init-secrets.sh && chmod +x init-secrets.sh

nano .env                       # set DOCNEST_BASE_URL=https://docs.your-domain.tld
./init-secrets.sh               # creates ./secrets — back up secrets/master_key offline!
docker compose up -d

docker compose exec app docnest createuser alice      # prints a generated password; sign in and set up your second factor
docker compose exec app docnest proton-login          # one-time Proton Drive sign-in (open the printed link)
```

Point your reverse proxy at `127.0.0.1:8000` (examples in [docs/operations.md](docs/operations.md)).

### Image tags

| Tag | Built from | Use for |
|---|---|---|
| `latest`, `X.Y.Z`, `X.Y` | release tags `vX.Y.Z` | production |
| `edge` | every push to `main` | trying the newest version |
| `<branch>`, `sha-<commit>` | every push to any branch | testing a specific change |

Set `DOCNEST_VERSION` in `.env` accordingly, then `docker compose pull && docker compose up -d`.

### Connect a scanner

In the web UI go to **Settings → Scanners → Add scanner** and copy the token. Then:

```bash
curl -X POST https://docs.your-domain.tld/api/upload/v1/documents \
  -H "Authorization: Bearer dn_scan_…" \
  -H "Idempotency-Key: $(uuidgen)" \
  -F file=@scan.pdf -F bucket=private -F document_type=auto -F todo=true
```

Full reference: [docs/scanner-api.md](docs/scanner-api.md).

## Documentation

- [Operations](docs/operations.md) — install, reverse proxy, backup/restore, upgrades, troubleshooting
- [Security model](docs/security.md)
- [Scanner upload API](docs/scanner-api.md)
- [Proton Drive integration](docs/proton-drive.md)
- [Architecture](docs/architecture.md)
- [Development](docs/development.md)

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md). Please report security issues privately ([SECURITY.md](SECURITY.md)).

## License

[Apache License 2.0](LICENSE) — free to use, modify and distribute, including commercially. Redistributions must keep the copyright and [NOTICE](NOTICE).
