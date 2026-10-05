"""
Push-уведомления: как пациент видит их на телефоне и по каким правилам пишется текст.

Правила (подробный разбор — docs/PUSH_GUIDE.md):
1. На экране блокировки нет диагноза, находки и названия специальности: экран видят посторонние.
   Пишем нейтрально: «Рекомендуется консультация профильного специалиста».
2. Есть конкретный следующий шаг: врач, дата, время и клиника ближайшего свободного приёма.
   Специальность и причина видны только после входа в личный кабинет.
3. Коротко: заголовок до 40 знаков, текст до 130 (столько показывает свернутое уведомление).
4. Одна кнопка действия («Выбрать время»), ссылка ведёт на запись после входа в кабинет.
5. Не пугаем и не торопим: без «срочно» и восклицательных знаков. Экстренное — звонок персонала, не push.
6. Тихие часы и лимит в сутки соблюдаются (NotificationService), повтор — не чаще раза в сутки.
"""
from django.utils import timezone

from apps.doctors.facade import DoctorsFacade
from apps.routing.facade import RoutingFacade

from ..models import Patient

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
          "декабря")
APP_NAME = "СМ-Клиника"
TITLE_LIMIT, BODY_LIMIT = 40, 130
RECOMMENDATION = "Рекомендуется консультация профильного специалиста."
# Одна кнопка на push: для записи — выбрать время, для подтверждённой записи — подтвердить.
ACTIONS = {"booking_confirmed": "Подтвердить", "postop_booked": "Подтвердить", "final_soft": "Открыть"}
DEFAULT_ACTION = "Выбрать время"


def human_date(value) -> str:
    local = timezone.localtime(value) if timezone.is_aware(value) else value
    return f"{local.day} {MONTHS[local.month - 1]} в {local:%H:%M}"


def doctor_offer(specialty_code: str, location_code: str = "") -> dict:
    """Ближайший свободный приём по специальности этапа: сначала в клинике исследования, затем в любой."""
    if not specialty_code:
        return {}
    slots = (DoctorsFacade.find_slots(specialty_code, limit=1, location_code=location_code or None)
             if location_code else []) or DoctorsFacade.find_slots(specialty_code, limit=1)
    if not slots:
        return {}
    slot = slots[0]
    where = "онлайн" if slot["format"] == "online" else f"клиника {slot['location']}"
    when = human_date(slot["starts_at"])
    return {"doctor": slot["doctor"], "slot_date": when, "location": where, "slot_id": slot["id"],
            "doctor_offer": f"Ближайшее время: врач {slot['doctor']}, {when}, {where}."}


def push_preview(patient: Patient, limit: int = 3) -> dict:
    """Как пуши выглядят на телефоне пациента: отправленные; если их не было — задержанные (тихие часы)
    с причиной; если нет и таких — пример по текущему этапу."""
    pushes = patient.notifications.filter(channel="push").order_by("-created_at")
    sent = list(pushes.filter(status__in=("sent", "read"))[:limit])
    if sent:
        return {"source": "sent", "items": [_item(n.title, n.body, n.created_at, n.status, n.template_code) for n in sent]}
    # Не отправлены (тихие часы, лимит в сутки): показываем настоящий текст и честно говорим, что push не ушёл.
    held = list(pushes.filter(status="suppressed")[:limit])
    if held:
        return {"source": "held", "reason": held[0].status_reason,
                "items": [_item(n.title, n.body, n.created_at, n.status, n.template_code) for n in held]}
    step = next((r["active_step"] for r in RoutingFacade.list_patient_routes(patient.id, open_only=True)
                 if r.get("active_step") and r["active_step"]["status"] == "awaiting_booking"), None)
    offer = doctor_offer(step["specialty_code"]) if step else doctor_offer("therapist")
    body = f"{RECOMMENDATION} {offer.get('doctor_offer', '')}".strip()
    return {"source": "example", "items": [_item("Результат исследования готов", body, timezone.now(), "example")]}


def _item(title: str, body: str, when, status: str, template_code: str = "result_ready") -> dict:
    return {"app": APP_NAME, "title": title, "body": body, "when": when, "status": status,
            "too_long": len(title) > TITLE_LIMIT or len(body) > BODY_LIMIT,
            "action": ACTIONS.get(template_code, DEFAULT_ACTION)}
