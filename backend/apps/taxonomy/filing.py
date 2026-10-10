"""Filing suggestions: sort selected documents deeper into the folder tree, on request.

The user selects documents (typically everything lying loose in a folder) and asks
for suggestions. A document is only ever moved *below* the folder it is in — never
sideways or up; documents without a folder may go anywhere.

How a suggestion comes about, per folder the selected documents lie in (the *base*):

1. **Group** the documents by what analysis already knows, strongest first: the
   same series; the same sender and type, split by the word their titles start
   with ("Verdienstabrechnung …" vs "Lohnsteuerbescheinigung …"); the same sender;
   otherwise the same first title word.
2. **Understand the existing subfolders.** Every folder below the base gets a
   profile from the documents filed in it and its subfolders: their series,
   senders, types and frequent title words, plus the folder's own name. A group
   goes into the subfolder whose profile it matches best, and further down as
   long as a deeper subfolder matches too.
3. **Propose new subfolders** for groups that fit nowhere. With an AI model chosen,
   it names them (and may still put a group into an existing subfolder, or give
   two groups the same name to merge them); otherwise the name comes from the
   titles. A new folder needs at least two documents.
4. **Years.** Where the target already has year subfolders ("2025", "2026"), or a
   new folder receives documents from several years or recurring ones (a monthly
   series, titles naming a month), the documents go into a subfolder per year of
   their document date.

The result only names folders and documents; nothing changes until the user
applies it (`apply`), and folders are created then.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone
from rapidfuzz import fuzz

from apps.analysis import ai
from apps.documents import crypto_fields
from apps.documents.models import Document, Source
from apps.processing.preferences import get_ai_model
from apps.search import tokenizer
from apps.taxonomy.models import FilingProposal, Folder, Series
from apps.taxonomy.services import MAX_FOLDER_DEPTH, folder_paths, normalize_label

logger = logging.getLogger(__name__)

MAX_DOCUMENTS = 500
MATCH_THRESHOLD = 2.0  # a group belongs in a folder from this score on (see `_score`)
NAME_SIMILARITY = 85  # a title word "is" a folder name word from this rapidfuzz ratio on
FREQUENT_WORD_SHARE = 0.3  # a folder's word counts as typical when this share of its documents has it
MIN_NEW_FOLDER = 2  # documents needed for a new folder
MIN_YEAR_SPLIT = 3  # documents needed before a new folder is divided into years
MAX_MODEL_GROUPS = 20  # groups shown to the model in one request (the context is 4096 tokens)
YEAR = re.compile(r"^(19|20)\d\d$")
MONTHS = frozenset(
    tokenizer.fold(m)
    for m in (  # noqa: SIM905 - a word list reads better as text
        "januar jänner februar märz april mai juni juli august september oktober november dezember "
        "january february march may june july october december "
        "jan feb mär mar apr jun jul aug sep sept okt oct nov dez dec quartal"
    ).split()
)
SKIP_TYPES = {"other"}  # "Other" says nothing about where a document belongs


# --- Inputs ---------------------------------------------------------------------------


@dataclass
class Doc:
    pk: int
    uuid: str
    folder_id: int | None
    title: str
    words: list[str]  # significant title words, folded, in order
    head: str  # the first significant title word as written ("Verdienstabrechnung")
    type_id: int | None
    type_name: str | None
    corr_id: int | None
    corr_name: str | None
    series_id: int | None
    series_name: str | None
    recurring: bool  # a monthly/quarterly series, or the title names a month ("… Juni 2026")
    year: int | None


@dataclass
class Profile:
    """What a folder holds: the documents in it and in its subfolders."""

    folder: Folder
    name_words: set[str]
    total: int = 0
    series: Counter[int] = field(default_factory=Counter)
    corrs: Counter[int] = field(default_factory=Counter)
    types: Counter[int] = field(default_factory=Counter)
    words: Counter[str] = field(default_factory=Counter)

    def typical_words(self) -> set[str]:
        if not self.total:
            return set()
        return {w for w, n in self.words.items() if n / self.total >= FREQUENT_WORD_SHARE}


@dataclass
class Group:
    docs: list[Doc]
    reason: str
    anchor_id: int | None = None  # deepest existing folder of the target (None: top level)
    new: list[str] = field(default_factory=list)  # folders to create below the anchor
    matched: bool = False  # placed into an existing subfolder

    def top_words(self) -> set[str]:
        counts = Counter(w for d in self.docs for w in set(d.words))
        need = max(1, (len(self.docs) + 1) // 2)
        return {w for w, n in counts.items() if n >= need}


def significant_words(title: str) -> list[tuple[str, str]]:
    """(folded, as written) title words that say what a document is: no numbers, months or stopwords."""
    out = []
    for word in tokenizer.words(title):
        folded = tokenizer.fold(word)
        if len(folded) < 3 or folded.isdigit() or any(c.isdigit() for c in folded):
            continue
        if folded in tokenizer.STOPWORDS or folded in MONTHS:
            continue
        out.append((folded, word))
    return out


def _load_docs(uuids: list[str]) -> list[Doc]:
    qs = (
        Document.objects.filter(uuid__in=uuids[:MAX_DOCUMENTS], deleted_at__isnull=True)
        .select_related("document_type", "correspondent", "series")
        .order_by("document_date", "uploaded_at")
    )
    return [_doc(d) for d in qs]


def _doc(d: Document) -> Doc:
    title = crypto_fields.get_title(d)
    words = significant_words(title)
    series = d.series
    return Doc(
        pk=d.pk,
        uuid=str(d.uuid),
        folder_id=d.folder_id,
        title=title,
        words=[w for w, _ in words],
        head=words[0][1] if words else "",
        type_id=d.document_type_id if d.document_type and d.document_type.slug not in SKIP_TYPES else None,
        type_name=d.document_type.name
        if d.document_type and d.document_type.slug not in SKIP_TYPES
        else None,
        corr_id=d.correspondent_id,
        corr_name=d.correspondent.name if d.correspondent else None,
        series_id=d.series_id,
        series_name=series.name if series else None,
        recurring=bool(series and series.cadence in (Series.Cadence.MONTHLY, Series.Cadence.QUARTERLY))
        or any(tokenizer.fold(w) in MONTHS for w in tokenizer.words(title)),
        year=d.document_date.year if d.document_date else None,
    )


class Tree:
    """The folder tree with a profile per folder."""

    def __init__(self, exclude: set[int]) -> None:
        self.folders = {f.pk: f for f in Folder.objects.all()}
        self.children: dict[int | None, list[int]] = defaultdict(list)
        for f in self.folders.values():
            self.children[f.parent_id].append(f.pk)
        self.paths = folder_paths()
        self.profiles = {
            pk: Profile(folder=f, name_words={w for w, _ in significant_words(f.name)})
            for pk, f in self.folders.items()
        }
        docs = (
            Document.objects.filter(deleted_at__isnull=True, folder__isnull=False)
            .exclude(pk__in=exclude)
            .select_related("document_type")
            .only("pk", "uuid", "folder", "series", "correspondent", "document_type__slug", "title_enc")
        )
        for d in docs:
            words = {w for w, _ in significant_words(crypto_fields.get_title(d))}
            type_id = (
                d.document_type_id if d.document_type and d.document_type.slug not in SKIP_TYPES else None
            )
            # A document describes its folder and every folder above it.
            pk: int | None = d.folder_id
            while pk is not None and pk in self.profiles:
                p = self.profiles[pk]
                p.total += 1
                if d.series_id:
                    p.series[d.series_id] += 1
                if d.correspondent_id:
                    p.corrs[d.correspondent_id] += 1
                if type_id:
                    p.types[type_id] += 1
                p.words.update(words)
                pk = self.folders[pk].parent_id

    def subfolders(self, parent: int | None) -> list[int]:
        """Children that are not year folders (years are handled on their own)."""
        return [pk for pk in self.children.get(parent, []) if not YEAR.match(self.folders[pk].name)]

    def year_children(self, parent: int | None) -> dict[str, int]:
        return {
            self.folders[pk].name: pk
            for pk in self.children.get(parent, [])
            if YEAR.match(self.folders[pk].name)
        }

    def child_named(self, parent: int | None, name: str) -> int | None:
        wanted = name.casefold()
        return next(
            (pk for pk in self.children.get(parent, []) if self.folders[pk].name.casefold() == wanted), None
        )

    def depth(self, pk: int | None) -> int:
        n = 0
        while pk is not None:
            n += 1
            pk = self.folders[pk].parent_id
        return n

    def path(self, pk: int | None) -> str:
        return self.paths.get(pk, "") if pk is not None else ""


# --- Grouping ---------------------------------------------------------------------------


def group_documents(docs: list[Doc]) -> list[Group]:
    """Documents that belong together, strongest signal first."""
    groups: list[Group] = []
    rest: list[Doc] = []
    by_series: dict[int, list[Doc]] = defaultdict(list)
    for d in docs:
        (by_series[d.series_id] if d.series_id else rest).append(d)
    for members in by_series.values():
        name = members[0].series_name
        groups.append(Group(members, f"same series “{name}”" if name else "same series"))

    by_pair: dict[tuple[int, int], list[Doc]] = defaultdict(list)
    by_corr: dict[int, list[Doc]] = defaultdict(list)
    loose: list[Doc] = []
    for d in rest:
        if d.corr_id and d.type_id:
            by_pair[(d.corr_id, d.type_id)].append(d)
        elif d.corr_id:
            by_corr[d.corr_id].append(d)
        else:
            loose.append(d)
    for members in by_pair.values():
        why = f"sender {members[0].corr_name} · {members[0].type_name}"
        groups.extend(_split_by_head(members, why))
    for members in by_corr.values():
        groups.extend(_split_by_head(members, f"sender {members[0].corr_name}"))
    by_head: dict[str, list[Doc]] = defaultdict(list)
    for d in loose:
        by_head[d.words[0] if d.words else ""].append(d)
    for head, members in by_head.items():
        if head:
            groups.append(Group(members, f"titles start with “{members[0].head}”"))
        else:
            groups.extend(Group([d], "no title or sender to go by") for d in members)
    return groups


def _split_by_head(docs: list[Doc], reason: str) -> list[Group]:
    """Split by first title word where that gives groups of their own ("Verdienstabrechnung" …)."""
    by_head: dict[str, list[Doc]] = defaultdict(list)
    for d in docs:
        by_head[d.words[0] if d.words else ""].append(d)
    big = {h: m for h, m in by_head.items() if h and len(m) >= MIN_NEW_FOLDER}
    if len(big) < 2:
        return [Group(docs, reason)]
    groups = [Group(m, f"{reason} · “{m[0].head}”") for m in big.values()]
    leftovers = [d for h, m in by_head.items() if h not in big for d in m]
    if leftovers:
        groups.append(Group(leftovers, reason))
    return groups


# --- Matching existing folders -------------------------------------------------------------


def _similar(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 4 and fuzz.ratio(a, b) >= NAME_SIMILARITY)


def _score(group: Group, profile: Profile) -> tuple[float, str]:
    """How well a group fits a folder (≥ MATCH_THRESHOLD: it belongs there) and why."""
    docs = group.docs
    n = len(docs)
    score = 0.0
    why: list[str] = []
    if profile.total:
        series = sum(1 for d in docs if d.series_id and d.series_id in profile.series) / n
        corrs = sum(1 for d in docs if d.corr_id and d.corr_id in profile.corrs) / n
        types = sum(1 for d in docs if d.type_id and d.type_id in profile.types) / n
        score += 3 * series + 1.5 * corrs + 0.5 * types
        if series >= 0.5:
            why.append("same series as documents there")
        elif corrs >= 0.5:
            why.append("same sender as documents there")
        top = group.top_words()
        if top:
            shared = top & profile.typical_words()
            score += 2 * len(shared) / len(top)
            if shared:
                why.append("similar titles")
    clues = group.top_words() | {
        w for d in docs[:1] for name in (d.corr_name, d.type_name) if name for w, _ in significant_words(name)
    }
    if any(_similar(a, b) for a in clues for b in profile.name_words):
        score += 2
        why.append(f"matches the name “{profile.folder.name}”")
    return score, ", ".join(why)


def place(group: Group, tree: Tree, base: int | None) -> None:
    """Descend from `base` into the best matching subfolder as long as one matches."""
    anchor, reasons = base, []
    while True:
        best: tuple[float, int, str] | None = None
        for pk in tree.subfolders(anchor):
            score, why = _score(group, tree.profiles[pk])
            if score >= MATCH_THRESHOLD and (best is None or score > best[0]):
                best = (score, pk, why)
        if best is None:
            break
        anchor = best[1]
        if best[2]:
            reasons.append(best[2])
    if anchor != base:
        group.anchor_id = anchor
        group.matched = True
        if reasons:
            group.reason = f"{group.reason} — {reasons[-1]}"


# --- Naming new folders --------------------------------------------------------------------


def rule_name(group: Group) -> str | None:
    """A folder name from the documents themselves: their common first title word, series or sender."""
    heads = Counter(d.head for d in group.docs if d.head)
    if heads:
        head, n = heads.most_common(1)[0]
        if n * 2 >= len(group.docs):
            return head[:1].upper() + head[1:]
    first = group.docs[0]
    return first.series_name or first.corr_name or first.type_name


def _apply_name(group: Group, name: str, tree: Tree, base: int | None) -> None:
    existing = tree.child_named(base, name)
    if existing is not None:
        group.anchor_id = existing
        group.matched = True
        group.reason = f"{group.reason} — fits “{tree.folders[existing].name}”"
    else:
        group.anchor_id = base
        group.new = [normalize_label(name)[:80]]


def _merge_same_target(groups: list[Group]) -> list[Group]:
    merged: dict[tuple[int | None, tuple[str, ...]], Group] = {}
    for g in groups:
        key = (g.anchor_id, tuple(n.casefold() for n in g.new))
        if key in merged:
            target = merged[key]
            target.docs.extend(g.docs)
            if g.reason not in target.reason:
                target.reason = f"{target.reason}; {g.reason}"
        else:
            merged[key] = g
    return list(merged.values())


# --- Years ---------------------------------------------------------------------------------


def split_years(group: Group, tree: Tree) -> list[Group]:
    """Divide a group into year subfolders where that is the folder's habit or the documents call for it."""
    years = {d.year for d in group.docs if d.year}
    if not years:
        return [group]
    if group.new:
        recurring = sum(d.recurring for d in group.docs) * 2 >= len(group.docs)
        if len(group.docs) < MIN_YEAR_SPLIT or (len(years) < 2 and not recurring):
            return [group]
        existing_years: dict[str, int] = {}
    else:
        existing_years = tree.year_children(group.anchor_id)
        if not existing_years:
            return [group]
    depth = tree.depth(group.anchor_id) + len(group.new)
    if depth >= MAX_FOLDER_DEPTH:
        return [group]
    out: list[Group] = []
    by_year: dict[int | None, list[Doc]] = defaultdict(list)
    for d in group.docs:
        by_year[d.year].append(d)
    for year, docs in sorted(by_year.items(), key=lambda item: item[0] or 0):
        part = Group(docs, group.reason, group.anchor_id, list(group.new), group.matched)
        if year is not None:
            if not group.new and str(year) in existing_years:
                part.anchor_id = existing_years[str(year)]
            else:
                part.new = [*group.new, str(year)]
        out.append(part)
    return out


