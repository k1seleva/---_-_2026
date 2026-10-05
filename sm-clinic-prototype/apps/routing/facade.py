"""Публичный интерфейс модуля маршрутизации (только чтение + проверка записи).
Изменения маршрута другие модули инициируют событиями, а не прямыми вызовами."""
from uuid import UUID

from .models import PatientRoute, RouteStep


def _step_dto(s: RouteStep) -> dict:
    return {
        "id": str(s.id), "order": s.order, "step_type": s.step_type, "step_type_display": s.get_step_type_display(),
        "title": s.title, "specialty_code": s.specialty_code, "status": s.status, "status_display": s.get_status_display(),
        "source": s.source, "earliest_date": s.earliest_date, "due_date": s.due_date, "appointment_id": str(s.appointment_id or ""),
        "attempt": s.attempt, "outcome": s.outcome, "comment": s.comment,
    }


def _route_dto(r: PatientRoute, with_steps: bool = True) -> dict:
    data = {
        "id": str(r.id), "patient_id": str(r.patient_id), "kind": r.kind, "kind_display": r.get_kind_display(),
        "reason": r.reason, "status": r.status, "status_display": r.get_status_display(), "is_open": r.is_open,
        "trigger_code": r.trigger_code, "rule_version": r.rule_version, "evidence": r.evidence,
        "source_document_id": str(r.source_document_id or ""), "detected_at": r.detected_at,
        "target_date": r.target_date, "cycle_no": r.cycle_no, "parent_id": str(r.parent_id or ""),
        "responsible_unit": r.responsible_unit, "closed_at": r.closed_at, "close_reason": r.close_reason,
    }
    if with_steps:
        data["steps"] = [_step_dto(s) for s in r.steps.order_by("order")]
        active = r.active_step
        data["active_step"] = _step_dto(active) if active else None
    return data


