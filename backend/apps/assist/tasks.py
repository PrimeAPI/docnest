"""The assistant: changes the AI model proposes for documents the user selected.

Nothing is changed here. A task produces groups of proposed changes — one per title
pattern, or per rule read from the user's wish — each listing documents with the old
and the new value and whether it is ticked. The user reviews them and the web app
applies the ticked ones like the user's own edits. The model is small and slow, so
it is asked for one small thing and the code does the rest:

- **Consistent names.** The documents are grouped like filing suggestions group them
  (series, sender and type, first title word); a lone document among groups is left
  alone. Per group the model gives one title pattern with date placeholders
  ("Verdienstabrechnung YYYY-MM") and the code fills in each document's date. A
  pattern the user wrote ("… als Verdienstabrechnung YYYY/MM") is used as written.
- **Custom.** One request turns the wish into rules — *tag: Gehalt*, *sender: ACME
  GmbH*, *type: invoice*, *title: …* — whose values must come from the wish. Which
  documents a rule is about is the code's call: the words around the value
  ("Stadtwerke", "Gehaltsabrechnungen") that match some of the selected documents;
  without such words, all of them. Every selected document is listed under each
  rule, the matching ones ticked.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from django.utils import timezone
from rapidfuzz import fuzz

from apps.analysis import ai
from apps.assist.models import AssistTask
from apps.crypto.aead import decrypt_bytes, encrypt_bytes
from apps.documents import crypto_fields
from apps.documents.models import Document
from apps.processing.preferences import get_ai_model
from apps.search import tokenizer
from apps.taxonomy import filing
from apps.taxonomy.models import DocumentType

logger = logging.getLogger(__name__)

MAX_DOCUMENTS = 100
RESULT_AAD = b"assist-result"
INSTRUCTION_AAD = b"assist-instruction"
MONTH_NAMES = "Januar Februar März April Mai Juni Juli August September Oktober November Dezember".split()  # noqa: SIM905
QUOTED = re.compile(r"[\"“„'‚‘»«]([^\"“”„'‚‘’»«]{3,120})[\"”“'’‘»«]")
# Words of a wish that say what to change, not which documents ("Füge allen … das Tag … hinzu").
_NOT_SCOPE = (
    "tag tags label labels schlagwort titel title name namen absender sender typ type art dokumenttyp "
    "dokument dokumente dokumenten datei dateien document documents file files alle allen all every"
)
NOT_SCOPE = frozenset(_NOT_SCOPE.split())
ALL = re.compile(r"\b(alle|allen|aller|sämtliche|all|every|everything)\b", re.I)
CLAUSES = re.compile(r"[,;]|\b(?:und|and|sowie)\b", re.I)
TYPE_SIMILARITY = 85


class TaskCancelled(Exception):
    pass


@dataclass
class Item:
    document: str  # uuid
    old: Any
    new: Any
    checked: bool = True


@dataclass
class ChangeGroup:
    label: str  # 'Pattern “Verdienstabrechnung YYYY-MM”', 'Add tag “Gehalt”'
    field: str  # title | sender | document_type | tags
    items: list[Item] = dataclasses.field(default_factory=list)


@dataclass
class Result:
    groups: list[ChangeGroup] = dataclasses.field(default_factory=list)
    note: str = ""


def encrypt_instruction(text: str) -> bytes | None:
    return encrypt_bytes(text.encode(), aad=INSTRUCTION_AAD) if text else None


def instruction_of(task: AssistTask) -> str:
    return (
        decrypt_bytes(bytes(task.instruction_enc), aad=INSTRUCTION_AAD).decode()
        if task.instruction_enc
        else ""
    )


def result_of(task: AssistTask) -> dict[str, Any]:
    if not task.result_enc:
        return {}
    return json.loads(decrypt_bytes(bytes(task.result_enc), aad=RESULT_AAD))


# --- Titles from a pattern ---------------------------------------------------------------------


def fill_pattern(pattern: str, day: date | None) -> str | None:
    """The title for a document of `day`; None when the pattern needs a date it does not have."""
    if ai.PATTERN_TOKEN.search(pattern) and day is None:
        return None
    if day is None:
        return " ".join(pattern.split())
    values = {
        "YYYY": f"{day.year:04d}",
        "MMMM": MONTH_NAMES[day.month - 1],
        "MM": f"{day.month:02d}",
        "DD": f"{day.day:02d}",
    }
    return " ".join(ai.PATTERN_TOKEN.sub(lambda m: values[m.group(1)], pattern).split())


# Words around a pattern that are not part of it ("… als Verdienstabrechnung YYYY/MM bitte").
_NOT_IN_PATTERN = (
    "als wie nach dem den der die das muster schema format name namen titel bitte für alle "
    "as like to the pattern format name names title please for all"
)
NOT_IN_PATTERN = frozenset(_NOT_IN_PATTERN.split())


def _pattern_word(word: str) -> bool:
    if ai.PATTERN_TOKEN.search(word) or not re.search(r"[A-Za-zÄÖÜäöüß0-9]", word):
        return True  # a placeholder ("YYYY/MM") or a separator ("–")
    bare = word.strip(".,;:!?()")
    return bool(bare) and bare[0].isupper() and bare.casefold() not in NOT_IN_PATTERN


def written_pattern(instruction: str) -> str | None:
    """A pattern the user wrote: in quotes (“Gehalt YYYY-MM”), or as capitalised words around
    the placeholders (… als Verdienstabrechnung YYYY/MM bitte). A small model cannot copy it reliably.
    """
    for match in QUOTED.finditer(instruction):
        if ai.PATTERN_TOKEN.search(match.group(1)) and (pattern := ai.clean_pattern(match.group(1))):
            return pattern
    words = instruction.split()
    marked = [i for i, w in enumerate(words) if ai.PATTERN_TOKEN.search(w)]
    if not marked:
        return None
    start, end = marked[0], marked[-1]
    if not all(_pattern_word(w) for w in words[start : end + 1]):
        return None
    while start > 0 and _pattern_word(words[start - 1]):
        start -= 1
    while end + 1 < len(words) and _pattern_word(words[end + 1]):
        end += 1
    return ai.clean_pattern(" ".join(words[start : end + 1]).strip(".,;:!?"))


def without_day(pattern: str, days: list[date | None]) -> str:
    """Drop DD when no two documents share a month: a monthly statement is named by its month."""
    months = [(d.year, d.month) for d in days if d]
    if "DD" not in pattern or len(months) != len(set(months)):
        return pattern
    return " ".join(
        re.sub(r"[-./]DD(?![A-Za-z])|(?<![A-Za-z])DD[-./]|(?<![A-Za-z])DD(?![A-Za-z])", "", pattern).split()
    )


def _rule_pattern(group: filing.Group) -> str | None:
    """Without a model: the group's common first title word, with the month where all have a date."""
    name = filing.rule_name(group)
    if not name:
        return None
    return f"{name} YYYY-MM" if all(d.year for d in group.docs) else name


