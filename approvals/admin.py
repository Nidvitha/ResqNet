"""Admin registration for approvals and payouts."""

from django.contrib import admin

from .models import Approval, Payout


@admin.register(Approval)
class ApprovalAdmin(admin.ModelAdmin):
    list_display = ["report", "decision", "approved_amount", "decided_by", "decided_at"]
    list_filter = ["decision", "decided_at"]
    search_fields = ["report__reference", "decided_by__username"]
    # Decisions are historical facts - readable, never editable.
    readonly_fields = [field.name for field in Approval._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = [
        "reference_number", "report", "amount", "status",
        "payment_method", "is_simulated", "completed_at",
    ]
    list_filter = ["status", "payment_method", "is_simulated"]
    search_fields = ["reference_number", "report__reference", "beneficiary_name"]
    readonly_fields = ["reference_number", "initiated_at", "completed_at", "created_at"]
