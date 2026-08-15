"""Audit serializers - read-only by construction."""

from rest_framework import serializers

from .models import AuditEvent


class AuditEventSerializer(serializers.ModelSerializer):
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    performed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = AuditEvent
        fields = [
            "id", "entity_type", "entity_id", "entity_label",
            "action", "action_display",
            "performed_by", "performed_by_username", "performed_by_name",
            "performed_by_role", "previous_state", "new_state",
            "metadata", "timestamp",
        ]
        # Every field is read-only. Even if a write route were added by mistake,
        # this serializer would accept nothing.
        read_only_fields = fields

    def get_performed_by_name(self, obj):
        if obj.performed_by:
            return obj.performed_by.get_full_name() or obj.performed_by.username
        return obj.performed_by_username or "System"
