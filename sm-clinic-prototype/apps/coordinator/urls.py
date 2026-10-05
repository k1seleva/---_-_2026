from django.urls import path

from . import views

app_name = "coordinator"
urlpatterns = [
    # Главная рабочего места
    path("", views.home, name="home"),
    path("clinic/", views.switch_clinic, name="clinic"),
    path("search/", views.search, name="search"),
    # Раздел «Маршруты и протоколы»
    path("inbox/", views.inbox, name="inbox"),
    path("inbox/bulk/", views.inbox_bulk, name="inbox_bulk"),
    path("cases/<uuid:document_id>/", views.case_detail, name="case"),
    path("cases/<uuid:document_id>/action/", views.case_action, name="case_action"),
    path("cases/<uuid:document_id>/advice/<uuid:advice_id>/review/", views.advice_review, name="advice_review"),
    path("cases/<uuid:document_id>/advice/regenerate/", views.advice_regenerate, name="advice_regenerate"),
    path("unmatched/", views.unmatched, name="unmatched"),
    path("unmatched/<uuid:placeholder_id>/assign/", views.unmatched_assign, name="unmatched_assign"),
    path("routes/", views.routes, name="routes"),
    path("routes/<uuid:route_id>/", views.route_compare, name="compare"),
    path("routes/<uuid:route_id>/correct/", views.correct_route, name="correct"),
    path("tasks/", views.tasks, name="tasks"),
    path("tasks/<uuid:pk>/done/", views.task_done, name="task_done"),
    path("audits/", views.audits, name="audits"),
    path("audits/<uuid:audit_id>/", views.audit_detail, name="audit"),
    # Раздел «Аналитика»
    path("analytics/", views.analytics, name="analytics"),
    path("analytics/clinics/", views.clinics, name="clinics"),
    path("analytics/features/", views.features, name="features"),
    path("stage/<str:code>/", views.stage, name="stage"),
    # Настройки
    path("settings/tags/", views.tags, name="tags"),
    path("settings/priorities/", views.priorities, name="priorities"),
]
