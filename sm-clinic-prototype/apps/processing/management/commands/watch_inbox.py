"""Папка-наблюдатель без Celery: `manage.py watch_inbox` (один проход) или `--loop` (каждые N секунд)."""
import time

from django.core.management.base import BaseCommand

from apps.processing.services.inbox import InboxWatcher


class Command(BaseCommand):
    help = "Забрать новые протоколы из папки-наблюдателя (PROTOCOL_INBOX_DIR/<клиника>/)"

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Работать постоянно")
        parser.add_argument("--interval", type=int, default=30, help="Пауза между проходами, сек")
        parser.add_argument("--dir", default=None, help="Другая папка вместо PROTOCOL_INBOX_DIR")

    def handle(self, *args, **opts):
        watcher = InboxWatcher(root=opts["dir"])
        while True:
            for report in watcher.scan():
                b = report.batch
                self.stdout.write(f"{b.location_code}: принято {b.accepted}, повторов {b.duplicates}, ошибок {len(b.rejected)}")
            if not opts["loop"]:
                break
            time.sleep(opts["interval"])
