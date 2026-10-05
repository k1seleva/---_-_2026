"""
Проверка ИИ-агента (Qwen) на синтетическом протоколе: запускается на компьютере, где развёрнута модель.
Ничего не сохраняет в базу, только читает словарь, пороги и справочник маршрутов.

    python manage.py check_llm                    # синтетический протокол
    python manage.py check_llm --text протокол.txt  # свой текст (UTF-8); выводится только на этот экран

Шаги те же, что при загрузке протокола:
1) модель отвечает; 2) находки заключения: словарь и модель; 3) разметка фрагментов моделью;
4) маркеры и триггеры по источникам (словарь, оба, только ИИ); 5) советы по маршрутизации (ответ модели целиком).
"""
import time
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.processing.services.ai_agent import build_chat_model, get_finding_extractor, model_label
from apps.processing.services.annotation import get_annotator
from apps.processing.services.dictionary import load_dictionary
from apps.processing.services.pipeline import build_analysis

SYNTHETIC = """УЗИ органов брюшной полости (синтетический пример для проверки ИИ-агента)
ЖЕЛЧНЫЙ ПУЗЫРЬ: размеры 70*35 мм, стенка 2 мм. Конкременты множественные размером до 14 мм.
ПЕЧЕНЬ: контуры ровные, структура однородная. В воротах печени визуализируется лимфатический узел 12 мм.
ПОДЖЕЛУДОЧНАЯ ЖЕЛЕЗА: эхогенность диффузно повышена, структура неоднородная.
ПУПОЧНОЕ КОЛЬЦО: расширено до 14 мм, при натуживании через него выходит предбрюшинная клетчатка.
ЗАКЛЮЧЕНИЕ: УЗ-признаки холецистолитиаза. Диффузные изменения поджелудочной железы.
Рекомендовано: консультация хирурга."""


