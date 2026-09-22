from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models

from reports.models import DisasterReport, TriageLevel
from reports.validators import secure_upload_path, validate_image_upload


class AnalysisStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"


class DetectionType(models.TextChoices):
    FLOOD = "FLOOD", "Flooded area"
    BUILDING_DAMAGE = "BUILDING_DAMAGE", "Damaged building"
    ROAD_DISRUPTION = "ROAD_DISRUPTION", "Affected road"


class SatelliteSeverity(models.TextChoices):
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"
    CRITICAL = "CRITICAL", "Critical"


class SatelliteAnalysis(models.Model):
    report = models.ForeignKey(
        DisasterReport,
        on_delete=models.CASCADE,
        related_name="satellite_analyses",
        null=True,
        blank=True,
    )
    zone_label = models.CharField(max_length=120, blank=True, db_index=True)
    pre_disaster_image = models.ImageField(
        upload_to=secure_upload_path,
        validators=[validate_image_upload],
    )
    post_disaster_image = models.ImageField(
        upload_to=secure_upload_path,
        validators=[validate_image_upload],
    )

    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, db_index=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, db_index=True)
    affected_area_hectares = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    region_geojson = models.JSONField(
        default=dict,
        blank=True,
        help_text="Optional GeoJSON polygon/multipolygon for the analysed area.",
    )

    status = models.CharField(max_length=12, choices=AnalysisStatus.choices, default=AnalysisStatus.PENDING, db_index=True)
    model_name = models.CharField(max_length=120, blank=True)
    model_version = models.CharField(max_length=50, blank=True)
    summary = models.TextField(blank=True)
    error_message = models.TextField(blank=True)

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_satellite_analyses",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["zone_label", "status"]),
        ]

    def __str__(self) -> str:
        report = self.report.reference if self.report_id else "UNLINKED"
        return f"{report} satellite analysis {self.pk} ({self.status})"


class SatelliteDetection(models.Model):
    analysis = models.ForeignKey(SatelliteAnalysis, on_delete=models.CASCADE, related_name="detections")
    detection_type = models.CharField(max_length=30, choices=DetectionType.choices, db_index=True)
    severity = models.CharField(max_length=10, choices=SatelliteSeverity.choices, db_index=True)
    confidence = models.DecimalField(max_digits=5, decimal_places=2, help_text="0-100 confidence from the model.")

    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, db_index=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, db_index=True)
    affected_area_hectares = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    geometry_geojson = models.JSONField(
        default=dict,
        blank=True,
        help_text="Optional GeoJSON polygon/line for the detection geometry.",
    )

    evidence = models.JSONField(
        default=dict,
        blank=True,
        help_text="Model-specific evidence payload. Stored for traceability.",
    )
    detected_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-detected_at"]
        indexes = [
            models.Index(fields=["detection_type", "severity"]),
            models.Index(fields=["-confidence"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_detection_type_display()} {self.severity} ({self.confidence}%)"


class DamageAssessment(models.Model):
    report = models.OneToOneField(DisasterReport, on_delete=models.CASCADE, related_name="damage_assessment")

    citizen_severity_score = models.PositiveSmallIntegerField(default=0)
    photo_ai_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    satellite_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    field_verification_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    overall_confidence = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"), db_index=True)
    recommended_priority = models.CharField(max_length=10, choices=TriageLevel.choices, default=TriageLevel.LOW, db_index=True)
    score_breakdown = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    is_field_verified = models.BooleanField(default=False, db_index=True)

    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_damage_assessments",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["recommended_priority", "-updated_at"]),
        ]

    def __str__(self) -> str:
        return f"Assessment {self.report.reference} ({self.recommended_priority})"
