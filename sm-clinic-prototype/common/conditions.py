"""
Проверка условий по атрибутам находки — общий «язык правил» для всех матриц
(матрица маршрутизации, пороги внимания, матрица показаний).

Формат условия: {"attr": "birads", "op": "gte", "value": 3}.
Условие с отсутствующим атрибутом НЕ выполняется: правило не срабатывает «на догадке».
"""
import operator

OPS = {
    "gte": operator.ge, "gt": operator.gt, "lte": operator.le, "lt": operator.lt, "eq": operator.eq,
    "in": lambda a, b: a in b,
}
OP_TITLES = {"gte": "≥", "gt": ">", "lte": "≤", "lt": "<", "eq": "=", "in": "∈"}


def condition_met(cond: dict, attributes: dict) -> bool:
    value = (attributes or {}).get(cond["attr"])
    if value is None:
        return False
    try:
        left = value if cond["op"] in ("in", "eq") and not isinstance(value, (int, float)) else float(value)
        return bool(OPS[cond["op"]](left, cond["value"]))
    except (TypeError, ValueError, KeyError):
        return False


def conditions_met(conditions: list[dict] | None, attributes: dict) -> bool:
    return all(condition_met(c, attributes) for c in conditions or [])


def describe(conditions: list[dict] | None) -> str:
    """Условие словами для экрана: «размер ≥ 10 мм», а не «size_mm ≥ 10» (подписи — в common/labels.py)."""
    from .labels import conditions_text

    return conditions_text(conditions) if conditions else "без условий"
