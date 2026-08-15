"""
Triage models - configurable scoring rules and their stored results.

Why store the rules in the database instead of hardcoding them?
--------------------------------------------------------------
Section 14 says the example weights "can be adjusted". If they live in Python,
adjusting them means a code change, a deploy, and a developer. If they live in a
table, a district administrator can retune them from the admin site during an
event - and, crucially, ResQNet can still explain *why* a case scored 90 six
months ago, because `TriageResult` stores the breakdown that was actually used.

That last point is what "explainable" means in Section 2. The score is not a
number that appeared from nowhere; it is an itemised sum you can read back.
"""

from django.db import models

from reports.models import DisasterReport, TriageLevel


class TriageRule(models.Model):
    """
    One scoring rule: "if this damage indicator is true, add these points".

    `indicator` names a key returned by `DisasterReport.damage_indicators()`.
    Keeping the two in step is what lets the engine stay a simple loop.
    """

    INDICATOR_CHOICES = [
        ("roof_collapsed", "Roof completely collapsed"),
        ("people_trapped", "People trapped"),
        ("exposed_live_wires", "Exposed live wires"),
        ("standing_water", "Standing water"),
        ("road_blocked", "Road blocked"),
        ("major_structural_damage", "Major structural damage"),
        ("wall_damage", "Wall damage"),
        ("medical_assistance_needed", "Medical assistance needed"),
        ("people_affected", "People affected (per person)"),
    ]

    indicator = models.CharField(max_length=40, choices=INDICATOR_CHOICES, unique=True)
    label = models.CharField(max_length=120, help_text="Wording shown in the score breakdown.")
    points = models.SmallIntegerField(help_text="Points added when this indicator applies.")
    is_per_unit = models.BooleanField(
        default=False,
        help_text="If true, points are multiplied by the numeric value (e.g. people affected).",
    )
    max_points = models.PositiveSmallIntegerField(
        default=0,
        help_text="Ceiling for per-unit rules. 0 means no ceiling.",
    )
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveSmallIntegerField(default=0)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "indicator"]
        verbose_name = "triage rule"

    def __str__(self) -> str:
        return f"{self.label} (+{self.points})"


class TriageThreshold(models.Model):
    """
    Score band to severity mapping: 0-39 LOW, 40-69 MEDIUM, and so on.

    Also configurable, for the same reason as the rules: a flood event and an
    earthquake event may warrant different cut-offs.
    """

    level = models.CharField(max_length=10, choices=TriageLevel.choices, unique=True)
    minimum_score = models.PositiveSmallIntegerField()
    maximum_score = models.PositiveSmallIntegerField(
        default=9999, help_text="Inclusive upper bound. Use a large number for the top band."
    )
    response_target_hours = models.PositiveSmallIntegerField(
        default=72, help_text="Target time to first inspection, shown to officers and citizens."
    )
    colour = models.CharField(max_length=7, default="#64748b", help_text="Hex colour for the UI badge.")

    class Meta:
        ordering = ["minimum_score"]
        verbose_name = "triage threshold"

    def __str__(self) -> str:
        return f"{self.level}: {self.minimum_score}-{self.maximum_score}"


class TriageResult(models.Model):
    """
    The stored outcome of scoring one report.

    `breakdown` is a JSON list of the rules that fired, with the points each
    contributed. That is the audit-friendly explanation: nobody has to re-run the
    engine or guess which rule version applied.
    """

    report = models.OneToOneField(
        DisasterReport, on_delete=models.CASCADE, related_name="triage_result"
    )
    score = models.PositiveSmallIntegerField()
    level = models.CharField(max_length=10, choices=TriageLevel.choices, db_index=True)
    breakdown = models.JSONField(
        default=list,
        help_text="Itemised list of rules that fired: [{indicator, label, points}, ...].",
    )
    response_target_hours = models.PositiveSmallIntegerField(default=72)
    rules_version = models.CharField(
        max_length=40,
        blank=True,
        help_text="Timestamp of the newest rule used, so old results stay interpretable.",
    )
    evaluated_at = models.DateTimeField(auto_now_add=True)
    recalculated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-score"]

    def __str__(self) -> str:
        return f"{self.report.reference}: {self.score} ({self.level})"

    @property
    def explanation(self) -> str:
        """One-line plain-English summary, e.g. for a tooltip."""
        if not self.breakdown:
            return "No severity indicators were reported."
        parts = [f"{item['label']} +{item['points']}" for item in self.breakdown]
        return " | ".join(parts) + f" = {self.score} ({self.level})"
