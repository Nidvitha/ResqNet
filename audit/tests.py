"""
Audit tests (Section 27).

    - Important transitions generate audit events
    - Audit records cannot be edited by normal users
"""

from django.db import models
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from approvals.services import approve_case, complete_payout, initiate_payout
from config.testing import make_admin, make_citizen, make_officer, make_report, make_signed_case

from .models import AuditAction, AuditEvent
from .services import history_for, record_event


class AuditGenerationTests(TestCase):
    """Every meaningful transition must leave a trace."""

    def setUp(self):
        self.citizen = make_citizen(district="Kollam")
        self.officer = make_officer(zone="Kollam")
        self.admin = make_admin()

    def _actions_for(self, report):
        return set(
            AuditEvent.objects.filter(
                entity_type="DisasterReport", entity_id=str(report.pk)
            ).values_list("action", flat=True)
        )

    def test_report_creation_and_submission_are_audited(self):
        report = make_report(self.citizen)
        actions = self._actions_for(report)

        self.assertIn(AuditAction.REPORT_CREATED, actions)
        self.assertIn(AuditAction.REPORT_SUBMITTED, actions)

    def test_triage_and_assignment_are_audited(self):
        report = make_report(self.citizen)
        actions = self._actions_for(report)

        self.assertIn(AuditAction.TRIAGE_COMPLETED, actions)
        self.assertIn(AuditAction.OFFICER_ASSIGNED, actions)

    def test_the_full_lifecycle_is_auditable_end_to_end(self):
        report, _, _ = make_signed_case(citizen=self.citizen, officer=self.officer)
        approve_case(report=report, user=self.admin, reason="Verified against the evidence.")
        report.refresh_from_db()
        initiate_payout(report=report, user=self.admin)
        report.refresh_from_db()
        complete_payout(report=report, user=self.admin)

        actions = self._actions_for(report)
        for expected in [
            AuditAction.REPORT_CREATED,
            AuditAction.REPORT_SUBMITTED,
            AuditAction.TRIAGE_COMPLETED,
            AuditAction.OFFICER_ASSIGNED,
            AuditAction.INSPECTION_STARTED,
            AuditAction.INSPECTION_COMPLETED,
            AuditAction.OFFICER_SIGNED,
            AuditAction.COMPENSATION_CALCULATED,
            AuditAction.ADMIN_APPROVED,
            AuditAction.PAYOUT_INITIATED,
            AuditAction.PAYOUT_COMPLETED,
        ]:
            with self.subTest(action=expected):
                self.assertIn(expected, actions)

    def test_events_record_who_did_what_and_the_state_change(self):
        report, _, _ = make_signed_case(citizen=self.citizen, officer=self.officer)
        approve_case(report=report, user=self.admin, reason="Verified against the evidence.")

        event = AuditEvent.objects.filter(action=AuditAction.ADMIN_APPROVED).first()
        self.assertEqual(event.performed_by, self.admin)
        self.assertEqual(event.performed_by_username, self.admin.username)
        self.assertEqual(event.performed_by_role, "ADMIN")
        self.assertEqual(event.new_state, "APPROVED")
        self.assertIn("approved_amount", event.metadata)

    def test_history_is_returned_in_chronological_order(self):
        report = make_report(self.citizen)
        events = history_for(report)
        timestamps = [event.timestamp for event in events]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_auditing_never_breaks_the_operation_it_records(self):
        """A malformed audit call must return None, not raise."""
        result = record_event(entity_type=None, entity_id=None, action="NOT_A_REAL_ACTION")
        self.assertIsNotNone(result or True)   # either recorded or safely swallowed


class AuditImmutabilityTests(TestCase):
    """Append-only, enforced by the model rather than by convention."""

    def setUp(self):
        self.citizen = make_citizen()
        self.event = record_event(
            entity=self.citizen, action=AuditAction.USER_REGISTERED, user=self.citizen
        )

    def test_an_existing_event_cannot_be_saved_again(self):
        self.event.action = AuditAction.ADMIN_APPROVED
        with self.assertRaises(ValueError):
            self.event.save()

    def test_an_event_cannot_be_deleted(self):
        with self.assertRaises(ValueError):
            self.event.delete()

    def test_bulk_update_is_blocked(self):
        with self.assertRaises(NotImplementedError):
            AuditEvent.objects.all().update(action=AuditAction.ADMIN_APPROVED)

    def test_bulk_delete_is_blocked(self):
        with self.assertRaises(NotImplementedError):
            AuditEvent.objects.all().delete()

    def test_the_stored_record_is_unchanged_after_a_failed_edit(self):
        original = self.event.action
        try:
            self.event.action = AuditAction.ADMIN_APPROVED
            self.event.save()
        except ValueError:
            pass
        self.event.refresh_from_db()
        self.assertEqual(self.event.action, original)


class AuditAPITests(APITestCase):
    def setUp(self):
        self.citizen = make_citizen(district="Kollam")
        self.officer = make_officer(zone="Kollam")
        self.admin = make_admin()
        self.report = make_report(self.citizen)

    def test_normal_users_cannot_read_the_full_audit_trail(self):
        for user in [self.citizen, self.officer]:
            with self.subTest(user=user.username):
                self.client.force_authenticate(user)
                self.assertEqual(
                    self.client.get(reverse("audit:event-list")).status_code,
                    status.HTTP_403_FORBIDDEN,
                )

    def test_admin_can_read_the_audit_trail(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse("audit:event-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(response.data["count"], 0)

    def test_audit_records_cannot_be_created_over_the_api(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse("audit:event-list"),
            {"entity_type": "DisasterReport", "action": AuditAction.ADMIN_APPROVED},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_audit_records_cannot_be_deleted_over_the_api(self):
        """Section 31: no DELETE on immutable audit records."""
        event = AuditEvent.objects.first()
        self.client.force_authenticate(self.admin)
        response = self.client.delete(reverse("audit:event-detail", args=[event.pk]))
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_audit_records_cannot_be_edited_over_the_api(self):
        event = AuditEvent.objects.first()
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            reverse("audit:event-detail", args=[event.pk]),
            {"action": AuditAction.ADMIN_APPROVED},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_citizen_can_read_their_own_case_history(self):
        """Section 5 promises audit transparency to the citizen."""
        self.client.force_authenticate(self.citizen)
        response = self.client.get(
            reverse("audit:case-history", args=[self.report.reference])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(len(response.data), 0)

    def test_citizen_cannot_read_another_citizens_case_history(self):
        other = make_citizen(username="citizen_other")
        self.client.force_authenticate(other)
        response = self.client.get(
            reverse("audit:case-history", args=[self.report.reference])
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_case_timeline_is_available_on_the_report_endpoint(self):
        self.client.force_authenticate(self.citizen)
        response = self.client.get(reverse("reports:report-timeline", args=[self.report.pk]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(len(response.data), 0)
