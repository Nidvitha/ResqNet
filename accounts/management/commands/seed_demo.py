"""
Seed demonstration data.

A management command is a script you run with `manage.py <name>`. It has full
access to the project, which makes it the right place for setup work that is not
part of the request/response cycle.

This one builds a realistic slice of ResQNet so the portals have something to
show: accounts for all three roles, and cases spread across every stage of the
lifecycle from freshly-triaged through to relief paid.

Everything is created through the **real services** - `create_report`,
`sign_off_inspection`, `approve_case` - never by writing rows directly. That
matters: it means the triage scores, dispatch decisions, compensation figures and
audit trail in the demo are genuinely produced by the platform, not faked. If the
seed runs, the pipeline works.

    python manage.py seed_demo
    python manage.py seed_demo --reset     # wipe existing cases first
"""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import OfficerProfile, Role
from approvals.services import approve_case, complete_payout, initiate_payout, reject_case
from inspections.services import (
    complete_inspection,
    sign_off_inspection,
    start_inspection,
    update_inspection,
)
from reports.models import DisasterReport
from reports.serializers import ReportCreateSerializer
from reports.services import create_report

User = get_user_model()

DEMO_PASSWORD = "ResQNet@2026"

#: The field-officer roster. Kept identical to `set_officers.py` so reseeding
#: never resurrects a different set of names. Duty stations are around the
#: Kollam and Alappuzha districts of Kerala.
OFFICERS = [
    ("officer.akmal", "Akmal", "", "EMP-1001", "Kollam", Decimal("8.8932"), Decimal("76.6141"), 6),
    ("officer.najeeb", "Najeeb", "", "EMP-1002", "Kollam", Decimal("8.9210"), Decimal("76.5810"), 5),
    ("officer.sohail", "Sohail", "", "EMP-1003", "Alappuzha", Decimal("9.4981"), Decimal("76.3388"), 5),
    ("officer.abdullah", "Abdullah", "", "EMP-1004", "Alappuzha", Decimal("9.4650"), Decimal("76.3300"), 5),
    ("officer.rehan", "Rehan", "", "EMP-1005", "Kollam", Decimal("8.9050"), Decimal("76.6300"), 5),
]

CITIZENS = [
    ("citizen.das", "Meera", "Das", "Kollam"),
    ("citizen.pillai", "Ravi", "Pillai", "Kollam"),
    ("citizen.nair", "Latha", "Nair", "Alappuzha"),
    ("citizen.joseph", "George", "Joseph", "Alappuzha"),
    ("citizen.kumar", "Arun", "Kumar", "Kollam"),
]

