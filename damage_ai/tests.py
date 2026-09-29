from unittest.mock import Mock, patch

from django.test import TestCase

from config.testing import make_citizen, make_image, make_report, make_officer
from inspections.services import start_inspection, update_inspection
from reports.services import attach_photo

from .models import AIAssessmentStatus, AIDamageAssessment


class DamageAssessmentTests(TestCase):
    def setUp(self):
        self.citizen = make_citizen()
        self.report = make_report(self.citizen, submit=False)

    @patch("damage_ai.services._get_predictor")
    def test_photo_upload_stores_model_prediction_and_reuses_it(self, get_predictor):
        predictor = Mock()
        predictor.predict.return_value = {
            "predicted_class": "Moderate",
            "confidence_percentage": "87.40",
        }
        get_predictor.return_value = predictor

        photo = attach_photo(report=self.report, image=make_image(), user=self.citizen)
        assessment = AIDamageAssessment.objects.get(photo=photo)

        self.assertEqual(assessment.status, AIAssessmentStatus.SUCCEEDED)
        self.assertEqual(assessment.predicted_severity, "MODERATE")
        self.assertEqual(str(assessment.confidence_percentage), "87.40")
        self.assertEqual(assessment.model_name, "MobileNetV3-Small")

        from damage_ai.services import assess_photo

        assess_photo(photo)
        predictor.predict.assert_called_once()

    @patch("damage_ai.services._get_predictor", side_effect=RuntimeError("checkpoint unavailable"))
    def test_inference_failure_does_not_block_photo_upload(self, get_predictor):
        photo = attach_photo(report=self.report, image=make_image(), user=self.citizen)
        assessment = AIDamageAssessment.objects.get(photo=photo)

        self.assertEqual(photo.report_id, self.report.id)
        self.assertEqual(assessment.status, AIAssessmentStatus.FAILED)
        self.assertIn("checkpoint unavailable", assessment.error_message)

    @patch("damage_ai.services._get_predictor")
    def test_officer_verification_is_separate_from_ai_result(self, get_predictor):
        predictor = Mock()
        predictor.predict.return_value = {
            "predicted_class": "Severe",
            "confidence_percentage": "93.98",
        }
        get_predictor.return_value = predictor
        officer = make_officer()
        self.report = make_report(self.citizen)
        photo = attach_photo(report=self.report, image=make_image(), user=self.citizen)
        assessment = AIDamageAssessment.objects.get(photo=photo)

        assignment = self.report.assignments.first()
        inspection = start_inspection(assignment=assignment, user=officer)
        update_inspection(
            inspection=inspection,
            data={"ai_verified_severity": "MODERATE"},
            user=officer,
        )
        assessment.refresh_from_db()
        inspection.refresh_from_db()

        self.assertEqual(assessment.predicted_severity, "SEVERE")
        self.assertEqual(inspection.ai_verified_severity, "MODERATE")