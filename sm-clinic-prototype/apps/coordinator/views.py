"""
Единое рабочее место координатора и врача (серверный рендеринг, общий каркас workspace/base.html).

Два рабочих раздела в одном меню:
  а) Маршруты и протоколы — главная, входящие с категориями и метками, карта протокола с объяснением
     «почему», обезличенные пациенты, маршруты, задачи, обсуждения рекомендаций;
  б) Аналитика — дашборд (без медианы), сравнение клиник, признаки и проверки.
Клиника выбирается один раз в верхней панели и действует на все разделы (хранится в сессии).
"""
import json

from django.contrib import messages
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from apps.audit.facade import AuditFacade
from apps.doctors.facade import DoctorsFacade
from apps.patients.facade import PatientsFacade
from apps.processing.facade import ProcessingFacade
from apps.routing.facade import RoutingFacade
from common import clock
from common.identity import actor, focus_owner

from .models import AdviceReview, CoordinatorTask, ProtocolCase, RouteFact, RouteReview, Tag
from .services.advice import AdviceReviewError, AdviceReviewService, ReviewCommand
from .services.analytics import FUNNEL, AnalyticsService
from .services.audit_reviews import AuditReviewService
from .services.cases import REASON_BY_CODE, REVIEW_REASONS, CaseService, IdentityDecisionService
from .services.disputes import DisputeService

Category = ProtocolCase.Category
CATEGORY_STYLE = {
    # код: (цвет, иконка, что значит — подсказка под цифрой)
    "emergency": ("red", "alert", "Связаться с пациентом сразу"),
    "failed": ("amber", "file", "Файл не прочитан"),
    "unmatched": ("violet", "user-question", "Привязать к пациенту"),
    "needs_review": ("amber", "eye", "Чего-то не хватает"),
    "routed": ("green", "route", "Всё на месте, пациент ведётся"),
    "no_findings": ("gray", "check", "Маршрут не нужен"),
    "processing": ("blue", "clock", "Читается сейчас"),
}
ATTENTION = ("emergency", "failed", "unmatched", "needs_review")


def _clinic(request) -> str:
    return request.session.get("clinic", "")


_user = actor
_owner = focus_owner


def _back(request, default: str):
    target = request.POST.get("next") or request.GET.get("next") or default
    return redirect(target if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}) else default)


# Причина, которая просто повторяет категорию, рядом с категорией не показывается.
SAME_AS_CATEGORY = {"routed": "route_started", "unmatched": "unmatched", "emergency": "emergency", "failed": "read_error"}


def _reason_tags(codes, category: str = "") -> list[dict]:
    skip = SAME_AS_CATEGORY.get(category)
    return [{"code": c, "title": REASON_BY_CODE[c].title, "color": REASON_BY_CODE[c].color,
             "description": REASON_BY_CODE[c].description} for c in codes if c in REASON_BY_CODE and c != skip]


def _case_rows(cases) -> list[dict]:
    """Строки списка: пациент, клиника, категория, причины-метки и короткое «почему» из протокола."""
    cases = list(cases)
    names = PatientsFacade.names({c.patient_id for c in cases})
    clinics = DoctorsFacade.location_titles()
    codes = {code for c in cases for code in c.findings}
    titles = ProcessingFacade.finding_titles(codes)
    rows = []
    for c in cases:
        color, icon, _ = CATEGORY_STYLE.get(c.category, ("gray", "file", ""))
        why = next(iter(c.explanation), None)
        rows.append({
            "case": c, "patient": names.get(str(c.patient_id), "—") if c.patient_id else "—",
            "clinic": clinics.get(c.location, c.location or "—"), "color": color, "icon": icon,
            "reasons": _reason_tags(c.reasons, c.category),
            "tags": list(c.tags.all()),
            "findings": [titles.get(code, code) for code in c.findings],
            "why": {"finding": titles.get(why["finding"], why["finding"]), "quote": why.get("quote", ""),
                    "rule": why.get("rule", "")} if why else None,
        })
    return rows


