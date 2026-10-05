"""Гибридный разбор (словарь + ИИ-агент) и настоящий ответ модели в советах.

Модель здесь — имитация сервера Ollama (tests/fake_ollama.py): тот же HTTP-протокол, что у Ollama, поэтому
проверяется весь путь LangChain → ChatOllama → HTTP → структурированный ответ → проверка цитат. Ответы
имитации собраны правилами, это не Qwen. Только синтетические протоколы.
"""
import importlib.util
import io
import unittest

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.patients.models import Patient
from apps.processing.facade import ProcessingFacade
from apps.processing.models import ExtractionResult, Finding, RoutingAdvice, StudyDocument
from common import clock

from .fake_ollama import FakeOllama
from .helpers import make_protocol_docx, seed, staff_client

HAS_OLLAMA = importlib.util.find_spec("langchain_ollama") is not None

PROTOCOL = [
    "УЗИ органов брюшной полости",
    "ЖЕЛЧНЫЙ ПУЗЫРЬ: размеры 70*35 мм, стенка 2 мм. Конкременты множественные размером до 14 мм.",
    "ПЕЧЕНЬ: контуры ровные, структура однородная. В воротах печени визуализируется лимфатический узел 12 мм.",
    "ПОДЖЕЛУДОЧНАЯ ЖЕЛЕЗА: эхогенность диффузно повышена, структура неоднородная.",
    "ПУПОЧНОЕ КОЛЬЦО: расширено до 14 мм, при натуживании через него выходит предбрюшинная клетчатка.",
    "ЗАКЛЮЧЕНИЕ: УЗ-признаки холецистолитиаза. Диффузные изменения поджелудочной железы.",
    "Рекомендовано: консультация хирурга.",
]


def llm_settings(url: str, model: str = "qwen-fake") -> dict:
    """Один Qwen на всё: разметка, находки и советы (как в .env.example)."""
    return {
        "AI_AGENT": {**settings.AI_AGENT, "PROVIDER": "ollama", "MODEL": model, "BASE_URL": url, "TIMEOUT_SEC": 20},
        "ROUTING_ADVISOR": {**settings.ROUTING_ADVISOR, "MODE": "auto", "PROVIDER": "ollama", "MODEL": model,
                            "BASE_URL": url, "TIMEOUT_SEC": 20},
    }


class HybridTestCase(TransactionTestCase):
    def setUp(self):
        seed()
        clock.reset()
        self.patient = Patient.objects.get(external_mis_id="AK-0001")

    def upload(self, lines=PROTOCOL) -> str:
        r = APIClient().post("/api/v1/processing/documents/", {
            "patient_id": self.patient.id, "file": SimpleUploadedFile("p.docx", make_protocol_docx(lines))},
            format="multipart")
        self.assertIn(r.status_code, (201, 202), r.content[:300])
        return r.json()["id"]


