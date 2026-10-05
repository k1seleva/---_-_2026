"""
Советы ИИ-агента по маршрутизации (Qwen через LangChain).

Совет — только рекомендация для координатора («рекомендация ИИ, требует проверки координатором»):
маршрут по нему не строится и не меняется, решение и оценку принимает человек (AdviceReview в модуле
координатора). Советы хранятся отдельно от аналитики (RoutingAdvice) и не смешиваются с маркерами
и триггерами.

Защита от выдумок такая же, как у извлечения находок: совет без дословной цитаты из протокола,
с маршрутом не из справочника или с неизвестной специальностью отбрасывается, причина сохраняется.

Режимы (settings.ROUTING_ADVISOR["MODE"]):
* qwen — модель Qwen (Ollama или vLLM в контуре клиники) через LangChain со структурированным ответом;
* demo — без модели: советы собираются правилами из триггеров разбора, на экране помечены как демо;
* auto — qwen, если модель настроена (провайдер задан), иначе demo; off — советы не формируются.

Если модель настроена, но не ответила, на странице видна ошибка и кнопка «Спросить Qwen ещё раз».
Демо-советы вместо ответа модели не подставляются: координатор должен видеть, что ответил именно Qwen.
Ответ модели целиком (до разбора по схеме) сохраняется в advice_meta["raw"] и показывается под советами.
"""
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction
from pydantic import BaseModel, Field

from common import clock

from . import scoring
from .normalization import normalize_text

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ контекст и результат
@dataclass
class AdviceContext:
    text: str
    conclusion: str
    study_type: str
    triggers: list[dict]
    recommendations: list[dict]
    catalog: list[dict]                       # маршруты: code, title, specialty_code, finding_codes
    specialties: dict[str, str]               # код -> название
    matrix_routes: set[str] = field(default_factory=set)


@dataclass
class AdviceDraft:
    text: str
    rationale: str
    evidence_quote: str
    confidence: float
    route_code: str = ""
    specialty_code: str = ""
    executor: str = ""


class AdviceItem(BaseModel):
    """Схема ответа модели (structured output)."""

    text: str = Field(description="Совет координатору по маршрутизации: к кому и по какому маршруту направить")
    rationale: str = Field(description="Обоснование: почему, со ссылкой на находку протокола")
    evidence_quote: str = Field(description="Дословная цитата из протокола, на которую опирается совет")
    confidence: float = Field(ge=0, le=1, description="Уверенность в совете от 0 до 1")
    route_code: str = Field(default="", description="Код маршрута из справочника или пусто")
    specialty_code: str = Field(default="", description="Код специальности из справочника или пусто")
    executor: str = Field(default="", description="Кому адресован совет: врач-специалист, координатор, подразделение")


class AdviceList(BaseModel):
    advice: list[AdviceItem] = Field(default_factory=list)


# ------------------------------------------------------------------ советчики
class RoutingAdvisor(ABC):
    engine: str = ""
    model_name: str = ""
    # Ответ модели целиком и время ответа последнего вызова (для экрана и журнала; у демо пусто).
    last_raw: str = ""
    last_ms: int | None = None

    @abstractmethod
    def advise(self, context: AdviceContext) -> list[AdviceDraft]:
        ...


ADVICE_SYSTEM_PROMPT = """Ты помощник координатора клиники. По протоколу УЗИ и найденным триггерам ты советуешь,
куда направить пациента: какой маршрут и к какому специалисту. Ты НЕ ставишь диагноз, НЕ назначаешь
лечение и НЕ пишешь пациенту. Твой совет проверит координатор. Отвечай только по-русски.

Правила (ответ проверяется программно, непроверяемое отбрасывается):
1. Не больше {max_advice} советов. Каждый совет опирается на находку из протокола.
2. evidence_quote — ДОСЛОВНАЯ цитата из текста протокола.
3. route_code — только код из справочника маршрутов ниже или пусто; specialty_code — только код из списка
   специальностей или пусто.
4. confidence — твоя уверенность от 0 до 1. Если сомневаешься, ставь меньше 0,5 и объясни почему.
5. Если триггер уже запускает маршрут, можно согласиться с ним или указать, чего не хватает
   (вторая специальность, срок, дообследование из рекомендаций протокола).
6. Триггер с пометкой [только ИИ] словарь не подтвердил: проверь его по тексту протокола особенно внимательно.
7. Если советовать нечего, верни пустой список advice.

Справочник маршрутов (код: название, первая специальность):
{catalog}

Специальности: {specialties}"""