#: (disaster, category, description, lat, lng, address, district, indicators)
CASES = [
    (
        "FLOOD", "RESIDENTIAL",
        "Flood water rose to chest height overnight. The rear wall has cracked badly and "
        "the entire ground floor wiring was submerged. We are sheltering with neighbours.",
        Decimal("8.8955"), Decimal("76.6180"), "23 Ashramam Road, Kollam", "Kollam",
        {"standing_water": True, "wall_damage": True, "exposed_live_wires": True,
         "major_structural_damage": True, "people_affected": 5},
    ),
    (
        "CYCLONE", "RESIDENTIAL",
        "The roof sheeting was torn off completely during the night storm. Two beams have "
        "come down in the front room. Elderly parents are trapped in the back room.",
        Decimal("8.8890"), Decimal("76.6090"), "7 Beach Road, Kollam", "Kollam",
        {"roof_collapsed": True, "people_trapped": True, "major_structural_damage": True,
         "medical_assistance_needed": True, "people_affected": 4},
    ),
    (
        "FLOOD", "RESIDENTIAL",
        "Water entered the house and stayed for three days. Furniture, appliances and "
        "stored grain are all destroyed. The approach road is still under water.",
        Decimal("9.4990"), Decimal("76.3410"), "112 Punnapra North, Alappuzha", "Alappuzha",
        {"standing_water": True, "road_blocked": True, "wall_damage": True, "people_affected": 6},
    ),
    (
        "LANDSLIDE", "RESIDENTIAL",
        "Part of the hillside came down against the back of the house. The foundation on "
        "that side has shifted and there are wide cracks through two walls.",
        Decimal("8.9350"), Decimal("76.6520"), "Hill View, Kottarakkara Road, Kollam", "Kollam",
        {"major_structural_damage": True, "wall_damage": True, "road_blocked": True,
         "people_affected": 3},
    ),
    (
        "FLOOD", "AGRICULTURAL",
        "Two acres of paddy just before harvest are completely submerged. The bund has "
        "breached in three places and the field is still holding water.",
        Decimal("9.4720"), Decimal("76.3250"), "Kuttanad paddy fields, Alappuzha", "Alappuzha",
        {"standing_water": True, "people_affected": 2},
    ),
    (
        "FIRE", "COMMERCIAL",
        "An electrical fault started a fire in the storeroom which spread to the shop front. "
        "The wiring is burnt through and the ceiling is unsafe.",
        Decimal("8.9010"), Decimal("76.5950"), "Chinnakada Market, Kollam", "Kollam",
        {"exposed_live_wires": True, "major_structural_damage": True, "people_affected": 2},
    ),
    (
        "CYCLONE", "RESIDENTIAL",
        "Tiles blown off about half the roof and the compound wall has fallen. Rain is now "
        "coming into two rooms.",
        Decimal("9.4880"), Decimal("76.3450"), "45 Mullackal, Alappuzha", "Alappuzha",
        {"wall_damage": True, "people_affected": 4},
    ),
    (
        "FLOOD", "RESIDENTIAL",
        "Standing water around the house for a week. Damp has risen through the walls and "
        "the septic tank has overflowed into the yard.",
        Decimal("8.9120"), Decimal("76.6250"), "Kadappakada, Kollam", "Kollam",
        {"standing_water": True, "people_affected": 3},
    ),
    (
        "EARTHQUAKE", "RESIDENTIAL",
        "Tremor caused cracks through the main load-bearing wall and the floor has settled "
        "unevenly. An engineer said not to sleep inside.",
        Decimal("9.5050"), Decimal("76.3520"), "Thathampally, Alappuzha", "Alappuzha",
        {"major_structural_damage": True, "wall_damage": True, "people_affected": 5},
    ),
    (
        "FLOOD", "RESIDENTIAL",
        "Water came up to knee height. Kitchen fittings and the refrigerator are ruined but "
        "the structure looks sound.",
        Decimal("8.8870"), Decimal("76.6210"), "Thevally, Kollam", "Kollam",
        {"standing_water": True, "people_affected": 2},
    ),
]

#: How far through the lifecycle each case should be taken.
#: (index, stage) - stages: "triaged", "inspecting", "pending", "approved", "paid", "rejected"
LIFECYCLE = [
    (0, "paid"),
    (1, "pending"),      # the critical one - left waiting for a decision
    (2, "approved"),
    (3, "pending"),
    (4, "inspecting"),
    (5, "pending"),
    (6, "paid"),
    (7, "triaged"),
    (8, "rejected"),
    (9, "triaged"),
]

SEVERITY_BY_STAGE = [
    {"roof_damage": "SEVERE", "wall_damage": "MODERATE", "electrical_damage": "SEVERE",
     "water_damage": "SEVERE", "household_damage": "MODERATE"},
    {"roof_damage": "DESTROYED", "structural_damage": "SEVERE", "wall_damage": "SEVERE"},
    {"water_damage": "SEVERE", "household_damage": "DESTROYED", "wall_damage": "MODERATE"},
    {"structural_damage": "SEVERE", "foundation_damage": "SEVERE", "wall_damage": "SEVERE"},
    {"water_damage": "MODERATE"},
    {"electrical_damage": "DESTROYED", "structural_damage": "MODERATE", "household_damage": "SEVERE"},
    {"roof_damage": "MODERATE", "wall_damage": "MINOR"},
    {"water_damage": "MINOR"},
    {"structural_damage": "SEVERE", "wall_damage": "SEVERE", "foundation_damage": "MODERATE"},
    {"household_damage": "MODERATE", "water_damage": "MINOR"},
]


