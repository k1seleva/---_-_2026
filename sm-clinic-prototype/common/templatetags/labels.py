"""Фильтры шаблонов для человекочитаемых подписей. Подключены как builtins (config/settings.py)."""
from django import template

from common import labels

register = template.Library()


@register.filter
def human(value, namespace: str = ""):
    """{{ status|human:"route_status" }} -> «Пациент уведомлён»."""
    return labels.title(value, namespace)


@register.filter
def human_list(values, namespace: str = ""):
    return ", ".join(labels.title(v, namespace) for v in values or [])


@register.filter
def conditions_text(conditions):
    return labels.conditions_text(conditions)


@register.filter
def get_item(mapping, key):
    return (mapping or {}).get(key)


@register.filter
def percent(part, total):
    try:
        return round(100 * float(part) / float(total)) if float(total) else 0
    except (TypeError, ValueError):
        return 0
