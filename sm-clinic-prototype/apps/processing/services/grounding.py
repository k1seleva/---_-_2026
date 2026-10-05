"""
Контроль достоверности ответов AI-агента («заземление» на текст протокола).

Агент работает только с тем, что есть в протоколе: не придумывает и не выкидывает.
Это обеспечивается не просьбой в промпте, а проверкой каждого ответа кодом:

* «Не придумывать»:
  - находка/метка без дословной цитаты из протокола отбрасывается;
  - число в атрибутах (размер, процент, категория) принимается, только если оно есть в цитате/фрагменте;
  - код находки — только из словаря (или OTHER), специальность — только из справочника;
  - ссылка на несуществующий фрагмент отбрасывается.
* «Не выкидывать»:
  - агент размечает заранее нарезанные фрагменты, а не пересказывает текст;
  - фрагмент, который агент пропустил, сохраняет разметку правил (и попадает в отчёт);
  - агент может только ПОВЫСИТЬ важность фрагмента, понизить или скрыть — нет.

Всё отброшенное попадает в GroundingReport и сохраняется вместе с результатом (аудит).
"""
import re
from dataclasses import asdict, dataclass, field

NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
SCALE_CODES = {"birads_category", "tirads_category", "orads_category"}


def normalize(text: str) -> str:
    return " ".join((text or "").split()).lower().replace("ё", "е")


def quote_in(quote: str, source: str) -> bool:
    q = normalize(quote)
    return bool(q) and q in normalize(source)


def number_in(value, source: str) -> bool:
    """Число из ответа агента должно буквально встречаться в источнике (15 == «15», «15,0», «15.0»)."""
    try:
        target = float(value)
    except (TypeError, ValueError):
        return False
    return any(float(n.replace(",", ".")) == target for n in NUMBER_RE.findall(source or ""))


@dataclass
class GroundingReport:
    llm_used: bool = False
    accepted: int = 0
    rejected: list[dict] = field(default_factory=list)       # {"what", "ref", "reason"}
    missing_segments: list[int] = field(default_factory=list)
    hidden_ignored: int = 0                                   # попытки ИИ снять подсветку правил

    def reject(self, what: str, ref, reason: str) -> None:
        self.rejected.append({"what": what, "ref": str(ref), "reason": reason})

    def as_dict(self) -> dict:
        data = asdict(self)
        data["rejected_count"] = len(self.rejected)
        return data

    def merge(self, other: "GroundingReport") -> "GroundingReport":
        self.llm_used = self.llm_used or other.llm_used
        self.accepted += other.accepted
        self.rejected += other.rejected
        self.missing_segments += other.missing_segments
        self.hidden_ignored += other.hidden_ignored
        return self


class GroundingGuard:
    """Проверка ответов агента против исходного текста и справочников."""

    def __init__(self, allowed_codes: set[str], allowed_specialties: set[str] | None = None,
                 report: GroundingReport | None = None) -> None:
        self.allowed_codes = set(allowed_codes) | SCALE_CODES | {"OTHER"}
        self.allowed_specialties = set(allowed_specialties or ())
        self.report = report or GroundingReport(llm_used=True)

    # ------------------------------------------------------------------ числовые атрибуты
    def clean_attributes(self, attributes: dict, source: str, ref) -> dict:
        clean = {}
        for key, value in (attributes or {}).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                clean[key] = value  # строковые атрибуты (сторона) не несут чисел
                continue
            if number_in(value, source):
                clean[key] = value
            else:
                self.report.reject("attribute", ref, f"{key}={value}: числа нет в тексте протокола")
        return clean

    # ------------------------------------------------------------------ находки экстрактора
    def verify_findings(self, findings: list, text: str) -> list:
        verified = []
        for f in findings:
            if not quote_in(f.evidence_quote, text):
                self.report.reject("finding", f.code, "нет дословной цитаты в протоколе")
                continue
            if f.code not in self.allowed_codes:
                self.report.reject("finding", f.code, "код вне словаря находок")
                continue
            f.attributes = self.clean_attributes(f.attributes, f.evidence_quote, f.code)
            raw_idx = text.lower().find(f.evidence_quote.lower())
            # Цитата совпала с точностью до пробелов — находку принимаем, но без подсветки.
            f.span_start, f.span_end = (raw_idx, raw_idx + len(f.evidence_quote)) if raw_idx >= 0 else (None, None)
            verified.append(f)
            self.report.accepted += 1
        return verified

    def verify_recommendations(self, recommendations: list, text: str) -> list:
        verified = []
        for r in recommendations:
            if not quote_in(r.text, text):
                self.report.reject("recommendation", r.text[:60], "текста рекомендации нет в протоколе")
                continue
            if r.specialty_code and r.specialty_code not in self.allowed_specialties:
                self.report.reject("recommendation", r.text[:60], f"специальность {r.specialty_code} вне справочника")
                r.specialty_code = None
            if r.interval_days and not number_in_interval(r.text, r.interval_days):
                self.report.reject("recommendation", r.text[:60], f"срок {r.interval_days} дн. не следует из текста")
                r.interval_days = None
            verified.append(r)
            self.report.accepted += 1
        return verified

    # ------------------------------------------------------------------ метки фрагментов
    def verify_labels(self, labels: list, segments: dict, *, kinds: set[str]) -> dict:
        """segments: {id: Segment}. Возвращает {id: проверенная метка}."""
        accepted: dict = {}
        for label in labels:
            seg = segments.get(label.segment_id)
            if seg is None:
                self.report.reject("label", label.segment_id, "ссылка на несуществующий фрагмент")
                continue
            if label.segment_id in accepted:
                self.report.reject("label", label.segment_id, "повторная метка фрагмента")
                continue
            if label.kind not in kinds:
                self.report.reject("label", label.segment_id, f"недопустимый kind: {label.kind}")
                continue
            if label.evidence_quote and not quote_in(label.evidence_quote, seg.text):
                self.report.reject("label", label.segment_id, "цитата не найдена во фрагменте")
                continue
            codes = []
            for code in label.finding_codes:
                if code in self.allowed_codes:
                    codes.append(code)
                else:
                    self.report.reject("code", label.segment_id, f"код {code} вне словаря")
            label.finding_codes = codes
            label.attributes = self.clean_attributes(label.attributes, seg.text, label.segment_id)
            accepted[label.segment_id] = label
            self.report.accepted += 1
        self.report.missing_segments += [sid for sid in segments if sid not in accepted]
        return accepted


INTERVAL_UNITS = {"дн": 1, "нед": 7, "мес": 30, "год": 365, "лет": 365}
INTERVAL_ANY_RE = re.compile(r"(\d+)\s*(дн|нед|мес|год|лет)", re.IGNORECASE)


def number_in_interval(text: str, days: int) -> bool:
    """Срок рекомендации должен вычисляться из текста: «через 6 мес» -> 180."""
    return any(int(n) * INTERVAL_UNITS[u.lower()] == days for n, u in INTERVAL_ANY_RE.findall(text or ""))
