"""
Маркеры и триггеры: точная разметка протокола по позициям в тексте.

Конвейер (1.1): извлечение текста -> фрагменты (segmentation) -> маркеры (этот модуль) ->
триггеры (этот модуль, правила матрицы получаем через RoutingFacade) -> агрегация (stats, quality).

Маркер — найденный признак с точным местом в исходном тексте: тип, код, позиция (start/end),
дословный текст, уверенность распознавания с расшифровкой и ссылка на правило (rule_id).
Поиск идёт по нормализованному тексту (normalization.py), позиции всегда в исходном.

Триггер — то, что требует действия: запуск маршрута (правило матрицы), экстренная находка (словарь),
нужна проверка (изменение описания не вынесено в заключение — эвристика). У триггера есть id,
тип, позиция, цитата, уверенность и правило; одинаковые триггеры из разных мест сводятся в один
с дополнительными цитатами (also).

Источник (source) у каждого маркера и триггера: rules — словарь и правила, llm — только ИИ-агент
(Qwen, с дословной цитатой), both — словарь и ИИ нашли одно и то же место. Найденное только ИИ
показывается отдельно и помечается «проверить».

Ранжирования по важности здесь нет: уверенность отвечает на вопрос «насколько надёжно распознано».
"""
import hashlib
import re
from dataclasses import asdict, dataclass, field

from common.conditions import conditions_met

from . import scoring
from .ai_agent import SCALE_PATTERNS, UNCERTAIN_RE, scale_values
from .annotation import (COUNT_RE, PERCENT_RE, SCALE_CODES, SIDE_BOTH, SIDE_LEFT, SIDE_RIGHT, SIGN_PATTERNS, SIGN_TITLES,
                         SIGNIFICANT_SIGNS, SIZE_RE, AttentionThreshold, is_negated, sign_hits)
from .dictionary import Dictionary
from .normalization import finditer, normalize_text, search

ANALYSIS_VERSION = 2  # 2: источник (словарь / ИИ / оба) у маркеров и триггеров

SOURCE_TITLES = {"rules": "Только словарь", "both": "Словарь и ИИ-агент", "llm": "Только ИИ-агент"}
# Цвет найденного только ИИ: пунктирная рамка поверх цвета типа (контраст проверяет тест).
AI_COLOR = "#4338CA"
# Какие маркеры считаются «признаками» в разрезе по источнику (числа, сторона и отрицания ищет только словарь).
SOURCE_TYPES = {"emergency", "finding", "scale", "sign"}


def merge_source(a: str, b: str) -> str:
    """Словарь и ИИ про одно место: both. Одинаковые источники остаются как есть."""
    return a if a == b else "both"

# Типы маркеров. Это не уровни важности: цвет различает природу признака, а не его «серьёзность».
MARKER_TYPES = {
    "emergency": "Экстренная находка",
    "finding": "Находка из словаря",
    "scale": "Шкала риска",
    "sign": "Признак изменения",
    "rule": "Порог внимания",
    "size": "Размер",
    "count": "Количество",
    "percent": "Процент",
    "side": "Сторона",
    "uncertain": "Сомнение",
    "negation": "С отрицанием",
}
TRIGGER_TYPES = {
    "route": "Запуск маршрута",
    "emergency": "Экстренная находка",
    "review": "Нужна проверка",
}
# Палитра: фон и линия. Текст всегда TEXT_COLOR; контраст по WCAG AA проверяет тест.
TEXT_COLOR = "#1b1f24"
PALETTE = {
    "emergency": ("#FDE2E1", "#B42318"),
    "finding": ("#DCEBFF", "#1D5FBF"),
    "scale": ("#D7F2EE", "#0B7A6F"),
    "sign": ("#EDE4FF", "#6B3FC9"),
    "rule": ("#FFE8CC", "#B54708"),
    "size": ("#FFF1B8", "#8A6100"),
    "count": ("#E8F5D9", "#3B7D23"),
    "percent": ("#FCE7F6", "#A1197A"),
    "side": ("#E6EEF5", "#3E5F80"),
    "uncertain": ("#FFF4E5", "#C4320A"),
    "negation": ("#F2F4F7", "#667085"),
}
TRIGGER_PALETTE = {"route": "#1D5FBF", "emergency": "#B42318", "review": "#B54708"}

