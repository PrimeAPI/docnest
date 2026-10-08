# Scanner upload API

Scanners, scripts and small devices (for example a Raspberry Pi attached to a USB scanner) upload documents through a dedicated API. It is separate from the web API: a scanner token can **only** upload documents and read the processing status of its own uploads. It can never read, search or download anything.

**The device does no processing.** It sends whatever the scanner produced — page images (PNG, JPEG, TIFF, PNM, …), PDFs, or a mix — and DocNest turns it into the PDF itself (see [What DocNest does with your files](#what-docnest-does-with-your-files)). That PDF becomes the document's *original*; OCR, Docling, field detection and storage all run on it afterwards, exactly as for an uploaded PDF.

- [Overview](#overview)
- [Authentication](#authentication)
- [Accepted files](#accepted-files)
- [What DocNest does with your files](#what-docnest-does-with-your-files)
- [Common fields](#common-fields)
- [Upload in one request — `POST /documents`](#upload-in-one-request)
- [Upload page by page — scan sessions](#upload-page-by-page--scan-sessions)
- [Processing status](#processing-status)
- [Connectivity check](#connectivity-check)
- [Errors](#errors)
- [Limits and server settings](#limits-and-server-settings)
- [Raspberry Pi scan station](#raspberry-pi-scan-station)
- [Recommendations for devices](#recommendations-for-devices)

## Overview

All paths are relative to `https://<your-docnest>/api/upload/v1`.

| Method & path | Scope | Purpose |
|---|---|---|
| `POST /documents` | `upload` | Upload one document in a single request: a PDF, one or more page images, or several files. |
| `POST /scans` | `upload` | Open a scan session to upload a long document page by page. |
| `POST /scans/{id}/pages` | `upload` | Upload (or replace) one page of a session. |
| `GET /scans/{id}` | `upload` | Which pages a session has received. |
| `POST /scans/{id}/complete` | `upload` | Finish the session; DocNest builds the document. |
| `DELETE /scans/{id}` | `upload` | Abandon a session and delete its pages. |
| `GET /documents/{id}` | `upload:status` | Processing status of a document this token uploaded. |
| `GET /ping` | – | Check token and connectivity. |

**Which upload style?**

- Use **`POST /documents`** when the whole scan is available at once and reasonably small (a letter, a few pages). One request, done.
- Use a **scan session** for long feeder scans: each page is its own small request that can be retried on its own, upload can start while the scanner is still feeding, and a flaky Wi-Fi connection never forces you to resend the whole stack.

Both produce exactly the same document.

## Authentication

Create a token per device in **Settings → Scanners → Add scanner**. The token (`dn_scan_<id>_<secret>`) is shown once; DocNest stores only a hash. You can restrict a token to IP addresses/networks, rotate it (*New token*) or revoke it at any time. *Allow status* adds the `upload:status` scope.

Send it as a bearer token on every request:

```
Authorization: Bearer dn_scan_0123456789ab_…
```

Web sessions/cookies are never accepted on this API, and scanner tokens are never accepted on the web API.

## Accepted files

The type is detected from the file **content** — file name and `Content-Type` are ignored, so `scan.bin` with PNG data is fine.

| Format | Typical source | Notes |
|---|---|---|
| PDF | scanners with built-in PDF output, `scanimage --format=pdf` | Several pages allowed. Encrypted PDFs are rejected. |
| PNG | `scanimage --format=png`, most scan apps | 1-bit, 8-bit, 16-bit, grey, colour, palette, with or without alpha. |
| JPEG | `scanimage --format=jpeg`, network scanners | Embedded **unchanged** (no re-compression). EXIF rotation is honoured. |
| TIFF | `scanimage --format=tiff`, `scanadf` | **Multi-page TIFFs** become several pages. Any compression Pillow reads (G4, LZW, JPEG, …). |
| PNM / PBM / PGM / PPM | `scanimage` default output (`--format=pnm`), `scanadf` | Raw scanner output. Carries **no resolution** — send `dpi`. 16-bit is reduced to 8-bit. |
| BMP, GIF, WebP | other tools | Supported for completeness. |

Anything else is rejected with `415`.

## What DocNest does with your files

These are the steps scan-to-PDF software normally runs on the scanning computer. DocNest runs them on the server, in the worker's first processing stage (**assemble**, shown as *Building PDF* in the processing queue) — before validation, OCR or Docling see the document:

1. **Order** — files are used in the order sent (`POST /documents`) or by page number (scan sessions). PDF pages and multi-page TIFF frames are inserted at their file's position.
2. **Decode** every page image.
3. **Rotate** according to the EXIF orientation tag, if present.
4. **Normalize colour**: transparency is flattened onto white paper, palettes are expanded (pure-grey palettes stay greyscale), 16-bit and floating-point greyscale become 8-bit, CMYK/Lab/YCbCr become RGB (except JPEGs, which are kept as they are).
5. **Compress**:
   - JPEG input → embedded byte-for-byte (no generation loss).
   - Black-and-white (1-bit) pages → CCITT Group 4, like fax/office scanners.
   - Grey and colour pages → JPEG quality 90 (`compression=auto`, default) or lossless Flate/PNG (`compression=lossless`, much larger files).
6. **Size each page** from its resolution: 2480 × 3508 px at 300 dpi becomes an A4 page. The resolution comes from `dpi` if you send it, otherwise from the file (PNG `pHYs`, TIFF, JPEG JFIF/EXIF), otherwise 300 dpi.
7. **Join** everything into one PDF. This PDF is stored as the document's **original** in Proton Drive; the raw uploaded files are deleted once it has been built.

The usual pipeline then follows: validate & sanitize → **scan enhancement** → OCR (OCRmyPDF, searchable PDF/A archive) or Docling → field detection → storage → search index. So **don't deskew, crop, OCR or compress on the device** — send the scanner's raw output.

**Scan enhancement** (configurable under Settings → System, and once per document when reprocessing) produces the version shown in the UI; the original stays untouched and can always be viewed and downloaded. It applies to every upload, but only to pages that are a scanned image:

- **Upright pages** — sideways / upside-down pages are turned using Tesseract's orientation detection (only above a confidence threshold).
- **Straight pages** — skew is measured with jdeskew and corrected.
- **Crop to the paper** — when the feeder scanned more than the sheet, the scanner backing beyond the paper edge (and the edge shadow) is cut off. Uniform, neutral bands only: a coloured letterhead band or anything with text on it stays.
- **Cleanup** — paper whitening, a mild contrast stretch and removal of isolated specks.
- **Blank pages** — a page counts as blank when less than 0.01 % of its area (outer 2 % of each edge ignored) is clearly darker than the paper; checked after cropping. A short line such as a page number line is enough to keep a page; dust and light bleed-through are not. If *every* page is blank, all pages are kept.

A single uploaded PDF skips the assemble stage and is used as-is.

Until assembly, the uploaded files are kept **encrypted** in the intake volume like every other upload; plaintext copies exist only in the RAM-backed work area while a page is being converted.

## Common fields

`POST /documents` and `POST /scans` take the same `multipart/form-data` fields (scan sessions take them when the session is opened):

| Field | Required | Default | Description |
|---|---|---|---|
| `bucket` | no | – | Folder to file the document in, as a path: `Private`, `Private/Taxes/2024`. Matched case-insensitively against the folders under *Filing*; **missing folders are created**. Empty → the document stays unfiled. Max 10 levels. |
| `document_type` | no | `auto` | Type slug/name (`mail`, `contract`, `invoice`, `notice`, `statement`, `other`) or `auto` to let DocNest decide. |
| `todo` | no | `false` | `true` → the document starts with status *Todo*. |
| `important` | no | `false` | `true` → marked important. |
| `tags` | no | – | Comma-separated tag names, e.g. `Tax,2026`. Existing tags and aliases are reused. Max 20. |
| `metadata` | no | – | JSON object with any extra data, e.g. `{"device":"pi-scanner","duplex":true}` (stored encrypted, max 8 KB). |
| `dpi` | no | from file, else 300 | Scan resolution (50–2400). **Overrides** the resolution stored in the images. Send it whenever you upload PNM files or know the scan resolution. |
| `skip_blank_pages` | no | `false` | `true` → drop blank pages from the enhanced version even if blank-page removal is switched off in the settings. Blank pages are always kept in the original. |
| `compression` | no | `auto` | `auto` or `lossless` — how grey/colour page images are stored (see step 6 above). |

Booleans accept `true/false`, `1/0`, `yes/no`, `on/off`.

Headers:

| Header | Description |
|---|---|
| `Idempotency-Key` | Recommended. A unique value per scan (e.g. a UUID). Retrying with the same key never creates a second document / session. Max 200 characters. |

## Upload in one request

`POST /api/upload/v1/documents` — `multipart/form-data`

The [common fields](#common-fields) plus:

| Field | Required | Description |
|---|---|---|
| `file` | yes | The document. **Repeat the field** to send several files; they become pages in the order sent. Each file: max `DOCNEST_MAX_UPLOAD_MB`. |

Responses:

| Status | Body |
|---|---|
| `202 Accepted` | Stored durably (encrypted) and queued for processing. `{"id": "…", "status": "processing", "duplicate": false, "status_url": "/api/upload/v1/documents/…"}` |
| `200 OK` | Already known — same content or same idempotency key. `duplicate: true`, `id` of the existing document. |

Once `202`/`200` is returned, the document is safe: later processing failures never lose it.

A PDF:

```bash
curl -X POST https://docs.example.com/api/upload/v1/documents \
  -H "Authorization: Bearer $DOCNEST_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -F "file=@scan.pdf" \
  -F bucket=private \
  -F todo=true \
  -F "tags=Car,Insurance" \
  -F 'metadata={"device":"desk-scanner"}'
```

Three raw page images straight from the scanner (curl keeps the order of the `-F` options):

```bash
curl -X POST https://docs.example.com/api/upload/v1/documents \
  -H "Authorization: Bearer $DOCNEST_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -F "file=@page-1.pnm" -F "file=@page-2.pnm" -F "file=@page-3.pnm" \
  -F bucket=private -F dpi=300 -F skip_blank_pages=true
```

Python (`requests`):

```python
import uuid, requests

pages = ["page-1.png", "page-2.png"]
files = [("file", (name, open(name, "rb"))) for name in pages]
r = requests.post(
    "https://docs.example.com/api/upload/v1/documents",
    headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": str(uuid.uuid4())},
    files=files,
    data={"bucket": "private", "dpi": "300", "important": "true"},
    timeout=300,
)
r.raise_for_status()
print(r.json())
```

The dedupe check compares content: uploading the same files again returns `200` with the existing document. (The same single PDF sent through either upload style is also recognized.)

## Upload page by page — scan sessions

A session collects the pages of one document. Nothing is processed until you complete it.

```
POST /scans                       → 201  {"id": S, "status": "open", …}
POST /scans/S/pages  page=1 file  → 201
POST /scans/S/pages  page=2 file  → 201
…
POST /scans/S/complete            → 202  {"id": D, "status": "processing", …}
GET  /documents/D                 → {"status": "processed", …}   (optional)
```

### Open a session

`POST /api/upload/v1/scans` — `multipart/form-data` (or `application/x-www-form-urlencoded`) with the [common fields](#common-fields).

| Status | Body |
|---|---|
| `201 Created` | New session. |
| `200 OK` | A session with this `Idempotency-Key` already exists; it is returned (whatever its state). |

```json
{
  "id": "5d1c0f7e-…",
  "status": "open",
  "pages": [],
  "page_count": 0,
  "size": 0,
  "expires_at": "2026-10-09T08:15:00Z",
  "document_id": null,
  "duplicate": false,
  "status_url": null
}
```

The type (and the folder path's syntax) is checked here, so a typo fails before any page is sent. Missing folders are only created once the document is complete. A session expires `DOCNEST_SCAN_SESSION_HOURS` (default 24 h) after its last page upload; a token can have at most `DOCNEST_MAX_OPEN_SCAN_SESSIONS` (default 10) open sessions.

### Upload a page

`POST /api/upload/v1/scans/{id}/pages` — `multipart/form-data`

| Field | Required | Description |
|---|---|---|
| `file` | yes | One file in any [accepted format](#accepted-files). A multi-page TIFF or a PDF counts as one "page" slot but contributes all its pages at that position. |
| `page` | no | Position, starting at `1`. Omit to append after the highest position so far. **Sending a position again replaces that page** — this is what makes retries safe. Pages may arrive in any order. |

`201 Created`:

```json
{"page": 2, "kind": "image", "size": 1843121, "page_count": 2}
```

`kind` is `image` or `pdf` (as detected from the content). Always send `page` when you might retry: a retried request without it would append the page twice.

### Check a session

`GET /api/upload/v1/scans/{id}` → the session object shown above. `pages` lists the positions received, so a device that restarted can find out what is missing.

### Complete

`POST /api/upload/v1/scans/{id}/complete`

| Field | Required | Description |
|---|---|---|
| `expected_pages` | no | Number of page slots you sent. If it doesn't match, completion fails with `409` and nothing is created — a cheap guard against lost requests. |

Responses are the same as for `POST /documents`: `202` with the new document, or `200` with `duplicate: true` if identical content is already archived (the session's pages are then discarded). Calling `complete` again returns the same document with `200`, so it is safe to retry. Positions must be contiguous: with pages 1, 2 and 4 the call fails with `409 Missing page(s): 3`.

After completion the session is read-only (`409` on further page uploads) and is kept for 7 days so that retries still get the answer.

### Abandon

`DELETE /api/upload/v1/scans/{id}` → `204`. Deletes the session and its pages. Not possible after completion (`409`). Expired sessions are cleaned up automatically by the worker.

### Session example (bash)

```bash
API=https://docs.example.com/api/upload/v1
AUTH="Authorization: Bearer $DOCNEST_TOKEN"

scan=$(curl -fsS -X POST "$API/scans" -H "$AUTH" -H "Idempotency-Key: $(uuidgen)" \
         -F bucket=private -F dpi=300 -F skip_blank_pages=true | jq -r .id)

n=0
for f in page-*.pnm; do
  n=$((n + 1))
  curl -fsS --retry 5 --retry-all-errors -X POST "$API/scans/$scan/pages" -H "$AUTH" \
       -F "page=$n" -F "file=@$f"
done

curl -fsS --retry 5 --retry-all-errors -X POST "$API/scans/$scan/complete" -H "$AUTH" \
     -F "expected_pages=$n"
```

## Processing status

`GET /api/upload/v1/documents/{id}` (scope `upload:status`, only for documents uploaded with the same token)

```json
{"id": "…", "status": "processing | processed | failed", "stage": "assemble", "error": null}
```

`stage` is the next step to run: `received`, `assemble`, `validate`, `enhance`, `ocr`, `analyze`, `store`, `index`, `done`. The response never contains document content or metadata. If the uploaded images cannot be turned into a PDF (e.g. a corrupt image), `status` becomes `failed`; the upload stays in DocNest and the reason is shown in the web UI.

## Connectivity check

`GET /api/upload/v1/ping` → `{"status": "ok", "scanner": "<name>"}`

## Errors

Errors are JSON: `{"detail": "<message>"}`.

| Status | Meaning |
|---|---|
| `400` | Unknown type, folder path too deep, invalid `metadata`, `dpi` or `compression`, empty file, invalid page number. |
| `401` | Missing, wrong, revoked or expired token, or IP not allowed. |
| `403` | Token lacks the required scope. |
| `404` | Session/document not found (or belongs to another token). |
| `409` | Session already completed; missing pages or `expected_pages` mismatch; no pages; too many open sessions. |
| `410` | Session expired. Open a new one and upload again. |
| `413` | A file exceeds `DOCNEST_MAX_UPLOAD_MB`, the scan exceeds `DOCNEST_MAX_SCAN_MB` or `DOCNEST_MAX_PAGES`, or image dimensions are implausibly large. Also returned by your reverse proxy if its body limit is lower. |
| `415` | Not a PDF or supported image. |
| `429` | Too many failed authentication attempts from this IP (wait 15 min). |
| `5xx` | Temporary server problem — retry with backoff. |

## Limits and server settings

| Setting | Default | Applies to |
|---|---|---|
| `DOCNEST_MAX_UPLOAD_MB` | `100` | Each uploaded file (one page image, one PDF). |
| `DOCNEST_MAX_SCAN_MB` | `2000` | All files of one scan together (one request or one session). |
| `DOCNEST_MAX_PAGES` | `500` | Files per request / page slots per session, and pages of the final PDF. |
| `DOCNEST_SCAN_SESSION_HOURS` | `24` | Session lifetime after the last page upload. |
| `DOCNEST_MAX_OPEN_SCAN_SESSIONS` | `10` | Open sessions per token. |

Your **reverse proxy** limits the request body too. A single-request upload of many pages needs `client_max_body_size` (nginx) / `request_body max_size` (Caddy) at least as large as the whole scan; scan sessions only need it as large as one page — another reason to prefer sessions for long scans. Assembly runs in the RAM-backed work area (`DOCNEST_TMP_SIZE`); it holds one raw page at a time plus the growing PDF.

## Raspberry Pi scan station

A minimal, no-processing setup: SANE talks to the USB scanner, a script uploads the raw pages. Nothing besides `sane-utils`, `curl` and `jq` is needed on the Pi.

```bash
sudo apt install sane-utils curl jq uuid-runtime
scanimage -L                     # find the device name, e.g. "fujitsu:ScanSnap S1500:1234"
```

Store the token readable only by the scan user:

```bash
install -m 600 /dev/null ~/.docnest-token && nano ~/.docnest-token
```

`/usr/local/bin/scan-to-docnest`:

```bash
#!/usr/bin/env bash
# Scan every sheet in the feeder and upload the raw pages to DocNest page by page.
set -euo pipefail

API="https://docs.example.com/api/upload/v1"
TOKEN="$(cat ~/.docnest-token)"
DEVICE="${SCANNER_DEVICE:-}"          # empty = first scanner SANE finds
DPI=300
BUCKET="${1:-Private}"                # folder path, e.g. "Private/Taxes"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
auth=(-H "Authorization: Bearer $TOKEN")
retry=(--retry 8 --retry-all-errors --retry-delay 5 --max-time 300)

# Raw scanner output: one PNM file per page, no conversion on the Pi.
scanimage ${DEVICE:+-d "$DEVICE"} --batch="$work/page-%04d.pnm" --format=pnm \
  --resolution "$DPI" --mode Color --source "ADF Duplex" || true   # exits non-zero when the feeder is empty
shopt -s nullglob
pages=("$work"/page-*.pnm)
[ ${#pages[@]} -gt 0 ] || { echo "nothing scanned"; exit 1; }

scan=$(curl -fsS "${retry[@]}" -X POST "$API/scans" "${auth[@]}" -H "Idempotency-Key: $(uuidgen)" \
  -F "bucket=$BUCKET" -F "dpi=$DPI" -F skip_blank_pages=true \
  -F "metadata={\"device\":\"$(hostname)\"}" | jq -r .id)

n=0
for f in "${pages[@]}"; do
  n=$((n + 1))
  curl -fsS "${retry[@]}" -X POST "$API/scans/$scan/pages" "${auth[@]}" -F "page=$n" -F "file=@$f" >/dev/null
  echo "uploaded page $n/${#pages[@]}"
done

curl -fsS "${retry[@]}" -X POST "$API/scans/$scan/complete" "${auth[@]}" -F "expected_pages=$n" | jq .
```

Notes:

- Use the `--source`/`--mode` names your scanner offers (`scanimage -A` lists them). `--mode Lineart` gives 1-bit pages, which DocNest stores as compact CCITT G4.
- `--format=png`, `jpeg` or `tiff` work just as well; PNM is simply what SANE produces without any conversion.
- Wire the script to the scanner's button with `scanbd`, or to a GPIO push button.
- Uploading while scanning: run `scanimage --batch` with `--batch-print` and upload each file as soon as it appears; since each page names its position, order doesn't matter.
- If the Pi loses power mid-upload, the open session simply expires. Starting the script again opens a new one.

## Recommendations for devices

- Send the scanner's raw output; let DocNest do the conversion.
- Send `dpi` with the real scan resolution, at least for PNM.
- Generate the `Idempotency-Key` **before** the first attempt and reuse it for retries.
- In sessions, always send `page` and `expected_pages`.
- Retry on network errors and `5xx` with backoff; do not retry `4xx` except `429` (wait 15 min).
- Always use HTTPS and keep the token in protected storage on the device.
