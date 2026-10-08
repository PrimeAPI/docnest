"""Archive statistics for Settings → Statistics: what is stored, how it grows, how fast it is processed."""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from django.db.models import Count, Q, QuerySet, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone
from ninja import Field, Schema

from apps.documents.models import Document, ProcessingEvent
from apps.taxonomy.models import Correspondent, Tag

MONTHS = 12
WEEKS = 12
PROCESSING_DAYS = 90  # processing times of documents uploaded this recently
TOP = 10
# Stages after which a new scan can be read, and after which it is complete.
READABLE_STAGE = "enhance"
DONE_STAGE = "index"
AI_MESSAGE = "Read by the AI model"  # written by pipeline._model_fields
AI_SECONDS = re.compile(r" in (\d+(?:\.\d+)?) s$")


class Counted(Schema):
    name: str
    count: int


class Month(Schema):
    month: str  # YYYY-MM
    documents: int
    pages: int


class StageTime(Schema):
    stage: str
    average_seconds: float
    runs: int


class Processing(Schema):
    readable_median_seconds: float | None = None  # upload → enhanced version viewable
    done_median_seconds: float | None = None  # upload → fully analysed, stored and indexed
    sample: int = 0
    stages: list[StageTime] = Field(default_factory=list)
    ai_documents: int = 0
    ai_average_seconds: float | None = None


class Stats(Schema):
    documents: int
    pages: int
    bytes: int
    correspondents: int
    tags: int
    unread: int
    todo: int
    failed: int
    processing_now: int
    per_week: float  # new documents per week over the last WEEKS weeks
    oldest_document_date: str | None
    months: list[Month]
    types: list[Counted]
    correspondents_top: list[Counted]
    sources: list[Counted]
    processing: Processing


def compute() -> Stats:
    now = timezone.now()
    docs = Document.objects.filter(deleted_at__isnull=True)
    totals = docs.aggregate(
        n=Count("id"),
        pages=Sum("page_count"),
        size=Sum("size"),
        unread=Count("id", filter=Q(read_at__isnull=True)),
        todo=Count("id", filter=Q(status=Document.Status.TODO)),
        failed=Count("id", filter=Q(processing_state=Document.State.FAILED)),
        busy=Count("id", filter=Q(processing_state__in=[Document.State.PENDING, Document.State.RUNNING])),
    )
    oldest = (
        docs.exclude(document_date__isnull=True)
        .order_by("document_date")
        .values_list("document_date", flat=True)
        .first()
    )
    recent = docs.filter(uploaded_at__gte=now - timedelta(weeks=WEEKS)).count()
    return Stats(
        documents=totals["n"],
        pages=totals["pages"] or 0,
        bytes=totals["size"] or 0,
        correspondents=Correspondent.objects.filter(document__deleted_at__isnull=True).distinct().count(),
        tags=Tag.objects.filter(is_suggested=False).count(),
        unread=totals["unread"],
        todo=totals["todo"],
        failed=totals["failed"],
        processing_now=totals["busy"],
        per_week=round(recent / WEEKS, 1),
        oldest_document_date=oldest.isoformat() if oldest else None,
        months=_months(docs, now),
        types=_counted(docs, "document_type__name", "No type"),
        correspondents_top=_counted(docs, "correspondent__name", "Unknown sender"),
        sources=_sources(docs),
        processing=_processing(now),
    )


def _months(docs: QuerySet[Document], now: datetime) -> list[Month]:
    local = timezone.localtime(now)
    index = local.year * 12 + local.month - 1 - (MONTHS - 1)  # months since year 0, counted from 0
    start = local.replace(
        year=index // 12, month=index % 12 + 1, day=1, hour=0, minute=0, second=0, microsecond=0
    )
    rows = (
        docs.filter(uploaded_at__gte=start)
        .annotate(month=TruncMonth("uploaded_at"))
        .values("month")
        .annotate(n=Count("id"), pages=Sum("page_count"))
    )
    found = {row["month"].strftime("%Y-%m"): row for row in rows}
    months: list[Month] = []
    year, month = start.year, start.month
    for _ in range(MONTHS):
        key = f"{year:04d}-{month:02d}"
        row = found.get(key)
        months.append(
            Month(month=key, documents=row["n"] if row else 0, pages=(row["pages"] or 0) if row else 0)
        )
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _counted(docs: QuerySet[Document], field_name: str, missing: str) -> list[Counted]:
    rows = docs.values(field_name).annotate(n=Count("id")).order_by("-n")
    counted = [Counted(name=row[field_name] or missing, count=row["n"]) for row in rows]
    if len(counted) <= TOP:
        return counted
    rest = sum(c.count for c in counted[TOP - 1 :])
    return [*counted[: TOP - 1], Counted(name="Other", count=rest)]


def _sources(docs: QuerySet[Document]) -> list[Counted]:
    rows = docs.values("received_from__name").annotate(n=Count("id")).order_by("-n")
    return [Counted(name=row["received_from__name"] or "Web upload", count=row["n"]) for row in rows]


def _processing(now: datetime) -> Processing:
    since = now - timedelta(days=PROCESSING_DAYS)
    events = (
        ProcessingEvent.objects.filter(
            outcome="ok", document__uploaded_at__gte=since, document__deleted_at__isnull=True
        )
        .values_list("document_id", "document__uploaded_at", "stage", "created_at", "duration_ms")
        .order_by("created_at")
    )
    readable: dict[int, float] = {}
    done: dict[int, float] = {}
    durations: dict[str, list[float]] = defaultdict(list)
    for document_id, uploaded_at, stage, created_at, duration_ms in events:
        durations[stage].append(duration_ms / 1000)
        # The first pass after the upload is what a new scan feels like; reprocessing comes later.
        if stage == READABLE_STAGE and document_id not in readable:
            readable[document_id] = (created_at - uploaded_at).total_seconds()
        if stage == DONE_STAGE and document_id not in done:
            done[document_id] = (created_at - uploaded_at).total_seconds()
    order = ["assemble", "validate", "enhance", "ocr", "analyze", "store", "index"]
    ai = [
        float(match.group(1))
        for message in ProcessingEvent.objects.filter(
            stage="analyze", created_at__gte=since, message__startswith=AI_MESSAGE
        ).values_list("message", flat=True)
        if (match := AI_SECONDS.search(message))
    ]
    return Processing(
        readable_median_seconds=round(statistics.median(readable.values()), 1) if readable else None,
        done_median_seconds=round(statistics.median(done.values()), 1) if done else None,
        sample=len(done),
        stages=[
            StageTime(
                stage=stage,
                average_seconds=round(statistics.fmean(durations[stage]), 1),
                runs=len(durations[stage]),
            )
            for stage in order
            if durations.get(stage)
        ],
        ai_documents=len(ai),
        ai_average_seconds=round(statistics.fmean(ai), 1) if ai else None,
    )
