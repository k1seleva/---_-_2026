"""
Входящие протоколы координатора: категория, причины-метки и объяснение «почему так» в одной строке.

Категория у протокола ровно одна. Правила проверяются сверху вниз, срабатывает первое:
  1. Экстренно            — экстренная находка (даже если пациент не определён);
  2. Ошибка обработки      — файл не прочитан;
  3. Пациент не определён  — обезличенная карточка, ждёт привязки к пациенту;
  4. Нужна проверка        — находки есть, но чего-то не хватает (нет заключения, рекомендаций, даты…);
  5. Маршрут запущен       — всё на месте, маршрут создан;
  6. Без находок           — маршрут не нужен.
Пока протокол читается — «В обработке»; снятые с разбора (аннулированные, заменённые) — отдельно.
"""
from dataclasses import dataclass

from django.db import transaction

from common import clock
from common.events import contracts
from common.events.bus import publish

from ..models import ProtocolCase, Tag

Category = ProtocolCase.Category


@dataclass(frozen=True)
class Reason:
    code: str
    title: str
    color: str
    description: str


REASONS: list[Reason] = [
    Reason("emergency", "Экстренная находка", "red", "В протоколе экстренная находка из словаря — врач связывается с пациентом сразу"),
    Reason("read_error", "Ошибка чтения", "amber", "Файл не удалось прочитать: повреждён, пустой или неподдерживаемый"),
    Reason("unmatched", "Пациент не определён", "violet", "В протоколе нет номера карты или он не найден в МИС"),
    Reason("no_conclusion", "Нет заключения", "amber", "Есть изменения в описании, но раздела «Заключение» нет"),
    Reason("no_recommendations", "Нет рекомендаций", "amber", "Есть находки, но врач диагностики не дал рекомендаций"),
    Reason("recs_insufficient", "Рекомендаций недостаточно", "amber", "По матрице показаний не хватает консультации или обследования"),
    Reason("recs_remarks", "Замечания к рекомендациям", "amber", "Проверка рекомендаций нашла замечания, решение не требуется"),
    Reason("not_in_conclusion", "Не вынесено в заключение", "amber", "Изменение из описания не попало в заключение"),
    Reason("uncertain", "Сомнение в формулировке", "amber", "«?», «нельзя исключить», «по типу» — уточнить у врача"),
    Reason("no_study_date", "Нет даты исследования", "amber", "Дата не найдена в шапке — сроки маршрута считаются от загрузки"),
    Reason("no_study_type", "Нет типа исследования", "amber", "Тип исследования не распознан"),
    Reason("finding_without_route", "Находка без маршрута", "amber", "Находка есть, но правило маршрута для неё не сработало"),
    Reason("route_started", "Маршрут запущен", "green", "По протоколу создан маршрут пациента"),
    Reason("new_version", "Новая версия протокола", "blue", "Протокол исправлен в МИС, маршрут пересчитан"),
    Reason("reviewed", "Проверено координатором", "green", "Координатор разобрал причины и снял протокол с проверки"),
]
REASON_BY_CODE = {r.code: r for r in REASONS}
REVIEW_REASONS = {"no_conclusion", "no_recommendations", "recs_insufficient", "recs_remarks", "not_in_conclusion",
                  "uncertain", "no_study_date", "no_study_type", "finding_without_route"}


def categorize(case: ProtocolCase) -> str:
    reasons = set(case.reasons)
    if case.status in ("uploaded", "processing"):
        return Category.PROCESSING
    if case.status in ("annulled", "superseded"):
        return Category.CLOSED
    if "emergency" in reasons:
        return Category.EMERGENCY
    if case.status == "failed":
        return Category.FAILED
    if case.is_placeholder:
        return Category.UNMATCHED
    if reasons & REVIEW_REASONS and not case.reviewed_at:
        return Category.NEEDS_REVIEW
    if case.route_ids:
        return Category.ROUTED
    return Category.CLOSED if case.findings else Category.NO_FINDINGS


def _dynamic_reasons(case: ProtocolCase) -> list[str]:
    """Причины, которые зависят от событий других модулей (маршрут создан позже разбора и т. п.)."""
    reasons = [r for r in case.reasons if r not in ("finding_without_route", "route_started", "unmatched", "reviewed")]
    if case.route_ids:
        reasons.append("route_started")
    elif case.findings and case.status == "processed" and not case.is_placeholder:
        reasons.append("finding_without_route")
    if case.is_placeholder:
        reasons.append("unmatched")
    if case.reviewed_at:
        reasons.append("reviewed")
    return list(dict.fromkeys(reasons))


def recompute(case: ProtocolCase) -> ProtocolCase:
    case.reasons = _dynamic_reasons(case)
    case.category = categorize(case)
    case.save()
    return case


def ensure_system_tags() -> None:
    for r in REASONS:
        Tag.objects.update_or_create(code=r.code, defaults={"title": r.title, "color": r.color,
                                                            "description": r.description, "is_system": True})


