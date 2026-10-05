"""
Построение маршрута из структурированного JSON AI-агента.

Пример (из постановки): рекомендация «консультация маммолога и УЗИ через 6 мес» ->
  Этап 1 — Консультация маммолога (целевой срок 7 дней);
  Этап 2 — УЗИ молочных желёз (таймер: 180 дней после этапа 1).
"""
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction

from common import clock
from common.events import contracts
from common.events.bus import publish

from ..models import EscalationPolicy, PatientRoute, RouteStep, RouteTemplate, StepType, TriggerRule
from .journal import log_event
from .matrix import RoutingMatrix, RuleMatch

def _genitive(code: str) -> str:
    # Справочник специальностей принадлежит модулю врачей — читаем через его фасад.
    from apps.doctors.facade import DoctorsFacade

    return DoctorsFacade.specialty_genitive(code, default=code)


def publish_route_created(route: PatientRoute) -> None:
    publish(contracts.ROUTE_CREATED, {
        "route_id": str(route.id), "patient_id": str(route.patient_id), "kind": route.kind,
        "trigger_code": route.trigger_code, "reason": route.reason, "location_code": route.location_code,
        "parent_id": str(route.parent_id) if route.parent_id else None,
        "is_surgical": bool(route.trigger_rule and route.trigger_rule.is_surgical),
        # Объяснимость для координатора: по какому протоколу, какой фразе и какому правилу запущен маршрут.
        "document_id": str(route.source_document_id or ""),
        "rule_title": route.trigger_rule.title if route.trigger_rule else "",
        "rule_version": route.rule_version,
        "evidence_quote": (route.evidence or {}).get("quote", ""),
    })


@dataclass
class RoutingInput:
    """Вход модуля маршрутизации (контракт с Processing, без импорта его кода)."""

    patient_id: str
    findings: list[dict] = field(default_factory=list)
    recommendations: list[dict] = field(default_factory=list)
    document_id: str | None = None
    external_id: str = ""
    study_type: str = ""
    study_date: str | None = None
    location_code: str = ""

    @classmethod
    def from_event(cls, payload: dict) -> "RoutingInput":
        extraction = payload.get("extraction", {})
        return cls(
            patient_id=payload["patient_id"],
            findings=extraction.get("findings", []),
            recommendations=extraction.get("recommendations", []),
            document_id=payload.get("document_id"),
            external_id=payload.get("external_id") or "",
            study_type=extraction.get("study_type", ""),
            study_date=extraction.get("study_date"),
            location_code=payload.get("location_code", ""),
        )

    @property
    def source_key(self) -> str:
        # Без внешнего ID дедуплицируем по документу.
        return self.external_id or (f"doc:{self.document_id}" if self.document_id else "")


