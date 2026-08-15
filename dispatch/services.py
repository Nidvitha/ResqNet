"""
The dispatch engine - picking the right field officer for a case.

Section 15 gives the pipeline:

    New Report -> Eligible Officers -> Available Officers -> Zone Filtering
               -> Distance Calculation -> Workload Consideration -> Best Officer

Two rules shape the implementation:

* **Explainable distance.** The Haversine formula computes great-circle distance
  between two latitude/longitude points using nothing but trigonometry. It is
  short enough to read, exact enough for dispatch, and needs no PostGIS. Section
  15 asks for exactly this.
* **No ML.** The "best" officer is a weighted sum of three plain factors -
  proximity, spare capacity, and zone match - and the weights are constants you
  can see. Every choice can be justified to a citizen in one sentence.
"""

from __future__ import annotations

import logging
import math
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from accounts.models import OfficerProfile
from audit.models import AuditAction
from audit.services import record_event
from reports.models import DisasterReport
from reports.states import CaseStatus, InvalidTransition

from .models import Assignment, AssignmentStatus

logger = logging.getLogger("resqnet.dispatch")

EARTH_RADIUS_KM = 6371.0

# Weights for the ranking score. They sum to 100 so a score reads as a percentage.
WEIGHT_DISTANCE = 50   # closer is better
WEIGHT_WORKLOAD = 30   # more spare capacity is better
WEIGHT_ZONE = 20       # officer's own zone is better

#: Beyond this, an officer is treated as too far to be a sensible first choice.
MAX_SENSIBLE_DISTANCE_KM = 100.0


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """
    Great-circle distance between two points, in kilometres.

    Straight-line, not road distance - a deliberate simplification. Road routing
    needs an external service, and for ranking a handful of officers in one
    district the ordering is almost always the same.
    """
    lat1, lon1, lat2, lon2 = (float(v) for v in (lat1, lon1, lat2, lon2))

    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(EARTH_RADIUS_KM * c, 2)


def eligible_officers(report: DisasterReport):
    """
    Officers who *could* take this case.

    Filtering happens in the database, not in Python, so the query stays fast as
    the roster grows. Unavailable officers are excluded here rather than being
    ranked and then discarded - Section 27 tests this specifically.
    """
    return (
        OfficerProfile.objects.select_related("officer")
        .filter(is_available=True, officer__is_active=True)
        .exclude(officer=report.citizen)  # nobody inspects their own claim
    )


def score_officer(profile: OfficerProfile, report: DisasterReport) -> dict | None:
    """
    Rank one officer against one report.

    Returns None when the officer is disqualified (at capacity, or implausibly
    far away). Otherwise returns the score plus the human-readable reasoning that
    gets stored on the assignment.
    """
    active_cases = profile.active_case_count
    if active_cases >= profile.max_active_cases:
        return None

    distance = haversine_km(
        profile.base_latitude, profile.base_longitude, report.latitude, report.longitude
    )
    zone_match = bool(
        report.district and profile.zone.strip().lower() == report.district.strip().lower()
    )

    # An officer far outside the area is only acceptable if nobody local is free.
    if distance > MAX_SENSIBLE_DISTANCE_KM and not zone_match:
        return None

    # Normalise each factor to 0..1, then apply the weights.
    distance_factor = max(0.0, 1 - min(distance, MAX_SENSIBLE_DISTANCE_KM) / MAX_SENSIBLE_DISTANCE_KM)
    capacity_factor = (profile.max_active_cases - active_cases) / profile.max_active_cases
    zone_factor = 1.0 if zone_match else 0.0

    score = (
        distance_factor * WEIGHT_DISTANCE
        + capacity_factor * WEIGHT_WORKLOAD
        + zone_factor * WEIGHT_ZONE
    )

    reason = (
        f"{distance} km from duty station"
        f"{' - same zone (' + profile.zone + ')' if zone_match else ' - outside assigned zone'}"
        f"; workload {active_cases}/{profile.max_active_cases}."
    )

    return {
        "profile": profile,
        "officer": profile.officer,
        "distance_km": distance,
        "score": round(score, 2),
        "reason": reason,
        "zone_match": zone_match,
        "active_cases": active_cases,
    }


def rank_officers(report: DisasterReport) -> list[dict]:
    """Every qualifying officer, best match first."""
    candidates = []
    for profile in eligible_officers(report):
        scored = score_officer(profile, report)
        if scored is not None:
            candidates.append(scored)
    candidates.sort(key=lambda item: (-item["score"], item["distance_km"]))
    return candidates


def find_best_officer(report: DisasterReport) -> dict | None:
    ranked = rank_officers(report)
    return ranked[0] if ranked else None


