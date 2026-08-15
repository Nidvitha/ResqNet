"""
Approvals and payouts.

Section 19 is explicit that real banking integration is out of scope: this
simulates the payout lifecycle for an academic build. Nothing here talks to a
bank, and no screen or document should claim otherwise. `Payout.is_simulated`
defaults to True and is surfaced in the API precisely so the simulation is
labelled rather than implied.
"""

from django.conf import settings
from django.db import models

from compensation.models import CompensationClaim
from reports.models import DisasterReport


class ApprovalDecision(models.TextChoices):
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    REVIEW_REQUESTED = "REVIEW_REQUESTED", "Re-inspection requested"


class Approval(models.Model):
    """
    One administrator decision on one case.

    Kept as its own table rather than a status column so a case that is rejected,
    re-inspected, and then approved carries all three decisions in order - the
    history an auditor actually needs.
    """

    report = models.ForeignKey(DisasterReport, on_delete=models.CASCADE, related_name="approvals")
    claim = models.ForeignKey(
        CompensationClaim, on_delete=models.PROTECT, related_name="approvals",
        null=True, blank=True,
    )

    decision = models.CharField(max_length=20, choices=ApprovalDecision.choices, db_index=True)
    approved_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Amount authorised. Null for rejections and review requests.",
    )
    reason = models.TextField(
        help_text="Mandatory. A relief decision must always carry a written justification."
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="approval_decisions"
    )
    decided_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-decided_at"]
        indexes = [models.Index(fields=["decision", "-decided_at"])]

    def __str__(self) -> str:
        return f"{self.report.reference}: {self.decision} by {self.decided_by.username}"


class PayoutStatus(models.TextChoices):
    PENDING = "PAYOUT_PENDING", "Payout pending"
    INITIATED = "PAYOUT_INITIATED", "Payout initiated"
    COMPLETED = "PAYOUT_COMPLETED", "Payout completed"
    FAILED = "PAYOUT_FAILED", "Payout failed"


class Payout(models.Model):
    """Tracks the disbursement of an approved relief amount."""

    report = models.OneToOneField(DisasterReport, on_delete=models.CASCADE, related_name="payout")
    approval = models.ForeignKey(Approval, on_delete=models.PROTECT, related_name="payouts")

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=20, choices=PayoutStatus.choices, default=PayoutStatus.PENDING, db_index=True
    )

    reference_number = models.CharField(
        max_length=40, unique=True, blank=True,
        help_text="Simulated transaction reference, e.g. PAY-2026-000012.",
    )
    beneficiary_name = models.CharField(max_length=150, blank=True)
    payment_method = models.CharField(
        max_length=30,
        choices=[("BANK_TRANSFER", "Bank transfer"), ("CHEQUE", "Cheque"), ("CASH", "Cash counter")],
        default="BANK_TRANSFER",
    )
    is_simulated = models.BooleanField(
        default=True,
        help_text="True for this academic build. No real banking system is connected.",
    )

    initiated_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payouts_initiated",
    )
    failure_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.report.reference}: Rs {self.amount:,.2f} ({self.status})"

    def save(self, *args, **kwargs):
        if not self.reference_number:
            self.reference_number = self._generate_reference()
        super().save(*args, **kwargs)

    @staticmethod
    def _generate_reference() -> str:
        from django.utils import timezone

        year = timezone.now().year
        prefix = f"PAY-{year}-"
        last = (
            Payout.objects.filter(reference_number__startswith=prefix)
            .order_by("-reference_number")
            .values_list("reference_number", flat=True)
            .first()
        )
        sequence = int(last.rsplit("-", 1)[1]) + 1 if last else 1
        return f"{prefix}{sequence:06d}"
