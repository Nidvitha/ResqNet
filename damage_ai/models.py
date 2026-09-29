from django.db import models

from reports.models import DisasterReport, ReportPhoto


class AIAssessmentStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"


class AIDamageAssessment(models.Model):
    """Preliminary model output for one citizen-submitted report photo."""

    report = models.ForeignKey(
        DisasterReport, on_delete=models.CASCADE, related_name="ai_assessments"
    )
    photo = models.OneToOneField(
        ReportPhoto, on_delete=models.CASCADE, related_name="ai_assessment"
    )
    predicted_severity = models.CharField(
        max_length=8,
        choices=[("MINOR", "Minor"), ("MODERATE", "Moderate"), ("SEVERE", "Severe")],
        blank=True,
    )
    confidence_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    model_name = models.CharField(max_length=100)
    model_version = models.CharField(max_length=50)
    status = models.CharField(
        max_length=10, choices=AIAssessmentStatus.choices, default=AIAssessmentStatus.PENDING
    )
    error_message = models.TextField(blank=True)
    inferred_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["photo_id"]
        indexes = [models.Index(fields=["report", "status"])]

    def __str__(self) -> str:
        return f"AI assessment for photo {self.photo_id} ({self.status})"