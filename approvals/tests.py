"""
Approval and payout tests (Section 27).

    - Uninspected case cannot be approved
    - Approved case moves correctly
    - Rejected case follows correct workflow
"""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from compensation.models import CompensationClaim
from config.testing import make_admin, make_citizen, make_officer, make_report, make_signed_case
from reports.states import CaseStatus, InvalidTransition

from .models import Approval, ApprovalDecision, Payout, PayoutStatus
from .services import approve_case, complete_payout, initiate_payout, reject_case, request_review


class ApprovalGuardTests(TestCase):
    """The rules Section 6 states as non-negotiable."""

    def setUp(self):
        self.citizen = make_citizen(district="Kollam")
        self.officer = make_officer(zone="Kollam")
        self.admin = make_admin()

    def test_uninspected_case_cannot_be_approved(self):
        report = make_report(self.citizen)          # assigned, never inspected
        with self.assertRaises(InvalidTransition):
            approve_case(report=report, user=self.admin, reason="Looks fine to me, approving.")

    def test_unsigned_inspection_cannot_be_approved(self):
        from inspections.services import complete_inspection, start_inspection, update_inspection

        report = make_report(self.citizen)
        assignment = report.assignments.first()
        inspection = start_inspection(assignment=assignment, user=assignment.officer)
        update_inspection(
            inspection=inspection, data={"roof_damage": "SEVERE"}, user=assignment.officer
        )
        complete_inspection(inspection=inspection, user=assignment.officer)

        with self.assertRaises(InvalidTransition):
            approve_case(report=report, user=self.admin, reason="Approving before sign-off.")

    def test_approval_requires_a_written_reason(self):
        report, _, _ = make_signed_case(citizen=self.citizen, officer=self.officer)
        with self.assertRaises(InvalidTransition):
            approve_case(report=report, user=self.admin, reason="   ")

    def test_already_approved_case_cannot_be_approved_again(self):
        report, _, _ = make_signed_case(citizen=self.citizen, officer=self.officer)
        approve_case(report=report, user=self.admin, reason="Verified and within policy.")
        report.refresh_from_db()

        with self.assertRaises(InvalidTransition):
            approve_case(report=report, user=self.admin, reason="Approving a second time.")


class ApprovalFlowTests(TestCase):
    def setUp(self):
        self.citizen = make_citizen(district="Kollam")
        self.officer = make_officer(zone="Kollam")
        self.admin = make_admin()
        self.report, self.inspection, _ = make_signed_case(
            citizen=self.citizen, officer=self.officer
        )

    def test_approved_case_moves_correctly(self):
        approval = approve_case(
            report=self.report, user=self.admin, reason="Damage verified and within policy limits."
        )
        self.report.refresh_from_db()

        self.assertEqual(approval.decision, ApprovalDecision.APPROVED)
        # Approval opens a payout, so the case lands on PAYOUT_PENDING.
        self.assertEqual(self.report.status, CaseStatus.PAYOUT_PENDING)
        self.assertTrue(Payout.objects.filter(report=self.report).exists())

    def test_approval_records_the_authorised_amount(self):
        claim = CompensationClaim.objects.get(report=self.report)
        approval = approve_case(
            report=self.report, user=self.admin, reason="Damage verified and within policy limits."
        )
        self.assertEqual(approval.approved_amount, claim.calculated_amount)

    def test_approval_with_an_adjusted_amount(self):
        approve_case(
            report=self.report,
            user=self.admin,
            reason="Partial award: only the roof damage is supported by the evidence.",
            adjusted_amount=Decimal("30000"),
        )
        claim = CompensationClaim.objects.get(report=self.report)

        self.assertEqual(claim.approved_amount, Decimal("30000.00"))
        self.assertTrue(claim.was_adjusted)
        self.assertNotEqual(claim.calculated_amount, Decimal("30000.00"))

    def test_rejected_case_follows_the_correct_workflow(self):
        approval = reject_case(
            report=self.report,
            user=self.admin,
            reason="The reported property falls outside the declared disaster zone.",
        )
        self.report.refresh_from_db()

        self.assertEqual(approval.decision, ApprovalDecision.REJECTED)
        self.assertEqual(self.report.status, CaseStatus.REJECTED)
        self.assertFalse(Payout.objects.filter(report=self.report).exists())

    def test_rejected_case_can_be_sent_for_review(self):
        """Section 6 names this recovery path explicitly."""
        reject_case(
            report=self.report, user=self.admin, reason="Insufficient photographic evidence."
        )
        self.report.refresh_from_db()

        request_review(
            report=self.report, user=self.admin, reason="Citizen supplied further photographs."
        )
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, CaseStatus.NEEDS_REVIEW)

    def test_case_under_review_can_still_be_approved(self):
        request_review(
            report=self.report, user=self.admin, reason="Please re-check the electrical damage."
        )
        self.report.refresh_from_db()

        approve_case(
            report=self.report, user=self.admin, reason="Re-inspection confirmed the assessment."
        )
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, CaseStatus.PAYOUT_PENDING)

    def test_every_decision_is_recorded_in_order(self):
        reject_case(report=self.report, user=self.admin, reason="Missing ownership documents.")
        self.report.refresh_from_db()
        request_review(report=self.report, user=self.admin, reason="Documents now provided.")
        self.report.refresh_from_db()
        approve_case(report=self.report, user=self.admin, reason="Documents verified; approving.")

        decisions = list(
            Approval.objects.filter(report=self.report).order_by("decided_at").values_list("decision", flat=True)
        )
        self.assertEqual(
            decisions,
            [
                ApprovalDecision.REJECTED,
                ApprovalDecision.REVIEW_REQUESTED,
                ApprovalDecision.APPROVED,
            ],
        )


