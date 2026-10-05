"""Конечный автомат статусов маршрута. Недопустимый переход — ошибка, а не «тихая» запись:
так маршрут не может перескочить этап (например, «операция» без «госпитализации»)."""
from common.events import contracts
from common.events.bus import publish
from common import clock

from ..models import PatientRoute
from .journal import log_event

S = PatientRoute.Status

TRANSITIONS: dict[str, set[str]] = {
    S.CREATED: {S.NOTIFIED, S.BOOKED, S.OBSERVATION},
    S.NOTIFIED: {S.BOOKED, S.OBSERVATION},
    S.BOOKED: {S.VISIT_DONE, S.REBOOKING_REQUIRED, S.NO_SHOW, S.HOSPITALIZATION_SCHEDULED},
    S.REBOOKING_REQUIRED: {S.BOOKED, S.NOTIFIED},
    S.NO_SHOW: {S.BOOKED, S.NOTIFIED},
    S.VISIT_DONE: {S.IN_DIAGNOSTICS, S.REFERRED_HOSPITALIZATION, S.OBSERVATION, S.NOTIFIED, S.BOOKED},
    S.IN_DIAGNOSTICS: {S.BOOKED, S.NOTIFIED, S.VISIT_DONE},
    S.REFERRED_HOSPITALIZATION: {S.HOSPITALIZATION_SCHEDULED},
    S.HOSPITALIZATION_SCHEDULED: {S.HOSPITALIZED, S.REFERRED_HOSPITALIZATION},
    S.HOSPITALIZED: {S.SURGERY_DONE, S.DISCHARGED},
    S.SURGERY_DONE: {S.DISCHARGED},
    S.DISCHARGED: set(),
    S.OBSERVATION: {S.BOOKED, S.NOTIFIED, S.VISIT_DONE},
}
# Из любого активного статуса маршрут можно закрыть.
CLOSING = {S.COMPLETED, S.NOT_ENGAGED, S.DECLINED, S.SEEN_ELSEWHERE, S.CANCELLED}


class InvalidTransition(Exception):
    pass


def can_transition(current: str, target: str) -> bool:
    if current == target:
        return True
    if current in PatientRoute.CLOSED_STATUSES:
        return False
    return target in CLOSING or target in TRANSITIONS.get(current, set())


def transition(
    route: PatientRoute, target: str, *, actor_type: str = "system", actor_id: str = "",
    basis: str = "", force: bool = False,
) -> PatientRoute:
    """Сменить статус маршрута с записью в журнал. force=True — только для координатора."""
    current = route.status
    if current == target:
        return route
    if not force and not can_transition(current, target):
        raise InvalidTransition(f"{current} -> {target} недопустим")
    route.status = target
    fields = ["status", "updated_at"]
    if target in CLOSING:
        route.closed_at, route.close_reason = clock.now(), basis[:255]
        fields += ["closed_at", "close_reason"]
    elif route.closed_at:  # координатор переоткрыл маршрут
        route.closed_at, route.close_reason = None, ""
        fields += ["closed_at", "close_reason"]
    route.save(update_fields=fields)
    log_event(route, "status_changed", from_status=current, to_status=target,
              actor_type=actor_type, actor_id=actor_id, basis=basis)
    publish(
        contracts.ROUTE_CLOSED if target in CLOSING else contracts.ROUTE_UPDATED,
        {"route_id": str(route.id), "patient_id": str(route.patient_id), "status": target,
         "from_status": current, "trigger_code": route.trigger_code, "kind": route.kind, "basis": basis},
    )
    return route
