"""Конфигурация Celery: асинхронная обработка документов (LangChain) и таймеры маршрутов."""
import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("sm_clinic")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Периодические задачи (celery beat). Сами интервалы эскалаций задаются в БД (EscalationPolicy),
# здесь только «пульс», который проверяет просроченные этапы маршрутов.
app.conf.beat_schedule = {
    "routing-escalation-tick": {
        "task": "apps.routing.tasks.run_escalation_tick",
        "schedule": crontab(minute="*/5"),
    },
    # Папка-наблюдатель: новые протоколы из <PROTOCOL_INBOX_DIR>/<клиника>/ забираются раз в минуту.
    "processing-watch-inbox": {
        "task": "apps.processing.tasks.watch_inbox",
        "schedule": 60.0,
    },
    # Очередь разбора: задачи, зависшие «в работе» (воркер упал, модель повисла), — обратно в очередь.
    "processing-recover-stale-jobs": {
        "task": "apps.processing.tasks.recover_stale_jobs",
        "schedule": crontab(minute="*/5"),
    },
    "coordinator-metrics-snapshot": {
        "task": "apps.coordinator.tasks.snapshot_metrics",
        "schedule": crontab(minute=0, hour="*/1"),
    },
}
