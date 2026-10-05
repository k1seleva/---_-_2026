"""Публичный интерфейс модуля координатора (только чтение) — для шлюза (меню рабочего пространства, колокольчик)."""
from django.db.models import Count

from .models import CoordinatorTask, ProtocolCase, RouteReview


class CoordinatorFacade:
    @staticmethod
    def workspace_counts(location: str = "") -> dict:
        cases = ProtocolCase.objects.all()
        tasks = CoordinatorTask.objects.filter(status__in=["open", "in_progress"])
        if location:
            cases = cases.filter(location=location)
        by_category = dict(cases.values_list("category").annotate(n=Count("document_id")))
        return {
            "categories": by_category,
            "inbox": sum(by_category.get(c, 0) for c in ("emergency", "failed", "unmatched", "needs_review")),
            "unmatched": by_category.get("unmatched", 0),
            "emergency": by_category.get("emergency", 0),
            "needs_review": by_category.get("needs_review", 0),
            "tasks": tasks.count(),
            "urgent_tasks": tasks.filter(priority=CoordinatorTask.Priority.CRITICAL).count(),
            "reviews": RouteReview.objects.filter(status=RouteReview.Status.OPEN).count(),
        }

    @staticmethod
    def bell_items(location: str = "", limit: int = 8) -> list[dict]:
        """Колокольчик координатора: экстренные протоколы и открытые задачи — самое срочное сверху."""
        items = []
        emergency = ProtocolCase.objects.filter(category=ProtocolCase.Category.EMERGENCY)
        if location:
            emergency = emergency.filter(location=location)
        for case in emergency.order_by("-uploaded_at")[:3]:
            items.append({"kind": "emergency", "icon": "alert", "tone": "red", "title": "Экстренная находка",
                          "body": case.filename or case.study_type, "when": case.uploaded_at,
                          "url": f"/coordinator/cases/{case.document_id}/", "action": "Открыть протокол"})
        for task in CoordinatorTask.objects.filter(status__in=["open", "in_progress"]).order_by("priority", "due_at")[:limit]:
            items.append({"kind": "task", "icon": "phone" if task.task_type in ("call_patient", "callback") else "tasks",
                          "tone": "red" if task.priority == CoordinatorTask.Priority.CRITICAL else "",
                          "title": task.get_task_type_display(), "body": task.title, "when": task.created_at,
                          "url": f"/coordinator/tasks/#task-{task.pk}", "action": "К задаче"})
        return items[:limit]
