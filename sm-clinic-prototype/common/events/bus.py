"""
Шина событий — единственный канал «горизонтального» взаимодействия модулей.

Модуль публикует событие и не знает, кто его обработает (принцип инверсии зависимостей).
Подписки регистрируются в AppConfig.ready() каждого модуля.

Реализации:
* InProcessEventBus — синхронный вызов обработчиков (прототип, тесты);
* CeleryEventBus    — каждый обработчик выполняется отдельной Celery-задачей.
  Следующий шаг — RabbitMQ/Kafka: меняется только реализация publish().
"""
import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Callable
from functools import wraps

from django.conf import settings
from django.db import IntegrityError, transaction

from .contracts import DomainEvent

logger = logging.getLogger(__name__)

Handler = Callable[[DomainEvent], None]


class EventBus(ABC):
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        if handler not in self._handlers[event_type]:
            self._handlers[event_type].append(handler)

    def handlers_for(self, event_type: str) -> list[Handler]:
        return list(self._handlers.get(event_type, []))

    def publish(self, event: DomainEvent) -> None:
        """Публикация после коммита транзакции: подписчики не увидят «несуществующих» данных."""
        self._store_outbox(event)
        transaction.on_commit(lambda: self._dispatch(event))

    @staticmethod
    def _store_outbox(event: DomainEvent) -> None:
        from common.models import OutboxEvent

        OutboxEvent.objects.get_or_create(
            event_id=event.event_id,
            defaults={
                "event_type": event.event_type,
                "payload": event.payload,
                "occurred_at": event.occurred_at,
            },
        )

    @abstractmethod
    def _dispatch(self, event: DomainEvent) -> None: ...


class InProcessEventBus(EventBus):
    def _dispatch(self, event: DomainEvent) -> None:
        for handler in self.handlers_for(event.event_type):
            try:
                handler(event)
            except Exception:  # один упавший подписчик не должен ломать остальных
                logger.exception("Ошибка обработчика %s для %s", handler, event.event_type)
        from common.models import OutboxEvent

        OutboxEvent.objects.filter(event_id=event.event_id).update(published=True)


class CeleryEventBus(EventBus):
    def _dispatch(self, event: DomainEvent) -> None:
        from common.tasks import deliver_event

        for handler in self.handlers_for(event.event_type):
            deliver_event.delay(f"{handler.__module__}.{handler.__qualname__}", event.to_dict())


def idempotent(handler: Handler) -> Handler:
    """Декоратор обработчика: одно и то же событие обрабатывается ровно один раз."""

    @wraps(handler)
    def wrapper(event: DomainEvent) -> None:
        from common.models import ProcessedEvent

        name = f"{handler.__module__}.{handler.__qualname__}"
        try:
            with transaction.atomic():
                ProcessedEvent.objects.create(event_id=event.event_id, handler=name)
                handler(event)
        except IntegrityError:
            logger.info("Событие %s уже обработано %s — пропуск", event.event_id, name)

    return wrapper


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        backend = getattr(settings, "EVENT_BUS_BACKEND", "inprocess")
        _bus = CeleryEventBus() if backend == "celery" else InProcessEventBus()
    return _bus


def publish(event_type: str, payload: dict, event_id: str | None = None) -> DomainEvent:
    """Короткий помощник для публикации из сервисов."""
    event = DomainEvent(event_type=event_type, payload=payload, **({"event_id": event_id} if event_id else {}))
    get_event_bus().publish(event)
    return event