# Где маркеры не ищутся: шапка, служебные строки, рекомендации (у рекомендаций своя проверка).
SCANNED_SECTIONS = {"description", "conclusion"}
SKIPPED_KINDS = {"meta", "disclaimer", "technical", "heading", "recommendation"}
CARD_KINDS = {"finding", "abnormal", "scale", "conclusion_item"}
TRIM = " \t\n\r\xa0.,;:()–—-"


@dataclass
class Marker:
    id: str
    type: str
    code: str
    title: str
    start: int
    end: int
    text: str
    confidence: float
    factors: list[dict]
    rule_id: str
    segment_id: int | None = None
    section: str = ""
    negated: bool = False
    corrected: bool = False
    source: str = "rules"
    value: str = ""
    rule_ids: list[str] = field(default_factory=list)
    parent_id: str | None = None
    subsumed_by: str | None = None
    layer: int = 0

    def __post_init__(self) -> None:
        if not self.rule_ids:
            self.rule_ids = [self.rule_id]

    def as_dict(self) -> dict:
        data = asdict(self)
        data["level"] = scoring.level(self.confidence)
        data["type_title"] = MARKER_TYPES[self.type]
        data["rule_title"] = rule_title(self.rule_id)
        data["source_title"] = SOURCE_TITLES[self.source]
        return data

    def confirm_by_llm(self) -> None:
        """ИИ-агент нашёл то же место: источник both, уверенность чуть выше (с причиной в расшифровке)."""
        if self.source == "rules":
            self.source = "both"
            self.confidence, self.factors = scoring.adjust(self.confidence, self.factors, scoring.LLM_CONFIRMED)


RULE_KIND_TITLES = {
    "dictionary": "Словарь находок", "scale": "Шкала риска", "lexicon": "Лексикон признаков",
    "attribute": "Число или сторона в тексте", "cue": "Слова сомнения", "attention": "Порог внимания",
    "llm": "ИИ-агент с дословной цитатой", "matrix": "Матрица маршрутизации", "heuristic": "Эвристика",
}


def rule_title(rule_id: str) -> str:
    """«dictionary:gallstones@v2#1» -> «Словарь находок, версия 2, шаблон 1» — для подсказки."""
    kind, _, rest = rule_id.partition(":")
    title = RULE_KIND_TITLES.get(kind, kind)
    if m := re.search(r"@v(\d+)", rest):
        title += f", версия {m.group(1)}"
    if m := re.search(r"#(\d+)", rest):
        title += f", шаблон {m.group(1)}"
    return title


# Маркеры-слова расширяются до границ слова: шаблон «полип\w*\s+эндометри» подсвечивает «Полип эндометрия» целиком.
WORD_KINDS = {"finding", "emergency", "negation", "sign", "scale"}


def _whole_words(text: str, start: int, end: int) -> tuple[int, int]:
    while start > 0 and text[start - 1].isalpha() and start < end and text[start].isalpha():
        start -= 1
    while end < len(text) and text[end].isalpha() and end > start and text[end - 1].isalpha():
        end += 1
    return start, end


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    """Границы без пробелов и знаков препинания по краям: подсветка ровно по словам."""
    while start < end and text[start] in TRIM:
        start += 1
    while end > start and text[end - 1] in TRIM:
        end -= 1
    return start, end


