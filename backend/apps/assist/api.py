from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from ninja import Field, Router, Schema
from ninja.errors import HttpError
from pydantic import AwareDatetime

from apps.analysis import ai
from apps.assist import review, tasks
from apps.assist.models import AssistTask
from apps.audit.service import audit
from apps.documents import crypto_fields
from apps.documents.models import Document
from apps.processing import queue
from apps.processing.models import Job
from apps.processing.preferences import get_ai_model
from apps.taxonomy.models import DocumentType, Folder
from apps.taxonomy.services import folder_subtree

router = Router(tags=["assist"])


class AssistSelection(Schema):
    ids: list[UUID] = Field(default_factory=list)
    folder_id: int | None = None
    subfolders: bool = False
    excluded_ids: list[UUID] = Field(default_factory=list)


def selected_ids(data: AssistSelection, scope: str) -> list[str]:
    """Snapshot the complete scope; preview pagination and bulk-action limits do not apply."""
    qs = Document.objects.filter(deleted_at__isnull=True)
    if scope == "folder":
        if data.folder_id is None or not Folder.objects.filter(pk=data.folder_id).exists():
            raise HttpError(400, "The selected folder is missing")
        if data.ids:
            raise HttpError(400, "Choose a folder or selected documents, not both")
        folders = folder_subtree([data.folder_id]) if data.subfolders else [data.folder_id]
        qs = qs.filter(folder_id__in=folders).exclude(uuid__in=data.excluded_ids)
    elif data.folder_id is not None or data.excluded_ids:
        raise HttpError(400, "Folder options require a folder scope")
    elif scope == "selection":
        ids = list(dict.fromkeys(str(i) for i in data.ids))
        if qs.filter(uuid__in=ids).count() != len(ids):
            raise HttpError(400, "A selected document is missing or in the trash")
        return ids
    elif data.ids:
        raise HttpError(400, "Choose all documents or a selection, not both")
    return [str(u) for u in qs.order_by("document_date", "uploaded_at", "pk").values_list("uuid", flat=True)]


class AssistTaskIn(AssistSelection):
    operation: Literal["rename", "custom"]
    instruction: str = Field("", max_length=1000)


class AssistDocOut(Schema):
    id: UUID
    title: str
    correspondent: str | None
    document_date: date | None
    tag_ids: list[int]  # the document's tags now: adding one keeps these


class AssistItemOut(Schema):
    document: AssistDocOut
    old: str  # as shown to the user
    new: str | list[str]  # a title, sender, type name, or tag names to add
    checked: bool  # proposed; the others are listed to be ticked by hand


class AssistGroupOut(Schema):
    label: str
    field: Literal["title", "sender", "document_type", "tags"]
    document_type_id: int | None = None  # the new type of a type change
    items: list[AssistItemOut]


class AssistTaskOut(Schema):
    id: int
    operation: str
    state: str
    done: int
    total: int
    error: str
    note: str
    groups: list[AssistGroupOut]


def _groups(raw: list[dict[str, Any]], docs: dict[str, Document]) -> list[AssistGroupOut]:
    types = {t.slug: t for t in DocumentType.objects.all()}
    groups = []
    for g in raw:
        type_id = None
        if g["field"] == "document_type":
            new_type = types.get(g["items"][0]["new"]) if g["items"] else None
            if new_type is None:
                continue
            type_id = new_type.pk
        items = []
        for item in g["items"]:
            d = docs.get(item["document"])
            if d is None:
                continue
            old: Any = item["old"]
            new: Any = item["new"]
            if g["field"] == "document_type":
                old = types[old].name if old in types else ""
                new = types[new].name if new in types else new
            items.append(
                AssistItemOut(
                    document=AssistDocOut(
                        id=d.uuid,
                        title=crypto_fields.get_title(d),
                        correspondent=d.correspondent.name if d.correspondent else None,
                        document_date=d.document_date,
                        tag_ids=[t.pk for t in d.tags.all()],
                    ),
                    old=str(old or ""),
                    new=new,
                    checked=bool(item.get("checked", True)),
                )
            )
        if items:
            groups.append(
                AssistGroupOut(label=g["label"], field=g["field"], document_type_id=type_id, items=items)
            )
    return groups


def _docs(uuids: set[str]) -> dict[str, Document]:
    return {
        str(d.uuid): d
        for d in Document.objects.filter(uuid__in=uuids, deleted_at__isnull=True)
        .select_related("correspondent")
        .prefetch_related("tags")
    }


