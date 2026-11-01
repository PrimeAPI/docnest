# Filing new documents

Every new document in the inbox gets a suggestion for where it belongs. DocNest keeps your folders in order, but **nothing moves until you approve**. Find the suggestions under **Inbox → Filing suggestions**, on the document itself (**File it here?**) and next to the folder field in **Review inbox**.

## What you see and what you can do

Suggestions are grouped by target folder, for example `Privat › Versicherungen › Kfz` *(new)* · 3 documents. For each group you can:

- **Approve** to move the documents and create the new folders.
- Rename a new folder before approving.
- Choose **Elsewhere…** to send the group to an existing folder of your choice.
- **Dismiss** to leave the documents where they are. A dismissed suggestion is not offered again.

Filing a document yourself, by moving it or by picking a folder in the review, also settles its suggestion. **Approve all** handles every group at once. Untick **Suggest automatically** to switch the feature off. Documents that were in the inbox before it was switched on get suggestions with **Suggest for … more**.

## How a suggestion comes about

1. **Documents that arrive together are planned together.** Once a document is processed, the plan waits while documents uploaded within ten minutes of it are still being processed, for up to twenty minutes. A scanner batch or a multi-file upload is then grouped in one pass: the same series, the same sender and type, similar titles. So three bank statements get one folder, not three.
2. **Open suggestions count as folders.** A suggestion you have not decided on yet is a *proposed* folder. A document arriving later matches it like a real folder, and the AI model sees it marked as proposed. A later letter from the car insurer therefore joins `Versicherungen/Kfz` instead of starting `Autoversicherung`. Approving one of them creates the folder; the others then simply go into it.
3. **Second chances.** A document nothing fitted is planned again with the next new documents, so two of a kind can form a folder together.

Where a group goes, strongest evidence first:

- **Your filing instructions**, saved in the filing suggestions dialog (*Remember*). For example, "Stadtwerke nach Wohnung/Nebenkosten".
- **How you filed such documents before.** If a folder already holds the same series, or the same sender with similar titles, the document goes there.
- **The AI model**, when one is set up (Settings → AI). It sees the whole folder tree, with document counts and senders per folder, and chooses an existing or proposed folder, or adds up to two new ones below one. Each answer is checked against the tree, and one that cannot be resolved is not used.
- **Without a model**, folders are matched and named from the titles. A new folder then needs at least two documents.

Year folders follow the target's habit. If the target already has `2025` and `2026`, a document goes into its year. The *year folders* and *new folders* switches of the filing suggestions apply here too.

A document you uploaded into a folder only goes deeper below that folder. A document without a folder, or one only in the folder a scanner puts everything into, may go anywhere.

The reasons shown with a suggestion can quote title words, so they are stored encrypted like the titles.
