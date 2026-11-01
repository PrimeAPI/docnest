"""Filing suggestions for new documents: where each one should go, for the user to approve.

When a document in the inbox has been processed, a plan job works out its folder. Nothing
moves until the user approves (`accept`); filing it elsewhere by hand overrules the plan.

The difficulty is consistency: documents arriving together often belong together, but a
plan made for one document alone knows nothing of the folder just proposed for the next.
So:

1. **Batches are planned together.** The plan waits while documents uploaded close to the
   waiting ones (`BATCH_WINDOW`) are still being processed, up to `MAX_WAIT`. Then all of
   them are grouped at once, as the filing suggestions in `apps.taxonomy.filing` do: the
   same series, the same sender and type, similar titles.
2. **Open suggestions count as folders.** Every suggestion not yet decided is laid over the
   folder tree: the folders it would create become *proposed* folders, and its documents
   describe the folder they are going to. A later document matches them like real ones,
   and the AI model sees them marked as proposed, so it joins "Versicherungen/Kfz" instead
   of inventing "Autoversicherung". Groups planned in one run see each other the same way.
3. **Second chances.** A document nothing fitted is planned again with the next new
   documents: two of a kind may then make a folder together.

Where a group goes, strongest first: the user's saved filing instructions; a folder whose
documents share its series or sender and titles (`STRONG_MATCH`: that is how the user
filed such documents before); otherwise the AI model's choice from the whole tree (an
existing or proposed folder, or up to `MAX_NEW` new ones below one), checked against the
tree; without a model, the rules of the filing suggestions. Year folders follow the
target's habit. The base rule of the filing suggestions holds: a document the user put
into a folder only goes deeper below it; one without a folder, or with only the folder a
scanner puts everything into, may go anywhere.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import connection, transaction
from django.utils import timezone

from apps.analysis import ai
from apps.crypto.aead import decrypt_text, encrypt_text
from apps.documents import alterations
from apps.documents.models import Document, Source
from apps.processing import queue
from apps.processing.models import Job, SystemState
from apps.processing.preferences import get_ai_model
from apps.taxonomy import filing
from apps.taxonomy.filing import Doc, Group, Tree
from apps.taxonomy.models import Correspondent, FilingSuggestion, Folder
from apps.taxonomy.services import MAX_FOLDER_DEPTH, fold, folder_paths

logger = logging.getLogger(__name__)

ENABLED_KEY = "inbox_filing"
BATCH_WINDOW = timedelta(minutes=10)  # documents uploaded this close together are planned together
MAX_WAIT = timedelta(minutes=20)  # ... but a plan waits no longer than this for the rest of a batch
WAIT_SECONDS = 30
STRONG_MATCH = 3.5  # from this score (filing._score) a folder is taken without asking the model
MAX_NEW = 2  # new folders the AI model may add below an existing one
MAX_PLAN = 200  # documents planned in one run; the rest in the next
LOCK = 0x646F_636E  # advisory lock: one plan at a time, also across workers
Suggestion = FilingSuggestion.State


# --- Switch ----------------------------------------------------------------------------------


def enabled() -> bool:
    value = SystemState.objects.filter(key=ENABLED_KEY).values_list("value", flat=True).first()
    return not isinstance(value, dict) or value.get("enabled") is not False


def set_enabled(on: bool) -> None:
    SystemState.objects.update_or_create(key=ENABLED_KEY, defaults={"value": {"enabled": on}})
    if on:
        schedule()


# --- Asking for a plan -------------------------------------------------------------------------


def request(document_id: int) -> None:
    """A document finished processing: plan its filing once the rest of its batch is processed."""
    status = Document.objects.filter(pk=document_id).values_list("status", flat=True).first()
    if status != Document.Status.NEW or not enabled():
        return
    # Reprocessed: its title or sender may have changed. A decision of the user stays.
    FilingSuggestion.objects.filter(
        document_id=document_id, state__in=[Suggestion.PENDING, Suggestion.NONE]
    ).delete()
    if not FilingSuggestion.objects.filter(document_id=document_id).exists():
        schedule()


def schedule(since: str | None = None, delay: int = 0) -> None:
    """Queue a plan unless one is waiting already (a running one may have read its documents)."""
    if Job.objects.filter(kind=Job.Kind.PLAN_FILING, state=Job.State.QUEUED).exists():
        return
    queue.enqueue(
        Job.Kind.PLAN_FILING,
        payload={"since": since or timezone.now().isoformat()},
        delay=delay,
        priority=5,
    )


def planning() -> bool:
    return Job.objects.filter(
        kind=Job.Kind.PLAN_FILING, state__in=[Job.State.QUEUED, Job.State.RUNNING]
    ).exists()


def _awaiting() -> list[Document]:
    return list(
        Document.objects.filter(
            deleted_at__isnull=True,
            status=Document.Status.NEW,
            processing_state=Document.State.DONE,
            filing_suggestion__isnull=True,
        ).order_by("uploaded_at")[:MAX_PLAN]
    )


def _batch_still_processing(docs: list[Document]) -> bool:
    low = min(d.uploaded_at for d in docs) - BATCH_WINDOW
    high = max(d.uploaded_at for d in docs) + BATCH_WINDOW
    return Document.objects.filter(
        deleted_at__isnull=True,
        processing_state__in=[Document.State.PENDING, Document.State.RUNNING],
        uploaded_at__range=(low, high),
    ).exists()


@contextmanager
def _lock() -> Iterator[bool]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK])
        got = bool(cursor.fetchone()[0])
    try:
        yield got
    finally:
        if got:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK])


def run(payload: dict[str, object]) -> None:
    """The plan job: wait for the batch, then plan every document that waits."""
    raw = payload.get("since")
    since = datetime.fromisoformat(raw) if isinstance(raw, str) else timezone.now()
    if not enabled():
        return
    with _lock() as got:
        if not got:
            schedule(since.isoformat(), WAIT_SECONDS)
            return
        fresh = _awaiting()
        if not fresh:
            return
        if timezone.now() - since < MAX_WAIT and _batch_still_processing(fresh):
            logger.debug("filing plan waits for its batch", extra={"documents": len(fresh)})
            schedule(since.isoformat(), WAIT_SECONDS)
            return
        plan(fresh)
    if len(fresh) >= MAX_PLAN:
        schedule()


# --- Planning ----------------------------------------------------------------------------------


@dataclass
class Planned:
    anchor_id: int | None  # deepest existing folder
    new: list[str]  # folders to create below it
    reason: str
    named_by: str


def plan(fresh: list[Document]) -> dict[int, Planned | None]:
    """Plan `fresh` documents, together with earlier ones nothing fitted; store and return the plans."""
    lonely = Document.objects.filter(
        deleted_at__isnull=True, status=Document.Status.NEW, filing_suggestion__state=Suggestion.NONE
    )
    documents = {d.pk: d for d in [*fresh, *lonely]}
    docs = filing._load_docs([str(d.uuid) for d in documents.values()])
    pending = list(
        FilingSuggestion.objects.filter(state=Suggestion.PENDING, document__deleted_at__isnull=True)
        .exclude(document_id__in=list(documents))
        .select_related("document")
    )
    # Documents with an open suggestion lie where it sends them, not where they are now.
    tree = Tree(exclude={*documents, *(s.document_id for s in pending)})
    pending_docs = {d.pk: d for d in filing._load_docs([str(s.document.uuid) for s in pending])}
    for s in pending:
        if (doc := pending_docs.get(s.document_id)) is not None:
            _overlay(tree, s.anchor_id, s.new, [doc])

    options = filing.load_preferences()
    model = get_ai_model() if ai.configured() else ""
    wishes = ai.FilingWishes([], None)
    if options.instructions and model:
        try:
            wishes = ai.read_filing_wishes(model, options.instructions)
        except (ai.ModelUnavailable, ai.ModelFailed, ai.ModelCrashed) as exc:
            logger.warning("the AI model could not read the filing instructions", extra={"error": str(exc)})

    plans: dict[int, Planned | None] = {d.pk: None for d in docs}
    by_base: dict[int | None, list[Doc]] = defaultdict(list)
    for d in docs:
        if d.title:
            by_base[_base(documents[d.pk])].append(d)
    for base, members in by_base.items():
        for g in _plan_base(members, tree, base, options, model, wishes):
            anchor, new = tree.real(g.anchor_id, g.new)
            joins = g.anchor_id is not None and g.anchor_id in tree.proposed
            reason = g.reason + (" — joins a folder proposed for other new documents" if joins else "")
            for d in g.docs:
                if d.folder_id != anchor or new:
                    plans[d.pk] = Planned(anchor, new, reason, "ai" if g.by_model else "rules")
            # What this base was given, the next one sees.
            _overlay(tree, anchor, new, g.docs)
    _store(documents, plans)
    logger.info(
        "filing planned",
        extra={"documents": len(plans), "suggested": sum(p is not None for p in plans.values())},
    )
    return plans


def _base(document: Document) -> int | None:
    """Below which folder a document must stay: the one the user chose, not the scanner's."""
    return document.folder_id if document.source_of("folder") == Source.USER else None


