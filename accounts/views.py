"""
Accounts API views.

A *view* receives an HTTP request and returns an HTTP response. DRF's generic
views handle the repetitive parts (parsing, validation, status codes) so each
view here only expresses what is specific to ResQNet.

Notice how little logic lives in these functions: validation is in the
serializers, access rules are in the permission classes, and side effects go
through the audit service. That is the layering from Section 28.
"""

from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.db.models import Q
from rest_framework import generics, status
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from audit.models import AuditAction
from audit.services import record_event

from .models import OfficerProfile, Role, User
from .permissions import IsAdminRole
from . import firebase
from .services import FirebaseAuthDenied, resolve_firebase_user
from .throttling import LoginRateThrottle, RegistrationRateThrottle
from .serializers import (
    LoginSerializer,
    OfficerCreateSerializer,
    OfficerProfileSerializer,
    PasswordChangeSerializer,
    RegistrationSerializer,
    UserSerializer,
)


class RegisterView(generics.CreateAPIView):
    """Public citizen self-registration. Throttled against automated signup floods."""

    serializer_class = RegistrationSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [RegistrationRateThrottle]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        record_event(
            entity=user,
            action=AuditAction.USER_REGISTERED,
            user=user,
            new_state=user.role,
            metadata={"username": user.username},
            request=request,
        )

        # Sign the new account in straight away. Without a session the browser
        # would be redirected to the citizen dashboard, hit @login_required, and
        # bounce back to the login page - which looks exactly like a failure.
        # `backend` is passed explicitly because the user was not authenticated
        # through `authenticate()`, so Django cannot infer which backend to use.
        django_login(request, user, backend="django.contrib.auth.backends.ModelBackend")

        token, _ = Token.objects.get_or_create(user=user)
        return Response(
            {"user": UserSerializer(user).data, "token": token.key},
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    """
    Log in and receive an API token.

    Both a session and a token are issued: the session powers the server-rendered
    portals, while the token lets an offline-capable client keep working after
    the browser has been closed and reopened in a disaster zone.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})

        if not serializer.is_valid():
            # Only a *failed* attempt consumes the rate limit. Signing in
            # correctly costs nothing, so real users on a shared address are
            # never locked out by each other's successful logins.
            for throttle in self.get_throttles():
                if isinstance(throttle, LoginRateThrottle):
                    throttle.record_failure(request, self)
            raise ValidationError(serializer.errors)

        user = serializer.validated_data["user"]

        django_login(request, user)
        token, _ = Token.objects.get_or_create(user=user)

        record_event(
            entity=user,
            action=AuditAction.USER_LOGGED_IN,
            user=user,
            metadata={"role": user.role},
            request=request,
        )

        return Response({"user": UserSerializer(user).data, "token": token.key})


class FirebaseConfigView(APIView):
    """
    Public Firebase web config for the browser.

    Served from the server rather than hardcoded into a template so the same
    build runs against different Firebase projects, and so the login page can
    fall back to password sign-in when Firebase is not configured at all.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        web_ready = firebase.is_web_configured()
        server_ready = firebase.is_configured()

        # Reported separately because the two halves fail for different reasons
        # and need different fixes: the web config is copied from the Firebase
        # console, while the service account is a downloaded private key.
        if web_ready and server_ready:
            reason = ""
        elif not web_ready and not server_ready:
            reason = "Firebase is not configured on this server."
        elif not web_ready:
            reason = "The Firebase web configuration is missing (FIREBASE_API_KEY and friends)."
        else:
            reason = (
                "Sign-in verification is unavailable. Set FIREBASE_PROJECT_ID, or "
                "supply a service account via FIREBASE_CREDENTIALS_FILE."
            )

        return Response(
            {
                "configured": web_ready and server_ready,
                "web_configured": web_ready,
                "server_configured": server_ready,
                # Officer provisioning needs a service account; sign-in does not.
                # Reported separately so the officers screen can explain itself.
                "admin_sdk": firebase.has_admin_credentials(),
                # None when it could not be determined — the page then leaves the
                # Google button enabled rather than hiding something that works.
                "google_enabled": firebase.google_provider_enabled() if web_ready else None,
                "reason": reason,
                "config": firebase.public_config() if web_ready else {},
                # The browser uses this to decide whether to show the Google
                # button; the server enforces the same rule on every request.
                "google_enabled_for": ["CITIZEN"],
            }
        )


class FirebaseLoginView(APIView):
    """
    Exchange a verified Firebase ID token for a ResQNet session.

    The client never states who it is or what role it wants. It presents a token;
    the server verifies it against Google's public keys and decides the rest.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        id_token = (request.data.get("id_token") or "").strip()

        if not firebase.is_configured():
            return Response(
                {"detail": "Firebase sign-in is not configured on this server."},
                status=status.HTTP_501_NOT_IMPLEMENTED,
            )

        try:
            claims = firebase.verify_id_token(id_token)
        except firebase.FirebaseError as exc:
            # A token that fails verification is a failed sign-in attempt and
            # consumes the brute-force allowance.
            for throttle in self.get_throttles():
                if isinstance(throttle, LoginRateThrottle):
                    throttle.record_failure(request, self)
            return Response({"detail": str(exc)}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            user, created = resolve_firebase_user(claims, request=request)
        except FirebaseAuthDenied as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)

        django_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        token, _ = Token.objects.get_or_create(user=user)

        record_event(
            entity=user,
            action=AuditAction.USER_LOGGED_IN,
            user=user,
            metadata={
                "role": user.role,
                "via": "firebase",
                "provider": claims.get("provider", ""),
                "new_account": created,
            },
            request=request,
        )

        return Response(
            {"user": UserSerializer(user).data, "token": token.key, "created": created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        record_event(
            entity=request.user,
            action=AuditAction.USER_LOGGED_OUT,
            user=request.user,
            request=request,
        )
        # Discard the token so a stolen device cannot keep using it.
        Token.objects.filter(user=request.user).delete()
        django_logout(request)
        return Response({"detail": "Logged out."})


class MeView(generics.RetrieveUpdateAPIView):
    """Read or update your own profile. You can never reach anyone else's."""

    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        user = request.user
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])

        # A password change invalidates the old token.
        Token.objects.filter(user=user).delete()
        token = Token.objects.create(user=user)
        return Response({"detail": "Password updated.", "token": token.key})


