"""
Inspections - the field officer's verified assessment of the reported damage.

This is the hinge of the whole platform. Everything before it is a *claim*;
everything after it is based on *verification*. The compensation engine reads
these severity ratings and nothing else - never the citizen's original report -
because relief money must follow what a trained officer confirmed on site.

Severity is a graded scale rather than a yes/no. "Wall damage" that is hairline
cracking and "wall damage" that is a collapsed load-bearing wall cannot attract
the same payment, so the officer records *how bad*, and the relief matrix
multiplies the base amount by that grade.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from dispatch.models import Assignment
from reports.models import DisasterReport
from reports.validators import secure_upload_path, validate_image_upload


class DamageSeverity(models.TextChoices):
    """Graded assessment for each damage type. Ordering matters - see SEVERITY_FACTOR."""

    NONE = "NONE", "No damage"
    MINOR = "MINOR", "Minor"
    MODERATE = "MODERATE", "Moderate"
    SEVERE = "SEVERE", "Severe"
    DESTROYED = "DESTROYED", "Completely destroyed"


#: Proportion of the full relief amount each grade attracts.
#: Deterministic and published - Section 17 requires explainable calculations.
SEVERITY_FACTOR = {
    DamageSeverity.NONE: 0.0,
    DamageSeverity.MINOR: 0.25,
    DamageSeverity.MODERATE: 0.50,
    DamageSeverity.SEVERE: 0.80,
    DamageSeverity.DESTROYED: 1.00,
}


class InspectionStatus(models.TextChoices):
    STARTED = "STARTED", "Inspection started"
    COMPLETED = "COMPLETED", "Inspection completed"
    SIGNED = "SIGNED", "Signed off by officer"


class Inspection(models.Model):
    """One officer's on-site assessment of one report."""

    upload_subdirectory = "inspections"

    report = models.OneToOneField(
        DisasterReport, on_delete=models.CASCADE, related_name="inspection"
    )
    assignment = models.ForeignKey(
        Assignment, on_delete=models.PROTECT, related_name="inspections"
    )
    officer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="inspections",
        limit_choices_to={"role": "FIELD_OFFICER"},
    )

    status = models.CharField(
        max_length=12, choices=InspectionStatus.choices, default=InspectionStatus.STARTED,
        db_index=True,
    )

    # --- The verified checklist -------------------------------------------
    structural_damage = models.CharField(
        max_length=10, choices=DamageSeverity.choices, default=DamageSeverity.NONE
    )
    roof_damage = models.CharField(
        max_length=10, choices=DamageSeverity.choices, default=DamageSeverity.NONE
    )
    wall_damage = models.CharField(
        max_length=10, choices=DamageSeverity.choices, default=DamageSeverity.NONE
    )
    foundation_damage = models.CharField(
        max_length=10, choices=DamageSeverity.choices, default=DamageSeverity.NONE
    )
    electrical_damage = models.CharField(
        max_length=10, choices=DamageSeverity.choices, default=DamageSeverity.NONE
    )
    water_damage = models.CharField(
        max_length=10, choices=DamageSeverity.choices, default=DamageSeverity.NONE
    )
    household_damage = models.CharField(
        max_length=10, choices=DamageSeverity.choices, default=DamageSeverity.NONE,
        help_text="Furniture, appliances and other household contents.",
    )
    category_data = models.JSONField(
        default=dict,
        blank=True,
        help_text="Verified fields specific to the report's damage category.",
    )

    people_affected = models.PositiveSmallIntegerField(
        default=0, help_text="Verified count, which may differ from the citizen's estimate."
    )
    is_habitable = models.BooleanField(
        default=True, help_text="Can the family safely remain in the property?"
    )
    requires_immediate_relief = models.BooleanField(default=False)

    estimated_damage_value = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Officer's own estimate in rupees. Advisory - the relief matrix decides the payment.",
    )
    remarks = models.TextField(blank=True, help_text="Officer's field notes.")

    # --- Where and when the officer actually was --------------------------
    inspection_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        help_text="Officer's GPS position at sign-off, for on-site verification.",
    )
    inspection_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    signed_at = models.DateTimeField(null=True, blank=True)
    signature_name = models.CharField(
        max_length=150, blank=True,
        help_text="Typed name recorded at sign-off. Not a cryptographic signature.",
    )

    # --- Offline sync ------------------------------------------------------
    idempotency_key = models.CharField(max_length=64, blank=True, default="", db_index=True)
    completed_offline = models.BooleanField(default=False)
    client_completed_at = models.DateTimeField(
        null=True, blank=True, help_text="When the officer finished on site, before syncing."
    )
    synced_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["officer", "status"])]
        constraints = [
            models.UniqueConstraint(
                fields=["officer", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="unique_inspection_idempotency_key_per_officer",
            ),
        ]

    def __str__(self) -> str:
        return f"Inspection of {self.report.reference} by {self.officer.username}"

    @property
    def is_editable(self) -> bool:
        """Once signed, an inspection is a finished legal record (Section 6)."""
        return self.status != InspectionStatus.SIGNED

    @property
    def duration_minutes(self) -> int | None:
        if not self.completed_at:
            return None
        return int((self.completed_at - self.started_at).total_seconds() // 60)

    def severity_map(self) -> dict[str, str]:
        """The verified findings, keyed the way the relief matrix expects."""
        return {
            "structural_damage": self.structural_damage,
            "roof_damage": self.roof_damage,
            "wall_damage": self.wall_damage,
            "foundation_damage": self.foundation_damage,
            "electrical_damage": self.electrical_damage,
            "water_damage": self.water_damage,
            "household_damage": self.household_damage,
        }

    def has_any_damage(self) -> bool:
        return any(value != DamageSeverity.NONE for value in self.severity_map().values())


class InspectionPhoto(models.Model):
    """
    Verified photographic evidence captured by the officer.

    Distinct from `ReportPhoto`: those are the citizen's claim, these are the
    officer's verification. An administrator reviewing a case needs to see both,
    and needs to know which is which.
    """

    upload_subdirectory = "inspections/evidence"

    inspection = models.ForeignKey(Inspection, on_delete=models.CASCADE, related_name="photos")
    image = models.ImageField(upload_to=secure_upload_path, validators=[validate_image_upload])
    caption = models.CharField(max_length=200, blank=True)
    damage_area = models.CharField(
        max_length=30, blank=True, help_text="Which part of the property this photo shows."
    )

    captured_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    captured_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    file_size = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["uploaded_at"]

    def __str__(self) -> str:
        return f"Inspection photo for {self.inspection.report.reference}"

    def save(self, *args, **kwargs):
        if self.image and not self.file_size:
            self.file_size = self.image.size
        super().save(*args, **kwargs)
