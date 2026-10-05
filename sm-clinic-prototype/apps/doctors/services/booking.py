"""Запись, отмена, неявка и завершение приёма."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction

from common import clock
from common.events import contracts
from common.events.bus import publish

from ..models import Appointment, Doctor, Prescription, ScheduleSlot, Specialty, VisitOutcome


class BookingError(Exception):
    pass


class SlotFinder:
    """Ближайшие подходящие слоты (этап 4 кейса: не заставлять пациента искать врача)."""

    def find(self, specialty_code: str, *, limit: int = 8, from_dt: datetime | None = None,
             location_code: str | None = None, include_online: bool = True, profile: str | None = None):
        start = from_dt or clock.now()
        qs = ScheduleSlot.objects.select_related("doctor", "location", "specialty").filter(
            specialty_id=specialty_code, status=ScheduleSlot.Status.FREE, starts_at__gte=start, doctor__is_active=True,
        )
        if location_code:
            qs = qs.filter(location_id__in=[location_code, "online"] if include_online else [location_code])
        elif not include_online:
            qs = qs.exclude(format=ScheduleSlot.Format.ONLINE)
        if profile:
            qs = qs.filter(doctor__surgical_profiles__contains=[profile])
        return list(qs.order_by("starts_at")[:limit])


class BookingService:
    @transaction.atomic
    def book(self, *, slot_id, patient_id, route_id=None, route_step_id=None, source_document_id=None,
             booked_via: str = Appointment.BookedVia.PATIENT) -> Appointment:
        # select_for_update — защита от двойной записи на один слот.
        slot = ScheduleSlot.objects.select_for_update().select_related("doctor", "location", "specialty").filter(pk=slot_id).first()
        if not slot or slot.status != ScheduleSlot.Status.FREE:
            raise BookingError("Слот уже занят, выберите другое время")
        if slot.starts_at < clock.now():
            raise BookingError("Нельзя записаться на прошедшее время")
        slot.status = ScheduleSlot.Status.BOOKED
        slot.save(update_fields=["status", "updated_at"])
        appointment = Appointment.objects.create(
            slot=slot, patient_id=patient_id, route_id=route_id, route_step_id=route_step_id,
            source_document_id=source_document_id, booked_via=booked_via,
        )
        publish(contracts.APPOINTMENT_BOOKED, self._payload(appointment))
        return appointment

    @transaction.atomic
    def cancel(self, appointment: Appointment, *, by: str = "patient") -> Appointment:
        if appointment.status not in (Appointment.Status.SCHEDULED, Appointment.Status.CONFIRMED):
            raise BookingError("Запись нельзя отменить в текущем статусе")
        appointment.status = Appointment.Status.CANCELLED
        appointment.save(update_fields=["status", "updated_at"])
        ScheduleSlot.objects.filter(pk=appointment.slot_id).update(status=ScheduleSlot.Status.FREE)
        publish(contracts.APPOINTMENT_CANCELLED, {**self._payload(appointment), "cancelled_by": by})
        return appointment

    @transaction.atomic
    def mark_no_show(self, appointment: Appointment) -> Appointment:
        appointment.status = Appointment.Status.NO_SHOW
        appointment.save(update_fields=["status", "updated_at"])
        publish(contracts.APPOINTMENT_NO_SHOW, self._payload(appointment))
        return appointment

    def auto_book(self, *, specialty_code: str, patient_id, route_id, route_step_id, earliest: datetime,
                  latest: datetime, location_code: str = "") -> Appointment | None:
        """Заблаговременная запись (контроль после выписки, этап 11): первый свободный слот в окне."""
        for slot in SlotFinder().find(specialty_code, from_dt=earliest, location_code=location_code or None, limit=20):
            if slot.starts_at > latest:
                break
            try:
                return self.book(slot_id=slot.id, patient_id=patient_id, route_id=route_id,
                                 route_step_id=route_step_id, booked_via=Appointment.BookedVia.AUTO)
            except BookingError:
                continue
        return None

    @staticmethod
    def _payload(a: Appointment) -> dict:
        slot = a.slot
        return {
            "appointment_id": str(a.id), "patient_id": str(a.patient_id),
            "route_id": str(a.route_id) if a.route_id else None,
            "route_step_id": str(a.route_step_id) if a.route_step_id else None,
            "doctor_id": str(slot.doctor_id), "doctor_name": slot.doctor.full_name,
            "specialty_code": slot.specialty_id, "location": slot.location.title, "format": slot.format,
            "starts_at": slot.starts_at.isoformat(), "booked_via": a.booked_via,
        }


@dataclass
class PrescriptionInput:
    kind: str
    title: str
    specialty_code: str = ""
    service_code: str = ""
    due_in_days: int | None = None
    comment: str = ""


@dataclass
class VisitCompletion:
    tactic: str
    prescriptions: list[PrescriptionInput] = field(default_factory=list)
    next_specialty_code: str = ""
    agrees_with_ai_route: bool = True
    disagreement_reason: str = ""
    comment: str = ""


class VisitService:
    """Завершение приёма. Без тактики — ошибка: «просто завершить приём» нельзя."""

    @transaction.atomic
    def complete(self, appointment: Appointment, doctor: Doctor, data: VisitCompletion) -> VisitOutcome:
        if data.tactic not in VisitOutcome.Tactic.values:
            raise BookingError("Выберите тактику ведения пациента")
        if data.tactic == VisitOutcome.Tactic.OTHER_PROFILE and not data.next_specialty_code:
            raise BookingError("Укажите профиль, в который направлен пациент")
        if not data.agrees_with_ai_route and not data.disagreement_reason:
            raise BookingError("Укажите, почему маршрут скорректирован — это нужно координатору")
        if hasattr(appointment, "outcome"):
            raise BookingError("Приём уже завершён")
        outcome = VisitOutcome.objects.create(
            appointment=appointment, doctor=doctor, tactic=data.tactic,
            next_specialty=Specialty.objects.filter(pk=data.next_specialty_code).first() if data.next_specialty_code else None,
            agrees_with_ai_route=data.agrees_with_ai_route, disagreement_reason=data.disagreement_reason, comment=data.comment,
        )
        for p in data.prescriptions:
            Prescription.objects.create(
                outcome=outcome, kind=p.kind, title=p.title, service_code=p.service_code, due_in_days=p.due_in_days,
                specialty=Specialty.objects.filter(pk=p.specialty_code).first() if p.specialty_code else None, comment=p.comment,
            )
        appointment.status = Appointment.Status.COMPLETED
        appointment.save(update_fields=["status", "updated_at"])
        publish(contracts.VISIT_COMPLETED, {
            **BookingService._payload(appointment), "tactic": data.tactic, "next_specialty_code": data.next_specialty_code,
            "agrees_with_ai_route": data.agrees_with_ai_route, "disagreement_reason": data.disagreement_reason,
            "comment": data.comment,
            "prescriptions": [p.__dict__ for p in data.prescriptions],
        })
        return outcome


class ScheduleService:
    """Генерация сетки слотов (в пилоте — синхронизация с сервисом расписания 1С)."""

    def generate(self, doctor: Doctor, specialty: Specialty, location, *, days: int = 14, start_hour: int = 9,
                 end_hour: int = 18, slot_minutes: int = 30, step_minutes: int = 90) -> int:
        created = 0
        base = clock.now().replace(minute=0, second=0, microsecond=0)
        for d in range(days):
            day = base + timedelta(days=d)
            if day.weekday() == 6:
                continue
            t = day.replace(hour=start_hour)
            while t.hour < end_hour:
                _, was_created = ScheduleSlot.objects.get_or_create(
                    doctor=doctor, starts_at=t,
                    defaults={"specialty": specialty, "location": location, "ends_at": t + timedelta(minutes=slot_minutes),
                              "format": ScheduleSlot.Format.ONLINE if location.is_online else ScheduleSlot.Format.OFFLINE},
                )
                created += int(was_created)
                t += timedelta(minutes=step_minutes)
        return created