class Command(BaseCommand):
    help = "Проверить подключение ИИ-агента (Qwen): разметка, находки, триггеры и советы на синтетическом протоколе"

    def add_arguments(self, parser):
        parser.add_argument("--text", help="файл с текстом протокола (UTF-8); по умолчанию синтетический пример")
        parser.add_argument("--skip-advice", action="store_true", help="не запрашивать советы по маршрутизации")

    def handle(self, text=None, skip_advice=False, **opts):
        protocol = Path(text).read_text(encoding="utf-8") if text else SYNTHETIC
        cfg = settings.AI_AGENT
        label = model_label()
        self.stdout.write(f"Модель: {label or 'не подключена'}; адрес: {cfg['BASE_URL'] or 'по умолчанию провайдера'}; "
                          f"окно контекста: {cfg.get('NUM_CTX')}; тайм-аут: {cfg['TIMEOUT_SEC']} с")
        if not label:
            raise CommandError("LLM_PROVIDER не задан. Заполните .env по образцу .env.example (раздел «Qwen»).")
        failures = 0

        # 1. Модель отвечает
        started = time.monotonic()
        try:
            answer = build_chat_model().invoke("Ответь одним словом по-русски: готов")
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"1. Модель не ответила: {exc}. Проверьте, что Ollama запущена и модель скачана "
                               f"(ollama list), и адрес LLM_BASE_URL.") from exc
        self.ok(f"1. Модель отвечает за {self.sec(started)}: «{str(answer.content).strip()[:60]}»")

        # 2. Находки заключения: словарь + модель
        dictionary = load_dictionary()
        payload = get_finding_extractor().extract(protocol, dictionary=dictionary)
        llm = payload.engines.get("llm", {})
        by_source = Counter(f.source for f in payload.findings)
        if llm.get("status") == "ok":
            self.ok(f"2. Находки заключения за {llm.get('ms', 0) / 1000:.1f} с: модель предложила {llm.get('proposed', 0)}, "
                    f"после проверки цитат {llm.get('found', 0)}. По источникам: {self.sources(by_source)}")
        else:
            failures += 1
            self.fail(f"2. Находки заключения только по словарю, модель: {llm.get('error', 'не вызывалась')}")

        # 3. Разметка фрагментов моделью
        annotation = get_annotator(dictionary).annotate(protocol, study_type=payload.study_type)
        markup = annotation.llm or {}
        if markup.get("status") == "ok":
            with_labels = sum(1 for a in annotation.segments if a.llm_labels)
            self.ok(f"3. Разметка фрагментов за {markup.get('ms', 0) / 1000:.1f} с: меток {markup.get('labels', 0)}, "
                    f"принято {markup.get('accepted', 0)}, отброшено {markup.get('rejected', 0)}; "
                    f"фрагментов с метками ИИ: {with_labels}")
        else:
            failures += 1
            self.fail(f"3. Разметка фрагментов только по словарю, модель: {markup.get('error', 'не вызывалась')}")

        # 4. Маркеры и триггеры по источникам
        analysis = build_analysis(protocol, [a.as_dict() for a in annotation.segments],
                                  [f.model_dump() for f in payload.findings], dictionary, study_type=payload.study_type)
        stats = analysis.get("stats", {})
        self.ok(f"4. Маркеров {stats.get('markers_counted', 0)} ({self.sources(stats.get('markers_by_source', {}))}), "
                f"триггеров {stats.get('triggers', 0)} ({self.sources(stats.get('triggers_by_source', {}))})")
        for m in analysis.get("markers", []):
            if m["source"] == "llm" and not m.get("subsumed_by"):
                self.stdout.write(f"   только ИИ: {m['title']}: «{m['text'][:80]}»")

        # 5. Советы по маршрутизации
        if not skip_advice:
            failures += self.advice(protocol, payload, analysis)
        if failures:
            raise CommandError(f"Шагов с ошибкой: {failures}. Протоколы при этом разбираются по словарю, "
                               "но разметки ИИ и ответа Qwen не будет.")
        self.ok("Готово: словарь и модель работают вместе. Загрузите протоколы заново, чтобы разметили оба.")

    def advice(self, protocol: str, payload, analysis: dict) -> int:
        from apps.doctors.facade import DoctorsFacade
        from apps.processing.services.routing_advice import (AdviceContext, QwenRoutingAdvisor, configured_engine,
                                                             ground_advice)
        from apps.routing.facade import RoutingFacade

        if configured_engine() != "qwen":
            self.fail(f"5. Советы по маршрутизации: режим {configured_engine()} (ROUTING_ADVISOR_MODE), модель не спрашиваем")
            return 1
        triggers = analysis.get("triggers", [])
        context = AdviceContext(
            text=protocol, conclusion=payload.conclusion, study_type=payload.study_type, triggers=triggers,
            recommendations=[r.model_dump() for r in payload.recommendations], catalog=RoutingFacade.route_catalog(),
            specialties=DoctorsFacade.specialty_titles(),
            matrix_routes={(t.get("target") or {}).get("route_code", "") for t in triggers if t["type"] == "route"})
        advisor = None
        try:
            advisor = QwenRoutingAdvisor()
            drafts = advisor.advise(context)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"5. Советы по маршрутизации: модель не ответила по схеме: {exc}")
            if advisor is not None and advisor.last_raw:
                self.stdout.write("   Ответ модели целиком:\n" + advisor.last_raw[:2000])
            return 1
        accepted, rejected = ground_advice(drafts, context)
        self.ok(f"5. Советы {advisor.model_name} за {(advisor.last_ms or 0) / 1000:.1f} с: предложено {len(drafts)}, "
                f"после проверки цитат {len(accepted)}, отброшено {len(rejected)}")
        for draft, _ in accepted:
            self.stdout.write(f"   • {draft.text} (уверенность {round(draft.confidence * 100)}%)")
        self.stdout.write("   Ответ модели целиком:\n" + advisor.last_raw[:2000])
        return 0

    @staticmethod
    def sources(counts: dict) -> str:
        titles = {"rules": "словарь", "both": "словарь и ИИ", "llm": "только ИИ"}
        return ", ".join(f"{titles.get(k, k)} {v}" for k, v in counts.items() if v) or "нет"

    @staticmethod
    def sec(started: float) -> str:
        return f"{time.monotonic() - started:.1f} с"

    def ok(self, message: str) -> None:
        self.stdout.write(self.style.SUCCESS(message))

    def fail(self, message: str) -> None:
        self.stdout.write(self.style.ERROR(message))
