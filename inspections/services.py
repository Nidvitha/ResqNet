"""
Inspection services - the officer's workflow, enforced.

Section 16 defines the sequence:

    ASSIGNED -> INSPECTION_STARTED -> INSPECTION_COMPLETED -> OFFICER_SIGNED

and one hard rule: *"Only the assigned officer or appropriately authorized
personnel should be able to modify the inspection."* That rule is enforced twice
over - once by the permission class before the view runs, and once here in
`_assert_can_modify`, so it holds even if the inspection is touched by a
management command or another service that never passed through a view.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from audit.models import AuditAction
from audit.services import record_event
from dispatch.models import Assignment, AssignmentStatus
from reports.states import CaseStatus, InvalidTransition

from .models import Inspection, InspectionPhoto, InspectionStatus

logger = logging.getLogger("resqnet.inspections")


class InspectionPermissionError(Exception):
    """Raised when someone other than the assigned officer tries to modify."""


def _assert_can_modify(inspection: Inspection, user) -> None:
    if user.is_admin_role or user.is_superuser:
        return
    if inspection.officer_id != user.id:
        raise InspectionPermissionError(
            "This inspection belongs to a different officer and cannot be modified."
        )
    if not inspection.is_editable:
        raise InvalidTransition("This inspection has been signed off and is now final.")


@transaction.atomic
def start_inspection(*, assignment: Assignment, user, request=None) -> Inspection:
    """
    Begin an inspection for an assigned case.

    Returns the existing inspection if one is already open, so an officer whose
    phone lost connection mid-form does not create a second record when they
    reopen the case.
    """
    if assignment.officer_id != user.id and not (user.is_admin_role or user.is_superuser):
        raise InspectionPermissionError("This case is assigned to a different officer.")

    report = assignment.report
    existing = getattr(report, "inspection", None)
    if existing is not None:
        _assert_can_modify(existing, user)
        return existing

    inspection = Inspection.objects.create(
        report=report,
        assignment=assignment,
        officer=assignment.officer,
        status=InspectionStatus.STARTED,
        started_at=timezone.now(),
    )

    if assignment.status in {AssignmentStatus.ASSIGNED, AssignmentStatus.ACCEPTED}:
        assignment.status = AssignmentStatus.IN_PROGRESS
        if assignment.accepted_at is None:
            assignment.accepted_at = timezone.now()
        assignment.save(update_fields=["status", "accepted_at"])

    previous = report.status
    if report.status == CaseStatus.ASSIGNED:
        previous = report.apply_transition(CaseStatus.INSPECTION_PENDING, role="FIELD_OFFICER")

    record_event(
        entity=report,
        action=AuditAction.INSPECTION_STARTED,
        user=user,
        previous_state=previous,
        new_state=report.status,
        metadata={"inspection_id": inspection.pk},
        request=request,
    )
    return inspection


@transaction.atomic
def update_inspection(*, inspection: Inspection, data: dict, user, request=None) -> Inspection:
    """Save partial checklist progress without completing the inspection."""
    _assert_can_modify(inspection, user)

    editable_fields = {
        "structural_damage", "roof_damage", "wall_damage", "foundation_damage",
        "electrical_damage", "water_damage", "household_damage",
        "people_affected", "is_habitable", "requires_immediate_relief",
        "estimated_damage_value", "remarks",
        "inspection_latitude", "inspection_longitude",
        "completed_offline", "client_completed_at",
    }
    changed = []
    for field, value in data.items():
        if field in editable_fields:
            setattr(inspection, field, value)
            changed.append(field)

    if changed:
        inspection.save(update_fields=changed + ["updated_at"])
    return inspection


@transaction.atomic
def complete_inspection(*, inspection: Inspection, user, request=None) -> Inspection:
    """
    Mark the checklist finished. Still editable until it is signed.

    Section 27 asks for a test that an inspection "cannot be completed twice
    incorrectly" - re-completing an already-completed inspection is refused here
    rather than silently overwriting the original completion time.
    """
    _assert_can_modify(inspection, user)

    if inspection.status != InspectionStatus.STARTED:
        raise InvalidTransition(
            f"Inspection is already {inspection.get_status_display().lower()}."
        )
    if not inspection.has_any_damage() and not inspection.remarks.strip():
        raise InvalidTransition(
            "Record at least one damage assessment, or explain in the remarks why "
            "no damage was found."
        )

    inspection.status = InspectionStatus.COMPLETED
    inspection.completed_at = timezone.now()
    inspection.save(update_fields=["status", "completed_at", "updated_at"])

    record_event(
        entity=inspection.report,
        action=AuditAction.INSPECTION_COMPLETED,
        user=user,
        metadata={
            "inspection_id": inspection.pk,
            "severity": inspection.severity_map(),
            "people_affected": inspection.people_affected,
            "habitable": inspection.is_habitable,
            "duration_minutes": inspection.duration_minutes,
        },
        request=request,
    )
    return inspection


@transaction.atomic
def sign_off_inspection(*, inspection: Inspection, signature_name: str, user, request=None):
    """
    The officer signs off. This is the point of no return.

    Sign-off does three things in one transaction: freezes the inspection, moves
    the case to INSPECTED, and runs the compensation engine so an administrator
    finds a fully costed case waiting for review rather than a to-do item.
    """
    from compensation.services import calculate_compensation

    _assert_can_modify(inspection, user)

    if inspection.status != InspectionStatus.COMPLETED:
        raise InvalidTransition("Complete the inspection checklist before signing off.")
    if not signature_name.strip():
        raise InvalidTransition("A signature name is required to sign off.")

    inspection.status = InspectionStatus.SIGNED
    inspection.signed_at = timezone.now()
    inspection.signature_name = signature_name.strip()
    inspection.save(update_fields=["status", "signed_at", "signature_name", "updated_at"])

    assignment = inspection.assignment
    assignment.status = AssignmentStatus.COMPLETED
    assignment.completed_at = timezone.now()
    assignment.save(update_fields=["status", "completed_at"])

    report = inspection.report
    previous = report.status
    if report.status == CaseStatus.INSPECTION_PENDING:
        previous = report.apply_transition(CaseStatus.INSPECTED, role="FIELD_OFFICER")

    record_event(
        entity=report,
        action=AuditAction.OFFICER_SIGNED,
        user=user,
        previous_state=previous,
        new_state=report.status,
        metadata={"inspection_id": inspection.pk, "signature": inspection.signature_name},
        request=request,
    )

    calculate_compensation(inspection=inspection, user=user, request=request)

    logger.info("Inspection signed for %s by %s", report.reference, user.username)
    return inspection


def attach_inspection_photo(*, inspection: Inspection, image, caption="", damage_area="",
                            latitude=None, longitude=None, user=None, request=None):
    """Attach verified evidence. Blocked once the inspection is signed."""
    _assert_can_modify(inspection, user)

    photo = InspectionPhoto.objects.create(
        inspection=inspection,
        image=image,
        caption=caption,
        damage_area=damage_area,
        captured_latitude=latitude,
        captured_longitude=longitude,
    )
    record_event(
        entity=inspection.report,
        action=AuditAction.EVIDENCE_UPLOADED,
        user=user,
        metadata={"inspection_photo_id": photo.pk, "verified": True},
        request=request,
    )
    return photo