class RoutingFacade:
    @staticmethod
    def get_route(route_id: UUID | str) -> dict | None:
        r = PatientRoute.objects.filter(pk=route_id).first()
        return _route_dto(r) if r else None

    @staticmethod
    def list_patient_routes(patient_id: UUID | str, *, open_only: bool = False) -> list[dict]:
        qs = PatientRoute.objects.filter(patient_id=patient_id)
        if open_only:
            qs = qs.exclude(status__in=PatientRoute.CLOSED_STATUSES)
        return [_route_dto(r) for r in qs]

    @staticmethod
    def unfinished_routes(patient_id: UUID | str) -> list[dict]:
        """Для баннера «Незавершённый клинический маршрут» на форме приёма (сценарий 2, этап 2)."""
        qs = PatientRoute.objects.filter(patient_id=patient_id).exclude(
            status__in=[PatientRoute.Status.COMPLETED, PatientRoute.Status.CANCELLED, PatientRoute.Status.SEEN_ELSEWHERE])
        return [_route_dto(r) for r in qs]

    @staticmethod
    def get_step(step_id: UUID | str) -> dict | None:
        s = RouteStep.objects.select_related("route").filter(pk=step_id).first()
        if not s:
            return None
        return {**_step_dto(s), "route_id": str(s.route_id), "patient_id": str(s.route.patient_id),
                "route_open": s.route.is_open, "reason": s.route.reason, "location_code": s.route.location_code}

    @staticmethod
    def validate_booking(step_id: UUID | str, patient_id: UUID | str, specialty_code: str) -> tuple[bool, str]:
        """Запись «строго согласно маршруту»: этап активен, принадлежит пациенту и совпадает специальность."""
        step = RoutingFacade.get_step(step_id)
        if not step:
            return False, "Этап маршрута не найден"
        if step["patient_id"] != str(patient_id):
            return False, "Этап принадлежит другому пациенту"
        if not step["route_open"] or step["status"] != RouteStep.Status.AWAITING_BOOKING:
            return False, "Этап сейчас не ожидает записи"
        if step["specialty_code"] and step["specialty_code"] != specialty_code:
            return False, "Специальность врача не соответствует этапу маршрута"
        return True, ""

    @staticmethod
    def routes_for_document(document_id: UUID | str, *, open_only: bool = True) -> list[dict]:
        """Маршруты, запущенные по протоколу: главный (самый приоритетный триггер) — первым."""
        qs = PatientRoute.objects.filter(source_document_id=document_id).select_related("trigger_rule")
        if open_only:
            qs = qs.exclude(status__in=PatientRoute.CLOSED_STATUSES)
        routes = sorted(qs, key=lambda r: (r.trigger_rule.priority if r.trigger_rule else 999, r.created_at))
        return [_route_dto(r) for r in routes]

    @staticmethod
    def trigger_titles(codes=None) -> dict[str, str]:
        """Подпись триггера: название правила матрицы по коду находки (самое приоритетное правило)."""
        from .models import TriggerRule

        qs = TriggerRule.objects.filter(is_active=True).order_by("-priority")
        if codes is not None:
            qs = qs.filter(finding_code__in=list(codes))
        return dict(qs.values_list("finding_code", "title"))

    @staticmethod
    def explain_non_triggers(findings: list[dict]) -> list[str]:
        from .services.matrix import RoutingMatrix

        return RoutingMatrix().explain_non_triggers(findings)

    @staticmethod
    def matched_rules(findings: list[dict]) -> list[str]:
        from .services.matrix import RoutingMatrix

        return [m.rule.code for m in RoutingMatrix().match(findings)]

    @staticmethod
    def match_rules_detail(findings: list[dict]) -> list[dict]:
        """Сработавшие правила матрицы с подробностями — для объекта «триггер» в разборе протокола."""
        from .services.matrix import RoutingMatrix

        return [{
            "rule_code": m.rule.code, "rule_version": m.rule.version, "rule_title": m.rule.title,
            "priority": m.rule.priority, "route_group": m.rule.route_group, "is_emergency": m.rule.is_emergency,
            "template_code": m.rule.template.code, "template_title": m.rule.template.title,
            "specialty_code": m.rule.first_specialty_code, "potential_route": m.rule.potential_route,
            "also_found": list(m.also_found), "finding": m.finding,
        } for m in RoutingMatrix().match(findings)]

    @staticmethod
    def route_catalog() -> list[dict]:
        """Справочник маршрутов для советов ИИ-агента и их проверки: шаблон, первая специальность, правило."""
        from .models import TriggerRule

        catalog: dict[str, dict] = {}
        for r in TriggerRule.objects.filter(is_active=True).select_related("template").order_by("priority"):
            item = catalog.setdefault(r.template.code, {
                "code": r.template.code, "title": r.template.title, "specialty_code": r.first_specialty_code,
                "responsible_unit": r.responsible_unit, "finding_codes": [], "rules": [],
            })
            item["finding_codes"] = list(dict.fromkeys(item["finding_codes"] + [r.finding_code]))
            item["rules"].append(r.code)
        return list(catalog.values())

    @staticmethod
    def rule_titles(codes=None) -> dict[str, str]:
        """Код правила матрицы -> название («Полип ЖП ≥ 10 мм → хирург…»): для отчётов о качестве разбора."""
        from .models import TriggerRule

        qs = TriggerRule.objects.all()
        if codes is not None:
            qs = qs.filter(code__in=list(codes))
        return dict(qs.values_list("code", "title"))

    @staticmethod
    def preview_routes(findings: list[dict]) -> list[dict]:
        """Какие маршруты матрица построила бы по находкам (без записи в БД) — для пакетной оценки."""
        from .services.matrix import RoutingMatrix

        return [{"id": None, "reason": m.rule.title, "steps": [
            {"id": "", "status": "planned", "step_type": s.step_type, "title": s.title, "specialty_code": s.specialty_code}
            for s in m.rule.template.steps.all()]} for m in RoutingMatrix().match(findings)]
