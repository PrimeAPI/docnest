"""The review: the AI model looks through documents for hours and leaves a report.

Meant to run overnight with a mid-sized local model (an 8B one): time is plentiful, the
model's context and judgment are not. So it is never asked to do much at once. It works in
phases, each a series of small questions with a JSON-schema answer:

1. **Read** — the documents, their text, page marks and extracted details (code).
2. **Understand** — the model reads each document and writes a note: what it is, who wrote
   it, references, whether it reads as complete. Notes are kept (`apps.assist.notes`):
   the next review starts from them.
3. **Check** — code looks for duplicates, letters split over several scans, missing pages,
   gaps in monthly series, missing details, documents sharing a reference (`checks`).
4. **Names** — consistent titles per group (`tasks.rename`), new titles for meaningless ones.
5. **Argue** — every finding with consequences is argued about: a critic looks for what
   could make it wrong, a defender answers, a judge decides. What is dropped stays visible.
6. **Explore** — with the time that is left, the model works through the user's task step
   by step: it reads, compares and searches documents and proposes changes, keeping a
   short memo instead of a growing chat. Every proposal is argued about like the rest.
7. **Report** — an overview and the next steps, from the notes and what was kept.

Nothing is changed: the report lists findings, many with a change the user can apply. A
run is resumable — its progress is checkpointed — so a restarted worker carries on, and it
stops by the time the user set ("until 07:00"), writing the report with what it has.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from datetime import datetime
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.analysis import ai
from apps.assist import checks, notes, page_analysis, tasks
from apps.assist.checks import Finding, Item
from apps.assist.models import AssistTask
from apps.crypto.aead import decrypt_bytes, encrypt_bytes
from apps.documents import crypto_fields, pages
from apps.documents.models import Document
from apps.processing import pipeline
from apps.processing.preferences import get_ai_model
from apps.storage.backends import StorageError

logger = logging.getLogger(__name__)

MAX_DOCUMENTS = 5000  # the whole archive: the code checks all; the model reads what time allows
JOURNAL_AAD = b"assist-journal"
STATE_AAD = b"assist-state"
MAX_JOURNAL = 3000
DEFAULT_CONTEXT = 8192  # tokens: room for a page or two of text; an 8B model needs ~1.5 GB more
CHARS_PER_TOKEN = 2  # OCR text with its errors and numbers, conservatively
PROMPT_RESERVE = 2000  # tokens for the instructions and the answer
LIST_SHARE = 0.35  # of the context for the list of documents in a step
PART_CHARS = 2500  # a "read" step of the exploration
MAX_CRASHES = 3  # the model stopped this often in a row: the run fails
MAX_REPEATS = 3  # the same step again and again: the exploration ends
STEPS_PER_DOCUMENT = 4
MIN_STEPS = 20
MAX_MEMO = 1200
MAX_FINDING_LINES = 40
DEFAULT_TASK = (
    "Make these documents tidy: consistent, clear names; find duplicates, letters split over "
    "several scans, missing pages and gaps; note anything else that looks wrong or incomplete."
)
GENERIC_TITLE = re.compile(
    r"^\s*(scan|scanned|dokument|document|img|image|bild|datei|file|unbenannt|untitled|neu|new|"
    r"seite|page|brief|letter|\d[\d\s._-]*)\b[\s\d._-]*$",
    re.I,
)
ARGUED = {
    "duplicate",
    "duplicate_pages",
    "split",
    "split_inside",
    "missing_pages",
    "gap",
    "related",
    "names",
    "note",
    "fix",
}
CERTAIN = {"duplicate", "duplicate_pages"}  # from page fingerprints with high confidence: not argued

SYSTEM = (
    "You help a private person keep their document archive in order. The documents are mostly German. "
    "You are careful: you only claim what the documents show. You answer with JSON only."
)

UNDERSTAND = """\
Read this document closely. It is one of {count} documents the user asked you to look through.
What DocNest knows: title "{title}", sender {sender}, date {date}, type {type}, {pages} page(s){marks}.

Text (from OCR, may contain recognition errors{cut}):
\"\"\"
{text}
\"\"\"

Answer:
- what: one sentence — what this document is and what it is about
- sender: who wrote it (the organisation or person in the letterhead); recipient: to whom
- date: the date of the document as YYYY-MM-DD, or ""
- period: the time it is about ("2026-03", "2025"), or ""
- references: numbers that identify a contract, customer, invoice, policy or case
- complete: "yes", "no" or "unsure" — is this the whole document? "no" when it starts or ends
  in the middle, or its page numbers name pages that are not there
- why: one sentence on why you think it is complete or not
- title: a short, clear German title for it, at most 6 words, without a date"""

PLAN = """\
You will look through the user's documents tonight, step by step. You have many hours.
The user's task: "{task}"

The documents (ref · title · sender · date · pages · what it is):
{documents}

What code already found:
{findings}

Write your plan: 3 to 8 short steps, in the order you will do them. Focus on what the code
cannot see: documents that belong together, wrong or unclear names, wrong senders or dates,
documents that seem incomplete."""

STEP = """\
You are looking through the user's documents, one step at a time. Take your time and be careful.
The user's task: "{task}"

Documents (ref · title · sender · date · pages · what it is):
{documents}

Already found — do not propose these again:
{findings}