# --- Running a task -------------------------------------------------------------------------------


def _tick(task: AssistTask) -> None:
    """Count one answered request; stop when the user cancelled."""
    task.done += 1
    AssistTask.objects.filter(pk=task.pk).update(done=task.done)
    if AssistTask.objects.filter(pk=task.pk, state=AssistTask.State.CANCELLED).exists():
        raise TaskCancelled


def _set_total(task: AssistTask, total: int) -> None:
    task.total = total
    AssistTask.objects.filter(pk=task.pk).update(total=total)


def numbered(titles: dict[str, str | None]) -> dict[str, str | None]:
    """Two documents of the same month would get the same name: number them."""
    seen = Counter(t for t in titles.values() if t)
    counters: Counter[str] = Counter()
    out: dict[str, str | None] = {}
    for key, title in titles.items():
        if title and seen[title] > 1:
            counters[title] += 1
            title = f"{title} ({counters[title]})"
        out[key] = title
    return out


def rename(
    task: AssistTask,
    documents: list[Document],
    instruction: str,
    model: str,
    *,
    before_request: Callable[[], float | None] | None = None,
) -> Result:
    by_pk = {d.pk: d for d in documents}
    docs = [filing._doc(d) for d in documents if crypto_fields.get_title(d)]
    explicit = written_pattern(instruction)
    groups = filing.group_documents(docs)
    # Consistent with what? A lone document among groups is left alone; a single one selected is not.
    if any(len(g.docs) > 1 for g in groups):
        groups = [g for g in groups if len(g.docs) > 1]
    total = 0 if explicit or not model else len(groups)
    _set_total(task, total + (task.total if before_request else 0))
    result = Result()
    if not model and not explicit:
        result.note = (
            "No AI model is chosen (Settings → AI): the names come from the titles' common first word."
        )
    for group in groups:
        pattern: str | None
        if explicit:
            pattern = explicit
        elif model:
            timeout = before_request() if before_request else None
            pattern = ai.title_pattern(
                model,
                [ai.AssistDocument(d.title, by_pk[d.pk].document_date, d.corr_name) for d in group.docs],
                wish=instruction,
                **({"timeout": timeout} if timeout is not None else {}),
            )
            _tick(task)
        else:
            pattern = _rule_pattern(group)
        if not pattern:
            continue
        if not explicit:  # the user's own pattern stays as written
            pattern = without_day(pattern, [by_pk[d.pk].document_date for d in group.docs])
        titles = numbered({d.uuid: fill_pattern(pattern, by_pk[d.pk].document_date) for d in group.docs})
        changes = ChangeGroup(f"Pattern “{pattern}”", "title")
        for d in group.docs:
            new = titles[d.uuid]
            if new and new != d.title:
                changes.items.append(Item(d.uuid, d.title, new))
        if changes.items:
            result.groups.append(changes)
    return result


