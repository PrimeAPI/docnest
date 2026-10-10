"""Page by page across the archive: what code finds from the pages' fingerprints.

- **The same page twice** in one document (scanned again) — remove the copy.
- **A document that is a copy** of another, or wholly contained in it — put it in the trash.
- **Pages of one document in another** — remove them where they are the minority.
- **One letter in several scans** — page marks ("Seite 2 von 3") that complete each other.
- **Several letters in one scan** — page marks that start again ("1/2 2/2 1/3 …").
- **Pages missing** — marks of pages that are nowhere.
- **Empty pages** left in a document.

Every suggestion is a composition (`apps.documents.alterations`): which pages make up which
documents afterwards. Nothing is applied here, and applying one never touches an original.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from apps.assist.checks import Finding, Item
from apps.documents.models import Document, Source
from apps.documents.pages import EMPTY, Fingerprint, fingerprints, same_page

COMMON = 40  # a sketch hash on more pages than this is boilerplate (a letterhead line)
MIN_SHARED = 6  # sketch hashes two pages share before they are compared
BANDS = 8  # the picture hash is cut into this many bands; pages alike in one are compared
MAX_CANDIDATES = 200_000


@dataclass(frozen=True)
class P:
    doc: int  # index into the items
    page: int  # 1-based


def _candidates(prints: dict[int, list[Fingerprint]]) -> set[tuple[P, P]]:
    by_hash: dict[int, list[P]] = defaultdict(list)
    by_band: dict[tuple[int, str], list[P]] = defaultdict(list)
    for doc, fps in prints.items():
        for n, fp in enumerate(fps, start=1):
            if fp.ink < EMPTY:
                continue
            if fp.has_text:
                for h in fp.sketch:
                    by_hash[h].append(P(doc, n))
            else:
                width = len(fp.dhash) // BANDS
                for b in range(BANDS):
                    by_band[(b, fp.dhash[b * width : (b + 1) * width])].append(P(doc, n))
    shared: Counter[tuple[P, P]] = Counter()
    for members in by_hash.values():
        if len(members) > COMMON:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                shared[(members[i], members[j])] += 1
                if len(shared) >= MAX_CANDIDATES:
                    return {pair for pair, n in shared.items() if n >= MIN_SHARED}
    pairs = {pair for pair, n in shared.items() if n >= MIN_SHARED}
    for members in by_band.values():
        if len(members) > COMMON:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pairs.add((members[i], members[j]))
                if len(pairs) >= MAX_CANDIDATES:
                    return pairs
    return pairs


def same_pages(prints: dict[int, list[Fingerprint]]) -> list[tuple[P, P, float]]:
    out = []
    for a, b in sorted(_candidates(prints), key=lambda p: (p[0].doc, p[0].page, p[1].doc, p[1].page)):
        score = same_page(prints[a.doc][a.page - 1], prints[b.doc][b.page - 1])
        if score > 0:
            out.append((a, b, score) if (a.doc, a.page) < (b.doc, b.page) else (b, a, score))
    return out


def _user_edited(d: Document) -> bool:
    return any(v == Source.USER for k, v in d.field_sources.items() if k != "tags_removed")


def _keep_first(a: tuple[Item, Document], b: tuple[Item, Document]) -> bool:
    """Whether to keep `a` rather than `b`: edited by the user, more pages, then the older one."""
    rank = lambda x: (not _user_edited(x[1]), -x[0].pages, x[0].uploaded_at)  # noqa: E731
    return rank(a) <= rank(b)


def _pages(numbers: list[int] | set[int]) -> str:
    numbers = sorted(numbers)
    return ", ".join(map(str, numbers))


def _all_pages(item: Item) -> list[list[object]]:
    return [[item.uuid, n] for n in range(1, item.pages + 1)]


def _compose(sources: list[Item], outputs: list[list[list[object]]]) -> dict[str, object]:
    return {
        "type": "compose",
        "sources": [s.uuid for s in sources],
        "outputs": [{"pages": pages, "title": ""} for pages in outputs],
    }


def analyse(items: list[Item], documents: dict[str, Document]) -> tuple[list[Finding], set[str]]:
    """Findings, and the uuids of the documents that had fingerprints (the others: text checks)."""
    docs = [documents[i.uuid] for i in items]
    by_pk = fingerprints(docs)
    index = {n: by_pk[d.pk] for n, d in enumerate(docs) if d.pk in by_pk}
    covered = {items[n].uuid for n in index}
    findings: list[Finding] = []
    pairs = same_pages(index)

    # The same page twice in one document.
    inner: dict[int, list[tuple[int, int]]] = defaultdict(list)
    across: dict[tuple[int, int], list[tuple[int, int, float]]] = defaultdict(list)
    for a, b, score in pairs:
        if a.doc == b.doc:
            inner[a.doc].append((a.page, b.page))
        else:
            across[(a.doc, b.doc)].append((a.page, b.page, score))
    for doc, twins in inner.items():
        item = items[doc]
        copies = {max(p, q) for p, q in twins}
        remaining = [n for n in range(1, item.pages + 1) if n not in copies]
        if not remaining:
            continue
        findings.append(
            Finding(
                "duplicate_pages",
                "A page is in it twice",
                f"In “{item.title}”, "
                + "; ".join(f"page {q} is the same as page {p}" for p, q in sorted(twins))
                + f". Without the copies it has {len(remaining)} pages.",
                [item.uuid],
                action=_compose([item], [[[item.uuid, n] for n in remaining]]),
                confidence="high",
                evidence=f"{item.ref}: pages " + "; ".join(f"{p} = {q}" for p, q in sorted(twins)),
                matches=[[item.uuid, p, item.uuid, q] for p, q in sorted(twins)],
            )
        )

    # Documents sharing pages.
    for (da, db), matched in across.items():
        ia, ib = items[da], items[db]
        content_a = {n for n, fp in enumerate(index[da], start=1) if fp.ink >= EMPTY}
        content_b = {n for n, fp in enumerate(index[db], start=1) if fp.ink >= EMPTY}
        in_a = {p for p, _, _ in matched}
        in_b = {q for _, q, _ in matched}
        match_list = [[ia.uuid, p, ib.uuid, q] for p, q, _ in sorted(matched)]
        evidence = f"{ia.ref} and {ib.ref}: pages " + "; ".join(f"{p} = {q}" for p, q, _ in sorted(matched))
        if (content_b and content_b <= in_b) or (content_a and content_a <= in_a):
            if content_a <= in_a and content_b <= in_b:
                first = _keep_first((ia, documents[ia.uuid]), (ib, documents[ib.uuid]))
                keep, drop = (ia, ib) if first else (ib, ia)
                title, text = (
                    "Duplicate",
                    f"“{drop.title}” and “{keep.title}” have the same pages. Keep “{keep.title}”, "
                    f"put “{drop.title}” in the trash.",
                )
            else:
                keep, drop = (ia, ib) if content_b <= in_b else (ib, ia)
                title, text = (
                    "Contained in another document",
                    f"Every page of “{drop.title}” is also in “{keep.title}”. "
                    f"“{drop.title}” can go to the trash.",
                )
            findings.append(
                Finding(
                    "duplicate",
                    title,
                    text,
                    [keep.uuid, drop.uuid],
                    action=_compose([drop], []),
                    confidence="high",
                    evidence=evidence,
                    matches=match_list,
                )
            )
            continue
        # Some pages in both: remove them where they are the smaller part.
        share_a, share_b = len(in_a) / max(1, len(content_a)), len(in_b) / max(1, len(content_b))
        source, other, dup = (ib, ia, in_b) if share_b <= share_a else (ia, ib, in_a)
        rest = [n for n in range(1, source.pages + 1) if n not in dup]
        findings.append(
            Finding(
                "duplicate_pages",
                "Pages that are also in another document",
                f"Page {_pages(dup)} of “{source.title}” {'is' if len(dup) == 1 else 'are'} "
                f"also in “{other.title}”. "
                f"Removed, “{source.title}” keeps {len(rest)} page(s).",
                [source.uuid, other.uuid],
                action=_compose([source], [[[source.uuid, n] for n in rest]]),
                confidence="medium",
                evidence=evidence,
                matches=match_list,
            )
        )

    findings.extend(_marks(items, index, documents))
    findings.extend(_empty_pages(items, index))
    return findings, covered


# --- Page marks ------------------------------------------------------------------------------


def _segments(fps: list[Fingerprint]) -> list[tuple[int, list[int], int]]:
    """Runs of pages that belong to one letter by their marks: (of, pages, first mark)."""
    segments: list[tuple[int, list[int], int]] = []
    current: list[int] = []
    total = 0
    last = 0
    first = 0
    for n, fp in enumerate(fps, start=1):
        mark = fp.mark
        if mark and current and (mark[1] != total or mark[0] < last):
            segments.append((total, current, first))
            current, total, last, first = [], 0, 0, 0
        if mark:
            if not current:
                first = mark[0]
            total, last = mark[1], mark[0]
        current.append(n)
    if current:
        segments.append((total, current, first))
    return segments


def _compatible(a: Item, b: Item) -> bool:
    from apps.assist.checks import _references

    if a.sender and b.sender and a.sender.casefold() != b.sender.casefold():
        return False
    ra, rb = _references(a), _references(b)
    if ra and rb and not ra & rb:
        return False
    return not (a.day and b.day and abs((a.day - b.day).days) > 7)


def _marks(
    items: list[Item], index: dict[int, list[Fingerprint]], documents: dict[str, Document]
) -> list[Finding]:
    findings: list[Finding] = []
    # Several letters in one scan.
    partial: list[tuple[int, int, set[int]]] = []  # (doc, of, marks shown) of single-letter documents
    for doc, fps in index.items():
        item = items[doc]
        segments = _segments(fps)
        marked = [s for s in segments if s[0]]
        if len(marked) >= 2 and len(segments) >= 2:
            outputs = [[[item.uuid, n] for n in pages] for _, pages, _ in segments]
            findings.append(
                Finding(
                    "split_inside",
                    "Several letters in one scan",
                    f"The page numbers in “{item.title}” start again: "
                    + "; ".join(
                        f"pages {_pages(pages)}" + (f" are a {of}-page letter" if of else "")
                        for of, pages, _ in segments
                    )
                    + f". Split, it becomes {len(segments)} documents.",
                    [item.uuid],
                    action=_compose([item], outputs),
                    confidence="medium",
                    evidence=f"{item.ref} page marks: "
                    + ", ".join(
                        f"{n}:{fp.mark[0]}/{fp.mark[1]}" for n, fp in enumerate(fps, start=1) if fp.mark
                    ),
                )
            )
            continue
        if len(marked) == 1:
            of = marked[0][0]
            shown = {fp.mark[0] for fp in fps if fp.mark and fp.mark[1] == of}
            if of > 1 and shown != set(range(1, of + 1)):
                partial.append((doc, of, shown))

    # One letter in several scans, and pages that are nowhere.
    used: set[int] = set()
    by_total: dict[int, list[tuple[int, set[int]]]] = defaultdict(list)
    for doc, of, shown in partial:
        by_total[of].append((doc, shown))
    for of, members in by_total.items():
        members.sort(key=lambda m: min(m[1]))
        for i, (doc, shown) in enumerate(members):
            if doc in used:
                continue
            group, have = [(doc, shown)], set(shown)
            for other, other_shown in members[i + 1 :]:
                if (
                    other in used
                    or other_shown & have
                    or not all(_compatible(items[g], items[other]) for g, _ in group)
                ):
                    continue
                group.append((other, other_shown))
                have |= other_shown
            missing = set(range(1, of + 1)) - have
            if len(group) >= 2:
                used.update(g for g, _ in group)
                parts = [items[g] for g, _ in group]
                findings.append(
                    Finding(
                        "split",
                        "One letter in several scans",
                        f"These scans hold pages {_pages(have)} of one {of}-page letter: "
                        + "; ".join(f"“{items[g].title}” page {_pages(s)}" for g, s in group)
                        + ". Merged, they become one document in page order."
                        + (f" Page {_pages(missing)} is still missing." if missing else ""),
                        [p.uuid for p in parts],
                        action=_compose(
                            parts,
                            [
                                [
                                    [items[g].uuid, n]
                                    for _, g, n in sorted(
                                        (fp.mark[0] if fp.mark else of + n, g, n)
                                        for g, _ in group
                                        for n, fp in enumerate(index[g], start=1)
                                    )
                                ]
                            ],
                        ),
                        confidence="medium",
                        evidence="; ".join(f"{items[g].ref} marks {_pages(s)} of {of}" for g, s in group),
                    )
                )
            elif missing and items[doc].pages < of:
                item = items[doc]
                findings.append(
                    Finding(
                        "missing_pages",
                        "Pages seem to be missing",
                        f"“{item.title}” is a {of}-page letter, but page {_pages(missing)} "
                        f"{'was' if len(missing) == 1 else 'were'} not identified in these documents. "
                        "The paper original may have it.",
                        [item.uuid],
                        confidence="medium",
                        evidence=f"{item.ref} shows page marks {_pages(shown)} of {of}",
                    )
                )
    return findings


def _empty_pages(items: list[Item], index: dict[int, list[Fingerprint]]) -> list[Finding]:
    findings = []
    for doc, fps in index.items():
        item = items[doc]
        empty = [n for n, fp in enumerate(fps, start=1) if fp.ink < EMPTY and not fp.has_text]
        keep = [n for n in range(1, item.pages + 1) if n not in empty]
        if not empty or not keep:
            continue
        findings.append(
            Finding(
                "empty_pages",
                "Empty pages",
                f"Page {_pages(empty)} of “{item.title}” {'is' if len(empty) == 1 else 'are'} empty.",
                [item.uuid],
                action=_compose([item], [[[item.uuid, n] for n in keep]]),
                confidence="medium",
                evidence=f"{item.ref}: pages {_pages(empty)} have no ink and no text",
            )
        )
    return findings
