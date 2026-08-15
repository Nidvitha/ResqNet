"""
Dispatch models - the record of which officer was sent to which case, and why.

`selection_reason` and `selection_score` exist for the same reason the triage
breakdown does: the platform must be able to explain itself. When an
administrator asks "why did Officer Rao get this case and not Officer Iyer?",
the answer is stored on the row, not reconstructed by re-running the algorithm
against data that has since changed.
"""

from django.conf import settings
from django.db import models

from reports.models import DisasterReport


class AssignmentStatus(models.TextChoices):
    ASSIGNED = "ASSIGNED", "Assigned"
    ACCEPTED = "ACCEPTED", "Accepted by officer"
    IN_PROGRESS = "IN_PROGRESS", "Inspection in progress"
    COMPLETED = "COMPLETED", "Completed"
    REASSIGNED = "REASSIGNED", "Reassigned to another officer"
    CANCELLED = "CANCELLED", "Cancelled"


class Assignment(models.Model):
    """One officer dispatched to one report."""

    report = models.ForeignKey(DisasterReport, on_delete=models.CASCADE, related_name="assignments")
    officer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="assignments",
        limit_choices_to={"role": "FIELD_OFFICER"},
    )

    status = models.CharField(
        max_length=15,
        choices=AssignmentStatus.choices,
        default=AssignmentStatus.ASSIGNED,
        db_index=True,
    )
    is_automatic = models.BooleanField(
        default=True, help_text="False when an administrator assigned this by hand."
    )

    # --- Why this officer --------------------------------------------------
    distance_km = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Straight-line distance from the officer's base to the incident.",
    )
    selection_score = models.DecimalField(
        max_digits=8, decimal_places=2, default=0, help_text="Higher is a better match."
    )
    selection_reason = models.TextField(
        blank=True, help_text="Plain-English explanation of why this officer was chosen."
    )

    assigned_at = models.DateTimeField(auto_now_add=True, db_index=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    due_by = models.DateTimeField(
        null=True, blank=True, help_text="Derived from the triage response target."
    )

    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignments_made",
        help_text="Null when the dispatch engine assigned automatically.",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-assigned_at"]
        indexes = [
            models.Index(fields=["officer", "status"]),
            models.Index(fields=["status", "-assigned_at"]),
        ]
        constraints = [
            # One report can only have one live assignment at a time. Reassigned
            # and cancelled rows are excluded so history is preserved.
            models.UniqueConstraint(
                fields=["report"],
                condition=models.Q(status__in=["ASSIGNED", "ACCEPTED", "IN_PROGRESS"]),
                name="one_active_assignment_per_report",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.report.reference} -> {self.officer.get_full_name() or self.officer.username}"

    @property
    def is_active(self) -> bool:
        return self.status in {
            AssignmentStatus.ASSIGNED,
            AssignmentStatus.ACCEPTED,
            AssignmentStatus.IN_PROGRESS,
        }

    @property
    def is_overdue(self) -> bool:
        from django.utils import timezone

        return bool(self.due_by and self.is_active and timezone.now() > self.due_by)
