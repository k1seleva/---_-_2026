from datetime import date, datetime, time

from django.utils import timezone

from common.events import contracts
from common.events.bus import get_event_bus, idempotent, publish
from common.events.contracts import DomainEvent

from .services.booking import BookingService


@idempotent
def on_step_activated(event: DomainEvent) -> None:
    """Этап с auto_book (контроль после выписки): записываем пациента заранее, а не напоминаем."""
    p = event.payload
    if not p.get("auto_book") or not p.get("specialty_code"):
        return
    tz = timezone.get_current_timezone()
    earliest = datetime.combine(date.fromisoformat(p["earliest_date"]), time(8), tzinfo=tz)
    latest = datetime.combine(date.fromisoformat(p["due_date"]), time(20), tzinfo=tz)
    appointment = BookingService().auto_book(
        specialty_code=p["specialty_code"], patient_id=p["patient_id"], route_id=p["route_id"],
        route_step_id=p["step_id"], earliest=earliest, latest=latest, location_code=p.get("location_code", ""),
    )
    if appointment is None:
        publish(contracts.AUTO_BOOKING_FAILED, {"route_step_id": p["step_id"], "route_id": p["route_id"],
                                                "patient_id": p["patient_id"]})


def register() -> None:
    get_event_bus().subscribe(contracts.ROUTE_STEP_ACTIVATED, on_step_activated)
