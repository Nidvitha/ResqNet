"""
Audit API views.

`ReadOnlyModelViewSet` gives list and retrieve and *nothing else*. There is no
create, no update, no destroy - Section 31 says plainly: "Do not allow DELETE
operations on immutable audit records." The safest way to honour that is to never
route the verb in the first place.
"""

from rest_framework import viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsAdminRole

from .models import AuditAction, AuditEvent
from .serializers import AuditEventSerializer


class AuditEventViewSet(viewsets.ReadOnlyModelViewSet):
    """The full audit trail. Administrators only."""

    serializer_class = AuditEventSerializer
    permission_classes = [IsAdminRole]

    def get_queryset(self):
        queryset = AuditEvent.objects.select_related("performed_by")
        params = self.request.query_params

        if params.get("action"):
            queryset = queryset.filter(action=params["action"])
        if params.get("entity_type"):
            queryset = queryset.filter(entity_type=params["entity_type"])
        if params.get("entity_id"):
            queryset = queryset.filter(entity_id=params["entity_id"])
        if params.get("user"):
            queryset = queryset.filter(performed_by_username__icontains=params["user"])
        if params.get("since"):
            queryset = queryset.filter(timestamp__gte=params["since"])
        if params.get("until"):
            queryset = queryset.filter(timestamp__lte=params["until"])
        return queryset


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def case_history(request, reference):
    """
    One case's history, by reference number.

    Open to the owning citizen as well as staff. Section 5 promises "audit
    transparency" as a feature, and a citizen being able to see who handled their
    claim and when is exactly what that means in practice.
    """
    from reports.models import DisasterReport

    report = DisasterReport.objects.filter(reference=reference).first()
    if report is None:
        return Response({"detail": "Case not found."}, status=404)

    user = request.user
    is_owner = report.citizen_id == user.id
    is_assigned = report.assignments.filter(officer=user).exists()
    if not (is_owner or is_assigned or user.is_admin_role or user.is_superuser):
        return Response({"detail": "You do not have access to this case."}, status=403)

    events = AuditEvent.objects.filter(
        entity_type="DisasterReport", entity_id=str(report.pk)
    ).order_by("timestamp")
    return Response(AuditEventSerializer(events, many=True).data)


@api_view(["GET"])
@permission_classes([IsAdminRole])
def action_types(request):
    """The catalogue of auditable actions, for building filter dropdowns."""
    return Response(
        [{"value": value, "label": label} for value, label in AuditAction.choices]
    )