def _out(task: AssistTask) -> AssistTaskOut:
    result = tasks.result_of(task) if task.state == AssistTask.State.DONE else {}
    raw = result.get("groups", [])
    docs = _docs({item["document"] for g in raw for item in g["items"]})
    return AssistTaskOut(
        id=task.pk,
        operation=task.operation,
        state=task.state,
        done=task.done,
        total=task.total,
        error=task.error,
        note=result.get("note", ""),
        groups=_groups(raw, docs),
    )


@router.post("/tasks", response=AssistTaskOut)
def start_task(request: HttpRequest, data: AssistTaskIn) -> AssistTaskOut:
    """Ask the AI model for changes to the selected documents; poll the result. Nothing changes by itself."""
    ids = selected_ids(data, "folder" if data.folder_id is not None else "selection")
    if not ids:
        raise HttpError(400, "Select documents first")
    instruction = " ".join(data.instruction.split())[: ai.MAX_INSTRUCTIONS]
    if data.operation == "custom" and not instruction:
        raise HttpError(400, "Write what should be done")
    task = AssistTask.objects.create(
        operation=data.operation, documents=ids, instruction_enc=tasks.encrypt_instruction(instruction)
    )
    queue.enqueue(Job.Kind.ASSIST, payload={"task": task.pk}, priority=10)
    audit("assist.requested", request=request, operation=data.operation, count=len(ids))
    return _out(task)


@router.get("/tasks/{task_id}", response=AssistTaskOut)
def get_task(request: HttpRequest, task_id: int) -> AssistTaskOut:
    task = AssistTask.objects.filter(pk=task_id).first()
    if task is None:
        raise HttpError(404, "Not found")
    return _out(task)


@router.post("/tasks/{task_id}/cancel", response=AssistTaskOut)
def cancel_task(request: HttpRequest, task_id: int) -> AssistTaskOut:
    AssistTask.objects.filter(
        pk=task_id, state__in=[AssistTask.State.PENDING, AssistTask.State.RUNNING]
    ).update(state=AssistTask.State.CANCELLED)
    return get_task(request, task_id)


# --- Reviews: the assistant looks through documents for hours ------------------------------------


class ReviewIn(AssistSelection):
    scope: Literal["selection", "folder", "all"] = "selection"
    instruction: str = Field("", max_length=1000)
    model: str = Field("", max_length=200)  # "": the model chosen in Settings
    ai: bool = True  # False: only what code finds (pages, duplicates, gaps) — minutes
    think: bool = False  # a thinking model reasons before it answers: slower, more careful
    explore: bool = True  # with the time left, the model works through the task step by step
    context: int = Field(review.DEFAULT_CONTEXT, ge=2048, le=65536)
    start: AwareDatetime | None = None  # later, e.g. tonight
    until: AwareDatetime | None = None  # stop by then and write the report


class ReviewDoc(Schema):
    id: UUID
    title: str
    correspondent: str | None
    document_date: date | None
    page_count: int
    trashed: bool  # changed or deleted since: the suggestion may be out of date
    what: str  # what the model understood reading it


class DebateOut(Schema):
    role: Literal["against", "for", "verdict"]
    text: str


class ReviewPage(Schema):
    document: UUID
    page: int


class OutputOut(Schema):
    pages: list[ReviewPage]
    title: str


class MatchOut(Schema):
    """Two pages that are alike."""

    a: ReviewPage
    b: ReviewPage


class ComposeOut(Schema):
    sources: list[UUID]
    outputs: list[OutputOut]


class FindingOut(Schema):
    id: str
    kind: str
    title: str
    text: str
    confidence: Literal["high", "medium", "low"]
    source: Literal["check", "model"]
    verdict: str  # keep | drop | unsure | "" (not argued about)
    debate: list[DebateOut]
    documents: list[ReviewDoc]
    compose: ComposeOut | None  # pages into documents; apply with POST /alterations/compose
    changes: list[AssistGroupOut]  # details; apply like the assistant's other suggestions
    matches: list[MatchOut]
    decision: str  # applied | dismissed | ""


class JournalOut(Schema):
    at: datetime
    text: str


class ReviewSummary(Schema):
    id: int
    state: str
    step: str
    done: int
    total: int
    error: str
    model: str
    ai: bool
    think: bool
    instruction: str
    documents: int
    created_at: datetime
    start: datetime | None
    until: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    read: bool
    open_findings: int


class ReviewOut(ReviewSummary):
    summary: str
    next_steps: list[str]
    stopped: str
    findings: list[FindingOut]
    journal: list[JournalOut]


