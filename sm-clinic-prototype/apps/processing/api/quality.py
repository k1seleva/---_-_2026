"""Проверка качества аналитики через API: скрипт клиники или эксперт отправляет до 100 протоколов и разметку."""
from django.conf import settings
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import QualityRun
from ..services.pipeline import IncomingFile
from ..services.quality import QualityRunService


def _dto(run: QualityRun, *, rows: bool = False) -> dict:
    data = {"id": str(run.id), "title": run.title, "status": run.status, "files_total": run.files_total,
            "processed": run.processed, "rejected": run.rejected, "summary": run.summary, "error": run.error,
            "created_at": run.created_at, "finished_at": run.finished_at, "url": f"/processing/quality/{run.id}/"}
    if rows:
        data["rows"] = run.rows
    return data


class QualityRunView(APIView):
    """
    POST /api/v1/processing/quality-runs/   multipart: files=<f1>&files=<f2>… или .zip (до QUALITY_MAX_PROTOCOLS),
                                            labels=<csv file,expected_rules> (необязательно), title
                                            → 202 {id, status, files_total, rejected, url}
    GET  /api/v1/processing/quality-runs/   — последние прогоны со сводкой
    Прогон «всухую»: пациенты, маршруты и уведомления не создаются, файлы удаляются после проверки.
    """

    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        return Response([_dto(r) for r in QualityRun.objects.defer("rows")[:20]])

    def post(self, request):
        files = [IncomingFile(name=f.name, data=f.read()) for f in request.FILES.getlist("files")]
        if not files:
            return Response({"detail": f"Передайте протоколы в поле files (до {settings.QUALITY_MAX_PROTOCOLS})"},
                            status=status.HTTP_400_BAD_REQUEST)
        labels = request.FILES.get("labels")
        run = QualityRunService().start(files, labels=labels.read() if labels else None,
                                        title=request.data.get("title", ""), created_by="api")
        code = status.HTTP_400_BAD_REQUEST if run.status == QualityRun.Status.FAILED else status.HTTP_202_ACCEPTED
        return Response(_dto(run), status=code)


class QualityRunDetailView(APIView):
    """GET /api/v1/processing/quality-runs/{id}/ — статус, сводка и строки по каждому протоколу."""

    def get(self, request, run_id):
        run = QualityRun.objects.filter(pk=run_id).first()
        return Response(_dto(run, rows=True)) if run else Response(status=status.HTTP_404_NOT_FOUND)