# ====================================================================== главная
def home(request):
    location = _clinic(request)
    service = AnalyticsService(location=location)
    counts = {c["code"]: c["count"] for c in service.categories()}
    attention = sum(counts.get(c, 0) for c in ATTENTION)
    now = clock.now()
    hour = timezone.localtime(now).hour
    greeting = "Доброе утро" if 5 <= hour < 12 else "Добрый день" if hour < 18 else "Добрый вечер"
    tasks = list(CoordinatorTask.objects.filter(status__in=["open", "in_progress"]).order_by("priority", "due_at")[:5])
    names = PatientsFacade.names({t.patient_id for t in tasks})
    tiles = [{"code": code, "title": Category(code).label, "count": counts.get(code, 0), "color": CATEGORY_STYLE[code][0],
              "icon": CATEGORY_STYLE[code][1], "hint": CATEGORY_STYLE[code][2]}
             for code in ("emergency", "unmatched", "needs_review", "failed", "routed", "no_findings")]
    recent = ProtocolCase.objects.filter(category__in=ATTENTION)
    if location:
        recent = recent.filter(location=location)
    return render(request, "coordinator/home.html", {
        "greeting": greeting, "now": now, "attention": attention, "counts": counts, "tiles": tiles, "kpis": service.kpis(),
        "tasks": [(t, names.get(str(t.patient_id), "—")) for t in tasks],
        "batches": ProcessingFacade.list_batches(4, location), "recent": _case_rows(recent.order_by("-uploaded_at")[:5]),
        "clinic_title": DoctorsFacade.location_titles().get(location, ""),
    })


@require_POST
def switch_clinic(request):
    request.session["clinic"] = request.POST.get("clinic", "")
    return _back(request, "/coordinator/")


def search(request):
    q = (request.GET.get("q") or "").strip()
    patients, cases, placeholders = [], [], []
    if q:
        patients = PatientsFacade.search(q)
        cases = _case_rows(ProtocolCase.objects.filter(
            Q(filename__icontains=q) | Q(card_number__icontains=q) | Q(study_type__icontains=q))[:30])
        placeholders = [p for p in PatientsFacade.list_placeholders() if q.lower() in
                        f"{p['placeholder_code']} {p['card_hint']}".lower()]
    return render(request, "coordinator/search.html", {"q": q, "patients": patients, "cases": cases,
                                                       "placeholders": placeholders})


# ====================================================================== входящие
def inbox(request):
    location = _clinic(request)
    category = request.GET.get("category", "attention")
    qs = ProtocolCase.objects.exclude(category=Category.CLOSED).prefetch_related("tags")
    if location:
        qs = qs.filter(location=location)
    batch = None
    if batch_id := request.GET.get("batch"):
        qs = qs.filter(batch_id=batch_id)
        batch = ProcessingFacade.get_batch(batch_id)
    if tag := request.GET.get("tag"):
        qs = qs.filter(Q(reasons__icontains=f'"{tag}"') | Q(tags__code=tag)).distinct()
    if q := (request.GET.get("q") or "").strip():
        qs = qs.filter(Q(filename__icontains=q) | Q(card_number__icontains=q) | Q(study_type__icontains=q))
    counts = {code: qs.filter(category=code).count() for code, _ in Category.choices if code != Category.CLOSED}
    tabs = [{"code": "attention", "title": "Требуют внимания", "count": sum(counts.get(c, 0) for c in ATTENTION)}]
    tabs += [{"code": code, "title": Category(code).label, "count": counts.get(code, 0)}
             for code in ("emergency", "unmatched", "needs_review", "failed", "routed", "no_findings", "processing")]
    tabs.append({"code": "all", "title": "Все", "count": sum(counts.values())})
    if category == "attention":
        shown = qs.filter(category__in=ATTENTION)
    elif category == "all":
        shown = qs
    else:
        shown = qs.filter(category=category)
    order = {"emergency": 0, "failed": 1, "unmatched": 2, "needs_review": 3}
    rows = sorted(_case_rows(shown[:300]), key=lambda r: (order.get(r["case"].category, 9), -(r["case"].uploaded_at or clock.now()).timestamp()))
    return render(request, "coordinator/inbox.html", {
        "rows": rows, "tabs": tabs, "category": category, "batch": batch,
        "tag_options": [{"code": r.code, "title": r.title} for r in REASON_BY_CODE.values()] +
                       [{"code": t.code, "title": t.title} for t in Tag.objects.filter(is_system=False)],
        "custom_tags": Tag.objects.filter(is_system=False), "styles": CATEGORY_STYLE,
    })


