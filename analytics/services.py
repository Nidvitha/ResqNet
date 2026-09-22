"""
Analytics services - aggregation only, never mutation.

Section 21 lists eleven statistics the backend should provide. Every one of them
is computed here with database aggregation rather than by loading rows into
Python and counting them. On a dataset of ten reports the difference is
invisible; on fifty thousand it is the difference between a dashboard that loads
and one that times out.

Nothing in this module writes. Analytics is a read-only view of the truth stored
by the other apps.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db.models import Avg, Count, F, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from accounts.models import OfficerProfile, Role, User
from approvals.models import Payout, PayoutStatus
from compensation.models import CompensationClaim
from dispatch.models import Assignment, AssignmentStatus
from inspections.models import Inspection, InspectionStatus
from reports.models import DisasterReport
from reports.states import CaseStatus, TERMINAL_STATES
from satellite.models import DetectionType, SatelliteAnalysis, SatelliteDetection


def overview() -> dict:
    """The headline numbers for the administrator dashboard and public home page."""
    reports = DisasterReport.objects.exclude(status=CaseStatus.DRAFT)
    total = reports.count()
    resolved = reports.filter(status=CaseStatus.PAYOUT_COMPLETED).count()

    approved_total = CompensationClaim.objects.filter(
        report__status__in=[
            CaseStatus.APPROVED, CaseStatus.PAYOUT_PENDING,
            CaseStatus.PAYOUT_INITIATED, CaseStatus.PAYOUT_COMPLETED,
        ]
    ).aggregate(total=Sum("approved_amount"))["total"] or Decimal("0")

    paid_total = Payout.objects.filter(status=PayoutStatus.COMPLETED).aggregate(
        total=Sum("amount")
    )["total"] or Decimal("0")

    return {
        "total_reports": total,
        "critical_cases": reports.filter(triage_level="CRITICAL").count(),
        "high_priority_cases": reports.filter(triage_level__in=["HIGH", "CRITICAL"]).count(),
        "cases_resolved": resolved,
        "pending_approvals": reports.filter(
            status__in=[CaseStatus.PENDING_APPROVAL, CaseStatus.NEEDS_REVIEW]
        ).count(),
        "active_cases": reports.exclude(status__in=TERMINAL_STATES).count(),
        "officers_total": User.objects.filter(role=Role.FIELD_OFFICER, is_active=True).count(),
        "officers_deployed": Assignment.objects.filter(
            status__in=[
                AssignmentStatus.ASSIGNED,
                AssignmentStatus.ACCEPTED,
                AssignmentStatus.IN_PROGRESS,
            ]
        ).values("officer").distinct().count(),
        "officers_available": OfficerProfile.objects.filter(
            is_available=True, officer__is_active=True
        ).count(),
        "relief_approved_total": str(approved_total),
        "relief_paid_total": str(paid_total),
        "resolution_rate": round((resolved / total) * 100, 1) if total else 0.0,
        "generated_at": timezone.now(),
    }


def reports_by_status() -> list[dict]:
    rows = (
        DisasterReport.objects.exclude(status=CaseStatus.DRAFT)
        .values("status")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    labels = dict(CaseStatus.choices)
    return [
        {"status": row["status"], "label": labels.get(row["status"], row["status"]), "count": row["count"]}
        for row in rows
    ]


def reports_by_disaster_type() -> list[dict]:
    from reports.models import DisasterType

    labels = dict(DisasterType.choices)
    rows = (
        DisasterReport.objects.exclude(status=CaseStatus.DRAFT)
        .values("disaster_type")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    return [
        {
            "disaster_type": row["disaster_type"],
            "label": labels.get(row["disaster_type"], row["disaster_type"]),
            "count": row["count"],
        }
        for row in rows
    ]


def reports_by_severity() -> list[dict]:
    rows = (
        DisasterReport.objects.exclude(status=CaseStatus.DRAFT)
        .exclude(triage_level="")
        .values("triage_level")
        .annotate(count=Count("id"))
    )
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    result = [{"level": row["triage_level"], "count": row["count"]} for row in rows]
    result.sort(key=lambda item: order.get(item["level"], 99))
    return result


def geographic_distribution() -> list[dict]:
    """District-level counts, for the choropleth and the district filter."""
    rows = (
        DisasterReport.objects.exclude(status=CaseStatus.DRAFT)
        .exclude(district="")
        .values("district")
        .annotate(
            total=Count("id"),
            critical=Count("id", filter=Q(triage_level="CRITICAL")),
            resolved=Count("id", filter=Q(status=CaseStatus.PAYOUT_COMPLETED)),
        )
        .order_by("-total")
    )
    return list(rows)


def heatmap_points(limit: int = 1000) -> list[dict]:
    """
    Raw coordinates weighted by severity, for the Leaflet heat layer.

    Capped deliberately: a browser cannot usefully render more than a few
    thousand points, and the response size matters on a low-bandwidth connection.
    """
    reports = (
        DisasterReport.objects.exclude(status=CaseStatus.DRAFT)
        .values("latitude", "longitude", "priority_score", "triage_level", "reference", "status")
        .order_by("-priority_score")[:limit]
    )
    return [
        {
            "lat": float(row["latitude"]),
            "lng": float(row["longitude"]),
            "weight": min(row["priority_score"] / 100, 1.0) if row["priority_score"] else 0.1,
            "level": row["triage_level"],
            "reference": row["reference"],
            "status": row["status"],
        }
        for row in reports
    ]


def officer_workload() -> list[dict]:
    """Per-officer caseload and throughput, for the officer monitoring screen."""
    officers = (
        User.objects.filter(role=Role.FIELD_OFFICER, is_active=True)
        .select_related("officer_profile")
        .annotate(
            active_cases=Count(
                "assignments",
                filter=Q(assignments__status__in=[
                    AssignmentStatus.ASSIGNED,
                    AssignmentStatus.ACCEPTED,
                    AssignmentStatus.IN_PROGRESS,
                ]),
                distinct=True,
            ),
            completed_cases=Count(
                "assignments",
                filter=Q(assignments__status=AssignmentStatus.COMPLETED),
                distinct=True,
            ),
            overdue_cases=Count(
                "assignments",
                filter=Q(
                    assignments__status__in=[
                        AssignmentStatus.ASSIGNED,
                        AssignmentStatus.ACCEPTED,
                        AssignmentStatus.IN_PROGRESS,
                    ],
                    assignments__due_by__lt=timezone.now(),
                ),
                distinct=True,
            ),
        )
        .order_by("-active_cases")
    )

    result = []
    for officer in officers:
        profile = getattr(officer, "officer_profile", None)
        result.append(
            {
                "officer_id": officer.id,
                "name": officer.get_full_name() or officer.username,
                "employee_id": profile.employee_id if profile else "",
                "zone": profile.zone if profile else "",
                "is_available": profile.is_available if profile else False,
                "active_cases": officer.active_cases,
                "completed_cases": officer.completed_cases,
                "overdue_cases": officer.overdue_cases,
                "capacity": profile.max_active_cases if profile else 0,
                "last_synced_at": profile.last_synced_at if profile else None,
            }
        )
    return result


def inspection_metrics() -> dict:
    """Average time from starting an inspection to completing it."""
    completed = Inspection.objects.filter(completed_at__isnull=False)
    average = completed.annotate(
        duration=F("completed_at") - F("started_at")
    ).aggregate(avg=Avg("duration"))["avg"]

    average_minutes = None
    if average is not None:
        average_minutes = round(average.total_seconds() / 60, 1)

    return {
        "total_inspections": Inspection.objects.count(),
        "completed_inspections": completed.count(),
        "signed_inspections": Inspection.objects.filter(status=InspectionStatus.SIGNED).count(),
        "in_progress": Inspection.objects.filter(status=InspectionStatus.STARTED).count(),
        "average_inspection_minutes": average_minutes,
        "offline_completed": Inspection.objects.filter(completed_offline=True).count(),
    }


def compensation_metrics() -> dict:
    claims = CompensationClaim.objects.all()
    return {
        "total_claims": claims.count(),
        "total_calculated": str(claims.aggregate(t=Sum("calculated_amount"))["t"] or Decimal("0")),
        "total_approved": str(claims.aggregate(t=Sum("approved_amount"))["t"] or Decimal("0")),
        "average_claim": str(
            (claims.aggregate(a=Avg("calculated_amount"))["a"] or Decimal("0")).quantize(Decimal("0.01"))
        ),
        "adjusted_claims": claims.exclude(approved_amount=F("calculated_amount"))
        .exclude(approved_amount__isnull=True)
        .count(),
    }


def payout_metrics() -> dict:
    payouts = Payout.objects.all()
    by_status = {
        row["status"]: {"count": row["count"], "amount": str(row["amount"] or Decimal("0"))}
        for row in payouts.values("status").annotate(count=Count("id"), amount=Sum("amount"))
    }
    return {
        "total_payouts": payouts.count(),
        "completed_amount": str(
            payouts.filter(status=PayoutStatus.COMPLETED).aggregate(t=Sum("amount"))["t"] or Decimal("0")
        ),
        "pending_amount": str(
            payouts.filter(status=PayoutStatus.PENDING).aggregate(t=Sum("amount"))["t"] or Decimal("0")
        ),
        "by_status": by_status,
        "is_simulated": True,   # stated plainly - Section 19
    }


def daily_trend(days: int = 30) -> list[dict]:
    """Report volume per day, for the trend chart."""
    since = timezone.now() - timedelta(days=days)
    rows = (
        DisasterReport.objects.filter(created_at__gte=since)
        .exclude(status=CaseStatus.DRAFT)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(
            total=Count("id"),
            critical=Count("id", filter=Q(triage_level="CRITICAL")),
        )
        .order_by("day")
    )
    return [
        {"date": row["day"].isoformat(), "total": row["total"], "critical": row["critical"]}
        for row in rows
    ]


def full_dashboard() -> dict:
    """Everything the admin command centre needs, in one round trip."""
    return {
        "overview": overview(),
        "by_status": reports_by_status(),
        "by_disaster_type": reports_by_disaster_type(),
        "by_severity": reports_by_severity(),
        "geographic": geographic_distribution(),
        "officer_workload": officer_workload(),
        "inspections": inspection_metrics(),
        "compensation": compensation_metrics(),
        "payouts": payout_metrics(),
        "trend": daily_trend(),
        "satellite": satellite_overview(),
        "zones": zone_intelligence(),
    }


def satellite_overview() -> dict:
    analyses = SatelliteAnalysis.objects.all()
    detections = SatelliteDetection.objects.all()
    return {
        "total_analyses": analyses.count(),
        "completed_analyses": analyses.filter(status="COMPLETED").count(),
        "failed_analyses": analyses.filter(status="FAILED").count(),
        "flood_detections": detections.filter(detection_type=DetectionType.FLOOD).count(),
        "building_detections": detections.filter(detection_type=DetectionType.BUILDING_DAMAGE).count(),
        "road_detections": detections.filter(detection_type=DetectionType.ROAD_DISRUPTION).count(),
        "average_detection_confidence": str(
            (detections.aggregate(a=Avg("confidence"))["a"] or Decimal("0")).quantize(Decimal("0.01"))
        ),
    }


def zone_intelligence() -> list[dict]:
    districts = (
        DisasterReport.objects.exclude(status=CaseStatus.DRAFT)
        .exclude(district="")
        .values_list("district", flat=True)
        .distinct()
    )

    rows = []
    for district in districts:
        reports_qs = DisasterReport.objects.filter(district=district).exclude(status=CaseStatus.DRAFT)
        report_ids = list(reports_qs.values_list("id", flat=True))

        analyses_qs = SatelliteAnalysis.objects.filter(report_id__in=report_ids, status="COMPLETED")
        detections_qs = SatelliteDetection.objects.filter(analysis__in=analyses_qs)

        rows.append(
            {
                "zone": district,
                "citizen_reports": len(report_ids),
                "critical_cases": reports_qs.filter(triage_level="CRITICAL").count(),
                "field_inspections": Inspection.objects.filter(report_id__in=report_ids).count(),
                "verified_damage": Inspection.objects.filter(
                    report_id__in=report_ids,
                    status=InspectionStatus.SIGNED,
                ).count(),
                "satellite_analyses": analyses_qs.count(),
                "satellite_damaged_structures": detections_qs.filter(
                    detection_type=DetectionType.BUILDING_DAMAGE
                ).count(),
                "satellite_flood_detections": detections_qs.filter(
                    detection_type=DetectionType.FLOOD
                ).count(),
                "satellite_road_detections": detections_qs.filter(
                    detection_type=DetectionType.ROAD_DISRUPTION
                ).count(),
                "estimated_compensation": str(
                    CompensationClaim.objects.filter(report_id__in=report_ids).aggregate(
                        total=Sum("approved_amount")
                    )["total"]
                    or Decimal("0")
                ),
            }
        )

    rows.sort(key=lambda item: item["critical_cases"], reverse=True)
    return rows


def public_statistics() -> dict:
    """
    The subset that is safe to show on the unauthenticated home page.

    Counts and totals only - no names, no coordinates, no case references.
    Aggregate figures cannot identify an individual household.
    """
    data = overview()
    return {
        "total_reports": data["total_reports"],
        "critical_cases": data["critical_cases"],
        "cases_resolved": data["cases_resolved"],
        "officers_deployed": data["officers_deployed"],
        "relief_approved_total": data["relief_approved_total"],
        "resolution_rate": data["resolution_rate"],
        "is_demo_data": not DisasterReport.objects.exclude(status=CaseStatus.DRAFT).exists(),
    }
