"""
Разметка протокола для врача: ИИ подсвечивает и структурирует, но не ранжирует.

Что делает модуль:
1. Режет протокол на фрагменты без потери текста (segmentation.Segmenter).
2. Размечает КАЖДЫЙ фрагмент: что это (находка, изменение, норма, параметр, рекомендация,
   служебная строка) и почему его стоит увидеть — «подсветки» с причиной: находка из словаря,
   шкала риска, порог внимания, размер, количество, сторона, сомнение, «не вынесено в заключение».
3. Связывает детали описания с пунктами заключения (общий код находки, общий признак или орган):
   «Конкременты множественные до 15 мм» встаёт под пункт «холецистолитиаз».

Чего модуль НЕ делает: не решает, какой признак важнее. Уровней «критично / высокое / низкое»
нет — порядок на экране задаёт врач (FocusProfile, закреплённые фрагменты), а по умолчанию
действует порядок самого протокола. Единственный жёсткий флаг — экстренная находка из словаря
(тромбоз): это требование безопасности, а не ранжирование, и он только добавляет предупреждение.
Ничего не удаляется: в выдаче остаётся каждый фрагмент (проверяется инвариантом).

Правила (словарь находок, лексикон признаков, пороги AttentionRule) детерминированы и
воспроизводимы. LLM (если включён) может только ДОБАВИТЬ подсветку с дословной цитатой
(см. grounding.py), снять подсветку правил он не может. Решение о маршруте здесь не принимается.
"""
import re
import time
from dataclasses import dataclass, field

from common.conditions import conditions_met

from .ai_agent import DISCLAIMER_RE, NEGATION_AFTER, NEGATION_BEFORE, NEGATION_VERBS, UNCERTAIN_RE, extract_scales
from .dictionary import Dictionary
from .grounding import GroundingGuard, GroundingReport
from .normalization import NormalizedText, finditer, normalize_text, search
from .segmentation import HEADING_RE, RECOMMENDATION_START_RE, Segment, Segmenter, organ_keys

KINDS = {"finding", "scale", "abnormal", "conclusion_item", "norm", "measurement", "recommendation",
         "meta", "technical", "disclaimer", "heading", "unclassified"}
SCALE_CODES = {"birads_category", "tirads_category", "orads_category"}
# Причины подсветки. Это не уровни важности: у них нет порядка, врач сам выбирает, что ему в фокусе.
HIGHLIGHT_TYPES = {
    "emergency": "Экстренная находка",
    "finding": "Находка из словаря",
    "scale": "Шкала риска",
    "rule": "Порог внимания",
    "change": "Изменение без кода словаря",
    "conclusion_item": "Пункт заключения без кода словаря",
    "not_in_conclusion": "Не вынесено в заключение",
    "uncertain": "Сомнение в формулировке",
    "size": "Размер",
    "count": "Количество",
    "side": "Сторона",
    "percent": "Процент",
    "negation_nearby": "Рядом отрицание",
    "unclassified": "Не классифицировано",
    "llm": "Отмечено ИИ-агентом",
}
# Подсветки, после которых фрагмент считается «значимым» для других модулей (проверка рекомендаций).
SIGNIFICANT_TYPES = {"emergency", "finding", "scale", "rule", "change", "conclusion_item", "not_in_conclusion", "llm"}


# ------------------------------------------------------------------ лексикон признаков
# Изменения, которых нет в словаре находок, но которые врач должен увидеть.
# (код, название, шаблон, контекст-исключение, «значимый» — проверять вынесение в заключение)
SIGNS: list[tuple[str, str, str, str, bool]] = [
    ("mass", "образование", r"образовани\w*", "", True),
    ("cyst", "киста", r"(?<!мелко)кист\w*", "", True),
    ("nodule", "узел", r"\bузл\w*|\bузел\b|\bузлов\w*", r"лимф|л/узл", True),  # исключение ищется от начала фрагмента
    ("stone", "конкременты", r"конкремент\w*|\bкамн\w*|литиаз\w*", "", True),
    ("polyp", "полип", r"полип\w*", "", True),
    ("thrombus", "тромб", r"тромб\w*", "", True),
    ("stenosis", "стеноз", r"стеноз\w*", "", True),
    ("fluid", "свободная жидкость", r"свободн\w*\s+жидкост\w*|жидкост\w*\s+в\s+(малом\s+тазу|брюшной)", "", True),
    ("hydro", "гидронефроз / гидросальпинкс", r"гидро(нефроз|сальпинкс|целе)\w*|пиелоэктази\w*", "", True),
    ("plaque", "атеросклеротические изменения", r"бляшк\w*|бляшек|атеросклер\w*", "", True),
    ("myoma", "миома", r"\bмиом(?!етр)\w*|лейомиом\w*", "", True),
    ("endometriosis", "эндометриоз / аденомиоз", r"эндометриоз\w*|аденомиоз\w*", "", True),
    ("hyperplasia", "гиперплазия", r"гиперплази\w*", "", True),
    ("thickening", "утолщение", r"утолщ\w*", "", False),
    ("dilation", "расширение", r"расширен\w*|дилатац\w*|дилатир\w*|эктази\w*", "", False),
    ("enlargement", "увеличение", r"увеличен\w*", "", False),
    ("deformation", "деформация", r"деформ\w*|перегиб\w*|загиб\w*", "", False),
    ("heterogeneity", "неоднородность", r"неоднородн\w*|гетерогенн\w*", "", False),
    ("calcification", "кальцинаты", r"кальцин\w*|кальциноз\w*", "", False),
    ("inclusion", "включения", r"включени\w*", "", False),
    ("sludge", "сладж / взвесь", r"сладж\w*|взвес\w*", "", False),
    ("echo", "изменение эхогенности", r"(повышенн|пониженн|сниженн)\w*\s+эхогенност\w*", "", False),
    ("reflux", "рефлюкс / несостоятельность", r"рефлюкс\w*|несостоятельн\w*", "", True),
    ("varicose", "варикозная трансформация", r"варикоз\w*", "", True),
    ("diffuse", "диффузные изменения", r"диффузн\w*\s+изменени\w*", "", False),
    ("named_focal", "очаговое образование (названное)",
     r"гемангиом\w*|фиброаденом\w*|\bлипом\w*|\bаденом(?!иоз)\w*|кистом\w*|тератом\w*|дермоид\w*|эндометриом\w*|"
     r"гамартом\w*|ангиомиолипом\w*|атером\w*", "", True),
    ("cholesterosis", "холестероз", r"холестероз\w*", "", True),
    ("lymph", "изменения лимфоузлов", r"лимфаденопат\w*|увеличенн\w*\s+лимф\w*", "", True),
    ("us_signs", "УЗ-признаки", r"(?:УЗ|ЭХО|ультразвуков\w*)[-\s]*признак\w*|УЗИ\s+картин\w*", "", False),
]
SIGN_PATTERNS = [(code, title, re.compile(rx, re.IGNORECASE), re.compile(ex, re.IGNORECASE) if ex else None, significant)
                 for code, title, rx, ex, significant in SIGNS]