@require_POST
def inbox_bulk(request):
    ids = request.POST.getlist("case")
    cases = ProtocolCase.objects.filter(pk__in=ids)
    op = request.POST.get("op")
    done = 0
    if op == "review":
        for case in cases.filter(category=Category.NEEDS_REVIEW):
            CaseService.mark_reviewed(case, _user(request))
            done += 1
        messages.success(request, f"Снято с проверки: {done}")
    elif op == "tag" and (tag := Tag.objects.filter(code=request.POST.get("tag"), is_system=False).first()):
        for case in cases:
            case.tags.add(tag)
            done += 1
        messages.success(request, f"Метка «{tag.title}» добавлена: {done}")
    else:
        messages.error(request, "Выберите протоколы и действие")
    return _back(request, "/coordinator/inbox/")


# ====================================================================== карта протокола
def case_detail(request, document_id):
    case = ProtocolCase.objects.filter(pk=document_id).prefetch_related("tags").first()
    document = ProcessingFacade.get_document(document_id)
    if not case or not document:
        raise Http404("Протокол не найден")
    focus = [k for k in request.GET.get("focus", "").split(",") if k] if "focus" in request.GET else None
    annotation = ProcessingFacade.get_annotation(document_id, owner=_owner(request), focus=focus)
    routes = RoutingFacade.routes_for_document(document_id, open_only=False)
    titles = ProcessingFacade.finding_titles(case.findings)
    specialties = DoctorsFacade.specialty_titles()
    # Объяснимость: фраза протокола → правило матрицы (с версией) → маршрут и его этапы.
    why = []
    for e in case.explanation:
        route = next((r for r in routes if r["trigger_code"] == e["finding"]), None)
        why.append({"finding": titles.get(e["finding"], e["finding"]), "code": e["finding"], "quote": e.get("quote", ""),
                    "rule": e.get("rule") or (route["evidence"].get("rule") if route else ""),
                    "version": e.get("version") or (route["rule_version"] if route else ""), "route": route})
    for r in routes:
        for s in r["steps"]:
            s["specialty_title"] = specialties.get(s["specialty_code"], "")
    patient = PatientsFacade.get_display(case.patient_id) if case.patient_id else None
    evidence = ProcessingFacade.evidence_view(document_id)
    evidence["html"], evidence["css"] = mark_safe(evidence["html"]), mark_safe(evidence["css"])
    return render(request, "coordinator/case.html", {
        "case": case, "doc": document, "annotation": annotation, "routes": routes, "why": why, "patient": patient,
        "reasons": _reason_tags(case.reasons, case.category), "style": CATEGORY_STYLE.get(case.category, ("gray", "file", "")),
        "needs_review": bool(set(case.reasons) & REVIEW_REASONS) and not case.reviewed_at,
        "not_triggered": RoutingFacade.explain_non_triggers(document["findings"]),
        "audits": AuditFacade.for_document(document_id), "custom_tags": Tag.objects.filter(is_system=False),
        "case_tags": {t.code for t in case.tags.all()}, "evidence": evidence,
        "triggers": _triggers_block(document_id, request.GET.get("tsort", "text"), specialties),
        "advice": _advice_block(document_id, specialties),
        "patients": PatientsFacade.list_all() if case.is_placeholder else [],
        "clinic": DoctorsFacade.location_titles().get(case.location, case.location),
        "focus_query": request.GET.get("focus", ""),
    })


TRIGGER_SORTS = {"text": "по порядку в тексте", "confidence": "по уверенности", "type": "по типу"}
TRIGGER_TYPE_ORDER = {"emergency": 0, "route": 1, "review": 2}


