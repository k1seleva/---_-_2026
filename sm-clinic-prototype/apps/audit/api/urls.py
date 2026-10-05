from django.urls import path

from .views import AuditCheckView, AuditListView, AuditStatsView, DocumentAuditsView

urlpatterns = [
    path("check/", AuditCheckView.as_view(), name="audit-check"),
    path("audits/", AuditListView.as_view(), name="audit-list"),
    path("stats/", AuditStatsView.as_view(), name="audit-stats"),
    path("documents/<uuid:document_id>/", DocumentAuditsView.as_view(), name="audit-document"),
]