# --- Main ------------------------------------------------------------------------------------


@dataclass
class Suggestion:
    groups: list[Group]
    unassigned: list[Doc]
    named_by: str  # "ai" | "rules"
    note: str = ""


def suggest(uuids: list[str]) -> Suggestion:
    docs = _load_docs(uuids)
    tree = Tree(exclude={d.pk for d in docs})
    model = get_ai_model() if ai.configured() else ""
    named_by = "ai" if model else "rules"
    note = ""
    placed: list[Group] = []
    unassigned: list[Doc] = []

    by_base: dict[int | None, list[Doc]] = defaultdict(list)
    for d in docs:
        if not d.title:
            unassigned.append(d)  # still being read: nothing to go by
        else:
            by_base[d.folder_id].append(d)

    for base, members in by_base.items():
        groups = group_documents(members)
        for g in groups:
            place(g, tree, base)
        open_groups = [g for g in groups if not g.matched]
        if open_groups and model:
            try:
                _name_with_model(model, open_groups, tree, base)
            except (ai.ModelUnavailable, ai.ModelFailed, ai.ModelCrashed) as exc:
                logger.warning(
                    "AI folder naming failed; names come from the titles", extra={"error": str(exc)}
                )
                named_by = "rules"
                note = (
                    f"The AI model could not name the folders ({str(exc)[:200]}); names come from the titles."
                )
                for g in open_groups:
                    g.new = []
                _name_with_rules(open_groups, tree, base)
        elif open_groups:
            _name_with_rules(open_groups, tree, base)
        candidates = _merge_same_target([g for g in groups if g.matched or g.new])
        for g in groups:
            if not g.matched and not g.new:
                unassigned.extend(g.docs)
        for g in candidates:
            if g.new and len(g.docs) < MIN_NEW_FOLDER:
                unassigned.extend(g.docs)
                continue
            placed.extend(split_years(g, tree))

    placed = [g for g in _merge_same_target(placed) if any(_moves(d, g) for d in g.docs)]
    for g in placed:
        unassigned.extend(d for d in g.docs if not _moves(d, g))
        g.docs = [d for d in g.docs if _moves(d, g)]
    placed.sort(key=lambda g: (tree.path(g.anchor_id).casefold(), [n.casefold() for n in g.new]))
    return Suggestion(placed, unassigned, named_by, note)