def scope_words(instruction: str, value: str) -> list[str]:
    """The words of the wish's clause with `value` that may name documents ("Stadtwerke", "Rechnungen").

    The value itself is cut out where it stands ("… ist Stadtwerke Delmenhorst"), so the same word
    naming the documents elsewhere in the clause ("der Stadtwerke Rechnung") still counts.
    """
    clauses = [c for c in CLAUSES.split(instruction) if c and c.strip()]
    clause = next((c for c in clauses if value.casefold() in c.casefold()), instruction)
    at = clause.casefold().rfind(value.casefold())
    if at >= 0:
        clause = clause[:at] + " " + clause[at + len(value) :]
    return [
        w
        for w in tokenizer.words(clause)
        if len(w) >= 3 and w not in tokenizer.STOPWORDS and w not in NOT_SCOPE
    ]


def chosen(rule_words: list[str], docs: list[filing.Doc]) -> set[str] | None:
    """The documents the words name, or None when they name none of them.

    Of the words, those that name any document must all match. A match brings the documents
    that belong with it ("Gehaltsabrechnungen" finds one payslip, and so all of them). Without
    words that could name documents, it is all of them.
    """
    if not rule_words:
        return {d.uuid for d in docs}
    naming = [w for w in rule_words if any(filing.wish_matches(w, d) for d in docs)]
    if not naming:
        return None
    hits = {d.uuid for d in docs if all(filing.wish_matches(w, d) for w in naming)}
    for group in filing.group_documents(docs):
        members = {d.uuid for d in group.docs}
        if len(members) > 1 and members & hits:
            hits |= members
    return hits


def find_type(value: str) -> DocumentType | None:
    wanted = value.casefold().strip()
    best: tuple[float, DocumentType] | None = None
    for t in DocumentType.objects.all():
        for name in (t.slug, t.name):
            score = 100.0 if name.casefold() == wanted else fuzz.ratio(name.casefold(), wanted)
            if score >= TYPE_SIMILARITY and (best is None or score > best[0]):
                best = (score, t)
    return best[1] if best else None


