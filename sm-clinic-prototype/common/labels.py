"""
Человекочитаемые подписи вместо технических ключей (правило UI: ни один `snake_case` не попадает на экран).

Как устроено:
- каждый модуль регистрирует свои справочники в AppConfig.ready(): перечисления моделей
  (register_choices), статические словари (register) и функции-резолверы для данных из БД
  (register_resolver: код находки -> «Желчнокаменная болезнь», код клиники -> «ВДНХ»);
- шаблоны выводят значение фильтром: {{ route.status|human:"route_status" }};
- неизвестный ключ не показывается как есть: подчёркивания убираются, первая буква заглавная,
  а сам ключ пишется в лог, чтобы добавить перевод (тест tests/test_ui_texts.py ловит такие места).

Модуль не импортирует приложения: зависимости направлены от модулей к ядру.
"""
import logging
import re

logger = logging.getLogger(__name__)

_STATIC: dict[str, dict[str, str]] = {}
_RESOLVERS: dict[str, list] = {}
SNAKE_RE = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")

# Общие справочники ядра: атрибуты находок и операторы условий (язык правил common.conditions).
ATTRIBUTES = {
    "size_mm": "Размер, мм", "dimensions_mm": "Размеры, мм", "percent": "Процент", "count": "Количество",
    "side": "Сторона", "birads": "BI-RADS", "tirads": "TI-RADS", "orads": "O-RADS", "specialties": "Специальности",
    "volume_ml": "Объём, мл", "thickness_mm": "Толщина, мм", "degree": "Степень",
}
OPERATORS = {"gte": "не меньше", "gt": "больше", "lte": "не больше", "lt": "меньше", "eq": "равно", "in": "одно из"}
SIDES = {"right": "справа", "left": "слева", "both": "с обеих сторон"}


def register(namespace: str, mapping: dict[str, str]) -> None:
    _STATIC.setdefault(namespace, {}).update({str(k): str(v) for k, v in mapping.items()})


def register_choices(namespace: str, choices) -> None:
    """Перечисление Django (TextChoices) или список пар (значение, подпись)."""
    pairs = choices.choices if hasattr(choices, "choices") else choices
    register(namespace, dict(pairs))


def register_resolver(namespace: str, resolver) -> None:
    """resolver(keys: set[str]) -> {key: title}; вызывается лениво (данные в БД)."""
    _RESOLVERS.setdefault(namespace, []).append(resolver)


def humanize(key: str) -> str:
    text = str(key).replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def title(value, namespace: str = "") -> str:
    """Подпись для ключа. Без пространства имён ищет во всех статических справочниках."""
    if value is None or value == "":
        return ""
    key = str(value)
    if namespace:
        if key in _STATIC.get(namespace, {}):
            return _STATIC[namespace][key]
        for resolver in _RESOLVERS.get(namespace, []):
            try:
                found = resolver({key}).get(key)
            except Exception:  # noqa: BLE001 — БД может быть не готова (миграции)
                found = None
            if found:
                return found
    else:
        for mapping in _STATIC.values():
            if key in mapping:
                return mapping[key]
    if SNAKE_RE.fullmatch(key) or key.islower() and key.isascii() and key.isalpha():
        logger.warning("Нет перевода для ключа %r (%s)", key, namespace or "без пространства имён")
        return humanize(key)
    return key


def condition_text(cond: dict) -> str:
    attr = title(cond.get("attr", ""), "attr")
    value = cond.get("value")
    if isinstance(value, (list, tuple)):
        value = ", ".join(map(str, value))
    return f"{attr} {OPERATORS.get(cond.get('op'), cond.get('op'))} {value}"


def conditions_text(conditions) -> str:
    return "; ".join(condition_text(c) for c in conditions or []) or "без дополнительных условий"


def has_raw_keys(text: str) -> list[str]:
    """Технические ключи в видимом тексте (для тестов локализации)."""
    return SNAKE_RE.findall(text)


register("attr", ATTRIBUTES)
register("operator", OPERATORS)
register("side", SIDES)
register("sex", {"F": "Женский", "M": "Мужской"})
register("section", {"header": "Шапка", "description": "Описание", "conclusion": "Заключение",
                     "recommendation": "Рекомендации", "tail": "После заключения"})
register("segment_kind", {
    "finding": "Находка из словаря", "scale": "Шкала риска", "abnormal": "Изменение", "conclusion_item": "Пункт заключения",
    "norm": "Норма", "measurement": "Параметр", "recommendation": "Рекомендация", "meta": "Шапка",
    "technical": "Технические сведения", "disclaimer": "Примечание", "heading": "Заголовок", "unclassified": "Не классифицировано",
})
register("engine", {"rules": "Правила (словарь находок)", "llm": "ИИ-агент", "hybrid": "Правила и ИИ-агент"})