def _overlay(tree: Tree, anchor: int | None, new: list[str], docs: list[Doc]) -> None:
    if anchor is not None and anchor not in tree.folders:
        return
    pk = anchor
    for name in new:
        pk = tree.propose(pk, name)
    if pk is not None:
        for d in docs:
            tree.describe(pk, d)


def _plan_base(
    members: list[Doc],
    tree: Tree,
    base: int | None,
    options: filing.Options,
    model: str,
    wishes: ai.FilingWishes,
) -> list[Group]:
    hits: Counter[int] = Counter()
    unassigned: list[Doc] = []
    wished, members = filing._apply_wishes(wishes.rules, members, tree, base, options, hits, unassigned)
    groups = filing.group_documents(members)
    for g in groups:
        filing.place(g, tree, base)
    ask = [g for g in groups if not (g.matched and g.score >= STRONG_MATCH)]
    if ask and model:
        try:
            _choose_with_model(model, ask, tree, base, options, wishes.style)
        except (ai.ModelUnavailable, ai.ModelFailed, ai.ModelCrashed) as exc:
            logger.warning("AI filing failed; the rules decide", extra={"error": str(exc)})
    filing._name_with_rules([g for g in ask if not g.by_model], tree, base, options)
    placed: list[Group] = []
    for g in filing._merge_same_target([*wished, *(g for g in groups if g.matched or g.new)]):
        # A new folder for one document only when the model chose it from the whole tree.
        if g.new and len(g.docs) < filing.MIN_NEW_FOLDER and not (g.wished or g.by_model):
            continue
        placed.extend(filing.split_years(g, tree, options))
    return placed