def ai_name(model: str) -> str:
    """Как называть ИИ-агента на экране: Qwen, если подключён он, иначе «ИИ-агент»."""
    return "Qwen" if "qwen" in (model or "").lower() else "ИИ-агент"


def _triggers_block(document_id, sort: str, specialties: dict) -> dict:
    """Блок «Выявленные триггеры»: сводка по типам и таблица с сортировкой (без ранжирования важности:
    сортировка — только способ просмотра, по умолчанию порядок самого протокола)."""
    analysis = ProcessingFacade.get_analysis(document_id) or {}
    triggers = [dict(t) for t in analysis.get("triggers") or []]
    for t in triggers:
        target = t.get("target") or {}
        t["specialty_title"] = specialties.get(target.get("specialty_code", ""), "")
        t["percent"] = round(t["confidence"] * 100)
    sort = sort if sort in TRIGGER_SORTS else "text"
    if sort == "confidence":
        triggers.sort(key=lambda t: (-t["confidence"], t["start"]))
    elif sort == "type":
        triggers.sort(key=lambda t: (TRIGGER_TYPE_ORDER.get(t["type"], 9), t["start"]))
    stats = analysis.get("stats") or {}
    types = ProcessingFacade.trigger_types()
    marker_types = ProcessingFacade.marker_types()
    # Найденное только ИИ-агентом — отдельно от подтверждённого словарём: триггеры и признаки («симптомы»).
    ai_triggers = [t for t in triggers if t.get("source") == "llm"]
    in_ai_triggers = {m for t in ai_triggers for m in t.get("marker_ids") or []}
    ai_markers = [{**m, "percent": round(m["confidence"] * 100)} for m in analysis.get("markers") or []
                  if m.get("source") == "llm" and not m.get("subsumed_by") and m["id"] not in in_ai_triggers]
    sources = stats.get("markers_by_source") or {}
    engines = ProcessingFacade.engines(document_id)
    ai = ai_name(engines.get("model", ""))
    return {
        "items": [t for t in triggers if t.get("source") != "llm"], "ai_items": ai_triggers, "ai_markers": ai_markers,
        "engines": engines, "ai_name": ai,
        "sources": [{"key": k, "title": title, "markers": sources.get(k, 0),
                     "triggers": (stats.get("triggers_by_source") or {}).get(k, 0)}
                    for k, title in (("both", f"Словарь и {ai} согласны"), ("rules", "Только словарь"), ("llm", f"Только {ai}"))],
        "sort": sort, "sorts": TRIGGER_SORTS, "stats": stats,
        "by_type": [{"key": k, "title": title, "count": (stats.get("triggers_by_type") or {}).get(k, 0)} for k, title in types.items()],
        "markers_by_type": [{"key": k, "title": title, "count": n} for k, title in marker_types.items()
                            if (n := (stats.get("markers_by_type") or {}).get(k))],
    }


def _advice_block(document_id, specialties: dict) -> dict:
    """Советы ИИ-агента: отдельный блок, не смешивается с аналитикой (маркерами и триггерами)."""
    advice = ProcessingFacade.routing_advice(document_id)
    reviews = AdviceReviewService.latest_by_advice(a["id"] for a in advice["items"])
    for a in advice["items"]:
        a["specialty_title"] = specialties.get(a["target_specialty_code"], "")
        a["percent"] = round(a["confidence"] * 100)
        a["review"] = reviews.get(a["id"])
    meta = advice["meta"]
    return {**advice, "catalog": RoutingFacade.route_catalog(), "verdicts": AdviceReview.Verdict.choices,
            "ai_name": ai_name(meta.get("model") or advice["configured_model"]),
            "duration": _duration(meta.get("duration_ms")), "raw": _pretty_raw(meta.get("raw", "")),
            # Советы собраны демо-режимом (или выключены), а сейчас подключён Qwen: предлагаем спросить модель.
            "can_ask_model": advice["configured_engine"] == "qwen" and meta.get("engine") != "qwen"}