SIGN_TITLES = {code: title for code, title, *_ in SIGNS}
SIGNIFICANT_SIGNS = {code for code, *_, significant in SIGNS if significant}

NORM_RE = re.compile(
    r"\b(ровн|четк|однородн|обычн|типичн|сохранен|симметричн|правильн|удовлетворительн|проходим|физиологич|нормальн|"
    r"сомкнут|соответству|anteflexio|anteversio)\w*|\bв\s+норме|\bв\s+пределах\s+норм\w*|\bбез\s+(особенностей|изменений|видимых\s+изменений|патолог\w*)|"
    r"\bне\s+(изменен|увеличен|расширен|утолщен|деформир|усилен|определя|визуализир|лоцир|выявлен|обнаруж)\w*",
    re.IGNORECASE,
)
# Отрицание в пределах той же части фразы (между запятыми/скобками).
# «Без …» сюда не входит: в «миомы малых размеров без признаков роста» отрицается рост, а не миома
# («без признаков …» перед находкой ловит NEGATION_BEFORE).
NEGATION_ANY = re.compile(
    rf"\bне\s+{NEGATION_VERBS}|\bне\s+(выражен|получен)\w*|\bнет\b|\bотсутств\w*|:\s*не\s*[.;]?\s*$",
    re.IGNORECASE,
)
NEGATION_IMMEDIATE = re.compile(r"(\bне|\bбез)\s+$", re.IGNORECASE)
# Общее отрицание перечня: «кисты, дополнительные образования – не определяются».
SENTENCE_END_RE = re.compile(r"[.;!?](?:\s|$)")
LIST_NEGATION_RE = re.compile(rf"[\s,–—-]*\bне\s+{NEGATION_VERBS}\s*$|[\s,–—:-]*\bнет\s*$", re.IGNORECASE)
POSITIVE_RE = re.compile(r"(?<!не )\b(есть|имеет\w*|определя\w*|лоцир\w*|визуализир\w*|выявлен\w*|отмеча\w*)", re.IGNORECASE)
LIST_FOLLOW_RE = re.compile(r"\s*([,–—]|(в|во|на)\s)", re.IGNORECASE)
OTHER_PROPERTY_RE = re.compile(r"кровот|ЦДК|контур|эхоген|структур|стенк|содержим|акустич|\bтен[ьи]\b|васкуляр|размер|"
                               r"перегород|включени|взвес|сосуд",
                               re.IGNORECASE)
TECH_RE = re.compile(
    r"(на\s+(ультразвуков\w+\s+)?(аппарат|сканер)\w*|^Аппарат\b|датчик\w*|Эхограммы\s+выдан\w*|Визуализация\s+(удовлетворит|затрудн|хорош)\w*|УЗДсистем\w*|"
    r"ИДС\s+получено|последний\s+при[её]м\s+пищи|Не\s+ел\w*\s+\d)",
    re.IGNORECASE,
)
SIZE_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)(?:\s*[хx\*×]\s*(\d+(?:[.,]\d+)?))?(?:\s*[хx\*×]\s*(\d+(?:[.,]\d+)?))?\s*(мм|см)(?![А-Яа-яA-Za-z0-9])",
    re.IGNORECASE,
)
PERCENT_RE = re.compile(r"(\d{1,3})\s*%")
COUNT_RE = re.compile(r"множествен\w*|единичн\w*|несколько|\b(\d+)\s+(?:узл|кист|конкремент|образован|полип)\w*", re.IGNORECASE)
SIDE_RIGHT = re.compile(r"справа|правой|правого|правая|правый", re.IGNORECASE)
SIDE_LEFT = re.compile(r"слева|левой|левого|левая|левый", re.IGNORECASE)
SIDE_BOTH = re.compile(r"с\s+обеих\s+сторон|обеих|двусторон\w*", re.IGNORECASE)
# Граница части фразы: запятая, точка с запятой, скобки и конец предложения (точка перед пробелом, не «1.5»).
# Без конца предложения «полип 6 мм. Вены: тромбоз не выявлен» отрицал бы и полип.
CLAUSE_SPLIT = re.compile(r"[,;()]|[.!?](?=\s)")
# Название исследования в начале описания: «УЗИ желчного пузыря», «Исследование молочных желез».
TITLE_RE = re.compile(r"^(УЗИ|УЗДС|ЦДК|Ультразвуковое\s+исследование|Исследование|Протокол|УЗДГ|Дуплексное|Триплексное|ТРУЗИ|ТВУЗИ|МРТ|КТ)\b[^\d]{0,120}$",
                      re.IGNORECASE)
