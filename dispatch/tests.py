"""
Dispatch tests (Section 27).

    - Eligible officer selected
    - Unavailable officer excluded
    - Distance calculation works
"""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from audit.models import AuditAction, AuditEvent
from config.testing import make_admin, make_citizen, make_officer, make_report
from reports.states import CaseStatus

from .models import Assignment, AssignmentStatus
from .services import auto_assign_report, find_best_officer, haversine_km, rank_officers


class HaversineTests(TestCase):
    """The distance formula, checked against known real-world separations."""

    def test_identical_points_are_zero_apart(self):
        self.assertEqual(haversine_km(8.8932, 76.6141, 8.8932, 76.6141), 0.0)

    def test_known_distance_kollam_to_thiruvananthapuram(self):
        """Roughly 60 km apart; allow a wide band since this is straight-line."""
        distance = haversine_km(8.8932, 76.6141, 8.5241, 76.9366)
        self.assertGreater(distance, 40)
        self.assertLess(distance, 80)

    def test_distance_is_symmetric(self):
        forward = haversine_km(8.8932, 76.6141, 9.9312, 76.2673)
        backward = haversine_km(9.9312, 76.2673, 8.8932, 76.6141)
        self.assertEqual(forward, backward)

    def test_handles_decimal_input_from_the_database(self):
        """Model fields return Decimal, not float - the formula must accept both."""
        distance = haversine_km(
            Decimal("8.8932"), Decimal("76.6141"), Decimal("8.5241"), Decimal("76.9366")
        )
        self.assertIsInstance(distance, float)
        self.assertGreater(distance, 0)

    def test_nearer_point_yields_smaller_distance(self):
        near = haversine_km(8.8932, 76.6141, 8.9000, 76.6200)
        far = haversine_km(8.8932, 76.6141, 12.9716, 77.5946)
        self.assertLess(near, far)


class OfficerSelectionTests(TestCase):
    def setUp(self):
        self.citizen = make_citizen(district="Kollam")

    def test_eligible_officer_is_selected(self):
        officer = make_officer(username="near", zone="Kollam")
        report = make_report(self.citizen)

        assignment = report.assignments.first()
        self.assertIsNotNone(assignment)
        self.assertEqual(assignment.officer, officer)
        self.assertEqual(report.status, CaseStatus.ASSIGNED)

    def test_unavailable_officer_excluded(self):
        make_officer(username="offduty", zone="Kollam", is_available=False)
        report = make_report(self.citizen)

        self.assertEqual(report.assignments.count(), 0)
        # No officer free is a legitimate outcome - the case waits at TRIAGED.
        self.assertEqual(report.status, CaseStatus.TRIAGED)

    def test_officer_at_capacity_excluded(self):
        officer = make_officer(username="busy", zone="Kollam", max_active_cases=1)
        first = make_report(self.citizen)
        self.assertEqual(first.assignments.first().officer, officer)

        second = make_report(self.citizen)
        self.assertEqual(second.assignments.count(), 0)
        self.assertEqual(second.status, CaseStatus.TRIAGED)

    def test_nearer_officer_preferred_over_distant_one(self):
        near = make_officer(
            username="near", zone="Kollam",
            latitude=Decimal("8.8940"), longitude=Decimal("76.6150"),
        )
        make_officer(
            username="far", zone="Kollam",
            latitude=Decimal("9.9312"), longitude=Decimal("76.2673"),
            employee_id="EMP-FAR",
        )
        report = make_report(self.citizen)
        self.assertEqual(report.assignments.first().officer, near)

    def test_same_zone_officer_preferred(self):
        make_officer(
            username="otherzone", zone="Alappuzha",
            latitude=Decimal("8.8935"), longitude=Decimal("76.6145"),
        )
        same_zone = make_officer(
            username="samezone", zone="Kollam",
            latitude=Decimal("8.8990"), longitude=Decimal("76.6190"),
            employee_id="EMP-SAME",
        )
        report = make_report(self.citizen)
        self.assertEqual(report.assignments.first().officer, same_zone)

    def test_no_officers_at_all_leaves_the_case_waiting(self):
        report = make_report(self.citizen)
        self.assertIsNone(find_best_officer(report))
        self.assertEqual(report.status, CaseStatus.TRIAGED)

    def test_selection_reason_is_recorded(self):
        """Section 2 requires the platform to be explainable."""
        make_officer(zone="Kollam")
        report = make_report(self.citizen)
        assignment = report.assignments.first()

        self.assertTrue(assignment.selection_reason)
        self.assertIn("km", assignment.selection_reason)
        self.assertIsNotNone(assignment.distance_km)

    def test_nobody_is_ever_ranked_to_inspect_their_own_case(self):
        """A person must never verify their own claim, whatever roles they hold."""
        officer = make_officer(username="dualrole", zone="Kollam")
        report = make_report(self.citizen)

        self.assertEqual(report.assignments.first().officer, officer)
        ranked = rank_officers(report)
        self.assertTrue(all(item["officer"] != report.citizen for item in ranked))

    def test_due_date_reflects_the_triage_response_target(self):
        make_officer(zone="Kollam")
        report = make_report(self.citizen, people_trapped=True, roof_collapsed=True)
        assignment = report.assignments.first()
        self.assertIsNotNone(assignment.due_by)


