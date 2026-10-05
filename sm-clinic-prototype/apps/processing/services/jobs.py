"""
Очередь разбора протоколов: загрузка пачки отвечает сразу, протоколы разбираются по одному в фоне.

Почему так. Разбор протокола вместе с Qwen занимает минуты, а локальная модель отвечает на запросы по
очереди. Раньше (Celery в eager-режиме) вся пачка разбиралась внутри одного запроса загрузки: 20 протоколов
по 2 минуты — 40 минут ожидания страницы, обрыв соединения, «зависание». При настоящем Celery с несколькими
процессами протоколы одновременно стучались в одну модель, ждали друг друга и падали по тайм-ауту.

Режимы (settings.PROCESSING_QUEUE):
* thread — фоновый поток в процессе сайта (runserver без Redis). Поток стартует с первым запросом к сайту,
  забирает задачи из базы (ProcessingJob в статусе queued), поэтому после перезапуска сервера очередь
  продолжается сама. Советы по маршрутизации идут после того, как разобраны все протоколы.
* celery — задачи в очереди llm, её слушает отдельный воркер с concurrency=PROCESSING_WORKERS (docker compose).
* sync — разбор прямо в запросе (тесты и отладка).

Надёжность во всех режимах: задачу забирает ровно один исполнитель (атомарный захват queued → running);
зависшая задача (исполнитель упал) через PROCESSING_STALE_MINUTES возвращается в очередь, после
PROCESSING_MAX_ATTEMPTS попыток помечается ошибкой с понятной причиной; пока модель не отвечает
(предохранитель ai_agent.circuit_for), очередь ждёт её до LLM_WAIT_MINUTES, а не гонит протоколы по одному словарю.
"""
import logging
import os
import socket
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import F

from common import clock

from ..models import ExtractionResult, ProcessingJob, StudyDocument

logger = logging.getLogger(__name__)

IDLE_WAIT_SEC = 15


def mode() -> str:
    return settings.PROCESSING_QUEUE


def worker_name() -> str:
    return f"{mode()}:{socket.gethostname()}:{os.getpid()}"[:64]


