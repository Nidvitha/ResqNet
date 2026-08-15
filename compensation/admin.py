"""Admin registration for compensation."""

from django.contrib import admin

from .models import CompensationClaim, CompensationItem, ReliefAllowance, ReliefRule


@admin.register(ReliefRule)
class ReliefRuleAdmin(admin.ModelAdmin):
    list_display = ["label", "damage_field", "base_amount", "is_active"]
    list_editable = ["base_amount", "is_active"]
    ordering = ["display_order"]


@admin.register(ReliefAllowance)
class ReliefAllowanceAdmin(admin.ModelAdmin):
    list_display = ["label", "code", "amount", "is_per_unit", "max_amount", "is_active"]
    list_editable = ["amount", "is_active"]
    ordering = ["display_order"]


class CompensationItemInline(admin.TabularInline):
    model = CompensationItem
    extra = 0
    readonly_fields = ["code", "label", "severity", "base_amount",
                       "severity_factor", "quantity", "amount"]
    can_delete = False


@admin.register(CompensationClaim)
class CompensationClaimAdmin(admin.ModelAdmin):
    list_display = ["report", "calculated_amount", "approved_amount", "status", "calculated_at"]
    list_filter = ["status"]
    search_fields = ["report__reference"]
    # Amounts are engine output; adjustments go through the API so the reason is
    # captured and audited.
    readonly_fields = ["report", "inspection", "calculated_amount",
                       "rules_snapshot", "calculated_at", "updated_at"]
    inlines = [CompensationItemInline]

    def has_add_permission(self, request):
        return False
