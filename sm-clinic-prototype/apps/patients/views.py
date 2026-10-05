"""
Личный кабинет пациента (прототип; в пилоте встраивается в ЛК и мобильное приложение СМ-Клиники).

Принципы: один главный шаг на первом экране («что мне сделать сейчас»), медицинские термины —
утверждёнными текстами для пациента с исходной формулировкой по запросу, уведомления — в колокольчике
(открываются по клику, прочитанными становятся только когда пациент их открыл), каналы связи выбирает пациент.
"""
from datetime import date

from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.doctors.facade import DoctorsFacade
from apps.processing.facade import ProcessingFacade
from apps.routing.facade import RoutingFacade
from common import clock

from .models import Notification, NotificationPreference, Patient
from .services.auth import PatientAuthService, PatientLoginError, patient_required
from .services.booking import PatientBookingError, PatientBookingService
from .services.push import push_preview
from .services.preferences import CHANNEL_TITLES, EXTERNAL_CHANNELS, PreferenceService

TABS = [("appointments", "Предстоящие записи"), ("results", "Результаты исследований"), ("routes", "История маршрутов"),
        ("notifications", "Уведомления")]
NOTICE_ICONS = {"result_ready": "flask", "booking_confirmed": "calendar", "postop_booked": "calendar", "no_show": "calendar",
                "rebooking": "calendar", "reminder_24h": "clock", "reminder_72h": "clock", "final_soft": "clock",
                "timer_due": "clock", "next_step": "route", "postop_choose_time": "route"}
# Шаги маршрута, понятные пациенту: что уже сделано и что впереди.
STEP_DONE = {"done", "skipped"}
STEP_NOW = {"awaiting_booking", "booked"}


def _patient(pk) -> Patient:
    return get_object_or_404(Patient, pk=pk, is_anonymous=False)


BOOK_ACTIONS = {"book", "book_online"}
VISIT_ACTIONS = {"confirm", "reschedule"}


def _notifications(patient: Patient, limit: int = 30) -> list[Notification]:
    """Уведомления кабинета. Кнопки показываются только пока действие возможно и только у последнего
    сообщения по маршруту: «Записаться» — пока этап ждёт записи, «Подтвердить» — пока визит впереди."""
    items = list(patient.notifications.filter(channel="lk").order_by("-created_at")[:limit])
    steps = {r["id"]: (r.get("active_step") or {}).get("status")
             for r in RoutingFacade.list_patient_routes(patient.id, open_only=True)}
    seen_routes = set()
    for n in items:
        n.icon = NOTICE_ICONS.get(n.template_code, "bell")
        n.actions = []
        route = str(n.route_id) if n.route_id else ""
        if not n.buttons or route not in steps or route in seen_routes:
            continue
        status = steps[route]
        n.actions = [b for b in n.buttons if (b["action"] in BOOK_ACTIONS and status == "awaiting_booking")
                     or (b["action"] in VISIT_ACTIONS and status == "booked")
                     or b["action"] not in BOOK_ACTIONS | VISIT_ACTIONS]
        if n.actions:
            seen_routes.add(route)
    return items


def _shell(patient: Patient) -> dict:
    """Общее для всех страниц кабинета: шапка с колокольчиком и крупный шрифт."""
    notifications = _notifications(patient, 8)
    return {"patient": patient, "bell": notifications,
            "unread": patient.notifications.filter(channel="lk", read_at__isnull=True).count(),
            "sms_hint": PreferenceService().sms_recommended(patient)}


