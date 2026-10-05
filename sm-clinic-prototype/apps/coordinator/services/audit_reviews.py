"""
Обсуждение результатов проверки рекомендаций.

Модуль audit нашёл существенный пропуск (например, нет консультации хирурга при ЖКБ) —
координатор получает разбор и задачу. Он видит размеченный протокол, рекомендации врача,
показание с цитатой-основанием и готовую правку маршрута, и принимает решение по каждому
замечанию: принять (маршрут корректируется событием) или отклонить с комментарием
(рекомендации врача достаточны). Решения возвращаются в audit — так считается точность правил.
"""
from django.db import transaction
from django.utils import timezone

from apps.audit.facade import AuditFacade
from apps.routing.facade import RoutingFacade
from common.events import contracts
from common.events.bus import publish

from ..models import CoordinatorTask, RouteCorrection, RouteReview
from .tasks import TaskService

BLOCKING = ("critical", "major")


class AuditReviewService:
    @transaction.atomic
    def open_from_event(self, payload: dict) -> RouteReview | None:
        blocking = [i for i in payload.get("issues", []) if i["severity"] in BLOCKING]
        if payload.get("verdict") != "insufficient" or not blocking:
            return None
        review, created = RouteReview.objects.get_or_create(
            audit_id=payload["audit_id"],
            defaults={
                "route_id": payload.get("route_id"), "patient_id": payload["patient_id"],
                "document_id": payload["document_id"], "source": RouteReview.Source.RECOMMENDATION_AUDIT,
                "reason": payload.get("summary", ""), "discrepancies": [i["message"] for i in blocking],
                "ai_route": {"audit_id": payload["audit_id"], "proposed_operations": [
                    i["proposed_operation"] for i in blocking if i.get("proposed_operation")]},
            },
        )
        if not created:  # повторная проверка того же протокола — обновляем разбор
            review.discrepancies, review.reason, review.status = [i["message"] for i in blocking], payload.get("summary", ""), RouteReview.Status.OPEN
            review.save()
        critical = any(i["severity"] == "critical" for i in blocking)
        source = "назначения на приёме" if payload.get("source") == "visit" else "рекомендации в протоколе"
        TaskService().create(
            task_type=CoordinatorTask.TaskType.REVIEW_RECOMMENDATIONS, patient_id=payload["patient_id"],
            route_id=payload.get("route_id"), title=f"Обсудить {source}: {payload.get('summary', '')}"[:255],
            dedupe_key=f"audit:{payload['audit_id']}",
            priority=CoordinatorTask.Priority.CRITICAL if critical else CoordinatorTask.Priority.HIGH,
        )
        return review

    @transaction.atomic
    def decide(self, review: RouteReview, decisions: dict[str, dict], *, comment: str, user=None) -> RouteReview:
        """decisions: {issue_id: {"decision": "accepted" | "rejected", "comment": "..."}}."""
        audit = AuditFacade.get(review.audit_id) or {"issues": []}
        actor = str(user.pk) if user else ""
        operations = [i["proposed_operation"] for i in audit["issues"]
                      if decisions.get(i["id"], {}).get("decision") == "accepted" and i.get("proposed_operation")]
        if operations:
            routes = RoutingFacade.routes_for_document(review.document_id) if review.document_id else []
            route_id = str(review.route_id) if review.route_id else (routes[0]["id"] if routes else "")
            correction = RouteCorrection.objects.create(review=review, route_id=route_id or None, operations=operations,
                                                        reason=comment, author=user)
            publish(contracts.ROUTE_CORRECTION_APPROVED, {
                "route_id": route_id, "patient_id": str(review.patient_id),
                "document_id": str(review.document_id) if review.document_id else None,
                "operations": operations, "reason": comment, "actor_id": actor, "correction_id": str(correction.id),
            })
        publish(contracts.AUDIT_RESOLVED, {
            "audit_id": str(review.audit_id), "actor_id": actor,
            "decisions": [{"issue_id": issue_id, **d} for issue_id, d in decisions.items()],
        })
        review.status = RouteReview.Status.CORRECTED if operations else RouteReview.Status.CONFIRMED
        review.resolution_comment, review.resolved_by, review.resolved_at = comment, user, timezone.now()
        review.save()
        CoordinatorTask.objects.filter(dedupe_key=f"audit:{review.audit_id}", status__in=["open", "in_progress"]).update(
            status=CoordinatorTask.Status.DONE, resolution=comment or "Решение по рекомендациям принято")
        return review