class MarkerExtractor:
    """Маркеры по фрагментам протокола. Фрагменты — словари (как в ProtocolSegment / SegmentAnnotation.as_dict),
    поэтому маркеры можно пересчитать и для старых протоколов без повторной разметки."""

    def __init__(self, dictionary: Dictionary, thresholds: list[AttentionThreshold] | None = None,
                 study_type: str = "") -> None:
        self.dictionary, self.thresholds, self.study_type = dictionary, thresholds or [], (study_type or "").lower()
        self._markers: list[Marker] = []

    def extract(self, text: str, segments: list[dict]) -> list[Marker]:
        self._markers = []
        for seg in segments:
            if seg["section"] not in SCANNED_SECTIONS or seg["kind"] in SKIPPED_KINDS:
                continue
            self._segment(text, seg)
        self._markers.sort(key=lambda m: (m.start, -(m.end - m.start), m.type))
        for i, m in enumerate(self._markers, start=1):
            m.id = f"m{i}"
        return self._markers

    # ---------------------------------------------------------------- один фрагмент
    def _segment(self, text: str, seg: dict) -> None:
        seg_text, offset = seg["text"], seg["start"]
        norm = normalize_text(seg_text)
        uncertain = bool(seg.get("uncertain"))
        context = {"seg": seg, "offset": offset, "uncertain": uncertain, "text": text}
        own: list[Marker] = []

        for definition in self.dictionary.findings:
            if definition.study_types and self.study_type and not any(
                    s.lower() in self.study_type for s in definition.study_types):
                continue
            for index, pattern in enumerate(definition.patterns):
                for start, end, _m, corrected in finditer(pattern, norm):
                    if any(search(ex, norm) for ex in definition.exclude):
                        continue
                    negated = is_negated(seg_text, start, end)
                    kind = "negation" if negated else ("emergency" if definition.severity == "emergency" else "finding")
                    title = definition.title if not negated else f"{definition.title} (с отрицанием)"
                    own.append(self._make(context, kind, definition.code, title, start, end, "dictionary",
                                          definition.rule_id(index), corrected=corrected, negated=negated))

        for scale, pattern in SCALE_PATTERNS.items():
            for start, end, m, corrected in finditer(pattern, norm):
                values = scale_values(m.group(1))
                if not values:
                    continue
                code = f"{scale}_category"
                own.append(self._make(context, "scale", code, f"{pattern_title(scale)} {max(values)}", start, end, "scale",
                                      f"scale:{scale}", corrected=corrected, value=str(max(values))))

        for hit in sign_hits(norm):
            code, _title, _rx, _ex, significant = SIGN_PATTERNS[hit.pattern_index]
            if code == "us_signs":
                continue
            kind = "negation" if hit.negated else "sign"
            title = SIGN_TITLES[code] + (" (с отрицанием)" if hit.negated else "")
            own.append(self._make(context, kind, f"sign:{code}", title, hit.start, hit.end,
                                  "lexicon" if significant else "lexicon_minor", f"lexicon:sign:{code}",
                                  corrected=hit.corrected, negated=hit.negated))

        if seg["kind"] in CARD_KINDS:
            focal = bool(set(seg.get("finding_codes") or []) - SCALE_CODES or set(seg.get("signs") or []) & SIGNIFICANT_SIGNS)
            own += self._attributes(context, seg_text, focal)
        if uncertain:
            for m in UNCERTAIN_RE.finditer(seg_text):
                own.append(self._make(context, "uncertain", "cue:uncertain", "Сомнение в формулировке",
                                      m.start(), m.end(), "cue", "cue:uncertain", apply_uncertain=False))
        if seg["kind"] in CARD_KINDS:
            own += self._thresholds(context, seg, own)
        own += self._llm(context, seg, seg_text, own)
        self._markers += [m for m in own if m.end > m.start]

    def _attributes(self, context: dict, seg_text: str, focal: bool) -> list[Marker]:
        found = []
        if focal:
            for m in SIZE_RE.finditer(seg_text):
                found.append(self._make(context, "size", "attr:size", "Размер", m.start(), m.end(), "attribute",
                                        "attribute:size", value=_size_mm(m)))
            for m in COUNT_RE.finditer(seg_text):
                found.append(self._make(context, "count", "attr:count", "Количество", m.start(), m.end(), "attribute",
                                        "attribute:count", value=m.group(1) or m.group(0).lower()))
        for m in PERCENT_RE.finditer(seg_text):
            if int(m.group(1)) <= 100:
                found.append(self._make(context, "percent", "attr:percent", "Процент", m.start(), m.end(), "attribute",
                                        "attribute:percent", value=m.group(1)))
        for rx, side in ((SIDE_BOTH, "both"), (SIDE_RIGHT, "right"), (SIDE_LEFT, "left")):
            for m in rx.finditer(seg_text):
                found.append(self._make(context, "side", "attr:side", "Сторона", m.start(), m.end(), "attribute",
                                        "attribute:side", value=side))
        return found

    def _thresholds(self, context: dict, seg: dict, own: list[Marker]) -> list[Marker]:
        """Порог внимания отмечается на числе, которое его превысило (размер, процент, категория шкалы)."""
        codes = set(seg.get("finding_codes") or []) | {f"sign:{s}" for s in seg.get("signs") or []}
        attrs = seg.get("attributes") or {}
        found = []
        for t in self.thresholds:
            if t.finding_code not in codes or not conditions_met(list(t.conditions), attrs):
                continue
            attr = t.conditions[0]["attr"] if t.conditions else ""
            target = _attribute_marker(own, attr, attrs.get(attr)) or next(
                (m for m in own if m.code == t.finding_code and not m.negated), None)
            if target is None:
                continue
            value = attrs.get(attr, "")
            message = t.message.format(value=value) if "{value}" in t.message else t.message
            found.append(self._make(context, "rule", f"attention:{t.code}", message, target.start - context["offset"],
                                    target.end - context["offset"], "attention", f"attention:{t.code}",
                                    value=str(value), apply_uncertain=False))
        return found

    def _llm(self, context: dict, seg: dict, seg_text: str, own: list[Marker]) -> list[Marker]:
        """Что во фрагменте увидел ИИ-агент (Qwen). Совпало со словарём — маркер словаря получает источник
        both. Нашёл только ИИ — отдельный маркер с источником llm, только по дословной цитате."""
        definitions = {d.code: d for d in self.dictionary.findings}
        offset, found = context["offset"], []
        for label in _llm_labels(seg):
            quote = label.get("quote") or ""
            pos = seg_text.find(quote) if quote else -1
            if pos < 0 and quote:
                pos = seg_text.lower().find(quote.lower())
            if pos < 0:
                continue  # цитаты нет во фрагменте — метка не принимается
            q_start, q_end = offset + pos, offset + pos + len(quote)
            positive = [m for m in own if not m.negated and m.type in SOURCE_TYPES]
            codes = [c for c in label.get("codes") or [] if c in definitions]
            inside = [m for m in positive if m.start < q_end and q_start < m.end]
            if codes:
                for m in inside:  # признаки словаря в том же месте («конкременты») ИИ тоже подтвердил
                    m.confirm_by_llm()
                for code in codes:
                    same = [m for m in positive if m.code == code]
                    if same:
                        for m in same:
                            m.confirm_by_llm()
                        continue
                    d = definitions[code]
                    kind = "emergency" if d.severity == "emergency" else "finding"
                    found.append(self._make(context, kind, code, d.title, pos, pos + len(quote), "llm",
                                            f"llm:segment#{code}", source="llm"))
                continue
            if inside:
                for m in inside:
                    m.confirm_by_llm()
            else:
                title = LLM_KIND_TITLES.get(label.get("kind", ""), "Изменение, отмеченное ИИ-агентом")
                found.append(self._make(context, "sign", "llm:change", title, pos, pos + len(quote), "llm",
                                        "llm:segment", source="llm"))
        return found

    def from_findings(self, text: str, segments: list[dict], findings: list[dict], markers: list[Marker]) -> list[Marker]:
        """Находки ИИ-агента из заключения (гибридный режим). Совпала со словарём — источник both;
        только ИИ — отдельный маркер по цитате, чтобы её было видно в тексте и в блоке «Найдено только ИИ»."""
        definitions = {d.code: d for d in self.dictionary.findings}
        found = []
        for f in findings:
            source = f.get("source") or "rules"
            if source == "rules" or f.get("negated"):
                continue
            start, end = f.get("span_start"), f.get("span_end")
            same = [m for m in markers + found if m.code == f["code"] and not m.negated]
            near = [m for m in same if start is not None and end is not None and m.start < end and start < m.end]
            if same:
                for m in near or same[:1]:
                    m.confirm_by_llm()
                continue
            seg = next((s for s in segments if start is not None and s["start"] <= start < s["end"]), None)
            d = definitions.get(f["code"])
            if seg is None or d is None or end is None:
                continue
            context = {"seg": seg, "offset": seg["start"], "uncertain": bool(f.get("uncertain")), "text": text}
            kind = "emergency" if d.severity == "emergency" else "finding"
            found.append(self._make(context, kind, f["code"], d.title, start - seg["start"], min(end, seg["end"]) - seg["start"],
                                    "llm", f.get("rule_id") or "llm:extract", source="llm"))
        return found

    # ---------------------------------------------------------------- сборка маркера
    def _make(self, context: dict, kind: str, code: str, title: str, start: int, end: int, base: str, rule_id: str, *,
              corrected: bool = False, negated: bool = False, value: str = "", source: str = "rules",
              apply_uncertain: bool = True) -> Marker:
        seg, offset, text = context["seg"], context["offset"], context["text"]
        start, end = _trim(text, offset + start, offset + end)
        if kind in WORD_KINDS:
            start, end = _whole_words(text, start, end)
        factors = []
        if seg["section"] == "conclusion" and kind in ("finding", "emergency", "scale", "sign"):
            factors.append(scoring.IN_CONCLUSION)
        if context["uncertain"] and apply_uncertain and kind not in ("negation", "size", "count", "percent", "side"):
            factors.append(scoring.UNCERTAIN)
        if corrected:
            factors.append(scoring.CORRECTED)
        if end - start <= 3 and kind in ("finding", "emergency", "sign"):
            factors.append(scoring.SHORT)
        confidence, details = scoring.score(base, factors)
        return Marker(id="", type=kind, code=code, title=title, start=start, end=end, text=text[start:end],
                      confidence=confidence, factors=details, rule_id=rule_id, segment_id=seg.get("id"),
                      section=seg["section"], negated=negated, corrected=corrected, source=source, value=value)