def _hero(patient: Patient, routes: list[dict], appointments: list[dict]) -> dict:
    """Первый экран: один главный шаг. Порядок: записаться по маршруту → прийти на приём → всё сделано."""
    specialties = DoctorsFacade.specialty_titles()
    for route in routes:
        step = route.get("active_step")
        if not route["is_open"] or not step:
            continue
        # Первый пункт шкалы — само исследование: пациент видит, что путь уже начат.
        stepper = [{"title": "Исследование", "status": "done", "skipped": False, "hint": "готово", "due": None}]
        for s in route["steps"]:
            if s["status"] == "cancelled":
                continue
            stepper.append({"title": s["title"], "status": "done" if s["status"] in STEP_DONE else
                            "now" if s["status"] in STEP_NOW else "next", "skipped": s["status"] == "skipped",
                            "hint": s["status_display"], "due": s["due_date"]})
        specialty = specialties.get(step["specialty_code"], "")
        if step["status"] == "awaiting_booking" and step["step_type"] != "hospitalization_referral":
            due = step["due_date"]
            if isinstance(due, str):
                due = date.fromisoformat(due[:10])
            return {"kind": "book", "route": route, "step": step, "stepper": stepper, "specialty": specialty,
                    "title": f"Следующий шаг: {step['title'][:1].lower()}{step['title'][1:]}",
                    "due": due, "overdue": bool(due and due < clock.now().date())}
        appointment = next((a for a in appointments if a["route_step_id"] == step["id"]), None)
        return {"kind": "visit", "route": route, "step": step, "stepper": stepper, "specialty": specialty,
                "appointment": appointment, "title": "Вы записаны. Ждём вас на приёме" if appointment else step["title"],
                "due": step["due_date"]}
    return {"kind": "calm", "title": "Сейчас от вас ничего не требуется"}


def login_view(request):
    """Вход пациента: номер карты и пароль. Вошедший пациент сразу попадает в свой кабинет."""
    target = request.POST.get("next") or request.GET.get("next") or ""
    safe = target if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}) else ""
    if current := PatientAuthService.current(request):
        return redirect(safe if safe.startswith(f"/patient/{current}/") else f"/patient/{current}/")
    error = ""
    if request.method == "POST":
        service = PatientAuthService()
        try:
            patient = service.authenticate(request.POST.get("card", ""), request.POST.get("password", ""))
        except PatientLoginError as exc:
            error = str(exc)
        else:
            service.login(request, patient)
            return redirect(safe if safe.startswith(f"/patient/{patient.id}/") else f"/patient/{patient.id}/")
    demo = None
    if settings.PATIENT_LOGIN["SHOW_DEMO_HINT"]:
        first = Patient.objects.filter(is_anonymous=False, user__isnull=False).order_by("external_mis_id").first()
        demo = {"card": first.external_mis_id, "password": settings.PATIENT_LOGIN["DEMO_PASSWORD"]} if first else None
    return render(request, "patients/login.html", {"error": error, "next": safe, "card": request.POST.get("card", ""),
                                                   "demo": demo}, status=400 if error else 200)


@require_POST
def logout_view(request):
    PatientAuthService.logout(request)
    messages.success(request, "Вы вышли из личного кабинета")
    return redirect("patients:login")


@patient_required
def cabinet(request, pk):
    patient = _patient(pk)
    tab = request.GET.get("tab", "appointments")
    specialties = DoctorsFacade.specialty_titles()
    routes = RoutingFacade.list_patient_routes(patient.id)
    for route in routes:
        for step in route["steps"]:
            step["specialty_title"] = specialties.get(step["specialty_code"], "")
    appointments = DoctorsFacade.patient_appointments(patient.id)
    for a in appointments:
        a["starts"] = timezone.localtime(timezone.datetime.fromisoformat(a["starts_at"]))
    upcoming = [a for a in appointments if a["status"] in ("scheduled", "confirmed")]
    documents = ProcessingFacade.list_patient_documents(patient.id)
    return render(request, "patients/cabinet.html", {
        **_shell(patient), "tab": tab, "tabs": TABS, "routes": routes, "hero": _hero(patient, routes, appointments),
        "upcoming": sorted(upcoming, key=lambda a: a["starts"]),
        "past": [a for a in appointments if a not in upcoming],
        "documents": documents, "notifications": _notifications(patient),
        "push": push_preview(patient) if tab == "notifications" else None,
        "outbox": patient.notifications.exclude(channel="lk").order_by("-created_at")[:12],
    })


