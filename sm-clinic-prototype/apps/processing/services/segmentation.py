"""
Разбиение протокола на фрагменты (сегменты) без потери текста.

Сегмент — одно предложение или строка «Параметр: значение» с точными границами в исходном
тексте (start/end). Разметка важности работает поверх сегментов, а не поверх пересказа,
поэтому исходный текст протокола сохраняется полностью: любой сегмент можно показать
дословно, а verify_coverage() доказывает, что каждый непробельный символ протокола
попал ровно в один сегмент («ничего не выкинуто»).

Разделы протокола:
  header      — шапка 1С (карта, дата, врач, ФИО);
  description — описание по органам;
  conclusion  — заключение;
  recommendation — рекомендации врача-диагноста;
  tail        — дисклеймер, данные аппарата и прочее после заключения.
"""
import re
from dataclasses import dataclass

from .ai_agent import DISCLAIMER_RE, find_conclusion_header


class SegmentationError(Exception):
    """Нарушена полнота разбиения — обработка останавливается, а не теряет текст молча."""


@dataclass(frozen=True)
class Segment:
    id: int          # порядковый номер = исходный порядок в протоколе
    text: str        # дословно text[start:end]
    start: int
    end: int
    section: str     # header | description | conclusion | recommendation | tail
    organ: str = ""  # ближайший заголовок органа («Желчный пузырь», «Правый яичник»)


