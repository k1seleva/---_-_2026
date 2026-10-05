"""
Сформировать советы по маршрутизации заново, например после подключения Qwen.

    python manage.py regenerate_advice                 # где советы собраны демо-режимом, с ошибкой или не собраны
    python manage.py regenerate_advice --all           # все протоколы
    python manage.py regenerate_advice --document <id> # один протокол

Разметку и триггеры команда не меняет: чтобы протокол разметили и словарь, и модель, загрузите его заново.
"""
from django.core.management.base import BaseCommand

from apps.processing.models import StudyDocument
from apps.processing.services.routing_advice import RoutingAdviceService, configured_engine


class Command(BaseCommand):
    help = "Сформировать советы ИИ-агента по маршрутизации заново (после подключения Qwen)"

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="все протоколы, включая уже отвеченные моделью")
        parser.add_argument("--document", help="идентификатор одного протокола")

    def handle(self, all=False, document=None, **opts):
        engine = configured_engine()
        self.stdout.write(f"Советчик по настройкам: {engine}")
        documents = StudyDocument.objects.order_by("created_at")
        if document:
            documents = documents.filter(pk=document)
        service, done, skipped, errors = RoutingAdviceService(), 0, 0, 0
        for doc in documents:
            result = doc.latest_result
            if result is None:
                continue
            meta = result.advice_meta or {}
            if not all and not document and meta.get("engine") == engine and meta.get("status") == "ready":
                skipped += 1
                continue
            count = service.generate(result.id)
            meta = type(result).objects.get(pk=result.pk).advice_meta
            if meta.get("status") == "error":
                errors += 1
                self.stdout.write(self.style.ERROR(f"{doc.id}: ошибка: {meta.get('error', '')}"))
            else:
                done += 1
                self.stdout.write(f"{doc.id}: советов {count}" + (f", {meta['duration_ms'] / 1000:.1f} с"
                                                                   if meta.get("duration_ms") else ""))
        self.stdout.write(self.style.SUCCESS(f"Готово: сформировано {done}, с ошибкой {errors}, пропущено {skipped}"))