def _moves(d: Doc, g: Group) -> bool:
    return bool(g.new) or g.anchor_id != d.folder_id


def _name_with_rules(groups: list[Group], tree: Tree, base: int | None) -> None:
    for g in groups:
        if len(g.docs) >= MIN_NEW_FOLDER and (name := rule_name(g)):
            _apply_name(g, name, tree, base)


def _name_with_model(model: str, groups: list[Group], tree: Tree, base: int | None) -> None:
    # Biggest groups first: when there are too many to ask about, the small ones wait.
    ordered = sorted(groups, key=lambda g: -len(g.docs))[:MAX_MODEL_GROUPS]
    existing = [tree.folders[pk].name for pk in tree.subfolders(base)]
    names = ai.name_folders(
        model,
        parent=tree.path(base),
        existing=existing,
        groups=[
            ai.FolderGroup(
                titles=[d.title for d in g.docs[:4]],
                sender=g.docs[0].corr_name if all(d.corr_id == g.docs[0].corr_id for d in g.docs) else None,
                doc_type=g.docs[0].type_name if all(d.type_id == g.docs[0].type_id for d in g.docs) else None,
                count=len(g.docs),
            )
            for g in ordered
        ],
    )
    for g, name in zip(ordered, names, strict=True):
        if name and not YEAR.match(name):
            _apply_name(g, name, tree, base)
    # A group the model left out of a new folder still gets one when it is big enough on its own.
    _name_with_rules([g for g in groups if g not in ordered], tree, base)


