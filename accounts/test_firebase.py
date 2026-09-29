"""
Firebase authentication tests.

Token verification is mocked throughout — these tests are about what ResQNet
does with a *verified* identity, not about Google's signature checking. The
security properties being pinned down are:

  1. A verified token never grants a privileged role.
  2. An unknown email becomes a citizen, always.
  3. Google sign-in works for citizens and is refused for officers and admins.
  4. A pre-provisioned officer signs in as that officer, not as a new citizen.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from config.testing import make_admin, make_citizen, make_officer

from .firebase import (
    FirebaseAccountExists,
    FirebaseError,
    has_admin_credentials,
    is_configured,
    verify_id_token,
)
from .models import Role
from .services import FirebaseAuthDenied, resolve_firebase_user

User = get_user_model()


def claims(email, *, uid="fb-uid-1", provider="password", name="Test User"):
    """Build the claim dict `verify_id_token` returns after a successful check."""
    return {
        "uid": uid,
        "email": email.lower(),
        "email_verified": True,
        "name": name,
        "provider": provider,
    }


class RoleAssignmentTests(TestCase):
    """A verified identity proves who you are, never what you may do."""

    def test_unknown_email_becomes_a_citizen(self):
        user, created = resolve_firebase_user(claims("newperson@example.com"))

        self.assertTrue(created)
        self.assertEqual(user.role, Role.CITIZEN)
        self.assertEqual(user.email, "newperson@example.com")
        self.assertEqual(user.firebase_uid, "fb-uid-1")

    def test_new_firebase_account_has_no_usable_django_password(self):
        """Firebase holds the credential; there must be no password to guess."""
        user, _ = resolve_firebase_user(claims("nopassword@example.com"))
        self.assertFalse(user.has_usable_password())

    def test_a_preprovisioned_officer_signs_in_as_that_officer(self):
        officer = make_officer(username="officer.pre")
        officer.email = "officer.pre@example.com"
        officer.save()

        user, created = resolve_firebase_user(claims("officer.pre@example.com"))

        self.assertFalse(created)
        self.assertEqual(user.pk, officer.pk)
        self.assertEqual(user.role, Role.FIELD_OFFICER)

    def test_email_match_is_case_insensitive(self):
        citizen = make_citizen(username="mixedcase")
        citizen.email = "Mixed.Case@Example.com"
        citizen.save()

        user, created = resolve_firebase_user(claims("mixed.case@example.com"))
        self.assertFalse(created)
        self.assertEqual(user.pk, citizen.pk)

    def test_uid_match_survives_an_email_change_at_the_provider(self):
        """Otherwise a user changing their Google email would orphan their cases."""
        user, _ = resolve_firebase_user(claims("original@example.com", uid="stable-uid"))

        again, created = resolve_firebase_user(
            claims("changed@example.com", uid="stable-uid")
        )
        self.assertFalse(created)
        self.assertEqual(again.pk, user.pk)

    def test_uid_takes_precedence_over_email(self):
        """
        The uid is matched first, so a token whose uid we already know signs in
        as that account even if the email now points at a different one. This is
        what makes an email change at the provider safe, and it is why the
        duplicate-uid guard in the service is defence against a concurrent
        create rather than something reachable in a single request.
        """
        original, _ = resolve_firebase_user(claims("first@example.com", uid="shared-uid"))

        other = make_citizen(username="other")
        other.email = "second@example.com"
        other.save()

        user, created = resolve_firebase_user(claims("second@example.com", uid="shared-uid"))

        self.assertFalse(created)
        self.assertEqual(user.pk, original.pk)
        self.assertNotEqual(user.pk, other.pk)

    def test_deactivated_account_is_refused(self):
        citizen = make_citizen(username="banned")
        citizen.email = "banned@example.com"
        citizen.is_active = False
        citizen.save()

        with self.assertRaises(FirebaseAuthDenied):
            resolve_firebase_user(claims("banned@example.com"))

    def test_usernames_do_not_collide(self):
        """
        A new Firebase identity whose email prefix matches an existing username
        must still get a unique username. The existing account here has a
        different email address, so it is genuinely a different person.
        """
        existing = make_citizen(username="duplicate")
        existing.email = "someone.else@example.com"
        existing.save()

        user, created = resolve_firebase_user(claims("duplicate@example.com"))

        self.assertTrue(created)
        self.assertNotEqual(user.username, "duplicate")
        self.assertTrue(user.username.startswith("duplicate"))


class GoogleRestrictionTests(TestCase):
    """Google sign-in is for citizens only."""

    def test_citizen_may_use_google(self):
        user, created = resolve_firebase_user(
            claims("googler@example.com", provider="google.com")
        )
        self.assertTrue(created)
        self.assertEqual(user.role, Role.CITIZEN)
        self.assertEqual(user.auth_provider, "google.com")

    def test_officer_may_not_use_google(self):
        officer = make_officer(username="officer.google")
        officer.email = "officer.google@example.com"
        officer.save()

        with self.assertRaises(FirebaseAuthDenied) as caught:
            resolve_firebase_user(claims("officer.google@example.com", provider="google.com"))
        self.assertIn("Google", str(caught.exception))

    def test_administrator_may_not_use_google(self):
        admin = make_admin(username="admin.google")
        admin.email = "admin.google@example.com"
        admin.save()

        with self.assertRaises(FirebaseAuthDenied):
            resolve_firebase_user(claims("admin.google@example.com", provider="google.com"))

    def test_officer_may_use_email_and_password(self):
        officer = make_officer(username="officer.pw")
        officer.email = "officer.pw@example.com"
        officer.save()

        user, _ = resolve_firebase_user(claims("officer.pw@example.com", provider="password"))
        self.assertEqual(user.pk, officer.pk)

    @override_settings(FIREBASE_CITIZEN_PROVIDERS=["password"])
    def test_google_can_be_disabled_entirely_by_configuration(self):
        with self.assertRaises(FirebaseAuthDenied):
            resolve_firebase_user(claims("anyone@example.com", provider="google.com"))


class FirebaseEndpointTests(APITestCase):
    """The HTTP surface: /api/auth/firebase/login/."""

    def setUp(self):
        self.url = reverse("accounts:firebase-login")

    @patch("accounts.views.firebase.is_configured", return_value=True)
    @patch("accounts.views.firebase.verify_id_token")
    def test_valid_token_creates_a_session(self, mock_verify, _mock_configured):
        mock_verify.return_value = claims("endpoint@example.com")

        response = self.client.post(self.url, {"id_token": "a-valid-token"})

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["user"]["role"], Role.CITIZEN)
        self.assertIn("token", response.data)
        self.assertIn("sessionid", response.cookies)

        # The session must actually work on a protected page.
        self.assertEqual(self.client.get(reverse("web:citizen_dashboard")).status_code, 200)

    @patch("accounts.views.firebase.is_configured", return_value=True)
    @patch("accounts.views.firebase.verify_id_token", side_effect=FirebaseError("bad token"))
    def test_invalid_token_is_rejected(self, _mock_verify, _mock_configured):
        response = self.client.post(self.url, {"id_token": "forged"})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("accounts.views.firebase.is_configured", return_value=True)
    @patch("accounts.views.firebase.verify_id_token")
    def test_google_token_for_an_officer_is_refused_at_the_api(self, mock_verify, _cfg):
        officer = make_officer(username="officer.api")
        officer.email = "officer.api@example.com"
        officer.save()
        mock_verify.return_value = claims("officer.api@example.com", provider="google.com")

        response = self.client.post(self.url, {"id_token": "google-token"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("accounts.views.firebase.is_configured", return_value=True)
    @patch("accounts.views.firebase.verify_id_token")
    def test_client_cannot_request_a_privileged_role(self, mock_verify, _cfg):
        """Extra fields in the body are ignored: the server decides the role."""
        mock_verify.return_value = claims("sneaky@example.com")

        response = self.client.post(
            self.url, {"id_token": "valid", "role": "ADMIN", "is_superuser": True}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        user = User.objects.get(email="sneaky@example.com")
        self.assertEqual(user.role, Role.CITIZEN)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_staff)

    @patch("accounts.views.firebase.is_configured", return_value=False)
    def test_unconfigured_server_reports_clearly(self, _mock_configured):
        response = self.client.post(self.url, {"id_token": "anything"})
        self.assertEqual(response.status_code, status.HTTP_501_NOT_IMPLEMENTED)

    @patch("accounts.views.firebase.is_configured", return_value=True)
    def test_missing_token_is_rejected(self, _mock_configured):
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_config_endpoint_is_public_and_leaks_no_key_material(self):
        """
        The web config is deliberately public — it ships in every Firebase web
        app. What must never appear is service-account key material.

        Note the endpoint may name the *setting* FIREBASE_CREDENTIALS_FILE in its
        diagnostic message; a setting name is a hint, not a secret.
        """
        response = self.client.get(reverse("accounts:firebase-config"))
        body = str(response.data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("configured", response.data)
        self.assertEqual(response.data["google_enabled_for"], ["CITIZEN"])

        for secret_marker in [
            "private_key",
            "BEGIN PRIVATE KEY",
            "client_secret",
            "service_account",
        ]:
            with self.subTest(marker=secret_marker):
                self.assertNotIn(secret_marker, body)

    @patch("accounts.views.firebase.google_provider_enabled", return_value=False)
    def test_config_reports_when_google_is_not_enabled(self, _mock):
        """
        Without this the citizen clicks "Continue with Google", a popup opens,
        and Firebase returns auth/operation-not-allowed — which reads as a broken
        site rather than an unconfigured provider.
        """
        response = self.client.get(reverse("accounts:firebase-config"))
        self.assertIs(response.data["google_enabled"], False)

    @patch("accounts.views.firebase.google_provider_enabled", return_value=None)
    def test_undetermined_google_status_is_not_reported_as_disabled(self, _mock):
        """
        None must stay distinct from False. If the probe cannot reach Google we
        must not disable a button that may work perfectly well.
        """
        response = self.client.get(reverse("accounts:firebase-config"))
        self.assertIsNone(response.data["google_enabled"])

    def test_provider_probe_failure_returns_none_rather_than_raising(self):
        from .firebase import google_provider_enabled

        with patch("requests.get", side_effect=OSError("network down")):
            with override_settings(FIREBASE_API_KEY="probe-key"):
                from django.core.cache import cache

                cache.delete("resqnet.firebase.providers")
                self.assertIsNone(google_provider_enabled())

    def test_config_endpoint_reports_which_half_is_missing(self):
        """
        Web config and service account fail for different reasons and need
        different fixes, so the status distinguishes them.
        """
        response = self.client.get(reverse("accounts:firebase-config"))

        self.assertIn("web_configured", response.data)
        self.assertIn("server_configured", response.data)
        if not response.data["configured"]:
            self.assertTrue(response.data["reason"])


class CredentialDetectionTests(TestCase):
    """
    `is_configured()` must verify the credential file actually exists.

    The regression this guards: it originally checked only that the setting was
    non-empty, so a path pointing at a missing file reported "ready". The login
    page then offered Google sign-in that could not possibly work, and failed at
    token verification — which reads as a broken site rather than a missing key.
    """

    @override_settings(
        FIREBASE_CREDENTIALS_FILE="", FIREBASE_CREDENTIALS_JSON="", FIREBASE_PROJECT_ID=""
    )
    def test_nothing_configured_at_all(self):
        self.assertFalse(is_configured())
        self.assertFalse(has_admin_credentials())

    @override_settings(
        FIREBASE_CREDENTIALS_FILE="secrets/does-not-exist.json",
        FIREBASE_CREDENTIALS_JSON="",
    )
    def test_a_path_to_a_missing_file_is_not_admin_credentials(self):
        """
        The regression this guards: `has_admin_credentials()` originally checked
        only that the setting was non-empty, so a path to a missing file reported
        "ready" and then failed at token verification.
        """
        self.assertFalse(has_admin_credentials())

    @override_settings(
        FIREBASE_CREDENTIALS_FILE="",
        FIREBASE_CREDENTIALS_JSON="",
        FIREBASE_PROJECT_ID="resqnet-demo",
    )
    def test_project_id_alone_is_enough_to_verify_sign_ins(self):
        """
        A Firebase ID token is an RS256 JWT signed by Google. Verifying it needs
        only a public key and the project id, so citizens and officers can sign
        in before any service account has been downloaded.
        """
        self.assertTrue(is_configured())
        self.assertFalse(has_admin_credentials())

    @override_settings(FIREBASE_CREDENTIALS_FILE="", FIREBASE_CREDENTIALS_JSON='{"type":"service_account"}')
    def test_inline_json_counts_as_admin_credentials(self):
        self.assertTrue(has_admin_credentials())

    def test_an_existing_file_counts_as_admin_credentials(self):
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as folder:
            path = _Path(folder) / "sa.json"
            path.write_text('{"type": "service_account"}', encoding="utf-8")
            with override_settings(
                FIREBASE_CREDENTIALS_FILE=str(path), FIREBASE_CREDENTIALS_JSON=""
            ):
                self.assertTrue(has_admin_credentials())


class PublicKeyVerificationTests(TestCase):
    """
    The no-service-account path must stay as strict as the SDK one.

    The dangerous mistake would be verifying the signature but not the audience:
    a token from *any* Firebase project is signed by the same Google keys, so
    without the `aud` check anyone with their own Firebase project could mint a
    token and sign in here as anybody.
    """

    @override_settings(FIREBASE_CREDENTIALS_FILE="", FIREBASE_CREDENTIALS_JSON="")
    def test_garbage_token_is_rejected(self):
        with self.assertRaises(FirebaseError):
            verify_id_token("not-a-jwt")

    @override_settings(FIREBASE_CREDENTIALS_FILE="", FIREBASE_CREDENTIALS_JSON="")
    def test_empty_token_is_rejected(self):
        with self.assertRaises(FirebaseError):
            verify_id_token("")

    @override_settings(
        FIREBASE_CREDENTIALS_FILE="", FIREBASE_CREDENTIALS_JSON="", FIREBASE_PROJECT_ID=""
    )
    def test_missing_project_id_refuses_to_verify(self):
        """Better to refuse every sign-in than to accept an unpinned token."""
        with self.assertRaises(FirebaseError):
            verify_id_token("a.b.c")

    def test_audience_and_issuer_are_pinned_to_this_project(self):
        """Read the call itself, since forging a real token in a test is not possible."""
        import inspect

        from . import firebase as firebase_module

        source = inspect.getsource(firebase_module._verify_with_public_keys)
        self.assertIn("audience=project_id", source)
        self.assertIn("securetoken.google.com", source)
        self.assertIn('algorithms=["RS256"]', source)


class OfficerProvisioningTests(APITestCase):
    """
    Creating an officer should also create their Firebase account.

    Officers cannot self-register, so without this an administrator must create
    every officer twice — once here and once by hand in the Firebase console.
    """

    def setUp(self):
        self.admin = make_admin()
        self.client.force_authenticate(self.admin)
        self.url = reverse("accounts:officer-list")
        self.payload = {
            "username": "officer.new",
            "email": "officer.new@example.com",
            "first_name": "New",
            "last_name": "Officer",
            "password": "Officer-Start-2026!",
            "profile": {
                "employee_id": "EMP-NEW-1",
                "zone": "Kollam",
                "base_latitude": "8.8932",
                "base_longitude": "76.6141",
            },
        }

    @patch("accounts.views.firebase.has_admin_credentials", return_value=True)
    @patch("accounts.views.firebase.provision_user")
    def test_creating_an_officer_uses_matching_firebase_and_django_credentials(self, mock_provision, _cfg):
        mock_provision.return_value = {"uid": "fb-officer-1", "created": True}

        response = self.client.post(self.url, self.payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["provisioning"]["firebase"], "created")
        self.assertNotIn("password", response.data)
        mock_provision.assert_called_once_with(
            email="officer.new@example.com",
            password=self.payload["password"],
            display_name="New Officer",
        )

        officer = User.objects.get(username="officer.new")
        self.assertEqual(officer.firebase_uid, "fb-officer-1")
        self.assertEqual(officer.role, Role.FIELD_OFFICER)
        self.assertEqual(officer.email, self.payload["email"])
        self.assertTrue(officer.check_password(self.payload["password"]))
        self.assertEqual(officer.officer_profile.zone, "Kollam")

    @patch("accounts.views.firebase.has_admin_credentials", return_value=True)
    @patch("accounts.views.firebase.provision_user", side_effect=FirebaseError("quota exceeded"))
    def test_firebase_creation_error_does_not_leave_a_django_officer(self, _p, _cfg):
        response = self.client.post(self.url, self.payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertFalse(User.objects.filter(username="officer.new").exists())

    @patch("accounts.views.firebase.has_admin_credentials", return_value=False)
    def test_missing_admin_credentials_explains_configuration_and_creates_nothing(self, _cfg):
        response = self.client.post(self.url, self.payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn("FIREBASE_CREDENTIALS_FILE", response.data["detail"])
        self.assertIn("FIREBASE_CREDENTIALS_JSON", response.data["detail"])
        self.assertFalse(User.objects.filter(username="officer.new").exists())

    def test_password_is_required(self):
        payload = {key: value for key, value in self.payload.items() if key != "password"}
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.data)

    @patch("accounts.views.firebase.has_admin_credentials", return_value=True)
    @patch("accounts.views.firebase.provision_user", return_value={"uid": "fb-officer-1", "created": True})
    def test_duplicate_officer_email_is_rejected(self, mock_provision, _cfg):
        first = self.client.post(self.url, self.payload, format="json")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)

        second = dict(self.payload)
        second["username"] = "officer.other"
        second["profile"] = dict(self.payload["profile"], employee_id="EMP-NEW-2")

        response = self.client.post(self.url, second, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(mock_provision.call_count, 1)

    @patch("accounts.views.firebase.has_admin_credentials", return_value=True)
    @patch("accounts.views.firebase.provision_user", side_effect=FirebaseAccountExists("already exists"))
    def test_duplicate_firebase_email_returns_conflict_without_local_officer(self, _p, _cfg):
        response = self.client.post(self.url, self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(User.objects.filter(username="officer.new").exists())
        self.assertNotIn(self.payload["password"], str(response.data))


class FirebaseOfficerCreationTests(TestCase):
    @patch("accounts.firebase._get_app", return_value=object())
    @patch("firebase_admin.auth.get_user_by_email")
    @patch("firebase_admin.auth.create_user")
    def test_provision_user_creates_email_password_identity(self, create_user, get_user, _app):
        from types import SimpleNamespace
        from firebase_admin import auth

        get_user.side_effect = auth.UserNotFoundError("not found")
        create_user.return_value = SimpleNamespace(uid="firebase-uid")

        result = __import__("accounts.firebase", fromlist=["provision_user"]).provision_user(
            " Officer@Example.com ", "Secret-Start-2026!", "New Officer"
        )

        self.assertEqual(result, {"uid": "firebase-uid", "created": True})
        create_user.assert_called_once_with(
            email="officer@example.com",
            password="Secret-Start-2026!",
            display_name="New Officer",
            email_verified=False,
            app=_app.return_value,
        )

    @patch("accounts.firebase._get_app", return_value=object())
    @patch("firebase_admin.auth.get_user_by_email", return_value=object())
    @patch("firebase_admin.auth.create_user")
    def test_provision_user_refuses_existing_firebase_email(self, create_user, _get_user, _app):
        from accounts.firebase import provision_user

        with self.assertRaises(FirebaseAccountExists):
            provision_user("already@example.com", "Secret-Start-2026!")
        create_user.assert_not_called()


class AuditTrailTests(TestCase):
    """Firebase sign-ins must be as auditable as password sign-ins."""

    def test_new_firebase_account_is_audited(self):
        from audit.models import AuditAction, AuditEvent

        user, _ = resolve_firebase_user(claims("audited@example.com"))

        event = AuditEvent.objects.filter(
            action=AuditAction.USER_REGISTERED, entity_id=str(user.pk)
        ).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.metadata.get("via"), "firebase")
