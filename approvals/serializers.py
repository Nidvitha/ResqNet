"""Approval and payout serializers."""

from rest_framework import serializers

from .models import Approval, Payout


class ApprovalSerializer(serializers.ModelSerializer):
    report_reference = serializers.CharField(source="report.reference", read_only=True)
    decided_by_name = serializers.CharField(source="decided_by.get_full_name", read_only=True)
    decision_display = serializers.CharField(source="get_decision_display", read_only=True)

    class Meta:
        model = Approval
        fields = [
            "id", "report", "report_reference", "claim",
            "decision", "decision_display", "approved_amount", "reason",
            "decided_by", "decided_by_name", "decided_at",
        ]
        read_only_fields = fields


class PayoutSerializer(serializers.ModelSerializer):
    report_reference = serializers.CharField(source="report.reference", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Payout
        fields = [
            "id", "report", "report_reference", "approval",
            "amount", "status", "status_display", "reference_number",
            "beneficiary_name", "payment_method", "is_simulated",
            "initiated_at", "completed_at", "failure_reason", "created_at",
        ]
        read_only_fields = fields


class DecisionSerializer(serializers.Serializer):
    """
    Body for approve / reject / request-review.

    `reason` is required on every path. A relief decision without a written
    justification is not auditable, and the citizen is entitled to know why.
    """

    reason = serializers.CharField(max_length=2000)
    adjusted_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True, min_value=0,
        help_text="Optional. Provide only when authorising a different amount from the calculated one.",
    )

    def validate_reason(self, value):
        if len(value.strip()) < 10:
            raise serializers.ValidationError(
                "Give a substantive reason (at least 10 characters). "
                "It is stored permanently and shown in the case history."
            )
        return value.strip()


class PayoutActionSerializer(serializers.Serializer):
    payment_method = serializers.ChoiceField(
        choices=["BANK_TRANSFER", "CHEQUE", "CASH"], default="BANK_TRANSFER"
    )
