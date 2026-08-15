"""
Frontend smoke tests (Phases 9-12).

These do not test JavaScript behaviour - they answer a narrower but essential
question: does every page actually render, and is each portal closed to the roles
that should not reach it?

A template with a typo in a `{% url %}` tag raises at render time, not at import
time, so without these a broken page would only be found by clicking it.
"""

from django.test import TestCase
from django.urls import reverse

from config.testing import make_admin, make_citizen, make_officer


class PublicPageTests(TestCase):
    """Section 5 - the home page and its supporting pages need no login."""

    def test_public_pages_render_without_authentication(self):
        for name in ["web:home", "web:about", "web:privacy", "web:terms",
                     "web:contact", "web:offline", "web:login", "web:register"]:
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)

    def test_home_page_explains_the_platform(self):
        response = self.client.get(reverse("web:home"))
        content = response.content.decode()

        self.assertIn("Report. Respond. Recover.", content)
        self.assertIn("Report damage", content)      # primary CTA
        self.assertIn("How ResQNet works", content)  # process explanation
        self.assertIn("Citizen portal", content)     # user access routes
        self.assertIn("Officer portal", content)
        self.assertIn("Admin portal", content)

    def test_home_page_shows_the_footer_sections(self):
        content = self.client.get(reverse("web:home")).content.decode()
        for expected in ["About", "Contact", "Privacy", "Terms", "Emergency information"]:
            with self.subTest(section=expected):
                self.assertIn(expected, content)

    def test_home_page_does_not_claim_emergency_integration(self):
        """Section 5 forbids implying a real emergency-service integration."""
        content = self.client.get(reverse("web:home")).content.decode()
        self.assertIn("<em>not</em> connected to", content)
        self.assertIn("does not summon help", content)
        self.assertIn("call your national emergency number", content)

    def test_payout_simulation_is_disclosed(self):
        """Section 19 - never imply real financial integration."""
        content = self.client.get(reverse("web:home")).content.decode()
        self.assertIn("simulated", content.lower())

    def test_home_page_renders_with_statistics(self):
        make_citizen()
        make_officer()
        response = self.client.get(reverse("web:home"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("total_reports", str(response.context["stats"]))


class ServiceWorkerTests(TestCase):
    def test_service_worker_is_served_from_the_root(self):
        response = self.client.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/javascript")
        # Without this header the browser refuses root scope.
        self.assertEqual(response["Service-Worker-Allowed"], "/")


class PortalAccessTests(TestCase):
    """Portal pages require a login; the API enforces the role boundaries."""

    def setUp(self):
        self.citizen = make_citizen()
        self.officer = make_officer()
        self.admin = make_admin()

    def test_portal_pages_redirect_anonymous_visitors_to_login(self):
        for name in ["web:citizen_dashboard", "web:citizen_reports",
                     "web:officer_dashboard", "web:officer_map",
                     "web:admin_dashboard", "web:admin_cases",
                     "web:admin_officers", "web:admin_heatmap", "web:admin_audit"]:
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login/", response.url)

    def test_citizen_pages_render(self):
        self.client.force_login(self.citizen)
        for name in ["web:citizen_dashboard", "web:citizen_reports", "web:citizen_report_form"]:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_officer_pages_render(self):
        self.client.force_login(self.officer)
        for name in ["web:officer_dashboard", "web:officer_map"]:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_admin_pages_render(self):
        self.client.force_login(self.admin)
        for name in ["web:admin_dashboard", "web:admin_cases",
                     "web:admin_officers", "web:admin_heatmap", "web:admin_audit"]:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_detail_pages_render(self):
        """These take a reference in the URL and fetch their data client-side."""
        self.client.force_login(self.citizen)
        self.assertEqual(
            self.client.get(reverse("web:citizen_report_detail", args=["RQN-2026-000001"])).status_code,
            200,
        )
        self.client.force_login(self.officer)
        self.assertEqual(
            self.client.get(reverse("web:officer_inspection", args=[1])).status_code, 200
        )
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get(reverse("web:admin_case_detail", args=["RQN-2026-000001"])).status_code,
            200,
        )

    def test_each_role_lands_on_its_own_portal(self):
        cases = [
            (self.citizen, "/citizen/"),
            (self.officer, "/officer/"),
            (self.admin, "/command/"),
        ]
        for user, destination in cases:
            with self.subTest(role=user.role):
                self.client.force_login(user)
                response = self.client.get(reverse("web:portal"))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, destination)

    def test_navigation_shows_role_appropriate_links(self):
        self.client.force_login(self.citizen)
        content = self.client.get(reverse("web:citizen_dashboard")).content.decode()
        self.assertIn("My reports", content)
        self.assertNotIn("Command centre", content)

        self.client.force_login(self.admin)
        content = self.client.get(reverse("web:admin_dashboard")).content.decode()
        self.assertIn("Audit", content)
