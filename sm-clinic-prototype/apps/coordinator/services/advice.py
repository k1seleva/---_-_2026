"""
Оценка советов ИИ-агента по маршрутизации координатором.

Совет — рекомендация ИИ, требует проверки координатором. Оценка («принять / отклонить / скорректировать»
с комментарием) пишется в отдельную таблицу AdviceReview и маршрут не меняет: если координатор согласен
с советом, правку маршрута он делает в карточке маршрута, где у каждой правки есть причина и автор.
Отклонение и корректировка требуют комментария: так собирается материал для улучшения промпта и правил.
"""
from dataclasses import dataclass

from django.db.models import Count

from apps.processing.facade import ProcessingFacade
from apps.routing.facade import RoutingFacade
from common import clock

from ..models import AdviceReview

Verdict = AdviceReview.Verdict


class AdviceReviewError(ValueError):
    pass


@dataclass
class ReviewCommand:
    advice_id: str
    user_id: str
    verdict: str
    comment: str = ""
    corrected_route_code: str = ""


class AdviceReviewService:
    def review(self, cmd: ReviewCommand) -> AdviceReview:
        advice = ProcessingFacade.get_advice(cmd.advice_id)
        if advice is None:
            raise AdviceReviewError("Совет не найден")
        if cmd.verdict not in Verdict.values:
            raise AdviceReviewError("Выберите оценку: принять, отклонить или скорректировать")
        comment = (cmd.comment or "").strip()
        route_code = (cmd.corrected_route_code or "").strip()
        if cmd.verdict in (Verdict.REJECT, Verdict.CORRECT) and not comment:
            raise AdviceReviewError("Напишите комментарий: почему совет отклонён или что исправлено")
        if cmd.verdict == Verdict.CORRECT:
            if not route_code:
                raise AdviceReviewError("Выберите маршрут, который предлагаете вместо совета")
            if route_code not in {r["code"] for r in RoutingFacade.route_catalog()}:
                raise AdviceReviewError("Такого маршрута нет в справочнике")
        else:
            route_code = ""
        return AdviceReview.objects.create(
            user_id=(cmd.user_id or "coordinator")[:150], advice_id=advice["id"], document_id=advice["document_id"],
            verdict=cmd.verdict, comment=comment, corrected_route_code=route_code, timestamp=clock.now(),
            advice_snapshot={k: advice[k] for k in ("text", "rationale", "evidence_quote", "confidence",
                                                    "target_route_code", "target_specialty_code", "engine", "model_name",
                                                    "prompt_version")},
        )

    @staticmethod
    def latest_by_advice(advice_ids) -> dict[str, AdviceReview]:
        """Последняя оценка по каждому совету (история хранится полностью)."""
        latest: dict[str, AdviceReview] = {}
        for r in AdviceReview.objects.filter(advice_id__in=list(advice_ids)).order_by("timestamp"):
            latest[str(r.advice_id)] = r
        return latest

    @staticmethod
    def stats() -> dict:
        """Сколько советов принято, отклонено и скорректировано (последняя оценка по совету)."""
        rows = AdviceReview.objects.values("advice_id", "verdict", "timestamp").order_by("timestamp")
        last = {r["advice_id"]: r["verdict"] for r in rows}
        counts = {v: 0 for v in Verdict.values}
        for verdict in last.values():
            counts[verdict] += 1
        total = len(last)
        return {"reviewed": total, "reviews_total": AdviceReview.objects.count(), "by_verdict": counts,
                "accept_share": round(counts[Verdict.ACCEPT] / total, 3) if total else None,
                "by_user": list(AdviceReview.objects.values("user_id").annotate(n=Count("id")).order_by("-n")[:10])}
