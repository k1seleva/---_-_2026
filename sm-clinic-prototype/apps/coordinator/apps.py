from django.apps import AppConfig


class CoordinatorConfig(AppConfig):
    name = "apps.coordinator"
    label = "coordinator"
    verbose_name = "Модуль координатора"

    def ready(self) -> None:
        from common import labels

        from . import handlers
        from .models import AdviceReview, CoordinatorTask, ProtocolCase, RouteReview
        from .services.cases import REASONS

        handlers.register()
        labels.register_choices("task_type", CoordinatorTask.TaskType)
        labels.register_choices("task_status", CoordinatorTask.Status)
        labels.register_choices("task_priority", [(str(v), t) for v, t in CoordinatorTask.Priority.choices])
        labels.register_choices("review_status", RouteReview.Status)
        labels.register_choices("review_source", RouteReview.Source)
        labels.register_choices("case_category", ProtocolCase.Category)
        labels.register_choices("advice_verdict", AdviceReview.Verdict)
        labels.register("case_reason", {r.code: r.title for r in REASONS})
        labels.register("funnel", {
            "triggered": "Исследования с триггерами", "notified": "Получили уведомление", "booked": "Записались",
            "visited": "Приём состоялся", "surgery_recommended": "Операция рекомендована", "referral": "Направление",
            "hosp_scheduled": "Госпитализация назначена", "hospitalized": "Госпитализированы", "operated": "Оперированы",
            "control_visit": "Контрольный визит",
        })
