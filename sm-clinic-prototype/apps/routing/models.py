"""
Routing Module — матрица маршрутизации (настройка) и маршруты пациентов (состояние).

Маршрут — циклический процесс: находка -> консультация -> решение врача -> (обследование |
госпитализация -> операция -> выписка) -> новый маршрут «послеоперационное наблюдение» ->
контрольный визит -> динамическое наблюдение ... до закрытия.
Каждый переход пишется в журнал RouteEvent (кто, что, когда, на каком основании).
"""
from django.db import models

from common.models import BaseModel


class StepType(models.TextChoices):
    CONSULTATION = "consultation", "Консультация специалиста"
    ONLINE_CONSULTATION = "online_consultation", "Онлайн-консультация"
    DIAGNOSTICS = "diagnostics", "Обследование"
    HOSPITALIZATION_REFERRAL = "hospitalization_referral", "Направление на госпитализацию"
    HOSPITALIZATION = "hospitalization", "Госпитализация"
    SURGERY = "surgery", "Операция"
    DISCHARGE = "discharge", "Выписка"
    FOLLOW_UP = "follow_up", "Контрольный визит"


# Этапы, которые пациент записывает сам (остальные ведёт стационар/менеджер госпитализации).
BOOKABLE_STEP_TYPES = {StepType.CONSULTATION, StepType.ONLINE_CONSULTATION, StepType.DIAGNOSTICS, StepType.FOLLOW_UP}


class EscalationPolicy(BaseModel):
    """Лестница эскалаций для «зависшего» этапа (сценарии 2 и 3 кейса).

    ladder: [{"after_hours": 24, "action": "notify", "template": "reminder_24h", "channels": ["lk", "push"]},
             {"after_hours": 120, "action": "coordinator_task", "task_type": "call_patient"},
             {"after_hours": 720, "action": "close_not_engaged"}]
    """

    code = models.SlugField(max_length=64, unique=True)
    title = models.CharField(max_length=255)
    ladder = models.JSONField(default=list)

    def __str__(self) -> str:
        return self.title


class RouteTemplate(BaseModel):
    """Шаблон маршрута (настройка): последовательность этапов для типового случая."""

    code = models.SlugField(max_length=64, unique=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)

    def __str__(self) -> str:
        return self.title


class RouteTemplateStep(models.Model):
    template = models.ForeignKey(RouteTemplate, on_delete=models.CASCADE, related_name="steps")
    order = models.PositiveSmallIntegerField()
    step_type = models.CharField(max_length=32, choices=StepType.choices)
    title = models.CharField(max_length=255)
    specialty_code = models.SlugField(max_length=64, blank=True)
    service_code = models.CharField(max_length=64, blank=True)
    offset_days = models.PositiveIntegerField(default=0, help_text="Через сколько дней после предыдущего этапа")
    window_days = models.PositiveIntegerField(default=7, help_text="Целевой срок выполнения этапа")
    auto_book = models.BooleanField(default=False, help_text="Записать пациента заранее (послеоперационный контроль)")
    escalation_policy = models.ForeignKey(EscalationPolicy, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["template", "order"]


class TriggerRule(BaseModel):
    """Строка матрицы маршрутизации: находка (+ условия по атрибутам) -> маршрут.

    conditions: [{"attr": "birads", "op": "gte", "value": 3}]; находки с отрицанием не срабатывают никогда.
    """

    code = models.SlugField(max_length=64)
    version = models.PositiveIntegerField(default=1)
    title = models.CharField(max_length=255)
    finding_code = models.SlugField(max_length=64, db_index=True)
    conditions = models.JSONField(default=list, blank=True)
    priority = models.PositiveSmallIntegerField(default=100, help_text="Меньше — важнее (при нескольких находках)")
    route_group = models.SlugField(max_length=64, blank=True,
                                   help_text="Клиническое направление: из одного протокола — один маршрут на группу "
                                             "(полип + миома -> один гинекологический маршрут)")
    template = models.ForeignKey(RouteTemplate, on_delete=models.PROTECT, related_name="rules")
    first_specialty_code = models.SlugField(max_length=64)
    potential_route = models.CharField(max_length=255, blank=True, help_text="Подсказка врачу: «Гистероскопия»")
    target_days = models.PositiveIntegerField(default=7, help_text="Целевой срок до консультации")
    responsible_unit = models.CharField(max_length=255, blank=True)
    is_surgical = models.BooleanField(default=False)
    is_emergency = models.BooleanField(default=False, help_text="Только эскалация персоналу, без сообщений пациенту")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["priority", "code"]
        constraints = [models.UniqueConstraint(fields=["code", "version"], name="uniq_rule_version")]

    def __str__(self) -> str:
        return f"{self.code} v{self.version}"


class PatientRoute(BaseModel):
    class Kind(models.TextChoices):
        TRIGGER = "trigger", "По клинически значимой находке"
        RECOMMENDATION = "recommendation", "По рекомендации в протоколе"
        POSTOP = "postop", "Послеоперационное наблюдение"
        OBSERVATION = "observation", "Динамическое наблюдение"

    class Status(models.TextChoices):
        CREATED = "created", "Маршрут создан"
        NOTIFIED = "notified", "Пациент уведомлён"
        BOOKED = "booked", "Записан к специалисту"
        REBOOKING_REQUIRED = "rebooking_required", "Требуется повторная запись"
        NO_SHOW = "no_show", "Неявка"
        VISIT_DONE = "visit_done", "Приём состоялся"
        IN_DIAGNOSTICS = "in_diagnostics", "Дообследование"
        REFERRED_HOSPITALIZATION = "referred_hospitalization", "Направлен на госпитализацию"
        HOSPITALIZATION_SCHEDULED = "hospitalization_scheduled", "Госпитализация назначена"
        HOSPITALIZED = "hospitalized", "Госпитализирован"
        SURGERY_DONE = "surgery_done", "Операция выполнена"
        DISCHARGED = "discharged", "Выписан"
        OBSERVATION = "observation", "Динамическое наблюдение"
        COMPLETED = "completed", "Маршрут завершён"
        NOT_ENGAGED = "not_engaged", "Маршрут не реализован / пациент не вовлечён"
        DECLINED = "declined", "Пациент отказался"
        SEEN_ELSEWHERE = "seen_elsewhere", "Обратился в другую клинику"
        CANCELLED = "cancelled", "Отменён (протокол аннулирован/исправлен)"

    CLOSED_STATUSES = {
        Status.COMPLETED, Status.NOT_ENGAGED, Status.DECLINED, Status.SEEN_ELSEWHERE, Status.CANCELLED,
    }

    patient_id = models.UUIDField(db_index=True)
    source_document_id = models.UUIDField(null=True, blank=True, db_index=True)
    source_external_id = models.CharField(max_length=128, blank=True, db_index=True)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.TRIGGER)
    trigger_rule = models.ForeignKey(TriggerRule, on_delete=models.PROTECT, null=True, blank=True)
    trigger_code = models.SlugField(max_length=64, blank=True)
    rule_version = models.PositiveIntegerField(null=True, blank=True)
    reason = models.CharField(max_length=500, help_text="«Полип эндометрия (УЗИ ОМТ от 26.08.2026)»")
    evidence = models.JSONField(default=dict, blank=True, help_text="Цитата, атрибуты, уверенность")
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.CREATED, db_index=True)
    parent = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="children")
    cycle_no = models.PositiveSmallIntegerField(default=1, help_text="Номер витка цикла маршрута")
    responsible_unit = models.CharField(max_length=255, blank=True)
    location_code = models.CharField(max_length=64, blank=True)
    detected_at = models.DateTimeField()
    target_date = models.DateField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    close_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Повторная доставка того же протокола не создаёт дублей маршрута.
            models.UniqueConstraint(
                fields=["patient_id", "source_external_id", "trigger_code"],
                condition=~models.Q(source_external_id="") & ~models.Q(status="cancelled"),
                name="uniq_route_per_trigger",
            )
        ]

    def __str__(self) -> str:
        return f"{self.reason} [{self.get_status_display()}]"

    @property
    def is_open(self) -> bool:
        return self.status not in self.CLOSED_STATUSES

    @property
    def active_step(self) -> "RouteStep | None":
        return self.steps.filter(status__in=RouteStep.ACTIVE_STATUSES).order_by("order").first()


