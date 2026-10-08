from __future__ import annotations

from django.db import models
from django.utils import timezone


class ClassifierModel(models.Model):
    """A trained Naive Bayes model. Features are blind-index term hashes, so no plaintext is stored."""

    target = models.CharField(max_length=40, unique=True)  # document_type | correspondent | tags
    model = models.JSONField(default=dict)
    sample_count = models.PositiveIntegerField(default=0)
    trained_at = models.DateTimeField(default=timezone.now)
