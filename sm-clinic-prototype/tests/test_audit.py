"""Проверка достаточности рекомендаций и коррекция маршрута через координатора."""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from apps.audit.facade import AuditFacade
from apps.audit.models import AuditIssue, RecommendationAudit
from apps.coordinator.models import CoordinatorTask, RouteReview
from apps.coordinator.services.audit_reviews import AuditReviewService
from apps.doctors.models import Doctor
from apps.patients.models import Patient
from apps.routing.models import PatientRoute
from common import clock

from .helpers import GALLSTONE_PROTOCOL, make_protocol_docx, seed

BREAST_BIRADS4 = [
    "Исследование молочных желез",
    "Структура ткани: однородная.",
    "В левой молочной железе на 2 часах лоцируется гипоэхогенное образование 11х9 мм с неровными контурами.",
    "ЗАКЛЮЧЕНИЕ: Образование левой молочной железы. Категория BI-RADS 4.",
    "Рекомендовано: консультация маммолога.",
    "Данное заключение не является диагнозом.",
]
# Конкременты только в описании; заключение о другом органе; рекомендаций нет -> маршрута нет.
STONES_ONLY_IN_DESCRIPTION = [
    "УЗИ органов брюшной полости",
    "ЖЕЛЧНЫЙ ПУЗЫРЬ: Конкременты в полости желчного пузыря до 6 мм.",
    "ПОДЖЕЛУДОЧНАЯ ЖЕЛЕЗА: эхогенность повышена.",
    "ЗАКЛЮЧЕНИЕ: Диффузные изменения поджелудочной железы.",
    "Данное заключение не является диагнозом.",
]


