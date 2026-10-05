"""
Вход пациента в личный кабинет: номер амбулаторной карты и пароль.

Пациент видит только свой кабинет: идентификатор вошедшего пациента хранится в сессии отдельно от
входа сотрудников (координатор и врач работают в своей учётной записи), поэтому роли не смешиваются.
Пароль хранится в учётной записи Django (Patient.user) в виде хеша. Защита от подбора: после
LOGIN_MAX_ATTEMPTS неудачных попыток вход по этой карте закрывается на LOGIN_LOCK_MINUTES.

В пилоте вход заменяется входом через личный кабинет СМ-Клиники (SSO) или кодом из SMS;
сервис кабинета от этого не меняется: ему нужен только идентификатор пациента в сессии.
"""
from functools import wraps
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Q
from django.shortcuts import redirect

from ..models import Patient

SESSION_KEY = "patient_id"
# Номер карты печатают по-разному: «АК-0001» кириллицей и «AK-0001» латиницей — это одна карта.
CARD_LOOKALIKES = str.maketrans("АВЕКМНОРСТХ", "ABEKMHOPCTX")


class PatientLoginError(ValueError):
    pass


class PatientAuthService:
    def __init__(self) -> None:
        cfg = settings.PATIENT_LOGIN
        self.max_attempts, self.lock_seconds = cfg["MAX_ATTEMPTS"], cfg["LOCK_MINUTES"] * 60

    def authenticate(self, card_number: str, password: str) -> Patient:
        typed = (card_number or "").strip().upper()
        card = typed.translate(CARD_LOOKALIKES)
        if not card or not password:
            raise PatientLoginError("Введите номер карты и пароль")
        key = f"patient-login:{card}"
        attempts = cache.get(key, 0)
        if attempts >= self.max_attempts:
            raise PatientLoginError(f"Слишком много попыток. Попробуйте через {self.lock_seconds // 60} минут "
                                    "или позвоните в регистратуру.")
        patient = (Patient.objects.filter(Q(external_mis_id__iexact=card) | Q(external_mis_id__iexact=typed), is_anonymous=False)
                   .select_related("user").first())
        if patient is None or patient.user is None or not patient.user.is_active or not patient.user.check_password(password):
            cache.set(key, attempts + 1, self.lock_seconds)
            # Одинаковый ответ для «нет такой карты» и «неверный пароль»: нельзя перебором узнать номера карт.
            raise PatientLoginError("Неверный номер карты или пароль")
        cache.delete(key)
        return patient

    @staticmethod
    def login(request, patient: Patient) -> None:
        request.session.cycle_key()  # новый идентификатор сессии после входа
        request.session[SESSION_KEY] = str(patient.id)

    @staticmethod
    def logout(request) -> None:
        request.session.pop(SESSION_KEY, None)
        request.session.cycle_key()

    @staticmethod
    def current(request) -> str:
        return request.session.get(SESSION_KEY, "")

    @staticmethod
    def set_password(patient: Patient, password: str) -> None:
        """Учётная запись пациента для входа (в прототипе её создаёт seed_demo для демо-пациентов)."""
        User = get_user_model()
        user = patient.user or User(username=f"patient-{patient.external_mis_id}".lower()[:150])
        user.set_password(password)
        user.is_staff = False
        user.save()
        if patient.user_id != user.id:
            patient.user = user
            patient.save(update_fields=["user", "updated_at"])


def patient_required(view):
    """Страница кабинета открывается только вошедшему пациенту и только его собственная.
    Не вошёл — на вход (с возвратом по ссылке из уведомления); чужой кабинет — свой кабинет."""

    @wraps(view)
    def wrapper(request, pk, *args, **kwargs):
        current = PatientAuthService.current(request)
        if not current:
            return redirect(f"/patient/?next={quote(request.get_full_path())}")
        if current != str(pk):
            return redirect("patients:cabinet", pk=current)
        return view(request, pk, *args, **kwargs)

    return wrapper
