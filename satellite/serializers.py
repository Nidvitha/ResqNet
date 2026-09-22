from rest_framework import serializers

from .models import DamageAssessment, SatelliteAnalysis, SatelliteDetection


class SatelliteDetectionSerializer(serializers.ModelSerializer):
    detection_type_display = serializers.CharField(source="get_detection_type_display", read_only=True)
    severity_display = serializers.CharField(source="get_severity_display", read_only=True)

    class Meta:
        model = SatelliteDetection
        fields = [
            "id",
            "detection_type",
            "detection_type_display",
            "severity",
            "severity_display",
            "confidence",
            "latitude",
            "longitude",
            "affected_area_hectares",
            "geometry_geojson",
            "evidence",
            "detected_at",
        ]


class SatelliteAnalysisCreateSerializer(serializers.ModelSerializer):
    run_now = serializers.BooleanField(default=True, required=False, write_only=True)

    class Meta:
        model = SatelliteAnalysis
        fields = [
            "report",
            "zone_label",
            "pre_disaster_image",
            "post_disaster_image",
            "latitude",
            "longitude",
            "affected_area_hectares",
            "region_geojson",
            "run_now",
        ]


class SatelliteAnalysisDetailSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    detections = SatelliteDetectionSerializer(many=True, read_only=True)
    report_reference = serializers.CharField(source="report.reference", read_only=True)

    class Meta:
        model = SatelliteAnalysis
        fields = [
            "id",
            "report",
            "report_reference",
            "zone_label",
            "pre_disaster_image",
            "post_disaster_image",
            "latitude",
            "longitude",
            "affected_area_hectares",
            "region_geojson",
            "status",
            "status_display",
            "model_name",
            "model_version",
            "summary",
            "error_message",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
            "detections",
        ]


class DamageAssessmentSerializer(serializers.ModelSerializer):
    report_reference = serializers.CharField(source="report.reference", read_only=True)

    class Meta:
        model = DamageAssessment
        fields = [
            "id",
            "report",
            "report_reference",
            "citizen_severity_score",
            "photo_ai_score",
            "satellite_score",
            "field_verification_score",
            "overall_confidence",
            "recommended_priority",
            "score_breakdown",
            "notes",
            "is_field_verified",
            "updated_at",
        ]
