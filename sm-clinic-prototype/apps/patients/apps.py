from django.apps import AppConfig


class PatientsConfig(AppConfig):
    name = "apps.patients"
    label = "patients"
    verbose_name = "Модуль пациента"

    def ready(self) -> None:
        from common import labels

        from . import handlers
        from .models import Notification, NotificationPreference, PatientAction

        handlers.register()
        labels.register_choices("channel", Notification.Channel)
        labels.register_choices("notification_status", Notification.Status)
        labels.register_choices("event_group", NotificationPreference.EventGroup)
        labels.register_choices("patient_action", PatientAction.Action)
        labels.register("notification_template", {
            "result_ready": "Результат готов", "next_step": "Следующий этап", "timer_due": "Срок контрольного исследования",
            "rebooking": "Нужна повторная запись", "reminder_24h": "Напоминание через сутки",
            "reminder_72h": "Напоминание через 3 дня", "final_soft": "Последнее напоминание", "no_show": "Неявка",
            "booking_confirmed": "Запись подтверждена", "postop_booked": "Контрольный приём назначен",
            "postop_choose_time": "Выбор времени контрольного приёма",
        })
