"""Admin registration for inspections."""

from django.contrib import admin

from .models import Inspection, InspectionPhoto


class InspectionPhotoInline(admin.TabularInline):
    model = InspectionPhoto
    extra = 0
    readonly_fields = ["uploaded_at", "file_size"]


@admin.register(Inspection)
class InspectionAdmin(admin.ModelAdmin):
    list_display = [
        "report", "officer", "status", "structural_damage",
        "roof_damage", "is_habitable", "completed_at", "signed_at",
    ]
    list_filter = ["status", "is_habitable", "requires_immediate_relief", "completed_offline"]
    search_fields = ["report__reference", "officer__username"]
    readonly_fields = ["started_at", "completed_at", "signed_at", "signature_name", "synced_at"]
    inlines = [InspectionPhotoInline]

    def get_readonly_fields(self, request, obj=None):
        """A signed inspection is a finished record - lock it in the admin too."""
        if obj and obj.status == "SIGNED":
            return [field.name for field in self.model._meta.fields]
        return super().get_readonly_fields(request, obj)


@admin.register(InspectionPhoto)
class InspectionPhotoAdmin(admin.ModelAdmin):
    list_display = ["inspection", "damage_area", "caption", "uploaded_at"]
    search_fields = ["inspection__report__reference"]