def _duration(ms) -> str:
    if not ms:
        return ""
    return f"{ms} мс" if ms < 1000 else f"{ms / 1000:.1f} с".replace(".", ",") if ms < 10000 else f"{round(ms / 1000)} с"


def _pretty_raw(raw: str) -> str:
    """Ответ модели как есть; JSON — с отступами, чтобы его можно было прочитать (хранится без изменений)."""
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    except ValueError:
        return raw


@require_POST
def advice_review(request, document_id, advice_id):
    try:
        review = AdviceReviewService().review(ReviewCommand(
            advice_id=str(advice_id), user_id=_user(request), verdict=request.POST.get("verdict", ""),
            comment=request.POST.get("comment", ""), corrected_route_code=request.POST.get("route_code", "")))
    except AdviceReviewError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Оценка сохранена: {review.get_verdict_display().lower()}. Маршрут не изменён.")
    return redirect(f"/coordinator/cases/{document_id}/#advice")


@require_POST
def advice_regenerate(request, document_id):
    count = ProcessingFacade.generate_routing_advice(document_id)
    meta = ProcessingFacade.routing_advice(document_id)["meta"]
    who = ai_name(meta.get("model", "")) if meta.get("engine") == "qwen" else "Демо-режим"
    if meta.get("status") == "error":
        messages.error(request, f"{who} не ответил: {meta.get('error', '')}")
    else:
        messages.success(request, f"{who}: советов после проверки {count}.")
    return redirect(f"/coordinator/cases/{document_id}/#advice")


@require_POST
def case_action(request, document_id):
    case = get_object_or_404(ProtocolCase, pk=document_id)
    op = request.POST.get("op")
    if op == "review":
        CaseService.mark_reviewed(case, _user(request))
        messages.success(request, "Протокол снят с проверки. Отметка видна в истории и в аналитике.")
    elif op == "tags":
        CaseService.set_tags(case, request.POST.getlist("tags"))
        messages.success(request, "Метки сохранены")
    elif op in ("assign", "create") and case.is_placeholder:
        return _assign(request, case.patient_id, back=f"/coordinator/cases/{document_id}/")
    elif op == "reject":
        IdentityDecisionService().reject(document_id, reason=request.POST.get("reason", ""), user=_user(request))
        messages.success(request, "Протокол снят с разбора: он не относится к пациентам клиники или загружен по ошибке")
    return redirect("coordinator:case", document_id=document_id)


def _assign(request, placeholder_id, *, back: str):
    """Привязать обезличенную карточку к пациенту: выбранному из списка или новому по номеру карты."""
    patient_id = request.POST.get("patient_id")
    if request.POST.get("op") == "create" or request.POST.get("new_mis_id"):
        mis_id = (request.POST.get("new_mis_id") or "").strip()
        if not mis_id:
            messages.error(request, "Укажите номер карты нового пациента")
            return redirect(back)
        patient_id = PatientsFacade.ensure_by_mis_id(mis_id, display_name=request.POST.get("new_name", "").strip())
    if not patient_id:
        messages.error(request, "Выберите пациента")
        return redirect(back)
    IdentityDecisionService().assign(placeholder_id, patient_id, user=_user(request), comment=request.POST.get("comment", ""))
    name = PatientsFacade.names([patient_id]).get(str(patient_id), "пациент")
    messages.success(request, f"Протоколы привязаны: {name}. Маршрут строится по обычным правилам, уведомления пойдут пациенту.")
    return redirect(back)


# ====================================================================== обезличенные пациенты
def unmatched(request):
    location = _clinic(request)
    cases = ProtocolCase.objects.filter(is_placeholder=True).exclude(category=Category.CLOSED).order_by("-uploaded_at")
    if location:
        cases = cases.filter(location=location)
    by_placeholder: dict[str, list] = {}
    for row in _case_rows(cases):
        by_placeholder.setdefault(str(row["case"].patient_id), []).append(row)
    cards = []
    for p in PatientsFacade.list_placeholders():
        rows = by_placeholder.get(p["id"])
        if not rows:
            continue
        cards.append({"p": p, "rows": rows, "emergency": any(r["case"].category == Category.EMERGENCY for r in rows)})
    cards.sort(key=lambda c: (not c["emergency"], -max(r["case"].uploaded_at.timestamp() for r in c["rows"])))
    return render(request, "coordinator/unmatched.html", {"cards": cards, "patients": PatientsFacade.list_all(),
                                                          "clinics": DoctorsFacade.location_titles()})


