"""
Audit trail - the append-only history of everything that mattered.

Why ResQNet needs this
----------------------
This platform moves public relief money. Months later somebody will ask "who
approved case RPT-2026-0031, when, and on what evidence?" If the answer lives
only in the current row of a table, it is gone the moment that row is updated.
An audit trail records the *transitions*, not just the destination.

"Append-only" here means: the application only ever inserts. There is no update
path and no delete path in any serializer, view, or admin screen, and the model
actively refuses both.

An honest caveat (Section 20 asks for this explicitly): this is a normal
database table. A database superuser with direct SQL access could still alter it.
It is *tamper-evident at the application layer*, not cryptographically immutable.
If stronger guarantees are ever needed, hash-chaining each row to its predecessor
is the next step - but that is a deliberate decision, not something to claim by
default.
"""

from django.conf import settings
from django.db import models


class AuditAction(models.TextChoices):
    """The catalogue of auditable actions from Section 20."""

    REPORT_CREATED = "REPORT_CREATED", "Report created"
    REPORT_SUBMITTED = "REPORT_SUBMITTED", "Report submitted"
    REPORT_UPDATED = "REPORT_UPDATED", "Report updated"
    REPORT_CANCELLED = "REPORT_CANCELLED", "Report cancelled"
    EVIDENCE_UPLOADED = "EVIDENCE_UPLOADED", "Evidence uploaded"

    TRIAGE_COMPLETED = "TRIAGE_COMPLETED", "Triage completed"
    OFFICER_ASSIGNED = "OFFICER_ASSIGNED", "Officer assigned"
    ASSIGNMENT_ACCEPTED = "ASSIGNMENT_ACCEPTED", "Assignment accepted"
    ASSIGNMENT_REASSIGNED = "ASSIGNMENT_REASSIGNED", "Assignment reassigned"

    INSPECTION_STARTED = "INSPECTION_STARTED", "Inspection started"
    INSPECTION_COMPLETED = "INSPECTION_COMPLETED", "Inspection completed"
    OFFICER_SIGNED = "OFFICER_SIGNED", "Officer signed off"

    COMPENSATION_CALCULATED = "COMPENSATION_CALCULATED", "Compensation calculated"

    ADMIN_APPROVED = "ADMIN_APPROVED", "Administrator approved"
    ADMIN_REJECTED = "ADMIN_REJECTED", "Administrator rejected"
    REVIEW_REQUESTED = "REVIEW_REQUESTED", "Re-inspection requested"

    PAYOUT_INITIATED = "PAYOUT_INITIATED", "Payout initiated"
    PAYOUT_COMPLETED = "PAYOUT_COMPLETED", "Payout completed"

    USER_REGISTERED = "USER_REGISTERED", "User registered"
    USER_LOGGED_IN = "USER_LOGGED_IN", "User logged in"
    USER_LOGGED_OUT = "USER_LOGGED_OUT", "User logged out"
    OFFICER_CREATED = "OFFICER_CREATED", "Officer account created"

    OFFLINE_SYNC = "OFFLINE_SYNC", "Offline data synchronised"


class AuditEventQuerySet(models.QuerySet):
    """Block bulk rewrites at the queryset level, not just on single rows."""

    def update(self, **kwargs):
        raise NotImplementedError("Audit events are append-only and cannot be updated.")

    def delete(self):
        raise NotImplementedError("Audit events are append-only and cannot be deleted.")


class AuditEvent(models.Model):
    """One immutable record of one meaningful action."""

    # What was touched. We store the app/model name and the primary key as plain
    # values rather than a foreign key, so an audit row survives even if the
    # thing it describes is later removed.
    entity_type = models.CharField(max_length=60, db_index=True)
    entity_id = models.CharField(max_length=64, db_index=True)
    entity_label = models.CharField(
        max_length=120,
        blank=True,
        help_text="Human-readable identifier, e.g. the report reference number.",
    )

    action = models.CharField(max_length=40, choices=AuditAction.choices, db_index=True)

    # Who did it. SET_NULL keeps the history intact if the account is deleted.
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    performed_by_username = models.CharField(
        max_length=150,
        blank=True,
        help_text="Snapshot of the username, preserved even if the account is removed.",
    )
    performed_by_role = models.CharField(max_length=20, blank=True)

    # The transition itself.
    previous_state = models.CharField(max_length=40, blank=True)
    new_state = models.CharField(max_length=40, blank=True)

    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Extra context: scores, amounts, reasons. Must not contain secrets.",
    )

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = AuditEventQuerySet.as_manager()

    class Meta:
        ordering = ["-timestamp", "-id"]
        indexes = [
            models.Index(fields=["entity_type", "entity_id"]),
            models.Index(fields=["action", "-timestamp"]),
        ]
        verbose_name = "audit event"
        verbose_name_plural = "audit events"

    def __str__(self) -> str:
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.action} {self.entity_type}#{self.entity_id}"

    def save(self, *args, **kwargs):
        """Allow the first insert; refuse every subsequent write."""
        if self.pk is not None:
            raise ValueError("Audit events are append-only and cannot be modified.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Audit events are append-only and cannot be deleted.")