class CaseService:
    """Обновление проекции по событиям. Порядок событий может быть любым — категория пересчитывается всякий раз."""

    @staticmethod
    def _case(document_id) -> ProtocolCase:
        case, _ = ProtocolCase.objects.get_or_create(document_id=document_id, defaults={"uploaded_at": clock.now()})
        return case

    def on_uploaded(self, p: dict) -> None:
        case = self._case(p["document_id"])
        case.filename = p.get("filename", "")[:255]
        case.batch_id = p.get("batch_id") or None
        case.location = p.get("location_code", "")
        case.patient_id = p.get("patient_id") or None
        case.version = p.get("version") or 1
        case.external_id = p.get("external_id", "")
        case.status = "uploaded"
        if case.version > 1:
            case.reasons = [*case.reasons, "new_version"]
            for old in ProtocolCase.objects.filter(external_id=case.external_id, version__lt=case.version).exclude(
                    external_id=""):
                old.status = "superseded"
                recompute(old)
        recompute(case)

    def on_failed(self, p: dict) -> None:
        case = self._case(p["document_id"])
        case.status, case.error = "failed", p.get("error", "")[:500]
        case.filename = case.filename or p.get("filename", "")
        case.location = case.location or p.get("location_code", "")
        case.reasons = [*case.reasons, "read_error"]
        recompute(case)

    def on_processed(self, p: dict, *, unmatched: bool) -> None:
        extraction = p.get("extraction") or {}
        summary = (p.get("annotation") or {}).get("summary") or {}
        positive = [f for f in extraction.get("findings", []) if not f.get("negated")]
        case = self._case(p["document_id"])
        case.status, case.processed_at = "processed", clock.now()
        case.patient_id, case.is_placeholder = p.get("patient_id") or None, unmatched
        case.card_number = p.get("card_number", "")
        case.location = p.get("location_code", "") or case.location
        case.filename = p.get("filename", "") or case.filename
        case.study_type = extraction.get("study_type") or ""
        study_date = extraction.get("study_date")
        case.study_date = study_date[:10] if study_date else None
        case.findings = list(dict.fromkeys(f["code"] for f in positive))
        quotes = {e["finding"]: e for e in case.explanation}
        for f in positive:
            quotes.setdefault(f["code"], {"finding": f["code"], "quote": (f.get("evidence_quote") or "")[:200], "rule": ""})
        case.explanation = list(quotes.values())

        reasons = [r for r in case.reasons if r in ("new_version", "recs_insufficient", "recs_remarks")]
        has_changes = bool(positive) or bool(summary.get("highlights"))
        if any(f.get("severity") == "emergency" for f in positive) or summary.get("emergency"):
            reasons.append("emergency")
        # Разметка знает, есть ли раздел «Заключение»; извлечение без него берёт весь текст как заключение.
        has_conclusion = summary["has_conclusion"] if "has_conclusion" in summary else bool(extraction.get("conclusion"))
        if has_changes and not has_conclusion:
            reasons.append("no_conclusion")
        if positive and not extraction.get("recommendations") and not summary.get("has_recommendations"):
            reasons.append("no_recommendations")
        if summary.get("not_in_conclusion"):
            reasons.append("not_in_conclusion")
        if summary.get("uncertain"):
            reasons.append("uncertain")
        if not extraction.get("study_date"):
            reasons.append("no_study_date")
        if not extraction.get("study_type"):
            reasons.append("no_study_type")
        case.reasons = reasons
        recompute(case)

    def on_route_created(self, p: dict) -> None:
        if not p.get("document_id"):
            return
        case = self._case(p["document_id"])
        if p["route_id"] not in case.route_ids:
            case.route_ids = [*case.route_ids, p["route_id"]]
        if p.get("trigger_code") and p.get("rule_title"):
            explanation = [e for e in case.explanation if e["finding"] != p["trigger_code"]]
            previous = next((e for e in case.explanation if e["finding"] == p["trigger_code"]), {})
            explanation.append({"finding": p["trigger_code"], "quote": p.get("evidence_quote") or previous.get("quote", ""),
                                "rule": p["rule_title"], "version": p.get("rule_version")})
            case.explanation = explanation
        recompute(case)

    def on_audit(self, p: dict) -> None:
        if not p.get("document_id"):
            return
        case = self._case(p["document_id"])
        case.audit_verdict = p.get("verdict", "")
        reasons = [r for r in case.reasons if r not in ("recs_insufficient", "recs_remarks")]
        if case.audit_verdict == "insufficient":
            reasons.append("recs_insufficient")
        elif case.audit_verdict == "needs_review":
            reasons.append("recs_remarks")
        case.reasons = reasons
        recompute(case)

    def on_annulled(self, p: dict) -> None:
        case = ProtocolCase.objects.filter(pk=p["document_id"]).first()
        if case:
            case.status = "annulled"
            recompute(case)

    def on_patient_identified(self, p: dict) -> None:
        for case in ProtocolCase.objects.filter(patient_id=p["placeholder_id"]):
            case.patient_id, case.is_placeholder = p["patient_id"], False
            recompute(case)

    # ------------------------------------------------------------ действия координатора
    @staticmethod
    def mark_reviewed(case: ProtocolCase, user: str = "") -> ProtocolCase:
        case.reviewed_at, case.reviewed_by = clock.now(), user[:150]
        return recompute(case)

    @staticmethod
    def set_tags(case: ProtocolCase, tag_codes: list[str]) -> None:
        case.tags.set(Tag.objects.filter(code__in=tag_codes, is_system=False))


class IdentityDecisionService:
    """Решения по обезличенным карточкам. Изменения в чужих модулях — только через события."""

    @transaction.atomic
    def assign(self, placeholder_id, patient_id, *, user: str = "", comment: str = "") -> None:
        publish(contracts.PATIENT_IDENTIFIED, {"placeholder_id": str(placeholder_id), "patient_id": str(patient_id),
                                               "by": user, "comment": comment})

    @transaction.atomic
    def reject(self, document_id, *, reason: str, user: str = "") -> None:
        publish(contracts.DOCUMENT_REJECTED, {"document_id": str(document_id), "reason": reason or "Отклонено координатором",
                                              "by": user})
