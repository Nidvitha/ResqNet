"""
Compensation tests (Section 27).

    - Correct calculation
    - Correct itemization
    - Invalid data rejected

The worked example from Section 17 is tested literally:

    Roof Damage    Rs 80,000
    Wall Damage    Rs 40,000
    Electrical     Rs 15,000
    -------------------------
    Total          Rs 1,35,000
"""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from config.testing import make_admin, make_citizen, make_officer, make_signed_case
from inspections.models import Inspection, InspectionStatus
from reports.states import CaseStatus, InvalidTransition

from .models import CompensationClaim, CompensationStatus, ReliefRule
from .services import adjust_claim, calculate_compensation, compute_breakdown, ensure_default_matrix


class ReliefMatrixTests(TestCase):
    def setUp(self):
        ensure_default_matrix()

    def test_default_amounts_match_the_specification(self):
        expected = {
            "roof_damage": Decimal("80000"),
            "wall_damage": Decimal("40000"),
            "electrical_damage": Decimal("15000"),
            "household_damage": Decimal("20000"),
        }
        for field, amount in expected.items():
            with self.subTest(field=field):
                self.assertEqual(ReliefRule.objects.get(damage_field=field).base_amount, amount)


class CalculationTests(TestCase):
    """The engine itself, driven through signed inspections."""

    def setUp(self):
        ensure_default_matrix()
        self.citizen = make_citizen(district="Kollam")
        self.officer = make_officer(zone="Kollam")

    def test_section_17_worked_example(self):
        """Three damage types, all DESTROYED (factor 1.0), no allowances."""
        _, inspection, _ = make_signed_case(
            citizen=self.citizen,
            officer=self.officer,
            roof_damage="DESTROYED",
            wall_damage="DESTROYED",
            electrical_damage="DESTROYED",
            household_damage="NONE",
            people_affected=0,
            is_habitable=True,
            requires_immediate_relief=False,
        )
        total, items, _ = compute_breakdown(inspection)

        self.assertEqual(total, Decimal("135000.00"))
        self.assertEqual(len(items), 3)

    def test_severity_scales_the_amount(self):
        """Section 17's amounts are the DESTROYED figure; lesser grades pay less."""
        _, inspection, _ = make_signed_case(
            citizen=self.citizen, officer=self.officer,
            roof_damage="SEVERE",           # 80000 x 0.80 = 64000
            wall_damage="NONE",
            people_affected=0, is_habitable=True,
        )
        total, items, _ = compute_breakdown(inspection)
        self.assertEqual(total, Decimal("64000.00"))
        self.assertEqual(items[0]["amount"], Decimal("64000.00"))

    def test_undamaged_categories_contribute_nothing(self):
        _, inspection, _ = make_signed_case(
            citizen=self.citizen, officer=self.officer,
            roof_damage="NONE", wall_damage="NONE", people_affected=0,
            is_habitable=True,
            remarks="Site attended. No qualifying damage found.",
        )
        total, items, _ = compute_breakdown(inspection)
        self.assertEqual(total, Decimal("0.00"))
        self.assertEqual(items, [])

    def test_per_person_allowance_is_capped(self):
        _, inspection, _ = make_signed_case(
            citizen=self.citizen, officer=self.officer,
            roof_damage="NONE", wall_damage="NONE",
            people_affected=100,           # 100 x 5000 = 500000, capped at 50000
            is_habitable=True,
        )
        total, _, _ = compute_breakdown(inspection)
        self.assertEqual(total, Decimal("50000.00"))

    def test_uninhabitable_property_adds_accommodation_support(self):
        _, habitable, _ = make_signed_case(
            citizen=self.citizen, officer=self.officer,
            roof_damage="MINOR", wall_damage="NONE", people_affected=0, is_habitable=True,
        )
        habitable_total, _, _ = compute_breakdown(habitable)

        other_citizen = make_citizen(username="citizen_two", district="Kollam")
        _, uninhabitable, _ = make_signed_case(
            citizen=other_citizen, officer=self.officer,
            roof_damage="MINOR", wall_damage="NONE", people_affected=0, is_habitable=False,
        )
        uninhabitable_total, _, _ = compute_breakdown(uninhabitable)

        self.assertEqual(uninhabitable_total - habitable_total, Decimal("25000.00"))

    def test_calculation_is_deterministic(self):
        """Same inputs, same answer - every time."""
        _, inspection, _ = make_signed_case(citizen=self.citizen, officer=self.officer)
        first, _, _ = compute_breakdown(inspection)
        second, _, _ = compute_breakdown(inspection)
        self.assertEqual(first, second)

    def test_retuned_matrix_changes_the_result(self):
        ReliefRule.objects.filter(damage_field="roof_damage").update(base_amount=Decimal("100000"))
        _, inspection, _ = make_signed_case(
            citizen=self.citizen, officer=self.officer,
            roof_damage="DESTROYED", wall_damage="NONE", people_affected=0, is_habitable=True,
        )
        total, _, _ = compute_breakdown(inspection)
        self.assertEqual(total, Decimal("100000.00"))


