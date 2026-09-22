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
import math

from django.db import transaction
from django.db.models import Avg

from audit.models import AuditAction
from audit.services import record_event
from inspections.models import DamageSeverity
from reports.models import DisasterReport, TriageLevel
from reports.states import CaseStatus
from satellite.models import SatelliteDetection

from .models import CriticalInfrastructureSite, TriageResult, TriageRule, TriageThreshold

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

EARTH_RADIUS_KM = 6371.0


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


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    lat1, lon1, lat2, lon2 = (float(v) for v in (lat1, lon1, lat2, lon2))
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_KM * c


def _geo_severity_points(report: DisasterReport) -> tuple[int, dict] | None:
    if not report.district:
        return None

    district_reports = DisasterReport.objects.filter(district__iexact=report.district).exclude(
        status=CaseStatus.DRAFT
    )
    total = district_reports.count()
    if total == 0:
        return None

    critical = district_reports.filter(triage_level=TriageLevel.CRITICAL).count()
    high = district_reports.filter(triage_level=TriageLevel.HIGH).count()
    pressure = (critical + high) / total

    points = min(15, int(round(pressure * 15)))
    if points <= 0:
        return None

    return points, {
        "indicator": "district_pressure",
        "label": f"District severity pressure ({critical} critical, {high} high in {report.district})",
        "points": points,
        "source": "GEOSPATIAL",
    }


def _infrastructure_points(report: DisasterReport) -> tuple[int, dict] | None:
    sites = CriticalInfrastructureSite.objects.filter(is_active=True)
    if report.district:
        sites = sites.filter(district__iexact=report.district)
    sites = list(sites[:200])
    if not sites:
        return None

    nearest = None
    nearest_distance = None
    for site in sites:
        distance = _haversine_km(report.latitude, report.longitude, site.latitude, site.longitude)
        if nearest_distance is None or distance < nearest_distance:
            nearest = site
            nearest_distance = distance

    if nearest is None or nearest_distance is None or nearest_distance > 5:
        return None

    distance_factor = max(0.2, 1 - (nearest_distance / 5))
    points = min(15, max(1, int(round(nearest.impact_weight * distance_factor))))
    return points, {
        "indicator": "critical_infrastructure",
        "label": f"Near critical infrastructure: {nearest.name} ({nearest_distance:.2f} km)",
        "points": points,
        "source": "INFRASTRUCTURE",
        "metadata": {
            "site": nearest.name,
            "type": nearest.infrastructure_type,
            "distance_km": round(nearest_distance, 2),
        },
    }


def _satellite_points(report: DisasterReport) -> tuple[int, dict] | None:
    if not report.satellite_analyses.exists():
        return None
    confidence = SatelliteDetection.objects.filter(analysis__report=report).aggregate(c=Avg("confidence"))["c"]
    if confidence is None:
        return None

    points = min(20, int(round(float(confidence) * 0.2)))
    if points <= 0:
        return None

    return points, {
        "indicator": "satellite_evidence",
        "label": f"Satellite evidence confidence {float(confidence):.1f}%",
        "points": points,
        "source": "SATELLITE",
    }


def _assessment_points(report: DisasterReport) -> tuple[int, list[dict]]:
    additions = []
    total = 0

    photo_ai = getattr(getattr(report, "damage_assessment", None), "photo_ai_score", None)
    if photo_ai is not None:
        points = min(25, int(round(float(photo_ai) * 0.25)))
        if points > 0:
            total += points
            additions.append(
                {
                    "indicator": "photo_ai",
                    "label": f"Photo AI confidence {float(photo_ai):.1f}%",
                    "points": points,
                    "source": "AI_PHOTO",
                }
            )

    satellite = _satellite_points(report)
    if satellite is not None:
        points, item = satellite
        total += points
        additions.append(item)

    geo = _geo_severity_points(report)
    if geo is not None:
        points, item = geo
        total += points
        additions.append(item)

    infra = _infrastructure_points(report)
    if infra is not None:
        points, item = infra
        total += points
        additions.append(item)

    inspection = getattr(report, "inspection", None)
    if inspection is not None and inspection.status == "SIGNED":
        severe_count = sum(
            1
            for value in inspection.severity_map().values()
            if value in {DamageSeverity.SEVERE, DamageSeverity.DESTROYED}
        )
        verification_points = min(25, 10 + severe_count * 3)
        if inspection.requires_immediate_relief:
            verification_points = min(25, verification_points + 3)
        total += verification_points
        additions.append(
            {
                "indicator": "field_verification",
                "label": "Field officer verification completed",
                "points": verification_points,
                "source": "FIELD_VERIFIED",
                "authoritative": True,
            }
        )

    return total, additions


def calculate_score(indicators: dict, *, report: DisasterReport | None = None) -> tuple[int, list[dict]]:
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
                "source": "CITIZEN_RULE",
            }
        )

    if report is not None:
        additional_points, additional_breakdown = _assessment_points(report)
        total += additional_points
        breakdown.extend(additional_breakdown)

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
    score, breakdown = calculate_score(report.damage_indicators(), report=report)
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

    # Keep the multi-source assessment in sync with the latest citizen triage.
    try:
        from satellite.services import refresh_damage_assessment

        refresh_damage_assessment(report, user=user, request=request)
    except Exception:  # noqa: BLE001
        logger.exception("Damage assessment refresh failed for %s", report.reference)

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
