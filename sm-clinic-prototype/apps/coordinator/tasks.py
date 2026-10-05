from celery import shared_task


@shared_task
def snapshot_metrics() -> None:
    from common import clock

    from .models import MetricSnapshot
    from .services.analytics import AnalyticsService

    service = AnalyticsService()
    MetricSnapshot.objects.create(taken_at=clock.now(), metrics={"funnel": service.funnel(), "kpis": service.kpis()})