class ItemizationTests(TestCase):
    def setUp(self):
        self.citizen = make_citizen(district="Kollam")
        self.officer = make_officer(zone="Kollam")

    def test_claim_stores_a_line_for_every_contribution(self):
        report, inspection, _ = make_signed_case(citizen=self.citizen, officer=self.officer)
        claim = CompensationClaim.objects.get(report=report)

        self.assertGreater(claim.items.count(), 0)
        self.assertEqual(
            sum(item.amount for item in claim.items.all()), claim.calculated_amount
        )

    def test_each_line_explains_its_own_arithmetic(self):
        report, _, _ = make_signed_case(
            citizen=self.citizen, officer=self.officer, roof_damage="SEVERE"
        )
        claim = CompensationClaim.objects.get(report=report)
        roof_line = claim.items.get(code="roof_damage")

        self.assertEqual(roof_line.severity, "SEVERE")
        self.assertEqual(roof_line.base_amount, Decimal("80000.00"))
        self.assertEqual(roof_line.severity_factor, Decimal("0.80"))
        self.assertIn("Severe", roof_line.calculation_note)

    def test_rules_snapshot_preserves_the_amounts_in_force(self):
        """So a claim from six months ago is still interpretable after retuning."""
        report, _, _ = make_signed_case(citizen=self.citizen, officer=self.officer)
        claim = CompensationClaim.objects.get(report=report)
        self.assertIn("roof_damage", claim.rules_snapshot)

    def test_recalculation_replaces_rather_than_appends(self):
        report, inspection, _ = make_signed_case(citizen=self.citizen, officer=self.officer)
        claim = CompensationClaim.objects.get(report=report)
        original_count = claim.items.count()

        calculate_compensation(inspection=inspection)
        claim.refresh_from_db()
        self.assertEqual(claim.items.count(), original_count)
        self.assertEqual(CompensationClaim.objects.filter(report=report).count(), 1)


class InvalidDataTests(TestCase):
    def setUp(self):
        self.citizen = make_citizen(district="Kollam")
        self.officer = make_officer(zone="Kollam")

    def test_unsigned_inspection_cannot_produce_a_claim(self):
        """Relief must never be costed from an unverified assessment."""
        from config.testing import make_report
        from inspections.services import start_inspection

        report = make_report(self.citizen)
        inspection = start_inspection(
            assignment=report.assignments.first(), user=report.assignments.first().officer
        )
        with self.assertRaises(InvalidTransition):
            calculate_compensation(inspection=inspection)

    def test_adjustment_requires_a_reason(self):
        report, _, _ = make_signed_case(citizen=self.citizen, officer=self.officer)
        claim = CompensationClaim.objects.get(report=report)
        admin = make_admin()

        with self.assertRaises(InvalidTransition):
            adjust_claim(claim=claim, amount=Decimal("50000"), reason="  ", user=admin)

    def test_negative_adjustment_rejected(self):
        report, _, _ = make_signed_case(citizen=self.citizen, officer=self.officer)
        claim = CompensationClaim.objects.get(report=report)
        admin = make_admin()

        with self.assertRaises(InvalidTransition):
            adjust_claim(
                claim=claim, amount=Decimal("-100"),
                reason="Attempting a negative payment.", user=admin,
            )

    def test_adjustment_preserves_the_original_calculation(self):
        report, _, _ = make_signed_case(citizen=self.citizen, officer=self.officer)
        claim = CompensationClaim.objects.get(report=report)
        original = claim.calculated_amount
        admin = make_admin()

        adjust_claim(
            claim=claim, amount=Decimal("50000"),
            reason="Partial award pending documentary proof of ownership.", user=admin,
        )
        claim.refresh_from_db()

        self.assertEqual(claim.calculated_amount, original)
        self.assertEqual(claim.approved_amount, Decimal("50000.00"))
        self.assertTrue(claim.was_adjusted)
        self.assertEqual(claim.status, CompensationStatus.ADJUSTED)


class CompensationAPITests(APITestCase):
    def setUp(self):
        self.citizen = make_citizen(district="Kollam")
        self.officer = make_officer(zone="Kollam")
        self.admin = make_admin()
        self.report, _, _ = make_signed_case(citizen=self.citizen, officer=self.officer)
        self.claim = CompensationClaim.objects.get(report=self.report)

    def test_citizen_sees_their_own_breakdown(self):
        self.client.force_authenticate(self.citizen)
        response = self.client.get(
            reverse("compensation:claim-breakdown", args=[self.claim.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("lines", response.data)

    def test_citizen_cannot_see_another_citizens_claim(self):
        other = make_citizen(username="citizen_other")
        self.client.force_authenticate(other)
        response = self.client.get(
            reverse("compensation:claim-detail", args=[self.claim.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_claims_cannot_be_created_over_the_api(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse("compensation:claim-list"), {"calculated_amount": "999999"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_only_admin_can_adjust(self):
        payload = {"amount": "50000", "reason": "Partial award pending ownership documents."}
        url = reverse("compensation:claim-adjust", args=[self.claim.pk])

        self.client.force_authenticate(self.citizen)
        self.assertEqual(
            self.client.post(url, payload, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )

        self.client.force_authenticate(self.admin)
        self.assertEqual(
            self.client.post(url, payload, format="json").status_code, status.HTTP_200_OK
        )

    def test_short_adjustment_reason_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse("compensation:claim-adjust", args=[self.claim.pk]),
            {"amount": "50000", "reason": "no"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