Your plan:
{plan}

Your memo (your own notes from earlier steps):
{memo}

Your last step: {last}
Its result:
{result}

{left} steps left. Choose the next one:
- "read": read the text of a document ("doc"; "part" 1, 2, … for long ones)
- "compare": compare two documents ("doc" and "other")
- "search": find documents whose text has some words ("words")
- "propose": propose something for the user ("kind", "documents", "value", "reason"):
  kind "rename" (value: the new title of one document), "sender" (value: the right sender),
  "tag" (value: a tag for the documents), "merge" (documents: the parts in page order),
  "delete" (documents: the one to keep first, then the duplicate to delete),
  "note" (something the user should know, e.g. a page that seems missing; value: the note)
- "finish": nothing more is worth doing
Answer with "thought" (your reasoning, 1–3 sentences), the step and its fields, and "memo":
your updated notes, at most 8 short lines, keeping what you will still need."""

CRITIC = """\
An assistant proposes this for the user's documents:
PROPOSAL: {title} — {text}
If the user agrees: {action}

Evidence: {evidence}
The documents:
{documents}

Argue AGAINST the proposal as well as you can. What could make it wrong or harmful? Look at
senders, dates, page numbers, amounts, references and what the documents are about. If the
evidence clearly supports it, say that there is little against it.
Answer: "objections" (short sentences) and "strength": "none", "weak" or "strong"."""

DEFENDER = """\
An assistant proposes this for the user's documents:
PROPOSAL: {title} — {text}
If the user agrees: {action}

Evidence: {evidence}
The documents:
{documents}

A critic objects:
{objections}

Answer the objections honestly, from the evidence. Concede the ones that are right.
Answer: "reply" (2–4 sentences)."""

JUDGE = """\
Decide about a proposal for the user's documents.
PROPOSAL: {title} — {text}
If the user agrees: {action}

Evidence: {evidence}
The documents:
{documents}

Against it: {objections}
For it: {reply}

Deleting or merging documents is hard to undo: keep such a proposal only when the evidence is
clear. A note costs nothing: keep it when it is plausible and useful.
Answer: "verdict": "keep", "drop" or "unsure", and "reason" (one sentence)."""

REPORT = """\
You looked through {count} documents for the user. Their task: "{task}"

The documents (what they are):
{documents}

What you found and kept:
{findings}

