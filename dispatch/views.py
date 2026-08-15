"""
Dispatch API views.

Includes the offline pre-cache endpoint from Section 13: an officer pulls their
assignments *before* driving into an area with no coverage, and the browser
stores the payload in IndexedDB. Everything the officer needs on site - address,
coordinates, severity, the citizen's phone number - is in that single response,
because there will be no second request.
"""

from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import User
from accounts.permissions import IsAdminRole, IsFieldOfficerOrAdmin
from reports.models import DisasterReport
from reports.states import CaseStatus, InvalidTransition

from .models import Assignment, AssignmentStatus
from .serializers import AssignmentSerializer, ManualAssignSerializer, OfficerCandidateSerializer
from .services import accept_assignment, auto_assign_report, manual_assign, rank_officers


class AssignmentViewSet(viewsets.ReadOnlyModelViewSet):
    """Assignments, scoped so an officer only ever sees their own."""

    serializer_class = AssignmentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = Assignment.objects.select_related(
            "report", "report__citizen", "officer"
        ).order_by("-report__priority_score", "due_by")

        if user.is_admin_role or user.is_superuser:
            officer_id = self.request.query_params.get("officer")
            if officer_id:
                queryset = queryset.filter(officer_id=officer_id)
        elif user.is_field_officer:
            queryset = queryset.filter(officer=user)
        else:
            queryset = queryset.filter(report__citizen=user)

        if self.request.query_params.get("status"):
            queryset = queryset.filter(status=self.request.query_params["status"])
        if self.request.query_params.get("active") == "true":
            queryset = queryset.filter(
                status__in=[
                    AssignmentStatus.ASSIGNED,
                    AssignmentStatus.ACCEPTED,
                    AssignmentStatus.IN_PROGRESS,
                ]
            )
        return queryset


@api_view(["GET"])
@permission_classes([IsFieldOfficerOrAdmin])
def my_assignments(request):
    """
    The officer's working queue, ordered the way triage says it should be worked.

    Critical cases first, then by deadline. This ordering is the whole point of
    the triage engine reaching the field.
    """
    assignments = (
        Assignment.objects.filter(
            officer=request.user,
            status__in=[
                AssignmentStatus.ASSIGNED,
                AssignmentStatus.ACCEPTED,
                AssignmentStatus.IN_PROGRESS,
            ],
        )
        .select_related("report", "report__citizen")
        .order_by("-report__priority_score", "due_by")
    )
    return Response(AssignmentSerializer(assignments, many=True, context={"request": request}).data)


@api_view(["GET"])
@permission_classes([IsFieldOfficerOrAdmin])
def offline_package(request):
    """
    Everything the officer needs to work a full day with no connectivity.

    Deliberately one flat response rather than a set of links: a client cannot
    follow links once it is offline.
    """
    assignments = (
        Assignment.objects.filter(
            officer=request.user,
            status__in=[
                AssignmentStatus.ASSIGNED,
                AssignmentStatus.ACCEPTED,
                AssignmentStatus.IN_PROGRESS,
            ],
        )
        .select_related("report", "report__citizen", "report__triage_result")
        .order_by("-report__priority_score")
    )

    cases = []
    for assignment in assignments:
        report = assignment.report
        cases.append(
            {
                "assignment_id": assignment.id,
                "report_id": report.id,
                "reference": report.reference,
                "disaster_type": report.disaster_type,
                "damage_category": report.damage_category,
                "description": report.description,
                "latitude": str(report.latitude),
                "longitude": str(report.longitude),
                "address": report.address,
                "district": report.district,
                "priority_score": report.priority_score,
                "triage_level": report.triage_level,
                "status": report.status,
                "distance_km": str(assignment.distance_km) if assignment.distance_km else None,
                "due_by": assignment.due_by,
                "citizen_name": report.citizen.get_full_name() or report.citizen.username,
                "citizen_phone": report.citizen.phone_number,
                "reported_indicators": report.damage_indicators(),
            }
        )

    profile = getattr(request.user, "officer_profile", None)
    if profile:
        profile.last_synced_at = timezone.now()
        profile.save(update_fields=["last_synced_at"])

    return Response(
        {
            "generated_at": timezone.now(),
            "officer": request.user.get_full_name() or request.user.username,
            "case_count": len(cases),
            "cases": cases,
            "severity_options": [
                {"value": "NONE", "label": "No damage"},
                {"value": "MINOR", "label": "Minor"},
                {"value": "MODERATE", "label": "Moderate"},
                {"value": "SEVERE", "label": "Severe"},
                {"value": "DESTROYED", "label": "Completely destroyed"},
            ],
        }
    )


