"""Рабочее место координатора и кабинет пациента: пачки, обезличенные протоколы, категории, каналы связи, тексты."""
import io
import re
import zipfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TransactionTestCase
from rest_framework.test import APIClient

from apps.coordinator.models import ProtocolCase
from apps.coordinator.services.cases import IdentityDecisionService
from apps.patients.models import Notification, Patient
from apps.patients.services.notifications import NotificationService
from apps.patients.services.preferences import PreferenceService
from apps.processing.services import pipeline
from apps.routing.models import PatientRoute
from common import clock

from .helpers import make_docx, make_protocol_docx, seed, staff_client

# Видимый текст страницы без кода, стилей и скриптов: там английские ключи допустимы.
HIDDEN_RE = re.compile(r"<(script|style|code|pre|template)[^>]*>.*?</\1>", re.S)
SNAKE_RE = re.compile(r"\b[a-z]+_[a-z_]+\b")


def visible_text(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", HIDDEN_RE.sub("", html))


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buf.getvalue()


class BatchUploadTests(TransactionTestCase):
    def setUp(self):
        seed()
        self.api = APIClient()

    def upload(self, *files):
        r = self.api.post("/api/v1/processing/batches/", {"files": list(files), "location_code": "vdnh"}, format="multipart")
        self.assertEqual(r.status_code, 202, r.content)
        return r.json()

    def test_zip_skips_junk_and_reports_unsupported(self):
        archive = make_zip({
            "клиника/AK-0001.docx": make_docx("Эхографические признаки полипа эндометрия."),
            "клиника/AK-0002.docx": make_docx("Патологии не выявлено."),
            "__MACOSX/клиника/._AK-0001.docx": b"junk",
            "клиника/.DS_Store": b"junk",
            "клиника/список.txt": b"not a protocol",
        })
        report = self.upload(SimpleUploadedFile("пачка.zip", archive))
        self.assertEqual(report["accepted"], 2)
        self.assertEqual([r["error"] for r in report["rejected"]], ["Формат не поддерживается"])
        batch = self.api.get(f"/api/v1/processing/batches/{report['batch_id']}/")
        self.assertEqual(batch.status_code, 200)

    def test_zip_limits_and_broken_archive(self):
        with mock.patch.object(pipeline.ZipLimits, "MAX_FILES", 1):
            files, errors = pipeline.expand_zip("big.zip", make_zip({"a.docx": b"1", "b.docx": b"2"}))
        self.assertEqual(files, [])
        self.assertIn("больше 1 файлов", errors[0]["error"])
        files, errors = pipeline.expand_zip("bad.zip", b"not a zip")
        self.assertEqual(errors[0]["error"], "Архив повреждён или это не zip")

    def test_duplicate_is_counted_not_processed_twice(self):
        data = make_docx("Эхографические признаки полипа эндометрия.")
        self.upload(SimpleUploadedFile("a.docx", data))
        report = self.upload(SimpleUploadedFile("a-копия.docx", data))
        self.assertEqual((report["accepted"], report["duplicates"]), (0, 1))


class UnmatchedProtocolTests(TransactionTestCase):
    """Протокол без известного номера карты: обезличенный пациент, без сообщений, маршрут после решения координатора."""

    def setUp(self):
        seed()
        clock.reset()
        self.api = APIClient()

    def test_placeholder_then_identify_then_route(self):
        r = self.api.post("/api/v1/processing/batches/", {
            "files": [SimpleUploadedFile("без-карты.docx", make_docx("Эхографические признаки полипа эндометрия."))],
            "location_code": "vdnh"}, format="multipart")
        self.assertEqual(r.status_code, 202, r.content)
        case = ProtocolCase.objects.get()
        self.assertEqual(case.category, ProtocolCase.Category.UNMATCHED)
        placeholder = Patient.objects.get(is_anonymous=True)
        self.assertTrue(placeholder.placeholder_code.startswith("ОП-"))
        # Обезличенному пациенту ничего не отправляем и маршрут не строим.
        self.assertFalse(Notification.objects.filter(patient=placeholder).exists())
        self.assertFalse(PatientRoute.objects.exists())

        patient = Patient.objects.get(external_mis_id="AK-0001")
        IdentityDecisionService().assign(placeholder.id, patient.id, user="тест")
        case.refresh_from_db()
        self.assertFalse(case.is_placeholder)
        self.assertIn("route_started", case.reasons)
        # Рекомендаций в синтетическом протоколе нет — значит, после привязки протокол ждёт разбора, а не «всё на месте».
        self.assertEqual(case.category, ProtocolCase.Category.NEEDS_REVIEW)
        self.assertIn("no_recommendations", case.reasons)
        route = PatientRoute.objects.get(patient_id=patient.id)
        self.assertEqual(route.trigger_code, "endometrial_polyp")
        self.assertTrue(Notification.objects.filter(patient=patient, template_code="result_ready").exists())


class CaseCategoryTests(TransactionTestCase):
    def setUp(self):
        seed()
        self.api = APIClient()
        self.patient = Patient.objects.get(external_mis_id="AK-0002")

    def upload(self, data: bytes) -> ProtocolCase:
        r = self.api.post("/api/v1/processing/documents/", {"patient_id": self.patient.id, "location_code": "vdnh",
                                                            "file": SimpleUploadedFile("p.docx", data)}, format="multipart")
        self.assertEqual(r.status_code, 202, r.content)
        return ProtocolCase.objects.get(document_id=r.json()["id"])

    def test_norm_has_no_findings(self):
        case = self.upload(make_docx("Патологии не выявлено."))
        self.assertEqual(case.category, ProtocolCase.Category.NO_FINDINGS)

    def test_changes_without_conclusion_need_review(self):
        case = self.upload(make_protocol_docx([
            "УЗИ органов брюшной полости",
            "Желчный пузырь: в просвете конкременты до 12 мм.",
            "Печень: в правой доле анэхогенное образование 9 мм.",
        ]))
        self.assertIn("no_conclusion", case.reasons)
        self.assertEqual(case.category, ProtocolCase.Category.NEEDS_REVIEW)


class ChannelTests(TransactionTestCase):
    def setUp(self):
        seed()
        clock.reset()
        self.service = PreferenceService()
        self.young = Patient.objects.get(external_mis_id="AK-0001")   # 1985
        self.senior = Patient.objects.get(external_mis_id="AK-0006")  # 1955

    def test_push_by_default_and_lk_always(self):
        self.assertEqual(self.service.channels_for(self.young, "result_ready"), ["lk", "push"])
        self.service.save(self.young, {"results": set()})
        self.assertEqual(self.service.channels_for(self.young, "result_ready"), ["lk"])

    def test_sms_hint_only_for_seniors_and_only_until_answered(self):
        self.assertFalse(self.service.sms_recommended(self.young))
        self.assertTrue(self.service.sms_recommended(self.senior))
        self.service.enable_sms_everywhere(self.senior)
        self.assertFalse(self.service.sms_recommended(self.senior))
        self.assertTrue(all(r.push and r.sms for r in self.service.matrix(self.senior)))  # push не выключили
        other = Patient.objects.get(external_mis_id="AK-0003")  # 1958
        self.service.dismiss_sms_hint(other)
        self.assertFalse(self.service.sms_recommended(other))

    def at_local_hour(self, hour: int) -> None:
        local = clock.now().astimezone()
        target = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        delta = (target - local).total_seconds() % (24 * 3600)  # ближайшее такое время впереди
        clock.advance(hours=delta / 3600)

    def test_quiet_hours_suppress_calls_but_not_booking_push(self):
        self.service.save(self.young, {"appointments": {"push", "call"}, "reminders": {"push", "call"}})
        self.at_local_hour(23)
        sent = {n.channel: n for n in NotificationService().notify(self.young, "booking_confirmed", context={"when": "завтра"})}
        self.assertEqual(sent["call"].status, Notification.Status.SUPPRESSED)
        self.assertEqual(sent["push"].status, Notification.Status.SENT)      # подтверждение записи приходит сразу
        reminder = {n.channel: n for n in NotificationService().notify(self.young, "reminder_24h", dedupe_suffix="n")}
        self.assertEqual(reminder["push"].status, Notification.Status.SUPPRESSED)
        self.assertEqual(reminder["lk"].status, Notification.Status.SENT)    # в кабинете сообщение есть всегда

    def test_calls_go_out_in_daytime(self):
        self.service.save(self.young, {"reminders": {"call"}})
        self.at_local_hour(12)
        sent = {n.channel: n for n in NotificationService().notify(self.young, "reminder_24h", dedupe_suffix="d")}
        self.assertEqual(sent["call"].status, Notification.Status.SENT)


class NotificationReadTests(TransactionTestCase):
    def setUp(self):
        seed()
        clock.reset()
        self.api = APIClient()
        self.patient = Patient.objects.get(external_mis_id="AK-0001")
        self.api.post("/patient/", {"card": "AK-0001", "password": "demo-2026"})  # API кабинета — только после входа
        NotificationService().notify(self.patient, "reminder_24h", channels=["lk"], dedupe_suffix="1")
        NotificationService().notify(self.patient, "reminder_72h", channels=["lk"], dedupe_suffix="2")

    def test_list_does_not_mark_read(self):
        url = f"/api/v1/patients/{self.patient.id}/notifications/"
        self.assertEqual(self.api.get(url).json()["unread"], 2)
        self.assertEqual(self.api.get(url).json()["unread"], 2)          # открыть колокольчик ≠ прочитать
        first = self.api.get(url).json()["items"][0]["id"]
        self.api.post(f"{url}read/", {"id": first}, format="json")
        self.assertEqual(self.api.get(url).json()["unread"], 1)
        self.api.post(f"{url}read/", {}, format="json")
        self.assertEqual(self.api.get(url).json()["unread"], 0)

    def test_preferences_api_roundtrip(self):
        url = f"/api/v1/patients/{self.patient.id}/preferences/"
        data = self.api.get(url).json()
        self.assertTrue(data["lk_always_on"])
        rows = data["rows"]
        self.assertTrue(all(r["push"] and not r["sms"] and not r["call"] for r in rows))
        rows[0]["sms"] = True
        self.assertEqual(self.api.put(url, rows, format="json").status_code, 200)
        self.assertTrue(self.api.get(url).json()["rows"][0]["sms"])


class InterfaceTextTests(TransactionTestCase):
    """На экранах нет английских ключей и медианы: всё через справочник подписей (common/labels.py)."""

    def setUp(self):
        seed()
        clock.reset()
        api = APIClient()
        self.patient = Patient.objects.get(external_mis_id="AK-0001")
        api.post("/api/v1/processing/documents/", {"patient_id": self.patient.id, "location_code": "vdnh",
                                                   "file": SimpleUploadedFile("p.docx", make_docx("Эхографические признаки полипа эндометрия."))},
                 format="multipart")
        api.post("/api/v1/processing/batches/", {"files": [SimpleUploadedFile("x.docx", make_docx("Миома матки."))]},
                 format="multipart")

    def test_main_pages_speak_russian(self):
        client = staff_client()
        client.post("/patient/", {"card": "AK-0001", "password": "demo-2026"})
        case = ProtocolCase.objects.filter(patient_id=self.patient.id).first()
        pages = ["/", "/coordinator/", "/coordinator/inbox/", "/coordinator/inbox/?category=all", "/coordinator/unmatched/",
                 "/coordinator/routes/", "/coordinator/tasks/", "/coordinator/analytics/", "/coordinator/analytics/clinics/",
                 "/coordinator/analytics/features/", "/coordinator/settings/tags/", "/coordinator/settings/priorities/",
                 f"/coordinator/cases/{case.document_id}/", "/processing/",
                 f"/patient/{self.patient.id}/", f"/patient/{self.patient.id}/?tab=routes",
                 f"/patient/{self.patient.id}/?tab=notifications", f"/patient/{self.patient.id}/settings/"]
        for url in pages:
            with self.subTest(url=url):
                r = client.get(url)
                self.assertEqual(r.status_code, 200)
                text = visible_text(r.content.decode())
                self.assertEqual(SNAKE_RE.findall(text), [])
                self.assertNotIn("медиан", text.lower())
