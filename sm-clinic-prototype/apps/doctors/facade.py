"""Публичный интерфейс модуля врача: поиск слотов, запись, справочники."""
from .models import Appointment, ClinicLocation, Doctor, Specialty
from .services.booking import BookingError, BookingService, SlotFinder

__all__ = ["DoctorsFacade", "BookingError"]


class DoctorsFacade:
    @staticmethod
    def find_slots(specialty_code: str, *, limit: int = 8, location_code: str | None = None) -> list[dict]:
        return [
            {"id": str(s.id), "starts_at": s.starts_at, "doctor": s.doctor.full_name, "doctor_id": str(s.doctor_id),
             "location": s.location.title, "location_code": s.location_id, "format": s.format,
             "specialty_code": s.specialty_id}
            for s in SlotFinder().find(specialty_code, limit=limit, location_code=location_code)
        ]

    @staticmethod
    def slot_specialty(slot_id) -> str | None:
        from .models import ScheduleSlot

        return ScheduleSlot.objects.filter(pk=slot_id).values_list("specialty_id", flat=True).first()

    @staticmethod
    def book(*, slot_id, patient_id, route_id, route_step_id, booked_via="patient") -> dict:
        appointment = BookingService().book(slot_id=slot_id, patient_id=patient_id, route_id=route_id,
                                            route_step_id=route_step_id, booked_via=booked_via)
        return BookingService._payload(appointment)

    @staticmethod
    def cancel(appointment_id, patient_id) -> None:
        appointment = Appointment.objects.select_related("slot").get(pk=appointment_id, patient_id=patient_id)
        BookingService().cancel(appointment)

    @staticmethod
    def patient_appointments(patient_id) -> list[dict]:
        return [
            {**BookingService._payload(a), "status": a.status, "status_display": a.get_status_display()}
            for a in Appointment.objects.select_related("slot__doctor", "slot__location").filter(patient_id=patient_id)
        ]

    @staticmethod
    def doctor_for_user(user_id) -> str:
        """Карточка врача, привязанная к учётной записи (вход врача)."""
        doctor = Doctor.objects.filter(user_id=user_id, is_active=True).values_list("id", flat=True).first()
        return str(doctor) if doctor else ""

    @staticmethod
    def specialty_titles() -> dict[str, str]:
        return dict(Specialty.objects.values_list("code", "title"))

    @staticmethod
    def specialty_genitive(code: str, default: str = "профильного специалиста") -> str:
        """«консультация {гинеколога}» — для текстов уведомлений и этапов."""
        value = Specialty.objects.filter(pk=code).values_list("title_genitive", flat=True).first() if code else None
        return value or default

    @staticmethod
    def location_titles() -> dict[str, str]:
        return dict(ClinicLocation.objects.values_list("code", "title"))
