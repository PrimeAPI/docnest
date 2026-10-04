from __future__ import annotations

from django.db import models


class SearchTerm(models.Model):
    """Blind index entry: a keyed hash of a normalized term. No plaintext."""

    class Field(models.IntegerChoices):
        TITLE = 1
        CONTENT = 2
        META = 3  # correspondent, tags, filename

    id = models.BigAutoField(primary_key=True)
    document = models.ForeignKey("documents.Document", on_delete=models.CASCADE, db_index=False)
    term = models.BigIntegerField()
    field = models.SmallIntegerField(choices=Field.choices)
    tf = models.SmallIntegerField(default=1)

    class Meta:
        indexes = [
            models.Index(fields=["term"], name="search_term_idx"),
            models.Index(fields=["document"], name="search_term_doc_idx"),
        ]


class DocumentStats(models.Model):
    document = models.OneToOneField("documents.Document", on_delete=models.CASCADE, primary_key=True)
    length = models.PositiveIntegerField(default=0)  # number of tokens (content + title)
    signature = models.JSONField(default=list)  # term hashes used for similarity (series detection)
