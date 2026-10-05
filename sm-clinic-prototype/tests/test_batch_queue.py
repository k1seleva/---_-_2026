"""Очередь разбора пачки протоколов (processing/services/jobs.py).

Загрузка папки отвечает сразу, протоколы разбираются в фоне по одному, советы ИИ — после протоколов;
зависшие задачи возвращаются в очередь; если модель не отвечает, пачка не ждёт её по тайм-ауту на каждом
протоколе (предохранитель), а такие протоколы можно повторить. Модель — имитация Ollama, не Qwen.
Фоновые потоки в тестах не запускаются: очередь разбирается в тесте вызовом LocalWorker.drain().
"""
import time
import unittest
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TransactionTestCase, override_settings

from apps.patients.models import Patient
from apps.processing.models import ExtractionResult, ProcessingJob, StudyDocument
from apps.processing.services.ai_agent import circuit_for, reset_circuits
from apps.processing.services.jobs import LocalWorker, ProcessingQueue
from apps.routing.models import PatientRoute
from common import clock

from .fake_ollama import FakeOllama
from .helpers import make_protocol_docx, seed, staff_client
from .test_hybrid_llm import HAS_OLLAMA, PROTOCOL, llm_settings

NORM = ["УЗИ органов брюшной полости", "ПЕЧЕНЬ: контуры ровные, структура однородная.",
        "ЗАКЛЮЧЕНИЕ: патологических изменений не выявлено."]


def protocol_files(count: int) -> list[SimpleUploadedFile]:
    """Синтетическая «папка» протоколов: через один — с камнями в желчном, остальные норма."""
    return [SimpleUploadedFile(f"AK-0001_protocol_{i:02d}.docx", make_protocol_docx(
        (PROTOCOL if i % 2 else NORM) + [f"Номер исследования {i}."])) for i in range(count)]


class QueueTestCase(TransactionTestCase):
    def setUp(self):
        seed()
        clock.reset()
        reset_circuits()
        self.addCleanup(reset_circuits)
        # Потоки очереди не стартуют: разбор идёт в тесте (drain), чтобы проверка была детерминированной.
        patcher = mock.patch.object(LocalWorker, "ensure_started")
        self.started = patcher.start()
        self.addCleanup(patcher.stop)

    def upload_folder(self, count: int) -> dict:
        r = staff_client().post("/processing/", {"files": protocol_files(count), "location_code": ""},
                          HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(r.status_code, 200, r.content[:300])
        return r.json()


@override_settings(PROCESSING_QUEUE="thread")
class BackgroundQueueTests(QueueTestCase):
    def test_folder_upload_answers_at_once_and_protocols_wait_in_queue(self):
        started = time.monotonic()
        data = self.upload_folder(6)
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(data["accepted"], 6)
        self.assertTrue(self.started.called)
        # Ни один протокол не разобран внутри запроса загрузки.
        self.assertEqual(ProcessingJob.objects.filter(status="queued").count(), 6)
        self.assertFalse(ExtractionResult.objects.exists())

        progress = staff_client().get(f"/processing/batches/{data['batch_id']}/progress/").json()
        self.assertEqual((progress["total"], progress["queued"], progress["done"], progress["finished"]), (6, 6, 0, False))
        inbox = staff_client().get(data["url"]).content.decode()
        self.assertIn("batch-progress", inbox)
        self.assertIn("Разбор идёт в фоне", inbox)

        # В шапке рабочего места виден идущий разбор со ссылкой на ход пачки (из любого раздела).
        header = staff_client().get("/processing/").content.decode()
        self.assertIn("Идёт разбор: 6", header)
        self.assertIn(f"batch={data['batch_id']}", header)

        self.assertEqual(LocalWorker.drain(), 12)  # 6 протоколов, затем 6 советов
        self.assertNotIn("Идёт разбор", staff_client().get("/processing/").content.decode())
        progress = staff_client().get(f"/processing/batches/{data['batch_id']}/progress/").json()
        self.assertEqual((progress["done"], progress["queued"], progress["failed"], progress["finished"]), (6, 0, 0, True))
        self.assertIsNotNone(progress["avg_ms"])
        self.assertEqual(set(progress["steps_avg"]), {"read", "extraction", "markup", "analysis", "save"})
        job = ProcessingJob.objects.first()
        self.assertEqual((job.attempts, job.worker.split(":")[0]), (1, "thread"))
        self.assertGreater(job.timings["total"], 0)

    def test_advice_waits_until_protocols_of_the_batch_are_read(self):
        order = []
        real_run, real_advice = ProcessingQueue.run_claimed, ProcessingQueue.run_next_advice
        with mock.patch.object(ProcessingQueue, "run", side_effect=lambda job_id: (order.append("protocol"),
                                                                                  real_run(job_id))[1]), \
                mock.patch.object(ProcessingQueue, "run_next_advice",
                                  side_effect=lambda: real_advice() and (order.append("advice") or True)), \
                override_settings(ROUTING_ADVISOR={**settings.ROUTING_ADVISOR,
                                                   "MODE": "demo"}):
            self.upload_folder(4)
            LocalWorker.drain()
        self.assertEqual(order[:4], ["protocol"] * 4)
        self.assertIn("advice", order)
        self.assertFalse(ExtractionResult.objects.filter(advice_meta__status="pending").exists())

    def test_job_is_claimed_by_exactly_one_worker(self):
        self.upload_folder(1)
        job = ProcessingJob.objects.get()
        self.assertTrue(ProcessingQueue.claim(job.id, "celery:a:1"))
        self.assertFalse(ProcessingQueue.claim(job.id, "celery:b:2"))
        job.refresh_from_db()
        self.assertEqual((job.status, job.worker, job.attempts), ("running", "celery:a:1", 1))

    def test_stuck_job_returns_to_queue_then_fails_after_max_attempts(self):
        self.upload_folder(1)
        job = ProcessingJob.objects.get()
        ProcessingQueue.claim(job.id, "celery:gone:1")
        ProcessingJob.objects.filter(pk=job.pk).update(started_at=clock.now() - timedelta(hours=1))
        self.assertEqual(ProcessingQueue.recover_stale(), 1)
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts), ("queued", 1))

        with override_settings(PROCESSING_MAX_ATTEMPTS=2):
            ProcessingQueue.claim(job.id, "celery:gone:1")
            ProcessingJob.objects.filter(pk=job.pk).update(started_at=clock.now() - timedelta(hours=1))
            ProcessingQueue.recover_stale()
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertIn("2 попытки", job.error)
        self.assertEqual(job.document.status, StudyDocument.Status.FAILED)

    def test_job_of_a_dead_server_process_is_requeued_on_start(self):
        self.upload_folder(1)
        job = ProcessingJob.objects.get()
        import socket

        ProcessingQueue.claim(job.id, f"thread:{socket.gethostname()}:999999")
        self.assertEqual(ProcessingQueue.recover_stale(check_dead=True), 1)
        self.assertEqual(ProcessingJob.objects.get().status, "queued")

    def test_cli_commands_read_protocols_themselves(self):
        """manage.py-команды живут недолго: очередь в них работает синхронно (см. settings)."""
        with override_settings(PROCESSING_QUEUE="sync"):
            self.upload_folder(2)
        self.assertEqual(ProcessingJob.objects.filter(status="done").count(), 2)


