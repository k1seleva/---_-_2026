"""
Уверенность распознавания (confidence) для маркеров, находок и триггеров.

Уверенность отвечает на вопрос «насколько надёжно распознан текст», а не «насколько находка важна»:
ранжирования по важности в системе нет. Оценка прозрачна: базовая надёжность правила плюс поправки,
каждая поправка сохраняется с причиной и показывается в подсказке.
"""

# Базовая надёжность вида правила.
BASE = {
    "dictionary": 0.90,     # шаблон словаря находок, утверждённый экспертом
    "scale": 0.95,          # шкала риска с числом (BI-RADS 4)
    "lexicon": 0.75,        # признак лексикона (образование, киста, конкременты)
    "lexicon_minor": 0.65,  # описательный признак (неоднородность, утолщение)
    "attribute": 0.90,      # число с единицей измерения, сторона
    "cue": 0.80,            # слова сомнения и отрицания
    "attention": 0.90,      # порог внимания (AttentionRule) по числу из текста
    "llm": 0.70,            # подсветка ИИ-агента с дословной цитатой
}

# Поправки: (код, изменение, причина для человека).
UNCERTAIN = ("uncertain", -0.25, "В той же фразе есть сомнение («?», «нельзя исключить», «по типу»)")
CORRECTED = ("corrected", -0.10, "Совпало только после исправления опечатки или раскладки")
IN_CONCLUSION = ("conclusion", 0.05, "Пункт заключения")
SHORT = ("short", -0.05, "Короткое совпадение: до трёх букв")
LLM_CONFIRMED = ("llm", 0.05, "Подтверждено ИИ-агентом с дословной цитатой")
LIST_NEGATION = ("list_negation", -0.10, "Отрицание общее для перечня, а не для этого слова")
CONFIRMED_ELSEWHERE = ("elsewhere", 0.03, "Та же находка есть и в описании, и в заключении")
HEURISTIC = ("heuristic", -0.10, "Вывод эвристики, а не правила словаря")

MIN_CONFIDENCE, MAX_CONFIDENCE = 0.05, 0.99


def score(base_kind: str, factors: list[tuple[str, float, str]]) -> tuple[float, list[dict]]:
    """Итоговая уверенность и расшифровка: [{"code", "delta", "reason"}]."""
    value = BASE[base_kind] + sum(delta for _code, delta, _reason in factors)
    value = round(min(MAX_CONFIDENCE, max(MIN_CONFIDENCE, value)), 2)
    details = [{"code": "base", "delta": BASE[base_kind], "reason": f"Базовая надёжность правила ({BASE_TITLES[base_kind]})"}]
    details += [{"code": code, "delta": delta, "reason": reason} for code, delta, reason in factors]
    return value, details


def adjust(value: float, details: list[dict], factor: tuple[str, float, str]) -> tuple[float, list[dict]]:
    """Добавить поправку к уже посчитанной уверенности (например, на уровне триггера)."""
    code, delta, reason = factor
    new = round(min(MAX_CONFIDENCE, max(MIN_CONFIDENCE, value + delta)), 2)
    return new, [*details, {"code": code, "delta": delta, "reason": reason}]


def level(value: float) -> str:
    """Словесная шкала для интерфейса."""
    if value >= 0.85:
        return "high"
    if value >= 0.6:
        return "medium"
    return "low"


BASE_TITLES = {
    "dictionary": "словарь находок", "scale": "шкала риска", "lexicon": "лексикон изменений",
    "lexicon_minor": "описательный признак", "attribute": "число или сторона", "cue": "слово-маркер",
    "attention": "порог внимания", "llm": "ИИ-агент",
}
LEVEL_TITLES = {"high": "высокая", "medium": "средняя", "low": "низкая"}
