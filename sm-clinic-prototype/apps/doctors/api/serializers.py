from rest_framework import serializers

from ..models import Appointment, ScheduleSlot, VisitOutcome


class SlotSerializer(serializers.ModelSerializer):
    doctor = serializers.CharField(source="doctor.full_name")
    location = serializers.CharField(source="location.title")

    class Meta:
        model = ScheduleSlot
        fields = ["id", "doctor_id", "doctor", "specialty_id", "location_id", "location", "starts_at", "ends_at",
                  "format", "status"]


class AppointmentSerializer(serializers.ModelSerializer):
    slot = SlotSerializer(read_only=True)

    class Meta:
        model = Appointment
        fields = ["id", "patient_id", "route_id", "route_step_id", "source_document_id", "status", "booked_via", "slot"]


class BookSerializer(serializers.Serializer):
    slot_id = serializers.UUIDField()
    patient_id = serializers.UUIDField()
    route_step_id = serializers.UUIDField(required=False, allow_null=True)


class PrescriptionSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=["consultation", "diagnostics", "follow_up", "surgery"])
    title = serializers.CharField()
    specialty_code = serializers.CharField(required=False, allow_blank=True, default="")
    service_code = serializers.CharField(required=False, allow_blank=True, default="")
    due_in_days = serializers.IntegerField(required=False, allow_null=True)
    comment = serializers.CharField(required=False, allow_blank=True, default="")


class CompleteVisitSerializer(serializers.Serializer):
    """Тактика обязательна — без неё приём не завершить."""

    doctor_id = serializers.UUIDField()
    tactic = serializers.ChoiceField(choices=VisitOutcome.Tactic.choices)
    prescriptions = PrescriptionSerializer(many=True, required=False, default=list)
    next_specialty_code = serializers.CharField(required=False, allow_blank=True, default="")
    agrees_with_ai_route = serializers.BooleanField(default=True)
    disagreement_reason = serializers.CharField(required=False, allow_blank=True, default="")
    comment = serializers.CharField(required=False, allow_blank=True, default="")
