"""Сценарии модуля обработки: приём протоколов (по одному и пачкой), определение пациента и разбор AI-агентом."""
import io
import logging
import time
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction

from apps.patients.facade import PatientsFacade
from common import clock
from common.events import contracts
from common.events.bus import publish

from ..models import ExtractionResult, Finding, ProcessingJob, ProtocolSegment, StudyDocument, UploadBatch
from .ai_agent import FindingExtractor, get_finding_extractor
from .annotation import ProtocolAnnotation, get_annotator, load_thresholds
from .dictionary import load_dictionary
from .markers import analyze_protocol
from .text_extraction import TextExtractionError, card_number_from_filename, get_extractor

logger = logging.getLogger(__name__)


class UploadValidationError(ValueError):
    pass


@dataclass
class UploadCommand:
    filename: str
    data: bytes
    # Пусто — пациента определит обработка по номеру карты в протоколе или в имени файла.
    patient_id: uuid.UUID | str | None = None
    external_id: str = ""
    location_code: str = ""
    batch_id: uuid.UUID | None = None


class DocumentIngestService:
    """Приём протокола: валидация, дедупликация, версионирование, постановка в очередь."""

    @staticmethod
    def validate(filename: str, size: int) -> None:
        ext = Path(filename).suffix.lower()
        if ext not in settings.UPLOAD_ALLOWED_EXTENSIONS:
            raise UploadValidationError(f"Формат {ext or 'без расширения'} не поддерживается "
                                        f"(можно: {', '.join(settings.UPLOAD_ALLOWED_EXTENSIONS)})")
        if size > settings.UPLOAD_MAX_BYTES:
            raise UploadValidationError(f"Файл больше {settings.UPLOAD_MAX_BYTES // (1024 * 1024)} МБ")
        if size == 0:
            raise UploadValidationError("Пустой файл")

    def find_duplicate(self, cmd: UploadCommand, checksum: str) -> StudyDocument | None:
        """Повторная доставка того же файла — не создаём дубль (у того же пациента или в общей очереди)."""
        qs = StudyDocument.objects.filter(checksum=checksum).exclude(status=StudyDocument.Status.ANNULLED)
        if cmd.patient_id:
            qs = qs.filter(patient_id=cmd.patient_id)
        return qs.first()

    def ingest(self, cmd: UploadCommand) -> StudyDocument:
        self.validate(cmd.filename, len(cmd.data))
        ext = Path(cmd.filename).suffix.lower()
        checksum = StudyDocument.compute_checksum(cmd.data)
        if duplicate := self.find_duplicate(cmd, checksum):
            duplicate.is_duplicate = True
            return duplicate

        with transaction.atomic():
            version = 1
            if cmd.external_id:
                previous = StudyDocument.objects.filter(external_id=cmd.external_id).order_by("-version").first()
                if previous:
                    version = previous.version + 1
                    previous.status = StudyDocument.Status.SUPERSEDED
                    previous.save(update_fields=["status", "updated_at"])
            document = StudyDocument.objects.create(
                patient_id=cmd.patient_id or None,
                identity=StudyDocument.Identity.MANUAL if cmd.patient_id else StudyDocument.Identity.PENDING,
                card_number=card_number_from_filename(cmd.filename),
                batch_id=cmd.batch_id,
                external_id=cmd.external_id,
                version=version,
                original_filename=Path(cmd.filename).name[:255],
                file_format=ext.lstrip("."),
                checksum=checksum,
                location_code=cmd.location_code,
            )
            document.file.save(f"{document.id}{ext}", ContentFile(cmd.data), save=True)
            job = ProcessingJob.objects.create(document=document)
            publish(contracts.DOCUMENT_UPLOADED, {
                "document_id": str(document.id), "patient_id": str(cmd.patient_id or ""),
                "filename": document.original_filename, "batch_id": str(cmd.batch_id or ""),
                "location_code": cmd.location_code, "version": version, "external_id": cmd.external_id,
            })
            transaction.on_commit(lambda: self._enqueue(job))
        document.is_duplicate = False
        return document

    @staticmethod
    def _enqueue(job: ProcessingJob) -> None:
        """Протокол — в очередь разбора (jobs.ProcessingQueue): загрузка пачки не ждёт модель."""
        from .jobs import ProcessingQueue

        ProcessingQueue.enqueue(job)

    def annul(self, external_id: str = "", reason: str = "", *, document_id=None) -> int:
        """Аннулирование протокола в МИС или отклонение координатором: закрыть маршрут и остановить сообщения."""
        qs = StudyDocument.objects.filter(pk=document_id) if document_id else StudyDocument.objects.filter(external_id=external_id)
        documents = list(qs.exclude(status=StudyDocument.Status.ANNULLED))
        for doc in documents:
            doc.status = StudyDocument.Status.ANNULLED
            doc.save(update_fields=["status", "updated_at"])
            publish(contracts.DOCUMENT_ANNULLED, {"document_id": str(doc.id), "patient_id": str(doc.patient_id or ""),
                                                  "reason": reason})
        return len(documents)


