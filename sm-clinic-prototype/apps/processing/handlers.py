"""Подписки модуля обработки: решения координатора по обезличенным протоколам."""
from common.events import contracts
from common.events.bus import get_event_bus, idempotent
from common.events.contracts import DomainEvent

from .services.pipeline import DocumentIngestService, IdentityService


@idempotent
def on_patient_identified(event: DomainEvent) -> None:
    """Координатор определил пациента: протоколы переходят к нему и уходят в маршрутизацию."""
    p = event.payload
    IdentityService().assign(p["placeholder_id"], p["patient_id"])


@idempotent
def on_document_rejected(event: DomainEvent) -> None:
    DocumentIngestService().annul(document_id=event.payload["document_id"], reason=event.payload.get("reason", ""))


def register() -> None:
    bus = get_event_bus()
    bus.subscribe(contracts.PATIENT_IDENTIFIED, on_patient_identified)
    bus.subscribe(contracts.DOCUMENT_REJECTED, on_document_rejected)
