from django.db import models
from django.utils import timezone


class AssistTask(models.Model):
    """Something the user asked the AI model to do to selected documents; computed by a job.

    The result only proposes changes; the user applies the ones they want. Instruction and
    result name titles and senders, so both are encrypted like the documents' own fields.
    """

    class Operation(models.TextChoices):
        RENAME = "rename"  # consistent titles for documents that belong together
        CUSTOM = "custom"  # the user's own wish, per document

    class State(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        DONE = "done"
        FAILED = "failed"
        CANCELLED = "cancelled"

    operation = models.CharField(max_length=10, choices=Operation.choices)
    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING)
    documents = models.JSONField(default=list)  # UUIDs of the selected documents
    instruction_enc = models.BinaryField(null=True)
    done = models.PositiveIntegerField(default=0)  # model requests answered so far
    total = models.PositiveIntegerField(default=0)  # model requests needed
    result_enc = models.BinaryField(null=True)  # see apps.assist.tasks.run
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