# ------------------------------------------------------------------ пачка протоколов
@dataclass
class IncomingFile:
    name: str
    data: bytes


@dataclass
class BatchReport:
    batch: UploadBatch
    documents: list[StudyDocument] = field(default_factory=list)
    duplicates: list[StudyDocument] = field(default_factory=list)


class ZipLimits:
    """Ограничения распаковки (защита от zip-бомб и мусора)."""

    MAX_FILES = 500
    MAX_TOTAL_BYTES = 200 * 1024 * 1024
    SKIP_PREFIXES = ("__MACOSX/", ".")


def expand_zip(name: str, data: bytes) -> tuple[list[IncomingFile], list[dict]]:
    """Достаёт протоколы из zip: только допустимые форматы, без служебных и скрытых файлов, с лимитами."""
    files, errors, total = [], [], 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return [], [{"file": name, "error": "Архив повреждён или это не zip"}]
    entries = [i for i in archive.infolist() if not i.is_dir()]
    if len(entries) > ZipLimits.MAX_FILES:
        return [], [{"file": name, "error": f"В архиве больше {ZipLimits.MAX_FILES} файлов"}]
    for info in entries:
        inner = info.filename
        base = Path(inner).name
        if inner.startswith(ZipLimits.SKIP_PREFIXES) or "/__MACOSX/" in inner or base.startswith((".", "~$")):
            continue
        if Path(base).suffix.lower() not in settings.UPLOAD_ALLOWED_EXTENSIONS:
            errors.append({"file": f"{name} › {inner}", "error": "Формат не поддерживается"})
            continue
        total += info.file_size
        if info.file_size > settings.UPLOAD_MAX_BYTES or total > ZipLimits.MAX_TOTAL_BYTES:
            errors.append({"file": f"{name} › {inner}", "error": "Превышен допустимый размер после распаковки"})
            continue
        files.append(IncomingFile(name=base, data=archive.read(info)))
    return files, errors


