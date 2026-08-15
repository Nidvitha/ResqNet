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

        record_event(
            entity=officer,
            action=AuditAction.OFFICER_CREATED,
            user=request.user,
            new_state=Role.FIELD_OFFICER,
            metadata={"employee_id": officer.officer_profile.employee_id},
            request=request,
        )
        return Response(UserSerializer(officer).data, status=status.HTTP_201_CREATED)


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
