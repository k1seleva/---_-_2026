"""Вход пациента в свой кабинет и push с конкретным врачом. Синтетические данные."""
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TransactionTestCase
from rest_framework.test import APIClient

from apps.patients.models import Notification, Patient
from common import clock

from .helpers import make_docx, seed, staff_client

PASSWORD = "demo-2026"


class PatientLoginTests(TransactionTestCase):
    def setUp(self):
        seed()
        clock.reset()
        cache.clear()
        self.me = Patient.objects.get(external_mis_id="AK-0001")
        self.other = Patient.objects.get(external_mis_id="AK-0002")

    def test_no_patient_list_only_login(self):
        page = staff_client().get("/patient/").content.decode()
        self.assertIn("Вход в личный кабинет", page)
        self.assertNotIn(self.other.display_name, page)      # списка пациентов больше нет
        self.assertEqual(APIClient().get("/api/v1/patients/").status_code, 403)

    def test_cabinet_requires_login_and_returns_after_it(self):
        client = staff_client()
        r = client.get(f"/patient/{self.me.id}/settings/")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("/patient/?next="))
        # Номер карты кириллицей и строчными («ак-0001») — та же карта AK-0001.
        r = client.post("/patient/", {"card": "ак-0001", "password": PASSWORD, "next": f"/patient/{self.me.id}/settings/"})
        self.assertEqual(r["Location"], f"/patient/{self.me.id}/settings/")
        self.assertEqual(client.get(f"/patient/{self.me.id}/").status_code, 200)

    def test_other_patients_cabinet_and_api_are_closed(self):
        client = APIClient()
        client.post("/patient/", {"card": "AK-0001", "password": PASSWORD})
        r = client.get(f"/patient/{self.other.id}/")
        self.assertEqual(r["Location"], f"/patient/{self.me.id}/")
        self.assertEqual(client.get(f"/api/v1/patients/{self.other.id}/routes/").status_code, 403)
        self.assertEqual(client.get(f"/api/v1/patients/{self.me.id}/routes/").status_code, 200)

    def test_wrong_password_and_lockout(self):
        client = staff_client()
        r = client.post("/patient/", {"card": "AK-0001", "password": "wrong"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Неверный номер карты или пароль", r.content.decode())
        r = client.post("/patient/", {"card": "NO-SUCH", "password": "wrong"})
        self.assertIn("Неверный номер карты или пароль", r.content.decode())   # одинаковый ответ: карты не перебрать
        for _ in range(5):
            client.post("/patient/", {"card": "AK-0001", "password": "wrong"})
        r = client.post("/patient/", {"card": "AK-0001", "password": PASSWORD})
        self.assertIn("Слишком много попыток", r.content.decode())

    def test_logout(self):
        client = staff_client()
        client.post("/patient/", {"card": "AK-0001", "password": PASSWORD})
        client.post("/patient/logout/")
        self.assertEqual(client.get(f"/patient/{self.me.id}/").status_code, 302)


class PushWithDoctorTests(TransactionTestCase):
    def setUp(self):
        seed()
        clock.reset()
        cache.clear()  # блокировка входа из соседних тестов живёт в общем кеше
        self.patient = Patient.objects.get(external_mis_id="AK-0001")
        APIClient().post("/api/v1/processing/documents/", {
            "patient_id": self.patient.id, "location_code": "vdnh",
            "file": SimpleUploadedFile("p.docx", make_docx("Эхографические признаки полипа эндометрия."))}, format="multipart")

    def test_push_names_doctor_but_not_specialty_or_finding(self):
        push = Notification.objects.filter(patient=self.patient, channel="push", template_code="result_ready").first()
        if push is None:  # тихие часы модельного времени: push ушёл в кабинет, текст тот же
            push = Notification.objects.get(patient=self.patient, channel="push")
        self.assertIn("Рекомендуется консультация профильного специалиста", push.body)
        self.assertIn("врач", push.body)
        self.assertRegex(push.body, r"\d{1,2} \w+ в \d{2}:\d{2}")             # дата и время ближайшего приёма
        for secret in ("гинеколог", "полип", "эндометри"):
            self.assertNotIn(secret, push.body.lower())                     # экран блокировки видят посторонние
        self.assertLessEqual(len(push.title), 40)
        lk = Notification.objects.get(patient=self.patient, channel="lk", template_code="result_ready")
        self.assertIn("врач", lk.body)

    def test_cabinet_shows_phone_preview(self):
        client = staff_client()
        client.post("/patient/", {"card": "AK-0001", "password": PASSWORD})
        page = client.get(f"/patient/{self.patient.id}/?tab=notifications").content.decode()
        self.assertIn("Так push выглядит на телефоне", page)
        self.assertIn("Рекомендуется консультация профильного специалиста", page)


class PushWithoutFreeTimeTests(TransactionTestCase):
    def test_text_stays_clean_without_doctor_offer(self):
        from apps.patients.services.notifications import NotificationService
        from apps.patients.services.push import doctor_offer

        seed()
        clock.reset()
        patient = Patient.objects.get(external_mis_id="AK-0001")
        self.assertEqual(doctor_offer("no_such_specialty"), {})
        NotificationService().notify(patient, "reminder_24h", context={**doctor_offer("no_such_specialty")},
                                     dedupe_suffix="t")
        push = Notification.objects.get(patient=patient, channel="push", template_code="reminder_24h")
        self.assertEqual(push.body, "Рекомендуется консультация профильного специалиста.")
