"""
The triage engine.

Section 14 lists exactly what this must do:

    1. Receive validated report data.   4. Determine severity.      7. Trigger assignment.
    2. Evaluate rules.                  5. Store the result.
    3. Calculate score.                 6. Record an audit event.

It is deliberately *rule-based*, not machine-learned. Section 15 says not to
introduce AI/ML without a genuine need, and here there is a strong reason to
avoid it: when a citizen asks why their case was ranked below their neighbour's,
"the model said so" is not an acceptable answer from a public relief programme.
A sum of published rules is.

The default weights below match Section 14 and are used only to seed the
database on first run. After that, the rows in `TriageRule` are the truth.
"""

from __future__ import annotations

import logging

from django.db import transaction

from audit.models import AuditAction
from audit.services import record_event
from reports.models import DisasterReport, TriageLevel
from reports.states import CaseStatus

from .models import TriageResult, TriageRule, TriageThreshold

logger = logging.getLogger("resqnet.triage")

#: Seed values, straight from Section 14 of the specification.
DEFAULT_RULES = [
    ("people_trapped", "People trapped", 50, False, 0, 1),
    ("roof_collapsed", "Roof completely collapsed", 40, False, 0, 2),
    ("exposed_live_wires", "Exposed live wires", 30, False, 0, 3),
    ("major_structural_damage", "Major structural damage", 30, False, 0, 4),
    ("medical_assistance_needed", "Medical assistance needed", 25, False, 0, 5),
    ("standing_water", "Standing water", 20, False, 0, 6),
    ("road_blocked", "Road blocked", 15, False, 0, 7),
    ("wall_damage", "Wall damage", 10, False, 0, 8),
    ("people_affected", "People affected", 2, True, 30, 9),
]

#: Seed bands, straight from Section 14.
DEFAULT_THRESHOLDS = [
    (TriageLevel.LOW, 0, 39, 72, "#0891b2"),
    (TriageLevel.MEDIUM, 40, 69, 48, "#ca8a04"),
    (TriageLevel.HIGH, 70, 99, 12, "#ea580c"),
    (TriageLevel.CRITICAL, 100, 9999, 4, "#dc2626"),
]


def ensure_default_configuration() -> None:
    """
    Create the seed rules and thresholds if the tables are empty.

    Idempotent: safe to call on every triage run. It only fills gaps, and never
    overwrites a weight an administrator has tuned.
    """
    for indicator, label, points, per_unit, max_points, order in DEFAULT_RULES:
        TriageRule.objects.get_or_create(
            indicator=indicator,
            defaults={
                "label": label,
                "points": points,
                "is_per_unit": per_unit,
                "max_points": max_points,
                "display_order": order,
            },
        )
    for level, minimum, maximum, hours, colour in DEFAULT_THRESHOLDS:
        TriageThreshold.objects.get_or_create(
            level=level,
            defaults={
                "minimum_score": minimum,
                "maximum_score": maximum,
                "response_target_hours": hours,
                "colour": colour,
            },
        )


def calculate_score(indicators: dict) -> tuple[int, list[dict]]:
    """
    Pure scoring function: indicators in, `(score, breakdown)` out.

    "Pure" means it touches no request, no user, and no case state - so it can be
    unit-tested with a plain dictionary, which is exactly what the Section 27
    triage tests do.
    """
    ensure_default_configuration()

    total = 0
    breakdown: list[dict] = []

    for rule in TriageRule.objects.filter(is_active=True).order_by("display_order"):
        value = indicators.get(rule.indicator)
        if not value:
            continue

        if rule.is_per_unit:
            points = rule.points * int(value)
            if rule.max_points:
                points = min(points, rule.max_points)
            detail = f"{rule.label} ({int(value)})"
        else:
            points = rule.points
            detail = rule.label

        total += points
        breakdown.append(
            {
                "indicator": rule.indicator,
                "label": detail,
                "points": points,
            }
        )

    return max(total, 0), breakdown


def classify(score: int) -> TriageThreshold:
    """Map a score to its severity band."""
    ensure_default_configuration()
    threshold = TriageThreshold.objects.filter(
        minimum_score__lte=score, maximum_score__gte=score
    ).first()
    if threshold is None:
        # A score above every configured band still has to land somewhere.
        threshold = TriageThreshold.objects.order_by("-minimum_score").first()
    return threshold


@transaction.atomic
def run_triage(report: DisasterReport, *, user=None, request=None) -> TriageResult:
    """
    Score one report, store the result, move it to TRIAGED, and audit it.

    Re-running triage on an already-scored report is allowed - a citizen may add
    photos, or an administrator may retune the weights - so the result row is
    updated rather than duplicated, and `recalculated_at` records that it happened.
    """
    score, breakdown = calculate_score(report.damage_indicators())
    threshold = classify(score)
    level = threshold.level if threshold else TriageLevel.LOW
    target_hours = threshold.response_target_hours if threshold else 72

    newest_rule = TriageRule.objects.order_by("-updated_at").values_list("updated_at", flat=True).first()

    result, created = TriageResult.objects.update_or_create(
        report=report,
        defaults={
            "score": score,
            "level": level,
            "breakdown": breakdown,
            "response_target_hours": target_hours,
            "rules_version": newest_rule.isoformat() if newest_rule else "",
        },
    )
    if not created:
        from django.utils import timezone

        result.recalculated_at = timezone.now()
        result.save(update_fields=["recalculated_at"])

    # Denormalise onto the report so queue sorting never needs a join.
    report.priority_score = score
    report.triage_level = level
    report.save(update_fields=["priority_score", "triage_level", "updated_at"])

    previous_state = report.status
    if report.status == CaseStatus.SUBMITTED:
        previous_state = report.apply_transition(CaseStatus.TRIAGED, role="SYSTEM")

    record_event(
        entity=report,
        action=AuditAction.TRIAGE_COMPLETED,
        user=user,
        previous_state=previous_state,
        new_state=report.status,
        metadata={
            "score": score,
            "level": level,
            "breakdown": breakdown,
            "recalculated": not created,
        },
        request=request,
    )

    logger.info("Triage %s -> score=%s level=%s", report.reference, score, level)
    return result


def preview_score(indicators: dict) -> dict:
    """
    Score a hypothetical set of indicators without saving anything.

    Used by the citizen reporting form to show, live, how urgent the situation
    they are describing will be judged to be.
    """
    score, breakdown = calculate_score(indicators)
    threshold = classify(score)
    return {
        "score": score,
        "level": threshold.level if threshold else TriageLevel.LOW,
        "colour": threshold.colour if threshold else "#64748b",
        "response_target_hours": threshold.response_target_hours if threshold else 72,
        "breakdown": breakdown,
    }
