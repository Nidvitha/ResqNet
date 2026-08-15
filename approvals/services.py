"""
Approval and payout services.

Section 6 gives two rules that this module exists to make unbreakable:

    * "Admin cannot approve an uninspected case."
    * "Payout cannot be completed before approval."

Both are enforced structurally rather than by convention. `approve_case()`
refuses to run unless a *signed* inspection and a calculated claim exist, and
`complete_payout()` walks the case through PAYOUT_PENDING -> PAYOUT_INITIATED ->
PAYOUT_COMPLETED using the state machine, so there is no route that skips a step.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from audit.models import AuditAction
from audit.services import record_event
from compensation.models import CompensationStatus
from compensation.services import adjust_claim
from inspections.models import InspectionStatus
from reports.models import DisasterReport
from reports.states import CaseStatus, InvalidTransition

from .models import Approval, ApprovalDecision, Payout, PayoutStatus

logger = logging.getLogger("resqnet.approvals")


def _require_reviewable_case(report: DisasterReport):
    """Guard shared by every decision path: is there anything legitimate to decide on?"""
    inspection = getattr(report, "inspection", None)
    if inspection is None:
        raise InvalidTransition("This case has not been inspected yet and cannot be decided.")
    if inspection.status != InspectionStatus.SIGNED:
        raise InvalidTransition("The inspecting officer has not signed off yet.")

    claim = getattr(report, "compensation_claim", None)
    if claim is None:
        raise InvalidTransition("No compensation has been calculated for this case.")
    return inspection, claim


@transaction.atomic
def approve_case(*, report: DisasterReport, user, reason: str,
                 adjusted_amount: Decimal | None = None, request=None) -> Approval:
    """
    Approve a case and open its payout.

    An administrator may authorise a different figure from the calculated one,
    but only with a written reason - `adjust_claim` enforces that.
    """
    inspection, claim = _require_reviewable_case(report)

    if report.status not in {CaseStatus.PENDING_APPROVAL, CaseStatus.NEEDS_REVIEW}:
        raise InvalidTransition(
            f"A case in state {report.status} cannot be approved. "
            "It must be pending approval or under review."
        )
    if not reason.strip():
        raise InvalidTransition("A written reason is required for every approval decision.")

    if adjusted_amount is not None and Decimal(str(adjusted_amount)) != claim.calculated_amount:
        adjust_claim(claim=claim, amount=adjusted_amount, reason=reason, user=user, request=request)
        claim.refresh_from_db()
    else:
        claim.approved_amount = claim.calculated_amount
        claim.status = CompensationStatus.APPROVED
        claim.save(update_fields=["approved_amount", "status", "updated_at"])

    approval = Approval.objects.create(
        report=report,
        claim=claim,
        decision=ApprovalDecision.APPROVED,
        approved_amount=claim.payable_amount,
        reason=reason.strip(),
        decided_by=user,
    )

    previous = report.apply_transition(CaseStatus.APPROVED, role="ADMIN")
    record_event(
        entity=report,
        action=AuditAction.ADMIN_APPROVED,
        user=user,
        previous_state=previous,
        new_state=report.status,
        metadata={
            "approved_amount": str(claim.payable_amount),
            "calculated_amount": str(claim.calculated_amount),
            "adjusted": claim.was_adjusted,
            "reason": reason,
        },
        request=request,
    )

    # An approved case immediately becomes a pending payout - there is no state
    # in which relief is approved but nobody is tracking the money.
    payout = _open_payout(report=report, approval=approval, user=user, request=request)
    logger.info("Approved %s for Rs %s (payout %s)", report.reference, payout.amount, payout.reference_number)
    return approval


@transaction.atomic
def reject_case(*, report: DisasterReport, user, reason: str, request=None) -> Approval:
    """Refuse relief. A reason is mandatory, since the citizen is entitled to know why."""
    _require_reviewable_case(report)

    if report.status not in {CaseStatus.PENDING_APPROVAL, CaseStatus.NEEDS_REVIEW}:
        raise InvalidTransition(f"A case in state {report.status} cannot be rejected.")
    if not reason.strip():
        raise InvalidTransition("A written reason is required when rejecting a case.")

    claim = getattr(report, "compensation_claim", None)
    if claim:
        claim.status = CompensationStatus.REJECTED
        claim.save(update_fields=["status", "updated_at"])

    approval = Approval.objects.create(
        report=report,
        claim=claim,
        decision=ApprovalDecision.REJECTED,
        reason=reason.strip(),
        decided_by=user,
    )

    previous = report.apply_transition(CaseStatus.REJECTED, role="ADMIN")
    record_event(
        entity=report,
        action=AuditAction.ADMIN_REJECTED,
        user=user,
        previous_state=previous,
        new_state=report.status,
        metadata={"reason": reason},
        request=request,
    )
    return approval


@transaction.atomic
def request_review(*, report: DisasterReport, user, reason: str, request=None) -> Approval:
    """
    Send a case back for re-inspection.

    Section 6 allows a rejected case to enter NEEDS_REVIEW, so this accepts both
    PENDING_APPROVAL and REJECTED as starting points.
    """
    if report.status not in {CaseStatus.PENDING_APPROVAL, CaseStatus.INSPECTED, CaseStatus.REJECTED}:
        raise InvalidTransition(f"A case in state {report.status} cannot be sent for review.")
    if not reason.strip():
        raise InvalidTransition("Explain what the officer needs to re-check.")

    approval = Approval.objects.create(
        report=report,
        claim=getattr(report, "compensation_claim", None),
        decision=ApprovalDecision.REVIEW_REQUESTED,
        reason=reason.strip(),
        decided_by=user,
    )

    previous = report.apply_transition(CaseStatus.NEEDS_REVIEW, role="ADMIN")
    record_event(
        entity=report,
        action=AuditAction.REVIEW_REQUESTED,
        user=user,
        previous_state=previous,
        new_state=report.status,
        metadata={"reason": reason},
        request=request,
    )
    return approval


def _open_payout(*, report: DisasterReport, approval: Approval, user, request=None) -> Payout:
    """Create the payout record and move the case to PAYOUT_PENDING."""
    payout, _ = Payout.objects.update_or_create(
        report=report,
        defaults={
            "approval": approval,
            "amount": approval.approved_amount or Decimal("0"),
            "status": PayoutStatus.PENDING,
            "beneficiary_name": report.citizen.get_full_name() or report.citizen.username,
        },
    )
    report.apply_transition(CaseStatus.PAYOUT_PENDING, role="SYSTEM")
    return payout


@transaction.atomic
def initiate_payout(*, report: DisasterReport, user, method: str = "BANK_TRANSFER", request=None):
    """
    Mark the disbursement as started.

    Simulated: no bank is contacted. The record exists so the citizen can see
    honest progress and an auditor can see who pressed the button and when.
    """
    payout = getattr(report, "payout", None)
    if payout is None:
        raise InvalidTransition("This case has no approved payout.")
    if payout.status != PayoutStatus.PENDING:
        raise InvalidTransition(f"Payout is already {payout.get_status_display().lower()}.")

    payout.status = PayoutStatus.INITIATED
    payout.payment_method = method
    payout.initiated_at = timezone.now()
    payout.initiated_by = user
    payout.save(update_fields=["status", "payment_method", "initiated_at", "initiated_by"])

    previous = report.apply_transition(CaseStatus.PAYOUT_INITIATED, role="ADMIN")
    record_event(
        entity=report,
        action=AuditAction.PAYOUT_INITIATED,
        user=user,
        previous_state=previous,
        new_state=report.status,
        metadata={
            "amount": str(payout.amount),
            "reference": payout.reference_number,
            "method": method,
            "simulated": payout.is_simulated,
        },
        request=request,
    )
    return payout


@transaction.atomic
def complete_payout(*, report: DisasterReport, user, request=None) -> Payout:
    """Close the case. Only reachable from PAYOUT_INITIATED - never straight from approval."""
    payout = getattr(report, "payout", None)
    if payout is None:
        raise InvalidTransition("This case has no payout to complete.")
    if payout.status != PayoutStatus.INITIATED:
        raise InvalidTransition("A payout must be initiated before it can be completed.")

    payout.status = PayoutStatus.COMPLETED
    payout.completed_at = timezone.now()
    payout.save(update_fields=["status", "completed_at"])

    previous = report.apply_transition(CaseStatus.PAYOUT_COMPLETED, role="ADMIN")
    record_event(
        entity=report,
        action=AuditAction.PAYOUT_COMPLETED,
        user=user,
        previous_state=previous,
        new_state=report.status,
        metadata={
            "amount": str(payout.amount),
            "reference": payout.reference_number,
            "simulated": payout.is_simulated,
        },
        request=request,
    )
    logger.info("Payout completed for %s: Rs %s", report.reference, payout.amount)
    return payout
