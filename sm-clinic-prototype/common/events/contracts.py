"""
Контракты событий между модулями. Это единственное, о чём модули «договариваются»:
имя события + форма payload. При выносе в микросервисы этот файл становится
схемой сообщений брокера (AsyncAPI / JSON Schema).

Правило: payload содержит только идентификаторы и минимум данных, никаких ORM-объектов.
"""
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from django.utils import timezone

# --- Processing ---------------------------------------------------------------
DOCUMENT_UPLOADED = "processing.document_uploaded"
DOCUMENT_PROCESSED = "processing.document_processed"      # {document_id, patient_id, extraction: {...}}
DOCUMENT_CORRECTED = "processing.document_corrected"      # новая версия протокола -> пересчёт маршрута
DOCUMENT_ANNULLED = "processing.document_annulled"        # закрыть маршрут, остановить сообщения
DOCUMENT_FAILED = "processing.document_failed"            # файл не прочитан: {document_id, filename, error}
DOCUMENT_UNMATCHED = "processing.document_unmatched"      # пациент не определён: как PROCESSED, но маршрут не строится

# --- Routing ------------------------------------------------------------------
ROUTE_CREATED = "routing.route_created"                   # {route_id, patient_id, step: {...}}
ROUTE_UPDATED = "routing.route_updated"
ROUTE_STEP_ACTIVATED = "routing.step_activated"           # следующий этап готов к записи
ROUTE_STEP_ESCALATED = "routing.step_escalated"           # {level, action, template_code}
ROUTE_CLOSED = "routing.route_closed"
EMERGENCY_FINDING = "routing.emergency_finding"           # только эскалация персоналу

# --- Doctors ------------------------------------------------------------------
APPOINTMENT_BOOKED = "doctors.appointment_booked"
APPOINTMENT_CANCELLED = "doctors.appointment_cancelled"
APPOINTMENT_NO_SHOW = "doctors.appointment_no_show"
VISIT_COMPLETED = "doctors.visit_completed"               # {appointment_id, tactic, prescriptions: [...]}
AUTO_BOOKING_FAILED = "doctors.auto_booking_failed"       # не нашлось слота для заблаговременной записи

# --- Patients -----------------------------------------------------------------
PATIENT_ACTION = "patients.action"                        # «уже обратился», «не планирую», «перезвоните»
NOTIFICATION_SENT = "patients.notification_sent"          # факт доставки уведомления (для воронки)

# --- Audit: проверка достаточности рекомендаций (необходимость коррекции маршрута) ---
AUDIT_COMPLETED = "audit.completed"                       # {audit_id, document_id, patient_id, route_id, verdict, issues: [...]}

# --- Coordinator --------------------------------------------------------------
ROUTE_CORRECTION_APPROVED = "coordinator.correction_approved"  # {route_id | "", patient_id, document_id, operations, reason}
AUDIT_RESOLVED = "coordinator.audit_resolved"             # {audit_id, decisions: [{issue_id, decision, comment}]}
PATIENT_IDENTIFIED = "coordinator.patient_identified"     # {placeholder_id, patient_id, comment} — обезличенная карточка разобрана
DOCUMENT_REJECTED = "coordinator.document_rejected"       # {document_id, reason} — протокол не относится к пациентам клиники

# --- Внешняя МИС (1С) -> через шлюз --------------------------------------------
MIS_HOSPITALIZATION_SCHEDULED = "mis.hospitalization_scheduled"
MIS_HOSPITALIZED = "mis.hospitalized"
MIS_SURGERY_DONE = "mis.surgery_done"
MIS_DISCHARGED = "mis.discharged"


@dataclass(frozen=True)
class DomainEvent:
    """Конверт события. event_id — ключ идемпотентности: внешние системы
    передают свой id, чтобы повторная доставка не порождала дублей."""

    event_type: str
    payload: dict[str, Any]
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: datetime = field(default_factory=timezone.now)
    source: str = "sm-clinic"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["occurred_at"] = self.occurred_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DomainEvent":
        occurred = data.get("occurred_at")
        return cls(
            event_type=data["event_type"],
            payload=data.get("payload", {}),
            event_id=data.get("event_id") or str(uuid.uuid4()),
            occurred_at=datetime.fromisoformat(occurred) if occurred else timezone.now(),
            source=data.get("source", "sm-clinic"),
        )
