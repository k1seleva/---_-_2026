"""
Каналы связи, которые выбрал пациент (Push, SMS, звонок) — по типам событий.

Правила:
- личный кабинет включён всегда: это главный канал (кейс, этап 3), другие лишь приглашают в него;
- по умолчанию у всех типов событий включён push;
- для пациентов старшего возраста (settings.NOTIFICATIONS["SMS_RECOMMENDED_AGE"]) кабинет советует SMS,
  но ничего не меняет сам: канал меняет только пациент, время согласия сохраняется;
- хотя бы один внешний канал на тип события должен остаться (иначе о результате узнают только из ЛК —
  это допустимо, но интерфейс предупреждает).
"""
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction

from common import clock

from ..models import NotificationPreference, Patient

EXTERNAL_CHANNELS = ("push", "sms", "call")
CHANNEL_TITLES = {"lk": "Личный кабинет", "push": "Push", "sms": "SMS", "call": "Звонок"}
# Шаблон сообщения -> тип события в настройках пациента.
TEMPLATE_GROUP = {
    "result_ready": "results",
    "next_step": "route", "timer_due": "route", "postop_choose_time": "route",
    "booking_confirmed": "appointments", "postop_booked": "appointments", "rebooking": "appointments", "no_show": "appointments",
    "reminder_24h": "reminders", "reminder_72h": "reminders", "final_soft": "reminders",
}
GROUP_HINTS = {
    "results": "Готов результат исследования и что делать дальше",
    "route": "Пора на следующий этап: консультация или контрольное исследование",
    "appointments": "Запись, перенос, подтверждение приёма",
    "reminders": "Если вы ещё не записались по рекомендации врача",
}


@dataclass
class PreferenceRow:
    group: str
    title: str
    hint: str
    push: bool
    sms: bool
    call: bool

    @property
    def channels(self) -> list[str]:
        return [c for c in EXTERNAL_CHANNELS if getattr(self, c)]


class PreferenceService:
    def matrix(self, patient: Patient) -> list[PreferenceRow]:
        existing = {p.event_group: p for p in patient.preferences.all()}
        defaults = set(settings.NOTIFICATIONS.get("DEFAULT_CHANNELS", ("push",)))
        rows = []
        for group, title in NotificationPreference.EventGroup.choices:
            pref = existing.get(group)
            rows.append(PreferenceRow(
                group=group, title=title, hint=GROUP_HINTS[group],
                push=pref.push if pref else "push" in defaults,
                sms=pref.sms if pref else "sms" in defaults,
                call=pref.call if pref else "call" in defaults))
        return rows

    def channels_for(self, patient: Patient, template_code: str) -> list[str]:
        group = TEMPLATE_GROUP.get(template_code, "reminders")
        row = next(r for r in self.matrix(patient) if r.group == group)
        return ["lk", *row.channels]

    @transaction.atomic
    def save(self, patient: Patient, values: dict[str, set[str]]) -> None:
        """values: {group: {"push", "sms"}}. Пустой набор допустим (останется личный кабинет)."""
        for group, _title in NotificationPreference.EventGroup.choices:
            chosen = values.get(group, set())
            NotificationPreference.objects.update_or_create(patient=patient, event_group=group, defaults={
                "push": "push" in chosen, "sms": "sms" in chosen, "call": "call" in chosen})
        patient.prefs_updated_at = clock.now()
        patient.save(update_fields=["prefs_updated_at", "updated_at"])

    @transaction.atomic
    def enable_sms_everywhere(self, patient: Patient) -> None:
        """Кнопка «Включить SMS» в подсказке для старшего возраста: добавляет SMS, push не выключает."""
        current = {r.group: set(r.channels) | {"sms"} for r in self.matrix(patient)}
        self.save(patient, current)

    def sms_recommended(self, patient: Patient) -> bool:
        """Показывать ли совет включить SMS: возраст от порога, SMS ещё не везде и пациент совет не скрыл."""
        age = patient.age
        if age is None or age < settings.NOTIFICATIONS.get("SMS_RECOMMENDED_AGE", 60) or patient.sms_hint_dismissed_at:
            return False
        return not all(r.sms for r in self.matrix(patient))

    def dismiss_sms_hint(self, patient: Patient) -> None:
        patient.sms_hint_dismissed_at = clock.now()
        patient.save(update_fields=["sms_hint_dismissed_at", "updated_at"])
