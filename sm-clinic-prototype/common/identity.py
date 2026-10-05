"""Кто работает в интерфейсе: вошедший сотрудник (gateway/staff_auth.py, роль из групп Django).
В пилоте — учётная запись СМ-Клиники (SSO)."""


def actor(request) -> str:
    """Имя для журналов решений (кто снял с проверки, кто привязал пациента)."""
    return (request.user.get_username() if request.user.is_authenticated else "") or "coordinator"


def focus_owner(request) -> str:
    """Чьи приоритеты признаков показывать: у каждого врача и координатора свои."""
    return request.session.get("focus_owner") or actor(request)