def _choose_with_model(
    model: str, groups: list[Group], tree: Tree, base: int | None, options: filing.Options, style: str | None
) -> None:
    ordered = sorted(groups, key=lambda g: -len(g.docs))[: filing.MAX_MODEL_GROUPS]
    paths = ai.choose_folders(
        model,
        folders=_listing(tree, base, ordered),
        groups=[
            ai.FolderGroup(
                titles=[d.title for d in g.docs[:4]],
                sender=g.docs[0].corr_name if all(d.corr_id == g.docs[0].corr_id for d in g.docs) else None,
                doc_type=g.docs[0].type_name if all(d.type_id == g.docs[0].type_id for d in g.docs) else None,
                count=len(g.docs),
            )
            for g in ordered
        ],
        below=_path(tree, base, None),
        style=style,
        allow_new=options.new_folders,
    )
    for g, path in zip(ordered, paths, strict=True):
        target = resolve(path, tree, base, allow_new=options.new_folders) if path else None
        if target is None:
            continue  # unusable: the rules decide
        g.anchor_id, g.new = target
        g.matched, g.by_model = not g.new, True
        g.reason = f"{g.reason} — chosen by the AI model"


def _path(tree: Tree, pk: int | None, base: int | None) -> str:
    """ "Privat/Versicherungen": the names from below `base` down to `pk`."""
    names = []
    while pk is not None and pk != base:
        names.append(tree.folders[pk].name)
        pk = tree.folders[pk].parent_id
    return "/".join(reversed(names))


