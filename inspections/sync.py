"""
Offline synchronisation for field inspections (Section 13).

The officer's flow is the harder of the two offline cases:

    Download assignments -> enter disaster zone -> no internet ->
    complete inspection -> IndexedDB -> connection returns -> sync

Unlike a citizen report, an inspection is not a fresh row - it attaches to an
assignment that already exists, and it may end in a sign-off that starts the
compensation engine. So a replayed sync must be safe even when the first attempt
partly succeeded.

Two protections make that true:

* Each queued inspection carries an `idempotency_key`. A key already seen from
  the same officer is reported as a duplicate, not processed again.
* An inspection that is already SIGNED is left alone. Sign-off is irreversible,
  and re-running it would recalculate compensation on a case an administrator may
  already have approved.
"""

from __future__ import annotations

import logging

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from accounts.permissions import IsFieldOfficerOrAdmin
from audit.models import AuditAction
from audit.services import record_event
from dispatch.models import Assignment
from reports.states import InvalidTransition

from .models import Inspection, InspectionStatus
from .serializers import InspectionUpdateSerializer
from .services import (
    InspectionPermissionError,
    complete_inspection,
    sign_off_inspection,
    start_inspection,
    update_inspection,
)

logger = logging.getLogger("resqnet.sync")

MAX_BATCH_SIZE = 25


class SyncThrottle(ScopedRateThrottle):
    scope = "sync"


@api_view(["POST"])
@permission_classes([IsFieldOfficerOrAdmin])
@throttle_classes([SyncThrottle])
def sync_inspections(request):
    """
    Upload inspections completed offline.

    Expects `{"inspections": [{assignment_id, idempotency_key, signature_name?, ...}]}`.
    Including `signature_name` signs the inspection off as part of the sync;
    omitting it leaves the inspection completed but unsigned.
    """
    payload = request.data.get("inspections")
    if not isinstance(payload, list):
        return Response(
            {"detail": "Expected an 'inspections' array."}, status=status.HTTP_400_BAD_REQUEST
        )
    if len(payload) > MAX_BATCH_SIZE:
        return Response(
            {"detail": f"Sync at most {MAX_BATCH_SIZE} inspections per request."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    synced, duplicates, failed = [], [], []

    for index, entry in enumerate(payload):
        entry = dict(entry or {})
        client_key = entry.pop("idempotency_key", "")
        assignment_id = entry.pop("assignment_id", None)
        signature_name = entry.pop("signature_name", "").strip()

        if not client_key:
            failed.append({"index": index, "errors": {"idempotency_key": ["Required for sync."]}})
            continue
        if not assignment_id:
            failed.append({"index": index, "errors": {"assignment_id": ["Required for sync."]}})
            continue

        # Already processed on an earlier attempt.
        existing = Inspection.objects.filter(
            officer=request.user, idempotency_key=client_key
        ).first()
        if existing is not None:
            duplicates.append(
                {
                    "idempotency_key": client_key,
                    "inspection_id": existing.id,
                    "reference": existing.report.reference,
                    "status": existing.status,
                }
            )
            continue

        try:
            assignment = Assignment.objects.select_related("report").get(pk=assignment_id)
        except Assignment.DoesNotExist:
            failed.append(
                {"index": index, "idempotency_key": client_key,
                 "errors": {"assignment_id": ["Assignment not found."]}}
            )
            continue

        serializer = InspectionUpdateSerializer(
            data=entry,
            partial=True,
            context={"category": assignment.report.damage_category},
        )
        if not serializer.is_valid():
            failed.append(
                {"index": index, "idempotency_key": client_key, "errors": serializer.errors}
            )
            continue

        try:
            inspection = start_inspection(
                assignment=assignment, user=request.user, request=request
            )

            # Never re-process a case that is already final.
            if inspection.status == InspectionStatus.SIGNED:
                duplicates.append(
                    {
                        "idempotency_key": client_key,
                        "inspection_id": inspection.id,
                        "reference": inspection.report.reference,
                        "status": inspection.status,
                        "note": "Already signed off; sync ignored.",
                    }
                )
                continue

            update_inspection(
                inspection=inspection,
                data=serializer.validated_data,
                user=request.user,
                request=request,
            )

            inspection.idempotency_key = client_key
            inspection.completed_offline = True
            inspection.synced_at = timezone.now()
            inspection.save(update_fields=["idempotency_key", "completed_offline", "synced_at"])

            if inspection.status == InspectionStatus.STARTED:
                complete_inspection(inspection=inspection, user=request.user, request=request)
            if signature_name:
                sign_off_inspection(
                    inspection=inspection,
                    signature_name=signature_name,
                    user=request.user,
                    request=request,
                )

            inspection.refresh_from_db()
            synced.append(
                {
                    "idempotency_key": client_key,
                    "inspection_id": inspection.id,
                    "reference": inspection.report.reference,
                    "status": inspection.status,
                    "case_status": inspection.report.status,
                }
            )
        except (InspectionPermissionError, InvalidTransition) as exc:
            failed.append(
                {"index": index, "idempotency_key": client_key, "errors": {"detail": [str(exc)]}}
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Inspection sync failed for key %s", client_key)
            failed.append(
                {"index": index, "idempotency_key": client_key, "errors": {"detail": [str(exc)]}}
            )

    record_event(
        entity_type="InspectionSyncBatch",
        entity_id=request.user.pk,
        action=AuditAction.OFFLINE_SYNC,
        user=request.user,
        metadata={
            "submitted": len(payload),
            "synced": len(synced),
            "duplicates": len(duplicates),
            "failed": len(failed),
        },
        request=request,
    )

    return Response(
        {
            "synced": len(synced),
            "duplicates": len(duplicates),
            "failed": len(failed),
            "results": synced,
            "duplicate_records": duplicates,
            "errors": failed,
        },
        status=status.HTTP_207_MULTI_STATUS if failed else status.HTTP_200_OK,
    )
