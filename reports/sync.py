"""
Offline synchronisation for citizen reports (Section 13).

The problem this solves
-----------------------
A citizen fills in a damage report while standing in a flooded street with no
signal. The browser stores it in IndexedDB. Two hours later the network returns
and the queued request is replayed - possibly more than once, because the phone
cannot tell whether the first attempt reached the server before the connection
dropped.

Without protection, that produces duplicate cases, duplicate inspections, and
duplicate relief payments.

The fix is an **idempotency key**: a unique id the client generates *once*, when
the citizen presses Save, and reuses on every retry of that same submission. The
server records the key. A second request carrying a key it has already seen gets
the original case back with `duplicate: true`, and nothing new is written.

Section 13 asks for "idempotency keys or another appropriate mechanism"; this is
the mechanism.
"""

from __future__ import annotations

import logging

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from audit.models import AuditAction
from audit.services import record_event

from .serializers import ReportCreateSerializer, ReportDetailSerializer
from .services import create_report

logger = logging.getLogger("resqnet.sync")

MAX_BATCH_SIZE = 50


class SyncThrottle(ScopedRateThrottle):
    scope = "sync"


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([SyncThrottle])
def sync_reports(request):
    """
    Upload a batch of reports queued while the device was offline.

    Expects `{"reports": [ {...}, {...} ]}` where each entry carries an
    `idempotency_key`.

    Each report is processed independently: one malformed entry is reported back
    with its error while the rest still succeed. A single bad record in the queue
    must never block a citizen's other reports from reaching the platform.
    """
    if not request.user.is_citizen:
        return Response(
            {"detail": "Only citizens can sync damage reports."},
            status=status.HTTP_403_FORBIDDEN,
        )

    payload = request.data.get("reports")
    if not isinstance(payload, list):
        return Response(
            {"detail": "Expected a 'reports' array."}, status=status.HTTP_400_BAD_REQUEST
        )
    if len(payload) > MAX_BATCH_SIZE:
        return Response(
            {"detail": f"Sync at most {MAX_BATCH_SIZE} reports per request."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    created, duplicates, failed = [], [], []

    for index, entry in enumerate(payload):
        client_id = (entry or {}).get("idempotency_key", "")
        if not client_id:
            failed.append({"index": index, "errors": {"idempotency_key": ["This field is required for sync."]}})
            continue

        entry = dict(entry)
        entry["created_offline"] = True

        serializer = ReportCreateSerializer(data=entry)
        if not serializer.is_valid():
            failed.append({"index": index, "idempotency_key": client_id, "errors": serializer.errors})
            continue

        try:
            report, was_created = create_report(
                citizen=request.user, validated_data=serializer.validated_data, request=request
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Sync failed for key %s", client_id)
            failed.append({"index": index, "idempotency_key": client_id, "errors": {"detail": [str(exc)]}})
            continue

        summary = {
            "idempotency_key": client_id,
            "id": report.id,
            "reference": report.reference,
            "status": report.status,
        }
        (created if was_created else duplicates).append(summary)

    record_event(
        entity_type="SyncBatch",
        entity_id=request.user.pk,
        action=AuditAction.OFFLINE_SYNC,
        user=request.user,
        metadata={
            "submitted": len(payload),
            "created": len(created),
            "duplicates": len(duplicates),
            "failed": len(failed),
        },
        request=request,
    )

    return Response(
        {
            "synced": len(created),
            "duplicates": len(duplicates),
            "failed": len(failed),
            "created": created,
            "duplicate_records": duplicates,
            "errors": failed,
        },
        status=status.HTTP_207_MULTI_STATUS if failed else status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def sync_status(request):
    """
    A lightweight snapshot the client uses to reconcile its local database.

    The device compares this against IndexedDB after reconnecting and knows
    which locally-cached cases have moved on without it.
    """
    reports = request.user.reports.order_by("-created_at")[:100]
    return Response(
        {
            "server_time": timezone.now(),
            "report_count": request.user.reports.count(),
            "reports": ReportDetailSerializer(
                reports, many=True, context={"request": request}
            ).data,
        }
    )
