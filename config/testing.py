"""
Shared test factories.

Every test needs users, reports, and sometimes a fully inspected case. Rebuilding
that setup in each test file would be repetitive and would drift apart over time,
so the builders live here and each app's tests import what they need.

`make_signed_case()` in particular is worth understanding: it walks a case all
the way from submission to a signed inspection with calculated compensation. That
is the starting point for every approval test, and building it through the real
services (rather than by writing rows directly) means the tests exercise the same
code path production does.
"""

from __future__ import annotations

import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from reports.models import DisasterReport

from accounts.models import OfficerProfile, Role

User = get_user_model()

DEFAULT_PASSWORD = "ResQNet!Test2026"


def make_citizen(username="citizen1", district="Kollam", **extra) -> "User":
    return User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=DEFAULT_PASSWORD,
        role=Role.CITIZEN,
        first_name=username.title(),
        last_name="Citizen",
        district=district,
        phone_number="+919000000001",
        **extra,
    )


def make_officer(
    username="officer1",
    zone="Kollam",
    latitude=Decimal("8.8932"),
    longitude=Decimal("76.6141"),
    is_available=True,
    max_active_cases=5,
    employee_id=None,
) -> "User":
    officer = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=DEFAULT_PASSWORD,
        role=Role.FIELD_OFFICER,
        first_name=username.title(),
        last_name="Officer",
        district=zone,
    )
    OfficerProfile.objects.create(
        officer=officer,
        employee_id=employee_id or f"EMP-{username.upper()}",
        zone=zone,
        base_latitude=latitude,
        base_longitude=longitude,
        is_available=is_available,
        max_active_cases=max_active_cases,
    )
    return officer


def make_admin(username="admin1") -> "User":
    return User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=DEFAULT_PASSWORD,
        role=Role.ADMIN,
        first_name="Admin",
        last_name="User",
    )


def report_payload(**overrides) -> dict:
    """Valid report data. Override any field to build an invalid variant."""
    payload = {
        "disaster_type": "FLOOD",
        "damage_category": "RESIDENTIAL",
        "description": "Flood water entered the house and damaged the walls and wiring.",
        "latitude": "8.8932",
        "longitude": "76.6141",
        "address": "12 Riverside Road, Kollam",
        "district": "Kollam",
        "standing_water": True,
        "wall_damage": True,
        "people_affected": 4,
    }
    payload.update(overrides)
    return payload


def make_report(citizen, *, submit=True, **overrides):
    """Create a report through the real service, so triage and dispatch run."""
    from reports.serializers import ReportCreateSerializer
    from reports.services import create_report

    data = report_payload(**overrides)
    data["submit_now"] = submit

    serializer = ReportCreateSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    report, _ = create_report(citizen=citizen, validated_data=serializer.validated_data)
    return report


def make_image(name="evidence.jpg", size=(400, 300), image_format="JPEG"):
    """A real in-memory JPEG, so upload validation genuinely runs."""
    buffer = io.BytesIO()
    Image.new("RGB", size, color=(120, 140, 160)).save(buffer, format=image_format)
    buffer.seek(0)
    content_type = "image/jpeg" if image_format == "JPEG" else f"image/{image_format.lower()}"
    return SimpleUploadedFile(name, buffer.read(), content_type=content_type)


def make_signed_case(citizen=None, officer=None, **severity):
    """
    Build a case that is inspected, signed, and costed.

    Returns `(report, inspection, officer)`. Severity keyword arguments override
    the inspection checklist, e.g. `roof_damage="DESTROYED"`.
    """
    from inspections.services import complete_inspection, sign_off_inspection, start_inspection, update_inspection

    # Distinct default names, so calling this from a test whose setUp already
    # built "citizen1"/"officer1" does not collide on the unique username.
    citizen = citizen or make_citizen(username="case_citizen")
    officer = officer or make_officer(username="case_officer", employee_id="EMP-CASE")
    # Keep this helper's property distinct from reports created by a test's
    # setUp. The real integrity workflow must hold genuinely matching claims;
    # this factory is asking specifically for a legitimate inspection fixture.
    sequence = DisasterReport.objects.count() + 1
    report = make_report(
        citizen,
        latitude=f"{8.70 + sequence * 0.01:.4f}",
        longitude=f"{76.20 + sequence * 0.01:.4f}",
        address=f"Parcel{sequence} Landmark{sequence}",
        description=f"EvidenceToken{sequence} records damage at Parcel{sequence}.",
    )

    assignment = report.assignments.first()
    assert assignment is not None, "Dispatch did not assign an officer - check officer availability."

    inspection = start_inspection(assignment=assignment, user=officer)
    checklist = {
        "roof_damage": "SEVERE",
        "wall_damage": "MODERATE",
        "people_affected": 4,
        "is_habitable": True,
        "remarks": "Verified on site. Roof sheeting torn off, wall plaster damaged.",
    }
    checklist.update(severity)
    update_inspection(inspection=inspection, data=checklist, user=officer)
    complete_inspection(inspection=inspection, user=officer)
    sign_off_inspection(inspection=inspection, signature_name="Test Officer", user=officer)

    report.refresh_from_db()
    inspection.refresh_from_db()
    return report, inspection, officer