class DecisionIn(Schema):
    decision: Literal["applied", "dismissed", "open"]


class ModelOut(Schema):
    name: str
    parameter_size: str
    size: int
    thinking: bool
    current: bool  # the one chosen in Settings


def _review(pk: int) -> AssistTask:
    task = AssistTask.objects.filter(pk=pk, operation=AssistTask.Operation.REVIEW).first()
    if task is None:
        raise HttpError(404, "Not found")
    return task


def _summary(task: AssistTask, result: dict[str, Any] | None = None) -> ReviewSummary:
    o = task.options or {}
    if result is None:
        result = tasks.result_of(task)
    findings = result.get("findings", [])
    open_findings = sum(1 for f in findings if f.get("verdict") != "drop" and not task.decisions.get(f["id"]))
    return ReviewSummary(
        id=task.pk,
        state=task.state,
        step=task.step,
        done=task.done,
        total=task.total,
        error=task.error,
        model=task.model,
        ai=o.get("ai", True) is not False,
        think=bool(o.get("think")),
        instruction=tasks.instruction_of(task),
        documents=len(task.documents),
        created_at=task.created_at,
        start=parse_datetime(o["start"]) if o.get("start") else None,
        until=parse_datetime(o["until"]) if o.get("until") else None,
        started_at=task.started_at,
        finished_at=task.finished_at,
        read=task.read_at is not None,
        open_findings=open_findings,
    )


def _finding(
    f: dict[str, Any],
    docs: dict[str, Document],
    gone: dict[str, Document],
    notes: dict[str, Any],
    decision: str,
) -> FindingOut:
    action = f.get("action") or {}
    refs = []
    for u in f["documents"]:
        d = docs.get(u) or gone.get(u)
        if d is None:
            continue
        refs.append(
            ReviewDoc(
                id=d.uuid,
                title=crypto_fields.get_title(d) or "Untitled",
                correspondent=d.correspondent.name if d.correspondent else None,
                document_date=d.document_date,
                page_count=d.page_count,
                trashed=d.deleted_at is not None,
                what=str((notes.get(u) or {}).get("what") or ""),
            )
        )
    compose = None
    if action.get("type") == "compose":
        compose = ComposeOut(
            sources=action["sources"],
            outputs=[
                OutputOut(
                    pages=[ReviewPage(document=u, page=n) for u, n in o["pages"]], title=o.get("title", "")
                )
                for o in action["outputs"]
            ],
        )
    changes = _groups(action["groups"], docs) if action.get("type") == "changes" else []
    return FindingOut(
        id=f["id"],
        kind=f["kind"],
        title=f["title"],
        text=f["text"],
        confidence=f.get("confidence", "medium"),
        source=f.get("source", "check"),
        verdict=f.get("verdict", ""),
        debate=[DebateOut(**d) for d in f.get("debate", [])],
        documents=refs,
        compose=compose,
        changes=changes,
        matches=[
            MatchOut(a=ReviewPage(document=a, page=p), b=ReviewPage(document=b, page=q))
            for a, p, b, q in f.get("matches", [])
        ],
        decision=decision,
    )


@router.get("/models", response=list[ModelOut])
def models(request: HttpRequest) -> list[ModelOut]:
    """Installed models for a review; a mid-sized one (8B) that runs all night works well."""
    if not ai.configured():
        return []
    try:
        installed = ai.list_models()
    except (ai.ModelUnavailable, ai.ModelFailed):
        return []
    current = get_ai_model()
    return [
        ModelOut(
            name=m.name,
            parameter_size=m.parameter_size,
            size=m.size,
            thinking=m.thinking,
            current=m.name == current,
        )
        for m in installed
    ]


@router.post("/reviews", response=ReviewSummary)
def start_review(request: HttpRequest, data: ReviewIn) -> ReviewSummary:
    """Let the assistant look through documents — now or later, for hours. It only suggests."""
    ids = selected_ids(data, data.scope)
    if not ids:
        raise HttpError(400, "Select documents first")
    model = data.model.strip() or (get_ai_model() if data.ai else "")
    if model and not ai.valid_model_name(model):
        raise HttpError(400, "Invalid model name")
    if data.ai and not (model or get_ai_model()):
        raise HttpError(400, "Choose an AI model, or look through without it")
    now = timezone.now()
    start = data.start if data.start and data.start > now else None
    if data.until and data.until <= (start or now):
        raise HttpError(400, "The end must be after the start")
    instruction = " ".join(data.instruction.split())
    task = AssistTask.objects.create(
        operation=AssistTask.Operation.REVIEW,
        documents=ids,
        instruction_enc=tasks.encrypt_instruction(instruction),
        model=model,
        options={
            "ai": data.ai,
            "think": data.think,
            "explore": data.explore,
            "context": data.context,
            "start": start.isoformat() if start else None,
            "until": data.until.isoformat() if data.until else None,
            "scope": data.scope,
            "folder_id": data.folder_id,
            "subfolders": data.subfolders,
        },
    )
    delay = int((start - now).total_seconds()) if start else 0
    queue.enqueue(Job.Kind.REVIEW, payload={"task": task.pk}, delay=delay, priority=-1)
    audit("assist.review", request=request, count=len(ids), scope=data.scope, ai=data.ai)
    return _summary(task)


