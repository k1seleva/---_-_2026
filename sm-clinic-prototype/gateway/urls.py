"""API-шлюз: версионированная точка входа. Каждый модуль монтируется под своим префиксом —
при выносе в микросервис префикс проксируется (nginx / Kong / Traefik) на отдельный сервис."""
from django.urls import include, path

from .views import HealthView, MisEventWebhook, SimulationView

urlpatterns = [
    path("processing/", include("apps.processing.api.urls")),
    path("routing/", include("apps.routing.api.urls")),
    path("coordinator/", include("apps.coordinator.api.urls")),
    path("patients/", include("apps.patients.api.urls")),
    path("doctors/", include("apps.doctors.api.urls")),
    path("audit/", include("apps.audit.api.urls")),
    path("mis/events/", MisEventWebhook.as_view(), name="mis-events"),
    path("sim/<str:op>/", SimulationView.as_view(), name="sim"),
    path("health/", HealthView.as_view(), name="health"),
]
