"""Координатор слушает события всех модулей: строит проекцию для аналитики и создаёт задачи."""
from common import clock
from common.events import contracts
from common.events.bus import get_event_bus, idempotent
from common.events.contracts import DomainEvent

from .models import CoordinatorTask, RouteFact
from .services.audit_reviews import AuditReviewService
from .services.cases import CaseService
from .services.disputes import DisputeService
from .services.tasks import CALL_SCRIPT, TaskService

STATUS_TIMESTAMP = {
    "hospitalization_scheduled": "hosp_scheduled_at",
    "hospitalized": "hospitalized_at",
    "surgery_done": "surgery_at",
    "discharged": "discharged_at",
}


def _fact(route_id, patient_id=None) -> RouteFact | None:
    if not route_id:
        return None
    fact = RouteFact.objects.filter(pk=route_id).first()
    if fact is None and patient_id:
        fact = RouteFact.objects.create(route_id=route_id, patient_id=patient_id, created_at=clock.now())
    return fact


def _set_once(fact: RouteFact, field: str) -> None:
    if getattr(fact, field) is None:
        setattr(fact, field, clock.now())


@idempotent
def on_route_created(event: DomainEvent) -> None:
    p = event.payload
    RouteFact.objects.update_or_create(route_id=p["route_id"], defaults={
        "patient_id": p["patient_id"], "kind": p.get("kind", ""), "trigger_code": p.get("trigger_code", ""),
        "location": p.get("location_code", ""), "created_at": clock.now(), "status": "created",
        "parent_route_id": p.get("parent_id"),
    })
    CaseService().on_route_created(p)


@idempotent
def on_route_status(event: DomainEvent) -> None:
    p = event.payload
    fact = _fact(p["route_id"], p.get("patient_id"))
    if not fact:
        return
    fact.status = p["status"]
    for status, field in STATUS_TIMESTAMP.items():
        if p["status"] == status or (status == "hospitalization_scheduled" and p["status"] in ("hospitalized", "surgery_done", "discharged")):
            _set_once(fact, field)
    if p["status"] in ("discharged",):
        _set_once(fact, "surgery_at")
        _set_once(fact, "hospitalized_at")
    if event.event_type == contracts.ROUTE_CLOSED:
        fact.closed_at, fact.close_status = clock.now(), p["status"]
        TaskService().close_for_route(p["route_id"], f"Маршрут закрыт: {p['status']}")
    fact.save()


@idempotent
def on_notification_sent(event: DomainEvent) -> None:
    if fact := _fact(event.payload.get("route_id")):
        _set_once(fact, "notified_at")
        fact.save()


@idempotent
def on_booked(event: DomainEvent) -> None:
    if fact := _fact(event.payload.get("route_id")):
        _set_once(fact, "booked_at")
        fact.save()


@idempotent
def on_no_show(event: DomainEvent) -> None:
    if fact := _fact(event.payload.get("route_id")):
        fact.no_show_count += 1
        fact.save()


@idempotent
def on_visit_completed(event: DomainEvent) -> None:
    p = event.payload
    fact = _fact(p.get("route_id"))
    if not fact:
        return
    _set_once(fact, "visit_at")
    if fact.kind == "postop":
        _set_once(fact, "control_visit_at")
    if not fact.tactic:
        fact.tactic = p["tactic"]
        fact.doctor_agreed = p.get("agrees_with_ai_route", True)
    if p["tactic"] == "surgery_indicated":
        _set_once(fact, "surgery_recommended_at")
    fact.save()
    if not p.get("agrees_with_ai_route", True):
        # Врач не согласился с маршрутом ИИ — разбор координатором и материал для улучшения правил.
        DisputeService().open_review(p["route_id"], source="doctor_disagreed", reason=p.get("disagreement_reason", ""))
        TaskService().create(task_type=CoordinatorTask.TaskType.REVIEW_ROUTE, patient_id=p["patient_id"],
                             route_id=p["route_id"], dedupe_key=f"review:{p['route_id']}:{p['appointment_id']}")


@idempotent
def on_step_escalated(event: DomainEvent) -> None:
    p = event.payload
    if fact := _fact(p.get("route_id")):
        fact.escalations += 1
        fact.save()
    if p.get("action") != "coordinator_task":
        return
    from apps.doctors.facade import DoctorsFacade
    from apps.patients.facade import PatientsFacade

    patient = PatientsFacade.get_display(p["patient_id"]) or {}
    TaskService().create(
        task_type=p.get("task_type") or CoordinatorTask.TaskType.CALL_PATIENT, patient_id=p["patient_id"],
        route_id=p["route_id"], step_id=p["step_id"], responsible_unit=p.get("responsible_unit", ""),
        title=f"{dict(CoordinatorTask.TaskType.choices).get(p.get('task_type'), 'Задача')}: {p.get('title', '')}",
        script=CALL_SCRIPT.format(name=patient.get("display_name", "пациент"),
                                  specialty=DoctorsFacade.specialty_genitive(p.get("specialty_code", "")))
        if p.get("task_type") in ("call_patient", "", None) else "",
        dedupe_key=f"esc:{p['step_id']}:{p.get('attempt')}:{p.get('level')}:{p.get('task_type')}",
    )


