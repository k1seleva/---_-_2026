"""Сценарии модуля проверки рекомендаций: проверить, сохранить, сообщить координатору, принять решение."""
from django.db import transaction
from django.utils import timezone

from common.events import contracts
from common.events.bus import publish

from ..models import AuditIssue, RecommendationAudit
from .auditor import AuditOutcome, Coverage, RecommendationAuditor
from .indications import IndicationMatrix, evidence_from_document
from .recommendations import from_protocol, from_visit


def _genitive(code: str) -> str:
    from apps.doctors.facade import DoctorsFacade

    return DoctorsFacade.specialty_genitive(code, default=code)


ACTIVE_STEP_STATUSES = {"planned", "awaiting_booking", "booked", "done"}


def route_coverage(routes: list[dict], skip_step_id: str | None = None) -> Coverage:
    """Этапы маршрута, которые уже закрывают показания (отменённые и пропущенные не считаются)."""
    steps = [s for r in routes for s in r.get("steps", [])
             if s["status"] in ACTIVE_STEP_STATUSES and s["id"] != skip_step_id]
    return Coverage(route_specialties={s["specialty_code"] for s in steps if s["specialty_code"]},
                    route_services=[s["title"] for s in steps if s["step_type"] == "diagnostics"])


class AuditService:
    def __init__(self, auditor: RecommendationAuditor | None = None) -> None:
        self.auditor = auditor or RecommendationAuditor(IndicationMatrix(), genitive=_genitive)

    # ------------------------------------------------------------------ протокол
    def check_protocol(self, payload: dict, routes: list[dict] | None = None) -> AuditOutcome:
        """Проверка без сохранения (dry-run для API и тестов). payload — событие document_processed,
        routes — открытые маршруты по протоколу (что матрица маршрутизации уже запустила)."""
        extraction = payload.get("extraction", {})
        annotation = payload.get("annotation", {})
        segments = annotation.get("segments", [])
        return self.auditor.audit(
            evidence=evidence_from_document(extraction.get("findings", []), segments),
            items=from_protocol(extraction.get("recommendations", []), segments),
            source="protocol", coverage=route_coverage(routes or []),
            not_in_conclusion=(annotation.get("summary") or {}).get("not_in_conclusion", []),
        )

    def audit_protocol(self, payload: dict) -> RecommendationAudit:
        # Модуль подключён после маршрутизации: к этому моменту маршруты по протоколу уже построены.
        # При асинхронной шине проверку ставят в цепочку после построения маршрута (Celery chain).
        routes = self._routes_for(payload["document_id"])
        outcome = self.check_protocol(payload, routes)
        return self._save(outcome, patient_id=payload["patient_id"], document_id=payload["document_id"],
                          route_id=routes[0]["id"] if routes else None, source=RecommendationAudit.Source.PROTOCOL)

    # ------------------------------------------------------------------ приём врача
    def audit_visit(self, payload: dict) -> RecommendationAudit | None:
        """Назначения врача на приёме против показаний из исходного протокола маршрута."""
        from apps.processing.facade import ProcessingFacade
        from apps.routing.facade import RoutingFacade

        route = RoutingFacade.get_route(payload["route_id"]) if payload.get("route_id") else None
        document_id = route and route.get("source_document_id")
        if not document_id:
            return None
        document = ProcessingFacade.get_document(document_id) or {}
        annotation = ProcessingFacade.get_annotation(document_id) or {}
        # Значимые фрагменты — с подсветкой находки, изменения, порога или «не в заключении» (уровней важности нет).
        segments = [s for s in annotation.get("segments", []) if s.get("significant")]
        coverage = route_coverage([route], skip_step_id=payload.get("route_step_id"))
        coverage.visit_specialties = {payload.get("specialty_code", "")} - {""}
        outcome = self.auditor.audit(
            evidence=evidence_from_document(document.get("findings", []), segments),
            items=from_visit(payload), source="visit", coverage=coverage,
        )
        return self._save(outcome, patient_id=payload["patient_id"], document_id=document_id, route_id=payload["route_id"],
                          source=RecommendationAudit.Source.VISIT, source_ref=payload.get("appointment_id", ""))

    # ------------------------------------------------------------------ сохранение и событие
    @transaction.atomic
    def _save(self, outcome: AuditOutcome, *, patient_id, document_id, route_id, source: str, source_ref: str = "") -> RecommendationAudit:
        needs_decision = outcome.verdict == RecommendationAudit.Verdict.INSUFFICIENT
        audit, _ = RecommendationAudit.objects.update_or_create(
            document_id=document_id, source=source, source_ref=source_ref,
            defaults={
                "patient_id": patient_id, "route_id": route_id, "verdict": outcome.verdict, "summary": outcome.summary,
                "status": RecommendationAudit.Status.OPEN if needs_decision else RecommendationAudit.Status.NOT_REQUIRED,
                "items": [i.as_dict() for i in outcome.items],
                "indications": [i.as_dict() for i in outcome.indications],
                "rules_version": self.auditor.matrix.version,
            },
        )
        audit.issues.all().delete()
        issues = AuditIssue.objects.bulk_create(
            AuditIssue(audit=audit, issue_type=i.issue_type, severity=i.severity, message=i.message,
                       finding_code=i.finding_code, evidence_quote=i.evidence_quote, specialty_code=i.specialty_code,
                       rule_code=i.rule_code, proposed_operation=i.proposed_operation,
                       decision=AuditIssue.Decision.PENDING if i.severity in ("critical", "major")
                       else AuditIssue.Decision.NOT_REQUIRED)
            for i in outcome.issues)
        publish(contracts.AUDIT_COMPLETED, {
            "audit_id": str(audit.id), "document_id": str(document_id), "patient_id": str(patient_id),
            "route_id": str(route_id) if route_id else None, "source": source, "verdict": audit.verdict,
            "summary": audit.summary,
            "issues": [{"id": str(i.id), "type": i.issue_type, "severity": i.severity, "message": i.message,
                        "proposed_operation": i.proposed_operation} for i in issues],
        }, event_id=f"{contracts.AUDIT_COMPLETED}:{audit.id}:{audit.updated_at.isoformat()}")
        return audit

    @staticmethod
    def _routes_for(document_id) -> list[dict]:
        from apps.routing.facade import RoutingFacade

        return RoutingFacade.routes_for_document(document_id)

    # ------------------------------------------------------------------ решения координатора
    @transaction.atomic
    def apply_decisions(self, audit_id, decisions: list[dict]) -> RecommendationAudit | None:
        audit = RecommendationAudit.objects.filter(pk=audit_id).first()
        if not audit:
            return None
        now = timezone.now()
        for d in decisions:
            AuditIssue.objects.filter(audit=audit, pk=d["issue_id"]).update(
                decision=d["decision"], decision_comment=d.get("comment", ""), decided_at=now)
        if not audit.issues.filter(decision=AuditIssue.Decision.PENDING).exists():
            audit.status = RecommendationAudit.Status.RESOLVED
            audit.save(update_fields=["status", "updated_at"])
        return audit

    @staticmethod
    def cancel_for_document(document_id) -> int:
        return RecommendationAudit.objects.filter(document_id=document_id).exclude(
            status=RecommendationAudit.Status.CANCELLED).update(status=RecommendationAudit.Status.CANCELLED)
