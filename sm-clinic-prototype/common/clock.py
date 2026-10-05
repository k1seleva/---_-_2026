"""Единый источник «текущего времени» для всех модулей.

Все сроки маршрута (таймеры, эскалации) считаются через clock.now(), а не timezone.now(),
чтобы на демонстрации можно было «перемотать» время вперёд.
"""
from datetime import datetime, timedelta

from django.utils import timezone


def now() -> datetime:
    from common.models import SimulationClock

    clock = SimulationClock.objects.filter(pk=1).first()
    offset = clock.offset_seconds if clock else 0
    return timezone.now() + timedelta(seconds=offset)


def advance(hours: float = 0, days: float = 0) -> datetime:
    """Сдвинуть модельное время вперёд (только для демо-режима)."""
    from common.models import SimulationClock

    clock, _ = SimulationClock.objects.get_or_create(pk=1)
    clock.offset_seconds += int(timedelta(hours=hours, days=days).total_seconds())
    clock.save(update_fields=["offset_seconds"])
    return now()


def reset() -> None:
    from common.models import SimulationClock

    SimulationClock.objects.update_or_create(pk=1, defaults={"offset_seconds": 0})
