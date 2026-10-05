"""Страницы входа координатора и врача (у пациента — своя страница /patient/)."""
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .staff_auth import ROLES, StaffAuthService, StaffLoginError, current_role

DEMO_LOGINS = {"coordinator": "coordinator", "doctor": "doctor1"}


def login_view(request, role: str):
    """Вход в роль: логин и пароль. Вошедший сразу попадает в свой раздел (или по ссылке, с которой пришёл)."""
    spec = ROLES[role]
    target = request.POST.get("next") or request.GET.get("next") or ""
    safe = target if target.startswith(spec.prefixes) and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}) else ""
    if current_role(request) == role:
        return redirect(safe or spec.home)
    error = ""
    if request.method == "POST":
        service = StaffAuthService()
        try:
            user = service.authenticate(request, request.POST.get("username", ""), request.POST.get("password", ""), role)
        except StaffLoginError as exc:
            error = str(exc)
        else:
            service.login(request, user, role)
            return redirect(safe or spec.home)
    demo = None
    if settings.STAFF_LOGIN["SHOW_DEMO_HINT"]:
        demo = {"username": DEMO_LOGINS[role], "password": settings.STAFF_LOGIN["DEMO_PASSWORD"]}
    other = current_role(request)
    return render(request, "gateway/staff_login.html", {
        "role": spec, "error": error, "next": safe, "username": request.POST.get("username", ""), "demo": demo,
        "other_role": ROLES[other].title if other else "",
    }, status=400 if error else 200)


@require_POST
def logout_view(request):
    role = current_role(request) or "coordinator"
    StaffAuthService.logout(request)
    messages.success(request, "Вы вышли из рабочего места")
    return redirect(ROLES[role].login_url)
