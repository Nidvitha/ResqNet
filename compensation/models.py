"""
Compensation - the configurable relief matrix and the calculated claim.

Section 17 asks for a *configurable* matrix and forbids hardcoding amounts in
views. So the rupee figures live in `ReliefRule` rows that an administrator can
edit, and the calculation is stored as itemised `CompensationItem` lines rather
than a single opaque total.

Money uses `DecimalField`, never `FloatField`. Binary floating point cannot
represent 0.1 exactly, and errors compound across a payout run - a well-known
class of financial bug that decimals avoid entirely.
"""

from django.conf import settings
from django.db import models

from inspections.models import Inspection
from reports.models import DisasterReport


class ReliefRule(models.Model):
    """
    One line of the relief matrix: "roof damage is worth up to Rs 80,000".

    `damage_field` names a key from `Inspection.severity_map()`. The engine
    multiplies `base_amount` by the severity factor for whatever grade the
    officer recorded, so a severe roof and a minor roof are paid differently
    from the same rule.
    """

    DAMAGE_FIELD_CHOICES = [
        ("structural_damage", "Structural damage"),
        ("roof_damage", "Roof damage"),
        ("wall_damage", "Wall damage"),
        ("foundation_damage", "Foundation damage"),
        ("electrical_damage", "Electrical damage"),
        ("water_damage", "Water damage"),
        ("household_damage", "Household damage"),
    ]

    damage_field = models.CharField(max_length=30, choices=DAMAGE_FIELD_CHOICES, unique=True)
    label = models.CharField(max_length=120, help_text="Wording shown on the citizen's breakdown.")
    base_amount = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text="Full relief amount in rupees for damage graded DESTROYED.",
    )
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveSmallIntegerField(default=0)
    notes = models.TextField(blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "damage_field"]
        verbose_name = "relief rule"

    def __str__(self) -> str:
        return f"{self.label}: Rs {self.base_amount:,.0f}"


class ReliefAllowance(models.Model):
    """
    Flat additions that do not depend on a damage grade.

    Per-person displacement support, an emergency uplift for an uninhabitable
    property, and similar. Kept separate from `ReliefRule` because the arithmetic
    is different: these are fixed or per-head, not severity-scaled.
    """

    ALLOWANCE_CHOICES = [
        ("PER_PERSON", "Per affected person"),
        ("UNINHABITABLE", "Property uninhabitable"),
        ("IMMEDIATE_RELIEF", "Immediate relief required"),
    ]

    code = models.CharField(max_length=30, choices=ALLOWANCE_CHOICES, unique=True)
    label = models.CharField(max_length=120)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    is_per_unit = models.BooleanField(
        default=False, help_text="If true, multiply by the count (e.g. people affected)."
    )
    max_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Ceiling for per-unit allowances. 0 means no ceiling.",
    )
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["display_order", "code"]
        verbose_name = "relief allowance"

    def __str__(self) -> str:
        return f"{self.label}: Rs {self.amount:,.0f}"


class CompensationStatus(models.TextChoices):
    CALCULATED = "CALCULATED", "Calculated - awaiting review"
    UNDER_REVIEW = "UNDER_REVIEW", "Under administrator review"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    ADJUSTED = "ADJUSTED", "Adjusted by administrator"


class CompensationClaim(models.Model):
    """The calculated relief package for one inspected case."""

    report = models.OneToOneField(
        DisasterReport, on_delete=models.CASCADE, related_name="compensation_claim"
    )
    inspection = models.OneToOneField(
        Inspection, on_delete=models.PROTECT, related_name="compensation_claim"
    )

    calculated_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="What the engine computed. Never overwritten by an adjustment.",
    )
    approved_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="What the administrator authorised. May differ from the calculated amount.",
    )
    status = models.CharField(
        max_length=15, choices=CompensationStatus.choices,
        default=CompensationStatus.CALCULATED, db_index=True,
    )

    adjustment_reason = models.TextField(
        blank=True, help_text="Mandatory justification when the approved amount differs."
    )
    adjusted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="compensation_adjustments",
    )

    rules_snapshot = models.JSONField(
        default=dict, blank=True,
        help_text="The rule amounts in force at calculation time, so old claims stay auditable.",
    )
    calculated_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-calculated_at"]

    def __str__(self) -> str:
        return f"{self.report.reference}: Rs {self.calculated_amount:,.2f}"

    @property
    def payable_amount(self):
        """The figure that will actually be paid out."""
        return self.approved_amount if self.approved_amount is not None else self.calculated_amount

    @property
    def was_adjusted(self) -> bool:
        return self.approved_amount is not None and self.approved_amount != self.calculated_amount


class CompensationItem(models.Model):
    """
    One line of the itemised breakdown.

    Storing `base_amount`, `severity`, and `severity_factor` alongside the final
    `amount` means the arithmetic can be re-checked by hand from the stored row -
    which is what Section 17's "deterministic and explainable" requires.
    """

    claim = models.ForeignKey(CompensationClaim, on_delete=models.CASCADE, related_name="items")

    code = models.CharField(max_length=40)
    label = models.CharField(max_length=120)
    severity = models.CharField(max_length=15, blank=True)
    base_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    severity_factor = models.DecimalField(max_digits=4, decimal_places=2, default=1)
    quantity = models.PositiveSmallIntegerField(default=1)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    display_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["display_order", "id"]

    def __str__(self) -> str:
        return f"{self.label}: Rs {self.amount:,.2f}"

    @property
    def calculation_note(self) -> str:
        """Human-readable arithmetic, e.g. 'Rs 80,000 x 0.80 (Severe) = Rs 64,000'."""
        if self.quantity > 1:
            return f"Rs {self.base_amount:,.0f} x {self.quantity} = Rs {self.amount:,.0f}"
        if self.severity:
            return (
                f"Rs {self.base_amount:,.0f} x {self.severity_factor} "
                f"({self.severity.title()}) = Rs {self.amount:,.0f}"
            )
        return f"Rs {self.amount:,.0f}"
