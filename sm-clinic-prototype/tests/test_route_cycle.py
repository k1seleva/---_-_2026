"""Сквозной цикл: протокол -> маршрут -> уведомление -> запись -> неявка -> перезапись ->
приём (операция показана) -> госпитализация -> операция -> выписка -> новый виток с контролем."""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.coordinator.models import CoordinatorTask, RouteFact
from apps.coordinator.services.analytics import AnalyticsService
from apps.doctors.models import Appointment, Doctor
from apps.patients.models import Notification, Patient
from apps.routing.models import PatientRoute, RouteStep
from common import clock

from .helpers import make_docx, seed


# Этапы стационара приходят событиями МИС: в этих тестах вебхук работает в рабочем режиме, а не заглушкой.
@override_settings(MIS_WEBHOOK_MODE="live")
class RouteCycleTests(TransactionTestCase):
    def setUp(self):
        seed()
        clock.reset()
        self.api = APIClient()
        self.patient = Patient.objects.get(external_mis_id="AK-0001")
        # Запись через API кабинета — от имени вошедшего пациента.
        self.api.post("/patient/", {"card": "AK-0001", "password": "demo-2026"})

    def upload(self, conclusion: str, external_id: str = "UZI-1"):
        file = SimpleUploadedFile("protocol.docx", make_docx(conclusion))
        r = self.api.post("/api/v1/processing/documents/", {"patient_id": self.patient.id, "file": file,
                                                            "external_id": external_id, "location_code": "vdnh"}, format="multipart")
        self.assertEqual(r.status_code, 202, r.content)
        return r.json()

    def book_first_slot(self, step_id):
        slots = self.api.get(f"/api/v1/patients/{self.patient.id}/steps/{step_id}/slots/").json()["slots"]
        self.assertTrue(slots, "нет подходящих слотов")
        r = self.api.post(f"/api/v1/patients/{self.patient.id}/book/", {"route_step_id": step_id, "slot_id": slots[0]["id"]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def test_full_surgical_cycle(self):
        doc = self.upload("Эхографические признаки полипа эндометрия.")
        self.assertEqual(doc["status"], "processed")
        route = PatientRoute.objects.get(patient_id=self.patient.id)
        self.assertEqual(route.trigger_code, "endometrial_polyp")
        self.assertEqual(route.status, PatientRoute.Status.NOTIFIED)
        self.assertTrue(Notification.objects.filter(patient=self.patient, template_code="result_ready", channel="lk").exists())

        step = route.active_step
        self.assertEqual(step.specialty_code, "gyn_surgeon")
        # Запись к врачу другой специальности запрещена — строго по маршруту.
        wrong = self.api.get("/api/v1/doctors/slots/?specialty=urologist").json()["results"][0]
        r = self.api.post(f"/api/v1/patients/{self.patient.id}/book/", {"route_step_id": step.id, "slot_id": wrong["id"]}, format="json")
        self.assertEqual(r.status_code, 400)

        appt = self.book_first_slot(step.id)
        route.refresh_from_db()
        self.assertEqual(route.status, PatientRoute.Status.BOOKED)

        # Неявка -> сценарий 3
        self.api.post(f"/api/v1/doctors/appointments/{appt['appointment_id']}/no-show/")
        route.refresh_from_db()
        self.assertEqual(route.status, PatientRoute.Status.NO_SHOW)
        self.api.post("/api/v1/sim/advance/", {"hours": 1}, format="json")
        self.assertTrue(Notification.objects.filter(patient=self.patient, template_code="no_show").exists())

        appt = self.book_first_slot(step.id)
        # Завершить приём без тактики нельзя
        doctor = Doctor.objects.get(pk=appt["doctor_id"])
        r = self.api.post(f"/api/v1/doctors/appointments/{appt['appointment_id']}/complete/", {"doctor_id": doctor.id}, format="json")
        self.assertEqual(r.status_code, 400)
        ctx = self.api.get(f"/api/v1/doctors/appointments/{appt['appointment_id']}/context/").json()
        self.assertIn("полипа эндометрия", ctx["banner"]["evidence"])

        r = self.api.post(f"/api/v1/doctors/appointments/{appt['appointment_id']}/complete/", {
            "doctor_id": doctor.id, "tactic": "surgery_indicated",
            "prescriptions": [{"kind": "surgery", "title": "Гистероскопия, полипэктомия"}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        route.refresh_from_db()
        self.assertEqual(route.status, PatientRoute.Status.REFERRED_HOSPITALIZATION)

        # Нет даты госпитализации 24 ч -> задача менеджеру
        self.api.post("/api/v1/sim/advance/", {"hours": 25}, format="json")
        self.assertTrue(CoordinatorTask.objects.filter(route_id=route.id, task_type="hospitalization_date").exists())

        for event_type, data in [("hospitalization.scheduled", {"date": "2026-10-20"}), ("patient.hospitalized", {}),
                                 ("surgery.done", {"service": "A16.20.033"}), ("patient.discharged", {})]:
            r = self.api.post("/api/v1/mis/events/", {"event_id": f"e-{event_type}", "event_type": event_type,
                                                       "patient": {"mis_id": "AK-0001"}, "data": data}, format="json")
            self.assertEqual(r.status_code, 202, r.content)
        # Повторная доставка события не создаёт дублей
        self.api.post("/api/v1/mis/events/", {"event_id": "e-patient.discharged", "event_type": "patient.discharged",
                                               "patient": {"mis_id": "AK-0001"}, "data": {}}, format="json")
        route.refresh_from_db()
        self.assertEqual(route.status, PatientRoute.Status.COMPLETED)
        child = PatientRoute.objects.get(parent=route)
        self.assertEqual(child.kind, PatientRoute.Kind.POSTOP)
        self.assertEqual(PatientRoute.objects.filter(parent=route).count(), 1)
        control = child.steps.get()
        self.assertEqual(control.status, RouteStep.Status.BOOKED)  # записан заранее
        self.assertTrue(Notification.objects.filter(template_code="postop_booked").exists())

        appointment = Appointment.objects.get(pk=control.appointment_id)
        self.api.post(f"/api/v1/doctors/appointments/{appointment.id}/complete/", {
            "doctor_id": appointment.slot.doctor_id, "tactic": "surgery_not_indicated"}, format="json")
        child.refresh_from_db()
        self.assertEqual(child.status, PatientRoute.Status.COMPLETED)

        funnel = {row["code"]: row["value"] for row in AnalyticsService().funnel()}
        self.assertEqual(funnel["triggered"], 1)
        self.assertEqual(funnel["operated"], 1)
        self.assertEqual(funnel["control_visit"], 1)
        self.assertTrue(route.events.filter(event_type="no_show").exists())

    def test_not_engaged_after_30_days_and_banner(self):
        self.upload("Эхографические признаки полипа эндометрия.")
        route = PatientRoute.objects.get(patient_id=self.patient.id)
        for hours in (25, 48, 50, 220, 400):
            self.api.post("/api/v1/sim/advance/", {"hours": hours}, format="json")
        route.refresh_from_db()
        self.assertEqual(route.status, PatientRoute.Status.NOT_ENGAGED)
        templates = set(Notification.objects.filter(patient=self.patient).values_list("template_code", flat=True))
        self.assertTrue({"reminder_24h", "reminder_72h", "final_soft"} <= templates)
        self.assertTrue(CoordinatorTask.objects.filter(task_type="call_patient", route_id=route.id).exists())
        routes = self.api.get(f"/api/v1/routing/routes/?patient_id={self.patient.id}").json()["results"]
        self.assertEqual(routes[0]["status"], "not_engaged")
        self.assertEqual(RouteFact.objects.get(pk=route.id).close_status, "not_engaged")

    def test_correction_and_annulment(self):
        self.upload("Эхографические признаки полипа эндометрия.", external_id="UZI-9")
        # Повторная доставка того же файла — без дублей
        self.upload("Эхографические признаки полипа эндометрия.", external_id="UZI-9")
        self.assertEqual(PatientRoute.objects.filter(patient_id=self.patient.id).count(), 1)
        # Исправленный протокол: полипа нет, есть желчнокаменная болезнь
        self.upload("УЗ-признаки холецистолитиаза.", external_id="UZI-9")
        statuses = dict(PatientRoute.objects.filter(patient_id=self.patient.id).values_list("trigger_code", "status"))
        self.assertEqual(statuses["endometrial_polyp"], "cancelled")
        self.assertIn(statuses["gallstones"], {"notified", "created"})
        r = self.api.post("/api/v1/mis/events/", {"event_id": "a1", "event_type": "protocol.annulled",
                                                   "patient": {"mis_id": "AK-0001"}, "protocol": {"id": "UZI-9"}}, format="json")
        self.assertEqual(r.status_code, 202)
        self.assertFalse(PatientRoute.objects.filter(patient_id=self.patient.id).exclude(status="cancelled").exists())

    def test_generate_route_from_json_example(self):
        """Пример из постановки: «консультация маммолога и УЗИ через 6 мес»."""
        r = self.api.post("/api/v1/routing/routes/generate/", {
            "patient_id": str(self.patient.id), "external_id": "JSON-1",
            "recommendations": [
                {"text": "консультация маммолога", "kind": "consultation", "specialty_code": "mammologist"},
                {"text": "УЗИ через 6 мес", "kind": "diagnostics", "service": "УЗИ молочных желёз", "interval_days": 180},
            ]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        steps = r.json()["routes"][0]["steps"]
        self.assertEqual([s["title"] for s in steps], ["Консультация маммолога", "УЗИ молочных желёз (через 180 дн.)"])
        self.assertEqual(steps[1]["offset_days"], 180)

    def test_emergency_finding_only_escalates_to_staff(self):
        self.upload("Признаки тромбоза глубоких вен левой голени.")
        route = PatientRoute.objects.get(patient_id=self.patient.id)
        self.assertTrue(CoordinatorTask.objects.filter(route_id=route.id, task_type="emergency").exists())
        self.assertFalse(Notification.objects.filter(patient=self.patient).exists())
