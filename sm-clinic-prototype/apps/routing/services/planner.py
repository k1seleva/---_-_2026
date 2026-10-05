"""
RoutePlanner — «двигатель» циклического маршрута.

Он реагирует на факты (запись, приём, тактика врача, события МИС, действия пациента)
и решает, какой этап следующий. Модуль ничего не знает о других модулях: получает
факты через события и сообщает о новых этапах тоже событиями.

Цикл: этап активирован -> пациент уведомлён -> записан -> приём -> тактика врача ->
новые этапы -> ... -> выписка -> новый виток «послеоперационное наблюдение» -> ... -> закрытие.
"""
from datetime import date, timedelta

from django.db import transaction

from common import clock
from common.events import contracts
from common.events.bus import publish

from ..models import BOOKABLE_STEP_TYPES, EscalationPolicy, PatientRoute, RouteStep, RouteTemplate, StepType
from . import state_machine
from .journal import log_event

S = PatientRoute.Status
StepStatus = RouteStep.Status

# Политика эскалации по умолчанию для типа этапа.
DEFAULT_POLICY = {
    StepType.CONSULTATION: "booking_standard",
    StepType.ONLINE_CONSULTATION: "booking_standard",
    StepType.DIAGNOSTICS: "booking_standard",
    StepType.FOLLOW_UP: "postop_control",
    StepType.HOSPITALIZATION_REFERRAL: "hospitalization_date",
}
# За сколько дней до срока «таймерного» этапа (УЗИ через 6 мес) начинаем приглашать пациента.
TIMER_LEAD_DAYS = 14


class Tactic:
    """Обязательный выбор тактики по итогам приёма (этап 7 кейса)."""

    SURGERY_INDICATED = "surgery_indicated"
    EXTRA_EXAM = "extra_exam"
    OBSERVATION = "observation"
    SURGERY_NOT_INDICATED = "surgery_not_indicated"
    PATIENT_REFUSED = "patient_refused"
    OTHER_PROFILE = "other_profile"