class RouteBuilder:
    def __init__(self, matrix: RoutingMatrix | None = None) -> None:
        self.matrix = matrix or RoutingMatrix()

    # ------------------------------------------------------------------ публичный API
    @transaction.atomic
    def build(self, data: RoutingInput, *, actor_type: str = "ai") -> list[PatientRoute]:
        from .planner import RoutePlanner

        matches = self.matrix.match(data.findings)
        routes: list[PatientRoute] = []
        for idx, match in enumerate(matches):
            route, created = self._get_or_create_trigger_route(data, match, actor_type)
            if created:
                self._add_template_steps(route, match.rule.template, target_days=match.rule.target_days,
                                         first_specialty=match.rule.first_specialty_code)
                if idx == 0:
                    # К главному маршруту добавляем обследования/таймеры из рекомендаций (УЗИ через 6 мес).
                    # Консультации не дублируем: профильного специалиста уже выбрала матрица.
                    self._add_recommendation_steps(
                        route, [r for r in data.recommendations if r.get("kind") != "consultation" or r.get("interval_days")],
                        skip_specialties={match.rule.first_specialty_code})
                RoutePlanner().start(route, emergency=match.rule.is_emergency)
            routes.append(route)

        if not matches and (recs := [r for r in data.recommendations if r.get("specialty_code") or r.get("interval_days")]):
            route, created = self._get_or_create_recommendation_route(data)
            if created:
                self._add_recommendation_steps(route, recs)
                RoutePlanner().start(route)
            routes.append(route)
        return routes

    @transaction.atomic
    def build_manual(self, *, patient_id: str, document_id: str | None, operations: list[dict], reason: str,
                     actor_id: str = "") -> PatientRoute:
        """Маршрут по решению координатора: проверка рекомендаций нашла показание, а триггера матрицы нет."""
        from .planner import RoutePlanner

        now = clock.now()
        route = PatientRoute.objects.create(
            patient_id=patient_id, source_document_id=document_id or None,
            source_external_id=f"doc:{document_id}" if document_id else "", kind=PatientRoute.Kind.RECOMMENDATION,
            trigger_code="coordinator", reason=f"Решение координатора: {reason}"[:500],
            evidence={"operations": operations, "basis": "Проверка достаточности рекомендаций"},
            detected_at=now, target_date=(now + timedelta(days=14)).date(),
        )
        log_event(route, "route_created", to_status=route.status, actor_type="coordinator", actor_id=actor_id, basis=reason[:500])
        publish_route_created(route)
        policy = EscalationPolicy.objects.filter(code="booking_standard").first()
        for order, op in enumerate((o for o in operations if o.get("op") == "add_step"), start=1):
            RouteStep.objects.create(
                route=route, order=order, step_type=op.get("step_type", StepType.CONSULTATION), title=op.get("title", "")[:255],
                specialty_code=op.get("specialty_code", ""), service_code=op.get("service_code", ""),
                offset_days=op.get("offset_days", 0), window_days=op.get("window_days", 14), escalation_policy=policy,
                source=RouteStep.Source.COORDINATOR)
        RoutePlanner().start(route)
        return route

    # ------------------------------------------------------------------ внутреннее
    def _reason(self, title: str, data: RoutingInput) -> str:
        parts = [title]
        if data.study_type:
            when = ""
            if data.study_date:
                y, m, d = data.study_date[:10].split("-")
                when = f" от {d}.{m}.{y}"
            # «УЗИ органов…», «УЗДС вен…» — аббревиатуру не опускаем в нижний регистр.
            study = data.study_type
            first = study.split()[0] if study.split() else ""
            parts.append((study if first.isupper() else study[:1].lower() + study[1:]) + when)
        return " — ".join(parts)[:500]

    def _get_or_create_trigger_route(self, data: RoutingInput, match: RuleMatch, actor_type: str):
        rule = match.rule
        existing = PatientRoute.objects.filter(
            patient_id=data.patient_id, source_external_id=data.source_key, trigger_code=rule.finding_code,
        ).exclude(status=PatientRoute.Status.CANCELLED).first()
        if existing and data.source_key:
            return existing, False
        now = clock.now()
        route = PatientRoute.objects.create(
            patient_id=data.patient_id,
            source_document_id=data.document_id,
            source_external_id=data.source_key,
            kind=PatientRoute.Kind.TRIGGER,
            trigger_rule=rule,
            trigger_code=rule.finding_code,
            rule_version=rule.version,
            reason=self._reason(rule.title, data),
            evidence={
                "quote": match.finding.get("evidence_quote", ""),
                "attributes": match.finding.get("attributes", {}),
                "confidence": match.finding.get("confidence"),
                "uncertain": match.finding.get("uncertain", False),
                "rule": match.explanation,
                "potential_route": rule.potential_route,
                "also_found": match.also_found,
            },
            responsible_unit=rule.responsible_unit,
            location_code=data.location_code,
            detected_at=now,
            target_date=(now + timedelta(days=rule.target_days)).date(),
        )
        log_event(route, "route_created", to_status=route.status, actor_type=actor_type,
                  basis=match.explanation, payload={"document_id": data.document_id, "finding": match.finding})
        publish_route_created(route)
        return route, True

    def _get_or_create_recommendation_route(self, data: RoutingInput):
        existing = PatientRoute.objects.filter(
            patient_id=data.patient_id, source_external_id=data.source_key, trigger_code="recommendation",
        ).exclude(status=PatientRoute.Status.CANCELLED).first()
        if existing and data.source_key:
            return existing, False
        now = clock.now()
        route = PatientRoute.objects.create(
            patient_id=data.patient_id, source_document_id=data.document_id, source_external_id=data.source_key,
            kind=PatientRoute.Kind.RECOMMENDATION, trigger_code="recommendation",
            reason=self._reason("Рекомендации по результатам исследования", data),
            evidence={"recommendations": data.recommendations}, location_code=data.location_code,
            detected_at=now, target_date=(now + timedelta(days=30)).date(),
        )
        log_event(route, "route_created", to_status=route.status, actor_type="ai",
                  basis="Рекомендации в протоколе без триггера матрицы")
        publish_route_created(route)
        return route, True

    @staticmethod
    def _add_template_steps(route: PatientRoute, template: RouteTemplate, *, target_days: int, first_specialty: str) -> None:
        for i, ts in enumerate(template.steps.all()):
            RouteStep.objects.create(
                route=route, order=ts.order, step_type=ts.step_type, title=ts.title,
                specialty_code=ts.specialty_code or (first_specialty if i == 0 else ""),
                service_code=ts.service_code, offset_days=ts.offset_days,
                window_days=target_days if i == 0 else ts.window_days,
                auto_book=ts.auto_book, escalation_policy=ts.escalation_policy, source=RouteStep.Source.RULE,
            )

    @staticmethod
    def _add_recommendation_steps(route: PatientRoute, recommendations: list[dict], skip_specialties: set[str] | None = None) -> None:
        skip = set(skip_specialties or ())
        order = (route.steps.order_by("-order").values_list("order", flat=True).first() or 0) + 1
        policy = EscalationPolicy.objects.filter(code="booking_standard").first()
        for rec in recommendations:
            specialty = rec.get("specialty_code") or ""
            interval = rec.get("interval_days") or 0
            if specialty and specialty in skip and not interval:
                continue
            if rec.get("kind") == "diagnostics" or (rec.get("service") and not specialty):
                step_type, title = StepType.DIAGNOSTICS, (rec.get("service") or "Обследование")
                if interval:
                    title += f" (через {interval} дн.)"
            elif rec.get("kind") == "observation":
                step_type, title = StepType.FOLLOW_UP, f"Наблюдение {_genitive(specialty)}".strip()
            else:
                step_type, title = StepType.CONSULTATION, f"Консультация {_genitive(specialty)}".strip()
            RouteStep.objects.create(
                route=route, order=order, step_type=step_type, title=title[:255], specialty_code=specialty,
                service_code=rec.get("service") or "", offset_days=interval, window_days=14,
                source=RouteStep.Source.AI_RECOMMENDATION, escalation_policy=policy,
                comment=rec.get("text", ""),
            )
            skip.add(specialty)
            order += 1


