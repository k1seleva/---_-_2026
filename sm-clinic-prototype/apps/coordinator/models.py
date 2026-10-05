"""
Coordinator Module — задачи координатора, разбор спорных маршрутов (сравнение маркеров ИИ
с маршрутом врача), корректировки и аналитика.

RouteFact — проекция (read model) по событиям всех модулей (CQRS): аналитика не делает
JOIN по чужим таблицам и переживёт разделение на микросервисы без изменений.
"""
from django.conf import settings
from django.db import models

from common.models import BaseModel


class CoordinatorTask(BaseModel):
    class TaskType(models.TextChoices):
        CALL_PATIENT = "call_patient", "Позвонить пациенту (5-7 дней без записи)"
        CALLBACK = "callback", "Обратный звонок по заявке пациента"
        HOSPITALIZATION_DATE = "hospitalization_date", "Назначить дату госпитализации"
        HEAD_ESCALATION = "head_escalation", "Эскалация руководителю"
        BOOK_FOLLOW_UP = "book_follow_up", "Записать на контрольный приём"
        EMERGENCY = "emergency", "Экстренная находка — немедленно связаться"
        REVIEW_ROUTE = "review_route", "Проверить маршрут (расхождение ИИ и врача)"
        REVIEW_RECOMMENDATIONS = "review_recommendations", "Обсудить рекомендации (возможен пропуск показания)"

    class Status(models.TextChoices):
        OPEN = "open", "Открыта"
        IN_PROGRESS = "in_progress", "В работе"
        DONE = "done", "Выполнена"
        CANCELLED = "cancelled", "Отменена"

    class Priority(models.IntegerChoices):
        CRITICAL = 1, "Критичный"
        HIGH = 2, "Высокий"
        NORMAL = 3, "Обычный"

    route_id = models.UUIDField(db_index=True, null=True, blank=True)
    route_step_id = models.UUIDField(null=True, blank=True)
    patient_id = models.UUIDField(db_index=True)
    task_type = models.CharField(max_length=32, choices=TaskType.choices)
    priority = models.PositiveSmallIntegerField(choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN, db_index=True)
    title = models.CharField(max_length=255)
    script = models.TextField(blank=True, help_text="Скрипт звонка")
    due_at = models.DateTimeField(null=True, blank=True)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    responsible_unit = models.CharField(max_length=255, blank=True)
    resolution = models.TextField(blank=True)
    dedupe_key = models.CharField(max_length=255, unique=True)

    class Meta:
        ordering = ["status", "priority", "due_at"]


class RouteReview(BaseModel):
    """Спор / разбор маршрута: исходные маркеры ИИ vs маршрут, назначенный врачом."""

    class Status(models.TextChoices):
        OPEN = "open", "Открыт"
        CORRECTED = "corrected", "Маршрут скорректирован"
        CONFIRMED = "confirmed", "Маршрут врача подтверждён"
        AI_ERROR = "ai_error", "Ошибка распознавания (в разметку)"

    class Source(models.TextChoices):
        DOCTOR_DISAGREED = "doctor_disagreed", "Врач не согласился с маршрутом ИИ"
        COORDINATOR = "coordinator", "Открыт координатором"
        PATIENT = "patient", "Обращение пациента"
        RECOMMENDATION_AUDIT = "recommendation_audit", "Проверка рекомендаций: возможен пропуск показания"

    route_id = models.UUIDField(db_index=True, null=True, blank=True,
                                help_text="Пусто, если по протоколу ещё нет маршрута (его создаст решение координатора)")
    audit_id = models.UUIDField(null=True, blank=True, db_index=True, help_text="Проверка рекомендаций (модуль audit)")
    patient_id = models.UUIDField()
    document_id = models.UUIDField(null=True, blank=True)
    source = models.CharField(max_length=24, choices=Source.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    ai_markers = models.JSONField(default=list, help_text="Снимок находок ИИ с цитатами")
    ai_route = models.JSONField(default=dict, help_text="Маршрут, предложенный матрицей")
    doctor_route = models.JSONField(default=dict, help_text="Тактика и назначения врача")
    discrepancies = models.JSONField(default=list)
    reason = models.TextField(blank=True)
    resolution_comment = models.TextField(blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)


class RouteCorrection(BaseModel):
    review = models.ForeignKey(RouteReview, on_delete=models.CASCADE, related_name="corrections", null=True, blank=True)
    route_id = models.UUIDField(db_index=True, null=True, blank=True)
    operations = models.JSONField(help_text="Операции для RoutePlanner.apply_correction")
    reason = models.TextField()
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)


class RouteFact(models.Model):
    """Проекция для дашборда: одна строка на маршрут, заполняется обработчиками событий."""

    route_id = models.UUIDField(primary_key=True)
    patient_id = models.UUIDField(db_index=True)
    kind = models.CharField(max_length=16, blank=True)
    trigger_code = models.CharField(max_length=128, blank=True, db_index=True)
    location = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(null=True)
    notified_at = models.DateTimeField(null=True)
    booked_at = models.DateTimeField(null=True)
    visit_at = models.DateTimeField(null=True)
    tactic = models.CharField(max_length=32, blank=True)
    doctor_agreed = models.BooleanField(null=True)
    surgery_recommended_at = models.DateTimeField(null=True)
    hosp_scheduled_at = models.DateTimeField(null=True)
    hospitalized_at = models.DateTimeField(null=True)
    surgery_at = models.DateTimeField(null=True)
    discharged_at = models.DateTimeField(null=True)
    control_visit_at = models.DateTimeField(null=True)
    no_show_count = models.PositiveSmallIntegerField(default=0)
    escalations = models.PositiveSmallIntegerField(default=0)
    closed_at = models.DateTimeField(null=True)
    close_status = models.CharField(max_length=32, blank=True)
    parent_route_id = models.UUIDField(null=True, blank=True)


