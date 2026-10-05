"""Модуль пациента реагирует на события маршрута и записи — отправляет уведомления."""
from datetime import datetime

from django.utils import timezone

from common.events import contracts
from common.events.bus import get_event_bus, idempotent
from common.events.contracts import DomainEvent

from apps.doctors.facade import DoctorsFacade

from .models import Patient
from .services.notifications import NotificationService, booking_link
from .services.push import doctor_offer


def _patient(payload: dict) -> Patient | None:
    return Patient.objects.filter(pk=payload.get("patient_id")).first()


def _context(p: dict) -> dict:
    # Конкретный врач и ближайшее время — в тексте push и в кабинете (см. services/push.py).
    offer = doctor_offer(p.get("specialty_code", ""), p.get("location_code", ""))
    return {"specialty": DoctorsFacade.specialty_genitive(p.get("specialty_code", "")),
            "step": p.get("title", ""), "due_date": p.get("due_date") or "", **offer}


@idempotent
def on_step_activated(event: DomainEvent) -> None:
    p = event.payload
    patient = _patient(p)
    if not patient or p.get("auto_book") or not p.get("bookable"):
        return
    if p.get("attempt", 1) > 1:
        template = "rebooking"
    elif p.get("order") == 1 and p.get("route_kind") in ("trigger", "recommendation"):
        template = "result_ready"
    elif p.get("offset_days"):
        template = "timer_due"  # «подходит срок контрольного исследования» (УЗИ через 6 мес)
    else:
        template = "next_step"
    NotificationService().notify(
        patient, template, context=_context(p), route_id=p["route_id"], step_id=p["step_id"],
        deep_link=booking_link(patient.id, p["step_id"]), dedupe_suffix=f"a{p.get('attempt', 1)}:{event.event_id}",
    )


@idempotent
def on_step_escalated(event: DomainEvent) -> None:
    p = event.payload
    if p.get("action") != "notify":
        return
    patient = _patient(p)
    if patient:
        NotificationService().notify(
            patient, p["template"], channels=p.get("channels") or None, context=_context(p),
            route_id=p["route_id"], step_id=p["step_id"], deep_link=booking_link(patient.id, p["step_id"]),
            dedupe_suffix=f"a{p.get('attempt', 1)}:l{p.get('level')}",
        )


@idempotent
def on_appointment_booked(event: DomainEvent) -> None:
    p = event.payload
    patient = _patient(p)
    if not patient:
        return
    starts = timezone.localtime(datetime.fromisoformat(p["starts_at"]))
    template = "postop_booked" if p.get("booked_via") == "auto" else "booking_confirmed"
    NotificationService().notify(
        patient, template, channels=["lk", "push"],
        context={"date": starts.strftime("%d.%m.%Y"), "time": starts.strftime("%H:%M"), "doctor": p.get("doctor_name", ""),
                 "location": p.get("location", "")},
        route_id=p.get("route_id"), step_id=p.get("route_step_id"), dedupe_suffix=p["appointment_id"],
    )


@idempotent
def on_route_closed(event: DomainEvent) -> None:
    NotificationService.suppress_route(event.payload["route_id"])


@idempotent
def on_patient_identified(event: DomainEvent) -> None:
    """Обезличенная карточка разобрана координатором: помечаем её объединённой с пациентом."""
    from .facade import PatientsFacade

    PatientsFacade.mark_merged(event.payload["placeholder_id"], event.payload["patient_id"])


def register() -> None:
    bus = get_event_bus()
    bus.subscribe(contracts.ROUTE_STEP_ACTIVATED, on_step_activated)
    bus.subscribe(contracts.ROUTE_STEP_ESCALATED, on_step_escalated)
    bus.subscribe(contracts.APPOINTMENT_BOOKED, on_appointment_booked)
    bus.subscribe(contracts.ROUTE_CLOSED, on_route_closed)
    bus.subscribe(contracts.PATIENT_IDENTIFIED, on_patient_identified)