ADVICE_HUMAN_PROMPT = """Тип исследования: {study_type}

Триггеры разбора (словарь и ИИ-агент):
{triggers}

Рекомендации врача в протоколе:
{recommendations}

Текст протокола:
<<<
{text}
>>>"""


class QwenRoutingAdvisor(RoutingAdvisor):
    engine = "qwen"

    def __init__(self, llm=None, config: dict | None = None) -> None:
        self.config = {**settings.ROUTING_ADVISOR, **(config or {})}
        self.model_name = self.config["MODEL"]
        self.llm = llm if llm is not None else self._build()
        if self.llm is None:
            raise RuntimeError("Модель для советов не настроена (ROUTING_ADVISOR_PROVIDER)")

    def _build(self):
        from .ai_agent import build_chat_model

        keys = ("PROVIDER", "MODEL", "BASE_URL", "TEMPERATURE", "TIMEOUT_SEC", "NUM_CTX", "REASONING", "NUM_PREDICT",
                "KEEP_ALIVE")
        return build_chat_model({key: self.config.get(key) for key in keys})

    def advise(self, context: AdviceContext) -> list[AdviceDraft]:
        from langchain_core.prompts import ChatPromptTemplate

        prompt = ChatPromptTemplate.from_messages([("system", ADVICE_SYSTEM_PROMPT), ("human", ADVICE_HUMAN_PROMPT)])
        # include_raw: кроме разобранного объекта, получаем ответ модели целиком — его видит координатор.
        chain = prompt | self.llm.with_structured_output(AdviceList, include_raw=True)
        self.last_raw, self.last_ms = "", None
        started = time.monotonic()
        from .ai_agent import call_llm

        out = call_llm(lambda: chain.invoke(self.prompt_inputs(context, self.config["MAX_ADVICE"])), self.config)
        self.last_ms = round((time.monotonic() - started) * 1000)
        result = out
        if isinstance(out, dict) and "parsed" in out:
            self.last_raw = raw_text(out.get("raw"))
            if out.get("parsing_error") is not None or out.get("parsed") is None:
                raise AdviceParsingError(f"ответ модели не совпал со схемой советов: {out.get('parsing_error') or 'пустой ответ'}")
            result = out["parsed"]
        if isinstance(result, dict):  # некоторые провайдеры возвращают словарь вместо pydantic-объекта
            result = AdviceList.model_validate(result)
        if not self.last_raw:
            self.last_raw = result.model_dump_json(indent=1)
        return [AdviceDraft(text=a.text, rationale=a.rationale, evidence_quote=a.evidence_quote, confidence=a.confidence,
                            route_code=a.route_code, specialty_code=a.specialty_code, executor=a.executor)
                for a in result.advice]

    @staticmethod
    def prompt_inputs(context: AdviceContext, max_advice: int) -> dict:
        return {
            "max_advice": max_advice,
            "catalog": "\n".join(f"- {r['code']}: {r['title']}, {context.specialties.get(r['specialty_code'], r['specialty_code'])}"
                                 for r in context.catalog) or "- нет",
            "specialties": ", ".join(f"{code} ({title})" for code, title in sorted(context.specialties.items())),
            "study_type": context.study_type or "не указан",
            "triggers": "\n".join(f"- {t['number']} {t['type_title']}: «{t['evidence']}» ({t['rule_title']})"
                                  f"{SOURCE_NOTES.get(t.get('source', 'rules'), '')}" for t in context.triggers) or "- нет",
            "recommendations": "\n".join(f"- {r.get('text', '')}" for r in context.recommendations) or "- нет",
            "text": context.text,
        }


SOURCE_NOTES = {"llm": " [только ИИ]", "both": " [словарь и ИИ]"}


class AdviceParsingError(ValueError):
    """Модель ответила, но не по схеме: ответ целиком сохраняется, чтобы его можно было прочитать."""


def raw_text(message) -> str:
    """Текст ответа модели: content, а при вызове инструмента (OpenAI-совместимые серверы) — его аргументы."""
    if message is None:
        return ""
    content = getattr(message, "content", message)
    if isinstance(content, list):
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    text = str(content or "").strip()
    if not text and getattr(message, "tool_calls", None):
        text = json.dumps([c.get("args") for c in message.tool_calls], ensure_ascii=False, indent=1)
    return text