# Подписи разделов, в том числе блоки 1С после заключения («Диагноз», «Лабораторная диагностика»).
SECTION_LABEL_RE = re.compile(r"^(Описание|Заключение|Диагноз|Лабораторная\s+диагностика|Инструментальная\s+диагностика)\s*:?$",
                              re.IGNORECASE)
SIDE_WORDS = {"правый", "левый", "правая", "левая", "правое", "левое", "обеих", "правой", "левой"}


# ------------------------------------------------------------------ результат разметки
@dataclass
class SegmentAnnotation:
    segment: Segment
    kind: str = "unclassified"
    finding_codes: list[str] = field(default_factory=list)
    signs: list[str] = field(default_factory=list)
    negated_codes: list[str] = field(default_factory=list)
    attributes: dict = field(default_factory=dict)
    highlights: list[dict] = field(default_factory=list)   # [{"type": "size", "text": "Размер: 14 мм"}]
    emergency: bool = False
    uncertain: bool = False
    linked_to: int | None = None          # id пункта заключения, к которому относится деталь описания
    link_reason: str = ""
    not_in_conclusion: bool = False
    sources: list[str] = field(default_factory=lambda: ["rules"])
    # Что во фрагменте нашёл ИИ-агент (проверенные метки): цитата, коды словаря, вид фрагмента.
    # По ним строятся маркеры «словарь и ИИ согласны» и «найдено только ИИ».
    llm_labels: list[dict] = field(default_factory=list)

    @property
    def id(self) -> int:
        return self.segment.id

    @property
    def codes(self) -> set[str]:
        """Коды для порогов и связей: находки словаря + признаки лексикона (sign:*)."""
        return set(self.finding_codes) | {f"sign:{s}" for s in self.signs}

    @property
    def highlight_types(self) -> list[str]:
        return list(dict.fromkeys(h["type"] for h in self.highlights))

    @property
    def significant(self) -> bool:
        return bool(set(self.highlight_types) & SIGNIFICANT_TYPES)

    def highlight(self, kind: str, text: str) -> None:
        if text and not any(h["text"] == text for h in self.highlights):
            self.highlights.append({"type": kind, "text": text})

    def as_dict(self) -> dict:
        s = self.segment
        return {
            "id": s.id, "text": s.text, "start": s.start, "end": s.end, "section": s.section, "organ": s.organ,
            "kind": self.kind, "finding_codes": self.finding_codes, "signs": self.signs,
            "negated_codes": self.negated_codes, "attributes": self.attributes, "highlights": self.highlights,
            "highlight_types": self.highlight_types, "emergency": self.emergency,
            "uncertain": self.uncertain, "linked_to": self.linked_to, "link_reason": self.link_reason,
            "not_in_conclusion": self.not_in_conclusion, "sources": self.sources, "llm_labels": self.llm_labels,
        }


@dataclass(frozen=True)
class AttentionThreshold:
    """Порог внимания (снимок AttentionRule): код находки/признака + условие -> пояснение-подсветка.
    Порог ничего не ранжирует: он лишь объясняет врачу, какое значение превышено."""

    code: str
    finding_code: str
    conditions: tuple
    message: str


@dataclass
class ProtocolAnnotation:
    segments: list[SegmentAnnotation]
    report: GroundingReport
    has_conclusion: bool
    llm: dict = field(default_factory=dict)  # как отработал ИИ-разметчик: status ok | error | off, model, ms, error

    def by_id(self) -> dict[int, SegmentAnnotation]:
        return {a.id: a for a in self.segments}

    def summary(self) -> dict:
        types: dict[str, int] = {}
        for a in self.segments:
            for t in a.highlight_types:
                types[t] = types.get(t, 0) + 1
        highlighted = [a for a in self.segments if a.highlights]
        return {
            "segments": len(self.segments),
            "coverage": 1.0,  # гарантируется verify_coverage, иначе обработка падает
            "highlighted": len(highlighted),
            "highlight_types": types,
            "has_conclusion": self.has_conclusion,
            "has_recommendations": any(a.kind == "recommendation" for a in self.segments),
            # Подсвеченное — в порядке протокола, без ранжирования.
            "highlights": [{"id": a.id, "text": a.segment.text[:300], "codes": sorted(a.codes), "types": a.highlight_types}
                           for a in highlighted if a.significant][:8],
            "emergency": [{"id": a.id, "text": a.segment.text[:300]} for a in self.segments if a.emergency],
            "not_in_conclusion": [{"id": a.id, "text": a.segment.text[:300], "codes": sorted(a.codes)}
                                  for a in self.segments if a.not_in_conclusion],
            "uncertain": [a.id for a in self.segments if a.uncertain and a.significant],
            "signs": sorted({s for a in self.segments for s in a.signs if a.kind in ("abnormal", "finding", "conclusion_item", "scale")}),
            "grounding": self.report.as_dict(),
            "llm": self.llm or {"status": "off"},
        }


