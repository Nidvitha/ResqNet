from django.contrib import admin

from .models import AIDamageAssessment


@admin.register(AIDamageAssessment)
class AIDamageAssessmentAdmin(admin.ModelAdmin):
    list_display = [
        "report", "photo", "predicted_severity", "confidence_percentage",
        "status", "model_name", "inferred_at",
    ]
    list_filter = ["status", "predicted_severity", "model_name"]
    search_fields = ["report__reference"]
    readonly_fields = [
        "report", "photo", "predicted_severity", "confidence_percentage",
        "model_name", "model_version", "status", "error_message", "inferred_at",
        "created_at", "updated_at",
    ]