class BatchIngestService:
    """Массовая загрузка: файлы, папки и zip-архивы — один конвейер, одна сводка по пачке."""

    def __init__(self, ingest: DocumentIngestService | None = None) -> None:
        self.ingest_service = ingest or DocumentIngestService()

    def ingest(self, files: list[IncomingFile], *, location_code: str = "", patient_id=None,
               source: str = UploadBatch.Source.MANUAL, created_by: str = "") -> BatchReport:
        batch = UploadBatch.objects.create(source=source, location_code=location_code, created_by=created_by[:150])
        report = BatchReport(batch=batch)
        rejected: list[dict] = []
        expanded: list[IncomingFile] = []
        for f in files:
            if Path(f.name).suffix.lower() == ".zip":
                if batch.source == UploadBatch.Source.MANUAL:
                    batch.source = UploadBatch.Source.ZIP
                inner, errors = expand_zip(f.name, f.data)
                expanded += inner
                rejected += errors
            else:
                expanded.append(f)
        for f in expanded:
            try:
                document = self.ingest_service.ingest(UploadCommand(
                    filename=f.name, data=f.data, patient_id=patient_id, location_code=location_code, batch_id=batch.id))
            except UploadValidationError as exc:
                rejected.append({"file": f.name, "error": str(exc)})
                continue
            (report.duplicates if document.is_duplicate else report.documents).append(document)
        batch.files_total = len(expanded) + sum(1 for r in rejected if " › " not in r["file"])
        batch.accepted, batch.duplicates, batch.rejected = len(report.documents), len(report.duplicates), rejected
        batch.save()
        return report