@unittest.skipUnless(HAS_OLLAMA, "нужен пакет langchain-ollama")
@override_settings(PROCESSING_QUEUE="thread")
class ModelOutageTests(QueueTestCase):
    def test_hung_model_does_not_stall_the_batch_and_protocols_can_be_retried(self):
        with FakeOllama(delay=3) as slow:
            cfg = llm_settings(slow.url)
            cfg["AI_AGENT"] = {**cfg["AI_AGENT"], "TIMEOUT_SEC": 1, "COOLDOWN_SEC": 600, "WAIT_MINUTES": 0}
            cfg["ROUTING_ADVISOR"] = {**cfg["ROUTING_ADVISOR"], "TIMEOUT_SEC": 1}
            with override_settings(**cfg):
                data = self.upload_folder(4)
                # Пациент выбран при загрузке: по разбору строится маршрут (проверим, что повтор его не задвоит).
                StudyDocument.objects.update(patient_id=Patient.objects.get(external_mis_id="AK-0001").id,
                                             identity=StudyDocument.Identity.MANUAL)
                started = time.monotonic()
                LocalWorker.drain()
                elapsed = time.monotonic() - started
                progress = staff_client().get(f"/processing/batches/{data['batch_id']}/progress/").json()
            calls = len(slow.requests)
        # Модель спросили один раз: после тайм-аута остальные протоколы идут по словарю без ожидания.
        self.assertLessEqual(calls, 2)
        self.assertLess(elapsed, 30)
        jobs = list(ProcessingJob.objects.all())
        self.assertTrue(all(j.status == "done" and j.llm_status == "error" for j in jobs))
        self.assertEqual((progress["done"], progress["llm_errors"], progress["model_paused"]), (4, 4, True))
        page = staff_client().get(data["url"]).content.decode()
        self.assertIn("Повторить с ИИ (4)", page)
        routes_before = PatientRoute.objects.count()
        self.assertGreater(routes_before, 0)

        # Модель снова отвечает: «Повторить с ИИ» ставит протоколы в очередь, маршруты не задваиваются.
        reset_circuits()
        with FakeOllama() as fast, override_settings(**llm_settings(fast.url)):
            r = staff_client().post(f"/processing/batches/{data['batch_id']}/retry/", {"what": "llm"})
            self.assertEqual(r.status_code, 302)
            self.assertEqual(ProcessingJob.objects.filter(status="queued").count(), 4)
            LocalWorker.drain()
        latest = {}
        for j in ProcessingJob.objects.order_by("created_at"):
            latest[j.document_id] = j
        self.assertTrue(all(j.llm_status == "ok" for j in latest.values()))
        # Маршруты по найденному словарём остались прежними; модель могла добавить новые, но не повторы.
        keys = list(PatientRoute.objects.values_list("source_document_id", "trigger_rule_id"))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertGreaterEqual(len(keys), routes_before)

    def test_waiting_for_model_pauses_the_queue(self):
        with override_settings(AI_AGENT={**settings.AI_AGENT,
                                         "COOLDOWN_SEC": 600, "WAIT_MINUTES": 10}):
            circuit_for().trip("нет связи с сервером модели")
            self.upload_folder(1)
            self.assertFalse(LocalWorker.step())
            self.assertEqual(ProcessingJob.objects.get().status, "queued")