@transaction.atomic
def auto_assign_report(report: DisasterReport, *, request=None) -> Assignment | None:
    """
    Assign the best available officer to a triaged report.

    Returns None when nobody qualifies - a normal outcome during a large event,
    not an error. The case simply waits at TRIAGED in the administrator's manual
    assignment queue.
    """
    if report.status != CaseStatus.TRIAGED:
        logger.info("Skipping dispatch for %s: status is %s", report.reference, report.status)
        return None

    if report.assignments.filter(
        status__in=[AssignmentStatus.ASSIGNED, AssignmentStatus.ACCEPTED, AssignmentStatus.IN_PROGRESS]
    ).exists():
        logger.info("Skipping dispatch for %s: already assigned", report.reference)
        return None

    best = find_best_officer(report)
    if best is None:
        logger.warning("No eligible officer available for %s", report.reference)
        return None

    return _create_assignment(
        report=report,
        officer=best["officer"],
        distance_km=best["distance_km"],
        score=best["score"],
        reason=best["reason"],
        is_automatic=True,
        assigned_by=None,
        request=request,
    )


@transaction.atomic
def manual_assign(report: DisasterReport, *, officer, admin_user, note: str = "", request=None):
    """
    An administrator overrides the engine and picks an officer themselves.

    Any existing live assignment is marked REASSIGNED rather than deleted, so the
    history of who held the case remains readable.
    """
    existing = report.assignments.filter(
        status__in=[AssignmentStatus.ASSIGNED, AssignmentStatus.ACCEPTED, AssignmentStatus.IN_PROGRESS]
    ).first()
    if existing:
        existing.status = AssignmentStatus.REASSIGNED
        existing.save(update_fields=["status"])
        record_event(
            entity=report,
            action=AuditAction.ASSIGNMENT_REASSIGNED,
            user=admin_user,
            metadata={
                "from_officer": existing.officer.username,
                "to_officer": officer.username,
                "note": note,
            },
            request=request,
        )

    profile = getattr(officer, "officer_profile", None)
    distance = None
    if profile:
        distance = haversine_km(
            profile.base_latitude, profile.base_longitude, report.latitude, report.longitude
        )

    # A manual assignment may legally start from TRIAGED or from a case being
    # sent back after review, so bring it to TRIAGED first if needed.
    if report.status == CaseStatus.NEEDS_REVIEW:
        report.apply_transition(CaseStatus.ASSIGNED, role="ADMIN")
        report.refresh_from_db()

    return _create_assignment(
        report=report,
        officer=officer,
        distance_km=distance,
        score=0,
        reason=note or f"Manually assigned by {admin_user.get_full_name() or admin_user.username}.",
        is_automatic=False,
        assigned_by=admin_user,
        request=request,
        allow_from_assigned=True,
    )


def _create_assignment(
    *, report, officer, distance_km, score, reason, is_automatic,
    assigned_by, request=None, allow_from_assigned=False,
) -> Assignment:
    """Shared tail of both assignment paths: create the row, move state, audit."""
    target_hours = getattr(getattr(report, "triage_result", None), "response_target_hours", 72)

    assignment = Assignment.objects.create(
        report=report,
        officer=officer,
        distance_km=distance_km,
        selection_score=score,
        selection_reason=reason,
        is_automatic=is_automatic,
        assigned_by=assigned_by,
        due_by=timezone.now() + timedelta(hours=target_hours),
    )

    previous = report.status
    if report.status == CaseStatus.TRIAGED:
        previous = report.apply_transition(CaseStatus.ASSIGNED, role="SYSTEM" if is_automatic else "ADMIN")
    elif not allow_from_assigned:
        raise InvalidTransition(f"Cannot assign a case in state {report.status}.")

    record_event(
        entity=report,
        action=AuditAction.OFFICER_ASSIGNED,
        user=assigned_by,
        previous_state=previous,
        new_state=report.status,
        metadata={
            "officer": officer.username,
            "officer_name": officer.get_full_name(),
            "distance_km": float(distance_km) if distance_km is not None else None,
            "automatic": is_automatic,
            "reason": reason,
            "due_by": assignment.due_by.isoformat(),
        },
        request=request,
    )

    logger.info("Assigned %s to %s (%s)", report.reference, officer.username, reason)
    return assignment


def accept_assignment(assignment: Assignment, *, user, request=None) -> Assignment:
    """The officer acknowledges the case, typically before heading into the field."""
    if assignment.status != AssignmentStatus.ASSIGNED:
        raise InvalidTransition(f"Assignment is already {assignment.get_status_display().lower()}.")

    assignment.status = AssignmentStatus.ACCEPTED
    assignment.accepted_at = timezone.now()
    assignment.save(update_fields=["status", "accepted_at"])

    record_event(
        entity=assignment.report,
        action=AuditAction.ASSIGNMENT_ACCEPTED,
        user=user,
        metadata={"assignment_id": assignment.pk},
        request=request,
    )
    return assignment