# ------------------------------------------------------------------ разбор протокола
class DocumentProcessingService:
    """Разбор протокола: текст -> AI-агент -> находки -> пациент -> событие для маршрутизации.

    Зависимости внедряются через конструктор (DIP), поэтому в тестах легко подменить LLM.
    """

    def __init__(self, extractor: FindingExtractor | None = None, dictionary_loader=load_dictionary,
                 annotator_factory=get_annotator) -> None:
        self.extractor = extractor or get_finding_extractor()
        self.dictionary_loader = dictionary_loader
        self.annotator_factory = annotator_factory

    def run(self, job_id: str) -> ExtractionResult | None:
        from .jobs import ProcessingQueue

        # Задачу из очереди забирает ровно один исполнитель; в режиме sync её никто не забирал — забираем здесь.
        ProcessingQueue.claim(job_id)
        job = ProcessingJob.objects.select_related("document").get(pk=job_id)
        document = job.document
        # Повторный разбор того же протокола (например, «Повторить с Qwen»): события публикуются с новым ключом,
        # обработчики других модулей идемпотентны (маршруты не задваиваются, добавляются только новые).
        rerun = document.jobs.exclude(pk=job.pk).filter(status=ProcessingJob.Status.DONE).exists()
        job.status, job.started_at = ProcessingJob.Status.RUNNING, clock.now()
        job.engine = self.extractor.name
        job.prompt_version = settings.AI_AGENT["PROMPT_VERSION"]
        job.save()
        document.status = StudyDocument.Status.PROCESSING
        document.save(update_fields=["status", "updated_at"])
        timer = StepTimer()

        try:
            with timer("read"), document.file.open("rb") as fh:
                extracted = get_extractor(document.original_filename).extract(fh.read())
            dictionary = self.dictionary_loader()
            payload, (annotation, annotation_summary) = self._extract_and_annotate(extracted, dictionary, timer)
        except (TextExtractionError, Exception) as exc:  # noqa: B014 — фиксируем любую ошибку в job
            logger.exception("Ошибка обработки документа %s", document.id)
            job.status, job.error, job.finished_at = ProcessingJob.Status.FAILED, str(exc), clock.now()
            job.timings = timer.as_dict()
            job.save()
            document.status = StudyDocument.Status.FAILED
            document.save(update_fields=["status", "updated_at"])
            publish(contracts.DOCUMENT_FAILED, {
                "document_id": str(document.id), "patient_id": str(document.patient_id or ""),
                "filename": document.original_filename, "location_code": document.location_code,
                "batch_id": str(document.batch_id or ""), "error": str(exc)[:500],
            }, event_id=f"{contracts.DOCUMENT_FAILED}:{job.id}")
            return None

        payload.study_date = payload.study_date or (extracted.study_date.isoformat() if extracted.study_date else None)
        payload.study_type = payload.study_type or extracted.study_type
        segments_report = annotation_summary.get("grounding")
        annotation_summary["grounding"] = {"extraction": payload.grounding,
                                           **({"segments": segments_report} if segments_report is not None else {})}
        with timer("analysis"):
            analysis = self._analyze(extracted.text, annotation, payload, dictionary)

        with timer("save"), transaction.atomic():
            document.raw_text = extracted.text
            document.study_type = payload.study_type
            document.study_date = extracted.study_date
            document.performed_by = extracted.performed_by
            document.card_number = extracted.card_number or document.card_number
            self._identify(document, payload)
            document.status = StudyDocument.Status.PROCESSED
            document.save()
            result = ExtractionResult.objects.create(
                document=document, job=job, payload=payload.model_dump(), conclusion=payload.conclusion,
                summary_for_patient=payload.summary_for_patient, engine=payload.engine,
                dictionary_version=payload.dictionary_version, annotation_summary=annotation_summary,
                analysis=analysis,
            )
            if annotation is not None:
                ProtocolSegment.objects.bulk_create(_segment_row(result, a) for a in annotation.segments)
            Finding.objects.bulk_create(
                Finding(result=result, **f.model_dump(include={
                    "code", "label", "evidence_quote", "negated", "uncertain", "attributes",
                    "confidence", "severity", "span_start", "span_end", "rule_id", "source",
                }))
                for f in payload.findings
            )
            job.status, job.finished_at = ProcessingJob.Status.DONE, clock.now()
            job.llm_status = llm_status(payload.engines, annotation_summary)
            job.save()
            publish_document_result(document, result, rerun_key=str(job.id) if rerun else "")
            transaction.on_commit(lambda: enqueue_routing_advice(result.id))
        job.timings = timer.as_dict()
        ProcessingJob.objects.filter(pk=job.pk).update(timings=job.timings)
        return result

    def _extract_and_annotate(self, extracted, dictionary, timer: "StepTimer"):
        """Находки заключения и разметка фрагментов — два независимых запроса к модели. При
        AI_AGENT["PARALLEL_CALLS"] >= 2 они идут одновременно (если сервер модели это умеет), иначе по очереди."""
        annotator = self.annotator_factory(dictionary)

        def extract():
            with timer("extraction"):
                return self.extractor.extract(extracted.text, study_type=extracted.study_type, dictionary=dictionary)

        def annotate(study_type: str):
            with timer("markup"):
                return self._annotate(extracted.text, study_type, annotator)

        if settings.AI_AGENT.get("PARALLEL_CALLS", 1) < 2:
            payload = extract()
            return payload, annotate(payload.study_type or extracted.study_type)
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as pool:
            markup = pool.submit(_in_thread, annotate, extracted.study_type)
            payload = extract()
            return payload, markup.result()

    @staticmethod
    def _identify(document: StudyDocument, payload) -> None:
        """Пациент протокола: выбран при загрузке, найден по номеру карты или «обезличенная» карточка.

        По обезличенной карточке маршрут не строится, пока координатор не подтвердит пациента:
        отправить сообщение не тому человеку хуже, чем задержаться на разбор.
        """
        if document.patient_id:
            return
        if document.card_number and (patient_id := PatientsFacade.find_by_mis_id(document.card_number)):
            document.patient_id, document.identity = patient_id, StudyDocument.Identity.MATCHED
            return
        document.patient_id = PatientsFacade.ensure_placeholder(
            card_number=document.card_number, location_code=document.location_code,
            hint={"filename": document.original_filename, "study_type": payload.study_type or "",
                  "study_date": payload.study_date or ""})
        document.identity = StudyDocument.Identity.UNMATCHED

    @staticmethod
    def _annotate(text: str, study_type: str, annotator) -> tuple[ProtocolAnnotation | None, dict]:
        """Разметка (подсветки). Сбой разметки не останавливает маршрутизацию, но фиксируется явно."""
        try:
            annotation = annotator.annotate(text, study_type=study_type)
        except Exception as exc:  # noqa: BLE001 — сохраняем причину, текст протокола не теряется
            logger.exception("Ошибка разметки протокола")
            return None, {"error": f"Разметка не выполнена: {exc}"}
        return annotation, annotation.summary()


    @staticmethod
    def _analyze(text: str, annotation: ProtocolAnnotation | None, payload, dictionary) -> dict:
        """Маркеры и триггеры с позициями. Сбой не останавливает обработку: разбор пересчитается при открытии."""
        if annotation is None:
            return {}
        try:
            return build_analysis(text, [a.as_dict() for a in annotation.segments],
                                  [f.model_dump() for f in payload.findings], dictionary, study_type=payload.study_type)
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка построения маркеров и триггеров")
            return {}