class RouteRecalculator:
    """Исправление/аннулирование протокола: пересчёт без дублей (п. 13 кейса)."""

    @transaction.atomic
    def on_corrected(self, data: RoutingInput) -> list[PatientRoute]:
        from .planner import RoutePlanner

        S = PatientRoute.Status
        current = {r.trigger_code: r for r in PatientRoute.objects.filter(
            patient_id=data.patient_id, source_external_id=data.source_key).exclude(status__in=PatientRoute.CLOSED_STATUSES)}
        matched = RouteBuilder().matrix.match(data.findings)
        new_codes = {m.rule.finding_code for m in matched}
        for code, route in current.items():
            if code not in new_codes and code != "recommendation":
                RoutePlanner().close(route, S.CANCELLED, basis="Протокол исправлен: находка не подтверждена", actor_type="mis")
            else:
                route.source_document_id = data.document_id
                route.save(update_fields=["source_document_id", "updated_at"])
                log_event(route, "protocol_corrected", actor_type="mis", basis=f"Новая версия протокола {data.document_id}")
        return RouteBuilder().build(data, actor_type="mis")

    @transaction.atomic
    def on_annulled(self, document_id: str, patient_id: str) -> int:
        from .planner import RoutePlanner

        routes = PatientRoute.objects.filter(patient_id=patient_id, source_document_id=document_id).exclude(
            status__in=PatientRoute.CLOSED_STATUSES)
        for route in routes:
            RoutePlanner().close(route, PatientRoute.Status.CANCELLED, basis="Протокол аннулирован в МИС", actor_type="mis")
        return len(routes)