class ProcessingQueue:
    """Поставить в очередь, забрать, вернуть зависшее, повторить разбор."""

    # ------------------------------------------------------------ постановка
    @staticmethod
    def enqueue(job: ProcessingJob) -> None:
        if mode() == "thread":
            LocalWorker.ensure_started()
            LocalWorker.wake()
            return
        if mode() == "sync":
            ProcessingQueue.run(job.id)
            return
        from ..tasks import process_document

        result = process_document.delay(str(job.id))
        ProcessingJob.objects.filter(pk=job.pk).update(celery_task_id=result.id or "")

    @staticmethod
    def enqueue_advice(result_id) -> bool:
        """True — совет поставлен в фоновую очередь (thread) или уже готов (sync); False — задача Celery."""
        if mode() == "sync":
            from .routing_advice import RoutingAdviceService

            RoutingAdviceService().generate(result_id)
            return True
        if mode() != "thread":
            return False
        LocalWorker.ensure_started()
        LocalWorker.wake()
        return True

    # ------------------------------------------------------------ захват и выполнение
    @staticmethod
    def claim(job_id, worker: str = "") -> bool:
        """Атомарно забрать задачу: из двух исполнителей (повторная доставка, второй воркер) выиграет один."""
        return bool(ProcessingJob.objects.filter(pk=job_id, status=ProcessingJob.Status.QUEUED).update(
            status=ProcessingJob.Status.RUNNING, started_at=clock.now(), worker=worker or worker_name(),
            attempts=F("attempts") + 1))

    @staticmethod
    def run_claimed(job_id):
        """Разобрать уже забранную задачу (или в режиме sync — забрать и разобрать)."""
        from .pipeline import DocumentProcessingService

        return DocumentProcessingService().run(str(job_id))

    run = run_claimed

    @classmethod
    def run_next(cls) -> bool:
        """Взять самую старую задачу из очереди и разобрать. False — очередь пуста."""
        for job_id in ProcessingJob.objects.filter(status=ProcessingJob.Status.QUEUED).order_by(
                "created_at").values_list("id", flat=True)[:5]:
            if cls.claim(job_id):
                cls.run(job_id)
                return True
        return False

    @staticmethod
    def run_next_advice() -> bool:
        """Советы — после протоколов: пока в очереди есть протоколы, координатор сначала получает разбор."""
        from .routing_advice import RoutingAdviceService

        result = ExtractionResult.objects.filter(advice_meta__status="pending").order_by("created_at").first()
        if result is None:
            return False
        RoutingAdviceService().generate(result.id)
        return True

    @staticmethod
    def waiting_for_model() -> bool:
        """Модель недавно не ответила: подождать её, пока пауза не дольше LLM_WAIT_MINUTES."""
        from .ai_agent import circuit_for

        circuit = circuit_for()
        return circuit.blocked() and circuit.open_for() < settings.AI_AGENT.get("WAIT_MINUTES", 10) * 60

    # ------------------------------------------------------------ зависшие задачи
    @staticmethod
    def recover_stale(*, check_dead: bool = False) -> int:
        """Задачи «в работе» дольше срока (или взятые потоком, которого уже нет) — обратно в очередь.
        После PROCESSING_MAX_ATTEMPTS попыток — ошибка: протокол виден во входящих, его можно повторить."""
        stale_before = clock.now() - timedelta(minutes=settings.PROCESSING_STALE_MINUTES)
        running = ProcessingJob.objects.filter(status=ProcessingJob.Status.RUNNING).select_related("document")
        count = 0
        for job in running:
            dead = check_dead and _worker_dead(job.worker)
            if not dead and (job.started_at is None or job.started_at > stale_before):
                continue
            count += 1
            if job.attempts >= settings.PROCESSING_MAX_ATTEMPTS:
                job.status, job.finished_at = ProcessingJob.Status.FAILED, clock.now()
                job.error = (f"Разбор не завершился за {job.attempts} попытки: обработчик останавливался "
                             "(перезапуск сервера или зависание модели). Повторите разбор.")
                job.save(update_fields=["status", "finished_at", "error", "updated_at"])
                StudyDocument.objects.filter(pk=job.document_id).update(status=StudyDocument.Status.FAILED)
                _publish_failed(job)
                logger.warning("Задача %s помечена ошибкой после %s попыток", job.id, job.attempts)
            else:
                ProcessingJob.objects.filter(pk=job.pk).update(status=ProcessingJob.Status.QUEUED, worker="")
                StudyDocument.objects.filter(pk=job.document_id).update(status=StudyDocument.Status.UPLOADED)
                logger.warning("Задача %s вернулась в очередь (попытка %s)", job.id, job.attempts)
                if mode() == "celery":
                    from ..tasks import process_document

                    process_document.delay(str(job.id))
        if count and mode() == "thread":
            LocalWorker.wake()
        return count

    # ------------------------------------------------------------ повтор
    @classmethod
    def reprocess(cls, document_ids, *, only_llm_errors: bool = False) -> int:
        """Разобрать заново (кнопка «Повторить с Qwen» или «Повторить неудавшиеся»). Создаётся новая задача,
        прежний результат остаётся в истории; маршруты не задваиваются (события идемпотентны)."""
        jobs = []
        documents = StudyDocument.objects.filter(pk__in=list(document_ids)).exclude(
            status__in=[StudyDocument.Status.ANNULLED, StudyDocument.Status.SUPERSEDED])
        for doc in documents:
            if doc.jobs.filter(status__in=[ProcessingJob.Status.QUEUED, ProcessingJob.Status.RUNNING]).exists():
                continue
            last = doc.jobs.order_by("-created_at").first()
            if only_llm_errors and not (last and last.llm_status == "error"):
                continue
            with transaction.atomic():
                job = ProcessingJob.objects.create(document=doc)
                doc.status = StudyDocument.Status.UPLOADED
                doc.save(update_fields=["status", "updated_at"])
            jobs.append(job)
        for job in jobs:
            transaction.on_commit(lambda job=job: cls.enqueue(job))
        return len(jobs)


def _worker_dead(worker: str) -> bool:
    """Исполнитель на этой же машине, процесса которого уже нет (сервер перезапустили посреди разбора).
    Сомнение трактуется как «жив»: зависшую задачу всё равно вернёт срок PROCESSING_STALE_MINUTES."""
    try:
        _mode, host, pid = worker.rsplit(":", 2)
        pid = int(pid)
    except ValueError:
        return False
    if host != socket.gethostname()[:len(host)] or pid == os.getpid():
        return False
    try:
        return not _pid_alive(pid)
    except Exception:  # noqa: BLE001 — проверка процесса не должна ронять очередь ни на одной ОС
        logger.warning("Не удалось проверить процесс %s, считаем его живым", pid, exc_info=True)
        return False


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)  # сигнал 0 только проверяет процесс (POSIX)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # процесс есть, но чужой
    return True


