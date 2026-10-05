"""Подписки модуля маршрутизации на события других модулей и МИС."""
from common.events import contracts
from common.events.bus import get_event_bus, idempotent
from common.events.contracts import DomainEvent

from .models import PatientRoute, RouteStep
from .services import state_machine
from .services.builder import RouteBuilder, RouteRecalculator, RoutingInput
from .services.journal import log_event
from .services.planner import RoutePlanner


@idempotent
def on_document_processed(event: DomainEvent) -> None:
    RouteBuilder().build(RoutingInput.from_event(event.payload))


@idempotent
def on_document_corrected(event: DomainEvent) -> None:
    RouteRecalculator().on_corrected(RoutingInput.from_event(event.payload))


@idempotent
def on_document_annulled(event: DomainEvent) -> None:
    RouteRecalculator().on_annulled(event.payload["document_id"], event.payload["patient_id"])


@idempotent
def on_appointment_booked(event: DomainEvent) -> None:
    p = event.payload
    if p.get("route_step_id"):
        RoutePlanner().on_booked(p["route_step_id"], p["appointment_id"], p.get("starts_at", ""), p.get("booked_via", "patient"))


@idempotent
def on_appointment_cancelled(event: DomainEvent) -> None:
    if step_id := event.payload.get("route_step_id"):
        RoutePlanner().on_cancelled(step_id)


@idempotent
def on_no_show(event: DomainEvent) -> None:
    if step_id := event.payload.get("route_step_id"):
        RoutePlanner().on_no_show(step_id)


@idempotent
def on_visit_completed(event: DomainEvent) -> None:
    p = event.payload
    if p.get("route_step_id"):
        RoutePlanner().on_visit_completed(
            p["route_step_id"], p["tactic"], p.get("prescriptions", []),
            next_specialty_code=p.get("next_specialty_code", ""), doctor_id=p.get("doctor_id", ""),
            comment=p.get("comment", ""),
        )


@idempotent
def on_notification_sent(event: DomainEvent) -> None:
    route = PatientRoute.objects.filter(pk=event.payload.get("route_id")).first()
    if route and route.status in (PatientRoute.Status.CREATED,):
        state_machine.transition(route, PatientRoute.Status.NOTIFIED, basis=f"Уведомление: {event.payload.get('channel')}")


@idempotent
def on_patient_action(event: DomainEvent) -> None:
    p = event.payload
    RoutePlanner().on_patient_action(p["route_id"], p["action"], p.get("patient_id", ""))


@idempotent
def on_correction_approved(event: DomainEvent) -> None:
    p = event.payload
    if p.get("route_id"):
        RoutePlanner().apply_correction(p["route_id"], p["operations"], actor_id=p.get("actor_id", ""), reason=p.get("reason", ""))
    elif p.get("patient_id"):
        # Показание найдено проверкой рекомендаций, а маршрута по протоколу нет — создаём его по решению координатора.
        RouteBuilder().build_manual(patient_id=p["patient_id"], document_id=p.get("document_id"), operations=p["operations"],
                                    reason=p.get("reason", ""), actor_id=p.get("actor_id", ""))


@idempotent
def on_mis_event(event: DomainEvent) -> None:
    p = event.payload
    route_id = p.get("route_id")
    if not route_id:
        route = PatientRoute.objects.filter(
            patient_id=p["patient_id"],
            steps__step_type__in=["hospitalization_referral", "hospitalization", "surgery", "discharge"],
        ).exclude(status__in=PatientRoute.CLOSED_STATUSES).distinct().first()
        route_id = route.id if route else None
    if route_id:
        RoutePlanner().on_mis_event(str(route_id), event.event_type, p)


@idempotent
def on_auto_booking_failed(event: DomainEvent) -> None:
    """Этап 12 кейса: контроль не назначен -> уведомление пациенту + задача координатору."""
    from common.events.bus import publish

    step = RouteStep.objects.select_related("route").filter(pk=event.payload.get("route_step_id")).first()
    if not step:
        return
    log_event(step.route, "auto_booking_failed", step=step, basis="Нет свободного слота для контрольного приёма")
    publish(contracts.ROUTE_STEP_ESCALATED, {
        **RoutePlanner.step_payload(step), "level": 0, "action": "notify", "template": "postop_choose_time",
        "channels": ["lk", "push"], "buttons": [], "task_type": "",
    })
    publish(contracts.ROUTE_STEP_ESCALATED, {
        **RoutePlanner.step_payload(step), "level": 0, "action": "coordinator_task", "template": "",
        "channels": [], "buttons": [], "task_type": "book_follow_up",
    })


def register() -> None:
    bus = get_event_bus()
    bus.subscribe(contracts.DOCUMENT_PROCESSED, on_document_processed)
    bus.subscribe(contracts.DOCUMENT_CORRECTED, on_document_corrected)
    bus.subscribe(contracts.DOCUMENT_ANNULLED, on_document_annulled)
    bus.subscribe(contracts.APPOINTMENT_BOOKED, on_appointment_booked)
    bus.subscribe(contracts.APPOINTMENT_CANCELLED, on_appointment_cancelled)
    bus.subscribe(contracts.APPOINTMENT_NO_SHOW, on_no_show)
    bus.subscribe(contracts.VISIT_COMPLETED, on_visit_completed)
    bus.subscribe(contracts.NOTIFICATION_SENT, on_notification_sent)
    bus.subscribe(contracts.PATIENT_ACTION, on_patient_action)
    bus.subscribe(contracts.ROUTE_CORRECTION_APPROVED, on_correction_approved)
    bus.subscribe(contracts.AUTO_BOOKING_FAILED, on_auto_booking_failed)
    for mis_event in (contracts.MIS_HOSPITALIZATION_SCHEDULED, contracts.MIS_HOSPITALIZED,
                      contracts.MIS_SURGERY_DONE, contracts.MIS_DISCHARGED):
        bus.subscribe(mis_event, on_mis_event)
