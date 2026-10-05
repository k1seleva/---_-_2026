from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..facade import AuditFacade
from ..services.service import AuditService


class CheckSerializer(serializers.Serializer):
    document_id = serializers.UUIDField()
    recommendations = serializers.ListField(child=serializers.CharField(), required=False,
                                            help_text="Рекомендации врача текстом; если не переданы — берутся из протокола")


class AuditCheckView(APIView):
    """
    POST /api/v1/audit/check/ — проверка достаточности рекомендаций без сохранения (dry-run).

    {"document_id": "...", "recommendations": ["консультация лечащего врача"]}
    -> вердикт, показания с цитатами-основаниями, замечания и предложенные правки маршрута.
    """

    def post(self, request):
        from apps.processing.facade import ProcessingFacade

        data = CheckSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        document = ProcessingFacade.get_document(data.validated_data["document_id"])
        if not document:
            return Response({"detail": "Протокол не найден"}, status=status.HTTP_404_NOT_FOUND)
        annotation = ProcessingFacade.get_annotation(document["id"]) or {}
        segments = [s for s in annotation.get("segments", [])
                    if s.get("significant") or s["kind"] == "recommendation"]
        recommendations = document["recommendations"]
        if "recommendations" in data.validated_data:
            texts = data.validated_data["recommendations"]
            recommendations = ProcessingFacade.parse_recommendations("Рекомендовано: " + "; ".join(texts))
            segments = [s for s in segments if s["kind"] != "recommendation"] + [
                {"kind": "recommendation", "text": t, "attributes": {"specialties": []}} for t in texts]
        from apps.routing.facade import RoutingFacade

        outcome = AuditService().check_protocol({
            "extraction": {"findings": document["findings"], "recommendations": recommendations},
            "annotation": {"segments": segments, "summary": annotation.get("summary", {})},
        }, routes=RoutingFacade.routes_for_document(document["id"]))
        return Response(outcome.as_dict())


class DocumentAuditsView(APIView):
    """GET /api/v1/audit/documents/{id}/ — сохранённые проверки по протоколу (протокол + приёмы)."""

    def get(self, request, document_id):
        return Response(AuditFacade.for_document(document_id))


class AuditListView(APIView):
    """GET /api/v1/audit/audits/?open=1 — проверки с замечаниями (open=1 — ждут решения координатора)."""

    def get(self, request):
        return Response(AuditFacade.list_audits(open_only=request.query_params.get("open") == "1"))


class AuditStatsView(APIView):
    """GET /api/v1/audit/stats/ — вердикты, частые пропуски, точность правил по решениям координатора."""

    def get(self, request):
        return Response(AuditFacade.stats())
