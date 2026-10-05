"""Что видит врач при открытии записи (этап 7 кейса): основание маршрута, находки с цитатами,
текущий маршрут и незавершённые маршруты пациента. Данные собираются через фасады модулей."""
from apps.audit.facade import AuditFacade
from apps.processing.facade import ProcessingFacade
from apps.routing.facade import RoutingFacade

from ..models import Appointment


def build_visit_context(appointment: Appointment, *, owner: str = "") -> dict:
    route = RoutingFacade.get_route(appointment.route_id) if appointment.route_id else None
    document_id = appointment.source_document_id or (route and route.get("source_document_id"))
    document = ProcessingFacade.get_document(document_id) if document_id else None
    unfinished = [r for r in RoutingFacade.unfinished_routes(appointment.patient_id) if not route or r["id"] != route["id"]]
    banner = None
    if route:
        banner = {
            "title": "Пациент включён в диагностический маршрут",
            "basis": route["reason"],
            "evidence": route["evidence"].get("quote", ""),
            "potential_route": route["evidence"].get("potential_route", ""),
            "rule": route["evidence"].get("rule", ""),
            "text": "Требуется определить дальнейшую тактику.",
        }
    return {
        "route": route, "document": document, "unfinished_routes": unfinished, "banner": banner,
        # Разметка важности: врач видит сначала главное (размер, количество, связанные детали описания).
        "annotation": ProcessingFacade.get_annotation(document_id, owner=owner) if document_id else None,
        # Замечания проверки рекомендаций по протоколу — подсказка врачу на приёме.
        "audit_notes": [a for a in (AuditFacade.for_document(document_id) if document_id else [])
                        if a["source"] == "protocol" and a["verdict"] != "sufficient"],
    }