# Вид фрагмента от ИИ-агента -> название маркера, если кода словаря нет.
LLM_KIND_TITLES = {
    "finding": "Находка, отмеченная ИИ-агентом",
    "abnormal": "Изменение, отмеченное ИИ-агентом",
    "scale": "Шкала риска, отмеченная ИИ-агентом",
    "conclusion_item": "Пункт заключения, отмеченный ИИ-агентом",
    "measurement": "Измерение, отмеченное ИИ-агентом",
}


def _llm_labels(seg: dict) -> list[dict]:
    """Метки ИИ-агента фрагмента. Для разборов до появления llm_labels — цитата из причины подсветки «llm»."""
    if seg.get("llm_labels"):
        return seg["llm_labels"]
    legacy = []
    for h in seg.get("highlights") or []:
        if h.get("type") == "llm" and (quote := re.search(r"«(.+?)»", h.get("text", ""))):
            legacy.append({"quote": quote.group(1), "codes": [], "kind": "abnormal", "highlight": True})
    return legacy


def pattern_title(scale: str) -> str:
    return {"birads": "BI-RADS", "tirads": "TI-RADS", "orads": "O-RADS"}[scale]


def _size_mm(m: re.Match) -> str:
    factor = 10 if m.group(4).lower() == "см" else 1
    dims = [float(v.replace(",", ".")) * factor for v in m.groups()[:3] if v]
    return f"{max(dims):g}"


