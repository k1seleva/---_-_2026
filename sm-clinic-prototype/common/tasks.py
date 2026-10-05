from importlib import import_module

from celery import shared_task

from common.events.contracts import DomainEvent


@shared_task(bind=True, max_retries=5, default_retry_delay=30)
def deliver_event(self, handler_path: str, event_data: dict) -> None:
    """Доставка события конкретному обработчику (режим EVENT_BUS_BACKEND=celery)."""
    module_name, _, attr = handler_path.rpartition(".")
    handler = getattr(import_module(module_name), attr)
    try:
        handler(DomainEvent.from_dict(event_data))
    except Exception as exc:  # повтор с задержкой; идемпотентность защищает от дублей
        raise self.retry(exc=exc)
