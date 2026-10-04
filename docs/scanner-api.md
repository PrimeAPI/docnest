# Scanner upload API

Automated devices (scanners, scripts, integrations) upload PDFs through a dedicated API. It is separate from the web API: a scanner token can **only** upload documents and read the processing status of its own uploads.

## Token

Create one per device in **Settings → Scanners → Add scanner**. The token (`dn_scan_<id>_<secret>`) is shown once; DocNest stores only a hash. You can restrict a token to IP addresses/networks, rotate it (*New token*) or revoke it at any time.

Send it as a bearer token:

```
Authorization: Bearer dn_scan_0123456789ab_…
```

## Upload a document

`POST /api/upload/v1/documents` — `multipart/form-data`

| Field | Required | Description |
|---|---|---|
| `file` | yes | The PDF (max `DOCNEST_MAX_UPLOAD_MB`, default 100 MB). |
| `bucket` | yes | Bucket slug or name, e.g. `private`, `business`, `studies` (see *Tags & more → Buckets & types*). |
| `document_type` | no | Type slug/name (`mail`, `contract`, `invoice`, `notice`, `statement`, `other`) or `auto` (default) to let DocNest decide. |
| `todo` | no | `true` → the document starts with status *Todo*. |
| `important` | no | `true` → marked important. |
| `tags` | no | Comma-separated tag names, e.g. `Tax,2026`. Existing tags and aliases are reused. |
| `metadata` | no | JSON object with any extra data (stored encrypted, max 8 KB). |

Headers:

| Header | Description |
|---|---|
| `Idempotency-Key` | Recommended. A unique value per scan (e.g. a UUID). Retrying with the same key never creates a second document. |

Responses:

| Status | Meaning |
|---|---|
| `202 Accepted` | Stored durably (encrypted) and queued for processing. `{"id", "status": "processing", "duplicate": false, "status_url"}` |
| `200 OK` | Already known — same file content or same idempotency key. `duplicate: true`, `id` of the existing document. |
| `400` | Unknown bucket or type, invalid metadata. |
| `401` | Missing, wrong, revoked or expired token, or IP not allowed. |
| `403` | Token lacks the required scope. |
| `413` | File too large. |
| `415` | Not a PDF. |
| `429` | Too many failed authentication attempts from this IP. |

Once `202`/`200` is returned, the document is safe: later processing failures never lose it.

Example:

```bash
curl -X POST https://docs.example.com/api/upload/v1/documents \
  -H "Authorization: Bearer $DOCNEST_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -F "file=@scan.pdf;type=application/pdf" \
  -F bucket=private \
  -F document_type=auto \
  -F todo=true \
  -F "tags=Car,Insurance" \
  -F 'metadata={"device":"desk-scanner","pages_scanned":3}'
```

Python:

```python
import uuid, requests

with open("scan.pdf", "rb") as f:
    r = requests.post(
        "https://docs.example.com/api/upload/v1/documents",
        headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("scan.pdf", f, "application/pdf")},
        data={"bucket": "private", "document_type": "auto", "important": "true"},
        timeout=120,
    )
r.raise_for_status()
print(r.json())
```

## Processing status

`GET /api/upload/v1/documents/{id}` (scope `upload:status`, only for documents uploaded with the same token)

```json
{"id": "…", "status": "processing | processed | failed", "stage": "ocr", "error": null}
```

The response never contains document content or metadata.

## Connectivity check

`GET /api/upload/v1/ping` → `{"status": "ok", "scanner": "<name>"}`

## Recommendations for scanner devices

- Generate the `Idempotency-Key` **before** the first attempt and reuse it for retries.
- Retry on network errors and `5xx` with backoff; do not retry `4xx` except `429` (wait 15 min).
- Always use HTTPS and keep the token in protected storage on the device.
