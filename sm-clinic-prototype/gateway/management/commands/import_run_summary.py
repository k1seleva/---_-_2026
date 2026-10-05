"""
Загрузить сводку прогона протоколов (только счётчики) в «Качество разбора».

    python manage.py import_run_summary docs/case_run_summary.json

Сводку готовит evaluate_protocols --summary-json на компьютере, где лежат протоколы. В ней нет текстов,
цитат и имён файлов, поэтому реальные цифры видны в прототипе, а сами протоколы остаются у владельца.
Повторный импорт той же сводки обновляет прогон, а не создаёт новый.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.processing.models import QualityRun
from common import clock

FORBIDDEN = ("labelled", "rows", "mismatches")


def import_summary(path: Path) -> QualityRun:
    data = json.loads(path.read_text(encoding="utf-8"))
    summary = data.get("summary") or {}
    if not summary or any(key in summary for key in FORBIDDEN):
        raise CommandError("Это не сводка без текстов: нет поля summary или в нём есть строки по файлам")
    total = int(summary.get("protocols", 0))
    run, _ = QualityRun.objects.update_or_create(title=data.get("title", "Прогон протоколов")[:200], defaults={
        "status": QualityRun.Status.DONE, "files_total": total, "processed": total, "rows": [], "summary": summary,
        "created_by": f"сводка от {data.get('date', '')}", "finished_at": clock.now()})
    return run


class Command(BaseCommand):
    help = "Загрузить сводку прогона протоколов (только счётчики) в «Качество разбора»"

    def add_arguments(self, parser):
        parser.add_argument("path")

    def handle(self, path, **opts):
        run = import_summary(Path(path))
        self.stdout.write(self.style.SUCCESS(f"Загружено: «{run.title}», протоколов {run.files_total}"))
