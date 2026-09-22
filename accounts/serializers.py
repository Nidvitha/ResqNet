"""
Accounts serializers.

A *serializer* does two jobs:

1. **Incoming**  - validate raw JSON from the client and turn it into safe
   Python values. This is where untrusted input gets checked.
2. **Outgoing**  - turn model instances into JSON, choosing exactly which fields
   the client is allowed to see.

Rule: never expose a field just because it exists. Password hashes, internal
flags, and other users' details stay out of the response.
"""

from django.contrib.auth import authenticate, password_validation
from django.db import transaction
from rest_framework import serializers

from .models import OfficerProfile, Role, User


class OfficerProfileSerializer(serializers.ModelSerializer):
    active_case_count = serializers.IntegerField(read_only=True)
    has_capacity = serializers.BooleanField(read_only=True)

    class Meta:
        model = OfficerProfile
        fields = [
            "employee_id",
            "zone",
            "base_latitude",
            "base_longitude",
            "is_available",
            "max_active_cases",
            "specialisation",
            "active_case_count",
            "has_capacity",
            "last_synced_at",
        ]
        read_only_fields = ["last_synced_at"]


class UserSerializer(serializers.ModelSerializer):
    """Public shape of a user. Never includes the password."""

    role_display = serializers.CharField(source="get_role_display", read_only=True)
    officer_profile = OfficerProfileSerializer(read_only=True)

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "role",
            "role_display",
            "phone_number",
            "address",
            "district",
            "date_joined",
            "officer_profile",
        ]
        read_only_fields = ["id", "role", "date_joined"]


class RegistrationSerializer(serializers.ModelSerializer):
    """
    Public self-registration.

    Only citizens may register themselves. Field officers and administrators are
    created by an existing administrator, because letting anyone claim the
    officer role over an open endpoint would hand out approval powers to the
    internet. That is why `role` is not accepted here at all.
    """

    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    password_confirm = serializers.CharField(write_only=True, style={"input_type": "password"})

    class Meta:
        model = User
        fields = [
            "username",
            "email",
            "first_name",
            "last_name",
            "phone_number",
            "address",
            "district",
            "password",
            "password_confirm",
        ]
        extra_kwargs = {
            "email": {"required": True},
            "first_name": {"required": True},
        }

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value.lower()

    def validate_password(self, value):
        # Runs Django's configured password validators: length, commonness,
        # not-entirely-numeric, and similarity to the username.
        password_validation.validate_password(value)
        return value

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        return attrs

    def create(self, validated_data):
        validated_data.pop("password_confirm")
        password = validated_data.pop("password")
        user = User(**validated_data, role=Role.CITIZEN)
        # `set_password` hashes with PBKDF2. We never touch `user.password`
        # directly - Section 9 forbids hand-rolled password storage.
        user.set_password(password)
        user.save()
        return user


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get("request"),
            username=attrs["username"],
            password=attrs["password"],
        )
        # One generic message for both "no such user" and "wrong password", so
        # the endpoint cannot be used to discover which usernames exist.
        if user is None:
            raise serializers.ValidationError("Invalid username or password.")
        if not user.is_active:
            raise serializers.ValidationError("This account has been deactivated.")
        attrs["user"] = user
        return attrs


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_current_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        password_validation.validate_password(value, self.context["request"].user)
        return value


class OfficerCreateSerializer(serializers.ModelSerializer):
    """
    Administrator-only creation of a field officer plus their profile.

    `password` is optional. Where Firebase is configured it holds the officer's
    credential, and the officer sets it themselves through a one-time link — so
    the administrator never knows their password. Omitting it leaves the Django
    account with no usable password, which is correct rather than insecure: an
    unusable password can never match any input.
    """

    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    profile = OfficerProfileSerializer(write_only=True)

    class Meta:
        model = User
        fields = [
            "username",
            "email",
            "first_name",
            "last_name",
            "phone_number",
            "district",
            "password",
            "profile",
        ]
        extra_kwargs = {"email": {"required": True}}

    def validate_email(self, value):
        # The email is how a Firebase sign-in is matched to this account, so a
        # duplicate would make the link ambiguous.
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value.lower()

    def validate_password(self, value):
        if value:
            password_validation.validate_password(value)
        return value

    @transaction.atomic
    def create(self, validated_data):
        # `transaction.atomic` makes the user and profile a single all-or-nothing
        # write: an officer can never exist without the profile dispatch needs.
        profile_data = validated_data.pop("profile")
        password = validated_data.pop("password", "")

        user = User(**validated_data, role=Role.FIELD_OFFICER)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()

        OfficerProfile.objects.create(officer=user, **profile_data)
        return user
