"""
Синтетический набор для проверки качества разбора: zip с протоколами и labels.csv с ожидаемыми правилами.

    python manage.py quality_sample --out ./quality_sample --count 100

Затем «Аналитика → Качество разбора»: загрузить protocols.zip и labels.csv. Разметка здесь задана
сценарием (какое правило должно сработать), это демонстрация механизма, а не оценка на реальных данных:
для оценки нужен набор клиники с разметкой врача-эксперта.
"""
import csv
import io
import zipfile
from pathlib import Path

from django.core.management.base import BaseCommand

from .demo_batch import ABDOMEN, EMERGENCY, SCENARIOS, protocol_docx

# Код сценария -> правила матрицы, которые должны сработать (пусто — норма). Из правил одной группы маршрута
# срабатывает одно, самое приоритетное: узел с TI-RADS 4 — правило по шкале, а не общее «узловое образование».
EXPECTED = {
    "gallstones": ["gallstones"], "gb_polyp": ["gallbladder_polyp"], "endo_polyp": ["endometrial_polyp"],
    "myoma": ["uterine_myoma"], "thyroid": ["thyroid_tirads_4_5"], "breast": ["birads_3_5"],
    "hernia": ["hernia"], "bph": ["bph"], "varicose": ["varicose_veins"], "norm": [], "no_recs": ["gallstones"],
    "no_conclusion": ["submucous_myoma"], "uncertain": ["endometrial_polyp"], "not_in_conclusion": ["gallstones"],
    "dvt": ["dvt"], "negated": [], "postop": [],
}
# Отрицание и состояние после операции: триггер срабатывать не должен.
EXTRA = [
    ("negated", ABDOMEN, ["ЖЕЛЧНЫЙ ПУЗЫРЬ: стенка 2 мм, конкрементов не выявлено.", "Полипов нет."],
     "Эхографических признаков патологии органов брюшной полости не выявлено.", None),
    ("postop", ABDOMEN, ["ЖЕЛЧНЫЙ ПУЗЫРЬ: удалён.", "Холедох 5 мм."], "Состояние после холецистэктомии.", None),
]


class Command(BaseCommand):
    help = "Синтетический набор протоколов с разметкой для страницы «Качество разбора»"

    def add_arguments(self, parser):
        parser.add_argument("--out", default="quality_sample")
        parser.add_argument("--count", type=int, default=100)

    def handle(self, out, count, **opts):
        scenarios = [*SCENARIOS, EMERGENCY, *EXTRA]
        folder = Path(out)
        folder.mkdir(parents=True, exist_ok=True)
        labels = io.StringIO()
        writer = csv.writer(labels)
        writer.writerow(["file", "expected_rules"])
        with zipfile.ZipFile(folder / "protocols.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for i in range(count):
                code, study, lines, conclusion, recs = scenarios[i % len(scenarios)]
                name = f"Q-{i + 1:03d}.docx"
                date = f"{1 + i % 28:02d}.09.2026"
                archive.writestr(name, protocol_docx(f"Q-{i + 1:03d}", date, study, lines, conclusion, recs))
                writer.writerow([name, "; ".join(EXPECTED[code])])
        (folder / "labels.csv").write_text(labels.getvalue(), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Готово: {folder / 'protocols.zip'} ({count} протоколов) и {folder / 'labels.csv'}"))
