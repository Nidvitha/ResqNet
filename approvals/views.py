"""
Approval and payout API views.

Every write here is administrator-only and goes through the services layer, so
the ordering guarantees of Section 6 hold no matter which endpoint is called
first. Citizens get read access to their own decisions and payout status, because
Section 4 lists tracking compensation and payout status as citizen capabilities.
"""

from rest_framework import status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsAdminRole
from reports.models import DisasterReport
from reports.serializers import ReportDetailSerializer
from reports.states import CaseStatus, InvalidTransition

from .models import Approval, Payout
from .serializers import (
    ApprovalSerializer,
    DecisionSerializer,
    PayoutActionSerializer,
    PayoutSerializer,
)
from .services import (
    approve_case,
    complete_payout,
    initiate_payout,
    reject_case,
    request_review,
)


class ApprovalViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ApprovalSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = Approval.objects.select_related("report", "decided_by", "claim")
        if user.is_admin_role or user.is_superuser:
            if self.request.query_params.get("decision"):
                queryset = queryset.filter(decision=self.request.query_params["decision"])
            return queryset
        if user.is_field_officer:
            return queryset.filter(report__assignments__officer=user).distinct()
        return queryset.filter(report__citizen=user)


class PayoutViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PayoutSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = Payout.objects.select_related("report", "approval")
        if user.is_admin_role or user.is_superuser:
            if self.request.query_params.get("status"):
                queryset = queryset.filter(status=self.request.query_params["status"])
            return queryset
        return queryset.filter(report__citizen=user)


def _get_report_or_404(report_id):
    try:
        return DisasterReport.objects.select_related("citizen").get(pk=report_id), None
    except DisasterReport.DoesNotExist:
        return None, Response({"detail": "Report not found."}, status=status.HTTP_404_NOT_FOUND)


@api_view(["GET"])
@permission_classes([IsAdminRole])
def review_queue(request):
    """
    Cases waiting for a decision, most urgent first.

    This is the administrator's actual working list, so the ordering follows the
    triage score rather than arrival time.
    """
    reports = (
        DisasterReport.objects.filter(
            status__in=[CaseStatus.PENDING_APPROVAL, CaseStatus.NEEDS_REVIEW]
        )
        .select_related("citizen", "inspection", "compensation_claim")
        .order_by("-priority_score", "created_at")
    )
    return Response(
        ReportDetailSerializer(reports, many=True, context={"request": request}).data
    )


@api_view(["POST"])
@permission_classes([IsAdminRole])
def approve(request, report_id):
    """Approve relief and open the payout."""
    report, error = _get_report_or_404(report_id)
    if error:
        return error

    serializer = DecisionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        approval = approve_case(
            report=report,
            user=request.user,
            reason=serializer.validated_data["reason"],
            adjusted_amount=serializer.validated_data.get("adjusted_amount"),
            request=request,
        )
    except InvalidTransition as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(ApprovalSerializer(approval).data, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAdminRole])
def reject(request, report_id):
    report, error = _get_report_or_404(report_id)
    if error:
        return error

    serializer = DecisionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        approval = reject_case(
            report=report,
            user=request.user,
            reason=serializer.validated_data["reason"],
            request=request,
        )
    except InvalidTransition as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(ApprovalSerializer(approval).data, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAdminRole])
def review(request, report_id):
    """Send the case back for re-inspection."""
    report, error = _get_report_or_404(report_id)
    if error:
        return error

    serializer = DecisionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        approval = request_review(
            report=report,
            user=request.user,
            reason=serializer.validated_data["reason"],
            request=request,
        )
    except InvalidTransition as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(ApprovalSerializer(approval).data, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAdminRole])
def payout_initiate(request, report_id):
    """Begin disbursement. Simulated - no banking system is contacted."""
    report, error = _get_report_or_404(report_id)
    if error:
        return error

    serializer = PayoutActionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        payout = initiate_payout(
            report=report,
            user=request.user,
            method=serializer.validated_data["payment_method"],
            request=request,
        )
    except InvalidTransition as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(PayoutSerializer(payout).data)


@api_view(["POST"])
@permission_classes([IsAdminRole])
def payout_complete(request, report_id):
    """Close the case. Only reachable once the payout has been initiated."""
    report, error = _get_report_or_404(report_id)
    if error:
        return error

    try:
        payout = complete_payout(report=report, user=request.user, request=request)
    except InvalidTransition as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(PayoutSerializer(payout).data)
