"""
Triage API views.

Administrators tune the rules here; everyone else can only read the result that
applies to a case they are already allowed to see.
"""

from rest_framework import status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsAdminRole
from reports.models import DisasterReport

from .models import TriageResult, TriageRule, TriageThreshold
from .serializers import TriageResultSerializer, TriageRuleSerializer, TriageThresholdSerializer
from .services import preview_score, run_triage


class TriageRuleViewSet(viewsets.ModelViewSet):
    """Administrators retune scoring weights without a code change or a deploy."""

    queryset = TriageRule.objects.all()
    serializer_class = TriageRuleSerializer
    permission_classes = [IsAdminRole]
    pagination_class = None


class TriageThresholdViewSet(viewsets.ModelViewSet):
    queryset = TriageThreshold.objects.all()
    serializer_class = TriageThresholdSerializer
    permission_classes = [IsAdminRole]
    pagination_class = None


class TriageResultViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only by design.

    A triage result is an output of the engine, never something a client
    supplies. Allowing writes would let a caller set their own priority score.
    """

    serializer_class = TriageResultSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = TriageResult.objects.select_related("report")
        if user.is_admin_role or user.is_superuser:
            return queryset
        if user.is_field_officer:
            return queryset.filter(report__assignments__officer=user).distinct()
        return queryset.filter(report__citizen=user)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def preview(request):
    """Score a set of indicators without saving. Mirrors /api/reports/triage-preview/."""
    indicators = dict(request.data)
    try:
        indicators["people_affected"] = int(indicators.get("people_affected", 0) or 0)
    except (TypeError, ValueError):
        indicators["people_affected"] = 0
    return Response(preview_score(indicators))


@api_view(["POST"])
@permission_classes([IsAdminRole])
def recalculate(request, report_id):
    """
    Re-run triage for one case.

    Needed after an administrator retunes the weights mid-event, or when a
    citizen adds information that changes the picture.
    """
    try:
        report = DisasterReport.objects.get(pk=report_id)
    except DisasterReport.DoesNotExist:
        return Response({"detail": "Report not found."}, status=status.HTTP_404_NOT_FOUND)

    result = run_triage(report, user=request.user, request=request)
    return Response(TriageResultSerializer(result).data)
