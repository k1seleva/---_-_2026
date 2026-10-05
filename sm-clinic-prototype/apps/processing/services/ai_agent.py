"""
Интеграция с AI-агентом (LangChain).

Ключевой принцип (рекомендация кейса, п. 9): ИЗВЛЕЧЕНИЕ ФАКТОВ отделено от РЕШЕНИЯ О МАРШРУТЕ.
Агент только находит факты в тексте и приводит цитату-доказательство. Какой маршрут
запускать, решает детерминированная матрица в модуле маршрутизации.

Иерархия (SOLID):
* FindingExtractor            — абстракция (DIP: сервис обработки зависит только от неё);
* RuleBasedFindingExtractor   — словари + регулярные выражения + отрицания (работает офлайн);
* LangChainFindingExtractor   — LLM со structured output (pydantic-схема);
* HybridFindingExtractor      — объединяет оба: правила гарантируют полноту по словарю,
                                LLM добавляет сложные формулировки; при сбое LLM — фолбэк.
                                У каждой находки источник: rules (словарь), llm (только ИИ), both.
"""
import logging
import os
import re
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

from django.conf import settings

from . import scoring
from .dictionary import Dictionary
from .grounding import GroundingGuard, normalize
from .normalization import finditer, normalize_text, search
from .schemas import ExtractedFinding, ExtractedRecommendation, ExtractionPayload

logger = logging.getLogger(__name__)


class FindingExtractor(ABC):
    """Контракт AI-агента: текст протокола -> структурированный JSON (ExtractionPayload)."""

    name: str = "abstract"

    @abstractmethod
    def extract(self, text: str, *, study_type: str = "", dictionary: Dictionary) -> ExtractionPayload: ...


# --------------------------------------------------------------------------------------
# Общие утилиты разбора текста
# --------------------------------------------------------------------------------------
# Заголовок раздела «Заключение» — в начале строки, с двоеточием или отдельной строкой
# (не путать с дисклеймером «Данное заключение не является диагнозом»).
CONCLUSION_HEADER_RE = re.compile(
    r"^[ \t]*[\"«*]?(?:ЗАКЛЮЧЕНИЕ|Заключение)(?:[ \t]+(?:ИССЛЕДОВАНИЯ|исследования))?[ \t]*(?P<punct>[:.]?)", re.MULTILINE
)
DISCLAIMER_RE = re.compile(
    r"(Данн(ое|ые)\s+(заключение|ультразвук\w*|исследовани\w*)|Уважаемые пациенты|Результаты УЗИ не являются|"
    r"Заключение узи не является|не\s+явля\w+\s+(клиническим\s+)?диагноз\w*)",
    re.IGNORECASE,
)
# Глаголы отрицания «не …»: основы без окончаний («не выявлено», «не выявляется»), с частыми
# опечатками протоколов («не вуизуализируется», «не регестрируется»).
NEGATION_VERBS = r"(выявл|определя|лоцир|в\w?изуализ|обнаруж|рег[еи]ст|прослежива)\w*"
# Признаки отрицания перед находкой в пределах фразы.
NEGATION_BEFORE = re.compile(
    rf"(\bне\s+{NEGATION_VERBS}|\bнет\b|\bбез\s+(признаков|данных)|данных\s+за|отсутств\w*|исключ[её]н\w*)",
    re.IGNORECASE,
)
# Отрицание сразу после находки: «Объёмные образования: не лоцируются», «… с учетом ЦДК: не».
NEGATION_AFTER = re.compile(rf"^[^\w\n]{{0,3}}\s*(не\s+{NEGATION_VERBS}|нет\b|отсутств|не\s*[.;]?\s*$)", re.IGNORECASE)
UNCERTAIN_RE = re.compile(r"(\?|нельзя\s+исключить|по\s+типу|может\s+соответствовать|вероятно)", re.IGNORECASE)

