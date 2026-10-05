"""
Общее ядро (shared kernel). Здесь только инфраструктура, без предметной логики:
базовая модель, модельное время для демонстрации и журнал обработанных событий.
"""
import uuid

from django.db import models


class BaseModel(models.Model):
    """Базовая модель: UUID вместо автоинкремента — идентификаторы уникальны
    между будущими микросервисами и не раскрывают объёмы данных."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SimulationClock(models.Model):
    """Смещение «модельного времени». Позволяет прогнать сценарии 24 ч / 72 ч / 7 / 14 / 30 дней
    за секунды на демонстрации (требование кейса, п. 12). В проде смещение всегда 0."""

    singleton = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    offset_seconds = models.BigIntegerField(default=0)

    class Meta:
        verbose_name = "Модельное время"


class ProcessedEvent(models.Model):
    """Журнал обработанных событий: повторная доставка того же события
    тем же обработчиком не создаёт дублей (идемпотентность, п. 13 кейса)."""

    event_id = models.CharField(max_length=128)
    handler = models.CharField(max_length=255)
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event_id", "handler"], name="uniq_event_per_handler"),
        ]


class OutboxEvent(models.Model):
    """Transactional outbox: событие пишется в той же транзакции, что и изменение данных,
    а отдельный релей публикует его в брокер. В прототипе — журнал всех событий шины."""

    event_id = models.CharField(max_length=128, unique=True)
    event_type = models.CharField(max_length=128, db_index=True)
    payload = models.JSONField(default=dict)
    occurred_at = models.DateTimeField()
    published = models.BooleanField(default=False)

    class Meta:
        ordering = ["-occurred_at"]
