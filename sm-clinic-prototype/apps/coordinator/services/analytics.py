"""
Аналитика: воронка хирургической конверсии диагностического потока (дашборд руководителя),
закономерности по триггерам и клиникам, качество ИИ (согласие врачей) и экономический эффект.
Источник — проекции RouteFact и ProtocolCase (собираются из событий).

Медиана не используется нигде: скорость записи показывается долями «записались за 24 часа» и
«за 72 часа». Доля прямо отвечает на вопрос «успеваем ли в срок» и одинаково читается для любой клиники.
"""
from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Count, F, Q

from ..models import ProtocolCase, RouteFact

BOOKED_24H = Q(booked_at__isnull=False, booked_at__lte=F("created_at") + timedelta(hours=24))
BOOKED_72H = Q(booked_at__isnull=False, booked_at__lte=F("created_at") + timedelta(hours=72))


def _rate(part: int, total: int) -> float | None:
    return round(100 * part / total, 1) if total else None

FUNNEL = [
    # (код, заголовок, фильтр)
    ("triggered", "Исследования с хирургическими триггерами", Q()),
    ("notified", "Получили уведомление", Q(notified_at__isnull=False)),
    ("booked", "Записались к специалисту", Q(booked_at__isnull=False)),
    ("visited", "Приём состоялся", Q(visit_at__isnull=False)),
    ("surgery_recommended", "Операция рекомендована", Q(surgery_recommended_at__isnull=False)),
    ("referral", "Создано направление", Q(surgery_recommended_at__isnull=False, tactic="surgery_indicated")),
    ("hosp_scheduled", "Назначена госпитализация", Q(hosp_scheduled_at__isnull=False)),
    ("hospitalized", "Госпитализированы", Q(hospitalized_at__isnull=False)),
    ("operated", "Оперированы", Q(surgery_at__isnull=False)),
]