def build_analysis(text: str, segments: list[dict], findings: list[dict], dictionary, *, study_type: str = "") -> dict:
    """Маркеры, перекрытия и триггеры. Правила матрицы — через фасад маршрутизации (только чтение)."""
    from apps.routing.facade import RoutingFacade

    return analyze_protocol(text, segments, findings, dictionary, thresholds=load_thresholds(),
                            route_matches=RoutingFacade.match_rules_detail(findings), study_type=study_type)


def enqueue_routing_advice(result_id) -> None:
    """Советы ИИ-агента по маршрутизации — фоном, после сохранения разбора (не задерживают маршрут)."""
    from ..tasks import generate_routing_advice
    from .routing_advice import configured_engine, configured_model

    engine = configured_engine()
    if engine == "off":
        return
    # Пока модель отвечает (при очереди Celery — фоном), на странице видно «Qwen готовит ответ».
    pending = {"status": "pending", "engine": engine, "model": configured_model()}
    ExtractionResult.objects.filter(pk=result_id).update(advice_meta=pending)
    from .jobs import ProcessingQueue

    try:
        # Фоновая очередь сама возьмёт совет, когда разберёт протоколы пачки.
        if ProcessingQueue.enqueue_advice(result_id):
            return
        generate_routing_advice.delay(str(result_id))
    except Exception as exc:  # noqa: BLE001 — очередь недоступна: совет можно запросить позже со страницы протокола
        logger.exception("Не удалось поставить в очередь советы по маршрутизации")
        result = ExtractionResult.objects.filter(pk=result_id).first()
        if result is not None and (result.advice_meta or {}).get("status") == "pending":
            result.advice_meta = {**pending, "status": "error", "error": f"советы не запущены: {exc}"[:300]}
            result.save(update_fields=["advice_meta", "updated_at"])