Write the report's start for the user, in plain English:
- "summary": 2–5 sentences — what these documents are, what state they are in, what matters most
- "next_steps": up to 5 short things the user should do, most important first"""


class Stopped(Exception):
    """The user said stop, or the time is up: write the report with what there is."""


@dataclasses.dataclass
class Options:
    think: bool = False
    until: datetime | None = None
    context: int = DEFAULT_CONTEXT
    explore: bool = True
    ai: bool = True  # False: only what code finds — minutes, no model

    @classmethod
    def of(cls, task: AssistTask) -> Options:
        o = task.options or {}
        until = parse_datetime(o["until"]) if isinstance(o.get("until"), str) else None
        context = o.get("context")
        return cls(
            think=bool(o.get("think")),
            until=until,
            context=context if isinstance(context, int) and 2048 <= context <= 65536 else DEFAULT_CONTEXT,
            explore=o.get("explore", True) is not False,
            ai=o.get("ai", True) is not False,
        )


def _encrypt(value: Any, aad: bytes) -> bytes:
    return encrypt_bytes(json.dumps(value, ensure_ascii=False).encode(), aad=aad)


def _decrypt(raw: bytes | memoryview | None, aad: bytes, default: Any) -> Any:
    return json.loads(decrypt_bytes(bytes(raw), aad=aad)) if raw else default


def journal_of(task: AssistTask) -> list[dict[str, str]]:
    return _decrypt(task.journal_enc, JOURNAL_AAD, [])


class Run:
    def __init__(self, task: AssistTask, model: str) -> None:
        self.task = task
        self.model = model
        self.options = Options.of(task)
        self.instruction = tasks.instruction_of(task) or DEFAULT_TASK
        self.journal: list[dict[str, str]] = journal_of(task)
        state = _decrypt(task.state_enc, STATE_AAD, {})
        self.phases: list[str] = state.get("phases", [])
        self.findings = [Finding(**f) for f in state.get("findings", [])]
        self.plan: list[str] = state.get("plan", [])
        self.memo: str = state.get("memo", "")
        self.steps: int = state.get("steps", 0)
        self.last: tuple[str, str] = tuple(state.get("last", ("none yet", "")))  # type: ignore[assignment]
        self.document_order: list[str] = state.get("document_order", [])
        self.crashes = 0
        self.items: list[Item] = []
        self.documents: dict[str, Document] = {}
        self.notes: dict[str, dict[str, Any]] = {}

    # -- bookkeeping

    def log(self, text: str) -> None:
        self.journal.append({"at": timezone.now().isoformat(timespec="seconds"), "text": text[:1000]})
        self.journal = self.journal[-MAX_JOURNAL:]
        AssistTask.objects.filter(pk=self.task.pk).update(journal_enc=_encrypt(self.journal, JOURNAL_AAD))

    def doing(self, step: str) -> None:
        AssistTask.objects.filter(pk=self.task.pk).update(step=step[:200])

    def save(self) -> None:
        state = {
            "phases": self.phases,
            "findings": [dataclasses.asdict(f) for f in self.findings],
            "plan": self.plan,
            "memo": self.memo,
            "steps": self.steps,
            "last": list(self.last),
            "document_order": self.document_order,
        }
        AssistTask.objects.filter(pk=self.task.pk).update(state_enc=_encrypt(state, STATE_AAD))

    def expect(self, more: int) -> None:
        self.task.total += more
        AssistTask.objects.filter(pk=self.task.pk).update(total=self.task.total)

    def check_stop(self) -> None:
        options = AssistTask.objects.filter(pk=self.task.pk).values_list("options", flat=True).first() or {}
        if options.get("stop"):
            raise Stopped("Stopped by you")
        if self.options.until and timezone.now() >= self.options.until:
            raise Stopped(f"Time is up ({timezone.localtime(self.options.until):%H:%M})")

    def ask(self, prompt: str, schema: dict[str, Any], *, think: bool | None = None) -> dict[str, Any] | None:
        """One question; None when the answer was unusable (logged, the run goes on)."""
        self.check_stop()
        try:
            answer = ai.ask(
                self.model,
                prompt,
                schema,
                system=SYSTEM,
                think=self.options.think if think is None else think,
                context=self.options.context,
                timeout=self.request_timeout(),
            )
        except ai.ModelUnavailable as exc:
            self.check_stop()
            self.save()
            self.log(f"The AI model is not available ({exc}); trying again in a few minutes.")
            raise pipeline.AnalysisModelUnavailable(str(exc)) from exc
        except ai.ModelCrashed as exc:
            self.crashes += 1
            self.log(f"The AI model stopped while answering: {exc}")
            if self.crashes >= MAX_CRASHES:
                raise
            return None
        except ai.ModelFailed as exc:
            self.log(f"An unusable answer, skipped: {exc}")
            return None
        finally:
            self.task.done += 1
            AssistTask.objects.filter(pk=self.task.pk).update(done=self.task.done)
        self.crashes = 0
        return answer

    def request_timeout(self) -> float | None:
        self.check_stop()
        if self.options.until:
            return max(0.1, (self.options.until - timezone.now()).total_seconds())
        return None

    def add(self, finding: Finding) -> Finding | None:
        if any(f.key == finding.key for f in self.findings):
            return None
        finding.id = f"F{len(self.findings) + 1}"
        self.findings.append(finding)
        return finding

    # -- what the model sees

    def by_ref(self, ref: str) -> Item | None:
        ref = ref.strip().upper()
        return next((i for i in self.items if i.ref == ref), None)

    def ref_of(self, uuid: str) -> str:
        return next((i.ref for i in self.items if i.uuid == uuid), "?")

    def line(self, item: Item, *, what: bool = True) -> str:
        note = self.notes.get(item.uuid, {})
        parts = [
            item.ref,
            f"“{item.title}”",
            item.sender or "no sender",
            item.day.isoformat() if item.day else "no date",
            f"{item.pages} p.",
        ]
        if what and note.get("what"):
            parts.append(str(note["what"])[:160])
        if note.get("complete") == "no":
            parts.append(f"seems incomplete: {str(note.get('why') or '')[:100]}")
        return " · ".join(parts)

    def document_lines(self, uuids: list[str] | None = None) -> str:
        """The documents, one per line — shorter lines, then fewer, when they would not fit."""
        items = self.items if uuids is None else [i for i in self.items if i.uuid in uuids]
        budget = int(self.options.context * CHARS_PER_TOKEN * LIST_SHARE)
        lines = [self.line(i) for i in items]
        if sum(len(x) + 1 for x in lines) > budget:
            lines = [self.line(i, what=False) for i in items]
        shown: list[str] = []
        used = 0
        for line in lines:
            used += len(line) + 1
            if used > budget:
                shown.append(f"… and {len(lines) - len(shown)} more: find them with search")
                break
            shown.append(line)
        return "\n".join(shown) or "(none)"

    def finding_lines(self) -> str:
        lines = [
            f"- {f.id} {f.title}: {', '.join(self.ref_of(u) for u in f.documents[:8])}"
            + (f" ({f.verdict})" if f.verdict else "")
            for f in self.findings
        ]
        if len(lines) > MAX_FINDING_LINES:
            lines = [*lines[:MAX_FINDING_LINES], f"- … and {len(lines) - MAX_FINDING_LINES} more"]
        return "\n".join(lines) or "(nothing yet)"

    def text_budget(self) -> int:
        return max(1500, (self.options.context - PROMPT_RESERVE) * CHARS_PER_TOKEN)


# --- Phases ---------------------------------------------------------------------------------------


def load(run: Run) -> None:
    documents = list(
        Document.objects.filter(uuid__in=run.task.documents[:MAX_DOCUMENTS], deleted_at__isnull=True)
        .select_related("correspondent", "document_type", "series", "folder")
        .prefetch_related("tags")
        .order_by("document_date", "uploaded_at")
    )
    if not run.document_order:
        run.document_order = [str(d.uuid) for d in documents]
    order = {u: n for n, u in enumerate(run.document_order, start=1)}
    documents.sort(key=lambda d: order[str(d.uuid)])
    for d in documents:
        uuid = str(d.uuid)
        run.documents[uuid] = d
        run.items.append(
            Item(
                uuid=uuid,
                ref=f"D{order[uuid]}",
                title=crypto_fields.get_title(d),
                sender=d.correspondent.name if d.correspondent else "",
                type_name=d.document_type.name if d.document_type else "",
                day=d.document_date,
                pages=d.page_count,
                uploaded_at=d.uploaded_at,
                text=crypto_fields.get_content(d),
                extracted=crypto_fields.get_extracted(d),
                series_id=d.series_id,
                series_name=d.series.name if d.series else "",
                tags=[t.name for t in d.tags.all()],
                folder=d.folder.name if d.folder else "",
            )
        )
        note = notes.load(d, run.items[-1].text, run.model)
        if note:
            run.notes[uuid] = note
    if "read" not in run.phases:
        pages = sum(i.pages for i in run.items)
        run.log(f"Read {len(run.items)} documents with {pages} pages.")
        run.phases.append("read")
        run.save()


NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "what": {"type": "string"},
        "sender": {"type": "string"},
        "recipient": {"type": "string"},
        "date": {"type": "string"},
        "period": {"type": "string"},
        "references": {"type": "array", "items": {"type": "string"}},
        "complete": {"type": "string", "enum": ["yes", "no", "unsure"]},
        "why": {"type": "string"},
        "title": {"type": "string"},
    },
    "required": ["what", "sender", "date", "references", "complete", "why", "title"],
}


def excerpt(text: str, budget: int) -> tuple[str, bool]:
    """The text, or its start and end when too long: the end tells whether something is cut off."""
    text = re.sub(r"[ \t]{3,}", "  ", re.sub(r"\n{3,}", "\n\n", text)).strip()
    if len(text) <= budget:
        return text, False
    head = int(budget * 0.75)
    return text[:head] + "\n[…]\n" + text[-(budget - head) :], True


def understand(run: Run, only: set[str] | None = None) -> None:
    todo = [
        i
        for i in run.items
        if i.uuid not in run.notes
        and len(i.text.strip()) >= checks.MIN_TEXT
        and (only is None or i.uuid in only)
    ]
    known = sum(1 for i in run.items if i.uuid in run.notes)
    if known and "read-notes" not in run.phases:
        run.log(f"{known} document(s) were read in an earlier run; their notes are used.")
        run.phases.append("read-notes")
    run.expect(len(todo))
    for n, item in enumerate(todo, start=1):
        run.doing(f"Reading {item.ref} “{item.title}” ({n} of {len(todo)})")
        seen, total = checks.page_marks(item.text)
        text, cut = excerpt(item.text, run.text_budget())
        answer = run.ask(
            UNDERSTAND.format(
                count=len(run.items),
                title=item.title,
                sender=item.sender or "unknown",
                date=item.day.isoformat() if item.day else "unknown",
                type=item.type_name or "unknown",
                pages=item.pages,
                marks=f", page marks {', '.join(map(str, sorted(seen)))} of {total}" if total else "",
                cut=", shortened in the middle" if cut else "",
                text=text,
            ),
            NOTE_SCHEMA,
        )
        if not answer:
            continue
        note = {k: answer.get(k) for k in notes.FIELDS}
        note["title"] = ai.clean(note.get("title"), limit=80)
        notes.save(run.documents[item.uuid], note, item.text, run.model)
        run.notes[item.uuid] = note
        if note.get("complete") == "no":
            run.log(f"{item.ref} “{item.title}” seems incomplete: {note.get('why') or ''}")


def understand_involved(run: Run) -> None:
    """First the documents the findings are about: arguing about them needs to know them."""
    understand(run, {u for f in run.findings for u in f.documents[:6]})
    run.phases.append("understand-involved")
    run.save()


def understand_rest(run: Run) -> None:
    understand(run)
    run.log(f"Understood {len(run.notes)} of {len(run.items)} documents.")
    notice(run)
    run.phases.append("understand")
    run.save()


def notice(run: Run) -> None:
    """What the model noticed reading: incomplete documents the page marks did not show."""
    marked = {
        u for f in run.findings if f.kind in ("missing_pages", "split", "split_inside") for u in f.documents
    }
    for item in run.items:
        note = run.notes.get(item.uuid, {})
        if note.get("complete") == "no" and item.uuid not in marked:
            run.add(
                Finding(
                    "missing_pages",
                    "Seems incomplete",
                    f"Reading “{item.title}”, the AI model found: {note.get('why') or 'it seems cut off'}",
                    [item.uuid],
                    confidence="low",
                    source="model",
                    evidence=f"{item.ref}: {item.pages} page(s); the model's reading: {note.get('why')}",
                )
            )


def check(run: Run) -> None:
    run.doing("Comparing the pages and documents")
    found, covered = page_analysis.analyse(run.items, run.documents)
    without = [i for i in run.items if i.uuid not in covered]
    if without:
        run.log(
            f"{len(without)} document(s) have no page fingerprints yet (Assistant → Prepare pages); "
            "they are compared by their text."
        )
    found += checks.run_all(run.items, without_pages=without)
    added = [f for f in found if run.add(f)]
    counts: dict[str, int] = {}
    for f in added:
        counts[f.title] = counts.get(f.title, 0) + 1
    summary = ", ".join(f"{n}× {t.lower()}" for t, n in counts.items())
    run.log(f"Compared the documents page by page: {summary or 'nothing stood out'}.")
    run.phases.append("check")
    run.save()


def prepare_pages(run: Run) -> None:
    """Include older documents in page comparisons without a separate preparation step."""
    for n, document in enumerate(run.documents.values(), start=1):
        run.check_stop()
        if document.processing_state != Document.State.DONE or not document.page_count:
            continue
        run.doing(f"Preparing pages ({n} of {len(run.documents)})")
        try:
            pages.ensure(document)
        except (FileNotFoundError, StorageError, RuntimeError) as exc:
            run.log(f"Pages unavailable for {run.ref_of(str(document.uuid))}: {exc}. Using text checks.")
    run.phases.append("prepare-pages")
    run.save()


def names(run: Run) -> None:
    run.doing("Finding consistent names")
    documents = [run.documents[i.uuid] for i in run.items]
    try:
        result = tasks.rename(run.task, documents, "", run.model, before_request=run.request_timeout)
    except ai.ModelUnavailable as exc:
        run.check_stop()
        run.save()
        raise pipeline.AnalysisModelUnavailable(str(exc)) from exc
    except (ai.ModelFailed, ai.ModelCrashed) as exc:
        run.log(f"Could not find consistent names: {exc}")
        result = tasks.Result()
    for group in result.groups:
        uuids = [item.document for item in group.items]
        run.add(
            Finding(
                "names",
                "Consistent names",
                f"{len(uuids)} documents that belong together, named in one pattern: {group.label}.",
                uuids,
                action={"type": "changes", "groups": [dataclasses.asdict(group)]},
                confidence="medium",
                evidence="; ".join(
                    f"{run.ref_of(i.document)} “{i.old}” → “{i.new}”" for i in group.items[:10]
                ),
            )
        )
    # Meaningless titles ("Scan 2026-01-03") get the title the model gave reading them.
    renamed = {u for f in run.findings if f.kind == "names" for u in f.documents}
    for item in run.items:
        title = (run.notes.get(item.uuid) or {}).get("title")
        if (
            item.uuid in renamed
            or not title
            or not (GENERIC_TITLE.match(item.title) or not item.title.strip())
        ):
            continue
        group = tasks.ChangeGroup("New title", "title", [tasks.Item(item.uuid, item.title, title)])
        run.add(
            Finding(
                "names",
                "A title that says what it is",
                f"“{item.title}” says nothing about the document. Reading it suggests “{title}”.",
                [item.uuid],
                action={"type": "changes", "groups": [dataclasses.asdict(group)]},
                confidence="medium",
                source="model",
                evidence=f"{item.ref}: {(run.notes.get(item.uuid) or {}).get('what', '')}",
            )
        )
    run.log(f"Looked at the names: {sum(f.kind == 'names' for f in run.findings)} suggestion(s).")
    run.phases.append("names")
    run.save()


def describe_action(run: Run, finding: Finding) -> str:
    action = finding.action or {}
    kind = action.get("type")
    if kind == "compose":
        outputs = action.get("outputs") or []
        kept = {u for o in outputs for u, _ in o["pages"]}
        trashed = [run.ref_of(u) for u in action["sources"] if u not in kept]
        if not outputs:
            return f"{', '.join(trashed)} go(es) to the trash (it can be restored)."
        made = [
            "a document of " + ", ".join(f"{run.ref_of(u)} p.{n}" for u, n in o["pages"][:30])
            for o in outputs
        ]
        return "; ".join(made) + (f"; {', '.join(trashed)} go(es) to the trash" if trashed else "")
    if kind == "changes":
        lines = []
        for group in action["groups"]:
            for item in group["items"][:10]:
                new = ", ".join(item["new"]) if isinstance(item["new"], list) else item["new"]
                lines.append(f"{run.ref_of(item['document'])} {group['field']} “{item['old']}” → “{new}”")
        return "; ".join(lines)
    return "nothing changes; the user is told."


def argue(run: Run, finding: Finding) -> None:
    """Critic, defender, judge: the finding is kept, dropped or left unsure."""
    involved = finding.documents[:6]
    facts = {
        "title": finding.title,
        "text": finding.text,
        "action": describe_action(run, finding),
        "evidence": finding.evidence or "(only the documents)",
        "documents": run.document_lines(involved),
    }
    run.doing(f"Arguing about {finding.id}: {finding.title}")
    critic = run.ask(
        CRITIC.format(**facts),
        {
            "type": "object",
            "properties": {
                "objections": {"type": "array", "items": {"type": "string"}},
                "strength": {"type": "string", "enum": ["none", "weak", "strong"]},
            },
            "required": ["objections", "strength"],
        },
    )
    if critic is None:
        return
    objections = [str(o) for o in critic.get("objections") or [] if str(o).strip()][:5]
    strength = critic.get("strength") if critic.get("strength") in ("none", "weak", "strong") else "weak"
    finding.debate.append({"role": "against", "text": " ".join(objections) or "Nothing against it."})
    reply = ""
    if objections:
        defender = run.ask(
            DEFENDER.format(**facts, objections="\n".join(f"- {o}" for o in objections)),
            {"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"]},
        )
        reply = str((defender or {}).get("reply") or "").strip()
        if reply:
            finding.debate.append({"role": "for", "text": reply})
    judge = run.ask(
        JUDGE.format(**facts, objections="; ".join(objections) or "nothing", reply=reply or "(no reply)"),
        {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["keep", "drop", "unsure"]},
                "reason": {"type": "string"},
            },
            "required": ["verdict", "reason"],
        },
        think=run.options.think,
    )
    if judge is None:
        return
    verdict = str(judge.get("verdict")) if judge.get("verdict") in ("keep", "drop", "unsure") else "unsure"
    finding.debate.append({"role": "verdict", "text": str(judge.get("reason") or "")})
    finding.verdict = verdict
    if verdict == "unsure":
        finding.confidence = "low"
    elif verdict == "keep" and strength == "none" and finding.confidence == "medium":
        finding.confidence = "high"
    elif verdict == "keep" and strength == "strong":
        finding.confidence = "low"
    run.log(f"{finding.id} {finding.title}: {verdict} — {judge.get('reason') or ''}")
    run.save()


def argue_all(run: Run) -> None:
    todo = [
        f
        for f in run.findings
        if f.kind in ARGUED and not f.verdict and not (f.kind in CERTAIN and f.confidence == "high")
    ]
    run.expect(len(todo) * 3)
    for finding in todo:
        argue(run, finding)
    if todo:
        kept = sum(f.verdict == "keep" for f in todo)
        dropped = sum(f.verdict == "drop" for f in todo)
        run.log(f"Argued about {len(todo)} finding(s): {kept} kept, {dropped} dropped.")
    run.phases.append("argue")
    run.save()


def argue_again(run: Run) -> None:
    argue_all(run)
    run.phases.append("argue-again")
    run.save()


# --- Exploring: one step at a time ------------------------------------------------------------

STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "action": {"type": "string", "enum": ["read", "compare", "search", "propose", "finish"]},
        "doc": {"type": "string"},
        "other": {"type": "string"},
        "part": {"type": "integer"},
        "words": {"type": "string"},
        "kind": {"type": "string", "enum": ["rename", "sender", "tag", "merge", "delete", "note"]},
        "documents": {"type": "array", "items": {"type": "string"}},
        "value": {"type": "string"},
        "reason": {"type": "string"},
        "memo": {"type": "string"},
    },
    "required": ["thought", "action", "memo"],
}


def tool_read(run: Run, step: dict[str, Any]) -> str:
    item = run.by_ref(str(step.get("doc") or ""))
    if item is None:
        return "There is no such document. Use a ref like D3."
    text = re.sub(r"\n{3,}", "\n\n", item.text).strip()
    parts = max(1, -(-len(text) // PART_CHARS))
    part = min(max(1, int(step.get("part") or 1)), parts)
    chunk = text[(part - 1) * PART_CHARS : part * PART_CHARS]
    note = run.notes.get(item.uuid, {})
    head = f"{run.line(item, what=False)} — part {part} of {parts}."
    if note:
        refs = ", ".join(note.get("references") or [])
        head += f" Your earlier note: {note.get('what', '')} References: {refs}."
    return f'{head}\n"""\n{chunk}\n"""'


def tool_compare(run: Run, step: dict[str, Any]) -> str:
    a, b = run.by_ref(str(step.get("doc") or "")), run.by_ref(str(step.get("other") or ""))
    if a is None or b is None or a is b:
        return "Name two different documents, e.g. doc D3 and other D7."
    sa, sb = checks.shingles(a.text), checks.shingles(b.text)
    common = len(sa & sb)
    alike = round(100 * common / max(1, len(sa | sb)))
    contained = round(100 * common / max(1, min(len(sa), len(sb))))
    ma, mb = checks.page_marks(a.text), checks.page_marks(b.text)
    refs = sorted(checks._references(a) & checks._references(b))
    minutes = abs(int((b.uploaded_at - a.uploaded_at).total_seconds() // 60))
    return "\n".join(
        [
            run.line(a),
            run.line(b),
            f"Text {alike} % alike; {contained} % of the smaller one is in the larger.",
            f"Page marks: {a.ref} {sorted(ma[0]) or '—'} of {ma[1] or '—'}; "
            f"{b.ref} {sorted(mb[0]) or '—'} of {mb[1] or '—'}.",
            f"Same sender: {'yes' if a.sender and a.sender == b.sender else 'no'}; "
            f"same date: {'yes' if a.day and a.day == b.day else 'no'}; "
            f"shared references: {', '.join(refs) or 'none'}.",
            f"Scanned {minutes} minutes apart.",
        ]
    )


def tool_search(run: Run, step: dict[str, Any]) -> str:
    words = [w.casefold() for w in str(step.get("words") or "").split() if len(w) >= 3][:5]
    if not words:
        return "Give some words to search for."
    hits = [i for i in run.items if all(w in i.text.casefold() or w in i.title.casefold() for w in words)]
    if not hits:
        return f"No document has {' '.join(words)}."
    lines = []
    for i in hits[:15]:
        at = i.text.casefold().find(words[0])
        snippet = " ".join(i.text[max(0, at - 60) : at + 100].split()) if at >= 0 else ""
        lines.append(f"{run.line(i, what=False)} … {snippet} …")
    return f"{len(hits)} document(s):\n" + "\n".join(lines)


def propose(run: Run, step: dict[str, Any]) -> str:
    """Turn the model's proposal into a finding — checked by code first, then argued about."""
    kind = str(step.get("kind") or "")
    items = [run.by_ref(str(r)) for r in step.get("documents") or [step.get("doc") or ""]]
    docs = [i for i in items if i is not None]
    docs = list({i.uuid: i for i in docs}.values())
    value = " ".join(str(step.get("value") or "").split())[:200]
    reason = " ".join(str(step.get("reason") or "").split())[:500]
    if not docs:
        return "Name the documents by their refs (D1, D2, …)."
    if not reason:
        return "Give a reason: the user decides by it."
    action: dict[str, Any] | None = None
    if kind == "merge":
        if len(docs) < 2:
            return "A merge needs at least two documents, in page order."
        action = checks.compose(docs, [docs])
        title = "One document in several scans"
    elif kind == "delete":
        if len(docs) != 2:
            return "For a duplicate, name two documents: the one to keep, then the one to delete."
        action = checks.compose([docs[1]])
        title = "Duplicate"
    elif kind in ("rename", "sender", "tag"):
        if not value:
            return "Give the new value."
        if kind == "rename" and len(docs) != 1:
            return "Rename one document at a time."
        field = {"rename": "title", "sender": "sender", "tag": "tags"}[kind]
        group = tasks.ChangeGroup(
            {"rename": "New title", "sender": f"Sender “{value}”", "tag": f"Add tag “{value}”"}[kind], field
        )
        for d in docs:
            old = {"title": d.title, "sender": d.sender, "tags": ", ".join(d.tags)}[field]
            if kind == "tag" and value.casefold() in {t.casefold() for t in d.tags}:
                continue
            if kind != "tag" and old.casefold() == value.casefold():
                continue
            group.items.append(tasks.Item(d.uuid, old, [value] if kind == "tag" else value))
        if not group.items:
            return "That is already so."
        action = {"type": "changes", "groups": [dataclasses.asdict(group)]}
        title = {"rename": "A better title", "sender": "Another sender", "tag": "A tag"}[kind]
    elif kind == "note":
        title = "Note"
    else:
        return "Choose a kind: rename, sender, tag, merge, delete or note."
    text = f"{value} — {reason}" if kind == "note" and value else reason
    finding = run.add(
        Finding(
            "fix" if action else "note",
            title,
            text,
            [d.uuid for d in docs],
            action=action,
            source="model",
            evidence=f"The assistant's reason: {reason}",
        )
    )
    if finding is None:
        return "That was proposed already."
    run.expect(3)
    argue(run, finding)
    verdict = {"keep": "kept", "drop": "dropped", "unsure": "kept as unsure"}.get(
        finding.verdict, "not judged"
    )
    reasons = " ".join(d["text"] for d in finding.debate)
    return f"Proposal {finding.id} was argued about and {verdict}. {reasons[:600]}"


