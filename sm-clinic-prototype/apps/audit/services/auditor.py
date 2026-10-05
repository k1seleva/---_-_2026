"""
Проверка достаточности рекомендаций: показания (из находок) против того, что рекомендовал врач.

Логика (детерминированная, каждое замечание с основанием):
1. Показание к консультации закрыто, если в рекомендациях есть специалист из списка допустимых
   (гинеколог или оперирующий гинеколог) — или пациент уже на приёме у такого специалиста.
2. Показание к обследованию закрыто, если в рекомендациях есть услуга по шаблону (ТАБ, биопсия).
3. Срок рекомендации не должен быть позже допустимого (BI-RADS 4: не «через 6 месяцев»).
4. Расплывчатая рекомендация («консультация лечащего врача») не закрывает показание к конкретному
   специалисту — это отражается в пояснении.
5. Изменение из описания, не вынесенное в заключение, — замечание к протоколу (без правки маршрута).
6. Рекомендация без основания в протоколе — информация, не ошибка: у врача могут быть свои причины.

Учитывается и текущий маршрут: если рекомендация неполная, но этап уже есть в маршруте (матрица
маршрутизации добавила консультацию хирурга), правка маршрута не нужна — это замечание к протоколу
(minor). На обсуждение (major/critical) уходит только то, чего нет ни в рекомендациях, ни в маршруте.

Вердикт: есть critical/major — «недостаточно» (обсуждение координатором), есть minor — «есть замечания»,
иначе — «достаточно». Решение о правке маршрута принимает человек (координатор), не алгоритм.
"""
from collections.abc import Callable
from dataclasses import dataclass, field

from .indications import SEVERITY_RANK, Indication, IndicationMatrix
from .recommendations import RecommendationItem


@dataclass
class IssueDraft:
    issue_type: str
    severity: str
    message: str
    finding_code: str = ""
    evidence_quote: str = ""
    specialty_code: str = ""
    rule_code: str = ""
    proposed_operation: dict | None = None
    short: str = ""          # краткая формулировка для списка координатора

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Coverage:
    """Что уже закрыто помимо рекомендаций: специалист текущего приёма и этапы открытого маршрута."""

    visit_specialties: set[str] = field(default_factory=set)
    route_specialties: set[str] = field(default_factory=set)
    route_services: list[str] = field(default_factory=list)   # названия этапов-обследований маршрута


@dataclass
class AuditOutcome:
    verdict: str
    summary: str
    issues: list[IssueDraft] = field(default_factory=list)
    indications: list[Indication] = field(default_factory=list)
    items: list[RecommendationItem] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict, "summary": self.summary,
            "issues": [i.as_dict() for i in self.issues],
            "indications": [i.as_dict() for i in self.indications],
            "items": [i.as_dict() for i in self.items],
        }


