"""Admin registration for dispatch."""

from django.contrib import admin

from .models import Assignment


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = [
        "report", "officer", "status", "distance_km",
        "selection_score", "is_automatic", "assigned_at", "due_by",
    ]
    list_filter = ["status", "is_automatic", "assigned_at"]
    search_fields = ["report__reference", "officer__username", "officer__first_name"]
    readonly_fields = ["assigned_at", "accepted_at", "completed_at",
                       "distance_km", "selection_score", "selection_reason"]
    date_hierarchy = "assigned_at"
