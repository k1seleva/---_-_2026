from django.apps import AppConfig


class AuditConfig(AppConfig):
    name = "apps.audit"
    label = "audit"
    verbose_name = "Модуль проверки рекомендаций (коррекция маршрута)"

    def ready(self) -> None:
        from common import labels

        from . import handlers
        from .models import AuditIssue, IndicationRule, RecommendationAudit

        handlers.register()
        labels.register_choices("audit_verdict", RecommendationAudit.Verdict)
        labels.register_choices("audit_status", RecommendationAudit.Status)
        labels.register_choices("audit_source", RecommendationAudit.Source)
        labels.register_choices("issue_type", AuditIssue.IssueType)
        labels.register_choices("issue_decision", AuditIssue.Decision)
        labels.register_choices("issue_severity", IndicationRule.Severity)
        labels.register_choices("requirement", IndicationRule.Requirement)
        labels.register_resolver("indication_rule", lambda codes=None: dict(
            (IndicationRule.objects.filter(code__in=list(codes)) if codes is not None else IndicationRule.objects.all())
            .values_list("code", "title")))