TOOLS = {"read": tool_read, "compare": tool_compare, "search": tool_search, "propose": propose}


def make_plan(run: Run) -> None:
    run.doing("Making a plan")
    answer = run.ask(
        PLAN.format(task=run.instruction, documents=run.document_lines(), findings=run.finding_lines()),
        {
            "type": "object",
            "properties": {"plan": {"type": "array", "items": {"type": "string"}}},
            "required": ["plan"],
        },
    )
    run.plan = [" ".join(str(s).split())[:200] for s in (answer or {}).get("plan") or [] if str(s).strip()][
        :8
    ]
    if run.plan:
        run.log("Plan: " + " · ".join(f"{n}. {s}" for n, s in enumerate(run.plan, start=1)))
    run.save()


def explore(run: Run) -> None:
    if not run.plan:
        run.expect(1)
        make_plan(run)
    budget = max(MIN_STEPS, STEPS_PER_DOCUMENT * len(run.items))
    run.expect(max(0, budget - run.steps))
    repeats, previous = 0, ""
    while run.steps < budget:
        run.doing(f"Step {run.steps + 1}: {run.last[0][:120]}")
        answer = run.ask(
            STEP.format(
                task=run.instruction,
                documents=run.document_lines(),
                findings=run.finding_lines(),
                plan="\n".join(f"{n}. {s}" for n, s in enumerate(run.plan, start=1)) or "(none)",
                memo=run.memo or "(empty)",
                last=run.last[0],
                result=run.last[1] or "(none)",
                left=budget - run.steps,
            ),
            STEP_SCHEMA,
        )
        run.steps += 1
        if answer is None:
            continue
        run.memo = str(answer.get("memo") or run.memo)[:MAX_MEMO]
        action = str(answer.get("action") or "")
        if action == "finish":
            run.log(f"Finished exploring: {answer.get('thought') or ''}")
            break
        tool = TOOLS.get(action)
        args = {k: answer.get(k) for k in ("doc", "other", "part", "words", "kind", "documents", "value")}
        signature = json.dumps([action, args], sort_keys=True, default=str)
        repeats = repeats + 1 if signature == previous else 0
        previous = signature
        if repeats >= MAX_REPEATS:
            run.log("Kept repeating the same step; ending the exploration.")
            break
        result = tool(run, answer) if tool else "Choose read, compare, search, propose or finish."
        if repeats:
            result = "You did exactly this already. Do something else, or finish.\n" + result
        described = f"{action} " + " ".join(
            str(v) for k, v in args.items() if v not in (None, "", []) and k != "documents"
        )
        if action == "propose":
            described += " " + " ".join(str(d) for d in answer.get("documents") or [])
        run.last = (described.strip()[:300], result[:3000])
        run.log(f"{described.strip()} — {str(answer.get('thought') or '')[:300]}")
        run.save()
    run.phases.append("explore")
    run.save()


