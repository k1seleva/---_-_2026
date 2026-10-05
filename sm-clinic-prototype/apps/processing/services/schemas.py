"""
Контракт JSON, который возвращает AI-агент и принимает модуль маршрутизации.
Pydantic-схема используется трижды: для structured output в LangChain,
для валидации ответа LLM и как документация API.
"""
from typing import Optional, Union

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema

AttrValue = Union[str, int, float, bool]


class ExtractedFinding(BaseModel):
    code: str = Field(description="Код находки из словаря или OTHER")
    label: str = Field(description="Формулировка находки как в протоколе")
    evidence_quote: str = Field(description="Дословная цитата из протокола, подтверждающая находку")
    negated: bool = Field(default=False, description="Находка упомянута с отрицанием")
    uncertain: bool = Field(default=False, description="Есть сомнение: «?», «нельзя исключить»")
    attributes: dict[str, AttrValue] = Field(default_factory=dict, description="birads, tirads, orads, size_mm, side ...")
    confidence: float = Field(default=0.8, ge=0, le=1)
    severity: str = Field(default="routine", description="routine | urgent | emergency")
    span_start: Optional[int] = None
    span_end: Optional[int] = None
    rule_id: str = Field(default="", description="Правило, которым найдено: dictionary:<код>@v<версия>#<шаблон> / scale:<шкала> / llm")
    # Кто нашёл: rules (словарь), llm (только ИИ-агент), both (словарь и ИИ согласны). Заполняет система,
    # в схему ответа модели поле не попадает (SkipJsonSchema).
    source: SkipJsonSchema[str] = "rules"


class ExtractedRecommendation(BaseModel):
    text: str = Field(description="Рекомендация из протокола дословно")
    kind: str = Field(default="consultation", description="consultation | diagnostics | observation")
    specialty_code: Optional[str] = Field(default=None, description="Код специальности из справочника")
    service: Optional[str] = Field(default=None, description="Исследование/услуга, например «УЗИ молочных желёз»")
    interval_days: Optional[int] = Field(default=None, description="Через сколько дней (6 мес = 180)")


class ExtractionPayload(BaseModel):
    study_type: str = ""
    study_date: Optional[str] = None
    conclusion: str = ""
    findings: list[ExtractedFinding] = Field(default_factory=list)
    recommendations: list[ExtractedRecommendation] = Field(default_factory=list)
    summary_for_patient: str = ""
    engine: str = ""
    dictionary_version: str = ""
    grounding: dict = Field(default_factory=dict, description="Отчёт контроля достоверности ответа LLM")
    # Как отработали движки: {"rules": {...}, "llm": {"status": "ok" | "error" | "off", "model", "ms", "error"}}.
    engines: SkipJsonSchema[dict] = Field(default_factory=dict)

    @property
    def positive_findings(self) -> list[ExtractedFinding]:
        return [f for f in self.findings if not f.negated]


# ------------------------------------------------------------------ разметка фрагментов (LLM)
class SegmentLabel(BaseModel):
    """Метка одного фрагмента протокола. Агент не пишет текст — только ссылается на фрагмент по id."""

    segment_id: int = Field(description="Номер фрагмента из списка")
    kind: str = Field(description="finding | scale | abnormal | conclusion_item | norm | measurement | recommendation | "
                                  "meta | technical | disclaimer | heading | unclassified")
    highlight: bool = Field(default=False, description="true — подсветить врачу (только с дословной цитатой)")
    finding_codes: list[str] = Field(default_factory=list, description="Коды из словаря находок или OTHER")
    attributes: dict[str, AttrValue] = Field(default_factory=dict, description="size_mm, percent, birads ... — только числа из фрагмента")
    evidence_quote: str = Field(default="", description="Дословная часть фрагмента, на которой основана метка")


class SegmentLabeling(BaseModel):
    labels: list[SegmentLabel] = Field(default_factory=list)
