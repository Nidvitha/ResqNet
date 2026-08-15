"""
Inspection tests (Section 27).

    - Only authorized officer can submit
    - Inspection cannot be completed twice incorrectly

Plus the Section 32 rule this app exists to protect: Officer A must not be able
to modify Officer B's inspection.
"""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from config.testing import make_admin, make_citizen, make_officer, make_report, make_signed_case
from reports.states import CaseStatus, InvalidTransition

from .models import Inspection, InspectionStatus
from .services import (
    InspectionPermissionError,
    complete_inspection,
    sign_off_inspection,
    start_inspection,
    update_inspection,
)


class InspectionWorkflowTests(TestCase):
    """ASSIGNED -> STARTED -> COMPLETED -> SIGNED, in that order only."""

    def setUp(self):
        self.citizen = make_citizen(district="Kollam")
        self.officer = make_officer(zone="Kollam")
        self.report = make_report(self.citizen)
        self.assignment = self.report.assignments.first()

    def test_starting_an_inspection_advances_the_case(self):
        inspection = start_inspection(assignment=self.assignment, user=self.officer)

        self.assertEqual(inspection.status, InspectionStatus.STARTED)
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, CaseStatus.INSPECTION_PENDING)

    def test_starting_twice_returns_the_same_inspection(self):
        """A dropped connection must not create a second record."""
        first = start_inspection(assignment=self.assignment, user=self.officer)
        second = start_inspection(assignment=self.assignment, user=self.officer)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Inspection.objects.count(), 1)

    def test_inspection_cannot_be_completed_twice(self):
        inspection = start_inspection(assignment=self.assignment, user=self.officer)
        update_inspection(
            inspection=inspection, data={"roof_damage": "SEVERE"}, user=self.officer
        )
        complete_inspection(inspection=inspection, user=self.officer)

        with self.assertRaises(InvalidTransition):
            complete_inspection(inspection=inspection, user=self.officer)

    def test_empty_inspection_cannot_be_completed(self):
        """No findings and no explanation is not a valid assessment."""
        inspection = start_inspection(assignment=self.assignment, user=self.officer)
        with self.assertRaises(InvalidTransition):
            complete_inspection(inspection=inspection, user=self.officer)

    def test_empty_inspection_completes_when_explained(self):
        inspection = start_inspection(assignment=self.assignment, user=self.officer)
        update_inspection(
            inspection=inspection,
            data={"remarks": "Attended site. No structural damage found; water had receded."},
            user=self.officer,
        )
        complete_inspection(inspection=inspection, user=self.officer)
        self.assertEqual(inspection.status, InspectionStatus.COMPLETED)

    def test_cannot_sign_off_before_completing(self):
        inspection = start_inspection(assignment=self.assignment, user=self.officer)
        with self.assertRaises(InvalidTransition):
            sign_off_inspection(
                inspection=inspection, signature_name="Test Officer", user=self.officer
            )

    def test_sign_off_freezes_the_record_and_advances_the_case(self):
        report, inspection, officer = make_signed_case()

        self.assertEqual(inspection.status, InspectionStatus.SIGNED)
        self.assertFalse(inspection.is_editable)
        self.assertIsNotNone(inspection.signed_at)
        # Sign-off runs compensation, which moves the case to PENDING_APPROVAL.
        self.assertEqual(report.status, CaseStatus.PENDING_APPROVAL)

    def test_signed_inspection_cannot_be_modified(self):
        _, inspection, officer = make_signed_case()
        with self.assertRaises(InvalidTransition):
            update_inspection(
                inspection=inspection, data={"roof_damage": "DESTROYED"}, user=officer
            )

    def test_sign_off_requires_a_signature_name(self):
        inspection = start_inspection(assignment=self.assignment, user=self.officer)
        update_inspection(inspection=inspection, data={"roof_damage": "MINOR"}, user=self.officer)
        complete_inspection(inspection=inspection, user=self.officer)

        with self.assertRaises(InvalidTransition):
            sign_off_inspection(inspection=inspection, signature_name="   ", user=self.officer)


class InspectionAuthorizationTests(TestCase):
    """Section 32 - officer A must not touch officer B's work."""

    def setUp(self):
        self.citizen = make_citizen(district="Kollam")
        self.officer_a = make_officer(username="officer_a", zone="Kollam")
        self.officer_b = make_officer(username="officer_b", zone="Kollam", employee_id="EMP-B")
        self.report = make_report(self.citizen)
        self.assignment = self.report.assignments.first()
        self.inspection = start_inspection(assignment=self.assignment, user=self.assignment.officer)
        self.other_officer = (
            self.officer_b if self.assignment.officer == self.officer_a else self.officer_a
        )

    def test_officer_cannot_modify_another_officers_inspection(self):
        with self.assertRaises(InspectionPermissionError):
            update_inspection(
                inspection=self.inspection,
                data={"roof_damage": "DESTROYED"},
                user=self.other_officer,
            )

    def test_officer_cannot_complete_another_officers_inspection(self):
        with self.assertRaises(InspectionPermissionError):
            complete_inspection(inspection=self.inspection, user=self.other_officer)

    def test_officer_cannot_start_an_inspection_on_an_unassigned_case(self):
        with self.assertRaises(InspectionPermissionError):
            start_inspection(assignment=self.assignment, user=self.other_officer)

    def test_admin_may_intervene(self):
        """Section 16 allows "appropriately authorized personnel"."""
        admin = make_admin()
        update_inspection(
            inspection=self.inspection, data={"roof_damage": "MINOR"}, user=admin
        )
        self.inspection.refresh_from_db()
        self.assertEqual(self.inspection.roof_damage, "MINOR")


