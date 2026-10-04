from __future__ import annotations

from django.db import models
from django.utils import timezone


class Job(models.Model):
    """A unit of background work, claimed by workers with SELECT ... FOR UPDATE SKIP LOCKED."""

    class Kind(models.TextChoices):
        PROCESS_DOCUMENT = "process_document"
        DELETE_STORAGE = "delete_storage"
        TRAIN_CLASSIFIER = "train_classifier"
        REINDEX_DOCUMENT = "reindex_document"

    class State(models.TextChoices):
        QUEUED = "queued"
        RUNNING = "running"
        DONE = "done"
        FAILED = "failed"

    kind = models.CharField(max_length=40, choices=Kind.choices)
    document = models.ForeignKey("documents.Document", null=True, blank=True, on_delete=models.CASCADE)
    payload = models.JSONField(default=dict, blank=True)
    state = models.CharField(max_length=20, choices=State.choices, default=State.QUEUED)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    run_after = models.DateTimeField(default=timezone.now)
    locked_by = models.CharField(max_length=100, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["state", "run_after"])]
        constraints = [
            # At most one active job of a kind per document: prevents duplicate work.
            models.UniqueConstraint(
                fields=["kind", "document"],
                condition=models.Q(state__in=["queued", "running"]),
                name="job_active_unique",
            )
        ]


class WorkerHeartbeat(models.Model):
    worker_id = models.CharField(max_length=100, unique=True)
    last_seen_at = models.DateTimeField(default=timezone.now)


class SystemState(models.Model):
    """Small key/value store for system-wide status flags (e.g. storage needs re-auth)."""

    key = models.CharField(max_length=100, unique=True)
    value = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)
