"""
Set the field-officer roster to an exact list.

Why this renames rather than deletes
------------------------------------
`Assignment.officer` and `Inspection.officer` are both `on_delete=PROTECT`, so an
officer who has ever been dispatched cannot be deleted — and that is deliberate.
An inspection is a signed record of who verified a property; deleting the officer
would leave relief decisions with no accountable author, which is exactly what
the audit trail exists to prevent.

So the roster is reshaped in place:

* Existing officers are **renamed** onto the new identities, keeping every
  assignment, inspection and audit entry attached to the same row.
* Any shortfall is **created**.
* Anyone left over is **deactivated**, not removed — they stop receiving
  dispatch and cannot sign in, while their past work stays attributable.

    python manage.py set_officers
    python manage.py set_officers --list
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import OfficerProfile, Role, User

#: The canonical roster. (display name, employee id, zone, latitude, longitude, capacity)
OFFICERS = [
    ("Akmal",    "EMP-1001", "Kollam",    Decimal("8.8932"), Decimal("76.6141"), 6),
    ("Najeeb",   "EMP-1002", "Kollam",    Decimal("8.9210"), Decimal("76.5810"), 5),
    ("Sohail",   "EMP-1003", "Alappuzha", Decimal("9.4981"), Decimal("76.3388"), 5),
    ("Abdullah", "EMP-1004", "Alappuzha", Decimal("9.4650"), Decimal("76.3300"), 5),
    ("Rehan",    "EMP-1005", "Kollam",    Decimal("8.9050"), Decimal("76.6300"), 5),
]

EMAIL_DOMAIN = "resqnet.local"


def username_for(name: str) -> str:
    return f"officer.{name.lower()}"


def email_for(name: str) -> str:
    return f"officer.{name.lower()}@{EMAIL_DOMAIN}"


class Command(BaseCommand):
    help = "Make the field-officer roster exactly the configured list."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list",
            action="store_true",
            help="Show the current roster and exit without changing anything.",
        )

    def handle(self, *args, **options):
        if options["list"]:
            self._show_roster()
            return
        self._apply()
        self.stdout.write("")
        self._show_roster()

    # ------------------------------------------------------------------ #

    @transaction.atomic
    def _apply(self):
        # Oldest first, so the officer carrying the most history keeps the first
        # slot and the renames stay stable across repeated runs.
        existing = list(
            User.objects.filter(role=Role.FIELD_OFFICER).order_by("date_joined", "id")
        )

        for index, (name, employee_id, zone, latitude, longitude, capacity) in enumerate(OFFICERS):
            username = username_for(name)
            email = email_for(name)

            if index < len(existing):
                officer = existing[index]
                previous = officer.username
                officer.username = username
                officer.email = email
                officer.first_name = name
                officer.last_name = ""
                officer.district = zone
                officer.is_active = True
                # The old Firebase account was created against the previous
                # email, so the link no longer describes this person. Cleared
                # here and re-established on their next Firebase sign-in.
                officer.firebase_uid = None
                officer.auth_provider = ""
                officer.save()
                action = f"renamed from {previous}"
            else:
                officer = User(
                    username=username,
                    email=email,
                    first_name=name,
                    role=Role.FIELD_OFFICER,
                    district=zone,
                    is_active=True,
                )
                # Firebase holds the credential; an unusable password can never
                # match any input, which is stronger than a blank one.
                officer.set_unusable_password()
                officer.save()
                action = "created"

            profile, _ = OfficerProfile.objects.get_or_create(
                officer=officer,
                defaults={
                    "employee_id": employee_id,
                    "zone": zone,
                    "base_latitude": latitude,
                    "base_longitude": longitude,
                    "max_active_cases": capacity,
                },
            )
            profile.employee_id = employee_id
            profile.zone = zone
            profile.base_latitude = latitude
            profile.base_longitude = longitude
            profile.max_active_cases = capacity
            profile.is_available = True
            profile.save()

            self.stdout.write(f"  {name:<10} {employee_id}  {zone:<10} {action}")

        # Anyone beyond the roster is stood down rather than removed.
        for officer in existing[len(OFFICERS):]:
            officer.is_active = False
            officer.save(update_fields=["is_active"])
            OfficerProfile.objects.filter(officer=officer).update(is_available=False)
            self.stdout.write(
                self.style.WARNING(
                    f"  {officer.username:<10} deactivated (past work preserved)"
                )
            )

    def _show_roster(self):
        self.stdout.write(self.style.SUCCESS("Field officers:"))
        officers = (
            User.objects.filter(role=Role.FIELD_OFFICER)
            .select_related("officer_profile")
            .order_by("officer_profile__employee_id")
        )
        if not officers:
            self.stdout.write("  (none)")
            return

        for officer in officers:
            profile = getattr(officer, "officer_profile", None)
            state = "active" if officer.is_active else "INACTIVE"
            cases = profile.active_case_count if profile else 0
            self.stdout.write(
                f"  {officer.first_name:<10} {profile.employee_id if profile else '—':<10} "
                f"{profile.zone if profile else '—':<11} {officer.email:<32} "
                f"{state:<9} {cases} open case(s)"
            )
