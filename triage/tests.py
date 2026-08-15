"""
Triage tests (Section 27).

    - Correct score generated
    - Correct severity generated
    - Critical conditions handled

The scoring function is pure, so most of these need no database rows beyond the
rule table - they pass a dictionary in and assert on the number that comes out.
"""

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from config.testing import make_admin, make_citizen, make_officer, make_report
from reports.models import TriageLevel
from reports.states import CaseStatus

from .models import TriageResult, TriageRule, TriageThreshold
from .services import calculate_score, classify, ensure_default_configuration, preview_score, run_triage


class ScoringTests(TestCase):
    def setUp(self):
        ensure_default_configuration()

    def test_default_rules_match_the_specification(self):
        """Section 14 gives these weights explicitly."""
        expected = {
            "people_trapped": 50,
            "roof_collapsed": 40,
            "exposed_live_wires": 30,
            "major_structural_damage": 30,
            "standing_water": 20,
            "road_blocked": 15,
        }
        for indicator, points in expected.items():
            with self.subTest(indicator=indicator):
                self.assertEqual(TriageRule.objects.get(indicator=indicator).points, points)

    def test_no_indicators_scores_zero(self):
        score, breakdown = calculate_score({})
        self.assertEqual(score, 0)
        self.assertEqual(breakdown, [])

    def test_single_indicator_scores_its_weight(self):
        score, _ = calculate_score({"roof_collapsed": True})
        self.assertEqual(score, 40)

    def test_indicators_add_up(self):
        """Roof collapsed (40) + people trapped (50) + road blocked (15) = 105."""
        score, breakdown = calculate_score(
            {"roof_collapsed": True, "people_trapped": True, "road_blocked": True}
        )
        self.assertEqual(score, 105)
        self.assertEqual(len(breakdown), 3)

    def test_per_unit_rule_multiplies_and_is_capped(self):
        uncapped, _ = calculate_score({"people_affected": 5})     # 5 x 2
        self.assertEqual(uncapped, 10)

        capped, _ = calculate_score({"people_affected": 100})     # would be 200
        self.assertEqual(capped, 30)                              # ceiling is 30

    def test_breakdown_explains_every_point(self):
        score, breakdown = calculate_score({"roof_collapsed": True, "standing_water": True})
        self.assertEqual(sum(item["points"] for item in breakdown), score)
        for item in breakdown:
            self.assertIn("label", item)
            self.assertIn("indicator", item)

    def test_deactivated_rule_stops_contributing(self):
        TriageRule.objects.filter(indicator="roof_collapsed").update(is_active=False)
        score, _ = calculate_score({"roof_collapsed": True})
        self.assertEqual(score, 0)

    def test_retuned_weight_changes_the_score(self):
        """The point of database-held rules: no code change required."""
        TriageRule.objects.filter(indicator="roof_collapsed").update(points=99)
        score, _ = calculate_score({"roof_collapsed": True})
        self.assertEqual(score, 99)


class ClassificationTests(TestCase):
    """Section 14 bands: 0-39 LOW, 40-69 MEDIUM, 70-99 HIGH, 100+ CRITICAL."""

    def setUp(self):
        ensure_default_configuration()

    def test_band_boundaries(self):
        cases = [
            (0, TriageLevel.LOW), (39, TriageLevel.LOW),
            (40, TriageLevel.MEDIUM), (69, TriageLevel.MEDIUM),
            (70, TriageLevel.HIGH), (99, TriageLevel.HIGH),
            (100, TriageLevel.CRITICAL), (250, TriageLevel.CRITICAL),
        ]
        for score, expected in cases:
            with self.subTest(score=score):
                self.assertEqual(classify(score).level, expected)

    def test_critical_conditions_are_handled(self):
        """People trapped (50) + roof collapsed (40) + live wires (30) = 120 -> CRITICAL."""
        score, _ = calculate_score(
            {"people_trapped": True, "roof_collapsed": True, "exposed_live_wires": True}
        )
        self.assertGreaterEqual(score, 100)
        self.assertEqual(classify(score).level, TriageLevel.CRITICAL)

    def test_critical_cases_get_the_shortest_response_target(self):
        critical = TriageThreshold.objects.get(level=TriageLevel.CRITICAL)
        low = TriageThreshold.objects.get(level=TriageLevel.LOW)
        self.assertLess(critical.response_target_hours, low.response_target_hours)


class TriageEngineTests(TestCase):
    def setUp(self):
        self.citizen = make_citizen()
        self.officer = make_officer()

    def test_running_triage_stores_a_result_and_advances_the_case(self):
        report = make_report(self.citizen, submit=True)
        result = TriageResult.objects.get(report=report)

        self.assertGreater(result.score, 0)
        self.assertEqual(report.priority_score, result.score)
        self.assertEqual(report.triage_level, result.level)
        # Triage moves SUBMITTED -> TRIAGED, then dispatch moves it to ASSIGNED.
        self.assertEqual(report.status, CaseStatus.ASSIGNED)

    def test_triage_writes_an_audit_event(self):
        from audit.models import AuditAction, AuditEvent

        report = make_report(self.citizen)
        self.assertTrue(
            AuditEvent.objects.filter(
                entity_type="DisasterReport",
                entity_id=str(report.pk),
                action=AuditAction.TRIAGE_COMPLETED,
            ).exists()
        )

    def test_recalculation_updates_rather_than_duplicates(self):
        report = make_report(self.citizen)
        TriageRule.objects.filter(indicator="standing_water").update(points=90)

        run_triage(report)
        report.refresh_from_db()

        self.assertEqual(TriageResult.objects.filter(report=report).count(), 1)
        self.assertIsNotNone(report.triage_result.recalculated_at)

    def test_preview_does_not_write_anything(self):
        before = TriageResult.objects.count()
        preview = preview_score({"roof_collapsed": True, "people_trapped": True})
        self.assertEqual(preview["score"], 90)
        self.assertEqual(preview["level"], TriageLevel.HIGH)
        self.assertEqual(TriageResult.objects.count(), before)


class TriageAPITests(APITestCase):
    def setUp(self):
        self.citizen = make_citizen()
        self.admin = make_admin()

    def test_citizen_can_preview_severity_while_filling_the_form(self):
        self.client.force_authenticate(self.citizen)
        response = self.client.post(
            reverse("reports:triage-preview"),
            {"people_trapped": True, "roof_collapsed": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["score"], 90)

    def test_only_admin_can_retune_the_rules(self):
        ensure_default_configuration()
        rule = TriageRule.objects.first()
        url = reverse("triage:rule-detail", args=[rule.pk])

        self.client.force_authenticate(self.citizen)
        self.assertEqual(
            self.client.patch(url, {"points": 5}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )

        self.client.force_authenticate(self.admin)
        self.assertEqual(
            self.client.patch(url, {"points": 5}, format="json").status_code,
            status.HTTP_200_OK,
        )

    def test_triage_results_are_read_only_over_the_api(self):
        """A client must never be able to set its own priority score."""
        self.client.force_authenticate(self.admin)
        response = self.client.post(reverse("triage:result-list"), {"score": 999}, format="json")
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