class InspectionAPITests(APITestCase):
    def setUp(self):
        self.citizen = make_citizen(district="Kollam")
        self.officer = make_officer(zone="Kollam")
        # The intruder sits in a different zone and far away, so automatic
        # dispatch never picks them - they are purely an unauthorised third party.
        self.intruder = make_officer(
            username="intruder",
            zone="Ernakulam",
            latitude=Decimal("9.9312"),
            longitude=Decimal("76.2673"),
            employee_id="EMP-INT",
        )
        self.report = make_report(self.citizen)
        self.assignment = self.report.assignments.first()
        self.assertEqual(self.assignment.officer, self.officer)

    def test_officer_starts_an_inspection_over_the_api(self):
        self.client.force_authenticate(self.assignment.officer)
        response = self.client.post(
            reverse("inspections:start"),
            {"assignment_id": self.assignment.pk},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_another_officer_is_refused_at_the_api(self):
        self.client.force_authenticate(self.intruder)
        response = self.client.post(
            reverse("inspections:start"),
            {"assignment_id": self.assignment.pk},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_another_officers_inspection_is_not_even_visible(self):
        inspection = start_inspection(assignment=self.assignment, user=self.assignment.officer)
        self.client.force_authenticate(self.intruder)
        response = self.client.get(reverse("inspections:inspection-detail", args=[inspection.pk]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_client_cannot_sign_off_by_posting_a_status(self):
        """The update serializer has no `status` field, so this is silently ignored."""
        inspection = start_inspection(assignment=self.assignment, user=self.assignment.officer)
        self.client.force_authenticate(self.assignment.officer)
        self.client.patch(
            reverse("inspections:inspection-detail", args=[inspection.pk]),
            {"status": "SIGNED", "roof_damage": "SEVERE"},
            format="json",
        )
        inspection.refresh_from_db()
        self.assertEqual(inspection.status, InspectionStatus.STARTED)

    def test_citizen_can_read_but_not_write_their_own_inspection(self):
        inspection = start_inspection(assignment=self.assignment, user=self.assignment.officer)
        self.client.force_authenticate(self.citizen)

        self.assertEqual(
            self.client.get(reverse("inspections:inspection-detail", args=[inspection.pk])).status_code,
            status.HTTP_200_OK,
        )
        response = self.client.patch(
            reverse("inspections:inspection-detail", args=[inspection.pk]),
            {"roof_damage": "DESTROYED"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class InspectionSyncTests(APITestCase):
    """Section 13 - offline inspections replayed safely."""

    def setUp(self):
        self.citizen = make_citizen(district="Kollam")
        self.officer = make_officer(zone="Kollam")
        self.report = make_report(self.citizen)
        self.assignment = self.report.assignments.first()
        self.url = reverse("inspections:sync")

    def test_offline_inspection_syncs_and_signs_off(self):
        self.client.force_authenticate(self.assignment.officer)
        response = self.client.post(
            self.url,
            {
                "inspections": [
                    {
                        "assignment_id": self.assignment.pk,
                        "idempotency_key": "device-insp-1",
                        "roof_damage": "SEVERE",
                        "wall_damage": "MODERATE",
                        "people_affected": 3,
                        "remarks": "Assessed on site while offline.",
                        "signature_name": "Field Officer",
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["synced"], 1)

        self.report.refresh_from_db()
        self.assertEqual(self.report.status, CaseStatus.PENDING_APPROVAL)

    def test_replayed_sync_does_not_duplicate_or_recalculate(self):
        self.client.force_authenticate(self.assignment.officer)
        payload = {
            "inspections": [
                {
                    "assignment_id": self.assignment.pk,
                    "idempotency_key": "device-insp-1",
                    "roof_damage": "SEVERE",
                    "remarks": "Assessed on site while offline.",
                    "signature_name": "Field Officer",
                }
            ]
        }
        self.client.post(self.url, payload, format="json")
        second = self.client.post(self.url, payload, format="json")

        self.assertEqual(second.data["synced"], 0)
        self.assertEqual(second.data["duplicates"], 1)
        self.assertEqual(Inspection.objects.count(), 1)
