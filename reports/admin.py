"""Admin registration for reports."""

from django.contrib import admin

from .models import DisasterReport, ReportPhoto


class ReportPhotoInline(admin.TabularInline):
    model = ReportPhoto
    extra = 0
    readonly_fields = ["uploaded_at", "file_size", "width", "height"]


@admin.register(DisasterReport)
class DisasterReportAdmin(admin.ModelAdmin):
    list_display = [
        "reference", "citizen", "disaster_type", "district",
        "status", "triage_level", "priority_score", "created_at",
    ]
    list_filter = ["status", "triage_level", "disaster_type", "damage_category", "district"]
    search_fields = ["reference", "citizen__username", "citizen__first_name", "address"]
    readonly_fields = ["reference", "status", "priority_score", "triage_level",
                       "created_at", "updated_at", "submitted_at", "closed_at"]
    inlines = [ReportPhotoInline]
    date_hierarchy = "created_at"

    # `status` is read-only here on purpose: even a superuser should move a case
    # through the API so the state machine and audit trail stay authoritative.


@admin.register(ReportPhoto)
class ReportPhotoAdmin(admin.ModelAdmin):
    list_display = ["report", "caption", "uploaded_by", "uploaded_at"]
    search_fields = ["report__reference"]
    readonly_fields = ["uploaded_at", "file_size", "width", "height"]
