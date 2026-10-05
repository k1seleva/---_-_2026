"""
Audit Module — проверка достаточности рекомендаций врача (необходимость коррекции маршрута).

На вход — что рекомендовал врач (рекомендации в протоколе или назначения на приёме) и
размеченные находки протокола. Модуль сверяет их с матрицей показаний (IndicationRule) и
находит пропуски: «есть показание к консультации хирурга, а в рекомендациях — „лечащий врач“».
Существенные пропуски уходят координатору на обсуждение; решение координатора применяется
к маршруту через событие, а итог (принято/отклонено) возвращается сюда — для точности правил.

Слабая связность: пациент, протокол и маршрут — UUID из других модулей, без ForeignKey.
"""
from django.db import models

from common.models import BaseModel


class IndicationRule(BaseModel):
    """Строка матрицы показаний (настройка, редактируется в админке): находка (+ условия) ->
    обязательная консультация или обследование. Значения утверждает клинический эксперт."""

    class Requirement(models.TextChoices):
        CONSULTATION = "consultation", "Консультация специалиста"
        DIAGNOSTICS = "diagnostics", "Обследование"

    class Severity(models.TextChoices):
        CRITICAL = "critical", "Критично (немедленно)"
        MAJOR = "major", "Существенно — на обсуждение координатору"
        MINOR = "minor", "Замечание"

    code = models.SlugField(max_length=64, unique=True)
    version = models.PositiveIntegerField(default=1)
    title = models.CharField(max_length=255)
    finding_code = models.CharField(max_length=64, db_index=True,
                                    help_text="Код словаря находок, шкалы (birads_category) или признака (sign:thrombus)")
    conditions = models.JSONField(default=list, blank=True, help_text='[{"attr": "birads", "op": "gte", "value": 4}]')
    requirement = models.CharField(max_length=16, choices=Requirement.choices)
    specialty_code = models.SlugField(max_length=64, blank=True)
    accepted_specialties = models.JSONField(default=list, blank=True,
                                            help_text="Другие специальности, которые тоже закрывают показание")
    service_pattern = models.CharField(max_length=255, blank=True, help_text="Регулярное выражение для обследования")
    service_title = models.CharField(max_length=255, blank=True)
    max_days = models.PositiveIntegerField(null=True, blank=True, help_text="Не позже, чем через N дней")
    severity = models.CharField(max_length=16, choices=Severity.choices, default=Severity.MAJOR)
    rationale = models.TextField(blank=True, help_text="Основание: клинические рекомендации, матрица маршрутизации")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["finding_code", "code"]

    def __str__(self) -> str:
        return f"{self.code} v{self.version}"


class RecommendationAudit(BaseModel):
    """Результат одной проверки: протокол (рекомендации диагноста) или приём (назначения врача)."""

    class Source(models.TextChoices):
        PROTOCOL = "protocol", "Рекомендации в протоколе исследования"
        VISIT = "visit", "Назначения врача на приёме"

    class Verdict(models.TextChoices):
        SUFFICIENT = "sufficient", "Рекомендации достаточны"
        NEEDS_REVIEW = "needs_review", "Есть замечания"
        INSUFFICIENT = "insufficient", "Недостаточно — на обсуждение координатору"

    class Status(models.TextChoices):
        OPEN = "open", "Ожидает решения координатора"
        RESOLVED = "resolved", "Решение принято"
        NOT_REQUIRED = "not_required", "Решение не требуется"
        CANCELLED = "cancelled", "Протокол аннулирован/исправлен"

    patient_id = models.UUIDField(db_index=True)
    document_id = models.UUIDField(db_index=True)
    route_id = models.UUIDField(null=True, blank=True)
    source = models.CharField(max_length=16, choices=Source.choices)
    source_ref = models.CharField(max_length=64, blank=True, help_text="ID приёма для проверки назначений")
    verdict = models.CharField(max_length=16, choices=Verdict.choices, db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.NOT_REQUIRED, db_index=True)
    summary = models.TextField(blank=True)
    items = models.JSONField(default=list, help_text="Что рекомендовал врач (нормализовано)")
    indications = models.JSONField(default=list, help_text="Показания по матрице с цитатами-основаниями")
    rules_version = models.CharField(max_length=16, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["document_id", "source", "source_ref"], name="uniq_audit_per_source")]


class AuditIssue(BaseModel):
    """Отдельное замечание с основанием и готовым предложением правки маршрута."""

    class IssueType(models.TextChoices):
        MISSING_CONSULTATION = "missing_consultation", "Не указана консультация специалиста"
        MISSING_DIAGNOSTICS = "missing_diagnostics", "Не указано обследование"
        LATE_TIMING = "late_timing", "Срок позже допустимого"
        NO_RECOMMENDATIONS = "no_recommendations", "Нет рекомендаций при значимых находках"
        NOT_IN_CONCLUSION = "not_in_conclusion", "Изменение не вынесено в заключение"
        UNSUPPORTED = "unsupported", "Рекомендация без основания в протоколе"

    class Decision(models.TextChoices):
        PENDING = "pending", "Ожидает решения"
        ACCEPTED = "accepted", "Принято — маршрут скорректирован"
        REJECTED = "rejected", "Отклонено — рекомендации врача достаточны"
        NOT_REQUIRED = "not_required", "Решение не требуется"

    audit = models.ForeignKey(RecommendationAudit, on_delete=models.CASCADE, related_name="issues")
    issue_type = models.CharField(max_length=24, choices=IssueType.choices)
    severity = models.CharField(max_length=16)
    message = models.TextField()
    finding_code = models.CharField(max_length=64, blank=True)
    evidence_quote = models.TextField(blank=True)
    specialty_code = models.SlugField(max_length=64, blank=True)
    rule_code = models.CharField(max_length=64, blank=True)
    proposed_operation = models.JSONField(null=True, blank=True, help_text="Операция для RoutePlanner.apply_correction")
    decision = models.CharField(max_length=16, choices=Decision.choices, default=Decision.PENDING)
    decision_comment = models.TextField(blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["audit", "created_at"]
