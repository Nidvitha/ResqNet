"""Django admin registration for accounts."""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import OfficerProfile, User


class OfficerProfileInline(admin.StackedInline):
    model = OfficerProfile
    can_delete = False
    extra = 0


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = [OfficerProfileInline]
    list_display = ["username", "email", "full_name", "role", "district", "is_active"]
    list_filter = ["role", "is_active", "is_staff", "district"]
    search_fields = ["username", "email", "first_name", "last_name", "phone_number"]

    # Extend Django's default fieldsets with the ResQNet-specific columns.
    fieldsets = BaseUserAdmin.fieldsets + (
        ("ResQNet profile", {"fields": ("role", "phone_number", "address", "district")}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ("ResQNet profile", {"fields": ("role", "email", "phone_number", "district")}),
    )

    @admin.display(description="Name")
    def full_name(self, obj):
        return obj.get_full_name() or "-"


@admin.register(OfficerProfile)
class OfficerProfileAdmin(admin.ModelAdmin):
    list_display = ["employee_id", "officer", "zone", "is_available", "active_case_count"]
    list_filter = ["zone", "is_available"]
    search_fields = ["employee_id", "officer__username", "officer__first_name"]
