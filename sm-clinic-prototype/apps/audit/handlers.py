"""Подписки модуля проверки рекомендаций.

* протокол обработан/исправлен -> проверка рекомендаций диагноста;
* приём завершён               -> проверка назначений врача;
* протокол аннулирован         -> проверки снимаются;
* координатор принял решение   -> фиксируем принятые/отклонённые замечания (точность правил).
"""
from common.events import contracts
from common.events.bus import get_event_bus, idempotent
from common.events.contracts import DomainEvent

from .services.service import AuditService


@idempotent
def on_document_processed(event: DomainEvent) -> None:
    AuditService().audit_protocol(event.payload)


@idempotent
def on_document_corrected(event: DomainEvent) -> None:
    from apps.processing.facade import ProcessingFacade

    p = event.payload
    for old_id in ProcessingFacade.other_versions(p["document_id"]):
        AuditService.cancel_for_document(old_id)
    AuditService().audit_protocol(p)


@idempotent
def on_document_annulled(event: DomainEvent) -> None:
    AuditService.cancel_for_document(event.payload["document_id"])


@idempotent
def on_visit_completed(event: DomainEvent) -> None:
    AuditService().audit_visit(event.payload)


@idempotent
def on_audit_resolved(event: DomainEvent) -> None:
    AuditService().apply_decisions(event.payload["audit_id"], event.payload.get("decisions", []))


def register() -> None:
    bus = get_event_bus()
    bus.subscribe(contracts.DOCUMENT_PROCESSED, on_document_processed)
    bus.subscribe(contracts.DOCUMENT_CORRECTED, on_document_corrected)
    bus.subscribe(contracts.DOCUMENT_ANNULLED, on_document_annulled)
    bus.subscribe(contracts.VISIT_COMPLETED, on_visit_completed)
    bus.subscribe(contracts.AUDIT_RESOLVED, on_audit_resolved)
