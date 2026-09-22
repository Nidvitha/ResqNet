from django.contrib import admin

from .models import DamageAssessment, SatelliteAnalysis, SatelliteDetection


@admin.register(SatelliteAnalysis)
class SatelliteAnalysisAdmin(admin.ModelAdmin):
    list_display = ["id", "report", "zone_label", "status", "model_name", "created_at", "completed_at"]
    list_filter = ["status", "model_name", "created_at"]
    search_fields = ["report__reference", "zone_label", "summary"]


@admin.register(SatelliteDetection)
class SatelliteDetectionAdmin(admin.ModelAdmin):
    list_display = ["id", "analysis", "detection_type", "severity", "confidence", "detected_at"]
    list_filter = ["detection_type", "severity", "detected_at"]
    search_fields = ["analysis__report__reference"]


@admin.register(DamageAssessment)
class DamageAssessmentAdmin(admin.ModelAdmin):
    list_display = ["report", "recommended_priority", "overall_confidence", "is_field_verified", "updated_at"]
    list_filter = ["recommended_priority", "is_field_verified", "updated_at"]
    search_fields = ["report__reference", "report__district"]
