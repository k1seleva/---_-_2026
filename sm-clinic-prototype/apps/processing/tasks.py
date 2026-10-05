from celery import shared_task


@shared_task(bind=True, max_retries=2, default_retry_delay=60, acks_late=True)
def process_document(self, job_id: str) -> str | None:
    """Разбор протокола в очереди llm (вызов модели — минуты). Повторная доставка того же сообщения
    (acks_late после падения воркера) не запускает второй разбор: задачу забирает ровно один исполнитель."""
    from django.conf import settings

    from .models import ProcessingJob
    from .services.jobs import ProcessingQueue

    if not ProcessingQueue.claim(job_id):
        return None  # уже в работе у другого воркера или завершена; зависшую вернёт recover_stale_jobs
    try:
        result = ProcessingQueue.run_claimed(job_id)
    except Exception as exc:  # noqa: BLE001 — сбой вне разбора (база занята, воркер перегружен)
        job = ProcessingJob.objects.filter(pk=job_id).first()
        if job is None or job.attempts >= settings.PROCESSING_MAX_ATTEMPTS:
            raise
        ProcessingJob.objects.filter(pk=job_id).update(status=ProcessingJob.Status.QUEUED, worker="")
        raise self.retry(exc=exc)
    return str(result.id) if result else None


@shared_task
def recover_stale_jobs() -> int:
    """Пульс очереди: задачи, зависшие «в работе» (воркер упал, модель повисла), — обратно в очередь или в ошибку."""
    from .services.jobs import ProcessingQueue

    return ProcessingQueue.recover_stale()


@shared_task
def watch_inbox() -> int:
    """Пульс папки-наблюдателя: забирает новые протоколы и создаёт пачки по клиникам."""
    from .services.inbox import InboxWatcher

    return sum(r.batch.accepted for r in InboxWatcher().scan())


@shared_task(acks_late=True)
def run_quality_check(run_id: str) -> int:
    """Проверка качества аналитики на пачке протоколов (без пациентов и маршрутов)."""
    from .services.quality import QualityRunService

    return QualityRunService().run(run_id).processed


@shared_task(acks_late=True)
def generate_routing_advice(result_id: str) -> int:
    """Советы ИИ-агента по маршрутизации (Qwen): фоном, после разбора протокола."""
    from .services.routing_advice import RoutingAdviceService

    return RoutingAdviceService().generate(result_id)