def _attribute_marker(markers: list[Marker], attr: str, value) -> Marker | None:
    """Маркер атрибута, на котором сработал порог: для размера — наибольший размер во фрагменте."""
    if attr == "size_mm":
        sizes = [m for m in markers if m.type == "size"]
        return max(sizes, key=lambda m: float(m.value or 0), default=None)
    if attr == "percent":
        return next((m for m in markers if m.type == "percent" and str(m.value) == str(value)), None)
    if attr in ("birads", "tirads", "orads"):
        return next((m for m in markers if m.code == f"{attr}_category"), None)
    return None


# ------------------------------------------------------------------ перекрытия (1.2, 2.2)
def resolve_overlaps(markers: list[Marker], text: str) -> tuple[list[Marker], list[dict]]:
    """Перекрытия не «съедают» друг друга:
    1. одинаковые маркеры (тип, код, границы) сливаются, правила складываются в rule_ids;
    2. маркеры одного типа и кода, вложенные или пересекающиеся, объединяются в один;
    3. вложенный маркер получает parent_id; признак внутри находки словаря помечается subsumed_by
       (виден, но в итогах не считается второй раз);
    4. каждый маркер получает слой (layer): пересекающиеся маркеры лежат на разных слоях подсветки.
    Возвращает маркеры и список пересечений [{a, b, kind: same_span | nested | partial}]."""
    merged: list[Marker] = []
    for m in sorted(markers, key=_span_order):
        twin = next((x for x in merged if x.type == m.type and x.code == m.code and x.start < m.end and m.start < x.end), None)
        if twin is None:
            merged.append(m)
            continue
        # Тот же признак на том же месте найден ещё одним шаблоном: одна подсветка, все правила в rule_ids.
        twin.rule_ids = list(dict.fromkeys(twin.rule_ids + m.rule_ids))
        if m.confidence > twin.confidence:
            twin.confidence, twin.factors = m.confidence, m.factors
        twin.start, twin.end = min(twin.start, m.start), max(twin.end, m.end)
        twin.text = text[twin.start:twin.end]
        twin.corrected = twin.corrected and m.corrected
        twin.source = merge_source(twin.source, m.source)
    merged.sort(key=_span_order)
    for i, m in enumerate(merged, start=1):
        m.id = f"m{i}"

    overlaps: list[dict] = []
    for i, a in enumerate(merged):
        for b in merged[i + 1:]:
            if b.start >= a.end:
                break
            if (a.start, a.end) == (b.start, b.end):
                kind = "same_span"
            elif a.start <= b.start and b.end <= a.end:
                kind = "nested"
            else:
                kind = "partial"
            overlaps.append({"a": a.id, "b": b.id, "kind": kind, "types": [a.type, b.type]})

    for m in merged:
        parents = [p for p in merged if p is not m and p.start <= m.start and m.end <= p.end and _outer(p, m)]
        if parents:
            m.parent_id = min(parents, key=lambda p: (p.end - p.start, _TYPE_ORDER.get(p.type, 99))).id
        if m.type == "sign":
            owner = next((p for p in merged if p.type in ("finding", "emergency") and p.start <= m.start and m.end <= p.end), None)
            if owner is not None:
                m.subsumed_by = owner.id

    # Слои: жадная укладка интервалов, внешние (длинные) маркеры — на нижних слоях.
    layer_ends: list[int] = []
    for m in merged:
        for layer, end in enumerate(layer_ends):
            if end <= m.start:
                m.layer, layer_ends[layer] = layer, m.end
                break
        else:
            m.layer = len(layer_ends)
            layer_ends.append(m.end)
    return merged, overlaps


