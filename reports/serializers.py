"""Report serializers - validation and JSON shaping for citizen damage reports."""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import DisasterReport, ReportPhoto
from .states import CaseStatus
from .validators import validate_coordinate_pair, validate_image_upload


class ReportPhotoSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = ReportPhoto
        fields = [
            "id",
            "image",
            "image_url",
            "caption",
            "uploaded_at",
            "exif_latitude",
            "exif_longitude",
            "captured_at",
            "width",
            "height",
            "file_size",
        ]
        read_only_fields = [
            "uploaded_at", "exif_latitude", "exif_longitude",
            "captured_at", "width", "height", "file_size",
        ]
        extra_kwargs = {"image": {"write_only": True}}

    def get_image_url(self, obj):
        if not obj.image:
            return None
        request = self.context.get("request")
        url = obj.image.url
        return request.build_absolute_uri(url) if request else url

    def validate_image(self, value):
        # The model validator runs on full_clean(); DRF does not call that, so we
        # invoke it explicitly here. Belt and braces, deliberately.
        try:
            validate_image_upload(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return value


class ReportListSerializer(serializers.ModelSerializer):
    """Compact shape for list views - deliberately omits the full description."""

    disaster_type_display = serializers.CharField(source="get_disaster_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    photo_count = serializers.IntegerField(source="photos.count", read_only=True)
    progress_percent = serializers.IntegerField(read_only=True)

    class Meta:
        model = DisasterReport
        fields = [
            "id", "reference", "disaster_type", "disaster_type_display",
            "damage_category", "status", "status_display", "priority_score",
            "triage_level", "latitude", "longitude", "district", "address",
            "people_affected", "photo_count", "progress_percent",
            "created_at", "submitted_at",
        ]


class ReportDetailSerializer(serializers.ModelSerializer):
    """Full read shape, including nested evidence and the citizen's contact details."""

    disaster_type_display = serializers.CharField(source="get_disaster_type_display", read_only=True)
    damage_category_display = serializers.CharField(source="get_damage_category_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    photos = ReportPhotoSerializer(many=True, read_only=True)
    citizen_name = serializers.CharField(source="citizen.get_full_name", read_only=True)
    citizen_phone = serializers.CharField(source="citizen.phone_number", read_only=True)
    progress_percent = serializers.IntegerField(read_only=True)
    is_editable_by_citizen = serializers.BooleanField(read_only=True)

    class Meta:
        model = DisasterReport
        fields = [
            "id", "reference", "citizen", "citizen_name", "citizen_phone",
            "disaster_type", "disaster_type_display", "damage_category",
            "damage_category_display", "description",
            "latitude", "longitude", "address", "district", "location_source",
            "roof_collapsed", "people_trapped", "exposed_live_wires",
            "standing_water", "road_blocked", "major_structural_damage",
            "wall_damage", "people_affected", "medical_assistance_needed",
            "status", "status_display", "priority_score", "triage_level",
            "progress_percent", "is_editable_by_citizen",
            "photos", "created_offline", "client_created_at",
            "created_at", "updated_at", "submitted_at", "closed_at",
        ]
        read_only_fields = [
            "id", "reference", "citizen", "status", "priority_score",
            "triage_level", "created_at", "updated_at", "submitted_at", "closed_at",
        ]


class ReportCreateSerializer(serializers.ModelSerializer):
    """
    Creation and update shape.

    `citizen` and `status` are absent by design. The owner comes from the
    authenticated request, and the status comes from the state machine - a client
    that posts `{"status": "APPROVED"}` is simply ignored (Section 26).
    """

    submit_now = serializers.BooleanField(
        write_only=True,
        required=False,
        default=True,
        help_text="False saves as DRAFT; True submits immediately and starts triage.",
    )

    class Meta:
        model = DisasterReport
        fields = [
            "disaster_type", "damage_category", "description",
            "latitude", "longitude", "address", "district", "location_source",
            "roof_collapsed", "people_trapped", "exposed_live_wires",
            "standing_water", "road_blocked", "major_structural_damage",
            "wall_damage", "people_affected", "medical_assistance_needed",
            "idempotency_key", "created_offline", "client_created_at",
            "submit_now",
        ]

    def validate_description(self, value):
        cleaned = value.strip()
        if len(cleaned) < 20:
            raise serializers.ValidationError(
                "Please describe the damage in at least 20 characters so an officer "
                "knows what to expect on site."
            )
        if len(cleaned) > 5000:
            raise serializers.ValidationError("Description is limited to 5000 characters.")
        return cleaned

    def validate_people_affected(self, value):
        if value > 10000:
            raise serializers.ValidationError(
                "That figure looks implausible for a single property. "
                "Please contact the district control room directly."
            )
        return value

    def validate(self, attrs):
        latitude = attrs.get("latitude", getattr(self.instance, "latitude", None))
        longitude = attrs.get("longitude", getattr(self.instance, "longitude", None))
        if latitude is not None and longitude is not None:
            try:
                attrs["latitude"], attrs["longitude"] = validate_coordinate_pair(latitude, longitude)
            except DjangoValidationError as exc:
                raise serializers.ValidationError({"location": exc.messages}) from exc

        # A citizen may only edit a report that is still a draft.
        if self.instance is not None and self.instance.status != CaseStatus.DRAFT:
            raise serializers.ValidationError(
                f"This report is already {self.instance.get_status_display().lower()} "
                "and can no longer be edited."
            )
        return attrs


class StatusChangeSerializer(serializers.Serializer):
    """Body for explicit administrator-driven status changes."""

    target_status = serializers.ChoiceField(choices=CaseStatus.choices)
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)