# --- Job and storage --------------------------------------------------------------------------


def to_result(s: Suggestion) -> dict[str, object]:
    return {
        "groups": [
            {
                "anchor_id": g.anchor_id,
                "new": g.new,
                "reason": g.reason,
                "documents": [d.uuid for d in g.docs],
            }
            for g in s.groups
        ],
        "unassigned": [d.uuid for d in s.unassigned],
        "named_by": s.named_by,
        "note": s.note,
    }


def run_proposal(proposal_id: int) -> None:
    proposal = FilingProposal.objects.filter(pk=proposal_id).first()
    if proposal is None or proposal.state != FilingProposal.State.PENDING:
        return
    try:
        result = to_result(suggest([str(u) for u in proposal.documents]))
    except Exception as exc:
        logger.exception("filing suggestion failed", extra={"proposal": proposal_id})
        proposal.state = FilingProposal.State.FAILED
        proposal.error = f"{type(exc).__name__}: {exc}"[:500]
    else:
        proposal.state = FilingProposal.State.DONE
        proposal.result = result
    proposal.finished_at = timezone.now()
    proposal.save(update_fields=["state", "result", "error", "finished_at"])


# --- Applying -----------------------------------------------------------------------------------


class ApplyError(ValueError):
    pass


@dataclass
class Move:
    documents: list[str]
    anchor_id: int | None
    new: list[str]


