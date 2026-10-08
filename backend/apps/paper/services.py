"""Where a paper original lies: stacks, positions and putting documents away.

A location's stack is ordered bottom → top by (batch, scan time): every
"put away" batch lies on top of the previous ones, and inside a batch the
newest scan lies on top. One page of the original is one sheet (simplex).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.documents.models import Document
from apps.paper.models import Location

SHEET_MM = 0.1  # 80 g/m² office paper


def sheets_of(document: Document) -> int:
    return max(1, document.original_page_count or document.page_count or 1)


def location_paths() -> dict[int, str]:
    """Full display path ("Cabinet / Binder 3") of every location, keyed by id."""
    rows = {
        pk: (name, parent) for pk, name, parent in Location.objects.values_list("pk", "name", "parent_id")
    }
    paths: dict[int, str] = {}

    def resolve(pk: int) -> str:
        if pk not in paths:
            name, parent = rows[pk]
            paths[pk] = f"{resolve(parent)} / {name}" if parent in rows else name
        return paths[pk]

    for pk in rows:
        resolve(pk)
    return paths


def location_subtree(location_id: int) -> set[int]:
    children: dict[int | None, list[int]] = {}
    for pk, parent in Location.objects.values_list("pk", "parent_id"):
        children.setdefault(parent, []).append(pk)
    found, todo = set(), [location_id]
    while todo:
        pk = todo.pop()
        if pk in found:
            continue
        found.add(pk)
        todo.extend(children.get(pk, []))
    return found


def active() -> QuerySet[Document]:
    return Document.objects.filter(deleted_at__isnull=True)


def pending() -> QuerySet[Document]:
    """Documents with a paper original that has not been put away yet, oldest scan first."""
    return active().filter(has_paper=True, paper_location__isnull=True).order_by("uploaded_at", "id")


def stack(location_id: int) -> list[Document]:
    """Documents in a location, bottom (put away first, oldest scan) to top."""
    return list(
        active()
        .filter(paper_location_id=location_id)
        .select_related("correspondent")
        .order_by("paper_placed_at", "uploaded_at", "id")
    )


@dataclass
class Neighbour:
    id: str
    title: str


@dataclass
class Position:
    index_from_top: int  # 1 = top of the stack
    count: int
    sheets: int
    sheets_above: int
    sheets_below: int
    total_sheets: int
    capacity: int
    above: Neighbour | None
    below: Neighbour | None

    @property
    def mm_from_top(self) -> float:
        return round(self.sheets_above * SHEET_MM, 1)

    @property
    def mm_from_bottom(self) -> float:
        return round(self.sheets_below * SHEET_MM, 1)


def position(document: Document, title_of: Callable[[Document], str] | None = None) -> Position | None:
    if document.paper_location_id is None:
        return None
    from apps.documents import crypto_fields

    title = title_of or crypto_fields.get_title
    location = Location.objects.get(pk=document.paper_location_id)
    docs = stack(location.pk)
    ids = [d.pk for d in docs]
    if document.pk not in ids:
        return None
    i = ids.index(document.pk)
    sheets = [sheets_of(d) for d in docs]
    above = docs[i + 1] if i + 1 < len(docs) else None
    below = docs[i - 1] if i > 0 else None
    return Position(
        index_from_top=len(docs) - i,
        count=len(docs),
        sheets=sheets[i],
        sheets_above=sum(sheets[i + 1 :]),
        sheets_below=sum(sheets[:i]),
        total_sheets=sum(sheets),
        capacity=location.capacity,
        above=Neighbour(str(above.uuid), title(above)) if above else None,
        below=Neighbour(str(below.uuid), title(below)) if below else None,
    )


def place(location: Location, documents: list[Document], at: datetime | None = None) -> int:
    """Put documents away in one batch on top of the location's stack."""
    at = at or timezone.now()
    with transaction.atomic():
        n = 0
        for document in documents:
            document.paper_location = location
            document.paper_placed_at = at
            document.has_paper = True
            document.save(update_fields=["paper_location", "paper_placed_at", "has_paper", "updated_at"])
            n += 1
    return n
