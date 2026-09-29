"""
Report services - the business logic for the reporting lifecycle.

Views should read like a table of contents, not like the book. When a citizen
submits a report, five things must happen in a fixed order, and every one of them
must succeed or none of them should: the case moves to SUBMITTED, triage scores
it, the score is stored, an officer is assigned, and each step is audited.
`submit_report()` owns that sequence so no view has to.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from audit.models import AuditAction
from audit.services import record_event

from .models import DisasterReport, ReportPhoto
from .states import CaseStatus, InvalidTransition

logger = logging.getLogger("resqnet.reports")


@transaction.atomic
def submit_report(report: DisasterReport, *, user=None, request=None) -> DisasterReport:
    """
    Move a report from DRAFT to SUBMITTED, then run triage and dispatch.

    `transaction.atomic` means the whole block is one database transaction. If
    dispatch raises halfway through, the SUBMITTED status and the triage result
    are both rolled back - the citizen sees a clean error rather than a case
    stranded in an impossible half-submitted state.

    Triage and dispatch are imported inside the function rather than at module
    level. `triage` imports from `reports`, so a top-level import here would
    create a circular import at startup.
    """
    from dispatch.services import auto_assign_report
    from triage.services import run_triage

    role = "CITIZEN" if user is None or user.is_citizen else "ADMIN"
    previous = report.apply_transition(CaseStatus.SUBMITTED, role=role)

    record_event(
        entity=report,
        action=AuditAction.REPORT_SUBMITTED,
        user=user,
        previous_state=previous,
        new_state=report.status,
        metadata={
            "disaster_type": report.disaster_type,
            "district": report.district,
            "created_offline": report.created_offline,
        },
        request=request,
    )

    # Triage is mandatory: an unscored case cannot be prioritised or dispatched.
    run_triage(report, user=user, request=request)

    # Dispatch is best-effort. If no officer is currently free in the zone, the
    # case legitimately waits at TRIAGED for an administrator to assign manually.
    try:
        auto_assign_report(report, request=request)
    except Exception:  # noqa: BLE001
        logger.exception("Automatic dispatch failed for %s; case remains TRIAGED", report.reference)

    report.refresh_from_db()
    return report


def create_report(*, citizen, validated_data: dict, request=None) -> tuple[DisasterReport, bool]:
    """
    Create a report, honouring the offline idempotency key.

    Returns `(report, created)`. When a phone replays a queued submission after a
    dropped connection, the existing case is returned with `created=False`
    instead of a duplicate being written (Section 13).
    """
    submit_now = validated_data.pop("submit_now", True)
    idempotency_key = (validated_data.get("idempotency_key") or "").strip()

    if idempotency_key:
        existing = DisasterReport.objects.filter(
            citizen=citizen, idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            logger.info("Duplicate submission ignored for key %s", idempotency_key)
            return existing, False

    if not validated_data.get("district"):
        validated_data["district"] = citizen.district or ""

    report = DisasterReport.objects.create(citizen=citizen, **validated_data)

    record_event(
        entity=report,
        action=AuditAction.REPORT_CREATED,
        user=citizen,
        new_state=report.status,
        metadata={"disaster_type": report.disaster_type, "reference": report.reference},
        request=request,
    )

    if submit_now:
        submit_report(report, user=citizen, request=request)

    return report, True


def cancel_report(report: DisasterReport, *, user, reason: str = "", request=None) -> DisasterReport:
    """A citizen withdraws their own report, or an administrator closes it."""
    role = "CITIZEN" if user.is_citizen else "ADMIN"
    previous = report.apply_transition(CaseStatus.CANCELLED, role=role)
    record_event(
        entity=report,
        action=AuditAction.REPORT_CANCELLED,
        user=user,
        previous_state=previous,
        new_state=report.status,
        metadata={"reason": reason},
        request=request,
    )
    return report


def change_status(report: DisasterReport, *, target: str, user, reason: str = "", request=None):
    """
    Administrator-driven status change.

    Even here the state machine is authoritative - an administrator gets no
    shortcut past `assert_transition`. What they get is the *right* to make moves
    that citizens and officers cannot.
    """
    role = "ADMIN" if (user.is_admin_role or user.is_superuser) else user.role
    previous = report.apply_transition(target, role=role)
    record_event(
        entity=report,
        action=AuditAction.REPORT_UPDATED,
        user=user,
        previous_state=previous,
        new_state=report.status,
        metadata={"reason": reason, "manual_override": True},
        request=request,
    )
    return report


def attach_photo(*, report: DisasterReport, image, caption: str = "", user=None, request=None):
    """Save one evidence photo and pull any usable GPS metadata out of it."""
    photo = ReportPhoto(report=report, image=image, caption=caption, uploaded_by=user)
    latitude, longitude, captured_at = extract_exif_location(image)
    photo.exif_latitude = latitude
    photo.exif_longitude = longitude
    photo.captured_at = captured_at
    photo.save()

    record_event(
        entity=report,
        action=AuditAction.EVIDENCE_UPLOADED,
        user=user,
        metadata={"photo_id": photo.pk, "has_exif_gps": latitude is not None},
        request=request,
    )
    try:
        from damage_ai.services import assess_photo

        assess_photo(photo)
    except Exception:  # noqa: BLE001 - AI must never block photo upload
        logger.exception("AI damage assessment integration failed for photo %s", photo.pk)
    return photo


def extract_exif_location(image_file):
    """
    Pull GPS coordinates and capture time out of a photo's EXIF block.

    Section 11 lists photo metadata as a location source. It is a *hint*, not a
    substitute for the reported location - phones strip EXIF, users edit files,
    and metadata is trivially forged. So this returns `(None, None, None)` on any
    problem and never raises: a photo without metadata is perfectly normal.
    """
    try:
        from PIL import Image
        from PIL.ExifTags import GPSTAGS, TAGS

        image_file.seek(0)
        with Image.open(image_file) as image:
            exif = image.getexif()
            if not exif:
                return None, None, None

            tags = {TAGS.get(key, key): value for key, value in exif.items()}

            captured_at = None
            raw_datetime = tags.get("DateTime") or tags.get("DateTimeOriginal")
            if raw_datetime:
                from datetime import datetime

                try:
                    naive = datetime.strptime(str(raw_datetime), "%Y:%m:%d %H:%M:%S")
                    captured_at = timezone.make_aware(naive, timezone.get_current_timezone())
                except ValueError:
                    captured_at = None

            gps_block = exif.get_ifd(0x8825)
            if not gps_block:
                return None, None, captured_at

            gps = {GPSTAGS.get(key, key): value for key, value in gps_block.items()}
            latitude = _dms_to_decimal(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
            longitude = _dms_to_decimal(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
            return latitude, longitude, captured_at
    except Exception:  # noqa: BLE001
        return None, None, None
    finally:
        try:
            image_file.seek(0)
        except Exception:  # noqa: BLE001
            pass


def _dms_to_decimal(dms, reference):
    """EXIF stores degrees/minutes/seconds; databases and maps want a decimal."""
    if not dms or reference is None:
        return None
    try:
        degrees, minutes, seconds = (float(part) for part in dms)
        decimal = degrees + minutes / 60 + seconds / 3600
        if str(reference).upper() in {"S", "W"}:
            decimal = -decimal
        return round(decimal, 6)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


__all__ = [
    "submit_report",
    "create_report",
    "cancel_report",
    "change_status",
    "attach_photo",
    "extract_exif_location",
    "InvalidTransition",
]