SCALE_PATTERNS = {
    # шкалы стратификации риска -> атрибуты находки
    "birads": re.compile(r"\b(?:BI-?RADS|Birads|Br)\s*[-:]?\s*((?:(?:категори\w*|справа|слева|правая|левая|[,\s(])*\d\b[^\n]{0,4})+)", re.IGNORECASE),
    "tirads": re.compile(r"\b(?:EU-?)?T[IІ]-?RADS\s*[-:]?\s*((?:(?:справа|слева|[,\s])*\d\b[^\n]{0,4})+)", re.IGNORECASE),
    "orads": re.compile(r"\bO-?RADS\s*[-:]?\s*((?:(?:справа|слева|[,\s])*(?:\d|IV|V|I{1,3})\b[^\n]{0,3})+)", re.IGNORECASE),
}
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}
SCALE_TITLES = {"birads": "Категория BI-RADS", "tirads": "Категория TI-RADS", "orads": "Категория O-RADS"}

INTERVAL_RE = re.compile(
    r"(?:через|в\s+динамике\s+через|контроль\s+через)\s*(\d+)\s*(дн|нед|мес|год|лет)", re.IGNORECASE
)
INTERVAL_DAYS = {"дн": 1, "нед": 7, "мес": 30, "год": 365, "лет": 365}
RECOMMENDATION_RE = re.compile(r"(Рекомендован\w*|Рекомендации|РЕКОМЕНДОВАНО|Рек-но|Рекомендовнао)\s*[:.]?\s*(.+)", re.IGNORECASE)
DIAGNOSTICS_RE = re.compile(r"(УЗИ|МРТ|КТ|маммограф\w*|МРХПГ|биопси\w*|ТАБ|пункци\w*|гистероскопи\w*)", re.IGNORECASE)


def find_conclusion_header(text: str) -> re.Match | None:
    """Заголовок заключения. В части выгрузок 1С над описанием стоит пустое поле «Заключение»
    (отдельной строкой без двоеточия), а настоящее заключение — в конце. Поэтому «голые»
    заголовки пропускаем, если ниже есть ещё один."""
    candidates = []
    for m in CONCLUSION_HEADER_RE.finditer(text):
        line_end = text.find("\n", m.end())
        rest = text[m.end(): line_end if line_end != -1 else len(text)]
        if m.group("punct") or not rest.strip():
            candidates.append(m)
    if not candidates:
        return None
    meaningful = [m for m in candidates[:-1] if m.group("punct")] + [candidates[-1]]
    return meaningful[0]


def split_conclusion(text: str) -> tuple[str, int]:
    """Возвращает текст заключения и его смещение в исходном тексте."""
    header = find_conclusion_header(text)
    if header is None:
        return text, 0
    start = header.end()
    tail = text[start:]
    if stop := DISCLAIMER_RE.search(tail):
        tail = tail[: stop.start()]
    lead = len(tail) - len(tail.lstrip())
    return tail.strip(), start + lead


def sentence_bounds(text: str, pos: int) -> tuple[int, int]:
    left = max(text.rfind(ch, 0, pos) for ch in ".;\n")
    rights = [i for i in (text.find(ch, pos) for ch in ".;\n") if i != -1]
    return left + 1, (min(rights) if rights else len(text))


def scale_values(raw: str) -> list[int]:
    """Категории из хвоста совпадения шкалы: «4», «справа 3, слева 2», «IV»."""
    values = []
    for token in re.findall(r"\b(\d|iv|v|i{1,3})\b", raw, re.IGNORECASE):
        value = int(token) if token.isdigit() else ROMAN[token.upper()]
        if 0 <= value <= 6:
            values.append(value)
    return values


def extract_scales(text: str) -> dict[str, int]:
    """BI-RADS / TI-RADS / O-RADS: берём максимальную категорию (худшая сторона).
    Поиск по нормализованному тексту: «Тi-rads» с кириллической «Т» тоже распознаётся."""
    norm = normalize_text(text)
    found: dict[str, int] = {}
    for scale, pattern in SCALE_PATTERNS.items():
        values: list[int] = []
        for _start, _end, m, _corrected in finditer(pattern, norm):
            values += scale_values(m.group(1))
        if values:
            found[scale] = max(values)
    return found