class Command(BaseCommand):
    help = "Create demonstration accounts and a realistic set of cases across the lifecycle."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete all existing cases before seeding. Does not touch user accounts.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        random.seed(2026)   # deterministic output, so reruns look the same

        if options["reset"]:
            self.stdout.write(self.style.WARNING("Deleting existing cases…"))
            self._reset()

        if DisasterReport.objects.exists() and not options["reset"]:
            self.stdout.write(self.style.WARNING(
                "Cases already exist — nothing to do. Use --reset to rebuild the demo data."
            ))
            return

        admin = self._create_admin()
        officers = self._create_officers()
        citizens = self._create_citizens()
        self._create_cases(citizens, officers, admin)
        self._backdate()
        self._report(admin)

    # ------------------------------------------------------------------ #

    def _reset(self):
        """
        Remove cases but keep accounts.

        Order matters: audit events must go last, and the queryset-level delete
        is blocked on purpose, so they are removed through the raw manager.
        """
        from audit.models import AuditEvent
        from django.db import connection

        DisasterReport.objects.all().delete()
        # AuditEvent blocks .delete() by design; clear the demo history via SQL.
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {AuditEvent._meta.db_table}")

    def _create_admin(self):
        admin, created = User.objects.get_or_create(
            username="admin",
            defaults={
                "email": "admin@resqnet.local",
                "first_name": "District",
                "last_name": "Administrator",
                "role": Role.ADMIN,
                "is_staff": True,
                "is_superuser": True,
                "district": "Kollam",
            },
        )
        if created:
            admin.set_password(DEMO_PASSWORD)
            admin.save()
            self.stdout.write(self.style.SUCCESS("  Created administrator: admin"))
        return admin

    def _create_officers(self):
        officers = []
        for username, first, last, employee_id, zone, lat, lng, capacity in OFFICERS:
            officer, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": f"{username}@resqnet.local",
                    "first_name": first,
                    "last_name": last,
                    "role": Role.FIELD_OFFICER,
                    "district": zone,
                    "phone_number": "+9190000" + employee_id[-5:],
                },
            )
            if created:
                officer.set_password(DEMO_PASSWORD)
                officer.save()
            OfficerProfile.objects.get_or_create(
                officer=officer,
                defaults={
                    "employee_id": employee_id,
                    "zone": zone,
                    "base_latitude": lat,
                    "base_longitude": lng,
                    "max_active_cases": capacity,
                },
            )
            officers.append(officer)
        self.stdout.write(self.style.SUCCESS(f"  Created {len(officers)} field officers"))
        return officers

    def _create_citizens(self):
        citizens = []
        for username, first, last, district in CITIZENS:
            citizen, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": f"{username}@resqnet.local",
                    "first_name": first,
                    "last_name": last,
                    "role": Role.CITIZEN,
                    "district": district,
                    "phone_number": "+9198470" + str(random.randint(10000, 99999)),
                    "address": f"{district} district",
                },
            )
            if created:
                citizen.set_password(DEMO_PASSWORD)
                citizen.save()
            citizens.append(citizen)
        self.stdout.write(self.style.SUCCESS(f"  Created {len(citizens)} citizens"))
        return citizens

    def _create_cases(self, citizens, officers, admin):
        for index, (disaster, category, description, lat, lng, address, district, indicators) in enumerate(CASES):
            citizen = citizens[index % len(citizens)]

            payload = {
                "disaster_type": disaster,
                "damage_category": category,
                "description": description,
                "latitude": str(lat),
                "longitude": str(lng),
                "address": address,
                "district": district,
                "submit_now": True,
                **indicators,
            }
            serializer = ReportCreateSerializer(data=payload)
            serializer.is_valid(raise_exception=True)
            report, _ = create_report(citizen=citizen, validated_data=serializer.validated_data)
            report.refresh_from_db()

            stage = dict(LIFECYCLE)[index]
            self._advance(report, stage, SEVERITY_BY_STAGE[index], admin)

            self.stdout.write(
                f"    {report.reference}  {report.triage_level or '—':<8} "
                f"score {report.priority_score:<4} → {report.status}"
            )

    def _advance(self, report, stage, severity, admin):
        """Walk a case forward through the real service layer."""
        if stage == "triaged":
            return

        assignment = report.assignments.first()
        if assignment is None:
            # No officer was free - the case legitimately waits at TRIAGED.
            return

        officer = assignment.officer
        inspection = start_inspection(assignment=assignment, user=officer)

        if stage == "inspecting":
            update_inspection(
                inspection=inspection,
                data={**severity, "remarks": "Initial walk-through done; assessment in progress."},
                user=officer,
            )
            return

        checklist = dict(severity)
        checklist.setdefault("remarks", "Damage verified on site against the citizen's report.")
        checklist["people_affected"] = report.people_affected
        checklist["is_habitable"] = report.triage_level not in {"CRITICAL", "HIGH"}
        checklist["requires_immediate_relief"] = report.triage_level == "CRITICAL"
        checklist["estimated_damage_value"] = Decimal(str(random.randint(40, 300) * 1000))

        update_inspection(inspection=inspection, data=checklist, user=officer)
        complete_inspection(inspection=inspection, user=officer)
        sign_off_inspection(
            inspection=inspection,
            signature_name=officer.get_full_name(),
            user=officer,
        )
        report.refresh_from_db()

        if stage == "pending":
            return

        if stage == "rejected":
            reject_case(
                report=report,
                user=admin,
                reason="The affected structure falls outside the notified disaster boundary "
                       "for this event. Referred to the district office for separate consideration.",
            )
            return

        approve_case(
            report=report,
            user=admin,
            reason="Damage verified against the inspection photographs and the officer's "
                   "assessment. Award is within the relief matrix for this damage profile.",
        )
        report.refresh_from_db()

        if stage == "paid":
            initiate_payout(report=report, user=admin)
            report.refresh_from_db()
            complete_payout(report=report, user=admin)

    def _backdate(self):
        """
        Spread the cases over the past few weeks.

        `created_at` uses auto_now_add, so it can only be changed with a queryset
        update after the fact. Without this the trend chart is a single spike.
        """
        now = timezone.now()
        for offset, report in enumerate(DisasterReport.objects.order_by("id")):
            days = 18 - offset * 2
            DisasterReport.objects.filter(pk=report.pk).update(
                created_at=now - timedelta(days=max(days, 0), hours=random.randint(0, 20))
            )

    def _report(self, admin):
        from approvals.models import Payout
        from audit.models import AuditEvent
        from compensation.models import CompensationClaim

        total = CompensationClaim.objects.count()
        paid = Payout.objects.filter(status="PAYOUT_COMPLETED").count()

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Demo data ready."))
        self.stdout.write(f"  Cases            {DisasterReport.objects.count()}")
        self.stdout.write(f"  Claims costed    {total}")
        self.stdout.write(f"  Payouts complete {paid}")
        self.stdout.write(f"  Audit events     {AuditEvent.objects.count()}")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("  Administrator — password: " + DEMO_PASSWORD))
        self.stdout.write("    admin")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("  Field officers sign in through Firebase with these emails:"))
        for _, name, _, employee_id, zone, _, _, _ in OFFICERS:
            self.stdout.write(f"    officer.{name.lower()}@resqnet.local   {name} ({zone}, {employee_id})")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("  Citizens — password: " + DEMO_PASSWORD))
        self.stdout.write("    citizen.das")