# Строки шапки выгрузки 1С. Ячейки таблицы склеены через « | ».
HEADER_LINE_RE = re.compile(
    r"(\s\|\s|^(Амбулаторная\sкарта|Пр[иё]ем\sврача|Дата\sпри[её]ма|Дата\sрождения|Дата\sвыполнения|ФИО|Пациент|"
    r"Врач\b|Время|Протокол\sобследования|Номер\sкарты|Описание\s*$|Заключение\s*$|Пол\b))",
    re.IGNORECASE,
)
# Начало рекомендаций: «Рекомендовано: …», «РЕКОМЕНДОВАНО» отдельной строкой, «Рекомендации:».
# «Назначения» / «Назначенные услуги» — блок назначений 1С после заключения (тоже то, что врач рекомендует дальше).
RECOMMENDATION_START_RE = re.compile(
    r"^\W{0,2}(Рекомендован\w*|Рекомендаци\w*|Рекоменду\w*|РЕКОМЕНДОВАНО|Рек-но|Рекомендовнао|Назначени\w*\s*:?\s*$|Назначенные\s+услуги)",
    re.IGNORECASE,
)
# Заголовок органа: строка ПРОПИСНЫМИ («ПРАВЫЙ ЯИЧНИК», «ШЕЙКА МАТКИ:»).
HEADING_RE = re.compile(r"^[А-ЯЁA-Z][А-ЯЁA-Z\s\-/,]{2,60}:?$")
# Орган в начале строки: «ЖЕЛЧНЫЙ ПУЗЫРЬ: Расположение обычное».
INLINE_ORGAN_RE = re.compile(r"^([А-ЯЁ][А-ЯЁ\s\-]{3,40}):")
# Граница предложения: знак препинания + пробел + заглавная буква/цифра/скобка.
SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?;])\s+(?=[А-ЯЁA-Z«(\d])")
NOT_ORGAN_WORDS = re.compile(r"ИССЛЕДОВАН|ЗАКЛЮЧЕН|РЕКОМЕНД|ОПИСАНИ|ПРОТОКОЛ", re.IGNORECASE)
# Продолжение предложения с новой строки: начинается со строчной буквы.
CONTINUATION_RE = re.compile(r"^[а-яёa-z]")
# Первые два слова строки — кандидат в название органа.
LEADING_WORDS_RE = re.compile(r"^([А-ЯЁ][а-яё]+(?:\s+[а-яё]+)?)")
# Подпись поля обычным регистром («Желчный пузырь: размером …») — органом считается, только если в ней есть орган.
LABEL_RE = re.compile(r"^([А-ЯЁ][А-ЯЁа-яё\s\-]{3,40}):")

# Органы и анатомические зоны: ключ -> шаблон. Нужны, чтобы связывать деталь описания с пунктом
# заключения только в пределах одного органа (киста шейки матки не «прилипает» к миоме тела матки).
ORGAN_PATTERNS: dict[str, re.Pattern] = {key: re.compile(rx, re.IGNORECASE) for key, rx in {
    "liver": r"печен|гепат",
    "gallbladder": r"желчн\w*\s+пузыр|\bЖП\b|холецист|холедох|желчн",
    "pancreas": r"поджелуд|панкреат",
    "spleen": r"селез[её]н|спленом",
    "kidney": r"почк|почеч|нефр|пиело|чашечн",
    "bladder": r"мочев\w*\s+пузыр",
    "cervix": r"шейк\w*\s+матк|цервик|эндоцерв|наботов",
    "endometrium": r"эндометри(?!оз|оидн)|полост\w*\s+матк|\bМ-?\s?эхо",
    "uterus": r"(?<!шейки )(?<!шейка )(?<!шейке )(?<!шейку )матк|миометр|\bмиом|лейомиом",
    "ovary": r"яичник|фолликул|O-?RADS|эндометриоидн",
    "breast": r"молочн|BI-?RADS|мастопат|ареол",
    "thyroid": r"щитовид|TI-?RADS|тиреоид|перешей",
    "prostate": r"предстат|простат|ДГПЖ|транзитор|переходн\w*\s+зон",
    "testis": r"яичк|мошонк|придат",
    "veins": r"\bвен\w*|венозн|\bБПВ\b|\bМПВ\b|варико|перфорант",
    "arteries": r"артери|аорт|сонн\w*|\bБЦА\b|\bОСА\b|\bВСА\b",
    "lymph": r"лимф",
}.items()}


def organ_keys(text: str) -> set[str]:
    """Органы, упомянутые в тексте (ключи ORGAN_PATTERNS)."""
    return {key for key, rx in ORGAN_PATTERNS.items() if rx.search(text or "")}


def _organ_title(raw: str) -> str:
    raw = raw.strip(" :").lower()
    return raw[:1].upper() + raw[1:]


class Segmenter:
    """Режет текст на сегменты и определяет раздел и орган для каждого."""

    def split(self, text: str) -> list[Segment]:
        segments: list[Segment] = []
        stage, organ = "body", ""  # body -> conclusion -> recommendation / tail
        header = find_conclusion_header(text)
        conclusion_at = header.start() if header else -1

        for line_begin, line_end in self._logical_lines(text):
            raw = text[line_begin:line_end]
            stripped = raw.strip()
            if not stripped:
                continue
            line_start = line_begin + (len(raw) - len(raw.lstrip()))
            if stage == "body" and line_begin <= conclusion_at < line_end:
                stage = "conclusion"

            for part_start, part_end in self._sentences(stripped):
                start, end = line_start + part_start, line_start + part_end
                piece = text[start:end]
                if RECOMMENDATION_START_RE.match(piece):
                    stage = "recommendation"
                elif DISCLAIMER_RE.search(piece):
                    # Дисклеймер завершает заключение/рекомендации: дальше «хвост» протокола.
                    stage = "tail"
                if stage == "body":
                    # Строки шапки 1С могут идти и после названия исследования — проверяем каждую.
                    section = "header" if HEADER_LINE_RE.search(stripped) else "description"
                    if section == "description" and part_start == 0:
                        organ = self._organ(stripped) or organ
                else:
                    section = stage
                segments.append(Segment(id=len(segments) + 1, text=piece, start=start, end=end, section=section,
                                        organ=organ if section == "description" else ""))
        verify_coverage(text, segments)
        return segments

    @staticmethod
    def _logical_lines(text: str) -> list[tuple[int, int]]:
        """Строки протокола с переносами внутри предложения, склеенные обратно: если строка не
        закончена знаком препинания, а следующая начинается со строчной буквы, это одно предложение
        («…изоэхогенное образование с четкими» + «ровными контурами 8,5*6,3 мм.»)."""
        lines = [(m.start(), m.end()) for m in re.finditer(r"[^\n]+", text) if m.group(0).strip()]
        merged: list[list[int]] = []
        for start, end in lines:
            current = text[start:end].strip()
            if merged:
                previous = text[merged[-1][0]:merged[-1][1]].rstrip()
                if CONTINUATION_RE.match(current) and not previous.endswith((".", "!", "?", ";", ":")) \
                        and not HEADER_LINE_RE.search(previous) and not RECOMMENDATION_START_RE.match(current):
                    merged[-1][1] = end
                    continue
            merged.append([start, end])
        return [(start, end) for start, end in merged]

    @staticmethod
    def _organ(line: str) -> str:
        if HEADING_RE.match(line) and not NOT_ORGAN_WORDS.search(line):
            return _organ_title(line)
        if (m := INLINE_ORGAN_RE.match(line)) and not NOT_ORGAN_WORDS.search(m.group(1)):
            return _organ_title(m.group(1))
        if (m := LABEL_RE.match(line)) and organ_keys(m.group(1)) and not NOT_ORGAN_WORDS.search(m.group(1)):
            return _organ_title(m.group(1))
        # Строка начинается с органа без двоеточия: «Предстательная железа увеличена …».
        if (m := LEADING_WORDS_RE.match(line)) and organ_keys(m.group(1)) and not NOT_ORGAN_WORDS.search(m.group(1)):
            return _organ_title(m.group(1))
        return ""

    @staticmethod
    def _sentences(line: str) -> list[tuple[int, int]]:
        bounds, pos = [], 0
        for m in SENTENCE_BOUNDARY_RE.finditer(line):
            bounds.append((pos, m.start()))
            pos = m.end()
        bounds.append((pos, len(line)))
        return [(s, e) for s, e in bounds if line[s:e].strip()]


def verify_coverage(text: str, segments: list[Segment]) -> float:
    """Инвариант «ничего не выкинуто»: сегменты не пересекаются, совпадают с исходником
    дословно и покрывают все непробельные символы. Возвращает долю покрытия (всегда 1.0)."""
    covered = [False] * len(text)
    for seg in segments:
        if text[seg.start:seg.end] != seg.text:
            raise SegmentationError(f"Сегмент {seg.id} не совпадает с исходным текстом")
        for i in range(seg.start, seg.end):
            if covered[i]:
                raise SegmentationError(f"Сегмент {seg.id} пересекается с предыдущим")
            covered[i] = True
    lost = [i for i, ch in enumerate(text) if not ch.isspace() and not covered[i]]
    if lost:
        sample = text[lost[0]: lost[0] + 40]
        raise SegmentationError(f"Не попало в разметку {len(lost)} символов, например «{sample}»")
    return 1.0
