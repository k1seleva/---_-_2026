from rest_framework import serializers

from ..models import Notification, Patient, PushSubscription


class PatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = ["id", "external_mis_id", "display_name", "birth_year", "sex", "is_anonymous", "placeholder_code",
                  "large_text"]


class NotificationPreferenceSerializer(serializers.Serializer):
    """Строка настроек каналов: тип событий и включённые каналы (личный кабинет включён всегда)."""

    group = serializers.ChoiceField(choices=["results", "route", "appointments", "reminders"])
    push = serializers.BooleanField()
    sms = serializers.BooleanField()
    call = serializers.BooleanField()


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "route_id", "route_step_id", "template_code", "channel", "title", "body", "buttons",
                  "deep_link", "status", "status_reason", "sent_at", "read_at", "created_at"]


class PushSubscriptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PushSubscription
        fields = ["id", "provider", "endpoint", "keys"]


class PatientBookSerializer(serializers.Serializer):
    route_step_id = serializers.UUIDField()
    slot_id = serializers.UUIDField()


class RouteActionSerializer(serializers.Serializer):
    route_id = serializers.UUIDField()
    action = serializers.ChoiceField(choices=["seen_elsewhere", "decline", "callback", "confirm"])
    comment = serializers.CharField(required=False, allow_blank=True, default="")
