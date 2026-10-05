"""
Processing Module — хранит протоколы исследований и результат их разбора.

Слабая связность: пациент задаётся как patient_id (UUID из модуля пациентов),
а не ForeignKey. Внутри модуля связи — обычные FK.
"""
import hashlib

from django.db import models

from common.models import BaseModel


class FindingDefinition(BaseModel):
    """Словарь находок (настройка, а не хардкод). Используется правиловым экстрактором
    и передаётся LLM как перечень допустимых кодов. Редактируется в админке."""

    class Severity(models.TextChoices):
        ROUTINE = "routine", "Плановая"
        URGENT = "urgent", "Срочная"
        EMERGENCY = "emergency", "Экстренная (только эскалация персоналу)"

    code = models.SlugField(max_length=64, unique=True)
    title = models.CharField(max_length=255)
    study_types = models.JSONField(default=list, blank=True, help_text="Пусто = любые исследования")
    patterns = models.JSONField(default=list, help_text="Регулярные выражения (синонимы, сокращения)")
    exclude_patterns = models.JSONField(default=list, blank=True)
    severity = models.CharField(max_length=16, choices=Severity.choices, default=Severity.ROUTINE)
    # Тексты для пациента утверждает врач-эксперт: без вероятностей, прогнозов и советов по лечению.
    patient_title = models.CharField(max_length=255, blank=True, help_text="Как назвать находку пациенту")
    patient_explanation = models.TextField(blank=True, help_text="Что это значит простыми словами (2–3 предложения)")
    is_active = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} — {self.title}"


class SpecialtyAlias(models.Model):
    """Сопоставление формулировок рекомендаций со справочником специальностей:
    «конс. маммолога» -> mammologist."""

    specialty_code = models.SlugField(max_length=64)
    pattern = models.CharField(max_length=255)

    def __str__(self) -> str:
        return f"{self.pattern} -> {self.specialty_code}"


class UploadBatch(BaseModel):
    """Пачка протоколов: одна загрузка (перетаскивание, zip, папка-наблюдатель или API МИС)."""

    class Source(models.TextChoices):
        MANUAL = "manual", "Загрузка вручную"
        ZIP = "zip", "Zip-архив"
        FOLDER = "folder", "Папка-наблюдатель"
        BROWSER_FOLDER = "browser_folder", "Папка на компьютере координатора"
        API = "api", "API МИС"

    source = models.CharField(max_length=16, choices=Source.choices, default=Source.MANUAL)
    location_code = models.CharField(max_length=64, blank=True)
    created_by = models.CharField(max_length=150, blank=True)
    files_total = models.PositiveIntegerField(default=0)
    accepted = models.PositiveIntegerField(default=0)
    duplicates = models.PositiveIntegerField(default=0)
    rejected = models.JSONField(default=list, blank=True, help_text='[{"file": "...", "error": "..."}]')

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Пачка {self.created_at:%d.%m %H:%M}: {self.accepted} из {self.files_total}"