class DemoRoutingAdvisor(RoutingAdvisor):
    """Без модели: советы собираются правилами из триггеров разбора. На экране помечены как демо-режим,
    нужны, чтобы проверить интерфейс оценки координатором до подключения Qwen."""

    engine = "demo"
    model_name = ""

    def advise(self, context: AdviceContext) -> list[AdviceDraft]:
        drafts = []
        by_code = {r["code"]: r for r in context.catalog}
        for t in context.triggers:
            target = t.get("target") or {}
            if t["type"] == "route":
                route = by_code.get(target.get("route_code", ""), {})
                specialty = target.get("specialty_code") or route.get("specialty_code", "")
                drafts.append(AdviceDraft(
                    text=f"Направить по маршруту «{route.get('title') or target.get('route_title', '')}»: "
                         f"первый этап у специалиста «{context.specialties.get(specialty, specialty)}».",
                    rationale=f"Сработало правило «{t['rule_title']}» по цитате «{t['evidence']}».",
                    evidence_quote=t["evidence"], confidence=t["confidence"], route_code=target.get("route_code", ""),
                    specialty_code=specialty, executor=context.specialties.get(specialty, "")))
            elif t["type"] == "review":
                route = next((r for r in context.catalog if t["code"] in r["finding_codes"]), None)
                drafts.append(AdviceDraft(
                    text="Уточнить у врача-диагноста, нужен ли маршрут: изменение описано, но не вынесено в заключение.",
                    rationale=f"В описании «{t['evidence']}», в заключении этого пункта нет.",
                    evidence_quote=t["evidence"], confidence=t["confidence"],
                    route_code=route["code"] if route else "", specialty_code="", executor="Координатор"))
        return drafts


def configured_engine() -> str:
    """Какой советчик включён настройками (без подключения к модели): qwen, demo или off."""
    cfg = settings.ROUTING_ADVISOR
    if cfg["MODE"] in ("off", "demo", "qwen"):
        return cfg["MODE"]
    return "demo" if cfg["PROVIDER"] in ("", "none") else "qwen"


def configured_model() -> str:
    return settings.ROUTING_ADVISOR["MODEL"] if configured_engine() == "qwen" else ""


def get_advisor() -> RoutingAdvisor | None:
    """Модель настроена — только она: если не поднялась, ошибка уходит в advice_meta (демо не подставляется)."""
    engine = configured_engine()
    if engine == "off":
        return None
    if engine == "demo":
        return DemoRoutingAdvisor()
    return QwenRoutingAdvisor()


# ------------------------------------------------------------------ проверка и сохранение
def ground_advice(drafts: list[AdviceDraft], context: AdviceContext) -> tuple[list[tuple[AdviceDraft, dict]], list[dict]]:
    """Совет проходит, только если цитата есть в протоколе, а маршрут и специальность — из справочников."""
    text_norm = " ".join(normalize_text(context.text).text.split())
    routes = {r["code"] for r in context.catalog}
    accepted, rejected, seen = [], [], set()
    for d in drafts[: settings.ROUTING_ADVISOR["MAX_ADVICE"]]:
        quote = " ".join(normalize_text(d.evidence_quote).text.split())
        key = (" ".join(normalize_text(d.text).text.lower().split()), d.route_code)
        checks = {
            "quote_found": bool(quote) and quote in text_norm,
            "route_known": not d.route_code or d.route_code in routes,
            "specialty_known": not d.specialty_code or d.specialty_code in context.specialties,
            "has_text": bool(d.text.strip() and d.rationale.strip()),
            "not_duplicate": key not in seen,   # модель повторила совет для второго триггера той же находки
        }
        seen.add(key)
        if all(checks.values()):
            accepted.append((d, checks))
        else:
            failed = [k for k, ok in checks.items() if not ok]
            rejected.append({"text": d.text[:200], "failed": failed})
    return accepted, rejected