def _span_order(m: Marker) -> tuple:
    return m.start, -(m.end - m.start), _TYPE_ORDER.get(m.type, 99)


def _outer(p: Marker, m: Marker) -> bool:
    """p — внешний маркер для m: длиннее, а при равных границах — «старше» по типу (находка внешняя для размера)."""
    if (p.end - p.start) != (m.end - m.start):
        return (p.end - p.start) > (m.end - m.start)
    return _TYPE_ORDER.get(p.type, 99) < _TYPE_ORDER.get(m.type, 99)


# Порядок типов при равных границах: какой маркер считать «внешним».
_TYPE_ORDER = {"emergency": 0, "finding": 1, "negation": 2, "scale": 3, "rule": 4, "sign": 5, "uncertain": 6,
               "size": 7, "count": 8, "percent": 9, "side": 10}


# ------------------------------------------------------------------ триггеры (1.3)
@dataclass
class Trigger:
    id: str
    type: str
    code: str
    title: str
    start: int
    end: int
    evidence: str
    confidence: float
    factors: list[dict]
    rule_id: str
    rule_title: str
    context: str = ""
    section: str = ""
    marker_ids: list[str] = field(default_factory=list)
    target: dict = field(default_factory=dict)
    also: list[dict] = field(default_factory=list)
    number: str = ""
    source: str = "rules"

    def as_dict(self) -> dict:
        data = asdict(self)
        data["level"] = scoring.level(self.confidence)
        data["type_title"] = TRIGGER_TYPES[self.type]
        data["source_title"] = SOURCE_TITLES[self.source]
        return data


