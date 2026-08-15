"""
Admin registration for the audit trail.

The whole point of this class is what it *refuses*: no add, no change, no delete.
An audit trail an administrator can quietly edit provides no assurance at all,
so even the Django admin is locked to read-only here.
"""

from django.contrib import admin

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = [
        "timestamp", "action", "entity_type", "entity_label",
        "performed_by_username", "previous_state", "new_state",
    ]
    list_filter = ["action", "entity_type", "performed_by_role", "timestamp"]
    search_fields = ["entity_label", "entity_id", "performed_by_username"]
    date_hierarchy = "timestamp"
    readonly_fields = [field.name for field in AuditEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
