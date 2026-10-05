from django.apps import AppConfig


class ProcessingConfig(AppConfig):
    name = "apps.processing"
    label = "processing"
    verbose_name = "Модуль обработки результатов"

    def ready(self) -> None:
        from common import labels

        from . import handlers
        from .facade import ProcessingFacade
        from .models import FindingDefinition, StudyDocument, UploadBatch
        from .models import RoutingAdvice
        from .services.annotation import HIGHLIGHT_TYPES, SIGN_TITLES
        from .services.markers import MARKER_TYPES, TRIGGER_TYPES
        from .services.scoring import LEVEL_TITLES

        handlers.register()
        _connect_queue_signals()
        labels.register_choices("document_status", StudyDocument.Status)
        labels.register_choices("identity", StudyDocument.Identity)
        labels.register_choices("batch_source", UploadBatch.Source)
        labels.register_choices("severity", FindingDefinition.Severity)
        labels.register("highlight", HIGHLIGHT_TYPES)
        labels.register("sign", SIGN_TITLES)
        labels.register("marker_type", MARKER_TYPES)
        labels.register("trigger_type", TRIGGER_TYPES)
        labels.register("confidence_level", LEVEL_TITLES)
        labels.register_choices("advice_engine", RoutingAdvice.Engine)
        # Названия находок (и шкал) вместо кодов во всех шаблонах.
        labels.register_resolver("finding", ProcessingFacade.finding_titles)


def _connect_queue_signals() -> None:
    """Фоновая очередь разбора стартует с первым запросом к сайту (режим thread); SQLite — в режиме WAL,
    чтобы чтение страниц не ждало записи разбора (и наоборот)."""
    from django.core.signals import request_started
    from django.db.backends.signals import connection_created

    request_started.connect(_start_worker, dispatch_uid="processing-queue-start")
    connection_created.connect(_sqlite_wal, dispatch_uid="processing-sqlite-wal")


def _start_worker(sender, **kwargs) -> None:
    from .services.jobs import LocalWorker

    LocalWorker.ensure_started()


def _sqlite_wal(sender, connection, **kwargs) -> None:
    if connection.vendor == "sqlite" and "memory" not in str(connection.settings_dict.get("NAME", "")):
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
