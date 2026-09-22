"""Triage serializers."""

from rest_framework import serializers

from .models import CriticalInfrastructureSite, TriageResult, TriageRule, TriageThreshold


class CriticalInfrastructureSiteSerializer(serializers.ModelSerializer):
    infrastructure_type_display = serializers.CharField(
        source="get_infrastructure_type_display", read_only=True
    )

    class Meta:
        model = CriticalInfrastructureSite
        fields = [
            "id",
            "name",
            "infrastructure_type",
            "infrastructure_type_display",
            "district",
            "latitude",
            "longitude",
            "impact_weight",
            "is_active",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class TriageRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = TriageRule
        fields = [
            "id", "indicator", "label", "points", "is_per_unit",
            "max_points", "is_active", "display_order", "updated_at",
        ]
        read_only_fields = ["id", "updated_at"]


class TriageThresholdSerializer(serializers.ModelSerializer):
    class Meta:
        model = TriageThreshold
        fields = [
            "id", "level", "minimum_score", "maximum_score",
            "response_target_hours", "colour",
        ]
        read_only_fields = ["id"]

    def validate(self, attrs):
        minimum = attrs.get("minimum_score", getattr(self.instance, "minimum_score", 0))
        maximum = attrs.get("maximum_score", getattr(self.instance, "maximum_score", 0))
        if minimum > maximum:
            raise serializers.ValidationError(
                {"maximum_score": "Maximum score must be greater than or equal to the minimum."}
            )
        return attrs


class TriageResultSerializer(serializers.ModelSerializer):
    report_reference = serializers.CharField(source="report.reference", read_only=True)
    explanation = serializers.CharField(read_only=True)

    class Meta:
        model = TriageResult
        fields = [
            "id", "report", "report_reference", "score", "level", "breakdown",
            "explanation", "response_target_hours", "rules_version",
            "evaluated_at", "recalculated_at",
        ]
        read_only_fields = fields