class PayoutTests(TestCase):
    def setUp(self):
        self.citizen = make_citizen(district="Kollam")
        self.officer = make_officer(zone="Kollam")
        self.admin = make_admin()
        self.report, _, _ = make_signed_case(citizen=self.citizen, officer=self.officer)
        approve_case(report=self.report, user=self.admin, reason="Verified and within policy.")
        self.report.refresh_from_db()

    def test_payout_cannot_complete_before_being_initiated(self):
        """Section 6: "Payout cannot be completed before approval." """
        with self.assertRaises(InvalidTransition):
            complete_payout(report=self.report, user=self.admin)

    def test_full_payout_sequence(self):
        initiate_payout(report=self.report, user=self.admin)
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, CaseStatus.PAYOUT_INITIATED)

        payout = complete_payout(report=self.report, user=self.admin)
        self.report.refresh_from_db()

        self.assertEqual(payout.status, PayoutStatus.COMPLETED)
        self.assertEqual(self.report.status, CaseStatus.PAYOUT_COMPLETED)
        self.assertIsNotNone(self.report.closed_at)

    def test_payout_cannot_be_initiated_twice(self):
        initiate_payout(report=self.report, user=self.admin)
        with self.assertRaises(InvalidTransition):
            initiate_payout(report=self.report, user=self.admin)

    def test_payout_is_labelled_as_simulated(self):
        """Section 19 - never imply a real banking integration."""
        payout = Payout.objects.get(report=self.report)
        self.assertTrue(payout.is_simulated)
        self.assertTrue(payout.reference_number.startswith("PAY-"))


class ApprovalAPITests(APITestCase):
    def setUp(self):
        self.citizen = make_citizen(district="Kollam")
        self.officer = make_officer(zone="Kollam")
        self.admin = make_admin()
        self.report, _, _ = make_signed_case(citizen=self.citizen, officer=self.officer)

    def test_citizen_cannot_approve_their_own_case(self):
        self.client.force_authenticate(self.citizen)
        response = self.client.post(
            reverse("approvals:approve", args=[self.report.pk]),
            {"reason": "I would like my own relief approved please."},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_officer_cannot_approve(self):
        self.client.force_authenticate(self.officer)
        response = self.client.post(
            reverse("approvals:approve", args=[self.report.pk]),
            {"reason": "Approving the case I just inspected myself."},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_approves_through_the_api(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse("approvals:approve", args=[self.report.pk]),
            {"reason": "Damage verified against the inspection photographs."},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_approval_without_a_reason_is_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse("approvals:approve", args=[self.report.pk]), {"reason": "ok"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_review_queue_lists_cases_awaiting_a_decision(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse("approvals:queue"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        references = [row["reference"] for row in response.data]
        self.assertIn(self.report.reference, references)

    def test_citizen_can_track_their_own_payout(self):
        from .services import approve_case as approve

        approve(report=self.report, user=self.admin, reason="Verified and within policy.")
        self.client.force_authenticate(self.citizen)
        response = self.client.get(reverse("approvals:payout-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_citizen_cannot_see_another_citizens_payout(self):
        from .services import approve_case as approve

        approve(report=self.report, user=self.admin, reason="Verified and within policy.")
        self.client.force_authenticate(make_citizen(username="citizen_other"))
        response = self.client.get(reverse("approvals:payout-list"))
        self.assertEqual(response.data["count"], 0)
