"""
Audit service - the single way anything gets written to the audit trail.

A *service* is plain Python that holds business logic, sitting between models and
views (Section 28). Views stay thin and readable; the rules live somewhere they
can be tested directly and reused from anywhere - an API view, a management
command, or another service.

Every app calls `record_event(...)` rather than creating `AuditEvent` rows
itself. That keeps the recording format consistent and means the day we add
hash-chaining, we change one function instead of thirty call sites.
"""

from __future__ import annotations

import logging
from typing import Any

from .models import AuditEvent

logger = logging.getLogger("resqnet.audit")


def _client_ip(request) -> str | None:
    """Best-effort client IP. Only trusted behind a properly configured proxy."""
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def record_event(
    *,
    entity: Any = None,
    entity_type: str | None = None,
    entity_id: Any = None,
    entity_label: str = "",
    action: str,
    user=None,
    previous_state: str = "",
    new_state: str = "",
    metadata: dict | None = None,
    request=None,
) -> AuditEvent | None:
    """
    Append one audit event.

    Pass either a model `entity` (type, id and label are derived from it) or the
    `entity_type` / `entity_id` pair directly.

    Auditing must never break the operation it is recording - if a citizen's
    report saved successfully, an audit failure should be logged loudly but must
    not turn their submission into an error. So this function swallows its own
    exceptions and returns None.
    """
    if entity is not None:
        entity_type = entity_type or entity.__class__.__name__
        entity_id = entity_id if entity_id is not None else entity.pk
        if not entity_label:
            entity_label = str(
                getattr(entity, "reference", None)
                or getattr(entity, "report_id", None)
                or entity
            )[:120]

    if user is None and request is not None:
        candidate = getattr(request, "user", None)
        if candidate is not None and candidate.is_authenticated:
            user = candidate

    try:
        return AuditEvent.objects.create(
            entity_type=entity_type or "Unknown",
            entity_id=str(entity_id) if entity_id is not None else "",
            entity_label=entity_label or "",
            action=action,
            performed_by=user,
            performed_by_username=getattr(user, "username", "") or "",
            performed_by_role=getattr(user, "role", "") or "",
            previous_state=previous_state or "",
            new_state=new_state or "",
            metadata=metadata or {},
            ip_address=_client_ip(request),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write audit event %s for %s", action, entity_type)
        return None


def history_for(entity) -> list[AuditEvent]:
    """Full chronological history of one object, oldest first."""
    return list(
        AuditEvent.objects.filter(
            entity_type=entity.__class__.__name__,
            entity_id=str(entity.pk),
        ).order_by("timestamp", "id")
    )
