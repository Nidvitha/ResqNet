"""
Reports - the citizen's damage report and its photographic evidence.

The `DisasterReport` row is the *case*. Everything else in ResQNet hangs off it:
triage scores it, dispatch assigns an officer to it, an inspection verifies it,
compensation is calculated for it, and an administrator approves it.

Two design choices worth understanding:

* **Damage indicators are individual boolean columns, not free text.** The triage
  engine needs to score "was the roof completely collapsed?" deterministically.
  A checkbox can be scored; a paragraph cannot.
* **Status is never assigned directly.** All changes go through
  `DisasterReport.apply_transition()`, which consults the state machine.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from .states import (
    CITIZEN_EDITABLE_STATES,
    CaseStatus,
    assert_transition,
    progress_percent,
)
from .validators import secure_upload_path, validate_image_upload


class DisasterType(models.TextChoices):
    FLOOD = "FLOOD", "Flood"
    EARTHQUAKE = "EARTHQUAKE", "Earthquake"
    CYCLONE = "CYCLONE", "Cyclone / Storm"
    LANDSLIDE = "LANDSLIDE", "Landslide"
    FIRE = "FIRE", "Fire"
    DROUGHT = "DROUGHT", "Drought"
    OTHER = "OTHER", "Other"


class DamageCategory(models.TextChoices):
    RESIDENTIAL = "RESIDENTIAL", "Residential property"
    COMMERCIAL = "COMMERCIAL", "Commercial property"
    AGRICULTURAL = "AGRICULTURAL", "Agricultural land / crops"
    INFRASTRUCTURE = "INFRASTRUCTURE", "Public infrastructure"
    LIVESTOCK = "LIVESTOCK", "Livestock"
    OTHER = "OTHER", "Other"


class TriageLevel(models.TextChoices):
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"
    CRITICAL = "CRITICAL", "Critical"


class DisasterReport(models.Model):
    """One citizen-submitted damage report - the case at the centre of ResQNet."""

    upload_subdirectory = "reports"

    reference = models.CharField(
        max_length=24,
        unique=True,
        editable=False,
        db_index=True,
        help_text="Human-friendly case number, e.g. RQN-2026-000042.",
    )
    citizen = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reports",
        help_text="Owner of the report. PROTECT because relief history must not vanish.",
    )

    # --- What happened -----------------------------------------------------
    disaster_type = models.CharField(max_length=20, choices=DisasterType.choices, db_index=True)
    damage_category = models.CharField(max_length=20, choices=DamageCategory.choices)
    description = models.TextField(help_text="The citizen's account of the damage.")

    # --- Where it happened -------------------------------------------------
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    address = models.TextField(blank=True)
    district = models.CharField(max_length=100, blank=True, db_index=True)
    location_source = models.CharField(
        max_length=20,
        choices=[("GPS", "Browser GPS"), ("MANUAL", "Manually entered"), ("EXIF", "Photo metadata")],
        default="GPS",
        help_text="How the coordinates were obtained. Recorded for transparency.",
    )

    # --- Damage indicators: the raw inputs the triage engine scores ---------
    roof_collapsed = models.BooleanField(default=False, verbose_name="Roof completely collapsed")
    people_trapped = models.BooleanField(default=False, verbose_name="People trapped")
    exposed_live_wires = models.BooleanField(default=False, verbose_name="Exposed live wires")
    standing_water = models.BooleanField(default=False, verbose_name="Standing water")
    road_blocked = models.BooleanField(default=False, verbose_name="Road blocked")
    major_structural_damage = models.BooleanField(default=False, verbose_name="Major structural damage")
    wall_damage = models.BooleanField(default=False, verbose_name="Wall damage")
    people_affected = models.PositiveSmallIntegerField(
        default=0, help_text="Number of people affected at this location."
    )
    medical_assistance_needed = models.BooleanField(default=False)

    # --- Workflow state ----------------------------------------------------
    status = models.CharField(
        max_length=25,
        choices=CaseStatus.choices,
        default=CaseStatus.DRAFT,
        db_index=True,
        editable=False,
        help_text="Changed only through apply_transition(); never assigned directly.",
    )
    priority_score = models.PositiveSmallIntegerField(default=0, db_index=True)
    triage_level = models.CharField(
        max_length=10, choices=TriageLevel.choices, blank=True, db_index=True
    )

    # --- Offline sync ------------------------------------------------------
    idempotency_key = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text=(
            "Client-generated unique key. If a phone retries a queued submission "
            "after a flaky reconnect, the duplicate is recognised and ignored."
        ),
    )
    created_offline = models.BooleanField(default=False)
    client_created_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the citizen actually filled the form, which may be days before it synced.",
    )

    # --- Timestamps --------------------------------------------------------
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-priority_score", "-created_at"]
        indexes = [
            models.Index(fields=["status", "-priority_score"]),
            models.Index(fields=["district", "status"]),
            models.Index(fields=["triage_level", "-created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(latitude__gte=-90) & models.Q(latitude__lte=90),
                name="report_latitude_within_range",
            ),
            models.CheckConstraint(
                condition=models.Q(longitude__gte=-180) & models.Q(longitude__lte=180),
                name="report_longitude_within_range",
            ),
            # A blank key must be allowed many times over; a real key only once.
            models.UniqueConstraint(
                fields=["citizen", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="unique_idempotency_key_per_citizen",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.reference} - {self.get_disaster_type_display()} ({self.status})"

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = self._generate_reference()
        super().save(*args, **kwargs)

    @staticmethod
    def _generate_reference() -> str:
        """RQN-<year>-<zero-padded sequence>, restarting each year."""
        year = timezone.now().year
        prefix = f"RQN-{year}-"
        last = (
            DisasterReport.objects.filter(reference__startswith=prefix)
            .order_by("-reference")
            .values_list("reference", flat=True)
            .first()
        )
        sequence = int(last.rsplit("-", 1)[1]) + 1 if last else 1
        return f"{prefix}{sequence:06d}"

    # --- State machine entry point -----------------------------------------
    def apply_transition(self, target: str, *, role: str = "SYSTEM", save: bool = True) -> str:
        """
        Move the case to `target`, or raise `InvalidTransition`.

        Returns the previous status so the caller can record it in the audit
        trail. This is the *only* supported way to change `status`.
        """
        previous = self.status
        assert_transition(previous, target, role=role)
        self.status = target

        if target == CaseStatus.SUBMITTED and self.submitted_at is None:
            self.submitted_at = timezone.now()
        if target in {CaseStatus.PAYOUT_COMPLETED, CaseStatus.CANCELLED, CaseStatus.REJECTED}:
            self.closed_at = timezone.now()

        if save:
            self.save(update_fields=["status", "submitted_at", "closed_at", "updated_at"])
        return previous

    # --- Read-only helpers used by the API and templates -------------------
    @property
    def is_editable_by_citizen(self) -> bool:
        return self.status in CITIZEN_EDITABLE_STATES

    @property
    def progress_percent(self) -> int:
        return progress_percent(self.status)

    @property
    def active_assignment(self):
        """The current officer assignment, if one exists."""
        return self.assignments.exclude(status__in=["REASSIGNED", "CANCELLED"]).order_by("-assigned_at").first()

    def damage_indicators(self) -> dict[str, bool | int]:
        """The exact inputs the triage engine consumes. Kept in one place."""
        return {
            "roof_collapsed": self.roof_collapsed,
            "people_trapped": self.people_trapped,
            "exposed_live_wires": self.exposed_live_wires,
            "standing_water": self.standing_water,
            "road_blocked": self.road_blocked,
            "major_structural_damage": self.major_structural_damage,
            "wall_damage": self.wall_damage,
            "medical_assistance_needed": self.medical_assistance_needed,
            "people_affected": self.people_affected,
        }


class ReportPhoto(models.Model):
    """
    Photographic evidence attached to a report.

    Separate model rather than columns on the report, because one report may
    carry many photos and a one-to-many relationship is what a foreign key
    expresses. Validation runs on upload; the stored filename is generated, never
    taken from the client.
    """

    upload_subdirectory = "reports/evidence"

    report = models.ForeignKey(DisasterReport, on_delete=models.CASCADE, related_name="photos")
    image = models.ImageField(upload_to=secure_upload_path, validators=[validate_image_upload])
    caption = models.CharField(max_length=200, blank=True)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="uploaded_photos"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    # EXIF coordinates, when the phone recorded them. Stored separately from the
    # report's own location so the two can be compared during verification.
    exif_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    exif_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    file_size = models.PositiveIntegerField(default=0)
    width = models.PositiveSmallIntegerField(default=0)
    height = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["uploaded_at"]

    def __str__(self) -> str:
        return f"Photo for {self.report.reference}"

    def save(self, *args, **kwargs):
        # Cache the dimensions so listing pages never have to open the files.
        if self.image and not self.file_size:
            self.file_size = self.image.size
            try:
                self.width, self.height = self.image.width, self.image.height
            except Exception:  # noqa: BLE001 - metadata is a nicety, not a blocker
                pass
        super().save(*args, **kwargs)
