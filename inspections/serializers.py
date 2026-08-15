"""Inspection serializers."""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from reports.validators import validate_image_upload

from .models import Inspection, InspectionPhoto


class InspectionPhotoSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = InspectionPhoto
        fields = [
            "id", "image", "image_url", "caption", "damage_area",
            "captured_latitude", "captured_longitude", "uploaded_at", "file_size",
        ]
        read_only_fields = ["uploaded_at", "file_size"]
        extra_kwargs = {"image": {"write_only": True}}

    def get_image_url(self, obj):
        if not obj.image:
            return None
        request = self.context.get("request")
        return request.build_absolute_uri(obj.image.url) if request else obj.image.url

    def validate_image(self, value):
        try:
            validate_image_upload(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return value


class InspectionSerializer(serializers.ModelSerializer):
    """Read shape, including the derived fields the officer's form displays."""

    report_reference = serializers.CharField(source="report.reference", read_only=True)
    officer_name = serializers.CharField(source="officer.get_full_name", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    photos = InspectionPhotoSerializer(many=True, read_only=True)
    is_editable = serializers.BooleanField(read_only=True)
    duration_minutes = serializers.IntegerField(read_only=True)

    class Meta:
        model = Inspection
        fields = [
            "id", "report", "report_reference", "assignment",
            "officer", "officer_name", "status", "status_display",
            "structural_damage", "roof_damage", "wall_damage", "foundation_damage",
            "electrical_damage", "water_damage", "household_damage",
            "people_affected", "is_habitable", "requires_immediate_relief",
            "estimated_damage_value", "remarks",
            "inspection_latitude", "inspection_longitude",
            "started_at", "completed_at", "signed_at", "signature_name",
            "completed_offline", "client_completed_at", "synced_at",
            "photos", "is_editable", "duration_minutes",
        ]
        read_only_fields = [
            "id", "report", "assignment", "officer", "status",
            "started_at", "completed_at", "signed_at", "signature_name", "synced_at",
        ]


class InspectionUpdateSerializer(serializers.ModelSerializer):
    """
    Write shape - only the checklist fields.

    `status`, `officer`, and every timestamp are absent, so no client can sign
    off an inspection by posting `{"status": "SIGNED"}`. Sign-off is a separate,
    guarded action.
    """

    class Meta:
        model = Inspection
        fields = [
            "structural_damage", "roof_damage", "wall_damage", "foundation_damage",
            "electrical_damage", "water_damage", "household_damage",
            "people_affected", "is_habitable", "requires_immediate_relief",
            "estimated_damage_value", "remarks",
            "inspection_latitude", "inspection_longitude",
            "completed_offline", "client_completed_at",
        ]

    def validate_estimated_damage_value(self, value):
        if value < 0:
            raise serializers.ValidationError("Estimated damage cannot be negative.")
        if value > 100_000_000:
            raise serializers.ValidationError(
                "That estimate exceeds the single-property ceiling. Please escalate this case manually."
            )
        return value

    def validate_remarks(self, value):
        if len(value) > 5000:
            raise serializers.ValidationError("Remarks are limited to 5000 characters.")
        return value


class SignOffSerializer(serializers.Serializer):
    signature_name = serializers.CharField(max_length=150)
    inspection_latitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, required=False, allow_null=True
    )
    inspection_longitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, required=False, allow_null=True
    )

    def validate_signature_name(self, value):
        if len(value.strip()) < 3:
            raise serializers.ValidationError("Enter your full name to sign off.")
        return value.strip()


class StartInspectionSerializer(serializers.Serializer):
    assignment_id = serializers.IntegerField()
