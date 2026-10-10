"""What code can find in a set of documents without a model; the review's first evidence.

Each check returns findings: something the user should know, often with a change to apply
(delete a duplicate, merge the parts of one letter). They are candidates — the review lets
the model argue about them before they go into the report — and they carry the evidence
the code saw, so the model and the user can judge them.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from apps.search import tokenizer

SHINGLE = 5  # words per shingle when comparing texts
DUPLICATE = 0.8  # Jaccard similarity of the texts from which two documents are the same
CONTAINED = 0.9  # share of the smaller text found in the larger: a part or a copy of it
COMMON_SHINGLE = 12  # a shingle in more documents is boilerplate (letterhead, footer)
MIN_TEXT = 40  # characters of text below which a document has no readable text
NEAR_UPLOAD = timedelta(minutes=20)  # scanned in one go
MAX_GAP_MONTHS = 6  # more missing months than this is not a series with gaps
PAGE_MARK = re.compile(
    r"(?<![\w])(?:seite|blatt|page|pg\.?|s\.)\s*(\d{1,3})\s*(?:von|of|/|aus|v\.)\s*(\d{1,3})(?!\d)", re.I
)
MONTH_NAMES = "Januar Februar März April Mai Juni Juli August September Oktober November Dezember".split()  # noqa: SIM905


@dataclass
class Item:
    """A document as the review sees it."""

    uuid: str
    ref: str  # "D3": how the model names it — short, and it cannot invent a UUID
    title: str
    sender: str
    type_name: str
    day: date | None
    pages: int
    uploaded_at: datetime
    text: str
    extracted: dict[str, Any]
    series_id: int | None
    series_name: str
    tags: list[str]
    folder: str


@dataclass
class Finding:
    # duplicate | duplicate_pages | split | split_inside | missing_pages | empty_pages | gap
    # | missing_info | related | names | note | fix
    kind: str
    title: str  # one line: "Possible duplicate", "One letter in two scans"
    text: str  # what was found and why it matters
    documents: list[str]  # uuids, in a meaningful order (the merge order, the one to keep first)
    # What applying does: {"type": "compose", "sources": [uuid], "outputs": [{"pages": [[uuid, n]], "title"}]}
    # (pages into documents; sources not kept unchanged go to the trash — see apps.documents.alterations)
    # | {"type": "changes", "groups": [ChangeGroup]} (details: title, sender, type, tags)
    action: dict[str, Any] | None = None
    confidence: str = "medium"  # high | medium | low
    source: str = "check"  # check | model
    evidence: str = ""  # what the code saw, given to the model when it argues about the finding
    debate: list[dict[str, str]] = dataclasses.field(default_factory=list)  # [{"role", "text"}]
    matches: list[list[Any]] = dataclasses.field(default_factory=list)  # [[uuid, page, uuid, page]] alike
    verdict: str = ""  # keep | drop | unsure: the model's judgment; "" not argued about
    id: str = ""

    @property
    def key(self) -> str:
        """Two findings with this key say the same: the second is not added."""
        what = json.dumps(self.action, sort_keys=True) if self.action else ""
        return f"{self.kind}:{','.join(sorted(self.documents))}:{what}"


def compose(sources: list[Item], outputs: list[list[Item]] | None = None) -> dict[str, Any]:
    """Whole documents into new ones (merged in this order); no outputs: the sources go to the trash."""
    return {
        "type": "compose",
        "sources": [s.uuid for s in sources],
        "outputs": [
            {"pages": [[i.uuid, n] for i in output for n in range(1, i.pages + 1)], "title": ""}
            for output in outputs or []
        ],
    }


# --- Page numbers ----------------------------------------------------------------------------------


def page_marks(text: str) -> tuple[set[int], int | None]:
    """The page numbers a text shows ("Seite 2 von 3") and of how many pages; None without marks."""
    totals: Counter[int] = Counter()
    seen: dict[int, set[int]] = defaultdict(set)
    for m in PAGE_MARK.finditer(text):
        page, total = int(m.group(1)), int(m.group(2))
        if 1 <= page <= total <= 200:
            totals[total] += 1
            seen[total].add(page)
    if not totals:
        return set(), None
    total = totals.most_common(1)[0][0]
    return seen[total], total


def _pages(numbers: set[int] | list[int]) -> str:
    return ", ".join(str(n) for n in sorted(numbers))


def missing_pages(items: list[Item]) -> list[Finding]:
    """Documents whose page numbers say pages are missing — and not in another selected document."""
    marks = {i.uuid: page_marks(i.text) for i in items}
    findings = []
    for item in items:
        seen, total = marks[item.uuid]
        if not total or total == 1:
            continue
        missing = set(range(1, total + 1)) - seen
        # OCR misses a mark now and then: only count it when the page count agrees.
        if not missing or item.pages >= total:
            continue
        elsewhere = [
            other
            for other in items
            if other.uuid != item.uuid and marks[other.uuid][1] == total and marks[other.uuid][0] & missing
        ]
        if elsewhere:
            continue  # split_scans proposes merging them
        findings.append(
            Finding(
                "missing_pages",
                "Pages seem to be missing",
                f"“{item.title}” is marked as {total} pages but has {item.pages}; "
                f"page {_pages(missing)} {'is' if len(missing) == 1 else 'are'} not there. "
                "The paper original may be worth checking.",
                [item.uuid],
                confidence="medium",
                evidence=f"{item.ref} shows page marks {_pages(seen)} of {total}; it has {item.pages} pages.",
            )
        )
    return findings


def _compatible(a: Item, b: Item) -> bool:
    """Could be the same letter: no different senders or dates that contradict it."""
    if a.sender and b.sender and a.sender.casefold() != b.sender.casefold():
        return False
    ra, rb = _references(a), _references(b)
    if ra and rb and not ra & rb:
        return False
    return not (a.day and b.day and abs((a.day - b.day).days) > 7)


def _references(item: Item) -> set[str]:
    refs = item.extracted.get("references") or {}
    values = set(refs.values()) if isinstance(refs, dict) else set()
    return {v.replace(" ", "").casefold() for v in values if isinstance(v, str) and len(v) >= 4}


def split_scans(items: list[Item]) -> list[Finding]:
    """Parts of one letter in several documents: complementary page marks, or a continuation."""
    marks = {i.uuid: page_marks(i.text) for i in items}
    findings: list[Finding] = []
    used: set[str] = set()
    by_total: dict[int, list[Item]] = defaultdict(list)
    for item in items:
        seen, total = marks[item.uuid]
        if total and total > 1 and seen:
            by_total[total].append(item)
    for total, members in by_total.items():
        members = sorted(members, key=lambda i: min(marks[i.uuid][0]))
        for start, first in enumerate(members):
            if first.uuid in used:
                continue
            parts, covered = [first], set(marks[first.uuid][0])
            for other in members[start + 1 :]:
                pages = marks[other.uuid][0]
                if other.uuid in used or pages & covered or not all(_compatible(p, other) for p in parts):
                    continue
                parts.append(other)
                covered |= pages
            if len(parts) < 2:
                continue
            used.update(p.uuid for p in parts)
            complete = covered == set(range(1, total + 1))
            text = (
                f"These {len(parts)} documents show pages {_pages(covered)} of one {total}-page letter: "
                + "; ".join(f"“{p.title}” page {_pages(marks[p.uuid][0])}" for p in parts)
                + ". Merged, they become one document in page order."
            )
            if not complete:
                text += f" Page {_pages(set(range(1, total + 1)) - covered)} is still missing."
            findings.append(
                Finding(
                    "split",
                    "One letter split over several scans",
                    text,
                    [p.uuid for p in parts],
                    action=compose(parts, [parts]),
                    confidence="medium",
                    evidence="; ".join(f"{p.ref} pages {_pages(marks[p.uuid][0])} of {total}" for p in parts),
                )
            )
    # A continuation: scanned right after another, no letterhead of its own, the same reference.
    ordered = sorted(items, key=lambda i: i.uploaded_at)
    for before, after in itertools.pairwise(ordered):
        if before.uuid in used or after.uuid in used:
            continue
        if after.uploaded_at - before.uploaded_at > NEAR_UPLOAD or after.sender or after.day:
            continue
        shared = _references(before) & _references(after)
        starts_late = marks[after.uuid][0] and min(marks[after.uuid][0]) > 1
        if not shared and not starts_late:
            continue
        why = f"the same reference {sorted(shared)[0]}" if shared else "page numbers that go on"
        gap = int((after.uploaded_at - before.uploaded_at).total_seconds())
        findings.append(
            Finding(
                "split",
                "Possibly the rest of the previous scan",
                f"“{after.title}” was scanned right after “{before.title}”, has no sender or date of its own "
                f"and {why}. It may be the second part of the same letter.",
                [before.uuid, after.uuid],
                action=compose([before, after], [[before, after]]),
                confidence="low",
                evidence=f"{after.ref} uploaded {gap} s after {before.ref}; "
                f"{after.ref} has no sender and no date; {why}.",
            )
        )
        used.update((before.uuid, after.uuid))
    return findings


# --- Duplicates ---------------------------------------------------------------------------------


def shingles(text: str) -> set[int]:
    words = [tokenizer.fold(w) for w in tokenizer.words(text)]
    return {hash(" ".join(words[i : i + SHINGLE])) for i in range(max(0, len(words) - SHINGLE + 1))}


def _better(a: Item, b: Item) -> tuple[Item, Item]:
    """(keep, drop): more pages, then more text, then the earlier one."""
    rank = sorted((a, b), key=lambda i: (-i.pages, -len(i.text), i.uploaded_at))
    return rank[0], rank[1]


def duplicates(items: list[Item]) -> list[Finding]:
    """The same document twice: nearly the same text, or one text contained in the other."""
    sets = {i.uuid: shingles(i.text) for i in items if len(i.text) >= MIN_TEXT}
    frequency: Counter[int] = Counter(s for shingle_set in sets.values() for s in shingle_set)
    index: dict[int, list[str]] = defaultdict(list)
    for uuid, shingle_set in sets.items():
        for s in shingle_set:
            if frequency[s] <= COMMON_SHINGLE:
                index[s].append(uuid)
    shared: Counter[tuple[str, str]] = Counter()
    for uuids in index.values():
        for x in range(len(uuids)):
            for y in range(x + 1, len(uuids)):
                shared[tuple(sorted((uuids[x], uuids[y])))] += 1  # type: ignore[index]
    by_uuid = {i.uuid: i for i in items}
    from apps.documents.pages import text_fingerprint

    numbers = {i.uuid: text_fingerprint(i.text)[1] for i in items}
    findings = []
    done: set[str] = set()
    for (ua, ub), _ in shared.most_common():
        if ua in done or ub in done:
            continue
        if numbers[ua] != numbers[ub]:
            continue  # boilerplate can be identical while the amounts or references differ
        a, b = sets[ua], sets[ub]
        common = len(a & b)
        jaccard = common / len(a | b)
        contained = common / min(len(a), len(b))
        if jaccard < DUPLICATE and contained < CONTAINED:
            continue
        keep, drop = _better(by_uuid[ua], by_uuid[ub])
        if jaccard >= DUPLICATE:
            title = "Duplicate"
            text = (
                f"“{drop.title}” has the same text as “{keep.title}” ({round(jaccard * 100)} % alike). "
                f"Keep “{keep.title}” ({keep.pages} pages) and put the other in the trash."
            )
        else:
            title = "Contained in another document"
            text = (
                f"The text of “{drop.title}” is also in “{keep.title}” ({round(contained * 100)} % of it). "
                "It is probably a single page scanned again, or an earlier part."
            )
        findings.append(
            Finding(
                "duplicate",
                title,
                text,
                [keep.uuid, drop.uuid],
                action=compose([drop]),
                confidence="medium",  # OCR alone cannot confirm identical page images
                evidence=f"{keep.ref} and {drop.ref}: text {round(jaccard * 100)} % alike, "
                f"{round(contained * 100)} % of the smaller one in the larger; "
                f"pages {keep.pages} and {drop.pages}.",
            )
        )
        done.add(drop.uuid)
    findings.extend(_same_facts(items, done))
    return findings


def _same_facts(items: list[Item], done: set[str]) -> list[Finding]:
    """Different texts, same letter: the same sender, date, amount and reference (a scan and a PDF)."""
    by_facts: dict[tuple[str, date, str, frozenset[str]], list[Item]] = defaultdict(list)
    for i in items:
        amount = str(i.extracted.get("total_amount") or "")
        refs = _references(i)
        if i.uuid in done or not (i.sender and i.day and (amount or refs)):
            continue
        by_facts[(i.sender.casefold(), i.day, amount, frozenset(refs))].append(i)
    findings = []
    for members in by_facts.values():
        if len(members) < 2:
            continue
        keep, drop = _better(members[0], members[1])
        findings.append(
            Finding(
                "duplicate",
                "Possibly the same letter twice",
                f"“{keep.title}” and “{drop.title}” have the same sender, date, amount and references, "
                "though their texts differ (e.g. a scan and an emailed PDF).",
                [keep.uuid, drop.uuid],
                action=compose([drop]),
                confidence="low",
                evidence=f"{keep.ref} and {drop.ref}: sender {keep.sender}, date {keep.day}, "
                f"amount {keep.extracted.get('total_amount') or '—'}, "
                f"references {sorted(_references(keep))}.",
            )
        )
    return findings


# --- Series and details ------------------------------------------------------------------------


def _months(d: date) -> int:
    return d.year * 12 + d.month - 1


def gaps(items: list[Item]) -> list[Finding]:
    """A monthly or quarterly run of documents with months in between that are not there."""
    runs: dict[tuple[Any, ...], list[Item]] = defaultdict(list)
    for i in items:
        if not i.day:
            continue
        key: tuple[Any, ...] = ("series", i.series_id) if i.series_id else ("sender", i.sender, i.type_name)
        if key[1:] != ("", ""):
            runs[key].append(i)
    findings = []
    for members in runs.values():
        months = sorted({_months(i.day) for i in members if i.day})
        if len(months) < 3:
            continue
        step = statistics.median(b - a for a, b in itertools.pairwise(months))
        if step not in (1, 3):
            continue
        expected = set(range(months[0], months[-1] + 1, int(step)))
        missing = sorted(expected - set(months))
        if not missing or len(missing) > MAX_GAP_MONTHS or len(missing) > len(months):
            continue
        names = ", ".join(f"{MONTH_NAMES[m % 12]} {m // 12}" for m in missing)
        first = members[0]
        what = first.series_name or " · ".join(x for x in (first.sender, first.type_name) if x)
        findings.append(
            Finding(
                "gap",
                "Gaps in a series",
                f"“{what}” comes {'monthly' if step == 1 else 'quarterly'}, but {names} "
                f"{'is' if len(missing) == 1 else 'are'} missing.",
                [i.uuid for i in sorted(members, key=lambda i: i.day or date.min)],
                confidence="medium",
                evidence=f"{len(members)} documents, dated "
                + ", ".join(sorted({i.day.strftime("%Y-%m") for i in members if i.day})),
            )
        )
    return findings


def missing_info(items: list[Item]) -> list[Finding]:
    """Documents the processing could not read or name."""
    unread = [i for i in items if len(i.text.strip()) < MIN_TEXT]
    undated = [i for i in items if not i.day and i not in unread]
    unsigned = [i for i in items if not i.sender and i not in unread]
    findings = []
    if unread:
        findings.append(
            Finding(
                "missing_info",
                "No readable text",
                f"{len(unread)} document(s) have (almost) no text, so search and the AI model "
                "cannot read them. "
                "Reprocessing with OCR, or a better scan, helps.",
                [i.uuid for i in unread],
                confidence="high",
            )
        )
    for missing, what in ((undated, "date"), (unsigned, "sender")):
        if missing:
            findings.append(
                Finding(
                    "missing_info",
                    f"Without a {what}",
                    f"{len(missing)} document(s) have no {what}. The notes below may say what it is.",
                    [i.uuid for i in missing],
                    confidence="high",
                )
            )
    return findings


def related(items: list[Item]) -> list[Finding]:
    """Documents of different kinds that name the same contract, customer or policy number."""
    by_ref: dict[str, list[Item]] = defaultdict(list)
    for i in items:
        for ref in _references(i):
            by_ref[ref].append(i)
    findings = []
    seen: set[frozenset[str]] = set()
    for ref, members in by_ref.items():
        members = list({m.uuid: m for m in members}.values())
        kinds = {(m.sender, m.type_name) for m in members}
        uuids = frozenset(m.uuid for m in members)
        if len(members) < 2 or len(kinds) < 2 or uuids in seen or len(members) > 20:
            continue
        seen.add(uuids)
        findings.append(
            Finding(
                "related",
                "Belong together",
                f"These documents all name {ref.upper()}: "
                + ", ".join(f"“{m.title}”" for m in members[:8])
                + ". A shared tag or folder keeps them together.",
                [m.uuid for m in members],
                confidence="medium",
                evidence=f"reference {ref.upper()} in " + ", ".join(m.ref for m in members),
            )
        )
    return findings


TEXT_CHECKS = (duplicates, split_scans, missing_pages)  # for documents without page fingerprints
ALL_CHECKS = (gaps, missing_info, related)


def run_all(items: list[Item], *, without_pages: list[Item] | None = None) -> list[Finding]:
    texts = items if without_pages is None else without_pages
    return [f for check in TEXT_CHECKS for f in check(texts)] + [
        f for check in ALL_CHECKS for f in check(items)
    ]
