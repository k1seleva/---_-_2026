"""
Прогон папки протоколов через весь конвейер без записи в БД — для разметки и метрик:
извлечение находок, матрица маршрутизации, подсветка значимого, проверка рекомендаций.

    python manage.py evaluate_protocols ./data/protocols --out report.csv
    python manage.py evaluate_protocols ./data/protocols --labels labels.csv   # точность/полнота по разметке
    python manage.py evaluate_protocols ./data/protocols --with-text           # добавить в отчёт начало заключения

    python manage.py evaluate_protocols ./data/protocols --summary-json summary.json  # только счётчики, без текстов

labels.csv: file,expected_rules  (правила через «;», пусто = норма)
summary.json: сводка прогона без имён файлов и цитат — её можно хранить и показывать вместе с прототипом
(manage.py import_run_summary), когда сами протоколы публиковать нельзя.

Каждый протокол проверяется на инварианты разметки: текст покрыт фрагментами полностью и без
наложений, экран врача выводит каждый фрагмент ровно один раз. Нарушение — строка с ошибкой в отчёте.
Тот же прогон без командной строки: «Аналитика → Качество разбора» (до 100 протоколов за раз).
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.processing.services.annotation import HIGHLIGHT_TYPES, SIGNIFICANT_TYPES
from apps.processing.services.quality import ProtocolEvaluator, parse_labels, rows_to_csv, summarize


class Command(BaseCommand):
    help = "Оценка распознавания триггеров, подсветки значимого и проверки рекомендаций на папке протоколов"

    def add_arguments(self, parser):
        parser.add_argument("folder")
        parser.add_argument("--out", default="evaluation.csv")
        parser.add_argument("--labels", default=None)
        parser.add_argument("--with-text", action="store_true", help="добавить в отчёт начало заключения")
        parser.add_argument("--summary-json", default=None, help="сохранить сводку (только счётчики) в JSON")
        parser.add_argument("--title", default="", help="название прогона для сводки")

    def handle(self, folder, out, labels, with_text, summary_json=None, title="", **opts):
        evaluator = ProtocolEvaluator()
        expected = parse_labels(Path(labels).read_bytes()) if labels else {}
        rows = [evaluator.evaluate(path.name, path.read_bytes(), with_text=with_text)
                for path in sorted(Path(folder).rglob("*")) if path.suffix.lower() in (".doc", ".docx", ".json")]
        Path(out).write_text(rows_to_csv(rows, expected), encoding="utf-8")
        summary = summarize(rows, expected)
        self._report(summary, out)
        if summary_json:
            Path(summary_json).write_text(json.dumps(public_summary(summary, title), ensure_ascii=False, indent=1),
                                          encoding="utf-8")
            self.stdout.write(f"Сводка без текстов: {summary_json}")

    def _report(self, s: dict, out: str) -> None:
        self.stdout.write(f"Протоколов: {s['protocols']}, не прочитано: {s['read_errors']}, "
                          f"разметка без потерь: {s['markup_ok']}, со сработавшим триггером: {s['with_trigger']} "
                          f"({s['trigger_rate']}%). Отчёт: {out}")
        self.stdout.write(f"Без найденного заключения: {s['no_conclusion']}; с изменением, не вынесенным в заключение: "
                          f"{s['not_in_conclusion']}; с экстренной находкой: {s['emergency']}")
        # Подсветки без ранжирования: сколько раз встретилась каждая причина, в порядке справочника.
        counts = dict(s["highlights"])
        self.stdout.write("Подсветки: " + ", ".join(
            f"{HIGHLIGHT_TYPES[t]} ({'значимая' if t in SIGNIFICANT_TYPES else 'деталь'})={counts[t]}"
            for t in HIGHLIGHT_TYPES if counts.get(t)))
        self.stdout.write("Проверка рекомендаций: " + ", ".join(f"{k}={v}" for k, v in s["verdicts"]))
        llm = s.get("llm") or {}
        if any(llm.get(step, {}).get(status) for step in ("extraction", "markup") for status in ("ok", "error")):
            src, msrc = s["triggers_by_source"], s["markers_by_source"]
            self.stdout.write(f"ИИ-агент ответил: находки {llm['extraction'].get('ok', 0)}, разметка {llm['markup'].get('ok', 0)} "
                              f"из {s['markup_ok']}; триггеры: оба {src.get('both', 0)}, только словарь {src.get('rules', 0)}, "
                              f"только ИИ {src.get('llm', 0)}; маркеры: оба {msrc.get('both', 0)}, только словарь "
                              f"{msrc.get('rules', 0)}, только ИИ {msrc.get('llm', 0)}")
        for issue_type, n in s["issues"]:
            self.stdout.write(f"  {issue_type}: {n}")
        if labelled := s.get("labelled"):
            self.stdout.write(f"Точность: {labelled['precision']}%, полнота: {labelled['recall']}%, F1: {labelled['f1']}% "
                              f"(размечено файлов: {labelled['files']}, расхождений: {len(labelled['mismatches'])})")


def public_summary(summary: dict, title: str = "") -> dict:
    """Сводка, которую можно показывать без протоколов: только счётчики и коды.
    Сравнение с разметкой эксперта не входит: в нём имена файлов."""
    from common import clock

    return {"title": title or "Прогон протоколов", "date": clock.now().date().isoformat(),
            "summary": {k: v for k, v in summary.items() if k != "labelled"}}
