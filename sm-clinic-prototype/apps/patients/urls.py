from django.urls import path

from . import views

app_name = "patients"
urlpatterns = [
    # Вход пациента вместо списка всех пациентов: каждый видит только свой кабинет.
    path("", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("<uuid:pk>/", views.cabinet, name="cabinet"),
    path("<uuid:pk>/settings/", views.settings_view, name="settings"),
    path("<uuid:pk>/notifications/read/", views.notifications_read, name="notifications_read"),
    path("<uuid:pk>/notifications/<uuid:notification_id>/", views.notification_open, name="notification_open"),
    path("<uuid:pk>/results/<uuid:doc_id>/", views.result_summary, name="result"),
    path("<uuid:pk>/results/<uuid:doc_id>/full/", views.result_full, name="result_full"),
    path("<uuid:pk>/book/<uuid:step_id>/", views.book, name="book"),
    path("<uuid:pk>/action/", views.route_action, name="action"),
]
