from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from ..models import StudyDocument
from ..services.pipeline import DocumentIngestService, UploadCommand, UploadValidationError
from .serializers import DocumentUploadSerializer, StudyDocumentSerializer


class StudyDocumentViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    POST /api/v1/processing/documents/          — загрузка .doc/.docx/.json (multipart)
    GET  /api/v1/processing/documents/{id}/     — статус обработки и результат AI-агента
    GET  /api/v1/processing/documents/{id}/text/ — полный текст исходного протокола
    GET  /api/v1/processing/documents/{id}/analysis/ — маркеры, перекрытия и триггеры с позициями
    GET  /api/v1/processing/documents/{id}/advice/   — советы ИИ-агента (требуют проверки координатором)
    """

    serializer_class = StudyDocumentSerializer
    parser_classes = [MultiPartParser, FormParser]

    def get_queryset(self):
        qs = StudyDocument.objects.all()
        if patient_id := self.request.query_params.get("patient_id"):
            qs = qs.filter(patient_id=patient_id)
        return qs

    def create(self, request, *args, **kwargs):
        upload = DocumentUploadSerializer(data=request.data)
        upload.is_valid(raise_exception=True)
        file = upload.validated_data["file"]
        try:
            document = DocumentIngestService().ingest(
                UploadCommand(
                    patient_id=upload.validated_data.get("patient_id"),
                    filename=file.name,
                    data=file.read(),
                    external_id=upload.validated_data.get("external_id", ""),
                    location_code=upload.validated_data.get("location_code", ""),
                )
            )
        except UploadValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        document.refresh_from_db()
        # 202: обработка асинхронная, клиент опрашивает GET /documents/{id}/
        return Response(StudyDocumentSerializer(document).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["get"])
    def text(self, request, pk=None):
        document = self.get_object()
        return Response({"id": str(document.id), "text": document.raw_text})

    @action(detail=True, methods=["get"])
    def analysis(self, request, pk=None):
        from ..facade import ProcessingFacade

        document = self.get_object()
        return Response({"id": str(document.id), "analysis": ProcessingFacade.get_analysis(document.id)})

    @action(detail=True, methods=["get"])
    def advice(self, request, pk=None):
        from .serializers import advice_payload

        document = self.get_object()
        result = document.latest_result
        return Response({"id": str(document.id), "qwen_advice": advice_payload(result) if result else None})