def clean_names(names: Iterable[str]) -> list[str]:
    out = []
    for raw in names:
        name = normalize_label(raw)
        if not name:
            raise ApplyError("Folder names cannot be empty")
        if "/" in name or "\\" in name:
            raise ApplyError("Folder names cannot contain slashes")
        out.append(name)
    return out


def apply(moves: list[Move]) -> tuple[int, int]:
    """Move documents into their targets, creating folders as needed. Returns (moved, created folders).

    A document only moves deeper below its current folder; one without a folder may go anywhere.
    """
    moved = created = 0
    with transaction.atomic():
        parents: dict[int, int | None] = dict(Folder.objects.values_list("pk", "parent_id"))
        for move in moves:
            names = clean_names(move.new)
            if move.anchor_id is not None and move.anchor_id not in parents:
                raise ApplyError("A target folder no longer exists")
            if move.anchor_id is None and not names:
                raise ApplyError("Documents cannot be moved to the top level")
            depth, level = 0, move.anchor_id
            while level is not None:
                depth, level = depth + 1, parents[level]
            if depth + len(names) > MAX_FOLDER_DEPTH:
                raise ApplyError(f"Folder paths can be at most {MAX_FOLDER_DEPTH} levels deep")
            documents = list(
                Document.objects.select_for_update().filter(uuid__in=move.documents, deleted_at__isnull=True)
            )
            if not documents:
                continue
            target = Folder.objects.get(pk=move.anchor_id) if move.anchor_id is not None else None
            for name in names:
                child = Folder.objects.filter(parent=target, name__iexact=name).first()
                if child is None:
                    child = Folder.objects.create(parent=target, name=name)
                    parents[child.pk] = child.parent_id
                    created += 1
                target = child
            assert target is not None
            ancestors: set[int] = set()
            up: int | None = target.pk
            while up is not None:
                ancestors.add(up)
                up = parents[up]
            for document in documents:
                if document.folder_id == target.pk:
                    continue
                if document.folder_id is not None and document.folder_id not in ancestors:
                    raise ApplyError("Documents can only be moved into subfolders of their folder")
                document.folder = target
                document.set_source("folder", Source.USER)
                document.save(update_fields=["folder", "field_sources", "updated_at"])
                moved += 1
    return moved, created