@require_POST
@patient_required
def notifications_read(request, pk):
    """«Прочитать все» в колокольчике. Открытие кабинета уведомления прочитанными не делает."""
    patient = _patient(pk)
    patient.notifications.filter(channel="lk", read_at__isnull=True).update(read_at=clock.now())
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({"unread": 0})
    target = request.POST.get("next") or f"/patient/{pk}/"
    return redirect(target if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}) else f"/patient/{pk}/")


@patient_required
def notification_open(request, pk, notification_id):
    """Клик по уведомлению: отмечаем прочитанным и ведём туда, где нужно действие."""
    patient = _patient(pk)
    n = get_object_or_404(Notification, pk=notification_id, patient=patient)
    if not n.read_at:
        n.read_at = clock.now()
        n.save(update_fields=["read_at", "updated_at"])
    if n.route_step_id and n.template_code in ("result_ready", "next_step", "timer_due", "rebooking", "reminder_24h",
                                               "reminder_72h", "final_soft", "no_show", "postop_choose_time"):
        step = RoutingFacade.get_step(n.route_step_id)
        if step and step["status"] == "awaiting_booking":
            return redirect("patients:book", pk=pk, step_id=n.route_step_id)
    if n.template_code == "result_ready":
        docs = ProcessingFacade.list_patient_documents(patient.id)
        if docs:
            return redirect("patients:result", pk=pk, doc_id=docs[0]["id"])
    return redirect(f"/patient/{pk}/?tab=appointments" if n.template_code in ("booking_confirmed", "postop_booked")
                    else f"/patient/{pk}/?tab=notifications")


@patient_required
def settings_view(request, pk):
    """Каналы связи по типам событий. Личный кабинет включён всегда, push — по умолчанию."""
    patient = _patient(pk)
    service = PreferenceService()
    if request.method == "POST":
        op = request.POST.get("op", "save")
        if op == "sms_all":
            service.enable_sms_everywhere(patient)
            messages.success(request, "SMS включены для всех сообщений. Push остались включёнными.")
        elif op == "sms_dismiss":
            service.dismiss_sms_hint(patient)
            messages.success(request, "Хорошо, оставили как есть. Изменить можно в любой момент в настройках.")
        elif op == "large_text":
            patient.large_text = request.POST.get("value") == "1"
            patient.save(update_fields=["large_text", "updated_at"])
        else:
            values = {group: {c for c in EXTERNAL_CHANNELS if request.POST.get(f"{group}_{c}")}
                      for group, _ in NotificationPreference.EventGroup.choices}
            service.save(patient, values)
            empty = [title for group, title in NotificationPreference.EventGroup.choices if not values[group]]
            messages.success(request, "Настройки сохранены." + (
                f" Сообщения «{', '.join(empty).lower()}» будут только в личном кабинете." if empty else ""))
        target = request.POST.get("next") or f"/patient/{pk}/settings/"
        return redirect(target if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()})
                        else f"/patient/{pk}/settings/")
    return render(request, "patients/settings.html", {
        **_shell(patient), "rows": service.matrix(patient), "channels": [(c, CHANNEL_TITLES[c]) for c in EXTERNAL_CHANNELS],
        "has_phone": bool(patient.phone_masked), "push": push_preview(patient),
    })