# ------------------------------------------------------------------ правила разметки
class RuleSegmentClassifier:
    """Детерминированная разметка одного фрагмента: словарь находок + лексикон + норма."""

    def __init__(self, dictionary: Dictionary, study_type: str = "") -> None:
        self.dictionary, self.study_type = dictionary, study_type.lower()

    def classify(self, seg: Segment) -> SegmentAnnotation:
        a = SegmentAnnotation(segment=seg)
        text = seg.text
        a.attributes = parse_attributes(text)
        a.uncertain = bool(UNCERTAIN_RE.search(text))

        if seg.section == "header":
            return self._set(a, "meta")
        if DISCLAIMER_RE.search(text):
            return self._set(a, "disclaimer")
        if seg.section == "recommendation" or RECOMMENDATION_START_RE.match(text):
            a.attributes["specialties"] = sorted({code for rx, code in self.dictionary.specialties if rx.search(text)})
            return self._set(a, "recommendation")
        if TECH_RE.search(text) and not self._signs(text)[0]:
            return self._set(a, "technical")
        stripped = text.strip()
        if HEADING_RE.match(stripped) or SECTION_LABEL_RE.match(stripped) or (
                # «Объемные образования:» — подпись поля, значение идёт следующими строками.
                stripped.endswith(":") and len(stripped) < 60 and not re.search(r"\d", stripped)) or (
                TITLE_RE.match(stripped) and not self._signs(text)[0]):
            return self._set(a, "heading")

        self._dictionary_findings(a)
        positive_signs, negated_signs = self._signs(text)
        a.signs = positive_signs
        scales = extract_scales(text)
        if scales:
            a.attributes.update(scales)
            a.finding_codes += [f"{scale}_category" for scale in scales if f"{scale}_category" not in a.finding_codes]

        in_conclusion = seg.section == "conclusion"
        found = [d for d in self.dictionary.findings if d.code in a.finding_codes]
        if found:
            a.kind = "finding"
            a.highlight("finding", "Находка из словаря: " + ", ".join(d.title.lower() for d in found))
            if any(d.severity == "emergency" for d in found):
                # Флаг безопасности, а не уровень важности: о такой находке персонал узнаёт сразу.
                a.emergency = True
                a.highlight("emergency", "Экстренная находка: сообщить врачу немедленно")
        elif scales:
            a.kind = "scale"
        elif positive_signs:
            a.kind = "abnormal"
            a.highlight("change", "Изменение: " + ", ".join(SIGN_TITLES[x] for x in positive_signs if x != "us_signs")
                        if set(positive_signs) - {"us_signs"} else "Описаны УЗ-признаки изменений")
        elif negated_signs or a.negated_codes or NORM_RE.search(text):
            a.kind = "norm"
        elif in_conclusion:
            a.kind = "conclusion_item"
            a.highlight("conclusion_item", "Пункт заключения без кода словаря — оценить врачу")
        elif a.attributes.get("size_mm") or re.search(r"\d", text):
            a.kind = "measurement"
        else:
            a.kind = "unclassified"
            a.highlight("unclassified", "Не классифицировано автоматически — прочитать")
        return a

    @staticmethod
    def _set(a: SegmentAnnotation, kind: str) -> SegmentAnnotation:
        a.kind = kind
        return a

    def _dictionary_findings(self, a: SegmentAnnotation) -> None:
        text = a.segment.text
        # Поиск по нормализованному тексту (регистр, «ё», раскладка, опечатки), позиции — в исходном.
        norm = normalize_text(text)
        for definition in self.dictionary.findings:
            if definition.study_types and self.study_type and not any(
                    s.lower() in self.study_type for s in definition.study_types):
                continue
            for pattern in definition.patterns:
                for start, end, _m, _corrected in finditer(pattern, norm):
                    if any(search(ex, norm) for ex in definition.exclude):
                        continue
                    if is_negated(text, start, end):
                        if definition.code not in a.negated_codes:
                            a.negated_codes.append(definition.code)
                    elif definition.code not in a.finding_codes:
                        a.finding_codes.append(definition.code)
        a.negated_codes = [c for c in a.negated_codes if c not in a.finding_codes]

    @staticmethod
    def _signs(text: str) -> tuple[list[str], list[str]]:
        positive, negated = [], []
        for hit in sign_hits(normalize_text(text)):
            target = negated if hit.negated else positive
            if hit.code not in target:
                target.append(hit.code)
        return positive, [c for c in negated if c not in positive]


@dataclass(frozen=True)
class SignHit:
    """Признак лексикона с точной позицией во фрагменте (для маркеров и подсветки)."""

    code: str
    start: int
    end: int
    negated: bool
    corrected: bool
    pattern_index: int