@require_POST
def unmatched_assign(request, placeholder_id):
    return _assign(request, placeholder_id, back="/coordinator/unmatched/")


# ====================================================================== маршруты и задачи
def routes(request):
    location = _clinic(request)
    status = request.GET.get("status", "open")
    qs = RouteFact.objects.filter(kind="trigger").order_by("-created_at")
    if location:
        qs = qs.filter(location=location)
    if status == "open":
        qs = qs.filter(closed_at__isnull=True)
    elif status == "closed":
        qs = qs.filter(closed_at__isnull=False)
    if trigger := request.GET.get("trigger"):
        qs = qs.filter(trigger_code=trigger)
    if patient := request.GET.get("patient"):
        qs = qs.filter(patient_id=patient)
    rows = list(qs[:200])
    names = PatientsFacade.names({r.patient_id for r in rows})
    triggers = RoutingFacade.trigger_titles()
    return render(request, "coordinator/routes.html", {
        "rows": [{"r": r, "patient": names.get(str(r.patient_id), "—"), "trigger": triggers.get(r.trigger_code, "")}
                 for r in rows],
        "status": status, "triggers": triggers, "trigger": request.GET.get("trigger", ""),
    })


def tasks(request):
    items = list(CoordinatorTask.objects.filter(status__in=["open", "in_progress"]).order_by("priority", "due_at"))
    names = PatientsFacade.names({t.patient_id for t in items})
    done = list(CoordinatorTask.objects.filter(status="done").order_by("-updated_at")[:10])
    return render(request, "coordinator/tasks.html", {
        "urgent": [(t, names.get(str(t.patient_id), "—")) for t in items if t.priority == CoordinatorTask.Priority.CRITICAL],
        "other": [(t, names.get(str(t.patient_id), "—")) for t in items if t.priority != CoordinatorTask.Priority.CRITICAL],
        "done": done, "now": clock.now(),
    })


@require_POST
def task_done(request, pk):
    task = get_object_or_404(CoordinatorTask, pk=pk)
    task.status, task.resolution = CoordinatorTask.Status.DONE, request.POST.get("resolution") or "Выполнено"
    task.save()
    messages.success(request, "Задача закрыта")
    return _back(request, "/coordinator/tasks/")


# ====================================================================== аналитика
def analytics(request):
    location = _clinic(request)
    service = AnalyticsService(location=location)
    funnel = service.funnel()
    triggers = RoutingFacade.trigger_titles()
    by_trigger = service.by_trigger()
    for row in by_trigger:
        row["title"] = triggers.get(row["trigger_code"], "")
    return render(request, "coordinator/analytics.html", {
        "funnel": funnel, "top": max((r["value"] for r in funnel), default=0) or 1, "kpis": service.kpis(),
        "by_trigger": by_trigger, "categories": [{**c, "color": CATEGORY_STYLE.get(c["code"], ("gray",))[0]}
                                                 for c in service.categories()],
        "case_run": ProcessingFacade.latest_quality_summary(),
        "clinic_title": DoctorsFacade.location_titles().get(location, ""),
    })


def clinics(request):
    rows = AnalyticsService().by_clinic()
    titles = DoctorsFacade.location_titles()
    for r in rows:
        r["title"] = titles.get(r["location"], "Клиника не указана" if not r["location"] else r["location"])
    return render(request, "coordinator/clinics.html", {"rows": rows,
                                                        "max_protocols": max((r["protocols"] for r in rows), default=0) or 1})


def features(request):
    location = _clinic(request)
    return render(request, "coordinator/features.html", {
        "features": ProcessingFacade.annotation_stats(location), "audit_stats": AuditFacade.stats(),
        "clinic_title": DoctorsFacade.location_titles().get(location, ""),
        "trigger_stats": ProcessingFacade.trigger_stats(location), "advice_stats": AdviceReviewService.stats(),
        "evidence_css": mark_safe(ProcessingFacade.evidence_css()),
    })