@router.get("/reviews", response=list[ReviewSummary])
def list_reviews(request: HttpRequest) -> list[ReviewSummary]:
    return [
        _summary(t)
        for t in AssistTask.objects.filter(operation=AssistTask.Operation.REVIEW).order_by("-created_at")[:50]
    ]


@router.get("/reviews/{review_id}", response=ReviewOut)
def get_review(request: HttpRequest, review_id: int) -> ReviewOut:
    task = _review(review_id)
    result = tasks.result_of(task)
    findings = result.get("findings", [])
    wanted = {u for f in findings for u in f["documents"]}
    for f in findings:
        for g in (f.get("action") or {}).get("groups", []):
            wanted |= {i["document"] for i in g["items"]}
    docs = _docs(wanted)
    gone = {
        str(d.uuid): d
        for d in Document.objects.filter(uuid__in=wanted - set(docs)).select_related("correspondent")
    }
    notes = result.get("notes", {})
    summary = _summary(task, result)
    return ReviewOut(
        **summary.dict(),
        summary=result.get("summary", ""),
        next_steps=result.get("next_steps", []),
        stopped=result.get("stopped", ""),
        findings=[_finding(f, docs, gone, notes, task.decisions.get(f["id"], "")) for f in findings],
        journal=[JournalOut(at=parse_datetime(j["at"]), text=j["text"]) for j in review.journal_of(task)],  # type: ignore[arg-type]
    )


@router.post("/reviews/{review_id}/stop", response=ReviewSummary)
def stop_review(request: HttpRequest, review_id: int) -> ReviewSummary:
    """Stop and write the report with what there is (a review that has not started is cancelled)."""
    task = _review(review_id)
    if task.state == AssistTask.State.PENDING:
        AssistTask.objects.filter(pk=task.pk).update(
            state=AssistTask.State.CANCELLED, finished_at=timezone.now()
        )
        Job.objects.filter(kind=Job.Kind.REVIEW, payload__task=task.pk, state=Job.State.QUEUED).delete()
    elif task.state == AssistTask.State.RUNNING:
        AssistTask.objects.filter(pk=task.pk).update(options={**task.options, "stop": True})
    task.refresh_from_db()
    return _summary(task)


@router.delete("/reviews/{review_id}")
def delete_review(request: HttpRequest, review_id: int) -> dict[str, bool]:
    task = _review(review_id)
    if task.state == AssistTask.State.RUNNING:
        raise HttpError(409, "Stop it first")
    Job.objects.filter(kind=Job.Kind.REVIEW, payload__task=task.pk, state=Job.State.QUEUED).delete()
    task.delete()
    return {"ok": True}


@router.post("/reviews/{review_id}/read", response=ReviewSummary)
def mark_read(request: HttpRequest, review_id: int) -> ReviewSummary:
    task = _review(review_id)
    if task.read_at is None and task.state in (AssistTask.State.DONE, AssistTask.State.FAILED):
        task.read_at = timezone.now()
        task.save(update_fields=["read_at"])
    return _summary(task)


@router.post("/reviews/{review_id}/findings/{finding_id}", response=ReviewSummary)
def decide(request: HttpRequest, review_id: int, finding_id: str, data: DecisionIn) -> ReviewSummary:
    """Remember what the user did with a suggestion (the change itself goes through its own API)."""
    task = _review(review_id)
    with transaction.atomic():
        task = AssistTask.objects.select_for_update().get(pk=task.pk)
        if not any(f["id"] == finding_id for f in tasks.result_of(task).get("findings", [])):
            raise HttpError(404, "Finding not found")
        decisions = {k: v for k, v in task.decisions.items() if k != finding_id}
        if data.decision != "open":
            decisions[finding_id[:20]] = data.decision
        task.decisions = decisions
        task.save(update_fields=["decisions"])
    return _summary(task)
