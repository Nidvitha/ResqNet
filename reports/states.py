"""
The ResQNet case state machine (Section 6).

Why a state machine?
--------------------
A case is not a bag of editable fields - it is a process that moves forward in a
defined order. Without an explicit machine, any endpoint could set
`status = "APPROVED"` and skip inspection entirely. Section 6 is blunt about
this: *"Do not allow arbitrary status changes from the API."*

So the rules live here, in one readable table, rather than being scattered across
views. Every status change in the platform goes through `transition()`, which
answers two questions:

1. Is this move legal from the current state?  (the `ALLOWED_TRANSITIONS` map)
2. Is this *user* allowed to make it?           (the `TRANSITION_ROLES` map)

Both must pass. That is how "an admin cannot approve an uninspected case"
becomes a fact of the system instead of a code review comment.
"""

from __future__ import annotations

from django.db import models


class CaseStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted"
    TRIAGED = "TRIAGED", "Triaged"
    ASSIGNED = "ASSIGNED", "Assigned to officer"
    INSPECTION_PENDING = "INSPECTION_PENDING", "Inspection in progress"
    INSPECTED = "INSPECTED", "Inspected"
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending approval"
    APPROVED = "APPROVED", "Approved"
    PAYOUT_PENDING = "PAYOUT_PENDING", "Payout pending"
    PAYOUT_INITIATED = "PAYOUT_INITIATED", "Payout initiated"
    PAYOUT_COMPLETED = "PAYOUT_COMPLETED", "Payout completed"

    REJECTED = "REJECTED", "Rejected"
    NEEDS_REVIEW = "NEEDS_REVIEW", "Needs review"
    CANCELLED = "CANCELLED", "Cancelled"


#: The only legal moves. Anything not listed here is impossible, by construction.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    CaseStatus.DRAFT: {CaseStatus.SUBMITTED, CaseStatus.CANCELLED},
    CaseStatus.SUBMITTED: {CaseStatus.TRIAGED, CaseStatus.CANCELLED, CaseStatus.REJECTED},
    CaseStatus.TRIAGED: {CaseStatus.ASSIGNED, CaseStatus.CANCELLED, CaseStatus.REJECTED},
    CaseStatus.ASSIGNED: {
        CaseStatus.INSPECTION_PENDING,
        CaseStatus.TRIAGED,          # returned to the pool for re-assignment
        CaseStatus.CANCELLED,
    },
    CaseStatus.INSPECTION_PENDING: {CaseStatus.INSPECTED, CaseStatus.CANCELLED},
    CaseStatus.INSPECTED: {CaseStatus.PENDING_APPROVAL, CaseStatus.NEEDS_REVIEW},
    CaseStatus.PENDING_APPROVAL: {
        CaseStatus.APPROVED,
        CaseStatus.REJECTED,
        CaseStatus.NEEDS_REVIEW,
    },
    CaseStatus.APPROVED: {CaseStatus.PAYOUT_PENDING},
    CaseStatus.PAYOUT_PENDING: {CaseStatus.PAYOUT_INITIATED},
    CaseStatus.PAYOUT_INITIATED: {CaseStatus.PAYOUT_COMPLETED},
    CaseStatus.PAYOUT_COMPLETED: set(),          # terminal
    CaseStatus.REJECTED: {CaseStatus.NEEDS_REVIEW},
    CaseStatus.NEEDS_REVIEW: {
        CaseStatus.ASSIGNED,                      # send back for re-inspection
        CaseStatus.PENDING_APPROVAL,              # corrected, ready again
        CaseStatus.APPROVED,                      # concerns resolved without re-inspection
        CaseStatus.REJECTED,
    },
    CaseStatus.CANCELLED: set(),                  # terminal
}

#: Which roles may perform each transition. Keyed by destination state.
TRANSITION_ROLES: dict[str, set[str]] = {
    CaseStatus.SUBMITTED: {"CITIZEN", "ADMIN"},
    CaseStatus.TRIAGED: {"SYSTEM", "ADMIN"},
    CaseStatus.ASSIGNED: {"SYSTEM", "ADMIN"},
    CaseStatus.INSPECTION_PENDING: {"FIELD_OFFICER", "ADMIN"},
    CaseStatus.INSPECTED: {"FIELD_OFFICER", "ADMIN"},
    CaseStatus.PENDING_APPROVAL: {"SYSTEM", "FIELD_OFFICER", "ADMIN"},
    CaseStatus.APPROVED: {"ADMIN"},
    CaseStatus.REJECTED: {"ADMIN"},
    CaseStatus.NEEDS_REVIEW: {"ADMIN"},
    CaseStatus.PAYOUT_PENDING: {"SYSTEM", "ADMIN"},
    CaseStatus.PAYOUT_INITIATED: {"ADMIN"},
    CaseStatus.PAYOUT_COMPLETED: {"ADMIN"},
    CaseStatus.CANCELLED: {"CITIZEN", "ADMIN"},
}

#: States in which a citizen may still edit their own report.
CITIZEN_EDITABLE_STATES = {CaseStatus.DRAFT}

#: States that count as "case closed" for analytics.
TERMINAL_STATES = {CaseStatus.PAYOUT_COMPLETED, CaseStatus.CANCELLED, CaseStatus.REJECTED}

#: The happy-path order, used to render progress bars in the portals.
PROGRESS_SEQUENCE = [
    CaseStatus.SUBMITTED,
    CaseStatus.TRIAGED,
    CaseStatus.ASSIGNED,
    CaseStatus.INSPECTION_PENDING,
    CaseStatus.INSPECTED,
    CaseStatus.PENDING_APPROVAL,
    CaseStatus.APPROVED,
    CaseStatus.PAYOUT_PENDING,
    CaseStatus.PAYOUT_COMPLETED,
]


class InvalidTransition(Exception):
    """Raised when a status change is not permitted. Views turn this into a 400."""


def can_transition(current: str, target: str) -> bool:
    """Is the move from `current` to `target` in the allowed table?"""
    return target in ALLOWED_TRANSITIONS.get(current, set())


def role_may_transition(role: str, target: str) -> bool:
    """May a user with this role drive a case into `target`?"""
    allowed = TRANSITION_ROLES.get(target)
    if allowed is None:
        return False
    return role in allowed


def assert_transition(current: str, target: str, role: str = "SYSTEM") -> None:
    """
    Raise `InvalidTransition` unless the move is legal for this role.

    `role="SYSTEM"` is used by internal services (triage, dispatch) that act on
    the platform's behalf rather than on behalf of a logged-in person.
    """
    if current == target:
        raise InvalidTransition(f"Case is already in state {target}.")
    if not can_transition(current, target):
        raise InvalidTransition(
            f"Cannot move a case from {current} to {target}. "
            f"Allowed from {current}: {sorted(ALLOWED_TRANSITIONS.get(current, set())) or 'nothing (terminal state)'}."
        )
    if not role_may_transition(role, target):
        raise InvalidTransition(f"Role {role} is not permitted to move a case to {target}.")


def progress_percent(status: str) -> int:
    """Rough completion percentage, for the citizen's status tracker."""
    if status in {CaseStatus.CANCELLED, CaseStatus.REJECTED}:
        return 100
    if status == CaseStatus.DRAFT:
        return 0
    if status == CaseStatus.NEEDS_REVIEW:
        return 60
    try:
        index = PROGRESS_SEQUENCE.index(status)
    except ValueError:
        return 0
    return round((index + 1) / len(PROGRESS_SEQUENCE) * 100)