class TriggerDetector:
    """Триггеры по маркерам, находкам заключения и сработавшим правилам матрицы."""

    def detect(self, text: str, markers: list[Marker], findings: list[dict], route_matches: list[dict],
               segments: list[dict]) -> list[Trigger]:
        triggers: list[Trigger] = []
        triggers += self._routes(text, markers, findings, route_matches)
        triggers += self._emergency(markers)
        triggers += self._review(markers, segments)
        triggers = self._dedup(triggers)
        triggers.sort(key=lambda t: (t.start, t.type))
        for i, t in enumerate(triggers, start=1):
            t.number = f"Т{i}"
        return triggers

    def _routes(self, text: str, markers: list[Marker], findings: list[dict], route_matches: list[dict]) -> list[Trigger]:
        found = []
        for match in route_matches:
            finding = match["finding"]
            code = finding.get("code", "")
            primary = self._primary(markers, code, finding)
            source = finding.get("source") or "rules"
            if primary is not None:
                start, end, evidence = primary.start, primary.end, primary.text
                confidence, factors = primary.confidence, primary.factors
                marker_ids = [primary.id]
                source = merge_source(source, primary.source)
            else:
                # Находка без маркера (например, найдена ИИ-агентом): позиция — цитата находки.
                start, end = finding.get("span_start"), finding.get("span_end")
                if start is None or end is None:
                    start = end = 0
                evidence = text[start:end].strip() if end > start else finding.get("evidence_quote", "")
                if source == "llm":  # найдено только ИИ: базовая надёжность ИИ, а не самооценка модели
                    confidence, factors = scoring.score("llm", [scoring.IN_CONCLUSION])
                else:
                    confidence = float(finding.get("confidence") or 0.7)
                    factors = [{"code": "base", "delta": confidence, "reason": "Уверенность находки заключения"}]
                marker_ids = []
            if self._confirmed_elsewhere(markers, code):
                confidence, factors = scoring.adjust(confidence, factors, scoring.CONFIRMED_ELSEWHERE)
            found.append(Trigger(
                id="", type="route", code=code, title=match["rule_title"], start=start, end=end, evidence=evidence,
                confidence=confidence, factors=factors,
                rule_id=f"matrix:{match['rule_code']}@v{match['rule_version']}", rule_title=match["rule_title"],
                context=finding.get("evidence_quote", ""), section="conclusion", marker_ids=marker_ids, source=source,
                target={"route_code": match.get("template_code", ""), "route_title": match.get("template_title", ""),
                        "specialty_code": match.get("specialty_code", ""), "emergency": match.get("is_emergency", False),
                        "finding_rule_id": finding.get("rule_id", "")},
            ))
        return found

    @staticmethod
    def _primary(markers: list[Marker], code: str, finding: dict) -> Marker | None:
        """Маркер, на котором стоит триггер: тот же код, без отрицания, внутри фразы находки; приоритет — заключение."""
        candidates = [m for m in markers if m.code == code and not m.negated]
        s_start, s_end = finding.get("span_start"), finding.get("span_end")
        if s_start is not None and s_end is not None:
            inside = [m for m in candidates if s_start <= m.start and m.end <= s_end]
            candidates = inside or candidates
        candidates.sort(key=lambda m: (m.section != "conclusion", m.start))
        return candidates[0] if candidates else None

    @staticmethod
    def _confirmed_elsewhere(markers: list[Marker], code: str) -> bool:
        sections = {m.section for m in markers if m.code == code and not m.negated}
        return {"description", "conclusion"} <= sections

    @staticmethod
    def _emergency(markers: list[Marker]) -> list[Trigger]:
        return [Trigger(id="", type="emergency", code=m.code, title=m.title, start=m.start, end=m.end, evidence=m.text,
                        confidence=m.confidence, factors=m.factors, rule_id=m.rule_id.split("#")[0],
                        rule_title=f"Словарь находок: {m.title}" if m.source != "llm" else f"ИИ-агент: {m.title}",
                        section=m.section, marker_ids=[m.id], source=m.source,
                        target={"route_title": "Сообщить врачу немедленно", "specialty_code": "", "emergency": True})
                for m in markers if m.type == "emergency"]

    @staticmethod
    def _review(markers: list[Marker], segments: list[dict]) -> list[Trigger]:
        """Эвристика «не вынесено в заключение»: значимое изменение описания без пункта заключения."""
        found = []
        flagged = {s["id"] for s in segments if s.get("not_in_conclusion")}
        for seg_id in sorted(flagged):
            own = [m for m in markers if m.segment_id == seg_id and not m.negated and m.subsumed_by is None
                   and (m.type in ("finding", "emergency") or (m.type == "sign" and m.code[5:] in SIGNIFICANT_SIGNS))]
            if not own:
                continue
            m = own[0]
            confidence, factors = scoring.adjust(m.confidence, m.factors, scoring.HEURISTIC)
            found.append(Trigger(id="", type="review", code=m.code, title=f"Не вынесено в заключение: {m.title.lower()}",
                                 start=m.start, end=m.end, evidence=m.text, confidence=confidence, factors=factors,
                                 rule_id="heuristic:not_in_conclusion", rule_title="Эвристика «есть в описании, нет в заключении»",
                                 section=m.section, marker_ids=[x.id for x in own], source=m.source,
                                 target={"route_title": "Координатор: проверить протокол", "specialty_code": ""}))
        return found

    @staticmethod
    def _dedup(triggers: list[Trigger]) -> list[Trigger]:
        """Один и тот же триггер (тип, правило, код) из нескольких мест — один триггер с дополнительными цитатами."""
        unique: dict[tuple, Trigger] = {}
        for t in sorted(triggers, key=lambda x: (-x.confidence, x.start)):
            key = (t.type, t.rule_id, t.code)
            if key not in unique:
                unique[key] = t
                continue
            main = unique[key]
            if (t.start, t.end) != (main.start, main.end) and all((a["start"], a["end"]) != (t.start, t.end) for a in main.also):
                main.also.append({"start": t.start, "end": t.end, "text": t.evidence})
            main.marker_ids = list(dict.fromkeys(main.marker_ids + t.marker_ids))
            main.source = merge_source(main.source, t.source)
        result = list(unique.values())
        for t in result:
            raw = f"{t.type}|{t.rule_id}|{t.code}|{t.start}|{t.end}"
            t.id = "t" + hashlib.sha1(raw.encode()).hexdigest()[:10]
        return result


