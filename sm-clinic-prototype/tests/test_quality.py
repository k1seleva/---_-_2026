"""Заглушка вебхука МИС и проверка качества аналитики на пачке протоколов."""
import io
import zipfile

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.patients.models import Patient
from apps.processing.models import QualityRun, StudyDocument
from apps.processing.services.quality import QualityRunService, parse_labels
from apps.routing.models import PatientRoute

from .helpers import make_docx, seed, staff_client


def zipped(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buf.getvalue()


class MisWebhookStubTests(TransactionTestCase):
    def setUp(self):
        seed()
        cache.clear()
        self.api = APIClient()

    def post(self, event: dict):
        return self.api.post("/api/v1/mis/events/", event, format="json")

    def test_stub_validates_and_changes_nothing(self):
        patients = Patient.objects.count()
        r = self.post({"event_id": "1c-1", "event_type": "protocol.signed", "patient": {"mis_id": "AK-9999"},
                       "protocol": {"id": "UZI-1", "text": "ЗАКЛЮЧЕНИЕ: Полип эндометрия."}})
        self.assertEqual(r.status_code, 202, r.content)
        self.assertEqual(r.json()["status"], "stub")
        self.assertEqual(r.json()["would"], "принять протокол и запустить разбор")
        self.assertEqual(Patient.objects.count(), patients)                # пациент из события не создан
        self.assertFalse(StudyDocument.objects.exists())
        self.assertTrue(self.post({"event_id": "1c-1", "event_type": "protocol.signed", "patient": {"mis_id": "AK-9999"},
                                   "protocol": {"id": "UZI-1", "text": "x"}}).json()["duplicate"])
        journal = self.api.get("/api/v1/mis/events/").json()
        self.assertEqual(journal["mode"], "stub")
        self.assertEqual([e["event_id"] for e in journal["journal"]], ["1c-1"])

    def test_stub_rejects_events_outside_contract(self):
        r = self.post({"event_type": "patient.discharged", "patient": {}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Нет event_id", r.json()["detail"])
        self.assertIn("Нет patient.mis_id", r.json()["detail"])
        r = self.post({"event_id": "2", "event_type": "lab.result", "patient": {"mis_id": "AK-0001"}})
        self.assertIn("Неизвестный тип события", r.json()["detail"])

    @override_settings(MIS_WEBHOOK_MODE="live")
    def test_live_mode_processes_event(self):
        r = self.post({"event_id": "1c-2", "event_type": "protocol.signed", "patient": {"mis_id": "AK-0001"},
                       "protocol": {"id": "UZI-2", "study_type": "УЗИ ОМТ",
                                    "text": "ЗАКЛЮЧЕНИЕ: Эхографические признаки полипа эндометрия."}})
        self.assertEqual(r.json()["status"], "accepted", r.content)
        self.assertTrue(StudyDocument.objects.filter(external_id="UZI-2").exists())


class QualityRunTests(TransactionTestCase):
    def setUp(self):
        seed()
        self.api = APIClient()

    def test_dry_run_reports_metrics_and_leaves_no_trace(self):
        archive = zipped({
            "a.docx": make_docx("Эхографические признаки полипа эндометрия."),
            "b.docx": make_docx("Патологии не выявлено."),
            "c.docx": make_docx("Миома матки."),
            "broken.docx": b"not a docx",
        })
        labels = "file,expected_rules\na.docx,endometrial_polyp\nb.docx,\nc.docx,uterine_myoma; submucous_myoma\n"
        r = self.api.post("/api/v1/processing/quality-runs/", {
            "files": [SimpleUploadedFile("пачка.zip", archive)], "labels": SimpleUploadedFile("labels.csv", labels.encode()),
            "title": "тест"}, format="multipart")
        self.assertEqual(r.status_code, 202, r.content)
        run = QualityRun.objects.get(pk=r.json()["id"])
        self.assertEqual(run.status, QualityRun.Status.DONE, run.error)
        s = run.summary
        self.assertEqual((s["protocols"], s["read_errors"], s["markup_ok"]), (4, 1, 3))
        self.assertEqual(dict(s["rules"]), {"endometrial_polyp": 1, "uterine_myoma": 1})
        labelled = s["labelled"]
        self.assertEqual((labelled["tp"], labelled["fp"], labelled["fn"]), (2, 0, 1))
        self.assertEqual(labelled["precision"], 100.0)
        self.assertEqual(labelled["mismatches"], [{"file": "c.docx", "missing": ["submucous_myoma"], "extra": []}])
        # Прогон «всухую»: ни протоколов, ни пациентов, ни маршрутов, файлы удалены.
        self.assertFalse(StudyDocument.objects.exists())
        self.assertFalse(PatientRoute.objects.exists())
        self.assertFalse(QualityRunService.workdir(run).exists())
        page = staff_client().get(f"/processing/quality/{run.pk}/?show=mismatch")
        self.assertContains(page, "c.docx")
        self.assertContains(page, "Миома матки")
        csv_text = staff_client().get(f"/processing/quality/{run.pk}/csv/").content.decode("utf-8-sig")
        self.assertIn("expected_rules", csv_text.splitlines()[0])

    def test_limit_of_protocols_per_run(self):
        files = [SimpleUploadedFile(f"{i:02d}.docx", make_docx("Миома матки.")) for i in range(5)]
        with self.settings(QUALITY_MAX_PROTOCOLS=3):
            r = self.api.post("/api/v1/processing/quality-runs/", {"files": files}, format="multipart")
        run = QualityRun.objects.get(pk=r.json()["id"])
        self.assertEqual(run.files_total, 3)
        self.assertEqual([x["file"] for x in run.rejected], ["03.docx", "04.docx"])
        self.assertIn("до 3 протоколов", run.rejected[0]["error"])

    def test_labels_in_excel_formats(self):
        self.assertEqual(parse_labels("file;expected_rules\r\na.docx;gallstones, hernia\r\n".encode("cp1251")),
                         {"a.docx": ["gallstones", "hernia"]})
        self.assertEqual(parse_labels("﻿file,expected_rules\nb.docx,\n".encode()), {"b.docx": []})