def sign_hits(norm: NormalizedText) -> list[SignHit]:
    """Все признаки лексикона во фрагменте: позиции в исходном тексте, отрицание, исправлена ли опечатка."""
    text = norm.original
    hits = []
    for index, (code, _title, rx, exclude, _sig) in enumerate(SIGN_PATTERNS):
        for start, end, _m, corrected in finditer(rx, norm):
            _clause_start, clause_end = clause_bounds(text, start)
            # Контекст-исключение ищем от начала фрагмента: «подмышечные лимфатические узлы не увеличены,
            # толщина узлов 5 мм» — «узлов» здесь тоже лимфоузлы.
            if exclude and search(exclude, normalize_text(text[:clause_end])):
                continue
            hits.append(SignHit(code, start, end, is_negated(text, start, end), corrected, index))
    return hits


def clause_bounds(text: str, pos: int) -> tuple[int, int]:
    left = max((m.end() for m in CLAUSE_SPLIT.finditer(text, 0, pos)), default=0)
    right = next((m.start() for m in CLAUSE_SPLIT.finditer(text, pos)), len(text))
    return left, right


def is_negated(text: str, start: int, end: int) -> bool:
    """Отрицание в той же части фразы: «образований не выявлено», «не увеличены», «Узловые образования: нет»,
    или общее отрицание перечня: «кисты, дополнительные образования – не определяются»."""
    c_start, c_end = clause_bounds(text, start)
    before, after = text[c_start:start], text[end:c_end]
    return bool(NEGATION_IMMEDIATE.search(before) or NEGATION_BEFORE.search(before)
                or NEGATION_AFTER.search(text[end:end + 30]) or NEGATION_ANY.search(after)
                or _list_negated(text[end:]))


def _list_negated(rest: str) -> bool:
    """Перечень с общим отрицанием в конце предложения. Строго: между находкой и отрицанием нет чисел,
    утвердительных глаголов и других свойств («киста, кровоток в ней не определяется» — не отрицание кисты)."""
    sentence = SENTENCE_END_RE.split(rest, maxsplit=1)[0]
    if len(sentence) > 150 or not LIST_NEGATION_RE.search(sentence):
        return False
    # Сразу за находкой — разделитель перечня или место («… в брюшной полости»), а не её описание
    # («Киста правой доли, перегородки не визуализируются» — отрицаются перегородки, не киста).
    if not (follow := LIST_FOLLOW_RE.match(sentence)):
        return False
    body = LIST_NEGATION_RE.sub("", sentence)
    if re.search(r"\d", body) or POSITIVE_RE.search(body) or OTHER_PROPERTY_RE.search(body):
        return False
    # «Конкременты в просвете, полипы не выявлены»: после места идёт другая находка — отрицается только она.
    if follow.group(2) and any(rx.search(body) for code, _t, rx, _ex, significant in SIGN_PATTERNS if significant):
        return False
    return True


def _num(value: str) -> float:
    return float(value.replace(",", "."))


def parse_attributes(text: str) -> dict:
    """Числовые и качественные атрибуты фрагмента — только то, что написано в тексте."""
    attrs: dict = {}
    dims_all = []
    for m in SIZE_RE.finditer(text):
        factor = 10 if m.group(4).lower() == "см" else 1
        dims = [round(_num(v) * factor, 1) for v in m.groups()[:3] if v]
        dims_all.append(dims)
    if dims_all:
        biggest = max(dims_all, key=max)
        attrs["size_mm"] = max(biggest)
        attrs["dimensions_mm"] = "×".join(f"{d:g}" for d in biggest)
    percents = [int(x) for x in PERCENT_RE.findall(text) if int(x) <= 100]
    if percents:
        attrs["percent"] = max(percents)
    if m := COUNT_RE.search(text):
        attrs["count"] = m.group(1) if m.group(1) else m.group(0).lower()
    if SIDE_BOTH.search(text):
        attrs["side"] = "both"
    elif SIDE_RIGHT.search(text) and SIDE_LEFT.search(text):
        attrs["side"] = "both"
    elif SIDE_RIGHT.search(text):
        attrs["side"] = "right"
    elif SIDE_LEFT.search(text):
        attrs["side"] = "left"
    return attrs


# Родственные признаки: как пункт заключения называется в описании. Ключ — код находки словаря
# или признак (sign:*), значение — признаки описания, которые относятся к тому же изменению.
# Связь действует только в пределах одного органа (см. segment_organs / organ_keys).
RELATED_SIGNS: dict[str, tuple[str, ...]] = {
    "uterine_myoma": ("myoma", "nodule", "mass"),
    "submucous_myoma": ("myoma", "nodule", "mass"),
    "thyroid_nodule": ("nodule", "mass"),
    "breast_mass": ("mass", "nodule"),
    "sign:nodule": ("mass",),
    "sign:mass": ("nodule",),
    "hydronephrosis": ("hydro", "dilation"),
    "arterial_stenosis": ("stenosis", "plaque"),
    "stenotic_atherosclerosis": ("stenosis", "plaque"),
    "dvt": ("thrombus",),
    "sign:myoma": ("nodule", "mass"),
    "endometrial_polyp": ("polyp", "mass"),
    "gallbladder_polyp": ("polyp", "mass"),
    "sign:polyp": ("mass",),
    "gallstones": ("stone", "mass"),
    "sign:named_focal": ("mass", "nodule"),
    "sign:cholesterosis": ("mass", "polyp"),
    "sign:stone": ("stone",),
    "bph": ("hyperplasia", "nodule", "enlargement"),
    "sign:hyperplasia": ("nodule", "enlargement"),
    "sign:cyst": ("mass",),
    "ovarian_mass": ("cyst", "mass"),
    "varicose_veins": ("varicose", "reflux", "dilation"),
    "sign:varicose": ("reflux", "dilation"),
    "sign:endometriosis": ("heterogeneity", "cyst"),
}
ORGAN_TITLES = {
    "liver": "печень", "gallbladder": "желчный пузырь", "pancreas": "поджелудочная железа", "spleen": "селезёнка",
    "kidney": "почки", "bladder": "мочевой пузырь", "cervix": "шейка матки", "endometrium": "эндометрий",
    "uterus": "матка", "ovary": "яичники", "breast": "молочные железы", "thyroid": "щитовидная железа",
    "prostate": "предстательная железа", "testis": "органы мошонки", "veins": "вены", "arteries": "артерии",
    "lymph": "лимфоузлы",
}


