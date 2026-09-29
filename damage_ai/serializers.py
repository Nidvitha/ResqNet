from rest_framework import serializers

from .models import AIDamageAssessment


class AIDamageAssessmentSerializer(serializers.ModelSerializer):
    photo_id = serializers.IntegerField(read_only=True)
    predicted_severity_display = serializers.CharField(
        source="get_predicted_severity_display", read_only=True
    )

    class Meta:
        model = AIDamageAssessment
        fields = [
            "id", "photo_id", "predicted_severity", "predicted_severity_display",
            "confidence_percentage", "model_name", "model_version", "status",
            "error_message", "inferred_at",
        ]
        read_only_fields = fields