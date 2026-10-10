# Document reviews and page editing

DocNest can look through a folder, a selection or your archive and leave suggestions for you to review later. The worker runs the review, so the browser can be closed. No suggestion changes documents automatically.

## Start a review

From a document list or a folder, open **Assistant → Look through**. From the **Assistant** page, choose **Look through everything**.

- **Quick, pages only** prepares page thumbnails and fingerprints, automatically hides obvious blank pages in Enhanced, and checks for duplicate documents or pages, complementary page numbers across scans, scans containing several letters, missing pages and series gaps. It needs no model.
- **Thorough, with the AI model** adds document reading, naming suggestions, criticism and a final report. Choose any installed model that fits your server, including your existing small model or an 8B model. Enable thinking only when the installed model supports it. More context consumes more memory.
- Start now or choose a time. By default, run until finished, even if a larger folder takes longer than one night. You may opt into a report deadline, for example 07:00, for a partial report by that time. A run can finish earlier if its work is complete or it stops finding useful steps. It does not deliberately repeat reasoning to fill twelve hours.

Folder assistant tasks resolve the complete folder on the server, not just the first 100 or 500 documents. **Include subfolders** controls whether descendants are included. The document picker is paginated for previews only; exclusions persist across preview pages. Folder, selection and whole-archive background reviews have no fixed document-count cap. Their scope is snapshotted when started or scheduled, so later uploads are not added to an already queued task. Larger scopes take longer, while each model question remains small. The separate interactive filing proposal still supports up to 500 documents and explicitly directs larger folders to a background review instead of silently truncating them.

Reviews prepare missing page fingerprints automatically. **Prepare all pages** also lets you do this in advance. Documents still being processed or whose files are unavailable may only receive text checks; the journal records this limitation.

The review uses small, structured questions instead of sending the entire archive in one prompt. Long documents may initially be shortened, with further sections accessible through the read tool. The model can read, compare and search selected documents, and propose changes. A critic, defender and judge assess uncertain proposals; their arguments are visible. More time or repeated agreement does not guarantee correctness, so inspect the page previews and evidence before applying changes.

## Decide what to change

Open **Assistant** to see progress, the journal and finished reports. Reports separate open findings, your decisions and findings set aside by the assistant. Review names and details individually; edit proposed values or untick changes you do not want.

For page changes, choose **Review the pages…**. You can compare matching pages side by side at readable size, change the proposed page order, split or join outputs and put left-out pages back. The editor shows which new documents will be created, which sources will go to trash and how many pages are left out. **Apply as suggested** uses the displayed proposal directly.

Page numbers and similarities are evidence, not proof that scans belong together. Missing marks can be OCR errors. Repeated forms can contain different details; handwritten annotations, signatures and unreadable scans deserve particular care. Conflicting suggestions can become outdated after you apply another one; inspect the resulting documents or start a new review.

## Make changes yourself

Use a document's **Pages** action, or select documents in a list and choose **Pages**. Click pages to select them, move them to a new or existing output, use the scissors to split, join adjacent outputs, reorder pages or leave pages out. These are the same operations used for assistant suggestions.

Blank pages are hidden automatically in **Enhanced**, while **Original** stays complete. Hiding or reordering pages keeps the same document and metadata, without rewriting stored files or rerunning OCR or AI. The editor includes hidden pages so you can put them back.

On a split, the first part keeps the existing document ID; only additional parts become new documents. Merging multiple sources creates a combined document and puts superseded sources in **Trash**. New outputs reuse processed pages and their OCR text; they only need storage, previews and indexing. Deleting a duplicate puts only that duplicate in trash, leaving the kept copy untouched. There is no permanent-delete action. See [the complete operation rules](document-operations.md).

New PDF uploads retain their exact bytes as the original; sanitization and enhancement use processing copies. Raw image uploads are assembled into a PDF, which is their document original. Existing originals sanitized by older versions cannot be reconstructed without the source upload.

Every document's **History** shows page changes and metadata edits, including old and new values and the assistant finding a change came from. Page changes can be undone while the relevant documents have not been independently restored or superseded and are not currently being processed. Undo puts generated documents in trash and brings the sources back; you can also restore individual sources directly. Original downloads remain available from the trash view.

## Restarts, stopping and retention

Progress is checkpointed. A restarted worker resumes a review, and a temporarily unavailable model is retried. Document notes are reused only when the text and model match. If the model repeatedly fails, completed findings remain in a partial report.

**Stop and report** stops at the next checkpoint after the current request and writes the report with completed findings. Cancelling a scheduled review prevents it from starting. The deadline bounds model requests; local rendering or comparison already in progress may finish after it.

Finished review reports are retained for 60 days. Document alteration history and original files are retained independently of reports. Deleting a report does not undo applied changes. Scheduling currently means a single review at a chosen time; recurring nightly reviews are not yet implemented. Bulk selection in document lists is still bounded separately; use a folder assistant or **Look through everything** to review the entire scope.
