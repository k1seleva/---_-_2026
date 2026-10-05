"""
Оспаривание и корректировка маршрута: координатор сравнивает исходные маркеры (ИИ) с маршрутом
врача и вносит правки. Правки применяет модуль маршрутизации по событию — координатор не
меняет чужие таблицы напрямую.
"""
import json

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone

from apps.processing.facade import ProcessingFacade
from apps.routing.facade import RoutingFacade
from common.events import contracts
from common.events.bus import publish

from ..models import RouteCorrection, RouteReview


def _plain(value):
    """Снимок для JSONField: даты этапов (date/datetime) превращаются в строки ISO."""
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


class DisputeService:
    def compare(self, route_id) -> dict:
        """Сводка «ИИ vs врач» для страницы сравнения."""
        route = RoutingFacade.get_route(route_id)
        if not route:
            raise ValueError("Маршрут не найден")
        document = ProcessingFacade.get_document(route["source_document_id"]) if route["source_document_id"] else None
        ai_markers = [f for f in (document or {}).get("findings", [])]
        ai_steps = [s for s in route["steps"] if s["source"] in ("rule", "ai_recommendation")]
        doctor_steps = [s for s in route["steps"] if s["source"] in ("doctor", "coordinator")]
        tactics = [s["outcome"] for s in route["steps"] if s.get("outcome")]
        return {
            "route": route, "document": document, "ai_markers": ai_markers, "ai_steps": ai_steps,
            "doctor_steps": doctor_steps, "tactics": tactics,
            "discrepancies": self._discrepancies(route, ai_markers, ai_steps, tactics),
        }

    @staticmethod
    def _discrepancies(route: dict, markers: list[dict], ai_steps: list[dict], tactics: list[dict]) -> list[str]:
        result = []
        positive = [m for m in markers if not m["negated"]]
        if route["kind"] == "trigger" and not positive:
            result.append("Маршрут открыт, но в протоколе нет подтверждённых находок")
        uncertain = [m for m in positive if m["uncertain"]]
        if uncertain:
            result.append("Находки с сомнением: " + ", ".join(m["label"] for m in uncertain))
        if any(t.get("tactic") in ("surgery_not_indicated", "patient_refused") for t in tactics) and route["evidence"].get("potential_route"):
            result.append(f"ИИ предполагал «{route['evidence']['potential_route']}», врач: операция не показана/отказ")
        skipped = [s for s in ai_steps if s["status"] in ("skipped", "cancelled")]
        if skipped:
            result.append("Этапы ИИ отменены: " + ", ".join(s["title"] for s in skipped))
        return result

    @transaction.atomic
    def open_review(self, route_id, *, source: str, reason: str = "", doctor_route: dict | None = None) -> RouteReview:
        data = self.compare(route_id)
        review, _ = RouteReview.objects.get_or_create(
            route_id=route_id, status=RouteReview.Status.OPEN, audit_id__isnull=True,
            defaults={
                "patient_id": data["route"]["patient_id"],
                "document_id": data["route"]["source_document_id"] or None,
                "source": source, "reason": reason,
                "ai_markers": _plain(data["ai_markers"]),
                "ai_route": _plain({"reason": data["route"]["reason"], "evidence": data["route"]["evidence"],
                                    "steps": data["ai_steps"]}),
                "doctor_route": _plain(doctor_route or {"tactics": data["tactics"], "steps": data["doctor_steps"]}),
                "discrepancies": data["discrepancies"],
            },
        )
        return review

    @transaction.atomic
    def resolve(self, review: RouteReview, *, status: str, operations: list[dict], comment: str, user=None) -> RouteReview:
        if operations:
            correction = RouteCorrection.objects.create(review=review, route_id=review.route_id, operations=operations,
                                                        reason=comment, author=user)
            publish(contracts.ROUTE_CORRECTION_APPROVED, {
                "route_id": str(review.route_id), "operations": operations, "reason": comment,
                "actor_id": str(user.pk) if user else "", "correction_id": str(correction.id),
            })
        review.status, review.resolution_comment = status, comment
        review.resolved_by, review.resolved_at = user, timezone.now()
        review.save()
        return review