def segment_organs(seg: Segment) -> set[str]:
    """Орган фрагмента описания: из его текста («Шейка матки неоднородная…»), иначе из подписи раздела."""
    return organ_keys(seg.text) or organ_keys(seg.organ)


def _organ_title(keys: set[str]) -> str:
    return ", ".join(sorted(ORGAN_TITLES.get(k, k) for k in keys))


def organ_stems(organ: str) -> set[str]:
    stems = set()
    for word in re.findall(r"[а-яё]+", organ.lower()):
        if len(word) < 4 or word in SIDE_WORDS:
            continue
        stems.add(word[:4] if len(word) > 5 else word[:-1])
    return stems


# ------------------------------------------------------------------ сборка
SIDE_TITLES = {"right": "справа", "left": "слева", "both": "с обеих сторон"}
SCALE_TITLES = {"birads": "BI-RADS", "tirads": "TI-RADS", "orads": "O-RADS"}


class ProtocolAnnotator:
    """Точка входа: текст протокола -> размеченные фрагменты с подсветками (без ранжирования).

    Зависимости внедряются (DIP): словарь, пороги внимания и (необязательно) LLM-классификатор.
    """

    def __init__(self, dictionary: Dictionary, thresholds: list[AttentionThreshold] | None = None,
                 llm_classifier=None, segmenter: Segmenter | None = None) -> None:
        self.dictionary = dictionary
        self.thresholds = thresholds or []
        self.llm = llm_classifier
        self.segmenter = segmenter or Segmenter()

    def _title(self, code: str) -> str:
        return _code_title(code, {f.code: f.title.lower() for f in self.dictionary.findings})

    def annotate(self, text: str, *, study_type: str = "") -> ProtocolAnnotation:
        segments = self.segmenter.split(text)
        rules = RuleSegmentClassifier(self.dictionary, study_type)
        annotations = [rules.classify(seg) for seg in segments]
        report, llm_status = GroundingReport(), {"status": "off"}
        if self.llm is not None:
            report, llm_status = self._apply_llm(annotations, study_type)
        self._apply_thresholds(annotations)
        self._link_details(annotations)
        self._attention(annotations)
        return ProtocolAnnotation(segments=annotations, report=report, llm=llm_status,
                                  has_conclusion=any(a.segment.section == "conclusion" for a in annotations))

    # ---------------------------------------------------------------- LLM (только повышение)
    def _apply_llm(self, annotations: list[SegmentAnnotation], study_type: str) -> tuple[GroundingReport, dict]:
        guard = GroundingGuard({d.code for d in self.dictionary.findings})
        started = time.monotonic()
        status = {"status": "ok", "model": getattr(self.llm, "model_label", "")}
        try:
            labels = self.llm.label([a.segment for a in annotations], study_type=study_type, dictionary=self.dictionary)
        except Exception as exc:  # агент недоступен — остаётся разметка правил
            from .ai_agent import describe_llm_error

            guard.report.reject("llm", "-", f"агент недоступен: {exc}")
            return guard.report, {**status, "status": "error", "error": describe_llm_error(exc),
                                  "ms": round((time.monotonic() - started) * 1000)}
        accepted = guard.verify_labels(labels, {a.id: a.segment for a in annotations}, kinds=KINDS)
        status |= {"ms": round((time.monotonic() - started) * 1000), "labels": len(labels), "accepted": len(accepted),
                   "rejected": len(guard.report.rejected)}
        titles = {d.code: d.title.lower() for d in self.dictionary.findings}
        for a in annotations:
            label = accepted.get(a.id)
            if label is None:
                continue
            codes = [c for c in label.finding_codes if c != "OTHER"]
            if label.evidence_quote and (label.highlight or codes) and label.kind not in ("norm", "meta", "technical",
                                                                                           "disclaimer", "heading"):
                # Всё, что ИИ увидел во фрагменте (и новое, и совпавшее со словарём), — для маркеров по источнику.
                a.llm_labels.append({"quote": label.evidence_quote, "codes": codes, "kind": label.kind,
                                     "highlight": label.highlight, "attributes": dict(label.attributes)})
            new_codes = [c for c in label.finding_codes if c not in a.finding_codes]
            a.finding_codes += new_codes
            for key, value in label.attributes.items():
                a.attributes.setdefault(key, value)
            if label.highlight and (new_codes or not a.highlights):
                # Добавить подсветку можно только с дословной цитатой (проверено GroundingGuard).
                if not label.evidence_quote:
                    guard.report.reject("label", a.id, "подсветка без цитаты")
                    continue
                if a.kind in ("norm", "measurement", "unclassified"):
                    a.kind = label.kind if label.kind in ("finding", "abnormal", "scale") else "abnormal"
                what = ", ".join(titles.get(c, c) for c in new_codes)
                a.highlight("llm", f"ИИ-агент: «{label.evidence_quote}»" + (f" ({what})" if what else ""))
                a.sources.append("llm")
            elif not label.highlight and a.significant:
                # Снять подсветку правил ИИ не может: фрагмент остаётся отмеченным, попытка учитывается.
                guard.report.hidden_ignored += 1
            elif new_codes:
                a.sources.append("llm")
        return guard.report, status

    # ---------------------------------------------------------------- связи описание -> заключение

    def _link_details(self, annotations: list[SegmentAnnotation]) -> None:
        conclusion = [a for a in annotations if a.segment.section == "conclusion"]
        # 1. Шкала риска сразу после пункта заключения относится к нему:
        #    «Образование левой молочной железы. Категория BI-RADS 4.» — одна карточка.
        previous = None
        for a in conclusion:
            if a.kind in ("finding", "abnormal", "conclusion_item"):
                previous = a
            elif a.kind == "scale" and previous is not None and not a.signs and set(a.finding_codes) <= SCALE_CODES:
                a.linked_to, a.link_reason = previous.id, "шкала риска к пункту заключения"
        anchors = [a for a in conclusion if a.kind in ("finding", "scale", "abnormal", "conclusion_item") and a.linked_to is None]
        groups = {anchor.id: [anchor] + [c for c in conclusion if c.linked_to == anchor.id] for anchor in anchors}

        # 2. Детали описания -> пункт заключения. Сильная связь (деталь наследует важность пункта):
        #    общий код находки; общий признак или родственный признак («узел» к «миоме матки») — в том же органе.
        #    Слабая связь (только группировка): тот же орган.
        for a in annotations:
            if a.segment.section != "description" or a.kind not in ("finding", "abnormal", "scale"):
                continue
            organs = segment_organs(a.segment)
            best, reason, strong = None, "", False
            for anchor in anchors:
                members = groups[anchor.id]
                group_codes = set().union(*(m.codes for m in members)) - {"sign:us_signs"}
                group_text = " ".join(m.segment.text for m in members)
                group_organs = organ_keys(group_text)
                # Орган детали неизвестен — не мешает связи; известен — должен быть в пункте заключения.
                same_organ = not organs or not group_organs or bool(organs & group_organs)
                related = {f"sign:{s}" for code in group_codes for s in RELATED_SIGNS.get(code, ())}
                if shared := (set(a.finding_codes) - SCALE_CODES) & group_codes:
                    candidate_reason, candidate_strong = "та же находка: " + ", ".join(sorted(self._title(c) for c in shared)), True
                elif same_organ and (shared := a.codes & group_codes):
                    candidate_reason, candidate_strong = "общий признак: " + ", ".join(sorted(self._title(c) for c in shared)), True
                elif same_organ and (shared := a.codes & related):
                    anchor_titles = sorted(self._title(c) for c in group_codes if set(RELATED_SIGNS.get(c, ())) & {x[5:] for x in shared})
                    candidate_reason = (f"{', '.join(sorted(self._title(c) for c in shared))} — к пункту "
                                        f"«{', '.join(anchor_titles)}»")
                    candidate_strong = True
                elif organs and organs & group_organs:
                    candidate_reason, candidate_strong = f"тот же орган: {_organ_title(organs & group_organs)}", False
                elif not organs and (stems := organ_stems(a.segment.organ)) and any(
                        re.search(r"\b" + stem, group_text.lower()) for stem in stems):
                    candidate_reason, candidate_strong = f"тот же орган: {a.segment.organ.lower()}", False
                else:
                    continue
                # Сильная связь важнее слабой; при равенстве — первый пункт заключения (порядок протокола).
                if best is None or (candidate_strong and not strong):
                    best, reason, strong = anchor, candidate_reason, candidate_strong
            if best is not None:
                a.linked_to, a.link_reason = best.id, reason
            # Значимое изменение без сильной связи не вынесено в заключение — даже если орган там упомянут:
            # «киста печени 15 мм» при заключении «диффузные изменения печени» — пропуск, который нельзя прятать.
            if conclusion and not strong and (set(a.finding_codes) - SCALE_CODES or set(a.signs) & SIGNIFICANT_SIGNS):
                a.not_in_conclusion = True

    # ---------------------------------------------------------------- пороги и пояснения
    def _apply_thresholds(self, annotations: list[SegmentAnnotation]) -> None:
        for a in annotations:
            if a.kind not in ("finding", "abnormal", "scale", "conclusion_item"):
                continue
            for t in self.thresholds:
                if t.finding_code in a.codes and conditions_met(list(t.conditions), a.attributes):
                    value = a.attributes.get(t.conditions[0]["attr"]) if t.conditions else ""
                    a.highlight("rule", t.message.format(value=_fmt(value)))

    def _attention(self, annotations: list[SegmentAnnotation]) -> None:
        for a in annotations:
            if a.kind not in ("finding", "abnormal", "scale", "conclusion_item"):
                continue
            attrs = a.attributes
            # Размер и количество важны для очаговых изменений (образование, узел, конкремент, полип…),
            # а не для толщины стенки или параметров органа.
            focal = bool(set(a.finding_codes) - SCALE_CODES or set(a.signs) & SIGNIFICANT_SIGNS)
            if focal and attrs.get("size_mm"):
                dims = attrs.get("dimensions_mm", "")
                a.highlight("size", f"Размер: {dims.replace('.', ',')} мм" if "×" in dims else f"Размер: {_fmt(attrs['size_mm'])} мм")
            if focal and attrs.get("count"):
                a.highlight("count", f"Количество: {attrs['count']}")
            if attrs.get("percent"):
                a.highlight("percent", f"Процент (стеноз/доля): {attrs['percent']}%")
            for scale, title in SCALE_TITLES.items():
                if scale in attrs:
                    a.highlight("scale", f"{title} {attrs[scale]}")
            if attrs.get("side"):
                a.highlight("side", f"Сторона: {SIDE_TITLES[attrs['side']]}")
            if a.uncertain:
                a.highlight("uncertain", "Формулировка с сомнением («?», «нельзя исключить», «по типу») — уточнить")
            if a.not_in_conclusion:
                a.highlight("not_in_conclusion", "Есть в описании, но не вынесено в заключение")
            if a.negated_codes and a.kind != "norm":
                a.highlight("negation_nearby", "В той же фразе есть отрицание: " + ", ".join(self._title(c) for c in a.negated_codes))


