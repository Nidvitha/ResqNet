"""Compensation serializers."""

from rest_framework import serializers

from .models import (
    CompensationClaim,
    CompensationItem,
    ReliefAllowance,
    ReliefRule,
)


class ReliefRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReliefRule
        fields = [
            "id", "damage_field", "label", "base_amount",
            "is_active", "display_order", "notes", "updated_at",
        ]
        read_only_fields = ["id", "updated_at"]

    def validate_base_amount(self, value):
        if value < 0:
            raise serializers.ValidationError("Relief amounts cannot be negative.")
        return value


class ReliefAllowanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReliefAllowance
        fields = [
            "id", "code", "label", "amount", "is_per_unit",
            "max_amount", "is_active", "display_order",
        ]
        read_only_fields = ["id"]


class CompensationItemSerializer(serializers.ModelSerializer):
    calculation_note = serializers.CharField(read_only=True)

    class Meta:
        model = CompensationItem
        fields = [
            "id", "code", "label", "severity", "base_amount",
            "severity_factor", "quantity", "amount", "calculation_note",
        ]
        read_only_fields = fields


class CompensationClaimSerializer(serializers.ModelSerializer):
    """The itemised breakdown a citizen or administrator reads."""

    items = CompensationItemSerializer(many=True, read_only=True)
    report_reference = serializers.CharField(source="report.reference", read_only=True)
    citizen_name = serializers.CharField(source="report.citizen.get_full_name", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    payable_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    was_adjusted = serializers.BooleanField(read_only=True)

    class Meta:
        model = CompensationClaim
        fields = [
            "id", "report", "report_reference", "citizen_name", "inspection",
            "calculated_amount", "approved_amount", "payable_amount",
            "status", "status_display", "was_adjusted",
            "adjustment_reason", "adjusted_by",
            "items", "rules_snapshot", "calculated_at", "updated_at",
        ]
        read_only_fields = fields


class AdjustClaimSerializer(serializers.Serializer):
    """An administrator overriding the calculated amount must justify it."""

    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    reason = serializers.CharField(max_length=1000)

    def validate_reason(self, value):
        if len(value.strip()) < 10:
            raise serializers.ValidationError(
                "Give a clear reason for the adjustment (at least 10 characters). "
                "This is recorded permanently in the audit trail."
            )
        return value.strip()
