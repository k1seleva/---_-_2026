from celery import shared_task


@shared_task
def run_escalation_tick() -> dict:
    from .services.escalation import EscalationEngine

    report = EscalationEngine().tick()
    return report.__dict__
