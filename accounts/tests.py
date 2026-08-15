"""
Authentication and authorisation tests (Section 27).

Covers the two checklists the specification names explicitly:

    Authentication          Authorization
    - Citizen login works   - Citizen cannot access admin APIs
    - Officer login works   - Officer cannot modify another officer's inspection
    - Admin login works     - Admin can review cases
    - Invalid login fails
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework.throttling import SimpleRateThrottle

from config.testing import DEFAULT_PASSWORD, make_admin, make_citizen, make_officer

from .models import Role

User = get_user_model()


class AuthenticationTests(APITestCase):
    """Every role can log in; bad credentials cannot."""

    def setUp(self):
        self.citizen = make_citizen()
        self.officer = make_officer()
        self.admin = make_admin()
        self.login_url = reverse("accounts:login")

    def test_citizen_login_works(self):
        response = self.client.post(
            self.login_url, {"username": "citizen1", "password": DEFAULT_PASSWORD}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user"]["role"], Role.CITIZEN)
        self.assertIn("token", response.data)

    def test_officer_login_works(self):
        response = self.client.post(
            self.login_url, {"username": "officer1", "password": DEFAULT_PASSWORD}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user"]["role"], Role.FIELD_OFFICER)

    def test_admin_login_works(self):
        response = self.client.post(
            self.login_url, {"username": "admin1", "password": DEFAULT_PASSWORD}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user"]["role"], Role.ADMIN)

    def test_invalid_login_fails(self):
        response = self.client.post(
            self.login_url, {"username": "citizen1", "password": "wrong-password"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_login_does_not_reveal_whether_username_exists(self):
        """Both failure modes must produce the same message, or the endpoint
        becomes a username-enumeration oracle."""
        missing = self.client.post(
            self.login_url, {"username": "nobody", "password": "whatever123"}
        )
        wrong = self.client.post(
            self.login_url, {"username": "citizen1", "password": "whatever123"}
        )
        self.assertEqual(str(missing.data), str(wrong.data))

    def test_password_is_hashed_never_stored_in_plaintext(self):
        self.assertNotEqual(self.citizen.password, DEFAULT_PASSWORD)
        self.assertTrue(self.citizen.password.startswith("pbkdf2_"))
        self.assertTrue(self.citizen.check_password(DEFAULT_PASSWORD))

    def test_inactive_account_cannot_log_in(self):
        self.citizen.is_active = False
        self.citizen.save()
        response = self.client.post(
            self.login_url, {"username": "citizen1", "password": DEFAULT_PASSWORD}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class RegistrationTests(APITestCase):
    def setUp(self):
        self.url = reverse("accounts:register")

    def test_citizen_can_register(self):
        response = self.client.post(
            self.url,
            {
                "username": "newcitizen",
                "email": "new@example.com",
                "first_name": "New",
                "last_name": "Citizen",
                "district": "Kollam",
                "password": DEFAULT_PASSWORD,
                "password_confirm": DEFAULT_PASSWORD,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(User.objects.get(username="newcitizen").role, Role.CITIZEN)

    def test_registration_signs_the_new_user_in(self):
        """
        Regression: registration returned 201 but created no session, so the
        browser redirected to the citizen dashboard, hit @login_required, and
        bounced straight back to the login page — indistinguishable from failure.
        """
        response = self.client.post(
            self.url,
            {
                "username": "freshuser",
                "email": "fresh@example.com",
                "first_name": "Fresh",
                "password": DEFAULT_PASSWORD,
                "password_confirm": DEFAULT_PASSWORD,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("sessionid", response.cookies)

        # The dashboard must now load rather than redirect to /login/.
        dashboard = self.client.get(reverse("web:citizen_dashboard"))
        self.assertEqual(dashboard.status_code, 200)

    def test_registration_cannot_self_assign_a_privileged_role(self):
        """Posting role=ADMIN must be ignored - the field is not in the serializer."""
        self.client.post(
            self.url,
            {
                "username": "sneaky",
                "email": "sneaky@example.com",
                "first_name": "Sneaky",
                "role": "ADMIN",
                "password": DEFAULT_PASSWORD,
                "password_confirm": DEFAULT_PASSWORD,
            },
        )
        self.assertEqual(User.objects.get(username="sneaky").role, Role.CITIZEN)

    def test_mismatched_passwords_rejected(self):
        response = self.client.post(
            self.url,
            {
                "username": "mismatch",
                "email": "mismatch@example.com",
                "first_name": "Mis",
                "password": DEFAULT_PASSWORD,
                "password_confirm": "SomethingElse123!",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password_confirm", response.data)

    def test_weak_password_rejected(self):
        response = self.client.post(
            self.url,
            {
                "username": "weakuser",
                "email": "weak@example.com",
                "first_name": "Weak",
                "password": "1234",
                "password_confirm": "1234",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_email_rejected(self):
        make_citizen(username="existing")
        response = self.client.post(
            self.url,
            {
                "username": "another",
                "email": "existing@example.com",
                "first_name": "Another",
                "password": DEFAULT_PASSWORD,
                "password_confirm": DEFAULT_PASSWORD,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class AuthorizationTests(APITestCase):
    """Role boundaries at the API surface."""

    def setUp(self):
        self.citizen = make_citizen()
        self.officer = make_officer()
        self.admin = make_admin()

    def test_citizen_cannot_access_admin_apis(self):
        self.client.force_authenticate(self.citizen)
        for url in [
            reverse("accounts:officer-list"),
            reverse("analytics:dashboard"),
            reverse("approvals:queue"),
            reverse("audit:event-list"),
        ]:
            with self.subTest(url=url):
                self.assertEqual(
                    self.client.get(url).status_code, status.HTTP_403_FORBIDDEN
                )

    def test_officer_cannot_access_admin_apis(self):
        self.client.force_authenticate(self.officer)
        self.assertEqual(
            self.client.get(reverse("analytics:dashboard")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_admin_can_reach_review_and_audit_apis(self):
        self.client.force_authenticate(self.admin)
        for url in [reverse("approvals:queue"), reverse("analytics:dashboard")]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)

    def test_unauthenticated_requests_are_refused(self):
        response = self.client.get(reverse("accounts:me"))
        self.assertIn(
            response.status_code,
            {status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN},
        )

    def test_health_check_is_public(self):
        """Monitoring must work without credentials."""
        response = self.client.get(reverse("health-check"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "ok")

    def test_only_admin_can_create_officers(self):
        payload = {
            "username": "officer_new",
            "email": "on@example.com",
            "first_name": "New",
            "last_name": "Officer",
            "password": DEFAULT_PASSWORD,
            "profile": {
                "employee_id": "EMP-NEW",
                "zone": "Kollam",
                "base_latitude": "8.8932",
                "base_longitude": "76.6141",
            },
        }
        self.client.force_authenticate(self.citizen)
        self.assertEqual(
            self.client.post(reverse("accounts:officer-list"), payload, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )

        self.client.force_authenticate(self.admin)
        response = self.client.post(reverse("accounts:officer-list"), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(User.objects.get(username="officer_new").role, Role.FIELD_OFFICER)


class ThrottlingTests(APITestCase):
    """
    Rate limiting is relaxed during the suite (see `settings.TESTING`), so it is
    verified here explicitly with the real limit switched back on.
    """

    def setUp(self):
        make_citizen()
        cache.clear()   # DRF keeps throttle history in the cache between tests

    def tearDown(self):
        cache.clear()

    def test_repeated_failed_logins_are_throttled(self):
        """
        DRF binds `THROTTLE_RATES` as a class attribute when `rest_framework.
        throttling` is first imported, so `override_settings` cannot reach it.
        Patching the dict itself is what actually exercises the limiter.
        """
        url = reverse("accounts:login")
        payload = {"username": "citizen1", "password": "wrong-password"}

        with patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"login": "3/min"}):
            statuses = [self.client.post(url, payload).status_code for _ in range(6)]

        self.assertIn(status.HTTP_429_TOO_MANY_REQUESTS, statuses)
        self.assertEqual(statuses[0], status.HTTP_400_BAD_REQUEST)

    def test_successful_logins_are_never_throttled(self):
        """
        The regression this guards: counting every request meant a correct
        password consumed quota, so users on one shared address locked each
        other out. Only failures may cost anything.
        """
        url = reverse("accounts:login")
        payload = {"username": "citizen1", "password": DEFAULT_PASSWORD}

        with patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"login": "3/min"}):
            statuses = [self.client.post(url, payload).status_code for _ in range(10)]

        self.assertTrue(
            all(code == status.HTTP_200_OK for code in statuses),
            "A correct password must never consume the rate limit: " + str(statuses),
        )

    def test_a_correct_password_still_works_after_some_failures(self):
        """Failures count, but must not lock out the genuine user below the limit."""
        url = reverse("accounts:login")

        with patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"login": "5/min"}):
            for _ in range(3):
                self.client.post(url, {"username": "citizen1", "password": "wrong"})
            response = self.client.post(
                url, {"username": "citizen1", "password": DEFAULT_PASSWORD}
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_registration_has_its_own_throttle_bucket(self):
        """
        Login and registration used to share one scope, so a burst of sign-ins
        exhausted the allowance for creating accounts.
        """
        login_url = reverse("accounts:login")

        with patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"login": "2/min", "register": "5/hour"}):
            for _ in range(4):
                self.client.post(login_url, {"username": "citizen1", "password": "wrong"})

            response = self.client.post(
                reverse("accounts:register"),
                {
                    "username": "unaffected",
                    "email": "unaffected@example.com",
                    "first_name": "Un",
                    "password": DEFAULT_PASSWORD,
                    "password_confirm": DEFAULT_PASSWORD,
                },
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class OfficerProfileTests(APITestCase):
    def test_officer_capacity_tracking(self):
        officer = make_officer(max_active_cases=2)
        profile = officer.officer_profile
        self.assertEqual(profile.active_case_count, 0)
        self.assertTrue(profile.has_capacity)

    def test_officer_updates_only_their_own_availability(self):
        officer = make_officer()
        self.client.force_authenticate(officer)
        response = self.client.patch(
            reverse("accounts:my-availability"), {"is_available": False}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        officer.officer_profile.refresh_from_db()
        self.assertFalse(officer.officer_profile.is_available)
