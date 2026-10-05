from django.apps import AppConfig


class DoctorsConfig(AppConfig):
    name = "apps.doctors"
    label = "doctors"
    verbose_name = "Модуль врача"

    def ready(self) -> None:
        from common import labels

        from . import handlers
        from .facade import DoctorsFacade
        from .models import Appointment, VisitOutcome

        handlers.register()
        labels.register_choices("appointment_status", Appointment.Status)
        labels.register_choices("booked_via", Appointment.BookedVia)
        labels.register_choices("tactic", VisitOutcome.Tactic)
        labels.register("location", {"": "Клиника не указана"})
        labels.register_resolver("location", lambda keys: DoctorsFacade.location_titles())
        labels.register_resolver("specialty", lambda keys: DoctorsFacade.specialty_titles())