def _listing(tree: Tree, base: int | None, groups: list[Group]) -> list[ai.TreeFolder]:
    """The folders below `base` the model may choose from; the likeliest when there are many."""
    below: list[int] = []
    level: list[int | None] = [base]
    while level:  # year folders are the year rule's business, and so is what lies in them
        found = [
            c for p in level for c in tree.children.get(p, []) if not filing.YEAR.match(tree.folders[c].name)
        ]
        below.extend(found)
        level = list(found)
    if len(below) > ai.MAX_PLACE_FOLDERS:
        likely = sorted(below, key=lambda pk: -max(filing._score(g, tree.profiles[pk])[0] for g in groups))[
            : ai.MAX_PLACE_FOLDERS // 2
        ]
        keep = set(tree.children.get(base, []))
        for pk in likely:
            up: int | None = pk
            while up is not None and up != base:
                keep.add(up)
                up = tree.folders[up].parent_id
        below = [pk for pk in below if pk in keep][: ai.MAX_PLACE_FOLDERS]
    names = dict(Correspondent.objects.values_list("pk", "name"))
    out = [
        ai.TreeFolder(
            path=_path(tree, pk, base),
            count=tree.profiles[pk].total,
            senders=[names[c] for c, _ in tree.profiles[pk].corrs.most_common(3) if c in names],
            proposed=pk in tree.proposed,
        )
        for pk in below
    ]
    return sorted(out, key=lambda f: f.path.casefold())


def _child(tree: Tree, parent: int | None, name: str) -> int | None:
    folded = fold(name)
    return next(
        (pk for pk in tree.children.get(parent, []) if fold(tree.folders[pk].name) == folded),
        None,
    )


def resolve(
    path: str, tree: Tree, base: int | None, *, allow_new: bool = True
) -> tuple[int | None, list[str]] | None:
    """(anchor, new folders) for a path the model gave, or None when it cannot be used.

    The path is read below `base`; its first name may also be a folder deeper down
    ("Versicherungen" for "Privat/Versicherungen"). Years are left to the year rule.
    """
    names = filing.clean_names(n for n in path.replace("\\", "/").split("/") if n.strip())
    while names and filing.YEAR.match(names[-1]):
        names.pop()
    if base is not None and names and fold(names[0]) == fold(tree.folders[base].name):
        names = names[1:]  # repeated the folder the documents are in
    if not names:
        return None
    anchor: int | None = base
    first = _child(tree, base, names[0])
    if first is None:
        first = filing._find_below(tree, base, names[0])
    if first is not None:
        anchor, names = first, names[1:]
        while names and (child := _child(tree, anchor, names[0])) is not None:
            anchor, names = child, names[1:]
    if len(names) > MAX_NEW or (names and not allow_new) or (anchor is None and not names):
        return None
    if any(len(n) > ai.MAX_FOLDER_NAME or filing.YEAR.match(n) for n in names):
        return None
    if tree.depth(anchor) + len(names) > MAX_FOLDER_DEPTH:
        return None
    return anchor, names


# --- Storing and reading -------------------------------------------------------------------------


def _aad(document: Document) -> bytes:
    return b"filing-reason:" + str(document.uuid).encode()


