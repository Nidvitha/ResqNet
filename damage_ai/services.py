from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .models import AIAssessmentStatus, AIDamageAssessment

logger = logging.getLogger("resqnet.damage_ai")

MODEL_NAME = "MobileNetV3-Small"
MODEL_VERSION = "damage-severity-v1"


@lru_cache(maxsize=1)
def _get_predictor():
    """Load the checkpoint once per Django process, on the first inference."""
    from ml_damage_severity.predict import DamageSeverityPredictor

    model_path = Path(
        getattr(
            settings,
            "DAMAGE_SEVERITY_MODEL_PATH",
            settings.BASE_DIR / "ml_damage_severity/artifacts/damage_severity_mobilenet_v3_small.pth",
        )
    )
    return DamageSeverityPredictor(model_path)


def assess_photo(photo) -> AIDamageAssessment:
    """Run at most one inference for a photo and always return its audit row."""
    assessment, created = AIDamageAssessment.objects.get_or_create(
        photo=photo,
        defaults={
            "report": photo.report,
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
        },
    )
    if not created and assessment.status in {
        AIAssessmentStatus.SUCCEEDED,
        AIAssessmentStatus.FAILED,
    }:
        return assessment

    try:
        result = _get_predictor().predict(photo.image.path)
        confidence = Decimal(str(result["confidence_percentage"]))
        assessment.predicted_severity = result["predicted_class"].upper()
        assessment.confidence_percentage = confidence
        assessment.status = AIAssessmentStatus.SUCCEEDED
        assessment.error_message = ""
        assessment.inferred_at = timezone.now()
    except Exception as exc:  # noqa: BLE001 - inference must never block a claim
        logger.exception("AI damage assessment failed for photo %s", photo.pk)
        assessment.status = AIAssessmentStatus.FAILED
        assessment.error_message = str(exc)[:4000]
        assessment.inferred_at = None
    assessment.save(
        update_fields=[
            "predicted_severity", "confidence_percentage", "status",
            "error_message", "inferred_at", "updated_at",
        ]
    )
    return assessment