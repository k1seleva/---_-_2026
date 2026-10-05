"""
EscalationEngine — «пульс» маршрутов (запускается Celery beat каждые 5 минут).

1. Активирует этапы с таймером, срок которых подошёл (УЗИ через 6 мес).
2. Для этапов, ожидающих записи/даты, проходит лестницу эскалаций из EscalationPolicy:
   24 ч — напоминание, 72 ч — SMS/push, 5-7 дней — звонок координатора,
   14 дней — последнее мягкое уведомление с кнопками, 30 дней — «пациент не вовлечён».
Все сроки — настройка (БД), время — модельное (common.clock).
"""
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction

from common import clock
from common.events import contracts
from common.events.bus import publish

from ..models import PatientRoute, RouteStep
from .journal import log_event
from .planner import TIMER_LEAD_DAYS, RoutePlanner


@dataclass
class TickReport:
    activated: int = 0
    escalations: int = 0
    closed: int = 0


class EscalationEngine:
    def __init__(self, planner: RoutePlanner | None = None) -> None:
        self.planner = planner or RoutePlanner()

    def tick(self) -> TickReport:
        report = TickReport()
        now = clock.now()
        self._activate_timers(now, report)
        self._run_ladders(now, report)
        return report

    def _activate_timers(self, now, report: TickReport) -> None:
        horizon = (now + timedelta(days=TIMER_LEAD_DAYS)).date()
        open_statuses = [s for s in PatientRoute.Status.values if s not in PatientRoute.CLOSED_STATUSES]
        steps = RouteStep.objects.select_related("route").filter(
            status=RouteStep.Status.PLANNED, earliest_date__lte=horizon, route__status__in=open_statuses,
        )
        for step in steps:
            route = step.route
            first_planned = route.steps.filter(status=RouteStep.Status.PLANNED).order_by("order").first()
            if first_planned == step and not route.steps.filter(status__in=RouteStep.ACTIVE_STATUSES).exists():
                with transaction.atomic():
                    self.planner._activate(step)
                report.activated += 1

    def _run_ladders(self, now, report: TickReport) -> None:
        steps = RouteStep.objects.select_related("route", "escalation_policy").filter(
            status=RouteStep.Status.AWAITING_BOOKING, escalation_policy__isnull=False, activated_at__isnull=False,
        )
        for step in steps:
            if not step.route.is_open:
                continue
            elapsed_h = (now - step.activated_at).total_seconds() / 3600
            ladder = step.escalation_policy.ladder
            for level in range(step.escalation_level, len(ladder)):
                rung = ladder[level]
                if rung["after_hours"] > elapsed_h:
                    break
                with transaction.atomic():
                    self._execute(step, level, rung)
                    step.escalation_level = level + 1
                    step.save(update_fields=["escalation_level", "updated_at"])
                report.escalations += 1
                if rung["action"] == "close_not_engaged":
                    report.closed += 1
                    break

    def _execute(self, step: RouteStep, level: int, rung: dict) -> None:
        route = step.route
        action = rung["action"]
        log_event(route, "escalation", step=step, basis=f"{step.escalation_policy.code}: уровень {level + 1} ({action})",
                  payload=rung)
        if action == "close_not_engaged":
            self.planner.close(route, PatientRoute.Status.NOT_ENGAGED,
                               basis="Нет записи 30 дней: маршрут не реализован / пациент не вовлечён")
            return
        publish(contracts.ROUTE_STEP_ESCALATED, {
            **RoutePlanner.step_payload(step), "level": level + 1, "action": action,
            "template": rung.get("template", ""), "channels": rung.get("channels", []),
            "buttons": rung.get("buttons", []), "task_type": rung.get("task_type", ""),
        }, event_id=f"escalation:{step.id}:{step.attempt}:{level}")
