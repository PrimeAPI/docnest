# DocNest

[![Build](https://github.com/PrimeAPI/docnest/actions/workflows/preview.yml/badge.svg)](https://github.com/PrimeAPI/docnest/actions/workflows/preview.yml)
[![Security](https://github.com/PrimeAPI/docnest/actions/workflows/security.yml/badge.svg)](https://github.com/PrimeAPI/docnest/actions/workflows/security.yml)

**A self-hosted, security-first document archive for scanned mail.** A scanner uploads PDFs or raw page images; DocNest runs selectable OCRmyPDF or Docling processing, recognizes sender, date, type and tags, groups recurring documents into series, stores the files in **Proton Drive** and makes everything searchable — without ever keeping readable document text in its database.

- **Inbox workflow** — new, unread, todo and important documents at a glance; one click to mark done.
- **Review mode** — go through the inbox one document at a time: PDF and OCR text side by side, select text to fill title, sender, date or tags, then “Save & next”.
- **Upload from anywhere** — scanners use the upload API and may send raw page images (PNG, JPEG, TIFF, PNM, …) page by page; DocNest builds the PDF itself, so a Raspberry Pi next to a USB scanner needs no processing at all. In the browser just drag & drop PDFs onto DocNest.
- **Full-text search over scans** — finds words that exist only as pixels in a scanned letter, with prefix and German compound-word matching (`versicherung` finds `Fahrzeugversicherung`).
- **Automatic analysis, no AI service** — rules, heuristics and a small classifier that learns from your corrections. Nothing leaves your server.
- **Upload by email** — forward an email to a mailbox set up for DocNest: each email becomes one document — its attachments first, then the email itself, which also gives the AI model context. Only senders on your allowlist are imported.
- **Assistant** — on a document, a folder or a selection: ask the AI model for *consistent names* (“Abrechnung”, “Verdienstabrechnung 25” and “Gehaltsabrechnung 03/26” become “Verdienstabrechnung 2025-01” …, or your own pattern like `Verdienstabrechnung YYYY/MM`), *filing suggestions*, or something *custom* (“die Stadtwerke-Rechnungen bekommen das Tag Energie”). It only proposes; you tick, edit and apply.
- **Overnight document review** — review a selection, folder or the archive, now or at a scheduled time. Choose an installed local model and a deadline, or run page checks without AI. Find duplicate scans and pages, split letters, missing pages and gaps; inspect the evidence, compare pages, edit the proposed changes and apply only what you choose. [How reviews work](docs/document-review.md).
- **Edit pages and keep originals** — blank pages are hidden in Enhanced, not Original. Hiding or reordering pages keeps the same document without OCR or AI reprocessing; splitting keeps the first part's identity. Only extra parts and merged documents are new, reusing existing processed pages. Encrypted history and undo retain provenance; originals are never overwritten or permanently deleted. [Operation rules](docs/document-operations.md).
- **Three views** — every document list switches between *List* (a table), *Compact* (rows with a small preview) and *Cards* (big previews); dialogs that name documents have the same switch, starting as a list. The Filing page has a quick search (title, sender, text) over the folder it shows.
- **Filing suggestions** — select the loose documents of a folder and DocNest proposes subfolders for them (`Work` → `Work/Verdienstabrechnungen/2026`), learning what each folder means from what is already in it. Add instructions (“Stadtwerke nach Wohnung/Nebenkosten”, “English folder names”) and ask again; move single documents between the suggested folders; nothing moves until you apply.
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
  -F file=@scan.pdf -F bucket=Private/Taxes -F document_type=auto -F todo=true
```

`bucket` is the folder path the document is filed in (see **Filing** in the web UI); missing folders are created automatically.

Raw scanner pages work too — DocNest turns them into the PDF:

```bash
curl -X POST https://docs.your-domain.tld/api/upload/v1/documents \
  -H "Authorization: Bearer dn_scan_…" \
  -F file=@page-1.png -F file=@page-2.png -F bucket=Private -F dpi=300
```

For long feeder scans, upload page by page with a scan session. Full reference, including a Raspberry Pi scan station script: [docs/scanner-api.md](docs/scanner-api.md).

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