def _fmt(value) -> str:
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:g}".replace(".", ",")
    return str(value)


def _code_title(code: str, finding_titles: dict[str, str] | None = None) -> str:
    if code.startswith("sign:"):
        return SIGN_TITLES.get(code.removeprefix("sign:"), code)
    return (finding_titles or {}).get(code) or code


# ------------------------------------------------------------------ LLM-разметчик (LangChain)
SEGMENT_SYSTEM_PROMPT = """Ты — ассистент врача. Ты размечаешь фрагменты протокола УЗИ: что это за фрагмент
и стоит ли подсветить его врачу. Ты НЕ ранжируешь по важности, НЕ ставишь диагноз,
НЕ пересказываешь и НЕ дополняешь текст.

Жёсткие правила:
1. Работай только с фрагментами из списка. Верни метки ТОЛЬКО для фрагментов, где есть находка, изменение,
   шкала, пункт заключения или сомнение: по одной метке с segment_id. Норму, заголовки, шапку, аппарат,
   дисклеймер и рекомендации НЕ перечисляй: их разметит словарь. Короткий ответ — быстрый ответ.
2. Не придумывай фрагменты, номера, находки и числа. Атрибуты (size_mm, percent, birads, tirads, orads) —
   только числа, которые буквально написаны в этом фрагменте.
3. evidence_quote — дословная часть фрагмента, на которой основана метка (или пусто для нормы).
4. finding_codes — только коды из справочника ниже; если подходящего нет — OTHER.
5. kind: finding | scale | abnormal | conclusion_item | norm | measurement | recommendation | meta | technical |
   disclaimer | heading | unclassified.
6. highlight: true — во фрагменте есть изменение, находка или сомнение, которое врач должен увидеть;
   тогда evidence_quote обязателен. Отрицание («не выявлено») — kind norm, highlight false.

Справочник находок:
{finding_codes}"""

