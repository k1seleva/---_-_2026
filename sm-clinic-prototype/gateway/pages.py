"""Главная страница прототипа и управление модельным временем из шапки."""
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import path
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.routing.services.escalation import EscalationEngine
from common import clock


def home(request):
    return render(request, "gateway/home.html")


@require_POST
def sim(request, op: str):
    if op == "advance":
        clock.advance(hours=float(request.POST.get("hours", 0)))
    elif op == "reset":
        clock.reset()
    report = EscalationEngine().tick()
    messages.success(request, f"Время сдвинуто. Активировано этапов: {report.activated}, эскалаций: {report.escalations}, "
                              f"закрыто маршрутов: {report.closed}.")
    target = request.POST.get("next", "/")
    return redirect(target if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}) else "/")


urlpatterns = [
    path("", home, name="home"),
    path("sim/<str:op>/", sim, name="sim-page"),
]
