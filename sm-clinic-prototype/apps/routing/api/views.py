from django.db import transaction
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from ..models import PatientRoute, TriggerRule
from ..services.builder import RouteBuilder, RoutingInput
from ..services.matrix import RoutingMatrix
from .serializers import GenerateRouteSerializer, PatientRouteSerializer, RouteEventSerializer, TriggerRuleSerializer


class _DryRunRollback(Exception):
    pass


class PatientRouteViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET  /api/v1/routing/routes/?patient_id=...  — маршруты пациента
    GET  /api/v1/routing/routes/{id}/            — маршрут с этапами
    GET  /api/v1/routing/routes/{id}/journal/    — журнал (аудит) маршрута
    POST /api/v1/routing/routes/generate/        — сгенерировать маршрут из JSON AI-агента
    """

    serializer_class = PatientRouteSerializer

    def get_queryset(self):
        qs = PatientRoute.objects.prefetch_related("steps")
        if patient_id := self.request.query_params.get("patient_id"):
            qs = qs.filter(patient_id=patient_id)
        if self.request.query_params.get("open") == "1":
            qs = qs.exclude(status__in=PatientRoute.CLOSED_STATUSES)
        return qs

    @action(detail=False, methods=["post"])
    def generate(self, request):
        serializer = GenerateRouteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        routing_input = RoutingInput(
            patient_id=str(data["patient_id"]),
            document_id=str(data["document_id"]) if data.get("document_id") else None,
            external_id=data.get("external_id", ""),
            study_type=data.get("study_type", ""),
            study_date=data["study_date"].isoformat() if data.get("study_date") else None,
            findings=[dict(f) for f in data["findings"]],
            recommendations=[dict(r) for r in data["recommendations"]],
        )
        matrix = RoutingMatrix()
        explanation = {
            "matched_rules": [m.explanation for m in matrix.match(routing_input.findings)],
            "not_triggered": matrix.explain_non_triggers(routing_input.findings),
        }
        if data["dry_run"]:
            # Строим маршрут в транзакции и откатываем — события не публикуются (on_commit не сработает).
            try:
                with transaction.atomic():
                    routes = RouteBuilder(matrix).build(routing_input)
                    payload = PatientRouteSerializer(routes, many=True).data
                    raise _DryRunRollback
            except _DryRunRollback:
                pass
            return Response({"dry_run": True, "routes": payload, "explanation": explanation})
        routes = RouteBuilder(matrix).build(routing_input, actor_type="api")
        return Response({"routes": PatientRouteSerializer(routes, many=True).data, "explanation": explanation},
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def journal(self, request, pk=None):
        route = self.get_object()
        return Response(RouteEventSerializer(route.events.all(), many=True).data)


class TriggerRuleViewSet(viewsets.ModelViewSet):
    """Матрица маршрутизации как настройка: GET/POST/PATCH /api/v1/routing/rules/."""

    queryset = TriggerRule.objects.select_related("template")
    serializer_class = TriggerRuleSerializer