class RecommendationAuditTests(TransactionTestCase):
    def setUp(self):
        seed()
        clock.reset()
        self.api = APIClient()
        self.patient = Patient.objects.get(external_mis_id="AK-0002")
        # Запись через API кабинета — от имени вошедшего пациента.
        self.api.post("/patient/", {"card": "AK-0002", "password": "demo-2026"})

    def upload(self, lines, external_id):
        file = SimpleUploadedFile("protocol.docx", make_protocol_docx(lines))
        r = self.api.post("/api/v1/processing/documents/", {"patient_id": self.patient.id, "file": file,
                                                            "external_id": external_id}, format="multipart")
        self.assertEqual(r.status_code, 202, r.content)
        return r.json()

    def test_vague_recommendation_covered_by_route_is_only_a_remark(self):
        doc = self.upload(GALLSTONE_PROTOCOL, "AUD-1")
        audit = RecommendationAudit.objects.get(document_id=doc["id"], source="protocol")
        # Матрица маршрутизации уже направила к хирургу -> правка маршрута не нужна, но замечания есть.
        self.assertEqual(audit.verdict, "needs_review")
        types = {i.issue_type: i for i in audit.issues.all()}
        self.assertIn("лечащего врача", types["missing_consultation"].message)
        self.assertEqual(types["missing_consultation"].severity, "minor")
        self.assertIn("образование 8 мм", types["not_in_conclusion"].message)
        self.assertFalse(RouteReview.objects.filter(audit_id=audit.id).exists())

    def test_missing_biopsy_goes_to_coordinator_and_correction_updates_route(self):
        doc = self.upload(BREAST_BIRADS4, "AUD-2")
        audit = RecommendationAudit.objects.get(document_id=doc["id"], source="protocol")
        self.assertEqual(audit.verdict, "insufficient")
        issue = audit.issues.get(issue_type="missing_diagnostics")
        self.assertEqual(issue.severity, "major")
        self.assertIn("BI-RADS 4", issue.evidence_quote)
        self.assertEqual(issue.proposed_operation["step_type"], "diagnostics")

        review = RouteReview.objects.get(audit_id=audit.id)
        task = CoordinatorTask.objects.get(dedupe_key=f"audit:{audit.id}")
        self.assertEqual(task.task_type, "review_recommendations")
        self.assertEqual(task.priority, CoordinatorTask.Priority.HIGH)

        AuditReviewService().decide(review, {str(issue.id): {"decision": "accepted", "comment": "Согласовано с маммологом"}},
                                    comment="Добавить биопсию")
        route = PatientRoute.objects.get(source_document_id=doc["id"])
        self.assertTrue(route.steps.filter(title="Биопсия образования молочной железы", source="coordinator").exists())
        issue.refresh_from_db()
        audit.refresh_from_db()
        self.assertEqual(issue.decision, AuditIssue.Decision.ACCEPTED)
        self.assertEqual(audit.status, RecommendationAudit.Status.RESOLVED)
        task.refresh_from_db()
        self.assertEqual(task.status, CoordinatorTask.Status.DONE)
        rule = next(r for r in AuditFacade.stats()["rules"] if r["rule_code"] == "birads_4_biopsy")
        self.assertEqual(rule["precision"], 100)

    def test_finding_only_in_description_creates_route_after_coordinator_decision(self):
        doc = self.upload(STONES_ONLY_IN_DESCRIPTION, "AUD-3")
        self.assertFalse(PatientRoute.objects.filter(source_document_id=doc["id"]).exists())
        audit = RecommendationAudit.objects.get(document_id=doc["id"], source="protocol")
        self.assertEqual(audit.verdict, "insufficient")
        issue = audit.issues.get(issue_type="missing_consultation")
        self.assertIn("в заключение не вынесено", issue.message)
        review = RouteReview.objects.get(audit_id=audit.id)
        self.assertIsNone(review.route_id)

        AuditReviewService().decide(review, {str(issue.id): {"decision": "accepted"}}, comment="Направить к хирургу")
        route = PatientRoute.objects.get(source_document_id=doc["id"])
        self.assertEqual(route.kind, PatientRoute.Kind.RECOMMENDATION)
        step = route.steps.get()
        self.assertEqual(step.specialty_code, "surgeon")
        self.assertEqual(step.status, "awaiting_booking")

    def test_rejected_issue_keeps_route_and_counts_against_rule(self):
        doc = self.upload(BREAST_BIRADS4, "AUD-4")
        audit = RecommendationAudit.objects.get(document_id=doc["id"], source="protocol")
        issue = audit.issues.get(issue_type="missing_diagnostics")
        steps_before = PatientRoute.objects.get(source_document_id=doc["id"]).steps.count()
        AuditReviewService().decide(RouteReview.objects.get(audit_id=audit.id),
                                    {str(issue.id): {"decision": "rejected", "comment": "Биопсия выполнена ранее"}},
                                    comment="Рекомендации достаточны")
        self.assertEqual(PatientRoute.objects.get(source_document_id=doc["id"]).steps.count(), steps_before)
        rule = next(r for r in AuditFacade.stats()["rules"] if r["rule_code"] == "birads_4_biopsy")
        self.assertEqual((rule["accepted"], rule["rejected"], rule["precision"]), (0, 1, 0))

    def test_visit_prescriptions_are_checked_against_protocol(self):
        doc = self.upload(BREAST_BIRADS4, "AUD-5")
        route = PatientRoute.objects.get(source_document_id=doc["id"])
        step = route.active_step
        slots = self.api.get(f"/api/v1/patients/{self.patient.id}/steps/{step.id}/slots/").json()["slots"]
        appt = self.api.post(f"/api/v1/patients/{self.patient.id}/book/", {"route_step_id": step.id, "slot_id": slots[0]["id"]},
                             format="json").json()
        doctor = Doctor.objects.get(pk=appt["doctor_id"])
        r = self.api.post(f"/api/v1/doctors/appointments/{appt['appointment_id']}/complete/", {
            "doctor_id": doctor.id, "tactic": "observation",
            "prescriptions": [{"kind": "diagnostics", "title": "УЗИ молочных желез", "due_in_days": 180}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        visit_audit = RecommendationAudit.objects.get(document_id=doc["id"], source="visit")
        self.assertEqual(visit_audit.verdict, "insufficient")
        # Маммолог закрыт самим приёмом; биопсии нет ни в назначениях, ни в маршруте.
        self.assertEqual(list(visit_audit.issues.values_list("issue_type", flat=True)), ["missing_diagnostics"])

    def test_dry_run_api_checks_doctor_text(self):
        doc = self.upload(BREAST_BIRADS4, "AUD-6")
        r = self.api.post("/api/v1/audit/check/", {"document_id": doc["id"],
                                                   "recommendations": ["консультация маммолога", "core-биопсия"]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["verdict"], "sufficient")