# --------------------------------------------------------------------------------------
# 1. Правила
# --------------------------------------------------------------------------------------
class RuleBasedFindingExtractor(FindingExtractor):
    name = "rules"

    def extract(self, text: str, *, study_type: str = "", dictionary: Dictionary) -> ExtractionPayload:
        conclusion, offset = split_conclusion(text)
        # Шаблоны ищутся по нормализованному тексту (регистр, «ё», пробелы, раскладка, опечатки),
        # позиции и цитаты берутся из исходного.
        norm = normalize_text(conclusion)
        findings: list[ExtractedFinding] = []

        for definition in dictionary.findings:
            if definition.study_types and study_type and not any(
                s.lower() in study_type.lower() for s in definition.study_types
            ):
                continue
            hits: list[ExtractedFinding] = []
            seen: set[tuple[int, int]] = set()
            for index, pattern in enumerate(definition.patterns):
                for m_start, m_end, _m, corrected in finditer(pattern, norm):
                    s_start, s_end = sentence_bounds(conclusion, m_start)
                    if (s_start, s_end) in seen:
                        continue  # два шаблона одной находки в одной фразе — одно упоминание
                    sentence = conclusion[s_start:s_end].strip()
                    if any(search(ex, normalize_text(sentence)) for ex in definition.exclude):
                        continue
                    seen.add((s_start, s_end))
                    before = conclusion[s_start: m_start]
                    after = conclusion[m_end: m_end + 30]
                    negated = bool(NEGATION_BEFORE.search(before) or NEGATION_AFTER.search(after))
                    uncertain = bool(UNCERTAIN_RE.search(sentence))
                    factors = [scoring.IN_CONCLUSION]
                    if uncertain:
                        factors.append(scoring.UNCERTAIN)
                    if corrected:
                        factors.append(scoring.CORRECTED)
                    confidence, _details = scoring.score("dictionary", factors)
                    hits.append(ExtractedFinding(
                        code=definition.code, label=definition.title, evidence_quote=sentence,
                        negated=negated, uncertain=uncertain, attributes=self._attributes(sentence),
                        confidence=confidence, severity=definition.severity,
                        span_start=offset + s_start, span_end=offset + s_end, rule_id=definition.rule_id(index),
                    ))
            if not hits:
                continue
            # Одна находка на код: приоритет — упоминание без отрицания; числовые атрибуты — максимум
            # по всем положительным упоминаниям (например, наибольший % стеноза).
            positives = [h for h in hits if not h.negated]
            chosen = (positives or hits)[0]
            for h in positives[1:]:
                for key, value in h.attributes.items():
                    if isinstance(value, (int, float)) and value > chosen.attributes.get(key, 0):
                        chosen.attributes[key] = value
            findings.append(chosen)

        # Шкалы риска — отдельные факты; пороги (BI-RADS >= 3 и т.п.) решает матрица маршрутизации.
        for scale, value in extract_scales(conclusion).items():
            quote = next(finditer(SCALE_PATTERNS[scale], norm), None)
            q_start, q_end = sentence_bounds(conclusion, quote[0]) if quote else (0, 0)
            factors = [scoring.IN_CONCLUSION] + ([scoring.CORRECTED] if quote and quote[3] else [])
            findings.append(
                ExtractedFinding(
                    code=f"{scale}_category",
                    label=f"{SCALE_TITLES[scale]} {value}",
                    evidence_quote=conclusion[q_start:q_end].strip(),
                    attributes={scale: value},
                    confidence=scoring.score("scale", factors)[0],
                    span_start=offset + q_start if quote else None,
                    span_end=offset + q_end if quote else None,
                    rule_id=f"scale:{scale}",
                )
            )

        return ExtractionPayload(
            study_type=study_type,
            conclusion=conclusion,
            findings=findings,
            recommendations=self._recommendations(text, dictionary),
            summary_for_patient=build_patient_summary(findings),
            engine=self.name,
            dictionary_version=dictionary.version,
            engines={"rules": {"status": "ok", "found": len(findings)}},
        )

    @staticmethod
    def _attributes(sentence: str) -> dict[str, Any]:
        """Числовые атрибуты из фразы: размер (мм), процент (стеноз), сторона."""
        sizes = [float(x.replace(",", ".")) for x in re.findall(r"(\d+(?:[.,]\d+)?)\s*(?:х|x|\*)?\s*\d*\s*мм", sentence)]
        percents = [int(x) for x in re.findall(r"(\d{1,3})\s*%", sentence) if int(x) <= 100]
        attrs: dict[str, Any] = {}
        if sizes:
            attrs["size_mm"] = max(sizes)
        if percents:
            attrs["percent"] = max(percents)
        if re.search(r"справа|правой|правого", sentence, re.IGNORECASE):
            attrs["side"] = "right"
        elif re.search(r"слева|левой|левого", sentence, re.IGNORECASE):
            attrs["side"] = "left"
        return attrs

    @staticmethod
    def _recommendations(text: str, dictionary: Dictionary) -> list[ExtractedRecommendation]:
        result: list[ExtractedRecommendation] = []
        for m in RECOMMENDATION_RE.finditer(text):
            body = m.group(2)
            if stop := DISCLAIMER_RE.search(body):
                body = body[: stop.start()]
            # «консультация маммолога и УЗИ через 6 мес» -> две рекомендации
            for part in re.split(r"[,;]|\s+и\s+|\.\s", body):
                part = part.strip(" .")
                if not part:
                    continue
                specialty = next((code for rx, code in dictionary.specialties if rx.search(part)), None)
                interval = INTERVAL_RE.search(part)
                diagnostics = DIAGNOSTICS_RE.search(part)
                if not (specialty or diagnostics or interval):
                    continue
                kind = "diagnostics" if diagnostics and not specialty else (
                    "observation" if re.search(r"наблюдени", part, re.IGNORECASE) else "consultation")
                result.append(
                    ExtractedRecommendation(
                        text=part,
                        kind=kind,
                        specialty_code=specialty,
                        service=diagnostics.group(0) if diagnostics else None,
                        interval_days=int(interval.group(1)) * INTERVAL_DAYS[interval.group(2).lower()]
                        if interval else None,
                    )
                )
        return result