def _pid_alive_windows(pid: int) -> bool:
    """На Windows os.kill(pid, 0) не проверяет процесс, а пытается его завершить (WinError 87 / SystemError).
    Спрашиваем систему напрямую: OpenProcess + GetExitCodeProcess."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    process_query_limited_information, still_active, access_denied = 0x1000, 259, 5
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == access_denied  # нет доступа — процесс есть; иначе его нет
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _publish_failed(job: ProcessingJob) -> None:
    from common.events import contracts
    from common.events.bus import publish

    doc = job.document
    publish(contracts.DOCUMENT_FAILED, {
        "document_id": str(doc.id), "patient_id": str(doc.patient_id or ""), "filename": doc.original_filename,
        "location_code": doc.location_code, "batch_id": str(doc.batch_id or ""), "error": job.error[:500],
    }, event_id=f"{contracts.DOCUMENT_FAILED}:{job.id}")


class LocalWorker:
    """Фоновые потоки очереди в процессе сайта (режим thread). Число потоков — PROCESSING_WORKERS."""

    _lock = threading.Lock()
    _threads: list[threading.Thread] = []
    _wake = threading.Event()

    @classmethod
    def ensure_started(cls) -> None:
        if mode() != "thread":
            return
        with cls._lock:
            cls._threads = [t for t in cls._threads if t.is_alive()]
            missing = settings.PROCESSING_WORKERS - len(cls._threads)
            if missing <= 0:
                return
            first_start = not cls._threads
            for i in range(missing):
                thread = threading.Thread(target=cls._loop, args=(first_start and i == 0,),
                                          name=f"protocol-queue-{len(cls._threads) + 1}", daemon=True)
                cls._threads.append(thread)
                thread.start()

    @classmethod
    def wake(cls) -> None:
        cls._wake.set()

    @classmethod
    def run_forever(cls) -> None:
        """Цикл очереди в текущем потоке (команда process_queue)."""
        cls._loop(False)

    @classmethod
    def _loop(cls, recover_on_start: bool) -> None:
        if recover_on_start:
            cls._safe(lambda: ProcessingQueue.recover_stale(check_dead=True))
        last_recover = time.monotonic()
        while True:
            did = cls._safe(cls.step)
            if time.monotonic() - last_recover > 60:
                cls._safe(lambda: ProcessingQueue.recover_stale(check_dead=True))
                last_recover = time.monotonic()
            if not did:
                cls._wake.wait(IDLE_WAIT_SEC)
                cls._wake.clear()

    @staticmethod
    def step() -> bool:
        """Один шаг: протокол из очереди, иначе совет; пока модель на паузе — ждём её."""
        if ProcessingQueue.waiting_for_model():
            return False
        return ProcessingQueue.run_next() or ProcessingQueue.run_next_advice()

    @staticmethod
    def _safe(fn):
        close_old_connections()
        try:
            return fn()
        except Exception:  # noqa: BLE001 — поток очереди не должен умирать из-за одного протокола
            logger.exception("Ошибка в фоновой очереди разбора")
            return False
        finally:
            close_old_connections()

    @classmethod
    def drain(cls, limit: int = 1000) -> int:
        """Разобрать всё, что в очереди, в текущем потоке (команда process_queue --once и тесты)."""
        done = 0
        while done < limit and (ProcessingQueue.run_next() or ProcessingQueue.run_next_advice()):
            done += 1
        return done


def in_background(task, *args) -> None:
    """Долгая задача без очереди протоколов (проверка качества): поток в режиме thread, иначе Celery/sync."""
    if mode() != "thread":
        task.delay(*args)
        return

    def target():
        LocalWorker._safe(lambda: task(*args))

    threading.Thread(target=target, name=f"bg-{task.name.rsplit('.', 1)[-1]}", daemon=True).start()


def batch_progress(batch_id) -> dict:
    """Ход разбора пачки: сколько в очереди, в работе, готово, с ошибкой; среднее время и оценка остатка."""
    from collections import Counter

    docs = list(StudyDocument.objects.filter(batch_id=batch_id).values("id", "original_filename", "status"))
    ids = [d["id"] for d in docs]
    latest: dict = {}
    for job in ProcessingJob.objects.filter(document_id__in=ids).order_by("created_at"):
        latest[job.document_id] = job
    statuses = Counter(j.status for j in latest.values())
    done = [j for j in latest.values() if j.status == ProcessingJob.Status.DONE and j.timings.get("total")]
    avg_ms = round(sum(j.timings["total"] for j in done) / len(done)) if done else None
    running = [j for j in latest.values() if j.status == ProcessingJob.Status.RUNNING]
    names = {d["id"]: d["original_filename"] for d in docs}
    queued = statuses.get(ProcessingJob.Status.QUEUED, 0)
    left = queued + len(running)
    eta_sec = round(avg_ms * left / max(1, settings.PROCESSING_WORKERS) / 1000) if avg_ms and left else None
    advice_pending = ExtractionResult.objects.filter(document_id__in=ids, advice_meta__status="pending").count()
    from .ai_agent import circuit_for

    circuit = circuit_for()
    return {
        "batch_id": str(batch_id), "total": len(docs), "queued": queued, "running": len(running),
        "done": statuses.get(ProcessingJob.Status.DONE, 0), "failed": statuses.get(ProcessingJob.Status.FAILED, 0),
        "llm_errors": sum(1 for j in latest.values() if j.status == ProcessingJob.Status.DONE and j.llm_status == "error"),
        "advice_pending": advice_pending, "avg_ms": avg_ms, "eta_sec": eta_sec,
        "current": [{"filename": names.get(j.document_id, ""), "seconds": round((clock.now() - j.started_at).total_seconds())
                     if j.started_at else 0} for j in running],
        "steps_avg": _steps_avg(done),
        "finished": left == 0,
        "model_paused": circuit.blocked(), "model_reason": circuit.reason,
        "mode": mode(),
    }


def _steps_avg(jobs: list[ProcessingJob]) -> dict:
    if not jobs:
        return {}
    keys = ("read", "extraction", "markup", "analysis", "save")
    return {k: round(sum(j.timings.get(k, 0) for j in jobs) / len(jobs)) for k in keys}
