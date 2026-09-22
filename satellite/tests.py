from __future__ import annotations

import io
from decimal import Decimal

from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Role, User
from reports.models import DamageCategory, DisasterReport, DisasterType
from triage.services import run_triage

from .models import DamageAssessment, SatelliteDetection


def image_file(name: str) -> SimpleUploadedFile:
    buffer = io.BytesIO()
    Image.new("RGB", (120, 120), color=(20, 100, 180)).save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


class SatelliteApiTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin.user",
            password="ResQNet@2026",
            role=Role.ADMIN,
        )
        self.citizen = User.objects.create_user(
            username="citizen.user",
            password="ResQNet@2026",
            role=Role.CITIZEN,
            district="Kollam",
        )
        self.report = DisasterReport.objects.create(
            citizen=self.citizen,
            disaster_type=DisasterType.FLOOD,
            damage_category=DamageCategory.RESIDENTIAL,
            description="Severe flooding with roof and wall impact near the riverbank.",
            latitude=Decimal("8.8932"),
            longitude=Decimal("76.6141"),
            district="Kollam",
            roof_collapsed=True,
            standing_water=True,
            major_structural_damage=True,
            people_affected=4,
        )

    def test_admin_can_run_satellite_analysis(self):
        self.client.force_authenticate(user=self.admin)
        url = reverse("satellite:satellite-analysis-list")

        response = self.client.post(
            url,
            {
                "report": self.report.id,
                "zone_label": "ZONE-03",
                "latitude": "8.8932",
                "longitude": "76.6141",
                "pre_disaster_image": image_file("pre.png"),
                "post_disaster_image": image_file("post.png"),
                "run_now": True,
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["status"], "COMPLETED")
        self.assertEqual(len(response.data["detections"]), 3)
        self.assertEqual(SatelliteDetection.objects.count(), 3)

    def test_non_admin_cannot_access_satellite_endpoint(self):
        self.client.force_authenticate(user=self.citizen)
        url = reverse("satellite:satellite-analysis-list")

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_triage_refreshes_damage_assessment(self):
        run_triage(self.report, user=self.citizen)
        assessment = DamageAssessment.objects.get(report=self.report)

        self.assertGreaterEqual(float(assessment.overall_confidence), 0.0)
        self.assertIn("authoritative_source", assessment.score_breakdown)