def build_patient_summary(findings: list[ExtractedFinding]) -> str:
    """Нейтральная выжимка для пациента. Без диагноза и без пугающих формулировок
    (требование безопасности, п. 17 кейса)."""
    if any(not f.negated and f.code not in {"birads_category", "tirads_category", "orads_category"} for f in findings):
        return (
            "В исследовании описаны изменения, по которым рекомендуется консультация профильного "
            "специалиста для определения дальнейшей тактики. Заключение исследования не является диагнозом."
        )
    return "Результат исследования готов. Интерпретацию результата проводит лечащий врач."


# --------------------------------------------------------------------------------------
# 2. LangChain
# --------------------------------------------------------------------------------------
SYSTEM_PROMPT = """Ты — ассистент по извлечению фактов из протоколов ультразвуковых исследований.
Ты НЕ ставишь диагноз и НЕ назначаешь лечение. Твоя задача — найти в тексте находки и рекомендации.

Правила (ответ проверяется программно, всё непроверяемое отбрасывается):
1. Работай ТОЛЬКО с текстом протокола. Не добавляй находок, чисел и рекомендаций, которых в нём нет,
   и не пропускай находки, которые в нём есть (включая упомянутые с отрицанием).
2. Используй только коды находок из справочника ниже; если подходящего нет — code="OTHER".
3. Для каждой находки приведи ДОСЛОВНУЮ цитату из протокола (evidence_quote), без пересказа.
4. Если находка упомянута с отрицанием («не выявлено», «данных за ... нет») — negated=true.
5. Если есть сомнение («?», «нельзя исключить», «по типу») — uncertain=true.
6. Шкалы BI-RADS, TI-RADS, O-RADS записывай в attributes (birads/tirads/orads) числом, по худшей стороне.
7. Размеры — в attributes.size_mm (максимальный размер в мм) — только число, которое есть в цитате.
8. Рекомендации разбей на отдельные пункты; text — дословно из протокола; интервал переведи в дни (6 мес = 180).
9. summary_for_patient оставь пустым: текст для пациента формируется по утверждённому шаблону.

Справочник находок:
{finding_codes}

Коды специальностей: {specialty_codes}"""

HUMAN_PROMPT = """Тип исследования: {study_type}

Текст протокола:
<<<
{text}
>>>"""