class RoutingAdviceService:
    """Сформировать советы для разбора протокола и сохранить их отдельно от аналитики."""

    def __init__(self, advisor: RoutingAdvisor | None = None) -> None:
        self.advisor = advisor

    def generate(self, result_id) -> int:
        from apps.doctors.facade import DoctorsFacade
        from apps.routing.facade import RoutingFacade

        from ..facade import ProcessingFacade
        from ..models import ExtractionResult, RoutingAdvice

        result = ExtractionResult.objects.select_related("document").get(pk=result_id)
        meta = {"generated_at": clock.now().isoformat(), "prompt_version": settings.ROUTING_ADVISOR["PROMPT_VERSION"]}
        try:
            advisor = self.advisor or get_advisor()
        except Exception as exc:  # noqa: BLE001 — модель не подключилась: причина видна на странице
            logger.exception("Модель для советов не подключилась")
            result.advice_meta = {**meta, "status": "error", "engine": "qwen", "model": configured_model(),
                                  "error": f"модель не подключилась: {exc}"[:300]}
            _save_error(result, "qwen")
            return 0
        if advisor is None:
            result.advice_meta = {**meta, "status": "off"}
            result.save(update_fields=["advice_meta", "updated_at"])
            return 0
        analysis = ProcessingFacade.get_analysis(result.document_id) or {}
        triggers = analysis.get("triggers", [])
        context = AdviceContext(
            text=result.document.raw_text, conclusion=result.conclusion, study_type=result.document.study_type,
            triggers=triggers, recommendations=result.payload.get("recommendations", []),
            catalog=RoutingFacade.route_catalog(), specialties=DoctorsFacade.specialty_titles(),
            matrix_routes={(t.get("target") or {}).get("route_code", "") for t in triggers if t["type"] == "route"},
        )
        meta |= {"engine": advisor.engine, "model": advisor.model_name}
        started = time.monotonic()
        try:
            drafts = advisor.advise(context)
        except Exception as exc:  # noqa: BLE001 — модель недоступна: аналитика и маршрут не страдают
            logger.exception("Ошибка формирования советов")
            from .ai_agent import describe_llm_error

            error = str(exc)[:300] if isinstance(exc, AdviceParsingError) else describe_llm_error(exc, _llm_config())
            result.advice_meta = {**meta, "status": "error", "error": error,
                                  "raw": advisor.last_raw[:RAW_LIMIT], "duration_ms": _ms(advisor, started)}
            _save_error(result, advisor.engine)
            return 0
        meta |= {"raw": advisor.last_raw[:RAW_LIMIT], "duration_ms": _ms(advisor, started), "proposed": len(drafts)}
        accepted, rejected = ground_advice(drafts, context)
        with transaction.atomic():
            RoutingAdvice.objects.filter(result=result).delete()
            RoutingAdvice.objects.bulk_create(RoutingAdvice(
                document_id=result.document_id, result=result, seq=i, text=d.text, rationale=d.rationale,
                evidence_quote=d.evidence_quote, confidence=round(min(1.0, max(0.0, d.confidence)), 2),
                target_route_code=d.route_code, target_route_title=_route_title(context.catalog, d.route_code),
                target_specialty_code=d.specialty_code, target_executor=d.executor[:255], engine=advisor.engine,
                model_name=advisor.model_name, prompt_version=meta["prompt_version"], grounding=checks,
                matches_matrix=bool(d.route_code) and d.route_code in context.matrix_routes,
            ) for i, (d, checks) in enumerate(accepted, start=1))
            result.advice_meta = {**meta, "status": "ready", "accepted": len(accepted), "rejected": rejected}
            result.save(update_fields=["advice_meta", "updated_at"])
        return len(accepted)


def _llm_config() -> dict:
    cfg = settings.ROUTING_ADVISOR
    return {key: cfg.get(key) for key in ("PROVIDER", "MODEL", "BASE_URL", "TIMEOUT_SEC")}


def _save_error(result, engine: str) -> None:
    """Модель не ответила: прежние советы этой же модели остаются (сбой мог быть временным), а советы
    другого движка (демо) убираются, чтобы их не приняли за ответ модели."""
    from ..models import RoutingAdvice

    with transaction.atomic():
        RoutingAdvice.objects.filter(result=result).exclude(engine=engine).delete()
        result.save(update_fields=["advice_meta", "updated_at"])


RAW_LIMIT = 8000  # символов ответа модели в advice_meta: хватает на 5 советов с обоснованием


def _ms(advisor: RoutingAdvisor, started: float) -> int | None:
    return advisor.last_ms if advisor.last_ms is not None else (
        round((time.monotonic() - started) * 1000) if advisor.engine == "qwen" else None)


def _route_title(catalog: list[dict], code: str) -> str:
    return next((r["title"] for r in catalog if r["code"] == code), "")[:255]


def confidence_level(value: float) -> str:
    return scoring.level(value)