def _store(documents: dict[int, Document], plans: dict[int, Planned | None]) -> None:
    with transaction.atomic():
        decided = set(
            FilingSuggestion.objects.filter(
                document_id__in=list(plans), state__in=[Suggestion.ACCEPTED, Suggestion.DISMISSED]
            ).values_list("document_id", flat=True)
        )
        for pk, p in plans.items():
            if pk in decided:
                continue  # the user decided while the plan ran
            FilingSuggestion.objects.update_or_create(
                document_id=pk,
                defaults={
                    "state": Suggestion.PENDING if p else Suggestion.NONE,
                    "anchor_id": p.anchor_id if p else None,
                    "new": p.new if p else [],
                    "reason_enc": encrypt_text(p.reason, aad=_aad(documents[pk])) if p else None,
                    "named_by": p.named_by if p else "",
                    "created_at": timezone.now(),
                },
            )


def reason(suggestion: FilingSuggestion, document: Document) -> str:
    return decrypt_text(suggestion.reason_enc, aad=_aad(document)) if suggestion.reason_enc else ""


class Folders:
    """The folder tree as it is now, for showing and comparing targets."""

    def __init__(self) -> None:
        self.paths = folder_paths()
        self.by_name: dict[tuple[int | None, str], int] = {
            (parent, name.casefold()): pk
            for pk, name, parent in Folder.objects.values_list("pk", "name", "parent_id")
        }

    def current(self, anchor_id: int | None, new: list[str]) -> tuple[int | None, list[str]]:
        """A target once folders it would create exist (another suggestion was approved)."""
        names = list(new)
        while names and (pk := self.by_name.get((anchor_id, names[0].casefold()))) is not None:
            anchor_id, names = pk, names[1:]
        return anchor_id, names

    def path(self, anchor_id: int | None) -> str:
        return self.paths.get(anchor_id, "") if anchor_id is not None else ""


# --- The user's decision ----------------------------------------------------------------------


def accept(uuids: list[str], target: tuple[int | None, list[str]] | None = None) -> tuple[int, int]:
    """Move documents where their suggestion says, or to `target` (anchor, new folders) instead.

    Returns (moved, created folders). The user decides here, so any folder may be the target.
    """
    moved = created = 0
    with transaction.atomic():
        parents: dict[int, int | None] = dict(Folder.objects.values_list("pk", "parent_id"))
        suggestions = FilingSuggestion.objects.select_for_update().filter(
            document__uuid__in=uuids, document__deleted_at__isnull=True
        )
        if target is None:
            suggestions = suggestions.filter(state=Suggestion.PENDING)
        for s in suggestions:
            anchor, new = target or (s.anchor_id, s.new)
            folder, made = filing.ensure_target(anchor, filing.clean_names(new), parents)
            created += made
            document = Document.objects.select_for_update().get(pk=s.document_id)
            if document.folder_id != folder.pk:
                before = alterations.snapshot(document)
                document.folder = folder
                document.set_source("folder", Source.USER)
                document.save(update_fields=["folder", "field_sources", "updated_at"])
                alterations.record_edit(document, before)
                moved += 1
            s.state, s.anchor, s.new, s.decided_at = Suggestion.ACCEPTED, folder, [], timezone.now()
            s.save(update_fields=["state", "anchor", "new", "decided_at"])
    return moved, created


def dismiss(uuids: list[str]) -> int:
    return FilingSuggestion.objects.filter(
        document__uuid__in=uuids, state__in=[Suggestion.PENDING, Suggestion.NONE]
    ).update(state=Suggestion.DISMISSED, decided_at=timezone.now())


def filed_by_user(document_ids: list[int], folder_id: int | None) -> None:
    """The user put documents into a folder themselves: their suggestions are settled."""
    open_ = list(
        FilingSuggestion.objects.filter(
            document_id__in=document_ids, state__in=[Suggestion.PENDING, Suggestion.NONE]
        )
    )
    if not open_:
        return
    folders = Folders()
    for s in open_:
        anchor, rest = folders.current(s.anchor_id, s.new)
        hit = s.state == Suggestion.PENDING and not rest and anchor == folder_id
        s.state = Suggestion.ACCEPTED if hit else Suggestion.DISMISSED
        s.decided_at = timezone.now()
        s.save(update_fields=["state", "decided_at"])
