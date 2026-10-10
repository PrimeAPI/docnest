from __future__ import annotations

from datetime import date
from typing import Any, Literal
from uuid import UUID

from django.http import HttpRequest
from ninja import Field, Router, Schema
from ninja.errors import HttpError

from apps.analysis import ai
from apps.assist import tasks
from apps.assist.models import AssistTask
from apps.audit.service import audit
from apps.documents import crypto_fields
from apps.documents.models import Document
from apps.processing import queue
from apps.processing.models import Job
from apps.taxonomy.models import DocumentType

router = Router(tags=["assist"])


class AssistTaskIn(Schema):
    ids: list[UUID]
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


def _out(task: AssistTask) -> AssistTaskOut:
    result = tasks.result_of(task) if task.state == AssistTask.State.DONE else {}
    raw = result.get("groups", [])
    wanted = {item["document"] for g in raw for item in g["items"]}
    docs = {
        str(d.uuid): d
        for d in Document.objects.filter(uuid__in=wanted, deleted_at__isnull=True)
        .select_related("correspondent")
        .prefetch_related("tags")
    }
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
    return AssistTaskOut(
        id=task.pk,
        operation=task.operation,
        state=task.state,
        done=task.done,
        total=task.total,
        error=task.error,
        note=result.get("note", ""),
        groups=groups,
    )


@router.post("/tasks", response=AssistTaskOut)
def start_task(request: HttpRequest, data: AssistTaskIn) -> AssistTaskOut:
    """Ask the AI model for changes to the selected documents; poll the result. Nothing changes by itself."""
    ids = [str(i) for i in data.ids[: tasks.MAX_DOCUMENTS]]
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
