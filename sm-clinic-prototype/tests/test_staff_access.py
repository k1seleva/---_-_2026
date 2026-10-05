"""Три отдельные роли: пациент (свой кабинет), координатор (/coordinator/, /processing/), врач (/doctor/, только свой приём).
И проверка процесса-исполнителя очереди, не падающая на Windows."""
from unittest import mock

from django.core.cache import cache
from django.test import Client, SimpleTestCase, TransactionTestCase

from apps.doctors.models import Appointment, Doctor
from apps.patients.models import Patient
from apps.processing.services import jobs
from common import clock

from .helpers import seed, staff_client


class StaffAccessTests(TransactionTestCase):
    def setUp(self):
        seed()
        clock.reset()
        cache.clear()
        self.addCleanup(cache.clear)

    def test_anonymous_goes_to_login_of_the_right_role(self):
        for url, login in [("/coordinator/inbox/", "/coordinator/login/"), ("/processing/", "/coordinator/login/"),
                           ("/doctor/", "/doctor/login/")]:
            with self.subTest(url=url):
                r = Client().get(url)
                self.assertEqual(r.status_code, 302)
                self.assertTrue(r["Location"].startswith(f"{login}?next="), r["Location"])
        self.assertContains(Client().get("/coordinator/login/"), "Вход: координатор")
        self.assertContains(Client().get("/doctor/login/"), "Вход: врач")

    def test_coordinator_login_returns_to_requested_page(self):
        client = Client()
        r = client.post("/coordinator/login/", {"username": "coordinator", "password": "demo-2026",
                                                "next": "/coordinator/routes/"})
        self.assertEqual(r["Location"], "/coordinator/routes/")
        page = client.get("/coordinator/inbox/").content.decode()
        self.assertIn("Входящие протоколы", page)
        self.assertIn("Выйти", page)
        self.assertNotIn("Приём врача", page)

    def test_wrong_password_and_lockout(self):
        client = Client()
        for _ in range(5):
            r = client.post("/coordinator/login/", {"username": "coordinator", "password": "nope"})
            self.assertContains(r, "Неверный логин или пароль", status_code=400)
        r = client.post("/coordinator/login/", {"username": "coordinator", "password": "demo-2026"})
        self.assertContains(r, "Слишком много попыток", status_code=400)

    def test_account_of_another_role_cannot_enter(self):
        r = Client().post("/coordinator/login/", {"username": "doctor1", "password": "demo-2026"})
        self.assertContains(r, "нет роли «Координатор»", status_code=400)
        r = Client().post("/doctor/login/", {"username": "coordinator", "password": "demo-2026"})
        self.assertContains(r, "нет роли «Врач»", status_code=400)

    def test_roles_do_not_open_each_others_sections(self):
        coordinator, doctor = staff_client("coordinator"), staff_client("doctor")
        self.assertEqual(coordinator.get("/doctor/")["Location"], "/coordinator/")
        self.assertEqual(doctor.get("/coordinator/inbox/")["Location"], "/doctor/")
        self.assertEqual(doctor.get("/processing/")["Location"], "/doctor/")
        # Пациент, вошедший в кабинет, в рабочее место сотрудников не попадает.
        patient = Client()
        patient.post("/patient/", {"card": "AK-0001", "password": "demo-2026"})
        self.assertTrue(patient.get("/coordinator/")["Location"].startswith("/coordinator/login/"))

    def test_doctor_sees_only_own_schedule_and_appointments(self):
        own = Doctor.objects.get(user__username="doctor1")
        other = Doctor.objects.get(user__username="doctor2")
        client = staff_client("doctor")
        self.assertEqual(client.get("/doctor/")["Location"], f"/doctor/{own.id}/")
        self.assertEqual(client.get(f"/doctor/{other.id}/")["Location"], f"/doctor/{own.id}/")
        page = client.get(f"/doctor/{own.id}/").content.decode()
        self.assertIn("Моё расписание и приём", page)
        self.assertNotIn("Входящие", page)

        slot = other.slots.first()
        patient = Patient.objects.get(external_mis_id="AK-0001")
        appt = Appointment.objects.create(slot=slot, patient_id=patient.id)
        self.assertEqual(client.get(f"/doctor/appointment/{appt.id}/")["Location"], "/doctor/")
        r = client.post(f"/doctor/appointment/{appt.id}/no-show/")
        self.assertEqual(r["Location"], "/doctor/")
        appt.refresh_from_db()
        self.assertNotEqual(appt.status, "no_show")

    def test_logout_closes_workspace_but_keeps_patient_session(self):
        client = staff_client("coordinator")
        client.post("/patient/", {"card": "AK-0001", "password": "demo-2026"})
        r = client.post("/staff/logout/")
        self.assertEqual(r["Location"], "/coordinator/login/")
        self.assertTrue(client.get("/coordinator/")["Location"].startswith("/coordinator/login/"))
        patient = Patient.objects.get(external_mis_id="AK-0001")
        self.assertEqual(client.get(f"/patient/{patient.id}/").status_code, 200)

    def test_removed_role_takes_effect_at_once(self):
        client = staff_client("coordinator")
        from django.contrib.auth.models import User

        User.objects.get(username="coordinator").groups.clear()
        self.assertTrue(client.get("/coordinator/")["Location"].startswith("/coordinator/login/"))


class WorkerLivenessTests(SimpleTestCase):
    """os.kill(pid, 0) на Windows не проверяет процесс (WinError 87 → SystemError): очередь не должна падать."""

    def test_unexpected_error_means_alive_not_crash(self):
        with mock.patch.object(jobs, "_pid_alive", side_effect=SystemError("WinError 87")):
            self.assertFalse(jobs._worker_dead(f"thread:{jobs.socket.gethostname()}:999999"))

    def test_windows_branch_does_not_use_os_kill(self):
        with mock.patch.object(jobs.os, "name", "nt"), mock.patch.object(jobs, "_pid_alive_windows", return_value=False) as win, \
                mock.patch.object(jobs.os, "kill", side_effect=AssertionError("os.kill на Windows")):
            self.assertTrue(jobs._worker_dead(f"thread:{jobs.socket.gethostname()}:999999"))
        win.assert_called_once_with(999999)

    def test_posix_dead_process(self):
        self.assertTrue(jobs._worker_dead(f"thread:{jobs.socket.gethostname()}:999999"))