class RoutePlanner:
    # ================================================================ запуск и активация
    def start(self, route: PatientRoute, *, emergency: bool = False) -> None:
        if emergency:
            # Экстренная находка: никаких сообщений пациенту, только эскалация персоналу (п. 17 кейса).
            log_event(route, "emergency_escalation", basis="Экстренная находка — эскалация персоналу")
            publish(contracts.EMERGENCY_FINDING, {
                "route_id": str(route.id), "patient_id": str(route.patient_id), "reason": route.reason,
                "evidence": route.evidence,
            })
            return
        self.activate_next(route)

    def activate_next(self, route: PatientRoute, *, actor_type: str = "system") -> RouteStep | None:
        """Активирует следующий этап. Если этапов не осталось — завершает маршрут."""
        if not route.is_open:
            return None
        if route.steps.filter(status__in=RouteStep.ACTIVE_STATUSES).exists():
            return route.active_step
        step = route.steps.filter(status=StepStatus.PLANNED).order_by("order").first()
        if step is None:
            self.close(route, S.COMPLETED, basis="Все этапы маршрута выполнены", actor_type=actor_type)
            return None

        today = clock.now().date()
        if step.earliest_date is None:
            step.earliest_date = today + timedelta(days=step.offset_days)
            step.due_date = step.earliest_date + timedelta(days=step.window_days)
            step.save(update_fields=["earliest_date", "due_date", "updated_at"])
        if step.earliest_date - timedelta(days=TIMER_LEAD_DAYS) > today:
            # Этап с таймером: ждём. Его активирует периодическая задача (EscalationEngine).
            if route.status in (S.VISIT_DONE, S.CREATED, S.NOTIFIED):
                state_machine.transition(route, S.OBSERVATION, basis=f"Таймер: «{step.title}» с {step.earliest_date:%d.%m.%Y}")
            log_event(route, "step_timer_set", step=step, basis=f"Срок этапа {step.earliest_date:%d.%m.%Y}")
            return None
        return self._activate(step, actor_type=actor_type)

    def _activate(self, step: RouteStep, *, actor_type: str = "system") -> RouteStep:
        route = step.route
        step.status = StepStatus.AWAITING_BOOKING
        step.activated_at = clock.now()
        step.escalation_level = 0
        if step.escalation_policy_id is None and (code := DEFAULT_POLICY.get(step.step_type)):
            step.escalation_policy = EscalationPolicy.objects.filter(code=code).first()
        step.save()
        log_event(route, "step_activated", step=step, actor_type=actor_type, basis=step.title)
        publish(contracts.ROUTE_STEP_ACTIVATED, self.step_payload(step))
        return step

    @staticmethod
    def step_payload(step: RouteStep) -> dict:
        route = step.route
        return {
            "route_id": str(route.id), "step_id": str(step.id), "patient_id": str(route.patient_id),
            "route_kind": route.kind, "reason": route.reason, "trigger_code": route.trigger_code,
            "order": step.order, "offset_days": step.offset_days,
            "step_type": step.step_type, "title": step.title, "specialty_code": step.specialty_code,
            "service_code": step.service_code, "due_date": step.due_date.isoformat() if step.due_date else None,
            "earliest_date": step.earliest_date.isoformat() if step.earliest_date else None,
            "bookable": step.step_type in BOOKABLE_STEP_TYPES, "auto_book": step.auto_book,
            "attempt": step.attempt, "location_code": route.location_code,
            "responsible_unit": route.responsible_unit,
        }

    # ================================================================ запись / приём
    @transaction.atomic
    def on_booked(self, step_id: str, appointment_id: str, starts_at: str = "", actor_type: str = "patient") -> None:
        step = RouteStep.objects.select_related("route").filter(pk=step_id).first()
        if not step or not step.route.is_open:
            return
        step.status, step.appointment_id = StepStatus.BOOKED, appointment_id
        step.save(update_fields=["status", "appointment_id", "updated_at"])
        log_event(step.route, "step_booked", step=step, actor_type=actor_type, basis=f"Запись на {starts_at}")
        if state_machine.can_transition(step.route.status, S.BOOKED):
            state_machine.transition(step.route, S.BOOKED, actor_type=actor_type, basis=step.title)

    @transaction.atomic
    def on_cancelled(self, step_id: str, actor_type: str = "patient") -> None:
        """Пациент отменил запись -> «Требуется повторная запись», лестница напоминаний заново."""
        step = RouteStep.objects.select_related("route").filter(pk=step_id).first()
        if not step or not step.route.is_open:
            return
        self._restart_booking(step, policy_code="booking_standard")
        state_machine.transition(step.route, S.REBOOKING_REQUIRED, actor_type=actor_type, basis="Запись отменена")
        publish(contracts.ROUTE_STEP_ACTIVATED, self.step_payload(step))

    @transaction.atomic
    def on_no_show(self, step_id: str) -> None:
        """Сценарий 3: неявка -> сообщение через 30-60 минут, далее лестница сценария 2."""
        step = RouteStep.objects.select_related("route").filter(pk=step_id).first()
        if not step or not step.route.is_open:
            return
        log_event(step.route, "no_show", step=step, actor_type="doctor", basis=f"Неявка №{step.attempt}")
        self._restart_booking(step, policy_code="no_show")
        state_machine.transition(step.route, S.NO_SHOW, actor_type="doctor", basis=f"Неявка №{step.attempt - 1}")

    def _restart_booking(self, step: RouteStep, *, policy_code: str) -> None:
        step.status = StepStatus.AWAITING_BOOKING
        step.appointment_id = None
        step.attempt += 1
        step.activated_at = clock.now()
        step.escalation_level = 0
        step.escalation_policy = EscalationPolicy.objects.filter(code=policy_code).first()
        step.save()

    @transaction.atomic
    def on_visit_completed(self, step_id: str, tactic: str, prescriptions: list[dict], *,
                           next_specialty_code: str = "", doctor_id: str = "", comment: str = "") -> PatientRoute | None:
        """Тактика врача определяет следующий виток маршрута (этапы 7-8 кейса)."""
        step = RouteStep.objects.select_related("route").filter(pk=step_id).first()
        if not step:
            return None
        route = step.route
        step.status, step.completed_at = StepStatus.DONE, clock.now()
        step.outcome = {"tactic": tactic, "prescriptions": prescriptions, "comment": comment}
        step.save()
        basis = f"Решение врача: {tactic}"
        # Факт приёма от врача первичен — допускаем переход из любого активного статуса.
        state_machine.transition(route, S.VISIT_DONE, actor_type="doctor", actor_id=doctor_id, basis=basis, force=True)

        if tactic == Tactic.PATIENT_REFUSED:
            self._skip_planned(route, "Пациент отказался")
            return self.close(route, S.DECLINED, basis=basis, actor_type="doctor", actor_id=doctor_id)

        new_steps: list[dict] = []
        if tactic == Tactic.SURGERY_INDICATED:
            # Операция показана: направление -> госпитализация -> операция -> выписка.
            new_steps = [
                {"step_type": StepType.HOSPITALIZATION_REFERRAL, "title": "Направление на госпитализацию (назначить дату ≤ 3 раб. дней)",
                 "window_days": 3, "comment": "; ".join(p.get("title", "") for p in prescriptions)},
                {"step_type": StepType.HOSPITALIZATION, "title": "Госпитализация"},
                {"step_type": StepType.SURGERY, "title": (prescriptions[0].get("title") if prescriptions else "") or route.evidence.get("potential_route") or "Операция"},
                {"step_type": StepType.DISCHARGE, "title": "Выписка"},
            ]
            self._skip_planned(route, "Заменено планом оперативного лечения", only_sources={RouteStep.Source.RULE})
        elif tactic == Tactic.OTHER_PROFILE:
            new_steps = [{"step_type": StepType.CONSULTATION, "title": "Консультация другого профиля",
                          "specialty_code": next_specialty_code, "window_days": 14}]
        elif tactic == Tactic.OBSERVATION:
            interval = next((p.get("due_in_days") for p in prescriptions if p.get("due_in_days")), 180)
            new_steps = [{"step_type": StepType.FOLLOW_UP, "title": "Контрольный визит (динамическое наблюдение)",
                          "specialty_code": step.specialty_code, "offset_days": interval, "window_days": 30}]
        # Назначения врача (обследования, консультации) добавляются как этапы.
        if tactic in (Tactic.EXTRA_EXAM, Tactic.SURGERY_NOT_INDICATED, Tactic.OBSERVATION):
            new_steps = [self._prescription_to_step(p, step.specialty_code) for p in prescriptions] + new_steps
        if tactic == Tactic.EXTRA_EXAM:
            new_steps.append({"step_type": StepType.CONSULTATION, "title": "Повторная консультация по результатам обследования",
                              "specialty_code": step.specialty_code, "window_days": 14})

        self._insert_after(step, new_steps, source=RouteStep.Source.DOCTOR)
        target = {
            Tactic.SURGERY_INDICATED: S.REFERRED_HOSPITALIZATION,
            Tactic.EXTRA_EXAM: S.IN_DIAGNOSTICS,
            Tactic.OBSERVATION: S.OBSERVATION,
        }.get(tactic)
        if target:
            state_machine.transition(route, target, actor_type="doctor", actor_id=doctor_id, basis=basis)
        self.activate_next(route, actor_type="doctor")
        return route

    @staticmethod
    def _prescription_to_step(p: dict, default_specialty: str) -> dict:
        kind = p.get("kind", "diagnostics")
        step_type = {"consultation": StepType.CONSULTATION, "follow_up": StepType.FOLLOW_UP}.get(kind, StepType.DIAGNOSTICS)
        return {"step_type": step_type, "title": p.get("title") or "Назначение врача",
                "specialty_code": p.get("specialty_code") or ("" if step_type == StepType.DIAGNOSTICS else default_specialty),
                "service_code": p.get("service_code", ""), "offset_days": p.get("due_in_days") or 0,
                "window_days": 14, "comment": p.get("comment", "")}

    def _insert_after(self, step: RouteStep, new_steps: list[dict], *, source: str) -> list[RouteStep]:
        if not new_steps:
            return []
        later = list(step.route.steps.filter(order__gt=step.order).order_by("-order"))
        for s in later:  # сдвигаем хвост маршрута
            s.order += len(new_steps)
            s.save(update_fields=["order"])
        created = []
        for i, data in enumerate(new_steps, start=1):
            created.append(RouteStep.objects.create(route=step.route, order=step.order + i, source=source, **data))
        log_event(step.route, "steps_added", step=step, actor_type=source,
                  basis=", ".join(s.title for s in created))
        return created

    @staticmethod
    def _skip_planned(route: PatientRoute, reason: str, only_sources: set[str] | None = None) -> None:
        qs = route.steps.filter(status=StepStatus.PLANNED)
        if only_sources:
            qs = qs.filter(source__in=only_sources)
        qs.update(status=StepStatus.SKIPPED, comment=reason)

    # ================================================================ события стационара (МИС)
    @transaction.atomic
    def on_mis_event(self, route_id: str, event_type: str, payload: dict) -> PatientRoute | None:
        route = PatientRoute.objects.filter(pk=route_id).first()
        if not route or not route.is_open:
            return route
        mapping = {
            contracts.MIS_HOSPITALIZATION_SCHEDULED: (StepType.HOSPITALIZATION_REFERRAL, S.HOSPITALIZATION_SCHEDULED),
            contracts.MIS_HOSPITALIZED: (StepType.HOSPITALIZATION, S.HOSPITALIZED),
            contracts.MIS_SURGERY_DONE: (StepType.SURGERY, S.SURGERY_DONE),
            contracts.MIS_DISCHARGED: (StepType.DISCHARGE, S.DISCHARGED),
        }
        step_type, target = mapping[event_type]
        # Закрываем все этапы стационара до текущего включительно (МИС могла прислать не все события).
        hospital_types = [StepType.HOSPITALIZATION_REFERRAL, StepType.HOSPITALIZATION, StepType.SURGERY, StepType.DISCHARGE]
        upto = hospital_types[: hospital_types.index(step_type) + 1]
        now = clock.now()
        for s in route.steps.filter(step_type__in=upto).exclude(status__in=[StepStatus.DONE, StepStatus.SKIPPED]):
            s.status, s.completed_at = StepStatus.DONE, now
            s.outcome = {**s.outcome, "mis": payload}
            s.save()
        if event_type == contracts.MIS_HOSPITALIZATION_SCHEDULED:
            nxt = route.steps.filter(step_type=StepType.HOSPITALIZATION, status=StepStatus.PLANNED).first()
            if nxt:
                nxt.status = StepStatus.BOOKED
                nxt.earliest_date = nxt.due_date = date.fromisoformat(payload["date"]) if payload.get("date") else None
                nxt.save()
        # Через force: МИС — источник истины о стационаре, допускаем пропуск промежуточных статусов.
        state_machine.transition(route, target, actor_type="mis", basis=f"Событие МИС {event_type}", force=True)
        if event_type == contracts.MIS_DISCHARGED:
            self.close(route, S.COMPLETED, basis="Выписка: хирургический этап завершён", actor_type="mis")
            self.spawn_child_route(route, template_code="postop_control", kind=PatientRoute.Kind.POSTOP,
                                   reason=f"Послеоперационное наблюдение — {route.reason}")
        return route

    def spawn_child_route(self, parent: PatientRoute, *, template_code: str, kind: str, reason: str) -> PatientRoute | None:
        """Новый виток цикла: после выписки — маршрут послеоперационного наблюдения (этап 11 кейса)."""
        template = RouteTemplate.objects.filter(code=template_code).first()
        if not template:
            return None
        now = clock.now()
        child = PatientRoute.objects.create(
            patient_id=parent.patient_id, source_document_id=parent.source_document_id,
            source_external_id=f"{parent.source_external_id}:cycle{parent.cycle_no + 1}",
            kind=kind, trigger_code=f"{parent.trigger_code}:{template_code}", reason=reason[:500],
            evidence=parent.evidence, parent=parent, cycle_no=parent.cycle_no + 1,
            responsible_unit=parent.responsible_unit, location_code=parent.location_code, detected_at=now,
        )
        specialty = parent.steps.filter(step_type__in=list(BOOKABLE_STEP_TYPES)).values_list("specialty_code", flat=True).first() or ""
        for ts in template.steps.all():
            RouteStep.objects.create(
                route=child, order=ts.order, step_type=ts.step_type, title=ts.title,
                specialty_code=ts.specialty_code or specialty, offset_days=ts.offset_days,
                window_days=ts.window_days, auto_book=ts.auto_book, escalation_policy=ts.escalation_policy,
                source=RouteStep.Source.SYSTEM,
            )
        log_event(child, "route_created", to_status=child.status, basis=f"Новый виток после маршрута {parent.id}")
        from .builder import publish_route_created

        publish_route_created(child)
        log_event(parent, "child_route_created", basis=f"Создан маршрут {child.id}")
        # Контроль назначаем сразу при выписке: этап активируется досрочно (auto_book).
        first = child.steps.order_by("order").first()
        if first:
            first.earliest_date = (now + timedelta(days=first.offset_days)).date()
            first.due_date = first.earliest_date + timedelta(days=first.window_days)
            first.save()
            self._activate(first)
        return child

    # ================================================================ действия пациента / координатора
    @transaction.atomic
    def on_patient_action(self, route_id: str, action: str, patient_id: str = "") -> None:
        route = PatientRoute.objects.filter(pk=route_id).first()
        if not route or not route.is_open:
            return
        if action == "seen_elsewhere":
            self.close(route, S.SEEN_ELSEWHERE, basis="Пациент отметил: уже обратился к врачу", actor_type="patient", actor_id=patient_id)
        elif action == "decline":
            self.close(route, S.DECLINED, basis="Пациент: не планирую обращаться", actor_type="patient", actor_id=patient_id)
        else:
            log_event(route, f"patient_{action}", actor_type="patient", actor_id=patient_id)

    def close(self, route: PatientRoute, status: str, *, basis: str, actor_type: str = "system", actor_id: str = "") -> PatientRoute:
        route.steps.filter(status__in=[StepStatus.PLANNED, StepStatus.AWAITING_BOOKING]).update(
            status=StepStatus.CANCELLED if status != S.COMPLETED else StepStatus.SKIPPED)
        return state_machine.transition(route, status, actor_type=actor_type, actor_id=actor_id, basis=basis)

    @transaction.atomic
    def apply_correction(self, route_id: str, operations: list[dict], *, actor_id: str = "", reason: str = "") -> PatientRoute:
        """Корректировка координатора. operations:
        {"op": "add_step", "step_type": "...", "title": "...", "specialty_code": "...", "offset_days": 0}
        {"op": "cancel_step", "step_id": "..."}
        {"op": "change_specialty", "step_id": "...", "specialty_code": "..."}
        {"op": "change_due_date", "step_id": "...", "due_date": "YYYY-MM-DD"}
        {"op": "reopen"}  — вернуть закрытый маршрут в работу
        """
        route = PatientRoute.objects.get(pk=route_id)
        for op in operations:
            kind = op["op"]
            if kind == "reopen" and not route.is_open:
                state_machine.transition(route, S.NOTIFIED, actor_type="coordinator", actor_id=actor_id, basis=reason, force=True)
            elif kind == "add_step":
                last = route.steps.order_by("-order").first()
                anchor = route.active_step or last
                fields = {k: op[k] for k in ("step_type", "title", "specialty_code", "service_code", "offset_days", "window_days") if k in op}
                if fields.get("step_type") in BOOKABLE_STEP_TYPES:
                    # Добавленный этап живёт по той же лестнице напоминаний, что и этапы матрицы.
                    fields["escalation_policy"] = EscalationPolicy.objects.filter(code="booking_standard").first()
                if anchor:
                    self._insert_after(anchor, [fields], source=RouteStep.Source.COORDINATOR)
                else:
                    RouteStep.objects.create(route=route, order=1, source=RouteStep.Source.COORDINATOR, **fields)
            elif kind == "cancel_step":
                route.steps.filter(pk=op["step_id"]).update(status=StepStatus.SKIPPED, comment=reason)
            elif kind == "change_specialty":
                route.steps.filter(pk=op["step_id"]).update(specialty_code=op["specialty_code"])
            elif kind == "change_due_date":
                route.steps.filter(pk=op["step_id"]).update(due_date=op["due_date"])
            log_event(route, f"correction_{kind}", actor_type="coordinator", actor_id=actor_id, basis=reason, payload=op)
        route.refresh_from_db()
        self.activate_next(route, actor_type="coordinator")
        publish(contracts.ROUTE_UPDATED, {"route_id": str(route.id), "patient_id": str(route.patient_id),
                                          "status": route.status, "basis": f"Корректировка координатора: {reason}"})
        return route
