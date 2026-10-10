# Document operations: identity first, processing only when needed

The original upload is evidence, a document is its stable identity and metadata, and Enhanced is a presentation of processed pages. Changing the presentation must not look like uploading the document again.

## What happens when

| Operation | Document identity | Files and processing |
|---|---|---|
| An obviously blank page is detected | Unchanged | Hidden automatically in Enhanced; Original remains complete. No replacement document or processing job. |
| Hide a page, remove a repeated page, or reorder pages within a document | Unchanged | Save a visible-page order. Do not rewrite either stored PDF, rerun OCR, or reinterpret metadata. |
| Put hidden pages back | Unchanged | Change the visible-page order; no processing. |
| Remove a duplicate document | Kept copy unchanged; duplicate goes to recoverable trash | No change, OCR, renaming or analysis of the kept copy. |
| Split a document or extract pages into a separate document | First single-source output keeps the existing ID; additional outputs get new IDs | Existing document gets a view-only change. New parts copy processed PDF pages and their text layer; only storage, previews and indexing run. |
| Merge pages from several documents | New combined document | Copy already processed pages, preserving OCR. Retain sources in recoverable trash unless an output still uses their identity. Only storage, previews and indexing run. |
| Edit names, folders, dates, tags or status | Unchanged | Update metadata and search as needed, log the edit; no document processing. |
| Explicitly choose Reprocess | Unchanged | Run only the requested processing steps. This is the way to request fresh OCR or model analysis. |

The editor shows **Unchanged**, **Same document · view only**, or **New** for each output and explains which sources would go to trash. Its first single-source output keeps that source's identity. A normal extract leaves the remainder first; splitting with the scissors keeps the first part. Putting every page of a source into no output is an explicit trash operation, never an automatic blank-page cleanup.

## Minimal storage model

Original and processed archive files remain intact. A small `Document.page_view` records physical archive page numbers, automatic blank-page detection, and an optional explicit visible order. Enhanced viewing and downloading apply this order to a temporary PDF; it is not a new stored document version. Fingerprints and page-editor previews continue to address the full physical archive, so hidden pages can be inspected and restored without another scan.

Automatic hiding is conservative: it requires no recognized text and virtually no ink. A wholly blank scan is left visible rather than silently removing an entire document. Existing fingerprints work immediately after upgrading; older scans without fingerprints are prepared when Enhanced is first opened, without OCR or AI. Explicitly putting a blank page back overrides automatic hiding.

Derived documents inherit filing labels and the first source's title unless a title is supplied. They reuse the embedded PDF text layer, or separately stored page-scoped Docling text, and do not ask a model to reinterpret the scan. Docling retains recognized lines for every page; older structured results provide a page-scoped fallback. Extracted financial values and document-level structured layouts are not blindly copied to a differently composed document. A digital derivative does not create a second physical paper original; provenance links back to the sources. Reprocess is available if fresh interpretation is wanted.

## Safety and undo

Every user-applied page view, composition, trash and restore operation is logged with provenance. View-only undo restores the previous visible order in the same document. Composition undo also puts additional outputs in recoverable trash and restores any retired sources. Original files are never rewritten or purged.

The editor submits the visible orders it started from; applying a stale editor is rejected. Row locks serialize changes. Undo rejects a newer incompatible view, an independently restored source, an unavailable derived output or active processing. Reprocessing preserves an explicit order while physical page counts stay compatible; when they change it resets to a safe automatically detected view. Undo does not apply an old page map to a newly processed archive.

View edits intentionally do not rerun OCR or rewrite the full-text search content. Hidden text is still retained and searchable, just as hidden pages remain in Original. On a genuinely new merged or split document, its search text comes only from its copied pages.

## What this replaces

The previous implementation registered every altered page list as an upload, trashed the source and reran OCR and AI. That was unnecessary for hiding a blank backside, and duplicated work even for real compositions. It is replaced by a lightweight presentation plus a storage/index-only path for genuinely new documents—not a new general-purpose versioning framework.

Existing alteration history and previously generated documents are not silently collapsed or deleted. Their existing undo/restore controls remain available. Scheduling and AI review behavior are separate from page editing; approving a page suggestion follows these same operation rules.