@patient_required
def result_summary(request, pk, doc_id):
    """Результат простыми словами: что увидели, что это значит, что делать дальше. Без диагноза и прогнозов."""
    patient = _patient(pk)
    doc = ProcessingFacade.get_document(doc_id)
    if not doc or doc["patient_id"] != str(patient.id):
        return redirect("patients:cabinet", pk=pk)
    positive = [f for f in doc["findings"] if not f["negated"]]
    texts = {t["code"]: t for t in ProcessingFacade.patient_texts({f["code"] for f in positive})}
    explained, seen = [], set()
    for f in positive:
        if f["code"] in seen or f["severity"] == "emergency":
            continue
        seen.add(f["code"])
        text = texts.get(f["code"])
        explained.append({"title": text["title"] if text else f["label"],
                          "explanation": text["explanation"] if text and text["explanation"] else
                          "Врач объяснит, что это значит именно для вас, на консультации.",
                          "quote": f["evidence_quote"], "uncertain": f["uncertain"]})
    routes = [r for r in RoutingFacade.list_patient_routes(patient.id, open_only=True) if r["source_document_id"] == doc_id]
    step = next((r["active_step"] for r in routes if r.get("active_step")), None)
    return render(request, "patients/result_summary.html", {
        **_shell(patient), "doc": doc, "explained": explained, "step": step,
        "specialty": DoctorsFacade.specialty_titles().get(step["specialty_code"], "") if step else "",
        "emergency": any(f["severity"] == "emergency" for f in positive),
    })


@patient_required
def result_full(request, pk, doc_id):
    patient = _patient(pk)
    doc = ProcessingFacade.get_document(doc_id)
    if not doc or doc["patient_id"] != str(patient.id):
        return redirect("patients:cabinet", pk=pk)
    return render(request, "patients/result_full.html", {**_shell(patient), "doc": doc,
                                                         "text": ProcessingFacade.get_full_text(doc_id)})


@patient_required
def book(request, pk, step_id):
    patient = _patient(pk)
    service = PatientBookingService()
    if request.method == "POST":
        try:
            appointment = service.book(patient, step_id, request.POST["slot_id"])
        except PatientBookingError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Вы записаны: {timezone.localtime(timezone.datetime.fromisoformat(appointment['starts_at'])):%d.%m.%Y в %H:%M}, "
                                      f"{appointment['doctor_name']}, {appointment['location']}")
            return redirect("patients:cabinet", pk=pk)
    try:
        step, slots = service.available_slots(patient, step_id)
    except PatientBookingError as exc:
        messages.error(request, str(exc))
        return redirect("patients:cabinet", pk=pk)
    for s in slots:
        s["starts"] = timezone.localtime(timezone.datetime.fromisoformat(s["starts_at"])) if isinstance(s["starts_at"], str) else s["starts_at"]
    return render(request, "patients/book.html", {**_shell(patient), "step": step, "slots": slots,
                                                  "specialty": DoctorsFacade.specialty_titles().get(step["specialty_code"], "")})


@require_POST
@patient_required
def route_action(request, pk):
    patient = _patient(pk)
    action = request.POST["action"]
    if action in ("book", "book_online"):
        step = (RoutingFacade.get_route(request.POST["route_id"]) or {}).get("active_step")
        if step:
            return redirect("patients:book", pk=pk, step_id=step["id"])
        messages.error(request, "Сейчас нет этапа, ожидающего записи")
        return redirect("patients:cabinet", pk=pk)
    if action == "reschedule":
        active = (RoutingFacade.get_route(request.POST["route_id"]) or {}).get("active_step") or {}
        if active.get("appointment_id"):
            DoctorsFacade.cancel(active["appointment_id"], patient.id)
            return redirect("patients:book", pk=pk, step_id=active["id"])
    try:
        service_action = {"confirm": "confirm", "callback": "callback", "seen_elsewhere": "seen_elsewhere", "decline": "decline"}[action]
        PatientBookingService().route_action(patient, request.POST["route_id"], service_action)
        messages.success(request, {"confirm": "Запись подтверждена", "callback": "Координатор перезвонит вам в рабочее время",
                                   "seen_elsewhere": "Спасибо, отметили, что вы уже обратились к врачу",
                                   "decline": "Отметили ваш выбор. Рекомендация остаётся в личном кабинете"}[action])
    except (KeyError, PatientBookingError) as exc:
        messages.error(request, f"Не удалось выполнить действие: {exc}")
    return redirect("patients:cabinet", pk=pk)