@unittest.skipUnless(HAS_OLLAMA, "нужен пакет langchain-ollama")
class FakeOllamaIntegrationTests(HybridTestCase):
    def test_dictionary_and_model_mark_up_together_and_qwen_answers(self):
        with FakeOllama() as fake, override_settings(**llm_settings(fake.url)):
            doc_id = self.upload()
            page = staff_client().get(f"/coordinator/cases/{doc_id}/").content.decode()
        result = StudyDocument.objects.get(pk=doc_id).latest_result

        # Модель вызвана по HTTP с окном контекста и схемой ответа для каждого шага.
        titles = [(r.get("format") or {}).get("title") for r in fake.requests]
        self.assertEqual(sorted(titles), ["AdviceList", "ExtractionPayload", "SegmentLabeling"])
        self.assertTrue(all(r["options"]["num_ctx"] == 8192 for r in fake.requests))

        # Оба движка отработали, у каждой находки известен источник.
        self.assertEqual(result.payload["engines"]["llm"]["status"], "ok")
        self.assertEqual(result.annotation_summary["llm"]["status"], "ok")
        sources = dict(Finding.objects.filter(result=result).values_list("code", "source"))
        self.assertEqual(sources["gallstones"], "both")      # словарь и модель согласны
        self.assertEqual(sources["hernia"], "llm")           # нашла только модель, по дословной цитате

        analysis = ProcessingFacade.get_analysis(doc_id)
        markers = {m["text"]: m for m in analysis["markers"]}
        self.assertEqual(markers["лимфатический узел 12 мм"]["source"], "llm")    # признак только от ИИ
        self.assertEqual(markers["Конкременты"]["source"], "both")                # признак словаря подтверждён
        self.assertEqual(markers["расширено"]["source"], "rules")                 # ИИ промолчал — остаётся словарь
        by_code = {(t["type"], t["code"]): t["source"] for t in analysis["triggers"]}
        self.assertEqual(by_code[("route", "gallstones")], "both")
        self.assertEqual(by_code[("route", "hernia")], "llm")
        self.assertEqual(by_code[("review", "hernia")], "llm")
        self.assertEqual(analysis["stats"]["ai_only"], {"markers": 2, "triggers": 2})

        # Найденное только ИИ — отдельным блоком; в общей таблице видно, кто нашёл.
        self.assertIn("Найдено только Qwen", page)
        self.assertIn("словарь этого не нашёл, проверьте", page)
        self.assertIn("словарь и Qwen", page)
        main, ai_only = page.split('id="ai-only"', 1)
        self.assertIn("лимфатический узел 12 мм", ai_only)
        self.assertNotIn("data-goto-m", main.split('id="triggers"', 1)[1])
        self.assertIn('value="ai"', page)                     # режим подсветки «Только найденное ИИ»

        # Внизу страницы — ответ модели, а не демо-заглушка.
        advice = RoutingAdvice.objects.filter(document_id=doc_id)
        self.assertTrue(advice.exists())
        self.assertEqual({a.engine for a in advice}, {"qwen"})
        self.assertEqual(result.__class__.objects.get(pk=result.pk).advice_meta["status"], "ready")
        self.assertIn("Ответ Qwen", page)
        self.assertIn("Ответ модели целиком", page)
        self.assertNotIn("Демо-режим", page)
        self.assertIn("[только ИИ]", fake.requests[-1]["messages"][-1]["content"])  # модель знает источник триггера

    def test_check_llm_and_regenerate_advice_commands(self):
        doc_id = self.upload()                                   # без модели: советы демо-режима
        self.assertEqual(RoutingAdvice.objects.filter(document_id=doc_id).first().engine, "demo")
        with FakeOllama() as fake, override_settings(**llm_settings(fake.url)):
            page = staff_client().get(f"/coordinator/cases/{doc_id}/").content.decode()
            self.assertIn("Получить ответ Qwen", page)          # Qwen подключён после разбора
            self.assertIn("Протокол разобран до подключения Qwen", page)
            out = io.StringIO()
            call_command("check_llm", stdout=out)
            self.assertIn("Готово: словарь и модель работают вместе", out.getvalue())
            self.assertIn("только ИИ: Грыжа", out.getvalue())
            out = io.StringIO()
            call_command("regenerate_advice", stdout=out)
            self.assertIn("сформировано 1, с ошибкой 0", out.getvalue())
        self.assertEqual({a.engine for a in RoutingAdvice.objects.filter(document_id=doc_id)}, {"qwen"})


class QwenAdviceErrorTests(HybridTestCase):
    def test_unreachable_model_shows_error_without_demo_substitution(self):
        doc_id = self.upload(["УЗИ органов малого таза", "ЗАКЛЮЧЕНИЕ: Эхографические признаки полипа эндометрия."])
        broken = {**settings.ROUTING_ADVISOR, "MODE": "auto", "PROVIDER": "ollama", "MODEL": "qwen2.5:14b",
                  "BASE_URL": "http://127.0.0.1:9", "TIMEOUT_SEC": 3}
        with override_settings(ROUTING_ADVISOR=broken):
            response = staff_client().post(f"/coordinator/cases/{doc_id}/advice/regenerate/", follow=True)
            page = response.content.decode()
        meta = ExtractionResult.objects.get(document_id=doc_id).advice_meta
        self.assertEqual((meta["status"], meta["engine"]), ("error", "qwen"))
        self.assertFalse(RoutingAdvice.objects.filter(document_id=doc_id).exists())   # демо не подставлено
        self.assertIn("Qwen не ответил", page)
        self.assertIn("Спросить Qwen ещё раз", page)

    def test_answer_outside_schema_is_kept_for_reading(self):
        from langchain_core.messages import AIMessage
        from langchain_core.runnables import RunnableLambda

        from apps.processing.services.routing_advice import QwenRoutingAdvisor, RoutingAdviceService

        class ChattyQwen:
            def with_structured_output(self, schema, include_raw=False):
                raw = AIMessage(content="Рекомендую направить пациентку к гинекологу.")
                return RunnableLambda(lambda _p: {"raw": raw, "parsed": None, "parsing_error": ValueError("не JSON")})

        doc_id = self.upload(["УЗИ органов малого таза", "ЗАКЛЮЧЕНИЕ: Эхографические признаки полипа эндометрия."])
        result = StudyDocument.objects.get(pk=doc_id).latest_result
        saved = RoutingAdviceService(QwenRoutingAdvisor(llm=ChattyQwen(), config={"MODEL": "qwen3:8b"})).generate(result.id)
        self.assertEqual(saved, 0)
        meta = ExtractionResult.objects.get(pk=result.pk).advice_meta
        self.assertEqual(meta["status"], "error")
        self.assertIn("не совпал со схемой", meta["error"])
        self.assertEqual(meta["raw"], "Рекомендую направить пациентку к гинекологу.")
        page = staff_client().get(f"/coordinator/cases/{doc_id}/").content.decode()
        self.assertIn("Рекомендую направить пациентку к гинекологу.", page)
        self.assertIn("не разобран по схеме", page)
