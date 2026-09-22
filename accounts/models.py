"""
Accounts models - identity and roles for ResQNet.

A Django *model* is a Python class that describes one database table. Each class
attribute becomes a column, and Django generates the SQL for you through
*migrations*.

Why a custom User model?
------------------------
Django ships a perfectly good User model, but it has no concept of a "role".
ResQNet needs three sharply different kinds of people (citizen, field officer,
administrator), and every permission decision in the platform depends on knowing
which one is making the request. Swapping the user model later is painful, so
Django's own documentation insists you define it before the very first
migration - which is exactly what we are doing here.

We inherit from `AbstractUser`, which keeps everything Django already does well
(hashed passwords, sessions, the admin site, password reset) and lets us add
only what ResQNet needs on top.
"""

from django.contrib.auth.models import AbstractUser
from django.core.validators import RegexValidator
from django.db import models


class Role(models.TextChoices):
    """The three roles from Section 4. Stored as short strings, shown as labels."""

    CITIZEN = "CITIZEN", "Citizen"
    FIELD_OFFICER = "FIELD_OFFICER", "Field Officer"
    ADMIN = "ADMIN", "Administrator / Approver"


phone_validator = RegexValidator(
    regex=r"^\+?[0-9]{7,15}$",
    message="Enter a valid phone number (7-15 digits, optional leading +).",
)


class User(AbstractUser):
    """
    The single user table for every ResQNet participant.

    One table with a `role` column is far simpler than three separate user
    tables, and it keeps authentication logic in one place. Role-specific data
    that only applies to officers lives in `OfficerProfile` instead of cluttering
    this model with columns that are null for 99% of rows.
    """

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.CITIZEN,
        db_index=True,
        help_text="Determines what this user is allowed to do across the platform.",
    )
    phone_number = models.CharField(
        max_length=16,
        blank=True,
        validators=[phone_validator],
        help_text="Contact number used to reach the person about their case.",
    )
    address = models.TextField(
        blank=True,
        help_text="Postal address (citizens) or posting address (officers).",
    )
    district = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        help_text="Administrative district. Used to match reports to nearby officers.",
    )
    # --- Firebase identity -------------------------------------------------
    # Firebase owns the credential (password or Google account); ResQNet owns the
    # role. The uid is stored so an account survives the user changing their
    # email address at the provider, which would otherwise orphan their case
    # history. Null rather than blank-string, so the unique constraint permits
    # many accounts that have never used Firebase.
    firebase_uid = models.CharField(
        max_length=128,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="Firebase user id, set on first Firebase sign-in.",
    )
    auth_provider = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="How this account last signed in: password, google.com, or django.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date_joined"]
        indexes = [models.Index(fields=["role", "district"])]

    def __str__(self) -> str:
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    # Convenience checks. Using these instead of comparing strings everywhere
    # means a future rename touches exactly one file.
    @property
    def is_citizen(self) -> bool:
        return self.role == Role.CITIZEN

    @property
    def is_field_officer(self) -> bool:
        return self.role == Role.FIELD_OFFICER

    @property
    def is_admin_role(self) -> bool:
        """Platform administrator. Distinct from Django's `is_staff` flag."""
        return self.role == Role.ADMIN


class OfficerProfile(models.Model):
    """
    Extra information that only Field Officers have.

    This is a *one-to-one* relationship: exactly one profile per officer. The
    dispatch engine (Phase 4) reads every field here to decide who should be
    assigned to a new report - availability, location, and current workload.
    """

    officer = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="officer_profile",
        limit_choices_to={"role": Role.FIELD_OFFICER},
    )
    employee_id = models.CharField(max_length=40, unique=True)
    zone = models.CharField(
        max_length=100,
        db_index=True,
        help_text="Operational zone this officer covers. Matched against a report's district.",
    )
    base_latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        help_text="Latitude of the officer's duty station, used for distance ranking.",
    )
    base_longitude = models.DecimalField(max_digits=9, decimal_places=6)

    is_available = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Unavailable officers are skipped entirely by automatic dispatch.",
    )
    max_active_cases = models.PositiveSmallIntegerField(
        default=5,
        help_text="Workload ceiling. An officer at capacity is not auto-assigned more work.",
    )
    specialisation = models.CharField(
        max_length=120,
        blank=True,
        help_text="Optional expertise, e.g. 'structural', 'electrical'.",
    )
    last_synced_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last time this officer's device pulled assignments for offline use.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["employee_id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(base_latitude__gte=-90) & models.Q(base_latitude__lte=90),
                name="officer_latitude_within_range",
            ),
            models.CheckConstraint(
                condition=models.Q(base_longitude__gte=-180) & models.Q(base_longitude__lte=180),
                name="officer_longitude_within_range",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.employee_id} - {self.officer.get_full_name() or self.officer.username}"

    @property
    def active_case_count(self) -> int:
        """How many assignments this officer currently has open."""
        return self.officer.assignments.filter(
            status__in=["ASSIGNED", "ACCEPTED", "IN_PROGRESS"]
        ).count()

    @property
    def has_capacity(self) -> bool:
        return self.active_case_count < self.max_active_cases