def build_chat_model(config: dict | None = None):
    """Фабрика LLM. Провайдер задаётся настройкой — код сервиса не меняется (OCP).

    Для реальных медицинских данных — только модель в контуре клиники
    (LLM_PROVIDER=ollama / vLLM через OpenAI-совместимый BASE_URL / GigaChat on-prem).
    """
    cfg = {**settings.AI_AGENT, **(config or {})}
    provider = cfg["PROVIDER"]
    if provider in ("", "none"):
        return None
    if provider == "gigachat":
        from langchain_gigachat import GigaChat  # pip install langchain-gigachat

        return GigaChat(model=cfg["MODEL"] or "GigaChat-Pro", temperature=cfg["TEMPERATURE"], timeout=cfg["TIMEOUT_SEC"])
    from langchain.chat_models import init_chat_model

    kwargs: dict[str, Any] = {"temperature": cfg["TEMPERATURE"]}
    if provider == "ollama":
        # Qwen в Ollama: окно контекста по умолчанию 2–4 тыс. токенов, длинный протокол вместе с инструкцией
        # и справочником молча обрезается. Поэтому num_ctx задаётся явно. Тайм-аут — через клиент httpx
        # (параметр timeout модель Ollama не принимает и тихо игнорирует).
        kwargs["num_ctx"] = cfg.get("NUM_CTX") or 8192
        kwargs["client_kwargs"] = {"timeout": cfg["TIMEOUT_SEC"]}
        if cfg.get("NUM_PREDICT"):
            kwargs["num_predict"] = cfg["NUM_PREDICT"]
        if cfg.get("KEEP_ALIVE"):
            kwargs["keep_alive"] = cfg["KEEP_ALIVE"]  # модель не выгружается между протоколами пачки
        if cfg.get("REASONING") is not None:
            kwargs["reasoning"] = cfg["REASONING"]  # Qwen3: false — без «размышлений», ответ быстрее
    else:
        kwargs["timeout"] = cfg["TIMEOUT_SEC"]
        if cfg.get("NUM_PREDICT"):
            kwargs["max_tokens"] = cfg["NUM_PREDICT"]
    if cfg.get("BASE_URL"):
        kwargs["base_url"] = cfg["BASE_URL"]
        if provider == "openai" and not os.getenv("OPENAI_API_KEY"):
            kwargs["api_key"] = "local"  # LM Studio и vLLM в контуре клиники ключ не проверяют
    return init_chat_model(cfg["MODEL"], model_provider=provider, **kwargs)


class LlmUnavailable(RuntimeError):
    """Модель сейчас не спрашиваем: недавно не ответила (тайм-аут или нет связи)."""


