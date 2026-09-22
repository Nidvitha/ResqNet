from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from audit.models import AuditAction
from audit.services import record_event
from inspections.models import DamageSeverity
from reports.models import TriageLevel

from .models import (
    AnalysisStatus,
    DamageAssessment,
    DetectionType,
    SatelliteAnalysis,
    SatelliteDetection,
    SatelliteSeverity,
)

logger = logging.getLogger("resqnet.satellite")


@dataclass
class InferenceDetection:
    detection_type: str
    severity: str
    confidence: Decimal
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    affected_area_hectares: Decimal = Decimal("0.00")
    geometry_geojson: dict | None = None
    evidence: dict | None = None


@dataclass
class InferenceResult:
    model_name: str
    model_version: str
    summary: str
    detections: list[InferenceDetection]


class BaseInferenceService:
    def analyze(self, analysis: SatelliteAnalysis) -> InferenceResult:
        raise NotImplementedError


class DemoInferenceService(BaseInferenceService):
    """
    Development backend with deterministic, data-derived outputs.

    This backend does not claim scientific validity. It exists so the rest of
    the pipeline can be exercised locally when a production-grade remote model is
    unavailable.
    """

    model_name = "resqnet-demo-change-detector"
    model_version = "0.1.0"

    def analyze(self, analysis: SatelliteAnalysis) -> InferenceResult:
        pre_size = analysis.pre_disaster_image.size or 1
        post_size = analysis.post_disaster_image.size or 1
        delta = abs(post_size - pre_size)

        digest = hashlib.sha256(f"{analysis.pk}:{pre_size}:{post_size}".encode("utf-8")).hexdigest()
        conf_seed = int(digest[:6], 16) % 100

        flood_conf = Decimal(str(min(95, 45 + (delta % 50))))
        building_conf = Decimal(str(min(95, 35 + (conf_seed % 60))))
        road_conf = Decimal(str(min(92, 30 + ((delta // 1024) % 55))))

        detections = [
            InferenceDetection(
                detection_type=DetectionType.FLOOD,
                severity=_severity_from_confidence(flood_conf),
                confidence=flood_conf,
                latitude=analysis.latitude,
                longitude=analysis.longitude,
                affected_area_hectares=Decimal(str(max(0.5, round(float(delta % 20000) / 2000.0, 2)))),
                geometry_geojson=analysis.region_geojson or {},
                evidence={"change_delta_bytes": delta},
            ),
            InferenceDetection(
                detection_type=DetectionType.BUILDING_DAMAGE,
                severity=_severity_from_confidence(building_conf),
                confidence=building_conf,
                latitude=analysis.latitude,
                longitude=analysis.longitude,
                affected_area_hectares=Decimal("0.00"),
                geometry_geojson=analysis.region_geojson or {},
                evidence={"signature": digest[:12]},
            ),
            InferenceDetection(
                detection_type=DetectionType.ROAD_DISRUPTION,
                severity=_severity_from_confidence(road_conf),
                confidence=road_conf,
                latitude=analysis.latitude,
                longitude=analysis.longitude,
                affected_area_hectares=Decimal("0.00"),
                geometry_geojson=analysis.region_geojson or {},
                evidence={"delta_kb": round(delta / 1024.0, 2)},
            ),
        ]

        summary = (
            "Demo inference backend generated detections from image-change proxies. "
            "Use for pipeline validation only."
        )

        return InferenceResult(
            model_name=self.model_name,
            model_version=self.model_version,
            summary=summary,
            detections=detections,
        )


def _severity_from_confidence(confidence: Decimal) -> str:
    if confidence >= Decimal("85"):
        return SatelliteSeverity.CRITICAL
    if confidence >= Decimal("70"):
        return SatelliteSeverity.HIGH
    if confidence >= Decimal("50"):
        return SatelliteSeverity.MEDIUM
    return SatelliteSeverity.LOW


def _get_backend() -> BaseInferenceService:
    backend = getattr(settings, "SATELLITE_INFERENCE_BACKEND", "demo").lower().strip()
    if backend == "demo":
        return DemoInferenceService()
    raise ValueError(f"Unsupported SATELLITE_INFERENCE_BACKEND '{backend}'.")


@transaction.atomic
def run_satellite_analysis(analysis: SatelliteAnalysis, *, user=None, request=None) -> SatelliteAnalysis:
    analysis.status = AnalysisStatus.RUNNING
    analysis.started_at = timezone.now()
    analysis.error_message = ""
    analysis.save(update_fields=["status", "started_at", "error_message", "updated_at"])

    record_event(
        entity=analysis.report if analysis.report_id else analysis,
        action=AuditAction.SATELLITE_ANALYSIS_REQUESTED,
        user=user,
        metadata={"analysis_id": analysis.pk, "zone_label": analysis.zone_label},
        request=request,
    )

    try:
        result = _get_backend().analyze(analysis)
    except Exception as exc:  # noqa: BLE001
        analysis.status = AnalysisStatus.FAILED
        analysis.error_message = str(exc)
        analysis.completed_at = timezone.now()
        analysis.save(update_fields=["status", "error_message", "completed_at", "updated_at"])
        logger.exception("Satellite analysis failed for %s", analysis.pk)
        return analysis

    analysis.detections.all().delete()
    SatelliteDetection.objects.bulk_create(
        [
            SatelliteDetection(
                analysis=analysis,
                detection_type=item.detection_type,
                severity=item.severity,
                confidence=item.confidence,
                latitude=item.latitude,
                longitude=item.longitude,
                affected_area_hectares=item.affected_area_hectares,
                geometry_geojson=item.geometry_geojson or {},
                evidence=item.evidence or {},
            )
            for item in result.detections
        ]
    )

    analysis.model_name = result.model_name
    analysis.model_version = result.model_version
    analysis.summary = result.summary
    analysis.status = AnalysisStatus.COMPLETED
    analysis.completed_at = timezone.now()
    analysis.save(
        update_fields=[
            "model_name",
            "model_version",
            "summary",
            "status",
            "completed_at",
            "updated_at",
        ]
    )

    if analysis.report_id:
        refresh_damage_assessment(analysis.report, user=user, request=request)

    record_event(
        entity=analysis.report if analysis.report_id else analysis,
        action=AuditAction.SATELLITE_ANALYSIS_COMPLETED,
        user=user,
        metadata={
            "analysis_id": analysis.pk,
            "detections": analysis.detections.count(),
            "model": analysis.model_name,
            "model_version": analysis.model_version,
        },
        request=request,
    )
    return analysis


def _average_detection_confidence(report) -> Decimal:
    rows = SatelliteDetection.objects.filter(analysis__report=report)
    if not rows.exists():
        return Decimal("0.00")
    total = Decimal("0.00")
    count = 0
    for row in rows:
        total += row.confidence
        count += 1
    return (total / Decimal(str(count))).quantize(Decimal("0.01"))


def _field_verification_score(report) -> tuple[Decimal | None, bool]:
    inspection = getattr(report, "inspection", None)
    if inspection is None or inspection.status != "SIGNED":
        return None, False

    mapping = {
        DamageSeverity.NONE: Decimal("0"),
        DamageSeverity.MINOR: Decimal("25"),
        DamageSeverity.MODERATE: Decimal("50"),
        DamageSeverity.SEVERE: Decimal("80"),
        DamageSeverity.DESTROYED: Decimal("95"),
    }
    severities = list(inspection.severity_map().values())
    values = [mapping.get(item, Decimal("0")) for item in severities]
    average = (sum(values, Decimal("0")) / Decimal(str(len(values)))).quantize(Decimal("0.01"))
    return average, True


def _priority_from_score(score: Decimal) -> str:
    if score >= Decimal("85"):
        return TriageLevel.CRITICAL
    if score >= Decimal("65"):
        return TriageLevel.HIGH
    if score >= Decimal("40"):
        return TriageLevel.MEDIUM
    return TriageLevel.LOW


@transaction.atomic
def refresh_damage_assessment(report, *, user=None, request=None) -> DamageAssessment:
    citizen_score = int(report.priority_score or 0)
    satellite_score = _average_detection_confidence(report)
    field_score, is_verified = _field_verification_score(report)

    components = {
        "citizen": Decimal(str(citizen_score)) * Decimal("0.40"),
        "satellite": satellite_score * Decimal("0.35"),
        "field": (field_score or Decimal("0")) * Decimal("0.25"),
    }

    overall = sum(components.values(), Decimal("0")).quantize(Decimal("0.01"))
    if is_verified and field_score is not None:
        overall = ((overall * Decimal("0.6")) + (field_score * Decimal("0.4"))).quantize(Decimal("0.01"))

    assessment, _ = DamageAssessment.objects.update_or_create(
        report=report,
        defaults={
            "citizen_severity_score": citizen_score,
            "satellite_score": satellite_score,
            "field_verification_score": field_score,
            "overall_confidence": overall,
            "recommended_priority": _priority_from_score(overall),
            "score_breakdown": {
                "weights": {"citizen": 0.40, "satellite": 0.35, "field": 0.25},
                "raw": {
                    "citizen_severity_score": citizen_score,
                    "satellite_score": float(satellite_score),
                    "field_verification_score": float(field_score) if field_score is not None else None,
                },
                "weighted": {k: float(v.quantize(Decimal("0.01"))) for k, v in components.items()},
                "authoritative_source": "FIELD_VERIFICATION" if is_verified else "MULTI_SOURCE_AI",
            },
            "is_field_verified": is_verified,
            "updated_by": user,
        },
    )

    record_event(
        entity=report,
        action=AuditAction.DAMAGE_ASSESSMENT_UPDATED,
        user=user,
        metadata={
            "overall_confidence": str(assessment.overall_confidence),
            "recommended_priority": assessment.recommended_priority,
            "field_verified": assessment.is_field_verified,
        },
        request=request,
    )

    return assessment