class StudyDocument(BaseModel):
    """Протокол диагностического исследования (версия). Исправление протокола в МИС
    создаёт новую версию с тем же external_id; аннулирование — статус ANNULLED."""

    class Format(models.TextChoices):
        DOCX = "docx", "Word (.docx)"
        DOC = "doc", "Word 97-2003 (.doc)"
        JSON = "json", "JSON из МИС"

    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Загружен"
        PROCESSING = "processing", "В обработке"
        PROCESSED = "processed", "Обработан"
        FAILED = "failed", "Ошибка"
        SUPERSEDED = "superseded", "Заменён новой версией"
        ANNULLED = "annulled", "Аннулирован"

    class Identity(models.TextChoices):
        PENDING = "pending", "Пациент ещё не определён"
        MATCHED = "matched", "Пациент определён по номеру карты"
        MANUAL = "manual", "Пациент выбран при загрузке"
        UNMATCHED = "unmatched", "Пациент не определён (обезличенная карточка)"
        CONFIRMED = "confirmed", "Пациент подтверждён координатором"

    # Пусто, пока протокол из пачки не прочитан и пациент не определён.
    patient_id = models.UUIDField(db_index=True, null=True, blank=True)
    identity = models.CharField(max_length=12, choices=Identity.choices, default=Identity.MANUAL)
    card_number = models.CharField(max_length=64, blank=True, help_text="Номер амбулаторной карты из протокола или имени файла")
    batch = models.ForeignKey(UploadBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="documents")
    external_id = models.CharField(max_length=128, blank=True, db_index=True, help_text="ID протокола в МИС 1С")
    version = models.PositiveIntegerField(default=1)
    file = models.FileField(upload_to="protocols/%Y/%m/", blank=True)
    original_filename = models.CharField(max_length=255, blank=True)
    file_format = models.CharField(max_length=8, choices=Format.choices)
    checksum = models.CharField(max_length=64, db_index=True)
    raw_text = models.TextField(blank=True)
    study_type = models.CharField(max_length=128, blank=True)
    study_date = models.DateField(null=True, blank=True)
    performed_by = models.CharField(max_length=255, blank=True)
    location_code = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UPLOADED)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["external_id", "version"],
                condition=~models.Q(external_id=""),
                name="uniq_protocol_version",
            )
        ]

    def __str__(self) -> str:
        return f"{self.study_type or self.original_filename} ({self.get_status_display()})"

    @staticmethod
    def compute_checksum(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @property
    def latest_result(self) -> "ExtractionResult | None":
        return self.results.order_by("-created_at").first()


class ProcessingJob(BaseModel):
    """Запуск обработки (асинхронно через Celery). Хранит, каким движком и какой
    версией промпта получен результат — для воспроизводимости и аудита."""

    class Status(models.TextChoices):
        QUEUED = "queued", "В очереди"
        RUNNING = "running", "Выполняется"
        DONE = "done", "Готово"
        FAILED = "failed", "Ошибка"

    document = models.ForeignKey(StudyDocument, on_delete=models.CASCADE, related_name="jobs")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    celery_task_id = models.CharField(max_length=64, blank=True)
    engine = models.CharField(max_length=64, blank=True, help_text="rules / llm:<provider> / hybrid")
    prompt_version = models.CharField(max_length=32, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    # Очередь (services/jobs.py): сколько раз задачу брали в работу и кто взял — чтобы зависшую вернуть в очередь.
    attempts = models.PositiveSmallIntegerField(default=0)
    worker = models.CharField(max_length=64, blank=True, help_text="thread:<pid> / celery:<hostname>")
    # Сколько заняли шаги, мс: read, extraction, markup, analysis, save, total.
    timings = models.JSONField(default=dict, blank=True)
    # Ответила ли модель на этом разборе: ok | error | off. error — можно «Повторить с Qwen».
    llm_status = models.CharField(max_length=8, blank=True)


class ExtractionResult(BaseModel):
    """Структурированный JSON от AI-агента — вход модуля маршрутизации."""

    document = models.ForeignKey(StudyDocument, on_delete=models.CASCADE, related_name="results")
    job = models.OneToOneField(ProcessingJob, on_delete=models.SET_NULL, null=True, related_name="result")
    payload = models.JSONField(help_text="ExtractionPayload (см. services/schemas.py)")
    conclusion = models.TextField(blank=True)
    summary_for_patient = models.TextField(blank=True, help_text="Нейтральная выжимка без диагноза")
    engine = models.CharField(max_length=64)
    dictionary_version = models.CharField(max_length=64, blank=True)
    annotation_summary = models.JSONField(default=dict, blank=True,
                                          help_text="Итог разметки важности и отчёт контроля достоверности ИИ")
    analysis = models.JSONField(default=dict, blank=True,
                                help_text="Маркеры, перекрытия и триггеры с позициями и уверенностью (services/markers.py)")
    advice_meta = models.JSONField(default=dict, blank=True,
                                   help_text="Статус советов ИИ-агента по маршрутизации: режим, модель, ошибка")


class Finding(BaseModel):
    """Отдельная находка с доказательством (цитатой) — основа объяснимости."""

    result = models.ForeignKey(ExtractionResult, on_delete=models.CASCADE, related_name="findings")
    code = models.SlugField(max_length=64, db_index=True)
    label = models.CharField(max_length=255)
    evidence_quote = models.TextField()
    span_start = models.IntegerField(null=True, blank=True)
    span_end = models.IntegerField(null=True, blank=True)
    negated = models.BooleanField(default=False, help_text="Упомянуто с отрицанием — не триггер")
    uncertain = models.BooleanField(default=False, help_text="«?», «нельзя исключить», «по типу»")
    attributes = models.JSONField(default=dict, blank=True)
    confidence = models.FloatField(default=1.0)
    severity = models.CharField(max_length=16, default="routine")
    rule_id = models.CharField(max_length=128, blank=True,
                               help_text="Правило, нашедшее находку: «dictionary:gallstones@v1#2», «scale:birads»")
    source = models.CharField(max_length=8, default="rules", blank=True,
                              help_text="Кто нашёл: rules — словарь, llm — только ИИ-агент, both — словарь и ИИ")

    class Meta:
        ordering = ["span_start"]


class RoutingAdvice(BaseModel):
    """Совет ИИ-агента по маршрутизации. Только рекомендация, требует проверки координатором:
    маршрут по нему не меняется, оценки координатора хранятся в модуле координатора (AdviceReview)."""

    class Engine(models.TextChoices):
        QWEN = "qwen", "Qwen через LangChain"
        DEMO = "demo", "Демо-режим без модели"

    document = models.ForeignKey(StudyDocument, on_delete=models.CASCADE, related_name="routing_advice")
    result = models.ForeignKey(ExtractionResult, on_delete=models.CASCADE, related_name="routing_advice")
    seq = models.PositiveSmallIntegerField(default=1)
    text = models.TextField(help_text="Совет: что сделать")
    rationale = models.TextField(help_text="Обоснование: почему")
    evidence_quote = models.TextField(blank=True, help_text="Дословная цитата протокола, на которую опирается совет")
    confidence = models.FloatField(default=0.5, help_text="Уверенность модели (0–1), как её вернула модель")
    target_route_code = models.CharField(max_length=64, blank=True)
    target_route_title = models.CharField(max_length=255, blank=True)
    target_specialty_code = models.CharField(max_length=64, blank=True)
    target_executor = models.CharField(max_length=255, blank=True, help_text="Кому адресован: врач, подразделение")
    engine = models.CharField(max_length=16, choices=Engine.choices)
    model_name = models.CharField(max_length=128, blank=True)
    prompt_version = models.CharField(max_length=64, blank=True)
    grounding = models.JSONField(default=dict, blank=True,
                                 help_text="Проверки совета: цитата есть в тексте, маршрут и специальность из справочника")
    matches_matrix = models.BooleanField(default=False, help_text="Совпадает с маршрутом, который построила матрица")

    class Meta:
        ordering = ["document", "seq"]


class AttentionRule(BaseModel):
    """Порог «на что обратить внимание» (настройка, редактируется в админке).

    Пример: полип желчного пузыря с size_mm >= 10 -> подсветка «Полип 12 мм (порог 10 мм)».
    Порог только объясняет, что превышено, и ничего не ранжирует: что важнее, решает врач.
    finding_code — код словаря находок, шкалы (birads_category) или признака лексикона (sign:mass).
    Значения порогов утверждает клинический эксперт; в демо-данных — примеры.
    """

    code = models.SlugField(max_length=64, unique=True)
    finding_code = models.CharField(max_length=64, db_index=True)
    conditions = models.JSONField(default=list, blank=True, help_text='[{"attr": "size_mm", "op": "gte", "value": 10}]')
    message = models.CharField(max_length=255, help_text="Пояснение врачу; {value} — значение атрибута")
    source = models.CharField(max_length=255, blank=True, help_text="Основание (клинические рекомендации, приказ)")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["finding_code", "code"]

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class ProtocolSegment(models.Model):
    """Фрагмент протокола с подсветками (без уровня важности). Сумма фрагментов = весь текст протокола
    (инвариант verify_coverage), поэтому исходник всегда восстанавливается полностью."""

    result = models.ForeignKey(ExtractionResult, on_delete=models.CASCADE, related_name="segments")
    seq = models.PositiveIntegerField(help_text="Исходный порядок в протоколе")
    section = models.CharField(max_length=16)
    organ = models.CharField(max_length=128, blank=True)
    text = models.TextField()
    span_start = models.PositiveIntegerField()
    span_end = models.PositiveIntegerField()
    kind = models.CharField(max_length=24)
    emergency = models.BooleanField(default=False, help_text="Экстренная находка: флаг безопасности, не уровень")
    finding_codes = models.JSONField(default=list, blank=True)
    signs = models.JSONField(default=list, blank=True)
    negated_codes = models.JSONField(default=list, blank=True)
    attributes = models.JSONField(default=dict, blank=True)
    highlights = models.JSONField(default=list, blank=True, help_text='Причины подсветки: [{"type": "size", "text": "..."}]')
    uncertain = models.BooleanField(default=False)
    linked_to = models.PositiveIntegerField(null=True, blank=True, help_text="seq пункта заключения")
    link_reason = models.CharField(max_length=255, blank=True)
    not_in_conclusion = models.BooleanField(default=False)
    sources = models.JSONField(default=list, blank=True, help_text="rules / llm")
    llm_labels = models.JSONField(default=list, blank=True,
                                  help_text='Что во фрагменте нашёл ИИ-агент: [{"quote", "codes", "kind", "highlight"}]')

    class Meta:
        ordering = ["result", "seq"]
        constraints = [models.UniqueConstraint(fields=["result", "seq"], name="uniq_segment_seq")]


class FocusProfile(BaseModel):
    """Личные приоритеты врача: какие находки, признаки и причины подсветки выносить «В фокус» и в каком порядке.
    Система ничего не ранжирует сама — порядок здесь задаёт только врач."""

    owner = models.CharField(max_length=150, unique=True, help_text="Логин врача / координатора")
    items = models.JSONField(default=list, blank=True, help_text='Ключи по порядку: "gallstones", "sign:cyst", "hl:uncertain"')

    def __str__(self) -> str:
        return f"Приоритеты {self.owner}"


class SegmentPin(BaseModel):
    """Фрагмент, закреплённый врачом в конкретном протоколе (решение по случаю)."""

    document = models.ForeignKey(StudyDocument, on_delete=models.CASCADE, related_name="pins")
    seq = models.PositiveIntegerField()
    owner = models.CharField(max_length=150)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["document", "seq", "owner"], name="uniq_segment_pin")]


class QualityRun(BaseModel):
    """Проверка качества аналитики: прогон пачки протоколов «всухую» (до settings.QUALITY_MAX_PROTOCOLS).

    Пациенты, маршруты и уведомления не создаются, файлы удаляются после прогона. В отчёте — коды находок
    и правил, короткие цитаты-доказательства, подсветки, вывод проверки рекомендаций и ошибки по каждому файлу.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "В очереди"
        RUNNING = "running", "Идёт прогон"
        DONE = "done", "Готово"
        FAILED = "failed", "Прогон прерван"

    title = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    created_by = models.CharField(max_length=150, blank=True)
    files_total = models.PositiveIntegerField(default=0)
    processed = models.PositiveIntegerField(default=0)
    rejected = models.JSONField(default=list, blank=True, help_text='[{"file": "...", "error": "..."}] — не вошли в прогон')
    labels = models.JSONField(default=dict, blank=True, help_text='{"файл": ["код правила", ...]} — ожидаемые триггеры')
    rows = models.JSONField(default=list, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Проверка качества {self.created_at:%d.%m %H:%M}: {self.processed} из {self.files_total}"
