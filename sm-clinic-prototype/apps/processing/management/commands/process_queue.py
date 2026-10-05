"""Очередь разбора отдельным процессом, без Celery и без сайта.

    python manage.py process_queue           # работать постоянно (как фоновый поток сайта)
    python manage.py process_queue --once    # разобрать всё, что в очереди, и выйти

Удобно, когда сайт запущен под gunicorn с несколькими процессами: тогда задайте PROCESSING_QUEUE=celery
или запустите этот обработчик один, чтобы модель получала протоколы по одному.
"""
from django.core.management.base import BaseCommand

from apps.processing.services.jobs import LocalWorker, ProcessingQueue


class Command(BaseCommand):
    help = "Разбирать протоколы из очереди (ProcessingJob в статусе queued) по одному"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Разобрать очередь и выйти")

    def handle(self, *args, **opts):
        recovered = ProcessingQueue.recover_stale(check_dead=True)
        if recovered:
            self.stdout.write(f"Возвращено в очередь после остановки: {recovered}")
        if opts["once"]:
            self.stdout.write(f"Разобрано задач: {LocalWorker.drain()}")
            return
        self.stdout.write("Очередь разбора запущена. Ctrl+C — остановить.")
        LocalWorker.run_forever()