# --- The report --------------------------------------------------------------------------------


def report(run: Run, *, stopped: str = "") -> dict[str, Any]:
    summary, steps = "", []
    if not stopped and run.options.ai:
        run.doing("Writing the report")
        answer = run.ask(
            REPORT.format(
                count=len(run.items),
                task=run.instruction,
                documents=run.document_lines(),
                findings="\n".join(
                    f"- {f.title}: {f.text[:200]}" for f in run.findings if f.verdict != "drop"
                )[: int(run.options.context * CHARS_PER_TOKEN * LIST_SHARE)]
                or "(nothing)",
            ),
            {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "next_steps": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary", "next_steps"],
            },
        )
        summary = str((answer or {}).get("summary") or "")
        steps = [str(s) for s in (answer or {}).get("next_steps") or [] if str(s).strip()][:5]
    for f in run.findings:  # the model's own titles stay plain text; refs become readable
        f.text = _readable(run, f.text)
        f.debate = [{**d, "text": _readable(run, d["text"])} for d in f.debate]
    return {
        "summary": _readable(run, summary),
        "next_steps": [_readable(run, s) for s in steps],
        "findings": [dataclasses.asdict(f) for f in run.findings],
        "notes": {
            uuid: {k: note.get(k) for k in ("what", "complete", "why", "references")}
            for uuid, note in run.notes.items()
        },
        "stopped": stopped,
        "documents": len(run.items),
    }