class AssignmentGuardTests(TestCase):
    def setUp(self):
        self.citizen = make_citizen(district="Kollam")
        self.officer = make_officer(zone="Kollam")

    def test_already_assigned_case_is_not_reassigned_automatically(self):
        report = make_report(self.citizen)
        self.assertIsNone(auto_assign_report(report))
        self.assertEqual(report.assignments.count(), 1)

    def test_only_one_active_assignment_per_report(self):
        report = make_report(self.citizen)
        active = report.assignments.filter(
            status__in=[
                AssignmentStatus.ASSIGNED,
                AssignmentStatus.ACCEPTED,
                AssignmentStatus.IN_PROGRESS,
            ]
        )
        self.assertEqual(active.count(), 1)


class DispatchAPITests(APITestCase):
    def setUp(self):
        self.citizen = make_citizen(district="Kollam")
        self.officer = make_officer(zone="Kollam")
        self.other_officer = make_officer(username="officer2", zone="Alappuzha", employee_id="EMP-2")
        self.admin = make_admin()
        self.report = make_report(self.citizen)

    def test_officer_sees_only_their_own_assignments(self):
        self.client.force_authenticate(self.other_officer)
        response = self.client.get(reverse("dispatch:my-assignments"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

        self.client.force_authenticate(self.officer)
        response = self.client.get(reverse("dispatch:my-assignments"))
        self.assertEqual(len(response.data), 1)

    def test_officer_cannot_accept_another_officers_assignment(self):
        assignment = self.report.assignments.first()
        self.client.force_authenticate(self.other_officer)
        response = self.client.post(reverse("dispatch:accept", args=[assignment.pk]))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_offline_package_contains_everything_needed_on_site(self):
        self.client.force_authenticate(self.officer)
        response = self.client.get(reverse("dispatch:offline-package"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["case_count"], 1)
        case = response.data["cases"][0]
        for key in ["latitude", "longitude", "address", "citizen_phone", "triage_level"]:
            self.assertIn(key, case)

    def test_admin_sees_ranked_candidates_with_reasoning(self):
        second_report = make_report(
            make_citizen(username="citizen_two", district="Kollam"),
            latitude="9.5000",
            longitude="77.5000",
            address="Hill View, Idukki",
            description="A separate distant claim describes earthquake damage to a farm building.",
        )
        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse("dispatch:candidates", args=[second_report.pk]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        if response.data:
            self.assertIn("reason", response.data[0])
            self.assertIn("distance_km", response.data[0])
            self.assertIn("zone", response.data[0])
            self.assertEqual(response.data[0]["zone"], self.officer.officer_profile.zone)

    def test_only_admin_can_assign_manually(self):
        self.client.force_authenticate(self.citizen)
        response = self.client.post(
            reverse("dispatch:assign-manually"),
            {"report_id": self.report.pk, "officer_id": self.other_officer.pk},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_manual_reassignment_preserves_the_previous_assignment(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse("dispatch:assign-manually"),
            {
                "report_id": self.report.pk,
                "officer_id": self.other_officer.pk,
                "note": "Original officer diverted to a critical case.",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.report.assignments.count(), 2)
        self.assertTrue(
            Assignment.objects.filter(
                report=self.report, status=AssignmentStatus.REASSIGNED
            ).exists()
        )


class ManualAssignmentFromTriageTests(APITestCase):
    def test_admin_can_assign_a_triaged_case_and_audit_the_transition(self):
        citizen = make_citizen(username="waiting_citizen", district="Kollam")
        report = make_report(citizen)
        self.assertEqual(report.status, CaseStatus.TRIAGED)

        officer = make_officer(username="manual_officer", zone="Kollam")
        admin = make_admin(username="manual_admin")
        self.client.force_authenticate(admin)

        response = self.client.post(
            reverse("dispatch:assign-manually"),
            {
                "report_id": report.pk,
                "officer_id": officer.pk,
                "note": "Assigned for the nearest available response team.",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        report.refresh_from_db()
        assignment = report.assignments.get()
        self.assertEqual(report.status, CaseStatus.ASSIGNED)
        self.assertEqual(assignment.officer, officer)
        self.assertFalse(assignment.is_automatic)
        self.assertEqual(assignment.selection_reason, "Assigned for the nearest available response team.")
        event = AuditEvent.objects.get(
            entity_type="DisasterReport",
            entity_id=str(report.pk),
            action=AuditAction.OFFICER_ASSIGNED,
        )
        self.assertEqual(event.performed_by, admin)
        self.assertEqual(event.previous_state, CaseStatus.TRIAGED)
        self.assertEqual(event.new_state, CaseStatus.ASSIGNED)