class AnalyticsService:
    def __init__(self, base_qs=None, *, location: str = "") -> None:
        self.qs = base_qs if base_qs is not None else RouteFact.objects.filter(kind="trigger")
        self.location = location
        self.cases = ProtocolCase.objects.exclude(category=ProtocolCase.Category.CLOSED, status__in=["annulled", "superseded"])
        if location:
            self.qs = self.qs.filter(location=location)
            self.cases = self.cases.filter(location=location)

    def funnel(self) -> list[dict]:
        rows, prev = [], None
        for code, title, flt in FUNNEL:
            value = self.qs.filter(flt).count()
            rows.append({"code": code, "title": title, "value": value,
                         "conversion": round(100 * value / prev, 1) if prev else None})
            prev = value or prev
        operated_ids = list(self.qs.filter(surgery_at__isnull=False).values_list("route_id", flat=True))
        control = RouteFact.objects.filter(parent_route_id__in=operated_ids, control_visit_at__isnull=False).count()
        rows.append({"code": "control_visit", "title": "Контрольный визит", "value": control,
                     "conversion": round(100 * control / len(operated_ids), 1) if operated_ids else None})
        return rows

    def stage_patients(self, code: str) -> list[dict]:
        """Переход от показателя воронки к списку пациентов."""
        flt = dict((c, f) for c, _, f in FUNNEL).get(code, Q())
        return list(self.qs.filter(flt).values("route_id", "patient_id", "trigger_code", "status", "created_at")[:200])

    def by_trigger(self) -> list[dict]:
        rows = []
        for item in self.qs.values("trigger_code").annotate(
            total=Count("route_id"), booked=Count("route_id", filter=Q(booked_at__isnull=False)),
            booked_72h=Count("route_id", filter=BOOKED_72H),
            visited=Count("route_id", filter=Q(visit_at__isnull=False)),
            not_engaged=Count("route_id", filter=Q(close_status="not_engaged")),
            disagreed=Count("route_id", filter=Q(doctor_agreed=False)),
        ).order_by("-total"):
            item["booking_rate"] = _rate(item["booked"], item["total"]) or 0
            item["booked_72h_rate"] = _rate(item["booked_72h"], item["total"])
            item["ai_disagreement_rate"] = _rate(item["disagreed"], item["visited"])
            rows.append(item)
        return rows

    def by_location(self) -> list[dict]:
        return list(self.qs.values("location").annotate(
            total=Count("route_id"), booked=Count("route_id", filter=Q(booked_at__isnull=False)),
            no_show=Count("route_id", filter=Q(no_show_count__gt=0)),
        ).order_by("-total"))

    def by_clinic(self) -> list[dict]:
        """Сравнение клиник: поток протоколов, находки, маршруты, запись в срок, неявки, разбор."""
        routes = {r["location"]: r for r in RouteFact.objects.filter(kind="trigger").values("location").annotate(
            routes=Count("route_id"), notified=Count("route_id", filter=Q(notified_at__isnull=False)),
            booked=Count("route_id", filter=Q(booked_at__isnull=False)), booked_72h=Count("route_id", filter=BOOKED_72H),
            visited=Count("route_id", filter=Q(visit_at__isnull=False)),
            no_show=Count("route_id", filter=Q(no_show_count__gt=0)),
            not_engaged=Count("route_id", filter=Q(close_status="not_engaged")))}
        cases = {c["location"]: c for c in ProtocolCase.objects.exclude(status__in=["annulled", "superseded"]).values(
            "location").annotate(
            protocols=Count("document_id"),
            with_findings=Count("document_id", filter=~Q(findings=[])),
            needs_review=Count("document_id", filter=Q(category=ProtocolCase.Category.NEEDS_REVIEW)),
            unmatched=Count("document_id", filter=Q(category=ProtocolCase.Category.UNMATCHED)),
            failed=Count("document_id", filter=Q(category=ProtocolCase.Category.FAILED)),
            emergency=Count("document_id", filter=Q(category=ProtocolCase.Category.EMERGENCY)))}
        rows = []
        for location in sorted(set(routes) | set(cases), key=lambda x: (x == "", x)):
            r, c = routes.get(location, {}), cases.get(location, {})
            row = {"location": location, "protocols": c.get("protocols", 0), "with_findings": c.get("with_findings", 0),
                   "needs_review": c.get("needs_review", 0), "unmatched": c.get("unmatched", 0),
                   "failed": c.get("failed", 0), "emergency": c.get("emergency", 0),
                   "routes": r.get("routes", 0), "notified": r.get("notified", 0), "booked": r.get("booked", 0),
                   "booked_72h": r.get("booked_72h", 0), "visited": r.get("visited", 0),
                   "no_show": r.get("no_show", 0), "not_engaged": r.get("not_engaged", 0)}
            row["findings_rate"] = _rate(row["with_findings"], row["protocols"])
            row["notified_rate"] = _rate(row["notified"], row["routes"])
            row["booking_rate"] = _rate(row["booked"], row["routes"])
            row["booked_72h_rate"] = _rate(row["booked_72h"], row["routes"])
            row["no_show_rate"] = _rate(row["no_show"], row["booked"])
            rows.append(row)
        return rows

    def categories(self) -> list[dict]:
        counts = dict(self.cases.values_list("category").annotate(n=Count("document_id")))
        return [{"code": code, "title": title, "count": counts.get(code, 0)}
                for code, title in ProtocolCase.Category.choices if code != ProtocolCase.Category.CLOSED]

    def kpis(self) -> dict:
        total = self.qs.count()
        protocols = self.cases.exclude(category=ProtocolCase.Category.PROCESSING).count()
        return {
            "protocols": protocols,
            "findings_rate": _rate(self.cases.exclude(findings=[]).count(), protocols),
            "routes_total": total,
            "open_routes": self.qs.filter(closed_at__isnull=True).count(),
            "notified_rate": _rate(self.qs.filter(notified_at__isnull=False).count(), total),
            "booking_rate": _rate(self.qs.filter(booked_at__isnull=False).count(), total),
            "booked_24h_rate": _rate(self.qs.filter(BOOKED_24H).count(), total),
            "booked_72h_rate": _rate(self.qs.filter(BOOKED_72H).count(), total),
            "no_show_rate": _rate(self.qs.filter(no_show_count__gt=0).count(), total) or 0,
            "not_engaged_rate": _rate(self.qs.filter(close_status="not_engaged").count(), total) or 0,
            "escalated_share": _rate(self.qs.filter(escalations__gt=0).count(), total) or 0,
        }


@dataclass
class EconomicAssumptions:
    """Допущения расчёта на 1000 исследований. Клинические доли (доля операций среди
    консультированных) одинаковы в обоих вариантах — считаем только прирост за счёт удержания."""

    studies: int = 1000
    trigger_share: float = 0.15
    baseline_booking_rate: float = 0.45       # без системы
    system_booking_rate: float = 0.66         # с системой (из воронки прототипа или пилота)
    surgery_share_after_visit: float = 0.37   # одинаковая в обоих вариантах
    consultation_revenue: float = 3500
    surgery_margin: float = 60000


def economic_effect(a: EconomicAssumptions) -> dict:
    triggered = a.studies * a.trigger_share
    extra_visits = triggered * (a.system_booking_rate - a.baseline_booking_rate)
    extra_surgeries = extra_visits * a.surgery_share_after_visit
    return {
        "triggered": round(triggered), "extra_visits": round(extra_visits, 1),
        "extra_surgeries": round(extra_surgeries, 1),
        "extra_revenue": round(extra_visits * a.consultation_revenue + extra_surgeries * a.surgery_margin),
        "assumptions": a.__dict__,
    }