class LlmCircuit:
    """Предохранитель для пачки: после тайм-аута или обрыва связи модель на COOLDOWN_SEC не вызывается,
    иначе каждый протокол пачки ждал бы её по три минуты на каждый запрос. Первый запрос после паузы —
    пробный: ответила — работаем дальше, нет — новая пауза. Состояние — в памяти процесса (поток очереди
    или Celery-воркер очереди llm)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.until = 0.0
        self.since = 0.0
        self.reason = ""

    def blocked(self) -> bool:
        with self._lock:
            return time.monotonic() < self.until

    def open_for(self) -> float:
        """Сколько секунд модель подряд не отвечает (0 — отвечает)."""
        with self._lock:
            return time.monotonic() - self.since if self.since else 0.0

    def trip(self, reason: str) -> None:
        with self._lock:
            now = time.monotonic()
            self.since = self.since or now
            self.until = now + settings.AI_AGENT.get("COOLDOWN_SEC", 60)
            self.reason = reason

    def reset(self) -> None:
        with self._lock:
            self.until = self.since = 0.0
            self.reason = ""


_circuits: dict[str, LlmCircuit] = {}
_circuits_lock = threading.Lock()


def circuit_for(config: dict | None = None) -> LlmCircuit:
    """Свой предохранитель у каждого сервера и модели: зависший сервер советов не останавливает разметку."""
    cfg = {**settings.AI_AGENT, **(config or {})}
    key = f"{cfg.get('PROVIDER')}|{cfg.get('BASE_URL')}|{cfg.get('MODEL')}"
    with _circuits_lock:
        return _circuits.setdefault(key, LlmCircuit())


def reset_circuits() -> None:
    with _circuits_lock:
        _circuits.clear()

UNAVAILABLE_MARKERS = ("connect", "refused", "timeout", "timed out", "name or service not known", "unreachable",
                       "remote end closed", "server disconnected", "502", "503")


def is_unavailable(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in UNAVAILABLE_MARKERS)


def call_llm(invoke, config: dict | None = None):
    """Вызов модели через предохранитель. Пауза — сразу LlmUnavailable (протокол не ждёт тайм-аут)."""
    circuit = circuit_for(config)
    if circuit.blocked():
        raise LlmUnavailable(f"пропущено: модель недавно не ответила ({circuit.reason}); "
                             "повторите разбор, когда она освободится")
    try:
        result = invoke()
    except Exception as exc:
        if is_unavailable(exc):
            circuit.trip(describe_llm_error(exc, config))
        raise
    circuit.reset()
    return result


def describe_llm_error(exc: Exception, config: dict | None = None) -> str:
    """Причина сбоя модели словами, понятными координатору и администратору (подробности — в журнале)."""
    if isinstance(exc, LlmUnavailable):
        return str(exc)[:300]
    cfg = {**settings.AI_AGENT, **(config or {})}
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    if "connect" in lowered or "refused" in lowered or "name or service not known" in lowered:
        reason = (f"нет связи с сервером модели ({cfg.get('BASE_URL') or 'адрес по умолчанию'}): "
                  "проверьте, что Ollama запущена и адрес LLM_BASE_URL верный")
    elif "timeout" in lowered or "timed out" in lowered:
        reason = (f"модель не ответила за {cfg.get('TIMEOUT_SEC')} с: увеличьте LLM_TIMEOUT_SEC "
                  "или возьмите модель поменьше")
    elif "not found" in lowered and "model" in lowered:
        reason = f"модель «{cfg.get('MODEL')}» не найдена на сервере: проверьте имя в `ollama list`"
    else:
        return str(exc)[:300] or type(exc).__name__
    return f"{reason} ({str(exc)[:120]})"


def model_label(config: dict | None = None) -> str:
    """«qwen2.5:14b (ollama)» — для экрана и журналов."""
    cfg = {**settings.AI_AGENT, **(config or {})}
    if cfg["PROVIDER"] in ("", "none"):
        return ""
    return f"{cfg['MODEL'] or 'модель по умолчанию'} ({cfg['PROVIDER']})"


class LangChainFindingExtractor(FindingExtractor):
    name = "llm"

    def __init__(self, llm=None, prompt_version: str | None = None) -> None:
        self.llm = llm if llm is not None else build_chat_model()
        if self.llm is None:
            raise RuntimeError("LLM не настроен (LLM_PROVIDER=none)")
        self.prompt_version = prompt_version or settings.AI_AGENT["PROMPT_VERSION"]

    def _chain(self):
        from langchain_core.prompts import ChatPromptTemplate

        prompt = ChatPromptTemplate.from_messages([("system", SYSTEM_PROMPT), ("human", HUMAN_PROMPT)])
        # with_structured_output: LLM обязан вернуть объект, валидный по pydantic-схеме.
        return prompt | self.llm.with_structured_output(ExtractionPayload)

    def extract(self, text: str, *, study_type: str = "", dictionary: Dictionary) -> ExtractionPayload:
        started = time.monotonic()
        payload: ExtractionPayload = call_llm(lambda: self._chain().invoke(
            {
                "finding_codes": dictionary.finding_codes_for_prompt(),
                "specialty_codes": dictionary.specialty_codes_for_prompt(),
                "study_type": study_type or "не указан",
                "text": text,
            }
        ))
        proposed = len(payload.findings)
        payload = self.ground(payload, text, dictionary)
        payload.engine = f"{self.name}:{settings.AI_AGENT['PROVIDER']}"
        payload.dictionary_version = dictionary.version
        payload.engines = {"llm": {"status": "ok", "model": model_label(), "ms": round((time.monotonic() - started) * 1000),
                                   "proposed": proposed, "found": len(payload.findings)}}
        return payload

    @staticmethod
    def ground(payload: ExtractionPayload, text: str, dictionary: Dictionary) -> ExtractionPayload:
        """Защита от галлюцинаций и пропусков (см. grounding.py): находка без дословной цитаты,
        код вне словаря, число, которого нет в цитате, — отбрасываются; текст для пациента —
        только утверждённый шаблон."""
        guard = GroundingGuard({f.code for f in dictionary.findings}, {code for _, code in dictionary.specialties})
        payload.findings = guard.verify_findings(payload.findings, text)
        for f in payload.findings:
            f.rule_id = f"llm:{settings.AI_AGENT['PROMPT_VERSION']}"
            f.source = "llm"
        payload.recommendations = guard.verify_recommendations(payload.recommendations, text)
        for item in guard.report.rejected:
            logger.warning("Ответ AI-агента отброшен: %s %s — %s", item["what"], item["ref"], item["reason"])
        payload.summary_for_patient = build_patient_summary(payload.findings)
        payload.grounding = guard.report.as_dict()
        return payload


# --------------------------------------------------------------------------------------
# 3. Гибрид
# --------------------------------------------------------------------------------------
class HybridFindingExtractor(FindingExtractor):
    name = "hybrid"

    def __init__(self, primary: FindingExtractor, fallback: FindingExtractor) -> None:
        self.primary, self.fallback = primary, fallback

    def extract(self, text: str, *, study_type: str = "", dictionary: Dictionary) -> ExtractionPayload:
        base = self.fallback.extract(text, study_type=study_type, dictionary=dictionary)
        started = time.monotonic()
        try:
            llm = self.primary.extract(text, study_type=study_type, dictionary=dictionary)
        except Exception as exc:
            # Сбой модели не теряет протокол: разбор по словарю, а причина видна на странице протокола.
            logger.exception("AI-агент недоступен, используем только правила")
            base.engines["llm"] = {"status": "error", "model": model_label(), "error": describe_llm_error(exc),
                                   "ms": round((time.monotonic() - started) * 1000)}
            return base
        # Объединяем: правило + LLM. При расхождении в отрицании доверяем более осторожному (не триггер
        # снимаем только если обе стороны считают отрицанием). Источник находки: both — словарь и ИИ
        # нашли одно и то же (с одинаковым отрицанием), llm — только ИИ (с дословной цитатой).
        merged = {f.code: f for f in base.findings}
        for f in llm.findings:
            if f.code in merged:
                rule = merged[f.code]
                if rule.negated == f.negated:
                    rule.source = "both"
                rule.negated = rule.negated and f.negated
                rule.attributes = {**f.attributes, **rule.attributes}
                rule.confidence = max(rule.confidence, f.confidence)
            else:
                f.source = "llm"
                merged[f.code] = f
        llm.findings = list(merged.values())
        # Рекомендации правил сохраняются всегда; LLM дополняет только тем, чего у правил нет.
        known = {normalize(r.text) for r in base.recommendations}
        llm.recommendations = base.recommendations + [r for r in llm.recommendations if normalize(r.text) not in known]
        llm.conclusion = base.conclusion or llm.conclusion
        llm.summary_for_patient = build_patient_summary(llm.findings)
        llm.engine = f"hybrid({llm.engine}+rules)"
        llm.engines = {**base.engines, **llm.engines}
        return llm


def get_finding_extractor() -> FindingExtractor:
    """Точка сборки: какой экстрактор использовать, решают настройки."""
    rules = RuleBasedFindingExtractor()
    if settings.AI_AGENT["PROVIDER"] in ("", "none"):
        return rules
    try:
        llm = LangChainFindingExtractor()
    except Exception as exc:
        logger.exception("Не удалось инициализировать LLM")
        return UnavailableLlmExtractor(rules, f"модель не подключилась: {exc}")
    return HybridFindingExtractor(primary=llm, fallback=rules) if settings.AI_AGENT["FALLBACK_TO_RULES"] else llm


class UnavailableLlmExtractor(FindingExtractor):
    """ИИ-агент настроен, но не поднялся (нет пакета провайдера, неверный адрес): разбор по словарю,
    а причина записывается в разбор и видна на странице протокола, а не только в журнале сервера."""

    name = "rules"

    def __init__(self, rules: FindingExtractor, error: str) -> None:
        self.rules, self.error = rules, error[:300]

    def extract(self, text: str, *, study_type: str = "", dictionary: Dictionary) -> ExtractionPayload:
        payload = self.rules.extract(text, study_type=study_type, dictionary=dictionary)
        payload.engines["llm"] = {"status": "error", "model": model_label(), "error": self.error}
        return payload