@api_view(["POST"])
@permission_classes([IsFieldOfficerOrAdmin])
def accept(request, assignment_id):
    """Officer acknowledges a case before heading out."""
    try:
        assignment = Assignment.objects.get(pk=assignment_id)
    except Assignment.DoesNotExist:
        return Response({"detail": "Assignment not found."}, status=status.HTTP_404_NOT_FOUND)

    if assignment.officer_id != request.user.id and not (
        request.user.is_admin_role or request.user.is_superuser
    ):
        return Response(
            {"detail": "This case is assigned to a different officer."},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        accept_assignment(assignment, user=request.user, request=request)
    except InvalidTransition as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(AssignmentSerializer(assignment, context={"request": request}).data)


@api_view(["GET"])
@permission_classes([IsAdminRole])
def candidates(request, report_id):
    """Show the administrator the ranked officer list, with the engine's reasoning."""
    try:
        report = DisasterReport.objects.get(pk=report_id)
    except DisasterReport.DoesNotExist:
        return Response({"detail": "Report not found."}, status=status.HTTP_404_NOT_FOUND)

    ranked = rank_officers(report)
    payload = [
        {
            "officer_id": item["officer"].id,
            "officer_name": item["officer"].get_full_name() or item["officer"].username,
            "employee_id": item["profile"].employee_id,
            "zone": item["profile"].zone,
            "distance_km": item["distance_km"],
            "score": item["score"],
            "active_cases": item["active_cases"],
            "max_active_cases": item["profile"].max_active_cases,
            "zone_match": item["zone_match"],
            "reason": item["reason"],
        }
        for item in ranked
    ]
    return Response(OfficerCandidateSerializer(payload, many=True).data)


@api_view(["POST"])
@permission_classes([IsAdminRole])
def assign_manually(request):
    """Administrator picks the officer themselves, overriding the engine."""
    serializer = ManualAssignSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        report = DisasterReport.objects.get(pk=serializer.validated_data["report_id"])
        officer = User.objects.get(pk=serializer.validated_data["officer_id"], role="FIELD_OFFICER")
    except DisasterReport.DoesNotExist:
        return Response({"detail": "Report not found."}, status=status.HTTP_404_NOT_FOUND)
    except User.DoesNotExist:
        return Response({"detail": "Field officer not found."}, status=status.HTTP_404_NOT_FOUND)

    try:
        assignment = manual_assign(
            report,
            officer=officer,
            admin_user=request.user,
            note=serializer.validated_data.get("note", ""),
            request=request,
        )
    except InvalidTransition as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(
        AssignmentSerializer(assignment, context={"request": request}).data,
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([IsAdminRole])
def retry_auto_assign(request, report_id):
    """Re-run automatic dispatch for a case that found nobody the first time."""
    try:
        report = DisasterReport.objects.get(pk=report_id)
    except DisasterReport.DoesNotExist:
        return Response({"detail": "Report not found."}, status=status.HTTP_404_NOT_FOUND)

    if report.status != CaseStatus.TRIAGED:
        return Response(
            {"detail": f"Only triaged cases can be auto-assigned (this one is {report.status})."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    assignment = auto_assign_report(report, request=request)
    if assignment is None:
        return Response(
            {"detail": "No eligible officer is currently available for this case."},
            status=status.HTTP_409_CONFLICT,
        )
    return Response(AssignmentSerializer(assignment, context={"request": request}).data)