# ------------------------------------------------------------------ сборка
def analyze_protocol(text: str, segments: list[dict], findings: list[dict], dictionary: Dictionary, *,
                     thresholds: list[AttentionThreshold] | None = None, route_matches: list[dict] | None = None,
                     study_type: str = "") -> dict:
    """Полный разбор: маркеры, перекрытия, триггеры и итоги. Результат — JSON для ExtractionResult.analysis."""
    extractor = MarkerExtractor(dictionary, thresholds, study_type)
    markers = extractor.extract(text, segments)
    markers += extractor.from_findings(text, segments, findings, markers)
    share_known_codes(markers)
    markers, overlaps = resolve_overlaps(markers, text)
    triggers = TriggerDetector().detect(text, markers, findings, route_matches or [], segments)
    return {
        "version": ANALYSIS_VERSION,
        "markers": [m.as_dict() for m in markers],
        "triggers": [t.as_dict() for t in triggers],
        "overlaps": overlaps,
        "stats": analysis_stats(markers, triggers, overlaps),
    }


def share_known_codes(markers: list[Marker]) -> None:
    """Находку с тем же кодом словарь нашёл в другом месте протокола (например, в заключении), а ИИ-агент
    ещё и в описании: это не «найдено только ИИ», у такого маркера источник both."""
    known = {m.code for m in markers if m.source != "llm" and not m.negated and m.type in ("finding", "emergency")}
    for m in markers:
        if m.source == "llm" and m.code in known:
            m.source = "both"


def analysis_stats(markers: list[Marker], triggers: list[Trigger], overlaps: list[dict]) -> dict:
    by_type: dict[str, int] = {}
    for m in markers:
        if m.subsumed_by is None:
            by_type[m.type] = by_type.get(m.type, 0) + 1
    trigger_types: dict[str, int] = {}
    for t in triggers:
        trigger_types[t.type] = trigger_types.get(t.type, 0) + 1
    return {
        "markers": len(markers),
        "markers_counted": sum(by_type.values()),
        "markers_by_type": by_type,
        "subsumed": sum(1 for m in markers if m.subsumed_by),
        "corrected": sum(1 for m in markers if m.corrected),
        "overlaps": len(overlaps),
        "overlap_kinds": {k: sum(1 for o in overlaps if o["kind"] == k) for k in ("same_span", "nested", "partial")},
        "layers": max((m.layer for m in markers), default=-1) + 1,
        "triggers": len(triggers),
        "triggers_by_type": trigger_types,
        # Кто нашёл: словарь, ИИ-агент или оба (признаки — находки, шкалы, признаки изменений).
        "markers_by_source": _count_sources(m for m in markers if m.subsumed_by is None and m.type in SOURCE_TYPES),
        "triggers_by_source": _count_sources(triggers),
        "ai_only": {"markers": sum(1 for m in markers if m.source == "llm" and m.subsumed_by is None),
                    "triggers": sum(1 for t in triggers if t.source == "llm")},
    }


def _count_sources(items) -> dict:
    counts = {"rules": 0, "both": 0, "llm": 0}
    for item in items:
        counts[item.source] += 1
    return counts
