"""Admin registration for triage configuration."""

from django.contrib import admin

from .models import CriticalInfrastructureSite, TriageResult, TriageRule, TriageThreshold


@admin.register(TriageRule)
class TriageRuleAdmin(admin.ModelAdmin):
    list_display = ["label", "indicator", "points", "is_per_unit", "max_points", "is_active"]
    list_editable = ["points", "is_active"]
    list_filter = ["is_active", "is_per_unit"]
    ordering = ["display_order"]


@admin.register(TriageThreshold)
class TriageThresholdAdmin(admin.ModelAdmin):
    list_display = ["level", "minimum_score", "maximum_score", "response_target_hours", "colour"]
    list_editable = ["minimum_score", "maximum_score", "response_target_hours"]


@admin.register(TriageResult)
class TriageResultAdmin(admin.ModelAdmin):
    list_display = ["report", "score", "level", "evaluated_at"]
    list_filter = ["level"]
    search_fields = ["report__reference"]
    # Results are engine output, so the admin site is read-only for them too.
    readonly_fields = [f.name for f in TriageResult._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(CriticalInfrastructureSite)
class CriticalInfrastructureSiteAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "infrastructure_type",
        "district",
        "impact_weight",
        "is_active",
    ]
    list_filter = ["infrastructure_type", "district", "is_active"]
    search_fields = ["name", "district", "notes"]
