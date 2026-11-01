from __future__ import annotations

from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


class Folder(models.Model):
    """A filing folder ("Ablage"). Folders nest; a document lives in at most one folder."""

    name = models.CharField(max_length=80)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    color = models.CharField(max_length=20, default="slate")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                "parent",
                Lower("name"),
                name="folder_name_ci_unique",
                condition=models.Q(parent__isnull=False),
            ),
            models.UniqueConstraint(
                Lower("name"), name="folder_root_name_ci_unique", condition=models.Q(parent__isnull=True)
            ),
        ]

    def __str__(self) -> str:
        return self.name


class DocumentType(models.Model):
    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(max_length=80, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Tag(models.Model):
    name = models.CharField(max_length=80)
    color = models.CharField(max_length=20, default="slate")
    is_suggested = models.BooleanField(
        default=False, help_text="Created automatically, awaiting confirmation"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(Lower("name"), name="tag_name_ci_unique")]

    def __str__(self) -> str:
        return self.name


class TagAlias(models.Model):
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="aliases")
    alias = models.CharField(max_length=80)

    class Meta:
        constraints = [models.UniqueConstraint(Lower("alias"), name="tag_alias_ci_unique")]


class Correspondent(models.Model):
    """Sender / organisation of a document."""

    name = models.CharField(max_length=150)
    aliases = models.JSONField(default=list, blank=True, help_text="Alternative spellings to match in text")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(Lower("name"), name="correspondent_name_ci_unique")]

    def __str__(self) -> str:
        return self.name


class Series(models.Model):
    """A group of recurring documents (e.g. monthly payslips)."""

    class Cadence(models.TextChoices):
        MONTHLY = "monthly"
        QUARTERLY = "quarterly"
        YEARLY = "yearly"
        IRREGULAR = "irregular"

    name = models.CharField(max_length=150)
    correspondent = models.ForeignKey(Correspondent, null=True, blank=True, on_delete=models.SET_NULL)
    document_type = models.ForeignKey(DocumentType, null=True, blank=True, on_delete=models.SET_NULL)
    cadence = models.CharField(max_length=20, choices=Cadence.choices, default=Cadence.IRREGULAR)
    is_suggested = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "series"

    def __str__(self) -> str:
        return self.name


class MatchRule(models.Model):
    """User-defined rule: if the text contains a phrase, set correspondent/type/tags."""

    phrase = models.CharField(max_length=200)
    correspondent = models.ForeignKey(Correspondent, null=True, blank=True, on_delete=models.CASCADE)
    document_type = models.ForeignKey(DocumentType, null=True, blank=True, on_delete=models.CASCADE)
    tags = models.ManyToManyField(Tag, blank=True)
    created_at = models.DateTimeField(default=timezone.now)


class FilingProposal(models.Model):
    """Suggested subfolders for documents the user selected; computed by a job, applied by the user.

    Holds document UUIDs and folder names only: titles stay encrypted on their documents.
    """

    class State(models.TextChoices):
        PENDING = "pending"
        DONE = "done"
        FAILED = "failed"

    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING)
    documents = models.JSONField(default=list)  # UUIDs of the selected documents
    options = models.JSONField(default=dict, blank=True)  # see apps.taxonomy.filing.Options.to_store
    result = models.JSONField(default=dict, blank=True)  # see apps.taxonomy.filing.suggest
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)


class FilingSuggestion(models.Model):
    """Where a new document should be filed, planned automatically (apps.taxonomy.inbox_filing).

    The document only moves when the user approves. The target is the deepest folder that
    existed when it was planned plus the folders to create below it, so a folder created
    meanwhile (another approved suggestion) is found again instead of made twice.
    """

    class State(models.TextChoices):
        PENDING = "pending"  # a target, waiting for the user
        NONE = "none"  # nothing fitting yet: planned again when new documents arrive
        ACCEPTED = "accepted"
        DISMISSED = "dismissed"  # rejected, or the user filed the document themselves

    document = models.OneToOneField(
        "documents.Document", on_delete=models.CASCADE, related_name="filing_suggestion"
    )
    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING, db_index=True)
    # Deleting it makes the target meaningless: the suggestion goes, and the document is planned anew.
    anchor = models.ForeignKey(Folder, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    new = models.JSONField(default=list, blank=True)  # folder names to create below the anchor
    reason_enc = models.BinaryField(null=True)  # may quote title words: encrypted like the titles
    named_by = models.CharField(max_length=10, blank=True)  # "ai" | "rules"
    created_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)
