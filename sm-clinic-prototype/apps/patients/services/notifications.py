"""
Уведомления пациента: каналы (Strategy), шаблоны (настройка), антиспам и дедупликация.

Порядок каналов — личный кабинет приоритетен (кейс, этап 3) и включён всегда. Внешние каналы
(push, SMS, звонок) выбирает пациент в настройках по типам событий (PreferenceService); лестница
эскалации задаёт только моменты отправки. SMS и звонок содержат минимум медицинской информации:
только «в личном кабинете новое сообщение».
"""
import logging
from abc import ABC, abstractmethod
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction

from common import clock
from common.events import contracts
from common.events.bus import publish

from ..models import Notification, NotificationTemplate, Patient
from .preferences import PreferenceService

logger = logging.getLogger(__name__)


class NotificationChannel(ABC):
    code: str

    @abstractmethod
    def deliver(self, notification: Notification) -> bool: ...


class LkChannel(NotificationChannel):
    """Личный кабинет: сообщение просто становится видимым в ЛК."""

    code = "lk"

    def deliver(self, notification: Notification) -> bool:
        return True


class PushChannel(NotificationChannel):
    """Push: в прототипе имитация (п. 4 кейса). В пилоте — pywebpush / FCM по PushSubscription."""

    code = "push"

    def deliver(self, notification: Notification) -> bool:
        logger.info("[PUSH имитация] %s: %s", notification.patient_id, notification.title)
        return True


class SmsChannel(NotificationChannel):
    """SMS через шлюз оператора (имитация). Текст — без диагноза и деталей."""

    code = "sms"

    def deliver(self, notification: Notification) -> bool:
        if not notification.patient.phone_masked:
            return False
        logger.info("[SMS имитация] %s: %s", notification.patient.phone_masked, notification.body)
        return True


class CallChannel(NotificationChannel):
    """Звонок голосового робота (имитация). Текст нейтральный, без диагноза; соблюдаются тихие часы.
    В пилоте — телефония клиники (SIP / облачная АТС) с переводом на оператора по кнопке «0»."""

    code = "call"

    def deliver(self, notification: Notification) -> bool:
        if not notification.patient.phone_masked:
            return False
        logger.info("[ЗВОНОК имитация] %s: %s", notification.patient.phone_masked, notification.body)
        return True


CHANNELS: dict[str, NotificationChannel] = {c.code: c for c in (LkChannel(), PushChannel(), SmsChannel(), CallChannel())}
# Транзакционные сообщения (подтверждение записи) антиспамом не ограничиваются.
TRANSACTIONAL = {"booking_confirmed", "postop_booked"}


def _clean(text: str) -> str:
    """Пустая подстановка (например, нет свободного времени у врача) не оставляет двойных пробелов."""
    return " ".join(text.split())


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


class NotificationService:
    def __init__(self, channels: dict[str, NotificationChannel] | None = None) -> None:
        self.channels = channels or CHANNELS
        self.cfg = settings.NOTIFICATIONS

    def notify(self, patient: Patient, template_code: str, *, channels: list[str] | None = None,
               context: dict | None = None, route_id=None, step_id=None, deep_link: str = "",
               dedupe_suffix: str = "") -> list[Notification]:
        if patient.is_anonymous:
            # Обезличенной карточке сообщения не отправляются: пациент ещё не подтверждён координатором.
            return []
        context = _SafeDict(name=patient.display_name, **(context or {}))
        # Внешние каналы выбирает пациент; channels=["lk"] — только личный кабинет (без внешних каналов).
        channels = ["lk"] if channels == ["lk"] else PreferenceService().channels_for(patient, template_code)
        created: list[Notification] = []
        for channel in sorted(channels, key=self.cfg["CHANNEL_PRIORITY"].index):
            template = NotificationTemplate.objects.filter(code=template_code, channel=channel).first()
            if template is None:
                continue
            dedupe_key = f"{patient.id}:{route_id}:{step_id}:{template_code}:{channel}:{dedupe_suffix}"
            try:
                with transaction.atomic():
                    notification = Notification.objects.create(
                        patient=patient, route_id=route_id, route_step_id=step_id, template_code=template_code,
                        channel=channel, title=_clean(template.title.format_map(context)),
                        body=_clean(template.body.format_map(context)),
                        buttons=template.buttons, deep_link=deep_link, dedupe_key=dedupe_key[:255],
                    )
            except IntegrityError:  # уже отправляли — повторная доставка события
                continue
            self._send(notification, transactional=template_code in TRANSACTIONAL)
            created.append(notification)
        return created

    def _send(self, notification: Notification, *, transactional: bool) -> None:
        now = clock.now()
        if notification.channel == "call" and self._quiet_hours(now):
            # Звонок ночью недопустим даже для подтверждения записи.
            return self._suppress(notification, "Тихие часы — звонок не выполняется, сообщение в личном кабинете")
        if not transactional and notification.channel != "lk":
            if self._quiet_hours(now):
                return self._suppress(notification, "Тихие часы — сообщение доступно в личном кабинете")
            sent_today = Notification.objects.filter(
                patient=notification.patient, status=Notification.Status.SENT, sent_at__gte=now - timedelta(days=1),
            ).exclude(channel="lk").count()
            if sent_today >= self.cfg["MAX_PER_DAY"]:
                return self._suppress(notification, "Антиспам: лимит сообщений за сутки")
        ok = self.channels[notification.channel].deliver(notification)
        notification.status = Notification.Status.SENT if ok else Notification.Status.FAILED
        notification.sent_at = now if ok else None
        notification.save(update_fields=["status", "sent_at", "updated_at"])
        if ok and notification.route_id:
            publish(contracts.NOTIFICATION_SENT, {
                "notification_id": str(notification.id), "patient_id": str(notification.patient_id),
                "route_id": str(notification.route_id), "channel": notification.channel,
                "template": notification.template_code,
            })

    def _quiet_hours(self, now) -> bool:
        start, end = self.cfg["QUIET_HOURS"]
        hour = now.astimezone().hour
        return hour >= start or hour < end

    @staticmethod
    def _suppress(notification: Notification, reason: str) -> None:
        notification.status, notification.status_reason = Notification.Status.SUPPRESSED, reason
        notification.save(update_fields=["status", "status_reason", "updated_at"])

    @staticmethod
    def suppress_route(route_id) -> int:
        """Маршрут закрыт/аннулирован — неотправленные сообщения больше не уходят."""
        return Notification.objects.filter(route_id=route_id, status=Notification.Status.QUEUED).update(
            status=Notification.Status.SUPPRESSED, status_reason="Маршрут закрыт")


def booking_link(patient_id, step_id) -> str:
    base = settings.NOTIFICATIONS["PUBLIC_BASE_URL"].rstrip("/")
    return f"{base}/patient/{patient_id}/book/{step_id}/"