SEGMENT_HUMAN_PROMPT = """Тип исследования: {study_type}

Фрагменты (номер, раздел, текст):
{segments}"""


class LangChainSegmentClassifier:
    """LLM-разметчик фрагментов. Ответ проходит GroundingGuard: всё непроверяемое отбрасывается."""

    def __init__(self, llm, model_label: str = "") -> None:
        self.llm = llm
        self.model_label = model_label

    def label(self, segments: list[Segment], *, study_type: str, dictionary: Dictionary) -> list:
        from langchain_core.prompts import ChatPromptTemplate

        from .ai_agent import call_llm
        from .schemas import SegmentLabeling

        prompt = ChatPromptTemplate.from_messages([("system", SEGMENT_SYSTEM_PROMPT), ("human", SEGMENT_HUMAN_PROMPT)])
        chain = prompt | self.llm.with_structured_output(SegmentLabeling)
        result = call_llm(lambda: chain.invoke({
            "finding_codes": dictionary.finding_codes_for_prompt(),
            "study_type": study_type or "не указан",
            "segments": "\n".join(f"[{s.id}] ({s.section}) {s.text}" for s in segments),
        }))
        return list(result.labels)


class _UnavailableClassifier:
    """ИИ-агент настроен, но не поднялся: разметка по правилам, ошибка попадает в статус разметки."""

    def __init__(self, error: str, model_label: str) -> None:
        self.error, self.model_label = error, model_label

    def label(self, *_args, **_kwargs) -> list:
        raise RuntimeError(self.error)


def load_thresholds() -> list[AttentionThreshold]:
    from apps.processing.models import AttentionRule

    return [AttentionThreshold(code=r.code, finding_code=r.finding_code, conditions=tuple(r.conditions or ()),
                               message=r.message)
            for r in AttentionRule.objects.filter(is_active=True)]


def get_annotator(dictionary: Dictionary) -> ProtocolAnnotator:
    """Точка сборки: LLM-разметчик подключается, только если агент настроен."""
    from .ai_agent import build_chat_model, model_label

    llm = None
    try:
        model = build_chat_model()
        llm = LangChainSegmentClassifier(model, model_label()) if model is not None else None
    except Exception as exc:  # без LLM разметка остаётся полной (правила), причина видна в статусе
        llm = _UnavailableClassifier(f"модель не подключилась: {exc}", model_label())
    return ProtocolAnnotator(dictionary, thresholds=load_thresholds(), llm_classifier=llm)


