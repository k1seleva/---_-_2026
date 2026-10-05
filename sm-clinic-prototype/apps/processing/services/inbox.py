"""
Папка-наблюдатель: протоколы, которые МИС или диагностический кабинет кладут в общую папку,
забираются автоматически, без ручной загрузки.

Структура: <PROTOCOL_INBOX_DIR>/<код клиники>/<файлы .docx/.json/.zip>.
Файл, который ещё дописывается (изменён меньше MIN_AGE_SEC назад), пропускается до следующего прохода.
Принятые файлы переносятся в processed/<дата>/, непринятые — в failed/<дата>/ рядом с текстом причины.
"""
import shutil
import time
from pathlib import Path

from django.conf import settings

from common import clock

from ..models import UploadBatch
from .pipeline import BatchIngestService, BatchReport, IncomingFile

SERVICE_DIRS = {"processed", "failed"}


class InboxWatcher:
    MIN_AGE_SEC = 5

    def __init__(self, root: Path | None = None, batch_service: BatchIngestService | None = None) -> None:
        self.root = Path(root or settings.PROTOCOL_INBOX_DIR)
        self.batch_service = batch_service or BatchIngestService()

    def scan(self) -> list[BatchReport]:
        if not self.root.exists():
            return []
        reports = []
        for clinic_dir in sorted(p for p in self.root.iterdir() if p.is_dir() and p.name not in SERVICE_DIRS):
            files = self._ready_files(clinic_dir)
            if not files:
                continue
            incoming = [IncomingFile(name=f.name, data=f.read_bytes()) for f in files]
            report = self.batch_service.ingest(incoming, location_code=clinic_dir.name,
                                               source=UploadBatch.Source.FOLDER, created_by="Папка-наблюдатель")
            failed_names = {r["file"].split(" › ")[0] for r in report.batch.rejected}
            for f in files:
                self._archive(f, "failed" if f.name in failed_names else "processed", clinic_dir.name)
            reports.append(report)
        return reports

    def _ready_files(self, folder: Path) -> list[Path]:
        now = time.time()
        return sorted(f for f in folder.iterdir()
                      if f.is_file() and not f.name.startswith((".", "~$")) and now - f.stat().st_mtime >= self.MIN_AGE_SEC)

    def _archive(self, file: Path, kind: str, clinic: str) -> None:
        target = self.root / kind / clock.now().strftime("%Y-%m-%d") / clinic
        target.mkdir(parents=True, exist_ok=True)
        destination = target / file.name
        if destination.exists():
            destination = target / f"{file.stem}-{int(time.time())}{file.suffix}"
        shutil.move(str(file), destination)