@idempotent
def on_emergency(event: DomainEvent) -> None:
    p = event.payload
    TaskService().create(task_type=CoordinatorTask.TaskType.EMERGENCY, patient_id=p["patient_id"], route_id=p["route_id"],
                         title=f"ЭКСТРЕННО: {p.get('reason', '')}", dedupe_key=f"emergency:{p['route_id']}")


@idempotent
def on_patient_action(event: DomainEvent) -> None:
    p = event.payload
    if p["action"] == "callback":
        TaskService().create(task_type=CoordinatorTask.TaskType.CALLBACK, patient_id=p["patient_id"], route_id=p["route_id"],
                             dedupe_key=f"callback:{event.event_id}")


@idempotent
def on_audit_completed(event: DomainEvent) -> None:
    """Проверка рекомендаций нашла существенный пропуск — разбор и задача координатору."""
    AuditReviewService().open_from_event(event.payload)
    CaseService().on_audit(event.payload)


# ------------------------------------------------------------------ входящие протоколы (категории)
@idempotent
def on_document_uploaded(event: DomainEvent) -> None:
    CaseService().on_uploaded(event.payload)


@idempotent
def on_document_failed(event: DomainEvent) -> None:
    CaseService().on_failed(event.payload)


@idempotent
def on_document_processed(event: DomainEvent) -> None:
    CaseService().on_processed(event.payload, unmatched=False)


@idempotent
def on_document_unmatched(event: DomainEvent) -> None:
    p = event.payload
    CaseService().on_processed(p, unmatched=True)
    # Экстренная находка у неопределённого пациента: маршрут не строится, поэтому задача — сразу координатору.
    findings = (p.get("extraction") or {}).get("findings", [])
    if any(f.get("severity") == "emergency" and not f.get("negated") for f in findings):
        TaskService().create(task_type=CoordinatorTask.TaskType.EMERGENCY, patient_id=p["patient_id"],
                             title=f"ЭКСТРЕННО: установить пациента по протоколу {p.get('filename', '')} и связаться",
                             dedupe_key=f"emergency-unmatched:{p['document_id']}")


@idempotent
def on_document_annulled(event: DomainEvent) -> None:
    CaseService().on_annulled(event.payload)


@idempotent
def on_patient_identified(event: DomainEvent) -> None:
    CaseService().on_patient_identified(event.payload)
    TaskService().reassign_patient(event.payload["placeholder_id"], event.payload["patient_id"])


def register() -> None:
    bus = get_event_bus()
    bus.subscribe(contracts.ROUTE_CREATED, on_route_created)
    bus.subscribe(contracts.ROUTE_UPDATED, on_route_status)
    bus.subscribe(contracts.ROUTE_CLOSED, on_route_status)
    bus.subscribe(contracts.NOTIFICATION_SENT, on_notification_sent)
    bus.subscribe(contracts.APPOINTMENT_BOOKED, on_booked)
    bus.subscribe(contracts.APPOINTMENT_NO_SHOW, on_no_show)
    bus.subscribe(contracts.VISIT_COMPLETED, on_visit_completed)
    bus.subscribe(contracts.ROUTE_STEP_ESCALATED, on_step_escalated)
    bus.subscribe(contracts.EMERGENCY_FINDING, on_emergency)
    bus.subscribe(contracts.PATIENT_ACTION, on_patient_action)
    bus.subscribe(contracts.AUDIT_COMPLETED, on_audit_completed)
    bus.subscribe(contracts.DOCUMENT_UPLOADED, on_document_uploaded)
    bus.subscribe(contracts.DOCUMENT_FAILED, on_document_failed)
    bus.subscribe(contracts.DOCUMENT_PROCESSED, on_document_processed)
    bus.subscribe(contracts.DOCUMENT_CORRECTED, on_document_processed)
    bus.subscribe(contracts.DOCUMENT_UNMATCHED, on_document_unmatched)
    bus.subscribe(contracts.DOCUMENT_ANNULLED, on_document_annulled)
    bus.subscribe(contracts.PATIENT_IDENTIFIED, on_patient_identified)
