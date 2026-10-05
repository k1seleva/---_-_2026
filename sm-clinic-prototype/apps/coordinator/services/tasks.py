from datetime import timedelta

from django.db import IntegrityError, transaction

from common import clock

from ..models import CoordinatorTask

CALL_SCRIPT = (
    "Добрый день, {name}. Вы недавно проходили у нас исследование. По его результатам рекомендована консультация "
    "{specialty} для определения дальнейшей тактики. Мы видим, что консультация пока не состоялась. "
    "Могу помочь подобрать врача и удобное время — очно или онлайн."
)

TASK_DEFAULTS = {
    CoordinatorTask.TaskType.CALL_PATIENT: (CoordinatorTask.Priority.NORMAL, 24, "Позвонить пациенту: нет записи 5-7 дней"),
    CoordinatorTask.TaskType.CALLBACK: (CoordinatorTask.Priority.HIGH, 4, "Пациент просит перезвонить"),
    CoordinatorTask.TaskType.HOSPITALIZATION_DATE: (CoordinatorTask.Priority.HIGH, 24, "Назначить дату госпитализации"),
    CoordinatorTask.TaskType.HEAD_ESCALATION: (CoordinatorTask.Priority.CRITICAL, 24, "Эскалация руководителю: нет даты госпитализации 5-7 дней"),
    CoordinatorTask.TaskType.BOOK_FOLLOW_UP: (CoordinatorTask.Priority.HIGH, 24, "Записать на контрольный приём после выписки"),
    CoordinatorTask.TaskType.EMERGENCY: (CoordinatorTask.Priority.CRITICAL, 1, "Экстренная находка — связаться немедленно"),
    CoordinatorTask.TaskType.REVIEW_ROUTE: (CoordinatorTask.Priority.NORMAL, 48, "Проверить маршрут: врач изменил предложенный ИИ"),
    CoordinatorTask.TaskType.REVIEW_RECOMMENDATIONS: (CoordinatorTask.Priority.HIGH, 24,
                                                      "Обсудить рекомендации: возможен пропуск показания"),
}


class TaskService:
    def create(self, *, task_type: str, patient_id, route_id=None, step_id=None, title: str = "", script: str = "",
               responsible_unit: str = "", dedupe_key: str, priority: int | None = None) -> CoordinatorTask | None:
        default_priority, sla_hours, default_title = TASK_DEFAULTS.get(task_type, (CoordinatorTask.Priority.NORMAL, 24, task_type))
        priority = priority or default_priority
        try:
            with transaction.atomic():
                return CoordinatorTask.objects.create(
                    task_type=task_type, patient_id=patient_id, route_id=route_id, route_step_id=step_id,
                    priority=priority, title=title or default_title, script=script,
                    due_at=clock.now() + timedelta(hours=sla_hours), responsible_unit=responsible_unit,
                    dedupe_key=dedupe_key[:255],
                )
        except IntegrityError:
            return None

    def close_for_route(self, route_id, resolution: str) -> int:
        return CoordinatorTask.objects.filter(route_id=route_id, status__in=["open", "in_progress"]).update(
            status=CoordinatorTask.Status.CANCELLED, resolution=resolution)

    def reassign_patient(self, placeholder_id, patient_id) -> int:
        """Задачи обезличенной карточки переходят к определённому пациенту (экстренные не теряются)."""
        return CoordinatorTask.objects.filter(patient_id=placeholder_id, status__in=["open", "in_progress"]).update(
            patient_id=patient_id)
