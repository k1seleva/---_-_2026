from common import clock

from ..models import PatientRoute, RouteEvent, RouteStep


def log_event(
    route: PatientRoute,
    event_type: str,
    *,
    step: RouteStep | None = None,
    from_status: str = "",
    to_status: str = "",
    actor_type: str = "system",
    actor_id: str = "",
    basis: str = "",
    payload: dict | None = None,
) -> RouteEvent:
    """Запись в журнал маршрута: кто, что, когда и на каком основании (п. 14 кейса)."""
    return RouteEvent.objects.create(
        route=route, step=step, event_type=event_type, from_status=from_status, to_status=to_status,
        actor_type=actor_type, actor_id=actor_id, basis=basis[:500], payload=payload or {},
        occurred_at=clock.now(),
    )
