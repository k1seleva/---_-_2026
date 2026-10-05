"""
Doctor Module — расписание, записи, итог приёма (обязательная тактика) и назначения.

В пилоте расписание — заглушка контракта сервиса расписания 1С (п. 4 кейса):
ScheduleSlot повторяет его форму, синхронизация — отдельным адаптером.
"""
from django.conf import settings
from django.db import models

from common.models import BaseModel


class Specialty(models.Model):
    code = models.SlugField(max_length=64, primary_key=True)
    title = models.CharField(max_length=255)
    title_genitive = models.CharField(max_length=255, blank=True, help_text="«к гинекологу», «гинеколога»")
    is_surgical = models.BooleanField(default=False)

    def __str__(self) -> str:
        return self.title


class ClinicLocation(models.Model):
    code = models.SlugField(max_length=64, primary_key=True)
    title = models.CharField(max_length=255, help_text="«Текстильщики», «Сенежская», «Онлайн»")
    address = models.CharField(max_length=255, blank=True)
    is_online = models.BooleanField(default=False)
    has_hospital = models.BooleanField(default=False)

    def __str__(self) -> str:
        return self.title


class Doctor(BaseModel):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    full_name = models.CharField(max_length=255)
    specialties = models.ManyToManyField(Specialty, related_name="doctors")
    locations = models.ManyToManyField(ClinicLocation, related_name="doctors")
    surgical_profiles = models.JSONField(default=list, blank=True,
                                         help_text="Профили, которые реально ведёт врач: hysteroscopy, cholecystectomy ...")
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return self.full_name


class ScheduleSlot(BaseModel):
    class Status(models.TextChoices):
        FREE = "free", "Свободен"
        HELD = "held", "Удержан (идёт запись)"
        BOOKED = "booked", "Занят"
        BLOCKED = "blocked", "Недоступен"

    class Format(models.TextChoices):
        OFFLINE = "offline", "Очно"
        ONLINE = "online", "Онлайн"

    doctor = models.ForeignKey(Doctor, on_delete=models.CASCADE, related_name="slots")
    specialty = models.ForeignKey(Specialty, on_delete=models.PROTECT)
    location = models.ForeignKey(ClinicLocation, on_delete=models.PROTECT)
    starts_at = models.DateTimeField(db_index=True)
    ends_at = models.DateTimeField()
    format = models.CharField(max_length=8, choices=Format.choices, default=Format.OFFLINE)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.FREE, db_index=True)
    external_id = models.CharField(max_length=128, blank=True, help_text="ID слота в сервисе расписания")

    class Meta:
        ordering = ["starts_at"]
        constraints = [models.UniqueConstraint(fields=["doctor", "starts_at"], name="uniq_doctor_slot")]


class Appointment(BaseModel):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Запланирован"
        CONFIRMED = "confirmed", "Подтверждён пациентом"
        CANCELLED = "cancelled", "Отменён"
        NO_SHOW = "no_show", "Неявка"
        COMPLETED = "completed", "Приём завершён"

    class BookedVia(models.TextChoices):
        PATIENT = "patient", "Пациент (личный кабинет)"
        COORDINATOR = "coordinator", "Координатор"
        AUTO = "auto", "Автоматически системой"
        DOCTOR = "doctor", "Врач"

    slot = models.OneToOneField(ScheduleSlot, on_delete=models.PROTECT, related_name="appointment")
    patient_id = models.UUIDField(db_index=True)
    # Ссылки на маршрут — только идентификаторы (другой модуль, будущий сервис).
    route_id = models.UUIDField(null=True, blank=True, db_index=True)
    route_step_id = models.UUIDField(null=True, blank=True)
    source_document_id = models.UUIDField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SCHEDULED, db_index=True)
    booked_via = models.CharField(max_length=12, choices=BookedVia.choices, default=BookedVia.PATIENT)

    class Meta:
        ordering = ["slot__starts_at"]


class VisitOutcome(BaseModel):
    """Итог приёма. Завершить приём без выбора тактики нельзя (этап 7 кейса)."""

    class Tactic(models.TextChoices):
        SURGERY_INDICATED = "surgery_indicated", "Оперативное лечение показано"
        EXTRA_EXAM = "extra_exam", "Требуется дополнительное обследование"
        OBSERVATION = "observation", "Динамическое наблюдение"
        SURGERY_NOT_INDICATED = "surgery_not_indicated", "Операция не показана"
        PATIENT_REFUSED = "patient_refused", "Пациент отказался"
        OTHER_PROFILE = "other_profile", "Направление в другой профиль"

    appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE, related_name="outcome")
    doctor = models.ForeignKey(Doctor, on_delete=models.PROTECT)
    tactic = models.CharField(max_length=32, choices=Tactic.choices)
    next_specialty = models.ForeignKey(Specialty, on_delete=models.PROTECT, null=True, blank=True)
    agrees_with_ai_route = models.BooleanField(default=True, help_text="Врач согласен с предложенным маршрутом")
    disagreement_reason = models.TextField(blank=True)
    comment = models.TextField(blank=True)


class Prescription(BaseModel):
    """Назначение врача — превращается в этап маршрута."""

    class Kind(models.TextChoices):
        CONSULTATION = "consultation", "Консультация"
        DIAGNOSTICS = "diagnostics", "Обследование"
        FOLLOW_UP = "follow_up", "Контрольный визит"
        SURGERY = "surgery", "Операция"

    outcome = models.ForeignKey(VisitOutcome, on_delete=models.CASCADE, related_name="prescriptions")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    title = models.CharField(max_length=255)
    specialty = models.ForeignKey(Specialty, on_delete=models.PROTECT, null=True, blank=True)
    service_code = models.CharField(max_length=64, blank=True)
    due_in_days = models.PositiveIntegerField(null=True, blank=True)
    comment = models.TextField(blank=True)
