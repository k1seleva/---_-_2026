"""
Приём событий МИС (1С) — контракт интеграции (п. 5 кейса).

Доставка: вебхук POST /api/v1/mis/events/ с подписью HMAC-SHA256 (заголовок X-MIS-Signature).
В пилоте тот же формат можно класть в очередь (RabbitMQ) — меняется только транспорт.

В прототипе вебхук работает как заглушка (settings.MIS_WEBHOOK_MODE = "stub"): событие проверяется
по контракту и попадает в журнал, но ничего в системе не меняет. Протоколы в прототип загружаются
вручную, пачкой или из папки. Обработку событий включает MIS_WEBHOOK_MODE=live.

Формат:
{
  "event_id": "1c-000123",                 # уникален в МИС, ключ идемпотентности
  "event_type": "protocol.signed",         # см. MIS_EVENT_TYPES
  "occurred_at": "2026-08-26T10:15:00+03:00",
  "patient": {"mis_id": "AK-0001", "birth_year": 1990, "sex": "F"},
  "protocol": {"id": "UZI-777", "study_type": "УЗИ ОМТ", "study_date": "2026-08-26", "text": "..."},
  "route_id": null,                        # для событий стационара, если МИС его знает
  "data": {"date": "2026-09-01"}           # дата госпитализации и т.п.
}
"""
import hashlib
import hmac
import json
import logging
import os

from django.conf import settings
from django.core.cache import cache

from apps.patients.facade import PatientsFacade
from apps.processing.services.pipeline import DocumentIngestService, UploadCommand
from common.events import contracts
from common.events.bus import publish

MIS_EVENT_TYPES = {
    "protocol.signed": "protocol",
    "protocol.corrected": "protocol",
    "protocol.annulled": "annul",
    "hospitalization.scheduled": contracts.MIS_HOSPITALIZATION_SCHEDULED,
    "patient.hospitalized": contracts.MIS_HOSPITALIZED,
    "surgery.done": contracts.MIS_SURGERY_DONE,
    "patient.discharged": contracts.MIS_DISCHARGED,
}

# Что сделала бы система в рабочем режиме — для ответа заглушки и журнала.
MIS_EVENT_EFFECTS = {
    "protocol.signed": "принять протокол и запустить разбор",
    "protocol.corrected": "принять новую версию протокола и пересчитать маршрут",
    "protocol.annulled": "аннулировать протокол и закрыть маршрут по нему",
    "hospitalization.scheduled": "отметить в маршруте назначенную госпитализацию",
    "patient.hospitalized": "отметить госпитализацию",
    "surgery.done": "отметить операцию и запланировать послеоперационный контроль",
    "patient.discharged": "отметить выписку и предложить запись на контроль",
}
PROTOCOL_EVENTS = {"protocol.signed", "protocol.corrected", "protocol.annulled"}
STUB_JOURNAL_KEY, STUB_JOURNAL_SIZE = "mis:stub:journal", 50

logger = logging.getLogger(__name__)


def verify_signature(body: bytes, signature: str) -> bool:
    secret = os.getenv("MIS_WEBHOOK_SECRET", "")
    if not secret:  # в прототипе подпись не обязательна
        return True
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


class MisEventValidator:
    """Проверка события по контракту. Ничего не меняет: общая для заглушки и рабочего режима."""

    def errors(self, event) -> list[str]:
        if not isinstance(event, dict):
            return ["Событие должно быть JSON-объектом"]
        errors = []
        if not str(event.get("event_id") or "").strip():
            errors.append("Нет event_id (ключ идемпотентности)")
        event_type = event.get("event_type", "")
        if event_type not in MIS_EVENT_TYPES:
            errors.append(f"Неизвестный тип события: {event_type or 'не указан'}")
        if not str((event.get("patient") or {}).get("mis_id") or "").strip():
            errors.append("Нет patient.mis_id (номер карты пациента в МИС)")
        if event_type in PROTOCOL_EVENTS:
            protocol = event.get("protocol") or {}
            if not protocol.get("id"):
                errors.append("Нет protocol.id")
            if event_type != "protocol.annulled" and not str(protocol.get("text") or "").strip():
                errors.append("Нет protocol.text (текст протокола)")
        return errors

    def validate(self, event) -> None:
        if errors := self.errors(event):
            raise ValueError("; ".join(errors))


class StubMisEventHandler:
    """Заглушка интеграции: событие проверено и записано в журнал, данные системы не меняются.

    Повтор того же event_id отмечается как дубль: так МИС может проверить свою логику повторной доставки.
    """

    def __init__(self, validator: MisEventValidator | None = None) -> None:
        self.validator = validator or MisEventValidator()

    def handle(self, event: dict) -> dict:
        self.validator.validate(event)
        journal = cache.get(STUB_JOURNAL_KEY, [])
        duplicate = any(e["event_id"] == event["event_id"] for e in journal)
        entry = {"event_id": event["event_id"], "event_type": event["event_type"], "mis_id": event["patient"]["mis_id"],
                 "would": MIS_EVENT_EFFECTS[event["event_type"]], "duplicate": duplicate}
        if not duplicate:
            cache.set(STUB_JOURNAL_KEY, [entry, *journal][:STUB_JOURNAL_SIZE], None)
        logger.info("[МИС заглушка] %s %s: %s", event["event_type"], event["event_id"], entry["would"])
        return {"status": "stub", **entry,
                "note": "Интеграция с МИС в прототипе работает как заглушка: формат проверен, событие записано "
                        "в журнал, данные не изменены"}

    @staticmethod
    def journal() -> list[dict]:
        return cache.get(STUB_JOURNAL_KEY, [])


class MisEventRouter:
    """Рабочий режим: переводит внешнее событие МИС во внутренние команды и события модулей."""

    def __init__(self, validator: MisEventValidator | None = None) -> None:
        self.validator = validator or MisEventValidator()

    def handle(self, event: dict) -> dict:
        self.validator.validate(event)
        kind = MIS_EVENT_TYPES[event["event_type"]]
        patient = event.get("patient") or {}
        patient_id = PatientsFacade.ensure_by_mis_id(
            patient["mis_id"], birth_year=patient.get("birth_year"), sex=patient.get("sex", ""))

        if kind == "protocol":
            protocol = event["protocol"]
            body = json.dumps(protocol, ensure_ascii=False).encode()
            document = DocumentIngestService().ingest(UploadCommand(
                patient_id=patient_id, filename=f"{protocol['id']}.json", data=body,
                external_id=protocol["id"], location_code=protocol.get("location_code", ""),
            ))
            return {"status": "accepted", "document_id": str(document.id), "patient_id": patient_id}
        if kind == "annul":
            count = DocumentIngestService().annul(event["protocol"]["id"], reason=event.get("data", {}).get("reason", ""))
            return {"status": "annulled", "documents": count}
        publish(kind, {"patient_id": patient_id, "route_id": event.get("route_id"), **(event.get("data") or {})},
                event_id=f"mis:{event['event_id']}")
        return {"status": "accepted", "patient_id": patient_id}


def get_mis_handler():
    """Обработчик по режиму интеграции (settings.MIS_WEBHOOK_MODE): заглушка по умолчанию."""
    return MisEventRouter() if getattr(settings, "MIS_WEBHOOK_MODE", "stub") == "live" else StubMisEventHandler()
