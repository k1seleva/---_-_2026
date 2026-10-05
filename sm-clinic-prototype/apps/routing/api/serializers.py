from rest_framework import serializers

from ..models import PatientRoute, RouteEvent, RouteStep, RouteTemplate, TriggerRule


class RouteStepSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    step_type_display = serializers.CharField(source="get_step_type_display", read_only=True)

    class Meta:
        model = RouteStep
        fields = ["id", "order", "step_type", "step_type_display", "title", "specialty_code", "service_code",
                  "status", "status_display", "source", "offset_days", "window_days", "earliest_date", "due_date",
                  "appointment_id", "attempt", "escalation_level", "outcome", "comment"]


class RouteEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = RouteEvent
        fields = ["event_type", "from_status", "to_status", "actor_type", "actor_id", "basis", "payload", "occurred_at"]


class PatientRouteSerializer(serializers.ModelSerializer):
    steps = RouteStepSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = PatientRoute
        fields = ["id", "patient_id", "kind", "reason", "status", "status_display", "trigger_code", "rule_version",
                  "evidence", "source_document_id", "cycle_no", "parent", "responsible_unit", "detected_at",
                  "target_date", "closed_at", "close_reason", "steps"]


class FindingInputSerializer(serializers.Serializer):
    code = serializers.CharField()
    label = serializers.CharField(required=False, allow_blank=True)
    evidence_quote = serializers.CharField(required=False, allow_blank=True)
    negated = serializers.BooleanField(default=False)
    uncertain = serializers.BooleanField(default=False)
    attributes = serializers.DictField(required=False)
    confidence = serializers.FloatField(required=False)


class RecommendationInputSerializer(serializers.Serializer):
    text = serializers.CharField()
    kind = serializers.ChoiceField(choices=["consultation", "diagnostics", "observation"], default="consultation")
    specialty_code = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    service = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    interval_days = serializers.IntegerField(required=False, allow_null=True)


class GenerateRouteSerializer(serializers.Serializer):
    """Вход генерации маршрута — структурированный JSON от LangChain-агента."""

    patient_id = serializers.UUIDField()
    document_id = serializers.UUIDField(required=False, allow_null=True)
    external_id = serializers.CharField(required=False, allow_blank=True)
    study_type = serializers.CharField(required=False, allow_blank=True)
    study_date = serializers.DateField(required=False, allow_null=True)
    findings = FindingInputSerializer(many=True, required=False, default=list)
    recommendations = RecommendationInputSerializer(many=True, required=False, default=list)
    dry_run = serializers.BooleanField(default=False, help_text="Показать маршрут без сохранения")


class TriggerRuleSerializer(serializers.ModelSerializer):
    template_code = serializers.SlugRelatedField(source="template", slug_field="code", queryset=RouteTemplate.objects.all())

    class Meta:
        model = TriggerRule
        fields = ["id", "code", "version", "title", "finding_code", "conditions", "priority", "template_code",
                  "first_specialty_code", "potential_route", "target_days", "responsible_unit", "is_surgical",
                  "is_emergency", "is_active"]
