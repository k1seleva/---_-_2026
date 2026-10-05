"""
Patient Module — профиль пациента (обезличенный в прототипе), уведомления и действия пациента.
"""
from django.conf import settings
from django.db import models

from common.models import BaseModel


class Patient(BaseModel):
    """В прототипе — только псевдоним. В пилоте ПДн остаются в МИС, здесь — ссылка external_mis_id.

    «Обезличенный пациент» (is_anonymous) — временная карточка для протоколов, по которым пациента
    определить не удалось (нет номера карты или он не найден). Ей не уходят сообщения и по ней
    не строится маршрут, пока координатор не привяжет протоколы к настоящему пациенту.
    """

    external_mis_id = models.CharField(max_length=64, unique=True, null=True, blank=True,
                                       help_text="Номер амбулаторной карты в 1С (пусто у обезличенной карточки)")
    display_name = models.CharField(max_length=255, help_text="Псевдоним / имя для обращения")
    birth_year = models.PositiveSmallIntegerField(null=True, blank=True)
    sex = models.CharField(max_length=1, choices=[("F", "Ж"), ("M", "М")], blank=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    phone_masked = models.CharField(max_length=32, blank=True, help_text="+7 *** ***-12-34 — сам номер хранит МИС")
    # Обезличенная карточка.
    is_anonymous = models.BooleanField(default=False, db_index=True)
    placeholder_code = models.CharField(max_length=16, blank=True, help_text="ОП-0001 — номер для разбора координатором")
    card_hint = models.CharField(max_length=64, blank=True, db_index=True, help_text="Номер карты из протокола, не найденный в МИС")
    location_code = models.CharField(max_length=64, blank=True)
    hints = models.JSONField(default=list, blank=True, help_text="Подсказки для разбора: файлы, типы и даты исследований")
    merged_into = models.UUIDField(null=True, blank=True, help_text="Пациент, к которому привязаны протоколы")
    merged_at = models.DateTimeField(null=True, blank=True)
    # Настройки личного кабинета.
    large_text = models.BooleanField(default=False, help_text="Крупный шрифт в личном кабинете")
    sms_hint_dismissed_at = models.DateTimeField(null=True, blank=True, help_text="Пациент скрыл совет включить SMS")
    prefs_updated_at = models.DateTimeField(null=True, blank=True, help_text="Когда пациент сам менял каналы (согласие)")

    def __str__(self) -> str:
        return self.display_name

    @property
    def age(self) -> int | None:
        from common import clock

        return clock.now().year - self.birth_year if self.birth_year else None


class PushSubscription(BaseModel):
    """Подписка Web Push / FCM / APNs устройства пациента."""

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="push_subscriptions")
    provider = models.CharField(max_length=16, default="webpush", choices=[("webpush", "Web Push"), ("fcm", "FCM"), ("apns", "APNs")])
    endpoint = models.TextField()
    keys = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)


class NotificationTemplate(models.Model):
    """Тексты сообщений — настройка (п. 11 кейса). Плейсхолдеры: {name}, {specialty}, {step}, {date}, {time}."""

    code = models.SlugField(max_length=64)
    channel = models.CharField(max_length=8, choices=[("lk", "Личный кабинет"), ("push", "Push"), ("sms", "SMS"),
                                                      ("call", "Звонок")])
    title = models.CharField(max_length=255, blank=True)
    body = models.TextField()
    buttons = models.JSONField(default=list, blank=True, help_text='[{"action": "book", "label": "Записаться"}]')

    class Meta:
        constraints = [models.UniqueConstraint(fields=["code", "channel"], name="uniq_template_channel")]

    def __str__(self) -> str:
        return f"{self.code}/{self.channel}"


class Notification(BaseModel):
    class Channel(models.TextChoices):
        LK = "lk", "Личный кабинет"
        PUSH = "push", "Push"
        SMS = "sms", "SMS"
        CALL = "call", "Звонок"

    class Status(models.TextChoices):
        QUEUED = "queued", "В очереди"
        SENT = "sent", "Отправлено"
        READ = "read", "Прочитано"
        FAILED = "failed", "Ошибка"
        SUPPRESSED = "suppressed", "Подавлено (антиспам/маршрут закрыт)"

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="notifications")
    route_id = models.UUIDField(null=True, blank=True, db_index=True)
    route_step_id = models.UUIDField(null=True, blank=True)
    template_code = models.SlugField(max_length=64)
    channel = models.CharField(max_length=8, choices=Channel.choices)
    title = models.CharField(max_length=255, blank=True)
    body = models.TextField()
    buttons = models.JSONField(default=list, blank=True)
    deep_link = models.CharField(max_length=500, blank=True, help_text="Ссылка сразу на подходящие слоты")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED, db_index=True)
    status_reason = models.CharField(max_length=255, blank=True)
    dedupe_key = models.CharField(max_length=255, unique=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class PatientAction(BaseModel):
    class Action(models.TextChoices):
        BOOK = "book", "Записался"
        SEEN_ELSEWHERE = "seen_elsewhere", "Уже обратился к врачу"
        DECLINE = "decline", "Не планирую обращаться"
        CALLBACK = "callback", "Заказал обратный звонок"
        CONFIRM = "confirm", "Подтвердил запись"

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="actions")
    route_id = models.UUIDField(null=True, blank=True)
    notification = models.ForeignKey(Notification, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=16, choices=Action.choices)
    comment = models.TextField(blank=True)


class NotificationPreference(BaseModel):
    """Канал связи, выбранный пациентом для типа событий. Личный кабинет включён всегда;
    по умолчанию — push (settings.NOTIFICATIONS["DEFAULT_CHANNELS"])."""

    class EventGroup(models.TextChoices):
        RESULTS = "results", "Результаты исследований"
        ROUTE = "route", "Следующий шаг маршрута"
        APPOINTMENTS = "appointments", "Записи к врачу"
        REMINDERS = "reminders", "Напоминания"

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="preferences")
    event_group = models.CharField(max_length=16, choices=EventGroup.choices)
    push = models.BooleanField(default=True)
    sms = models.BooleanField(default=False)
    call = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["patient", "event_group"], name="uniq_patient_event_group")]