class RecommendationAuditor:
    def __init__(self, matrix: IndicationMatrix, genitive: Callable[[str], str] = lambda code: code) -> None:
        self.matrix = matrix
        self.genitive = genitive

    def audit(self, *, evidence: list, items: list[RecommendationItem], source: str = "protocol",
              coverage: Coverage | None = None, not_in_conclusion: list[dict] | None = None) -> AuditOutcome:
        covered = coverage or Coverage()
        indications = self.matrix.match(evidence)
        issues: list[IssueDraft] = []
        vague = [i for i in items if i.vague]
        concrete = [i for i in items if not i.vague]

        for ind in indications:
            if ind.spec.requirement == "consultation":
                issues += self._check_consultation(ind, concrete, vague, covered, source)
            else:
                issues += self._check_diagnostics(ind, concrete, covered, source)

        if source == "protocol":
            if indications and not items:
                issues.append(IssueDraft(
                    "no_recommendations", "minor",
                    f"В протоколе нет рекомендаций, хотя по находкам есть показания ({len(indications)})."))
            for seg in not_in_conclusion or []:
                issues.append(IssueDraft(
                    "not_in_conclusion", "minor",
                    f"В описании есть изменение, не вынесенное в заключение: «{_short(seg['text'])}». "
                    "Проверьте протокол: заключение — основа для маршрута.",
                    evidence_quote=seg["text"]))
        issues += self._unsupported(indications, concrete)

        issues.sort(key=lambda i: SEVERITY_RANK.get(i.severity, 9))
        verdict = self._verdict(issues)
        return AuditOutcome(verdict=verdict, summary=self._summary(verdict, indications, issues),
                            issues=issues, indications=indications, items=items)

    # ------------------------------------------------------------------ проверки
    def _check_consultation(self, ind: Indication, items, vague, covered: Coverage, source: str) -> list[IssueDraft]:
        spec = ind.spec
        who = self.genitive(spec.specialty_code)
        if ind.accepted & covered.visit_specialties:
            return []  # пациент уже на приёме у нужного специалиста
        matching = [i for i in items if i.specialty_code in ind.accepted]
        in_route = bool(ind.accepted & covered.route_specialties)
        if matching:
            return self._check_timing(ind, matching, f"Консультация {who}", in_route)
        if vague:
            tail = f"В рекомендациях указано «{_short(vague[0].text)}» без профиля специалиста."
        elif items:
            tail = "В рекомендациях этого специалиста нет."
        else:
            tail = "Рекомендаций нет."
        base = f"Есть показание к консультации {who}: {self._basis(ind)} {tail}"
        if in_route:
            return [] if source == "visit" else [IssueDraft(
                "missing_consultation", "minor",
                f"{base} Маршрут по матрице уже включает консультацию {who} — правка маршрута не нужна, "
                "замечание к полноте рекомендаций в протоколе.",
                finding_code=ind.main.finding_code, evidence_quote=ind.main.quote, specialty_code=spec.specialty_code,
                rule_code=spec.code)]
        return [IssueDraft(
            "missing_consultation", spec.severity,
            f"{base} В маршруте такого этапа тоже нет. Основание правила: {spec.rationale or spec.title}.",
            finding_code=ind.main.finding_code, evidence_quote=ind.main.quote, specialty_code=spec.specialty_code,
            rule_code=spec.code, short=f"нет консультации {who}",
            proposed_operation={"op": "add_step", "step_type": "consultation", "title": f"Консультация {who}",
                                "specialty_code": spec.specialty_code, "offset_days": 0,
                                "window_days": spec.max_days or 14})]

    def _check_diagnostics(self, ind: Indication, items, covered: Coverage, source: str) -> list[IssueDraft]:
        spec = ind.spec
        matching = [i for i in items if ind.matches_service(f"{i.text} {i.service}")]
        in_route = any(ind.matches_service(title) for title in covered.route_services)
        if matching:
            return self._check_timing(ind, matching, spec.service_title, in_route)
        base = f"Есть показание к обследованию «{spec.service_title}»: {self._basis(ind)} В рекомендациях его нет."
        if in_route:
            return [] if source == "visit" else [IssueDraft(
                "missing_diagnostics", "minor",
                f"{base} Маршрут уже включает этот этап — правка маршрута не нужна.",
                finding_code=ind.main.finding_code, evidence_quote=ind.main.quote, rule_code=spec.code)]
        return [IssueDraft(
            "missing_diagnostics", spec.severity,
            f"{base} В маршруте его тоже нет. Основание правила: {spec.rationale or spec.title}.",
            finding_code=ind.main.finding_code, evidence_quote=ind.main.quote, rule_code=spec.code,
            short=f"нет обследования «{spec.service_title}»",
            proposed_operation={"op": "add_step", "step_type": "diagnostics", "title": spec.service_title,
                                "specialty_code": "", "offset_days": 0, "window_days": spec.max_days or 14})]

    def _check_timing(self, ind: Indication, matching, what: str, in_route: bool) -> list[IssueDraft]:
        spec = ind.spec
        if not spec.max_days:
            return []
        timely = [i for i in matching if not i.interval_days or i.interval_days <= spec.max_days]
        if timely:
            return []
        late = min(i.interval_days for i in matching)
        note = (" Этап с нужным сроком уже есть в маршруте — замечание к формулировке рекомендации."
                if in_route else "")
        return [IssueDraft(
            "late_timing", "minor" if in_route else spec.severity,
            f"{what}: рекомендовано через {late} дн., а по правилу «{spec.title}» — не позже {spec.max_days} дн. "
            f"{self._basis(ind)}{note}",
            finding_code=ind.main.finding_code, evidence_quote=ind.main.quote, specialty_code=spec.specialty_code,
            rule_code=spec.code,
            short="" if in_route else f"поздний срок: {what.lower()} через {late} дн. (нужно ≤ {spec.max_days})")]

    def _unsupported(self, indications: list[Indication], items) -> list[IssueDraft]:
        accepted = set().union(*(i.accepted for i in indications)) if indications else set()
        result = []
        for item in items:
            if item.specialty_code and item.kind == "consultation" and item.specialty_code not in accepted:
                result.append(IssueDraft(
                    "unsupported", "info",
                    f"Рекомендация «{_short(item.text)}» не следует из находок по матрице показаний. "
                    "Это не ошибка: у врача могут быть свои основания — для сведения.",
                    specialty_code=item.specialty_code))
        return result

    # ------------------------------------------------------------------ итог
    @staticmethod
    def _basis(ind: Indication) -> str:
        ev = ind.main
        parts = [f"«{_short(ev.quote)}»"]
        if ind.only_description:
            parts.append("(только в описании, в заключение не вынесено)")
        if ind.uncertain:
            parts.append("(описано с сомнением — показание требует подтверждения врачом)")
        return " ".join(parts) + "."

    @staticmethod
    def _verdict(issues: list[IssueDraft]) -> str:
        severities = {i.severity for i in issues}
        if severities & {"critical", "major"}:
            return "insufficient"
        if "minor" in severities:
            return "needs_review"
        return "sufficient"

    @staticmethod
    def _summary(verdict: str, indications: list[Indication], issues: list[IssueDraft]) -> str:
        if verdict == "sufficient":
            if not indications:
                return "Показаний по матрице нет; рекомендации не противоречат находкам."
            return f"Все показания ({len(indications)}) закрыты рекомендациями."
        gaps = [i.short for i in issues if i.short]
        if gaps:
            return "Требует решения: " + "; ".join(dict.fromkeys(gaps)) + "."
        return "Правка маршрута не нужна: маршрут закрывает все показания. Есть замечания к протоколу."


def _short(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
