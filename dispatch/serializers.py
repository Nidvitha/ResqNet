"""Dispatch serializers."""

from rest_framework import serializers

from reports.serializers import ReportListSerializer

from .models import Assignment


class AssignmentSerializer(serializers.ModelSerializer):
    report_reference = serializers.CharField(source="report.reference", read_only=True)
    report_detail = ReportListSerializer(source="report", read_only=True)
    officer_name = serializers.CharField(source="officer.get_full_name", read_only=True)
    officer_username = serializers.CharField(source="officer.username", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)
    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = Assignment
        fields = [
            "id", "report", "report_reference", "report_detail",
            "officer", "officer_name", "officer_username",
            "status", "status_display", "is_automatic",
            "distance_km", "selection_score", "selection_reason",
            "assigned_at", "accepted_at", "completed_at", "due_by",
            "is_overdue", "is_active", "notes",
        ]
        read_only_fields = fields


class ManualAssignSerializer(serializers.Serializer):
    """Body for an administrator overriding automatic dispatch."""

    report_id = serializers.IntegerField()
    officer_id = serializers.IntegerField()
    note = serializers.CharField(max_length=500, required=False, allow_blank=True)


class OfficerCandidateSerializer(serializers.Serializer):
    """
    A ranked dispatch candidate, with the reasoning shown.

    The administrator sees not just who the engine would pick, but why - and can
    disagree with it on the same screen.
    """

    officer_id = serializers.IntegerField()
    officer_name = serializers.CharField()
    employee_id = serializers.CharField()
    zone = serializers.CharField()
    distance_km = serializers.FloatField()
    score = serializers.FloatField()
    active_cases = serializers.IntegerField()
    max_active_cases = serializers.IntegerField()
    zone_match = serializers.BooleanField()
    reason = serializers.CharField()
