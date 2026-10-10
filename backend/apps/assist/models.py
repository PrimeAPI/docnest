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
        REVIEW = "review"  # look through the documents for hours: see apps.assist.review

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

    # A review: which model, how long, what it did so far (resumable), what the user did with it.
    model = models.CharField(max_length=200, blank=True)  # "": the model chosen in Settings
    # {"think": bool, "until": ISO time to stop by, "start": ISO time to begin, "scope": "…"}
    options = models.JSONField(default=dict, blank=True)
    step = models.CharField(max_length=200, blank=True)  # what it is doing right now
    started_at = models.DateTimeField(null=True, blank=True)
    journal_enc = models.BinaryField(null=True)  # [{"at", "text"}]: what it did, for the report
    state_enc = models.BinaryField(null=True)  # checkpoint to resume after a restart
    decisions = models.JSONField(default=dict, blank=True)  # finding id -> applied | dismissed
    read_at = models.DateTimeField(null=True, blank=True)  # the user opened the finished report


class DocumentNote(models.Model):
    """What the AI model understood reading a document: kept, so later runs build on it.

    The note quotes the document (parties, references, what it is about), so it is encrypted.
    It belongs to a version of the document's text: a reprocessed document is read again.
    """

    document = models.OneToOneField(
        "documents.Document", on_delete=models.CASCADE, related_name="assist_note", primary_key=True
    )
    note_enc = models.BinaryField()
    text_length = models.PositiveIntegerField(default=0)  # of the text it was read from
    model = models.CharField(max_length=200)
    created_at = models.DateTimeField(default=timezone.now)