class StepTimer:
    """Время шагов разбора (мс) — для прогресса пачки и поиска узкого места («куда уходят 2 минуты»)."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.steps: dict[str, int] = {}

    @contextmanager
    def __call__(self, step: str):
        started = time.monotonic()
        try:
            yield
        finally:
            self.steps[step] = self.steps.get(step, 0) + round((time.monotonic() - started) * 1000)

    def as_dict(self) -> dict:
        return {**self.steps, "total": round((time.monotonic() - self.started) * 1000)}


def _in_thread(fn, *args):
    """Шаг в отдельном потоке: своё подключение к БД закрываем сами, иначе оно повиснет."""
    from django.db import connection

    try:
        return fn(*args)
    finally:
        connection.close()


def llm_status(engines: dict, annotation_summary: dict) -> str:
    """ok — модель ответила на оба запроса; error — хотя бы на один нет (протокол можно повторить с Qwen)."""
    statuses = {((engines or {}).get("llm") or {}).get("status", "off"),
                ((annotation_summary or {}).get("llm") or {}).get("status", "off")}
    return "error" if "error" in statuses else "ok" if "ok" in statuses else "off"


def publish_document_result(document: StudyDocument, result: ExtractionResult, *, rerun_key: str = "") -> None:
    """Событие по итогам разбора. Пациент не определён — DOCUMENT_UNMATCHED (маршрут не строится);
    иначе DOCUMENT_PROCESSED / DOCUMENT_CORRECTED (новая версия) — вход маршрутизации и проверки рекомендаций."""
    summary = result.annotation_summary or {}
    segments = [_segment_payload(s) for s in result.segments.all()]
    payload = {
        "document_id": str(document.id),
        "external_id": document.external_id,
        "version": document.version,
        "patient_id": str(document.patient_id),
        "location_code": document.location_code,
        "card_number": document.card_number,
        "filename": document.original_filename,
        "batch_id": str(document.batch_id or ""),
        "extraction": result.payload,
        # Разметка: итог + значимые фрагменты (для модуля проверки рекомендаций и категорий координатора).
        "annotation": {
            "summary": {k: v for k, v in summary.items() if k != "grounding"},
            "segments": [s for s in segments if s["significant"] or s["kind"] == "recommendation"],
        },
    }
    if document.identity == StudyDocument.Identity.UNMATCHED:
        rerun = f":rerun:{rerun_key}" if rerun_key else ""
        publish(contracts.DOCUMENT_UNMATCHED, payload, event_id=f"{contracts.DOCUMENT_UNMATCHED}:{document.id}{rerun}")
        return
    event_type = contracts.DOCUMENT_CORRECTED if document.version > 1 else contracts.DOCUMENT_PROCESSED
    # После подтверждения пациента координатором событие публикуется ещё раз — с новым ключом.
    suffix = ":confirmed" if document.identity == StudyDocument.Identity.CONFIRMED else ""
    suffix += f":rerun:{rerun_key}" if rerun_key else ""
    publish(event_type, payload, event_id=f"{event_type}:{document.id}{suffix}")


def _segment_payload(s: ProtocolSegment) -> dict:
    from .annotation import SIGNIFICANT_TYPES

    types = list(dict.fromkeys(h["type"] for h in s.highlights))
    return {
        "id": s.seq, "text": s.text, "start": s.span_start, "end": s.span_end, "section": s.section, "organ": s.organ,
        "kind": s.kind, "finding_codes": s.finding_codes, "signs": s.signs, "negated_codes": s.negated_codes,
        "attributes": s.attributes, "highlights": s.highlights, "highlight_types": types, "emergency": s.emergency,
        "uncertain": s.uncertain, "linked_to": s.linked_to, "link_reason": s.link_reason,
        "not_in_conclusion": s.not_in_conclusion, "sources": s.sources, "significant": bool(set(types) & SIGNIFICANT_TYPES),
    }


class IdentityService:
    """Координатор подтвердил пациента обезличенной карточки: протоколы переходят к пациенту и идут в маршрутизацию."""

    def assign(self, placeholder_id, patient_id) -> int:
        documents = list(StudyDocument.objects.filter(patient_id=placeholder_id,
                                                      identity=StudyDocument.Identity.UNMATCHED))
        for document in documents:
            with transaction.atomic():
                document.patient_id, document.identity = patient_id, StudyDocument.Identity.CONFIRMED
                document.save(update_fields=["patient_id", "identity", "updated_at"])
                if document.status == StudyDocument.Status.PROCESSED and (result := document.latest_result):
                    publish_document_result(document, result)
        return len(documents)


def _segment_row(result: ExtractionResult, a) -> ProtocolSegment:
    s = a.segment
    return ProtocolSegment(
        result=result, seq=s.id, section=s.section, organ=s.organ[:128], text=s.text,
        span_start=s.start, span_end=s.end, kind=a.kind, emergency=a.emergency,
        finding_codes=a.finding_codes, signs=a.signs, negated_codes=a.negated_codes, attributes=a.attributes,
        highlights=a.highlights, uncertain=a.uncertain, linked_to=a.linked_to, link_reason=a.link_reason[:255],
        not_in_conclusion=a.not_in_conclusion, sources=a.sources, llm_labels=a.llm_labels,
    )
