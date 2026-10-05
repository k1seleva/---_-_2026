"""
Публичный интерфейс модуля проверки рекомендаций (только чтение, DTO-словари).
Изменения (решения координатора) приходят событием coordinator.audit_resolved.
"""
from collections import Counter
from uuid import UUID

from django.db.models import Count, Q

from .models import AuditIssue, RecommendationAudit


def _issue_dto(i: AuditIssue) -> dict:
    return {
        "id": str(i.id), "type": i.issue_type, "type_display": i.get_issue_type_display(), "severity": i.severity,
        "message": i.message, "finding_code": i.finding_code, "evidence_quote": i.evidence_quote,
        "specialty_code": i.specialty_code, "rule_code": i.rule_code, "proposed_operation": i.proposed_operation,
        "decision": i.decision, "decision_display": i.get_decision_display(), "decision_comment": i.decision_comment,
    }


def _audit_dto(a: RecommendationAudit, with_issues: bool = True) -> dict:
    data = {
        "id": str(a.id), "patient_id": str(a.patient_id), "document_id": str(a.document_id),
        "route_id": str(a.route_id) if a.route_id else None, "source": a.source, "source_display": a.get_source_display(),
        "source_ref": a.source_ref, "verdict": a.verdict, "verdict_display": a.get_verdict_display(),
        "status": a.status, "status_display": a.get_status_display(), "summary": a.summary, "items": a.items,
        "indications": a.indications, "rules_version": a.rules_version, "created_at": a.created_at,
    }
    if with_issues:
        data["issues"] = [_issue_dto(i) for i in a.issues.all()]
    return data


class AuditFacade:
    @staticmethod
    def check_protocol(payload: dict, routes: list[dict] | None = None) -> dict:
        """Проверка рекомендаций без сохранения (пакетная оценка, dry-run)."""
        from .services.service import AuditService

        return AuditService().check_protocol(payload, routes).as_dict()

    @staticmethod
    def get(audit_id: UUID | str) -> dict | None:
        a = RecommendationAudit.objects.filter(pk=audit_id).first()
        return _audit_dto(a) if a else None

    @staticmethod
    def for_document(document_id: UUID | str) -> list[dict]:
        return [_audit_dto(a) for a in RecommendationAudit.objects.filter(document_id=document_id)]

    @staticmethod
    def list_audits(*, open_only: bool = False, limit: int = 50) -> list[dict]:
        qs = RecommendationAudit.objects.exclude(status=RecommendationAudit.Status.CANCELLED)
        if open_only:
            qs = qs.filter(status=RecommendationAudit.Status.OPEN)
        else:
            qs = qs.exclude(verdict=RecommendationAudit.Verdict.SUFFICIENT)
        return [_audit_dto(a) for a in qs[:limit]]

    @staticmethod
    def stats() -> dict:
        """Аналитика проверок: вердикты, частые пропуски, точность правил по решениям координатора."""
        audits = RecommendationAudit.objects.exclude(status=RecommendationAudit.Status.CANCELLED)
        verdicts = Counter(audits.values_list("verdict", flat=True))
        issues = AuditIssue.objects.filter(audit__in=audits)
        by_type = issues.values("issue_type").annotate(n=Count("id")).order_by("-n")
        rules = issues.exclude(rule_code="").values("rule_code").annotate(
            total=Count("id"),
            accepted=Count("id", filter=Q(decision=AuditIssue.Decision.ACCEPTED)),
            rejected=Count("id", filter=Q(decision=AuditIssue.Decision.REJECTED)),
        ).order_by("-total")
        titles = dict(AuditIssue.IssueType.choices)
        return {
            "total": sum(verdicts.values()),
            "verdicts": [{"code": c, "title": t, "count": verdicts.get(c, 0)} for c, t in RecommendationAudit.Verdict.choices],
            "open": audits.filter(status=RecommendationAudit.Status.OPEN).count(),
            "by_type": [{"type": r["issue_type"], "title": titles.get(r["issue_type"], r["issue_type"]), "count": r["n"]}
                        for r in by_type],
            "rules": [{**r, "precision": round(100 * r["accepted"] / (r["accepted"] + r["rejected"]))
                       if r["accepted"] + r["rejected"] else None} for r in rules],
        }