def _readable(run: Run, text: str) -> str:
    """“D3” means nothing to the user: the document's title instead."""
    titles = {i.ref: i.title for i in run.items}
    return re.sub(r"\bD(\d{1,4})\b", lambda m: f"“{titles.get(m.group(0), m.group(0))}”", text)


# --- Running --------------------------------------------------------------------------------------


def run(task_id: int) -> None:
    task = AssistTask.objects.filter(pk=task_id, operation=AssistTask.Operation.REVIEW).first()
    # Running: a worker restarted, or the model was away. Carry on from the checkpoint.
    if task is None or task.state not in (AssistTask.State.PENDING, AssistTask.State.RUNNING):
        return
    model = task.model or (get_ai_model() if ai.configured() else "")
    if model and not task.model:
        task.model = model
        task.save(update_fields=["model"])
    if not model and (task.options or {}).get("ai", True) is not False:
        _finish(task, AssistTask.State.FAILED, error="Choose an AI model first (Settings → AI).")
        return
    if task.state == AssistTask.State.PENDING:
        task.state = AssistTask.State.RUNNING
        task.started_at = timezone.now()
        AssistTask.objects.filter(pk=task.pk).update(state=task.state, started_at=task.started_at)
    r = Run(task, model)
    if not r.journal:
        r.log(
            f"Started with {model}" + (", thinking before it answers." if r.options.think else ".")
            if r.options.ai
            else "Started: comparing pages and documents, without the AI model."
        )
    load(r)
    stopped = ""
    phases = [("prepare-pages", prepare_pages), ("check", check)]
    if r.options.ai:
        phases += [
            ("understand-involved", understand_involved),
            ("argue", argue_all),
            ("names", names),
            ("understand", understand_rest),
            ("argue-again", argue_again),
        ]
        if r.options.explore:
            phases.append(("explore", explore))
    try:
        for phase, step in phases:
            if phase not in r.phases:
                step(r)
    except Stopped as exc:
        stopped = str(exc)
        r.log(f"{stopped}: writing the report with what there is.")
        if "check" not in r.phases:
            check(r)
    except ai.ModelCrashed as exc:
        _fail(r, f"The AI model kept stopping: {exc}")
        return
    except pipeline.AnalysisModelUnavailable:
        raise  # the job is deferred and resumes here
    except Exception as exc:
        logger.exception("review failed", extra={"task": task.pk})
        _fail(r, f"{type(exc).__name__}: {exc}")
        return
    try:
        result = report(r, stopped=stopped)
    except Stopped as exc:  # the time ran out just now
        result = report(r, stopped=str(exc))
    except ai.ModelCrashed as exc:
        _fail(r, f"The AI model kept stopping: {exc}")
        return
    r.log("Report written.")
    task.result_enc = _encrypt(result, tasks.RESULT_AAD)
    _finish(task, AssistTask.State.DONE)


def _fail(run: Run, error: str) -> None:
    """Keep the findings already obtained even when the model fails later in the night."""
    run.save()
    run.log("Review failed; the findings collected so far are available.")
    run.task.result_enc = _encrypt(report(run, stopped="Review failed"), tasks.RESULT_AAD)
    _finish(run.task, AssistTask.State.FAILED, error=error[:500])


def _finish(task: AssistTask, state: str, *, error: str = "") -> None:
    AssistTask.objects.filter(pk=task.pk).update(
        state=state, error=error, result_enc=task.result_enc, finished_at=timezone.now(), step=""
    )
