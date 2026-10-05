"""
Вход сотрудников: координатор и врач. Три роли не смешиваются: пациент входит по номеру карты в свой
кабинет (patients/services/auth.py, ключ сессии patient_id), координатор и врач — по логину и паролю
каждый на своей странице, и каждый видит только свой раздел.

Роль задаётся группой учётной записи Django («coordinator» или «doctor»); врач связан со своей карточкой
(Doctor.user) и видит только своё расписание и своих пациентов. Суперпользователь (createsuperuser) может
войти в любую роль. Защита от подбора как у пациента: после MAX_ATTEMPTS неудачных попыток вход под этим
логином закрывается на LOCK_MINUTES. В пилоте вход заменяется SSO клиники, роль — из групп каталога.
"""
from dataclasses import dataclass
from urllib.parse import quote

from django.conf import settings
from django.contrib import auth, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.shortcuts import redirect

ROLE_KEY = "staff_role"
DOCTOR_KEY = "doctor_id"


@dataclass(frozen=True)
class Role:
    code: str
    title: str
    home: str
    login_url: str
    prefixes: tuple[str, ...]


ROLES = {
    "coordinator": Role("coordinator", "Координатор", "/coordinator/", "/coordinator/login/", ("/coordinator/", "/processing/")),
    "doctor": Role("doctor", "Врач", "/doctor/", "/doctor/login/", ("/doctor/",)),
}
# Страницы без входа внутри закрытых разделов.
OPEN_PATHS = ("/coordinator/login/", "/doctor/login/", "/staff/logout/")


class StaffLoginError(ValueError):
    pass


class StaffAuthService:
    def __init__(self) -> None:
        cfg = settings.STAFF_LOGIN
        self.max_attempts, self.lock_seconds = cfg["MAX_ATTEMPTS"], cfg["LOCK_MINUTES"] * 60

    def authenticate(self, request, username: str, password: str, role: str):
        username = (username or "").strip()
        if not username or not password:
            raise StaffLoginError("Введите логин и пароль")
        key = f"staff-login:{username.lower()}"
        attempts = cache.get(key, 0)
        if attempts >= self.max_attempts:
            raise StaffLoginError(f"Слишком много попыток. Попробуйте через {self.lock_seconds // 60} минут "
                                  "или обратитесь к администратору.")
        user = auth.authenticate(request, username=username, password=password)
        if user is None or not user.is_active:
            cache.set(key, attempts + 1, self.lock_seconds)
            # Одинаковый ответ для «нет такого логина» и «неверный пароль».
            raise StaffLoginError("Неверный логин или пароль")
        if not has_role(user, role):
            # Пароль верный, но роль другая: подсказываем нужную страницу, попытку не считаем подбором.
            other = next((r for r in ROLES.values() if has_role(user, r.code)), None)
            hint = f" Войдите на странице «{other.title}»." if other else ""
            raise StaffLoginError(f"У этой учётной записи нет роли «{ROLES[role].title}».{hint}")
        cache.delete(key)
        return user

    @staticmethod
    def login(request, user, role: str) -> None:
        patient_id = request.session.get("patient_id")  # вход пациента в этом браузере не трогаем
        auth.login(request, user)  # новый идентификатор сессии
        if patient_id:
            request.session["patient_id"] = patient_id
        request.session[ROLE_KEY] = role
        if role == "doctor":
            from apps.doctors.facade import DoctorsFacade

            doctor_id = DoctorsFacade.doctor_for_user(user.id)
            if doctor_id:
                request.session[DOCTOR_KEY] = doctor_id
            else:
                request.session.pop(DOCTOR_KEY, None)

    @staticmethod
    def logout(request) -> None:
        patient_id = request.session.get("patient_id")
        auth.logout(request)  # сессия очищается целиком
        if patient_id:
            request.session["patient_id"] = patient_id

    @staticmethod
    def set_account(username: str, password: str, role: str, *, full_name: str = ""):
        """Учётная запись сотрудника с ролью (seed_demo создаёт демо-координатора и демо-врачей)."""
        User = get_user_model()
        user = User.objects.filter(username=username).first() or User(username=username)
        if full_name:
            parts = full_name.split(" ", 1)
            user.last_name, user.first_name = parts[0][:150], (parts[1] if len(parts) > 1 else "")[:150]
        user.set_password(password)
        user.is_active = True
        user.save()
        user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user


def has_role(user, role: str) -> bool:
    return bool(user and user.is_authenticated and (user.is_superuser or user.groups.filter(name=role).exists()))


def current_role(request) -> str:
    role = request.session.get(ROLE_KEY, "")
    # Роль проверяется по учётной записи каждый раз: снятая администратором роль действует сразу.
    return role if role in ROLES and has_role(request.user, role) else ""


def required_role(path: str) -> str:
    if path.startswith(OPEN_PATHS):
        return ""
    return next((r.code for r in ROLES.values() if path.startswith(r.prefixes)), "")


class StaffAccessMiddleware:
    """Закрытые разделы: /coordinator/ и /processing/ — координатору, /doctor/ — врачу.
    Не вошёл — на страницу входа своей роли с возвратом; вошёл в другой роли — в свой раздел."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        need = required_role(request.path)
        if need:
            role = current_role(request)
            if not role:
                return redirect(f"{ROLES[need].login_url}?next={quote(request.get_full_path())}")
            if role != need:
                messages.info(request, f"Этот раздел для роли «{ROLES[need].title}». Вы вошли как "
                                       f"«{ROLES[role].title}»: выйдите, чтобы сменить роль.")
                return redirect(ROLES[role].home)
        return self.get_response(request)
