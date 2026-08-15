"""
Report and state-machine tests (Section 27).

    Reports                                          State machine
    - Citizen can create report                      - Illegal transitions refused
    - Invalid report rejected                        - Role restrictions enforced
    - Citizen cannot access another citizen's report - Terminal states are terminal
"""

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from config.testing import make_admin, make_citizen, make_image, make_officer, make_report, report_payload

from .models import DisasterReport
from .states import CaseStatus, InvalidTransition, assert_transition, can_transition, progress_percent


class ReportCreationTests(APITestCase):
    def setUp(self):
        self.citizen = make_citizen()
        self.officer = make_officer()          # so dispatch has somebody to assign
        self.url = reverse("reports:report-list")

    def test_citizen_can_create_report(self):
        self.client.force_authenticate(self.citizen)
        response = self.client.post(self.url, report_payload(), format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        report = DisasterReport.objects.get(pk=response.data["id"])
        self.assertEqual(report.citizen, self.citizen)
        self.assertTrue(report.reference.startswith("RQN-"))
        # Submitted reports are triaged and dispatched immediately.
        self.assertEqual(report.status, CaseStatus.ASSIGNED)
        self.assertGreater(report.priority_score, 0)

    def test_report_can_be_saved_as_draft(self):
        self.client.force_authenticate(self.citizen)
        response = self.client.post(
            self.url, report_payload(submit_now=False), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], CaseStatus.DRAFT)

    def test_invalid_report_rejected_short_description(self):
        self.client.force_authenticate(self.citizen)
        response = self.client.post(self.url, report_payload(description="flood"), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("description", response.data)

    def test_invalid_report_rejected_out_of_range_coordinates(self):
        self.client.force_authenticate(self.citizen)
        response = self.client.post(self.url, report_payload(latitude="120.0"), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_report_rejected_null_island_coordinates(self):
        """(0, 0) is almost always an uninitialised GPS reading, not a location."""
        self.client.force_authenticate(self.citizen)
        response = self.client.post(
            self.url, report_payload(latitude="0", longitude="0"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_officer_cannot_file_a_report(self):
        self.client.force_authenticate(self.officer)
        response = self.client.post(self.url, report_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_client_cannot_set_its_own_status_or_score(self):
        """Section 26: never allow arbitrary status manipulation."""
        self.client.force_authenticate(self.citizen)
        response = self.client.post(
            self.url,
            report_payload(status="APPROVED", priority_score=999),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        report = DisasterReport.objects.get(pk=response.data["id"])
        self.assertNotEqual(report.status, CaseStatus.APPROVED)
        self.assertNotEqual(report.priority_score, 999)


class ReportOwnershipTests(APITestCase):
    """Section 32 - ownership is enforced by the backend, not by hidden buttons."""

    def setUp(self):
        self.citizen_a = make_citizen(username="citizen_a")
        self.citizen_b = make_citizen(username="citizen_b")
        make_officer()
        self.report_a = make_report(self.citizen_a)

    def test_citizen_cannot_access_another_citizens_report(self):
        self.client.force_authenticate(self.citizen_b)
        response = self.client.get(
            reverse("reports:report-detail", args=[self.report_a.pk])
        )
        # 404, not 403: the row is not in citizen B's queryset at all.
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_citizen_list_shows_only_their_own_reports(self):
        make_report(self.citizen_b)
        self.client.force_authenticate(self.citizen_b)
        response = self.client.get(reverse("reports:report-list"))
        references = [row["reference"] for row in response.data["results"]]
        self.assertNotIn(self.report_a.reference, references)

    def test_admin_can_read_any_report(self):
        self.client.force_authenticate(make_admin())
        response = self.client.get(
            reverse("reports:report-detail", args=[self.report_a.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_officer_sees_only_assigned_reports(self):
        other_officer = make_officer(username="officer_other", employee_id="EMP-OTHER")
        self.client.force_authenticate(other_officer)
        response = self.client.get(reverse("reports:report-list"))
        self.assertEqual(response.data["count"], 0)


class ReportEditingTests(APITestCase):
    def setUp(self):
        self.citizen = make_citizen()
        make_officer()

    def test_draft_can_be_edited(self):
        report = make_report(self.citizen, submit=False)
        self.client.force_authenticate(self.citizen)
        response = self.client.patch(
            reverse("reports:report-detail", args=[report.pk]),
            {"description": "Updated description with more than twenty characters."},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_submitted_report_cannot_be_edited(self):
        report = make_report(self.citizen, submit=True)
        self.client.force_authenticate(self.citizen)
        response = self.client.patch(
            reverse("reports:report-detail", args=[report.pk]),
            {"description": "Trying to change a submitted report after the fact."},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_delete_cancels_rather_than_destroying(self):
        report = make_report(self.citizen, submit=False)
        self.client.force_authenticate(self.citizen)
        self.client.delete(reverse("reports:report-detail", args=[report.pk]))

        report.refresh_from_db()
        self.assertEqual(report.status, CaseStatus.CANCELLED)
        self.assertTrue(DisasterReport.objects.filter(pk=report.pk).exists())


class PhotoUploadTests(APITestCase):
    def setUp(self):
        self.citizen = make_citizen()
        make_officer()
        self.report = make_report(self.citizen)

    def test_valid_image_accepted(self):
        self.client.force_authenticate(self.citizen)
        response = self.client.post(
            reverse("reports:report-upload-photo", args=[self.report.pk]),
            {"image": make_image(), "caption": "Damaged wall"},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.report.photos.count(), 1)

    def test_non_image_file_rejected(self):
        """A renamed script must fail even though the extension looks right."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.force_authenticate(self.citizen)
        fake = SimpleUploadedFile("evil.jpg", b"<?php echo 'not an image'; ?>", content_type="image/jpeg")
        response = self.client.post(
            reverse("reports:report-upload-photo", args=[self.report.pk]),
            {"image": fake},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_uploaded_filename_is_not_taken_from_the_client(self):
        """Path traversal in a filename must never reach the filesystem."""
        self.client.force_authenticate(self.citizen)
        self.client.post(
            reverse("reports:report-upload-photo", args=[self.report.pk]),
            {"image": make_image(name="../../../settings.jpg")},
            format="multipart",
        )
        photo = self.report.photos.first()
        self.assertIsNotNone(photo)
        self.assertNotIn("..", photo.image.name)
        self.assertTrue(photo.image.name.startswith("reports/evidence/"))


class StateMachineTests(APITestCase):
    """The rules from Section 6, tested directly against the transition table."""

    def test_legal_forward_transition_allowed(self):
        self.assertTrue(can_transition(CaseStatus.DRAFT, CaseStatus.SUBMITTED))
        self.assertTrue(can_transition(CaseStatus.INSPECTED, CaseStatus.PENDING_APPROVAL))

    def test_skipping_ahead_is_refused(self):
        """"Admin cannot approve an uninspected case" - structurally impossible."""
        self.assertFalse(can_transition(CaseStatus.SUBMITTED, CaseStatus.APPROVED))
        with self.assertRaises(InvalidTransition):
            assert_transition(CaseStatus.SUBMITTED, CaseStatus.APPROVED, role="ADMIN")

    def test_payout_cannot_complete_before_approval(self):
        self.assertFalse(can_transition(CaseStatus.PENDING_APPROVAL, CaseStatus.PAYOUT_COMPLETED))
        with self.assertRaises(InvalidTransition):
            assert_transition(CaseStatus.APPROVED, CaseStatus.PAYOUT_COMPLETED, role="ADMIN")

    def test_role_restrictions_enforced(self):
        """A citizen cannot approve, however legal the transition itself is."""
        with self.assertRaises(InvalidTransition):
            assert_transition(CaseStatus.PENDING_APPROVAL, CaseStatus.APPROVED, role="CITIZEN")
        assert_transition(CaseStatus.PENDING_APPROVAL, CaseStatus.APPROVED, role="ADMIN")

    def test_terminal_states_have_no_exit(self):
        for terminal in [CaseStatus.PAYOUT_COMPLETED, CaseStatus.CANCELLED]:
            with self.subTest(state=terminal):
                with self.assertRaises(InvalidTransition):
                    assert_transition(terminal, CaseStatus.SUBMITTED, role="ADMIN")

    def test_rejected_case_may_enter_needs_review(self):
        """Section 6 names this path explicitly."""
        self.assertTrue(can_transition(CaseStatus.REJECTED, CaseStatus.NEEDS_REVIEW))

    def test_same_state_transition_refused(self):
        with self.assertRaises(InvalidTransition):
            assert_transition(CaseStatus.SUBMITTED, CaseStatus.SUBMITTED, role="ADMIN")

    def test_progress_percentage_increases_along_the_happy_path(self):
        self.assertEqual(progress_percent(CaseStatus.DRAFT), 0)
        self.assertLess(
            progress_percent(CaseStatus.SUBMITTED), progress_percent(CaseStatus.INSPECTED)
        )
        self.assertEqual(progress_percent(CaseStatus.PAYOUT_COMPLETED), 100)


class OfflineSyncTests(APITestCase):
    """Section 13 - a replayed queue must not create duplicate cases."""

    def setUp(self):
        self.citizen = make_citizen()
        make_officer()
        self.url = reverse("reports:sync")

    def test_offline_batch_is_synced(self):
        self.client.force_authenticate(self.citizen)
        response = self.client.post(
            self.url,
            {"reports": [report_payload(idempotency_key="device-abc-1")]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["synced"], 1)
        self.assertEqual(DisasterReport.objects.filter(citizen=self.citizen).count(), 1)

    def test_replaying_the_same_key_does_not_duplicate(self):
        self.client.force_authenticate(self.citizen)
        payload = {"reports": [report_payload(idempotency_key="device-abc-1")]}

        self.client.post(self.url, payload, format="json")
        second = self.client.post(self.url, payload, format="json")

        self.assertEqual(second.data["synced"], 0)
        self.assertEqual(second.data["duplicates"], 1)
        self.assertEqual(DisasterReport.objects.filter(citizen=self.citizen).count(), 1)

    def test_one_bad_record_does_not_block_the_others(self):
        self.client.force_authenticate(self.citizen)
        response = self.client.post(
            self.url,
            {
                "reports": [
                    report_payload(idempotency_key="good-1"),
                    report_payload(idempotency_key="bad-1", description="short"),
                    report_payload(idempotency_key="good-2"),
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_207_MULTI_STATUS)
        self.assertEqual(response.data["synced"], 2)
        self.assertEqual(response.data["failed"], 1)

    def test_sync_requires_an_idempotency_key(self):
        self.client.force_authenticate(self.citizen)
        response = self.client.post(self.url, {"reports": [report_payload()]}, format="json")
        self.assertEqual(response.data["failed"], 1)
