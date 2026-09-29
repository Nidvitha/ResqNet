"""
Report API views.

The single most important method in this file is `get_queryset()`. Section 32
demands backend-enforced ownership, and the reliable way to achieve that is to
never let a forbidden row into the queryset in the first place. A citizen
requesting `/api/reports/999/` does not get a 403 - they get a 404, because that
report is not in *their* queryset at all. Filtering beats checking: there is no
code path where forgetting a permission check leaks another citizen's data.
"""

from django.db.models import Prefetch
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsOwnerOrStaff
from audit.services import history_for
from damage_ai.models import AIDamageAssessment

from .models import DisasterReport, ReportPhoto
from .serializers import (
    ReportCreateSerializer,
    ReportDetailSerializer,
    ReportListSerializer,
    ReportPhotoSerializer,
    StatusChangeSerializer,
)
from .services import attach_photo, cancel_report, change_status, create_report, submit_report
from .states import CaseStatus, InvalidTransition


class ReportViewSet(viewsets.ModelViewSet):
    """
    Damage reports, scoped to whoever is asking.

    A *ViewSet* bundles the standard actions (list, create, retrieve, update,
    destroy) into one class, and the router in `urls.py` turns them into URLs.
    Extra actions are added with the `@action` decorator.
    """

    permission_classes = [IsAuthenticated, IsOwnerOrStaff]
    parser_classes = [MultiPartParser, FormParser, *viewsets.ModelViewSet.parser_classes]

    def get_queryset(self):
        user = self.request.user
        queryset = DisasterReport.objects.select_related("citizen", "triage_result").prefetch_related(
            Prefetch("photos", queryset=ReportPhoto.objects.order_by("uploaded_at")),
            Prefetch(
                "ai_assessments",
                queryset=AIDamageAssessment.objects.select_related("photo").order_by("photo_id"),
            ),
        )

        if user.is_admin_role or user.is_superuser:
            pass                                              # administrators see everything
        elif user.is_field_officer:
            queryset = queryset.filter(assignments__officer=user).distinct()
        else:
            queryset = queryset.filter(citizen=user)          # citizens see only their own

        # --- Optional filters, applied after the ownership scope -----------
        params = self.request.query_params
        if params.get("status"):
            queryset = queryset.filter(status=params["status"])
        if params.get("triage_level"):
            queryset = queryset.filter(triage_level=params["triage_level"])
        if params.get("disaster_type"):
            queryset = queryset.filter(disaster_type=params["disaster_type"])
        if params.get("district"):
            queryset = queryset.filter(district__iexact=params["district"])
        if params.get("search"):
            queryset = queryset.filter(reference__icontains=params["search"])
        return queryset

    def get_serializer_class(self):
        if self.action == "list":
            return ReportListSerializer
        if self.action in {"create", "update", "partial_update"}:
            return ReportCreateSerializer
        return ReportDetailSerializer

    def get_throttles(self):
        if self.action == "create":
            self.throttle_scope = "report_create"
        return super().get_throttles()

    def create(self, request, *args, **kwargs):
        # Only citizens file reports. An administrator filing on someone's behalf
        # is a separate workflow, not this endpoint.
        if not request.user.is_citizen:
            return Response(
                {"detail": "Only citizens can file damage reports."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        report, created = create_report(
            citizen=request.user, validated_data=serializer.validated_data, request=request
        )
        return Response(
            ReportDetailSerializer(report, context={"request": request}).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def perform_update(self, serializer):
        # The serializer already refuses to update anything past DRAFT.
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        """
        Reports are never deleted - they are cancelled.

        Deleting a case would erase the audit trail's subject and let someone
        make an inconvenient claim disappear. Cancellation is reversible history;
        deletion is not.
        """
        report = self.get_object()
        try:
            cancel_report(report, user=request.user, reason="Withdrawn by user", request=request)
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            ReportDetailSerializer(report, context={"request": request}).data
        )

    # --- Extra actions -----------------------------------------------------

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        """Submit a saved draft, triggering triage and dispatch."""
        report = self.get_object()
        if report.status != CaseStatus.DRAFT:
            return Response(
                {"detail": "Only a draft report can be submitted."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            submit_report(report, user=request.user, request=request)
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ReportDetailSerializer(report, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="photos",
            parser_classes=[MultiPartParser, FormParser])
    def upload_photo(self, request, pk=None):
        """Attach evidence. Validation runs server-side regardless of the client."""
        report = self.get_object()
        serializer = ReportPhotoSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        photo = attach_photo(
            report=report,
            image=serializer.validated_data["image"],
            caption=serializer.validated_data.get("caption", ""),
            user=request.user,
            request=request,
        )
        return Response(
            ReportPhotoSerializer(photo, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"])
    def timeline(self, request, pk=None):
        """
        The case's full audit history.

        Reachable by the owning citizen because transparency is the point -
        Section 5 lists "audit transparency" as a headline feature.
        """
        report = self.get_object()
        events = history_for(report)
        return Response(
            [
                {
                    "action": event.action,
                    "action_display": event.get_action_display(),
                    "previous_state": event.previous_state,
                    "new_state": event.new_state,
                    "performed_by": event.performed_by_username or "System",
                    "performed_by_role": event.performed_by_role,
                    "timestamp": event.timestamp,
                    "metadata": event.metadata,
                }
                for event in events
            ]
        )

    @action(detail=True, methods=["post"], url_path="status")
    def change_case_status(self, request, pk=None):
        """Administrator-only manual status move. Still bound by the state machine."""
        if not (request.user.is_admin_role or request.user.is_superuser):
            return Response(
                {"detail": "Only administrators can change case status directly."},
                status=status.HTTP_403_FORBIDDEN,
            )
        report = self.get_object()
        serializer = StatusChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            change_status(
                report,
                target=serializer.validated_data["target_status"],
                user=request.user,
                reason=serializer.validated_data.get("reason", ""),
                request=request,
            )
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ReportDetailSerializer(report, context={"request": request}).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def triage_preview(request):
    """
    Score a hypothetical report without saving it.

    Powers the live severity indicator on the citizen reporting form, so people
    can see that ticking "people trapped" genuinely escalates their case.
    """
    from triage.services import preview_score

    indicators = {
        key: request.data.get(key, False)
        for key in [
            "roof_collapsed", "people_trapped", "exposed_live_wires", "standing_water",
            "road_blocked", "major_structural_damage", "wall_damage",
            "medical_assistance_needed",
        ]
    }
    try:
        indicators["people_affected"] = int(request.data.get("people_affected", 0) or 0)
    except (TypeError, ValueError):
        indicators["people_affected"] = 0

    if request.data.get("disaster_type") and request.data.get("damage_category"):
        report = DisasterReport(
            disaster_type=request.data["disaster_type"],
            damage_category=request.data["damage_category"],
            damage_details=request.data.get("damage_details") or {},
            people_affected=indicators["people_affected"],
            **{key: value for key, value in indicators.items() if key != "people_affected"},
        )
        indicators = report.damage_indicators()
        return Response(preview_score(indicators, report=report))

    return Response(preview_score(indicators))