def stage(request, code):
    rows = AnalyticsService(location=_clinic(request)).stage_patients(code)
    names = PatientsFacade.names({r["patient_id"] for r in rows})
    triggers = RoutingFacade.trigger_titles()
    for r in rows:
        r["name"] = names.get(str(r["patient_id"]), "—")
        r["trigger"] = triggers.get(r["trigger_code"], "")
    title = dict((c, t) for c, t, _ in FUNNEL).get(code, "Контрольный визит" if code == "control_visit" else code)
    return render(request, "coordinator/stage.html", {"rows": rows, "title": title})


# ====================================================================== настройки: метки и приоритеты
def tags(request):
    if request.method == "POST":
        if request.POST.get("op") == "delete":
            Tag.objects.filter(pk=request.POST.get("id"), is_system=False).delete()
            messages.success(request, "Метка удалена")
        else:
            from django.utils.text import slugify

            title = (request.POST.get("title") or "").strip()[:64]
            if not title:
                messages.error(request, "Введите название метки")
            else:
                code = slugify(title, allow_unicode=True)[:56] or "tag"
                if Tag.objects.filter(code=code).exists():
                    code = f"{code}-{Tag.objects.count() + 1}"
                color = request.POST.get("color") if request.POST.get("color") in Tag.Color.values else "blue"
                if color == "red":
                    color = "violet"  # красный зарезервирован за экстренным, чтобы он не терял смысл
                Tag.objects.create(code=code, title=title, color=color,
                                   description=request.POST.get("description", "")[:255])
                messages.success(request, f"Метка «{title}» создана")
        return redirect("coordinator:tags")
    custom = Tag.objects.filter(is_system=False)
    return render(request, "coordinator/tags.html", {
        "system": list(REASON_BY_CODE.values()), "custom": custom,
        "colors": [(c, t) for c, t in Tag.Color.choices if c != "red"],
        "categories": [(code, Category(code).label, CATEGORY_STYLE[code]) for code in CATEGORY_STYLE],
    })


def priorities(request):
    """Мои приоритеты: врач сам решает, какие признаки показывать первыми. Система ничего не ранжирует."""
    owner = _owner(request)
    profile = ProcessingFacade.focus_profile(owner)
    if request.method == "POST":
        op, key = request.POST.get("op"), request.POST.get("key", "")
        if op == "save":
            chosen = request.POST.getlist("keys")
            profile = [k for k in profile if k in chosen] + [k for k in chosen if k not in profile]
        elif op == "up" and key in profile and profile.index(key) > 0:
            i = profile.index(key)
            profile[i - 1], profile[i] = profile[i], profile[i - 1]
        elif op == "down" and key in profile and profile.index(key) < len(profile) - 1:
            i = profile.index(key)
            profile[i + 1], profile[i] = profile[i], profile[i + 1]
        elif op == "remove":
            profile = [k for k in profile if k != key]
        elif op == "clear":
            profile = []
        ProcessingFacade.save_focus_profile(owner, profile)
        messages.success(request, "Приоритеты сохранены. Они меняют только порядок показа: ни один фрагмент не скрывается.")
        return redirect("coordinator:priorities")
    catalog = ProcessingFacade.focus_catalog()
    titles = {c["key"]: c["title"] for c in catalog}
    groups: dict[str, list] = {}
    for item in catalog:
        groups.setdefault(item["group"], []).append({**item, "active": item["key"] in profile})
    return render(request, "coordinator/priorities.html", {
        "profile": [{"key": k, "title": titles.get(k, k)} for k in profile], "groups": groups, "owner": owner,
    })


# ====================================================================== обсуждения и корректировка маршрута
def audits(request):
    """Список проверок с замечаниями: сначала ожидающие решения координатора."""
    items = AuditFacade.list_audits(open_only=request.GET.get("open") == "1", limit=100)
    names = PatientsFacade.names({a["patient_id"] for a in items})
    for a in items:
        a["patient_name"] = names.get(a["patient_id"], "—")
    items.sort(key=lambda a: (a["status"] != "open", a["verdict"] != "insufficient"))
    return render(request, "coordinator/audits.html", {"audits": items, "stats": AuditFacade.stats()})