class MetricSnapshot(models.Model):
    """Ежечасный снимок метрик воронки — история для трендов."""

    taken_at = models.DateTimeField(db_index=True)
    metrics = models.JSONField()


class Tag(BaseModel):
    """Метка для быстрой идентификации. Системные метки ставит сама система (причины категорий),
    свои метки создаёт координатор («Перезвонить», «Нужен переводчик»)."""

    class Color(models.TextChoices):
        RED = "red", "Красный — только экстренное"
        AMBER = "amber", "Янтарный — проверить"
        GREEN = "green", "Зелёный — в порядке"
        BLUE = "blue", "Синий — информация"
        VIOLET = "violet", "Фиолетовый"
        GRAY = "gray", "Серый"

    code = models.SlugField(max_length=64, unique=True)
    title = models.CharField(max_length=64)
    color = models.CharField(max_length=8, choices=Color.choices, default=Color.BLUE)
    description = models.CharField(max_length=255, blank=True)
    is_system = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_system", "title"]

    def __str__(self) -> str:
        return self.title


class ProtocolCase(models.Model):
    """Проекция «протокол во входящих координатора»: одна строка на загруженный протокол.

    Категория — ровно одна, по первому сработавшему правилу (services/cases.py: categorize):
    экстренно → ошибка обработки → пациент не определён → нужна проверка → маршрут запущен → без находок.
    Причины (reasons) — системные метки, они объясняют категорию прямо в строке списка.
    """

    class Category(models.TextChoices):
        PROCESSING = "processing", "В обработке"
        EMERGENCY = "emergency", "Экстренно"
        FAILED = "failed", "Ошибка обработки"
        UNMATCHED = "unmatched", "Пациент не определён"
        NEEDS_REVIEW = "needs_review", "Нужна проверка"
        ROUTED = "routed", "Маршрут запущен"
        NO_FINDINGS = "no_findings", "Без находок"
        CLOSED = "closed", "Снято с разбора"

    document_id = models.UUIDField(primary_key=True)
    patient_id = models.UUIDField(null=True, blank=True, db_index=True)
    is_placeholder = models.BooleanField(default=False)
    card_number = models.CharField(max_length=64, blank=True)
    batch_id = models.UUIDField(null=True, blank=True, db_index=True)
    location = models.CharField(max_length=64, blank=True, db_index=True)
    filename = models.CharField(max_length=255, blank=True)
    study_type = models.CharField(max_length=128, blank=True)
    study_date = models.DateField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    external_id = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=16, default="uploaded", help_text="Статус документа в модуле обработки")
    error = models.CharField(max_length=500, blank=True)
    findings = models.JSONField(default=list, blank=True, help_text="Коды находок (без отрицаний)")
    explanation = models.JSONField(default=list, blank=True,
                                   help_text='Почему так: [{"finding", "quote", "rule"}] — для строки списка')
    route_ids = models.JSONField(default=list, blank=True)
    audit_verdict = models.CharField(max_length=16, blank=True)
    reasons = models.JSONField(default=list, blank=True, help_text="Коды системных меток")
    category = models.CharField(max_length=16, choices=Category.choices, default=Category.PROCESSING, db_index=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.CharField(max_length=150, blank=True)
    tags = models.ManyToManyField(Tag, blank=True, related_name="cases")
    uploaded_at = models.DateTimeField(null=True, blank=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-uploaded_at"]


class AdviceReview(BaseModel):
    """Оценка координатором совета ИИ-агента по маршрутизации (принять / отклонить / скорректировать).

    Отдельная таблица: совет живёт в модуле обработки, здесь только ссылка advice_id (без внешнего ключа —
    модули связаны фасадами, а не таблицами) и снимок совета на момент оценки. Оценка маршрут не меняет:
    правки маршрута координатор делает отдельными действиями с причиной.
    """

    class Verdict(models.TextChoices):
        ACCEPT = "accept", "Принять"
        REJECT = "reject", "Отклонить"
        CORRECT = "correct", "Скорректировать"

    user_id = models.CharField(max_length=150, db_index=True, help_text="Кто оценил (логин сотрудника)")
    advice_id = models.UUIDField(db_index=True)
    document_id = models.UUIDField(db_index=True)
    verdict = models.CharField(max_length=16, choices=Verdict.choices)
    comment = models.TextField(blank=True)
    corrected_route_code = models.CharField(max_length=64, blank=True, help_text="Маршрут, который предлагает координатор")
    advice_snapshot = models.JSONField(default=dict, blank=True, help_text="Совет на момент оценки (текст, маршрут, уверенность)")
    timestamp = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-timestamp"]
