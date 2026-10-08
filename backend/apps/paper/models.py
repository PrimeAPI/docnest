from __future__ import annotations

from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

DEFAULT_CAPACITY = 500  # sheets; an 8 cm lever-arch binder holds about 500 sheets of 80 g/m² paper


class Location(models.Model):
    """A physical place for paper originals (cabinet, shelf, binder, box). Locations nest.

    Documents are put away in batches ("everything scanned so far goes into
    this binder"). Inside a location the newest batch lies on top, and within a
    batch the documents are stacked in scan order (newest on top), so the
    position of a sheet can be estimated from the sheets above and below it.
    """

    name = models.CharField(max_length=80)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    capacity = models.PositiveIntegerField(default=DEFAULT_CAPACITY)  # sheets
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                "parent",
                Lower("name"),
                name="paper_location_name_ci_unique",
                condition=models.Q(parent__isnull=False),
            ),
            models.UniqueConstraint(
                Lower("name"),
                name="paper_location_root_name_ci_unique",
                condition=models.Q(parent__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        return self.name