def audit_detail(request, audit_id):
    """Обсуждение: размеченный протокол, рекомендации врача, показания с основаниями, решения по замечаниям."""
    audit = AuditFacade.get(audit_id)
    if not audit:
        return redirect("coordinator:audits")
    if request.method == "POST":
        review = RouteReview.objects.filter(audit_id=audit_id).first()
        if review is None:
            messages.error(request, "Разбор не открыт: замечания не требуют решения")
            return redirect("coordinator:audit", audit_id=audit_id)
        decisions = {}
        for issue in audit["issues"]:
            decision = request.POST.get(f"decision_{issue['id']}")
            if decision in ("accepted", "rejected"):
                decisions[issue["id"]] = {"decision": decision, "comment": request.POST.get(f"comment_{issue['id']}", "")}
        if not decisions:
            messages.error(request, "Выберите решение хотя бы по одному замечанию")
            return redirect("coordinator:audit", audit_id=audit_id)
        AuditReviewService().decide(review, decisions, comment=request.POST.get("comment") or "Решение координатора",
                                    user=request.user if request.user.is_authenticated else None)
        messages.success(request, "Решение сохранено. Принятые предложения применены к маршруту.")
        return redirect("coordinator:audit", audit_id=audit_id)
    document = ProcessingFacade.get_document(audit["document_id"]) or {}
    return render(request, "coordinator/audit.html", {
        "a": audit, "document": document,
        "annotation": ProcessingFacade.get_annotation(audit["document_id"], owner=_owner(request)),
        "patient": PatientsFacade.get_display(audit["patient_id"]),
        "routes": RoutingFacade.routes_for_document(audit["document_id"], open_only=False),
        "review": RouteReview.objects.filter(audit_id=audit_id).first(),
        "specialties": DoctorsFacade.specialty_titles(),
    })


def route_compare(request, route_id):
    data = DisputeService().compare(route_id)
    specialties = DoctorsFacade.specialty_titles()
    for s in data["route"]["steps"]:
        s["specialty_title"] = specialties.get(s["specialty_code"], "")
    return render(request, "coordinator/compare.html", {
        **data, "patient": PatientsFacade.get_display(data["route"]["patient_id"]),
        "review": RouteReview.objects.filter(route_id=route_id).order_by("-created_at").first(),
        "specialties": specialties,
    })


@require_POST
def correct_route(request, route_id):
    """Координатор вносит правку: открывает (или берёт) разбор и публикует корректировку."""
    service = DisputeService()
    review = RouteReview.objects.filter(route_id=route_id, status=RouteReview.Status.OPEN, audit_id__isnull=True).first() or \
        service.open_review(route_id, source="coordinator", reason=request.POST.get("comment", ""))
    op = request.POST.get("op")
    operations = []
    if op == "add_step":
        operations.append({"op": "add_step", "step_type": request.POST.get("step_type", "consultation"),
                           "title": request.POST.get("title") or "Консультация", "specialty_code": request.POST.get("specialty_code", ""),
                           "offset_days": int(request.POST.get("offset_days") or 0)})
    elif op in ("cancel_step", "change_specialty"):
        operations.append({"op": op, "step_id": request.POST["step_id"], "specialty_code": request.POST.get("specialty_code", "")})
    elif op == "reopen":
        operations.append({"op": "reopen"})
    status = RouteReview.Status.CONFIRMED if op == "confirm" else (
        RouteReview.Status.AI_ERROR if request.POST.get("ai_error") else RouteReview.Status.CORRECTED)
    service.resolve(review, status=status, operations=operations, comment=request.POST.get("comment") or "Корректировка координатора",
                    user=request.user if request.user.is_authenticated else None)
    messages.success(request, "Решение сохранено, маршрут обновлён")
    return redirect("coordinator:compare", route_id=route_id)