class RouteStep(BaseModel):
    class Status(models.TextChoices):
        PLANNED = "planned", "Запланирован (ожидает срока)"
        AWAITING_BOOKING = "awaiting_booking", "Ожидает записи"
        BOOKED = "booked", "Записан"
        DONE = "done", "Выполнен"
        MISSED = "missed", "Неявка"
        CANCELLED = "cancelled", "Отменён"
        SKIPPED = "skipped", "Пропущен (решение врача/координатора)"

    ACTIVE_STATUSES = (Status.AWAITING_BOOKING, Status.BOOKED)

    class Source(models.TextChoices):
        RULE = "rule", "Матрица маршрутизации"
        AI_RECOMMENDATION = "ai_recommendation", "Рекомендация из протокола (AI)"
        DOCTOR = "doctor", "Назначение врача"
        COORDINATOR = "coordinator", "Корректировка координатора"
        SYSTEM = "system", "Система (МИС)"

    route = models.ForeignKey(PatientRoute, on_delete=models.CASCADE, related_name="steps")
    order = models.PositiveSmallIntegerField()
    step_type = models.CharField(max_length=32, choices=StepType.choices)
    title = models.CharField(max_length=255)
    specialty_code = models.SlugField(max_length=64, blank=True)
    service_code = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PLANNED, db_index=True)
    source = models.CharField(max_length=24, choices=Source.choices, default=Source.RULE)
    offset_days = models.PositiveIntegerField(default=0, help_text="Таймер: через сколько дней после предыдущего этапа")
    window_days = models.PositiveIntegerField(default=7)
    earliest_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    auto_book = models.BooleanField(default=False)
    activated_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    appointment_id = models.UUIDField(null=True, blank=True)
    escalation_policy = models.ForeignKey(EscalationPolicy, on_delete=models.SET_NULL, null=True, blank=True)
    escalation_level = models.PositiveSmallIntegerField(default=0)
    attempt = models.PositiveSmallIntegerField(default=1, help_text="Номер попытки записи (после неявки/отмены)")
    outcome = models.JSONField(default=dict, blank=True, help_text="Тактика врача по итогам этапа")
    comment = models.TextField(blank=True)

    class Meta:
        ordering = ["route", "order"]

    def __str__(self) -> str:
        return f"Этап {self.order}: {self.title}"


class RouteEvent(models.Model):
    """Журнал маршрута — аудит и источник для аналитики."""

    route = models.ForeignKey(PatientRoute, on_delete=models.CASCADE, related_name="events")
    step = models.ForeignKey(RouteStep, on_delete=models.SET_NULL, null=True, blank=True)
    event_type = models.CharField(max_length=64)
    from_status = models.CharField(max_length=32, blank=True)
    to_status = models.CharField(max_length=32, blank=True)
    actor_type = models.CharField(max_length=24, default="system", help_text="system / ai / patient / doctor / coordinator / mis")
    actor_id = models.CharField(max_length=64, blank=True)
    basis = models.CharField(max_length=500, blank=True, help_text="Основание: правило+версия, событие МИС, решение врача")
    payload = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField()

    class Meta:
        ordering = ["occurred_at", "id"]
