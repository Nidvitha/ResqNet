"""
The compensation engine.

Section 17 lists the six steps:

    1. Read verified inspection data.   4. Produce an itemised breakdown.
    2. Match applicable relief rules.   5. Store the result.
    3. Calculate eligible amounts.      6. Allow admin review.

Two properties matter more than anything else here.

**Deterministic.** The same inspection always produces the same number. No
randomness, no clock, no external service. Run it twice, get the same answer -
which is why it can be unit-tested against a fixed expected total.

**Explainable.** Every rupee traces back to a named rule and a recorded severity
grade. A citizen who receives Rs 1,35,000 can be shown the three lines that make
it up, and an auditor can recompute each one by hand.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from audit.models import AuditAction
from audit.services import record_event
from inspections.models import SEVERITY_FACTOR, DamageSeverity, Inspection, InspectionStatus
from reports.states import CaseStatus, InvalidTransition

from .models import (
    CompensationClaim,
    CompensationItem,
    CompensationStatus,
    ReliefAllowance,
    ReliefRule,
)

logger = logging.getLogger("resqnet.compensation")

#: Seed matrix. The first four amounts are exactly the Section 17 example.
DEFAULT_RELIEF_RULES = [
    ("roof_damage", "Roof damage", Decimal("80000"), 1),
    ("wall_damage", "Wall damage", Decimal("40000"), 2),
    ("household_damage", "Household damage", Decimal("20000"), 3),
    ("electrical_damage", "Electrical damage", Decimal("15000"), 4),
    ("structural_damage", "Structural damage", Decimal("100000"), 5),
    ("foundation_damage", "Foundation damage", Decimal("60000"), 6),
    ("water_damage", "Water damage", Decimal("25000"), 7),
]

DEFAULT_ALLOWANCES = [
    ("PER_PERSON", "Displacement support per affected person", Decimal("5000"), True, Decimal("50000"), 1),
    ("UNINHABITABLE", "Temporary accommodation (property uninhabitable)", Decimal("25000"), False, Decimal("0"), 2),
    ("IMMEDIATE_RELIEF", "Immediate relief grant", Decimal("10000"), False, Decimal("0"), 3),
]


def ensure_default_matrix() -> None:
    """Seed the relief matrix once. Never overwrites administrator edits."""
    for field, label, amount, order in DEFAULT_RELIEF_RULES:
        ReliefRule.objects.get_or_create(
            damage_field=field,
            defaults={"label": label, "base_amount": amount, "display_order": order},
        )
    for code, label, amount, per_unit, ceiling, order in DEFAULT_ALLOWANCES:
        ReliefAllowance.objects.get_or_create(
            code=code,
            defaults={
                "label": label,
                "amount": amount,
                "is_per_unit": per_unit,
                "max_amount": ceiling,
                "display_order": order,
            },
        )


def _money(value: Decimal) -> Decimal:
    """Round to whole paise, half-up - the convention for currency."""
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_breakdown(inspection: Inspection) -> tuple[Decimal, list[dict], dict]:
    """
    The pure calculation: inspection in, `(total, line_items, rules_snapshot)` out.

    Separated from the database writes so it can be tested directly, and so the
    citizen portal can preview a figure without creating a claim.
    """
    ensure_default_matrix()

    severities = inspection.severity_map()
    items: list[dict] = []
    total = Decimal("0.00")
    snapshot: dict[str, str] = {}

    # --- Severity-scaled damage rules -------------------------------------
    for rule in ReliefRule.objects.filter(is_active=True).order_by("display_order"):
        snapshot[rule.damage_field] = str(rule.base_amount)

        severity = severities.get(rule.damage_field, DamageSeverity.NONE)
        factor = Decimal(str(SEVERITY_FACTOR.get(severity, 0.0)))
        if factor == 0:
            continue

        amount = _money(rule.base_amount * factor)
        total += amount
        items.append(
            {
                "code": rule.damage_field,
                "label": rule.label,
                "severity": severity,
                "base_amount": rule.base_amount,
                "severity_factor": factor,
                "quantity": 1,
                "amount": amount,
                "display_order": rule.display_order,
            }
        )

    # --- Flat and per-head allowances -------------------------------------
    for allowance in ReliefAllowance.objects.filter(is_active=True).order_by("display_order"):
        snapshot[allowance.code] = str(allowance.amount)
        quantity = 0

        if allowance.code == "PER_PERSON":
            quantity = inspection.people_affected
        elif allowance.code == "UNINHABITABLE":
            quantity = 0 if inspection.is_habitable else 1
        elif allowance.code == "IMMEDIATE_RELIEF":
            quantity = 1 if inspection.requires_immediate_relief else 0

        if quantity <= 0:
            continue

        amount = allowance.amount * quantity if allowance.is_per_unit else allowance.amount
        if allowance.max_amount and amount > allowance.max_amount:
            amount = allowance.max_amount
        amount = _money(amount)

        total += amount
        items.append(
            {
                "code": allowance.code,
                "label": allowance.label,
                "severity": "",
                "base_amount": allowance.amount,
                "severity_factor": Decimal("1"),
                "quantity": quantity,
                "amount": amount,
                "display_order": 50 + allowance.display_order,
            }
        )

    return _money(total), items, snapshot


@transaction.atomic
def calculate_compensation(*, inspection: Inspection, user=None, request=None) -> CompensationClaim:
    """
    Calculate and store the claim, then move the case to PENDING_APPROVAL.

    Called automatically at officer sign-off. Recalculating replaces the line
    items rather than appending, so a claim never shows the same damage twice
    after the relief matrix is retuned.
    """
    if inspection.status != InspectionStatus.SIGNED:
        raise InvalidTransition(
            "Compensation can only be calculated from a signed-off inspection."
        )

    total, line_items, snapshot = compute_breakdown(inspection)

    claim, _ = CompensationClaim.objects.update_or_create(
        report=inspection.report,
        defaults={
            "inspection": inspection,
            "calculated_amount": total,
            "status": CompensationStatus.CALCULATED,
            "rules_snapshot": snapshot,
        },
    )

    claim.items.all().delete()
    CompensationItem.objects.bulk_create(
        [CompensationItem(claim=claim, **item) for item in line_items]
    )

    report = inspection.report
    previous = report.status
    if report.status == CaseStatus.INSPECTED:
        previous = report.apply_transition(CaseStatus.PENDING_APPROVAL, role="SYSTEM")

    record_event(
        entity=report,
        action=AuditAction.COMPENSATION_CALCULATED,
        user=user,
        previous_state=previous,
        new_state=report.status,
        metadata={
            "total": str(total),
            "item_count": len(line_items),
            "items": [
                {"label": item["label"], "amount": str(item["amount"])} for item in line_items
            ],
        },
        request=request,
    )

    logger.info("Compensation for %s = Rs %s", report.reference, total)
    return claim


def preview_compensation(inspection: Inspection) -> dict:
    """Show the figure without storing it - used by the officer's inspection form."""
    total, items, _ = compute_breakdown(inspection)
    return {
        "total": str(total),
        "items": [
            {
                "label": item["label"],
                "severity": item["severity"],
                "amount": str(item["amount"]),
            }
            for item in items
        ],
    }


@transaction.atomic
def adjust_claim(*, claim: CompensationClaim, amount: Decimal, reason: str, user, request=None):
    """
    An administrator overrides the calculated figure.

    The original `calculated_amount` is deliberately never overwritten - the
    record must always show both what the rules produced and what a human
    decided, along with the mandatory reason for the difference.
    """
    if not reason.strip():
        raise InvalidTransition("A written reason is required when adjusting a compensation amount.")
    amount = _money(Decimal(str(amount)))
    if amount < 0:
        raise InvalidTransition("Compensation cannot be negative.")

    claim.approved_amount = amount
    claim.adjustment_reason = reason.strip()
    claim.adjusted_by = user
    claim.status = CompensationStatus.ADJUSTED
    claim.save(
        update_fields=["approved_amount", "adjustment_reason", "adjusted_by", "status", "updated_at"]
    )

    record_event(
        entity=claim.report,
        action=AuditAction.COMPENSATION_CALCULATED,
        user=user,
        metadata={
            "adjusted": True,
            "calculated_amount": str(claim.calculated_amount),
            "approved_amount": str(amount),
            "reason": reason,
        },
        request=request,
    )
    return claim