def custom(task: AssistTask, documents: list[Document], instruction: str, model: str) -> Result:
    if not model:
        return Result(note="Custom requests need an AI model (Settings → AI).")
    if not instruction:
        return Result(note="Write what should be done.")
    _set_total(task, 1)
    rules = ai.read_edit_rules(model, instruction)
    _tick(task)
    result = Result()
    if not rules:
        result.note = (
            "The AI model found nothing to change in your request. Name the detail and the value, "
            "e.g. “Tag Gehalt für die Gehaltsabrechnungen” or “Absender ACME GmbH”."
        )
        return result
    docs = {str(d.uuid): d for d in documents}
    parsed = [filing._doc(d) for d in documents]
    notes = []
    for rule in rules:
        words = scope_words(instruction, rule.value)
        found = chosen(words, parsed)
        if found is None and ALL.search(instruction):
            found = set(docs)  # "alle bekommen …": nothing narrower is named
        if found is None:
            unclear = " ".join(words)
            notes.append(f"Which documents “{unclear}” means was unclear: tick them under “{rule.value}”.")
        picked = found or set()
        if rule.change == "tag":
            group = ChangeGroup(f"Add tag “{rule.value}”", "tags")
            for uuid, d in docs.items():
                have = [t.name for t in d.tags.all()]
                if rule.value.casefold() not in {t.casefold() for t in have}:
                    group.items.append(Item(uuid, ", ".join(have), [rule.value], uuid in picked))
        elif rule.change == "sender":
            group = ChangeGroup(f"Sender “{rule.value}”", "sender")
            for uuid, d in docs.items():
                old = d.correspondent.name if d.correspondent else ""
                if old.casefold() != rule.value.casefold():
                    group.items.append(Item(uuid, old, rule.value, uuid in picked))
        elif rule.change == "document_type":
            doc_type = find_type(rule.value)
            if doc_type is None:
                names = ", ".join(DocumentType.objects.order_by("name").values_list("name", flat=True))
                notes.append(f"There is no document type “{rule.value}” (types: {names}).")
                continue
            group = ChangeGroup(f"Type “{doc_type.name}”", "document_type")
            for uuid, d in docs.items():
                if d.document_type_id != doc_type.pk:
                    old = d.document_type.slug if d.document_type else ""
                    group.items.append(Item(uuid, old, doc_type.slug, uuid in picked))
        else:  # title, possibly a pattern
            pattern = written_pattern(rule.value) or rule.value
            titles = numbered({uuid: fill_pattern(pattern, d.document_date) for uuid, d in docs.items()})
            group = ChangeGroup(f"Title “{pattern}”", "title")
            for uuid, d in docs.items():
                old, new = crypto_fields.get_title(d), titles[uuid]
                if new and new != old:
                    group.items.append(Item(uuid, old, new, uuid in picked))
        if group.items:
            result.groups.append(group)
    result.note = " ".join(notes)
    return result


def run(task_id: int) -> None:
    task = AssistTask.objects.filter(pk=task_id).first()
    if task is None or task.state != AssistTask.State.PENDING:
        return
    AssistTask.objects.filter(pk=task.pk).update(state=AssistTask.State.RUNNING)
    model = get_ai_model() if ai.configured() else ""
    documents = list(
        Document.objects.filter(uuid__in=task.documents[:MAX_DOCUMENTS], deleted_at__isnull=True)
        .select_related("correspondent", "document_type", "series")
        .prefetch_related("tags")
        .order_by("document_date", "uploaded_at")
    )
    instruction = instruction_of(task)
    try:
        if task.operation == AssistTask.Operation.RENAME:
            result = rename(task, documents, instruction, model)
        else:
            result = custom(task, documents, instruction, model)
    except TaskCancelled:
        AssistTask.objects.filter(pk=task.pk).update(finished_at=timezone.now())
        return
    except (ai.ModelUnavailable, ai.ModelFailed, ai.ModelCrashed) as exc:
        _finish(task, AssistTask.State.FAILED, error=f"The AI model failed: {exc}"[:500])
        return
    except Exception as exc:
        logger.exception("assistant task failed", extra={"task": task.pk})
        _finish(task, AssistTask.State.FAILED, error=f"{type(exc).__name__}: {exc}"[:500])
        return
    payload = {"groups": [asdict(g) for g in result.groups], "note": result.note}
    task.result_enc = encrypt_bytes(json.dumps(payload, ensure_ascii=False).encode(), aad=RESULT_AAD)
    _finish(task, AssistTask.State.DONE)


def _finish(task: AssistTask, state: str, *, error: str = "") -> None:
    task.state = state
    task.error = error
    task.finished_at = timezone.now()
    # Not when the user cancelled meanwhile: that stays.
    AssistTask.objects.filter(pk=task.pk).exclude(state=AssistTask.State.CANCELLED).update(
        state=state, error=error, result_enc=task.result_enc, finished_at=task.finished_at
    )
