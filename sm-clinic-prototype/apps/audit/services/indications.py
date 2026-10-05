"""
Матрица показаний: какие консультации и обследования следуют из находок протокола.

Детерминированно и объяснимо: каждое показание несёт правило (код + версия), находку,
дословную цитату и раздел протокола, из которого она взята. LLM здесь не участвует.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field

from common.conditions import conditions_met, describe

SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2, "info": 3}


@dataclass(frozen=True)
class IndicationSpec:
    """Снимок IndicationRule (экстрактор не ходит в ORM — легко тестировать и вынести в сервис)."""

    code: str
    title: str
    finding_code: str
    requirement: str
    conditions: tuple = ()
    specialty_code: str = ""
    accepted_specialties: tuple = ()
    service_pattern: str = ""
    service_title: str = ""
    max_days: int | None = None
    severity: str = "major"
    rationale: str = ""
    version: int = 1


@dataclass
class Evidence:
    finding_code: str
    label: str
    quote: str
    attributes: dict
    uncertain: bool = False
    source: str = "conclusion"   # conclusion | description | ...


@dataclass
class Indication:
    spec: IndicationSpec
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def key(self) -> tuple:
        return (self.spec.requirement, self.spec.specialty_code or self.spec.service_title)

    @property
    def accepted(self) -> set[str]:
        return {self.spec.specialty_code, *self.spec.accepted_specialties} - {""}

    @property
    def main(self) -> Evidence:
        return self.evidence[0]

    @property
    def only_description(self) -> bool:
        return all(e.source != "conclusion" for e in self.evidence)

    @property
    def uncertain(self) -> bool:
        return all(e.uncertain for e in self.evidence)

    def matches_service(self, text: str) -> bool:
        return bool(self.spec.service_pattern and re.search(self.spec.service_pattern, text or "", re.IGNORECASE))

    def as_dict(self) -> dict:
        s = self.spec
        return {
            "rule_code": s.code, "rule_version": s.version, "title": s.title, "requirement": s.requirement,
            "specialty_code": s.specialty_code, "accepted_specialties": sorted(self.accepted),
            "service_title": s.service_title, "max_days": s.max_days, "severity": s.severity,
            "rationale": s.rationale, "conditions": describe(list(s.conditions)),
            "evidence": [e.__dict__ for e in self.evidence],
        }


class IndicationMatrix:
    def __init__(self, specs: list[IndicationSpec] | None = None) -> None:
        self.specs = specs if specs is not None else load_specs()

    @property
    def version(self) -> str:
        raw = json.dumps([[s.code, s.version, list(s.conditions), s.max_days, s.severity] for s in self.specs],
                         ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode()).hexdigest()[:10]

    def match(self, evidence: list[Evidence]) -> list[Indication]:
        """Одно показание на (тип, специалист/обследование): при нескольких основаниях —
        самое строгое правило (тяжесть, затем срок), все цитаты сохраняются."""
        result: dict[tuple, Indication] = {}
        for ev in evidence:
            for spec in self.specs:
                if spec.finding_code != ev.finding_code or not conditions_met(list(spec.conditions), ev.attributes):
                    continue
                candidate = Indication(spec=spec, evidence=[ev])
                current = result.get(candidate.key)
                if current is None:
                    result[candidate.key] = candidate
                    continue
                if _stricter(spec, current.spec):
                    candidate.evidence += [e for e in current.evidence if e not in candidate.evidence]
                    result[candidate.key] = candidate
                elif ev not in current.evidence:
                    current.evidence.append(ev)
        for ind in result.values():
            # Основание из заключения — первым (оно весомее строки описания).
            ind.evidence.sort(key=lambda e: (e.source != "conclusion", e.uncertain))
        return sorted(result.values(), key=lambda i: (SEVERITY_RANK.get(i.spec.severity, 9), i.spec.max_days or 999))


def _stricter(a: IndicationSpec, b: IndicationSpec) -> bool:
    return (SEVERITY_RANK.get(a.severity, 9), a.max_days or 999) < (SEVERITY_RANK.get(b.severity, 9), b.max_days or 999)


def load_specs() -> list[IndicationSpec]:
    from ..models import IndicationRule

    return [
        IndicationSpec(
            code=r.code, title=r.title, finding_code=r.finding_code, requirement=r.requirement,
            conditions=tuple(r.conditions or ()), specialty_code=r.specialty_code,
            accepted_specialties=tuple(r.accepted_specialties or ()), service_pattern=r.service_pattern,
            service_title=r.service_title, max_days=r.max_days, severity=r.severity, rationale=r.rationale,
            version=r.version,
        )
        for r in IndicationRule.objects.filter(is_active=True)
    ]


def evidence_from_document(findings: list[dict], segments: list[dict]) -> list[Evidence]:
    """Основания для показаний: находки заключения (экстрактор) + размеченные фрагменты
    (в т.ч. из описания — например, конкременты, не вынесенные в заключение)."""
    evidence: list[Evidence] = []
    seen: set[tuple] = set()
    for f in findings:
        if f.get("negated"):
            continue
        key = (f["code"], f.get("evidence_quote", ""))
        if key not in seen:
            seen.add(key)
            evidence.append(Evidence(finding_code=f["code"], label=f.get("label", f["code"]), quote=f.get("evidence_quote", ""),
                                     attributes=f.get("attributes") or {}, uncertain=bool(f.get("uncertain"))))
    for s in segments:
        if s.get("kind") not in ("finding", "abnormal", "scale", "conclusion_item"):
            continue
        codes = list(s.get("finding_codes", [])) + [f"sign:{x}" for x in s.get("signs", [])]
        for code in codes:
            key = (code, s["text"])
            if key in seen or any(e.finding_code == code and e.quote and e.quote in s["text"] for e in evidence):
                continue
            seen.add(key)
            evidence.append(Evidence(finding_code=code, label=code, quote=s["text"], attributes=s.get("attributes") or {},
                                     uncertain=bool(s.get("uncertain")), source=s.get("section", "")))
    return evidence
