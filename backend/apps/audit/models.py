from django.db import models
from django.utils import timezone


class AuditLog(models.Model):
    class Actor(models.TextChoices):
        USER = "user"
        SCANNER = "scanner"
        SYSTEM = "system"
        ANONYMOUS = "anonymous"

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor_type = models.CharField(max_length=20, choices=Actor.choices)
    actor_id = models.CharField(max_length=64, blank=True)
    actor_label = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=64, db_index=True)
    target = models.CharField(max_length=200, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
