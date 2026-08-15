"""
Compensation API views.

Claims are read-only over the API. There is no endpoint that accepts an amount
and stores it, because that would make the relief matrix decorative - anybody
with an administrator account could type any number. The only ways a figure
changes are: re-running the engine, or an explicit `adjust/` call that demands a
written reason and records both numbers.
"""

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsAdminRole
from reports.states import InvalidTransition

from .models import CompensationClaim, ReliefAllowance, ReliefRule
from .serializers import (
    AdjustClaimSerializer,
    CompensationClaimSerializer,
    ReliefAllowanceSerializer,
    ReliefRuleSerializer,
)
from .services import adjust_claim, ensure_default_matrix


class ReliefRuleViewSet(viewsets.ModelViewSet):
    """The configurable relief matrix (Section 17)."""

    serializer_class = ReliefRuleSerializer
    permission_classes = [IsAdminRole]
    pagination_class = None

    def get_queryset(self):
        ensure_default_matrix()
        return ReliefRule.objects.all()


class ReliefAllowanceViewSet(viewsets.ModelViewSet):
    serializer_class = ReliefAllowanceSerializer
    permission_classes = [IsAdminRole]
    pagination_class = None

    def get_queryset(self):
        ensure_default_matrix()
        return ReliefAllowance.objects.all()


class CompensationClaimViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = CompensationClaimSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = CompensationClaim.objects.select_related(
            "report", "report__citizen", "inspection"
        ).prefetch_related("items")

        if user.is_admin_role or user.is_superuser:
            if self.request.query_params.get("status"):
                queryset = queryset.filter(status=self.request.query_params["status"])
            return queryset
        if user.is_field_officer:
            return queryset.filter(inspection__officer=user)
        return queryset.filter(report__citizen=user)

    @action(detail=True, methods=["post"], permission_classes=[IsAdminRole])
    def adjust(self, request, pk=None):
        """Override the calculated amount, with a mandatory recorded reason."""
        claim = self.get_object()
        serializer = AdjustClaimSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            adjust_claim(
                claim=claim,
                amount=serializer.validated_data["amount"],
                reason=serializer.validated_data["reason"],
                user=request.user,
                request=request,
            )
        except InvalidTransition as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        claim.refresh_from_db()
        return Response(CompensationClaimSerializer(claim).data)

    @action(detail=True, methods=["get"])
    def breakdown(self, request, pk=None):
        """
        The plain-language itemisation, ready to render or print.

        This is what "explainable" looks like at the API boundary: not a total,
        but every line, its severity grade, and the arithmetic behind it.
        """
        claim = self.get_object()
        return Response(
            {
                "reference": claim.report.reference,
                "calculated_amount": str(claim.calculated_amount),
                "approved_amount": str(claim.approved_amount) if claim.approved_amount is not None else None,
                "payable_amount": str(claim.payable_amount),
                "was_adjusted": claim.was_adjusted,
                "adjustment_reason": claim.adjustment_reason,
                "lines": [
                    {
                        "label": item.label,
                        "severity": item.severity,
                        "amount": str(item.amount),
                        "explanation": item.calculation_note,
                    }
                    for item in claim.items.all()
                ],
            }
        )
