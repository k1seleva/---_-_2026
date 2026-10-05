from django.contrib import admin

from .models import AuditIssue, IndicationRule, RecommendationAudit


@admin.register(IndicationRule)
class IndicationRuleAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "finding_code", "requirement", "specialty_code", "service_title", "max_days", "severity", "is_active")
    list_filter = ("requirement", "severity", "is_active")
    search_fields = ("code", "title", "finding_code")


class AuditIssueInline(admin.TabularInline):
    model = AuditIssue
    extra = 0


@admin.register(RecommendationAudit)
class RecommendationAuditAdmin(admin.ModelAdmin):
    list_display = ("created_at", "source", "verdict", "status", "patient_id", "document_id")
    list_filter = ("source", "verdict", "status")
    inlines = [AuditIssueInline]