class OfficerListCreateView(generics.ListCreateAPIView):
    """Administrators manage the field-officer roster here."""

    permission_classes = [IsAdminRole]

    def get_queryset(self):
        queryset = (
            User.objects.filter(role=Role.FIELD_OFFICER)
            .select_related("officer_profile")
            .order_by("first_name", "username")
        )
        zone = self.request.query_params.get("zone")
        if zone:
            queryset = queryset.filter(officer_profile__zone__iexact=zone)
        available = self.request.query_params.get("available")
        if available in {"true", "false"}:
            queryset = queryset.filter(officer_profile__is_available=(available == "true"))
        search = self.request.query_params.get("search")
        if search:
            queryset = queryset.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(username__icontains=search)
                | Q(officer_profile__employee_id__icontains=search)
            )
        return queryset

    def get_serializer_class(self):
        return OfficerCreateSerializer if self.request.method == "POST" else UserSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        officer = serializer.save()

        # Provision the matching Firebase account so the administrator does not
        # have to create every officer twice. Best-effort: a Firebase failure
        # must not roll back a valid ResQNet officer record, so it is reported
        # rather than raised.
        provisioning = {"firebase": "skipped", "reset_link": "", "detail": ""}
        if firebase.is_configured():
            try:
                result = firebase.provision_user(
                    email=officer.email,
                    display_name=officer.get_full_name() or officer.username,
                )
                officer.firebase_uid = result["uid"]
                officer.auth_provider = "password"
                officer.save(update_fields=["firebase_uid", "auth_provider", "updated_at"])
                provisioning = {
                    "firebase": "created" if result["created"] else "linked",
                    "reset_link": result["reset_link"],
                    "detail": (
                        "Send this link to the officer so they can set their own password. "
                        "It is single-use and expires."
                    ),
                }
            except firebase.FirebaseError as exc:
                provisioning = {
                    "firebase": "failed",
                    "reset_link": "",
                    "detail": (
                        f"The ResQNet officer record was created, but the Firebase "
                        f"account was not: {exc}"
                    ),
                }

        record_event(
            entity=officer,
            action=AuditAction.OFFICER_CREATED,
            user=request.user,
            new_state=Role.FIELD_OFFICER,
            metadata={
                "employee_id": officer.officer_profile.employee_id,
                "firebase": provisioning["firebase"],
            },
            request=request,
        )

        payload = UserSerializer(officer).data
        payload["provisioning"] = provisioning
        return Response(payload, status=status.HTTP_201_CREATED)


class OfficerDetailView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    permission_classes = [IsAdminRole]
    queryset = User.objects.filter(role=Role.FIELD_OFFICER).select_related("officer_profile")


class MyAvailabilityView(APIView):
    """
    An officer toggles their own availability and updates their duty location.

    Officers control only their own record - the object is fetched from
    `request.user`, so there is no id in the URL for anyone to tamper with.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile = self._profile(request)
        if profile is None:
            return Response(
                {"detail": "No officer profile for this account."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(OfficerProfileSerializer(profile).data)

    def patch(self, request):
        profile = self._profile(request)
        if profile is None:
            return Response(
                {"detail": "No officer profile for this account."},
                status=status.HTTP_404_NOT_FOUND,
            )
        allowed = {"is_available", "base_latitude", "base_longitude"}
        data = {k: v for k, v in request.data.items() if k in allowed}
        serializer = OfficerProfileSerializer(profile, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @staticmethod
    def _profile(request) -> OfficerProfile | None:
        return getattr(request.user, "officer_profile", None)
