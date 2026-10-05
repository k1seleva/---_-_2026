"""Самостоятельная запись пациента — строго по маршруту."""
from apps.doctors.facade import BookingError, DoctorsFacade
from apps.routing.facade import RoutingFacade
from common.events import contracts
from common.events.bus import publish

from ..models import Patient, PatientAction


class PatientBookingError(Exception):
    pass


class PatientBookingService:
    def available_slots(self, patient: Patient, step_id) -> tuple[dict, list[dict]]:
        step = RoutingFacade.get_step(step_id)
        if not step or step["patient_id"] != str(patient.id):
            raise PatientBookingError("Этап маршрута не найден")
        # Слоты только тех специалистов, которые соответствуют этапу маршрута.
        slots = DoctorsFacade.find_slots(step["specialty_code"], limit=8) if step["specialty_code"] else []
        return step, slots

    def book(self, patient: Patient, step_id, slot_id) -> dict:
        specialty = DoctorsFacade.slot_specialty(slot_id) or ""
        ok, reason = RoutingFacade.validate_booking(step_id, patient.id, specialty)
        if not ok:
            raise PatientBookingError(reason)
        step = RoutingFacade.get_step(step_id)
        try:
            appointment = DoctorsFacade.book(slot_id=slot_id, patient_id=patient.id, route_id=step["route_id"],
                                             route_step_id=step_id, booked_via="patient")
        except BookingError as exc:
            raise PatientBookingError(str(exc)) from exc
        PatientAction.objects.create(patient=patient, route_id=step["route_id"], action=PatientAction.Action.BOOK)
        return appointment

    def route_action(self, patient: Patient, route_id, action: str, comment: str = "") -> PatientAction:
        """Кнопки из уведомления: «Уже обратился к врачу», «Не планирую обращаться», «Перезвоните мне»."""
        if action not in PatientAction.Action.values:
            raise PatientBookingError("Неизвестное действие")
        record = PatientAction.objects.create(patient=patient, route_id=route_id, action=action, comment=comment)
        publish(contracts.PATIENT_ACTION, {"patient_id": str(patient.id), "route_id": str(route_id), "action": action,
                                           "comment": comment})
        return record
