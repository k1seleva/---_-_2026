"""Пакетная загрузка через API: МИС или скрипт клиники отправляет сразу много протоколов (или zip)."""
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from ..facade import ProcessingFacade
from ..models import UploadBatch
from ..services.pipeline import BatchIngestService, IncomingFile


class BatchUploadView(APIView):
    """
    POST /api/v1/processing/batches/   multipart: files=<f1>&files=<f2>… (или один .zip), location_code
                                       → 202 {batch_id, accepted, duplicates, rejected}
    GET  /api/v1/processing/batches/   — последние пачки со сводкой
    """

    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        return Response(ProcessingFacade.list_batches(50, request.query_params.get("location_code", "")))

    def post(self, request):
        files = [IncomingFile(name=f.name, data=f.read()) for f in request.FILES.getlist("files")]
        if not files:
            return Response({"detail": "Передайте хотя бы один файл в поле files"}, status=status.HTTP_400_BAD_REQUEST)
        report = BatchIngestService().ingest(files, location_code=request.data.get("location_code", ""),
                                             source=UploadBatch.Source.API, created_by="api")
        b = report.batch
        return Response({"batch_id": str(b.id), "accepted": b.accepted, "duplicates": b.duplicates, "rejected": b.rejected},
                        status=status.HTTP_202_ACCEPTED)


class BatchDetailView(APIView):
    def get(self, request, batch_id):
        batch = ProcessingFacade.get_batch(batch_id)
        return Response(batch) if batch else Response(status=status.HTTP_404_NOT_FOUND)
