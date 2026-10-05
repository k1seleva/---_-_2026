from rest_framework import serializers

from ..models import AdviceReview, CoordinatorTask, RouteReview


class CoordinatorTaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = CoordinatorTask
        fields = ["id", "route_id", "route_step_id", "patient_id", "task_type", "priority", "status", "title",
                  "script", "due_at", "responsible_unit", "resolution", "created_at"]
        read_only_fields = ["route_id", "route_step_id", "patient_id", "task_type", "title", "script", "due_at"]


class RouteReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = RouteReview
        fields = ["id", "route_id", "patient_id", "document_id", "source", "status", "ai_markers", "ai_route",
                  "doctor_route", "discrepancies", "reason", "resolution_comment", "resolved_at", "created_at"]
        read_only_fields = fields


class OpenReviewSerializer(serializers.Serializer):
    route_id = serializers.UUIDField()
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class ResolveReviewSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[c for c, _ in RouteReview.Status.choices if c != "open"])
    operations = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    comment = serializers.CharField()


class AdviceReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = AdviceReview
        fields = ["id", "user_id", "advice_id", "document_id", "verdict", "comment", "corrected_route_code",
                  "advice_snapshot", "timestamp"]
        read_only_fields = fields


class AdviceReviewCreateSerializer(serializers.Serializer):
    advice_id = serializers.UUIDField()
    verdict = serializers.ChoiceField(choices=AdviceReview.Verdict.choices)
    comment = serializers.CharField(required=False, allow_blank=True)
    corrected_route_code = serializers.CharField(required=False, allow_blank=True)
