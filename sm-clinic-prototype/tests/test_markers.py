"""Маркеры, перекрытия, триггеры, подсветка, советы агента и их оценка. Только синтетические протоколы."""
import html
import re
from pathlib import Path
import json
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import CommandError
from django.test import Client, SimpleTestCase, TransactionTestCase
from rest_framework.test import APIClient

from apps.coordinator.models import AdviceReview
from apps.patients.models import Patient
from apps.processing.facade import ProcessingFacade
from apps.processing.models import ExtractionResult, RoutingAdvice, StudyDocument
from apps.processing.services import scoring
from apps.processing.services.dictionary import Dictionary, FindingPattern
from apps.processing.services.markers import (ANALYSIS_VERSION, MARKER_TYPES, PALETTE, TEXT_COLOR, TRIGGER_PALETTE,
                                              Marker, TriggerDetector, analyze_protocol, resolve_overlaps)
from apps.processing.services.normalization import finditer, normalize_text
from apps.processing.services.presenter import layered_evidence_html
from apps.processing.services.quality import summarize
from common import clock
from gateway.management.commands.import_run_summary import import_summary

from .helpers import GALLSTONE_PROTOCOL, make_docx, make_protocol_docx, seed, staff_client


def _marker(mid, kind, code, start, end, text, confidence=0.9, rule="r"):
    return Marker(id=mid, type=kind, code=code, title=code, start=start, end=end, text=text[start:end],
                  confidence=confidence, factors=[], rule_id=rule)


def _luminance(color: str) -> float:
    r, g, b = (int(color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted([_luminance(a), _luminance(b)], reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _strip(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


# Мини-словарь для модульных тестов: находка «полип эндометрия» и экстренный тромбоз.
DICTIONARY = Dictionary(findings=(
    FindingPattern(code="endometrial_polyp", title="Полип эндометрия", version=2,
                   patterns=(re.compile(r"полип\w*\s+эндометри", re.IGNORECASE),)),
    FindingPattern(code="dvt", title="Тромбоз", severity="emergency", patterns=(re.compile(r"тромбоз\w*", re.IGNORECASE),)),
), specialties=())


class NormalizationTests(SimpleTestCase):
    def test_positions_point_to_original_text(self):
        original = "Узел щитовидной железы. Тi-rads 2. Гидросальпингс справа, обрaзование 5 мм."
        norm = normalize_text(original)
        self.assertNotIn(" ", norm.text)
        self.assertIn("ti-rads 2", norm.text)            # кириллическая «Т» исправлена
        self.assertIn("гидросальпинкс", norm.text)       # опечатка исправлена
        self.assertIn("образование", norm.text)          # латинская «a» исправлена
        hits = list(finditer(re.compile(r"гидросальпинкс"), norm))
        self.assertEqual(len(hits), 1)
        start, end, _m, corrected = hits[0]
        self.assertEqual(original[start:end], "Гидросальпингс")
        self.assertTrue(corrected)

    def test_clean_text_is_not_marked_corrected(self):
        norm = normalize_text("Полип эндометрия 6 мм")
        start, end, _m, corrected = next(finditer(re.compile(r"полип\w*"), norm))
        self.assertEqual((start, end, corrected), (0, 5, False))


class ScoringTests(SimpleTestCase):
    def test_factors_are_explained_and_clamped(self):
        value, details = scoring.score("dictionary", [scoring.IN_CONCLUSION, scoring.UNCERTAIN])
        self.assertEqual(value, 0.7)
        self.assertEqual([d["code"] for d in details], ["base", "conclusion", "uncertain"])
        self.assertTrue(all(d["reason"] for d in details))
        self.assertEqual(scoring.score("scale", [scoring.IN_CONCLUSION, scoring.IN_CONCLUSION])[0], 0.99)
        self.assertEqual(scoring.level(0.9), "high")
        self.assertEqual(scoring.level(0.6), "medium")
        self.assertEqual(scoring.level(0.3), "low")


class OverlapTests(SimpleTestCase):
    text = "Полип эндометрия 6 мм с кистой"

    def test_same_span_from_two_rules_is_merged(self):
        markers = [_marker("m1", "finding", "endometrial_polyp", 0, 16, self.text, rule="a#1"),
                   _marker("m2", "finding", "endometrial_polyp", 0, 16, self.text, rule="a#2")]
        merged, overlaps = resolve_overlaps(markers, self.text)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].rule_ids, ["a#1", "a#2"])
        self.assertEqual(overlaps, [])

    def test_nested_sign_is_layered_and_subsumed_not_lost(self):
        markers = [_marker("x", "finding", "endometrial_polyp", 0, 16, self.text),
                   _marker("y", "sign", "sign:polyp", 0, 5, self.text),
                   _marker("z", "size", "attr:size", 17, 21, self.text)]
        merged, overlaps = resolve_overlaps(markers, self.text)
        finding = next(m for m in merged if m.type == "finding")
        sign = next(m for m in merged if m.type == "sign")
        self.assertEqual(sign.parent_id, finding.id)
        self.assertEqual(sign.subsumed_by, finding.id)
        self.assertNotEqual(sign.layer, finding.layer)
        self.assertEqual(overlaps, [{"a": finding.id, "b": sign.id, "kind": "nested", "types": ["finding", "sign"]}])

    def test_partial_overlap_is_reported(self):
        markers = [_marker("a", "finding", "f", 0, 10, self.text), _marker("b", "size", "attr:size", 6, 20, self.text)]
        _merged, overlaps = resolve_overlaps(markers, self.text)
        self.assertEqual(overlaps[0]["kind"], "partial")


class AnalyzeProtocolTests(SimpleTestCase):
    text = ("Описание: в полости матки полип эндометрия 6 мм. Вены: тромбоз не выявлен.\n"
            "Заключение: Полип эндометрия. Тромбоз глубоких вен?")

    def segments(self):
        concl = self.text.index("Заключение")
        return [
            {"id": 1, "text": self.text[:concl].strip(), "start": 0, "end": len(self.text[:concl].strip()),
             "section": "description", "kind": "finding", "finding_codes": ["endometrial_polyp"], "signs": ["polyp"],
             "attributes": {"size_mm": 6}, "uncertain": False},
            {"id": 2, "text": self.text[concl:], "start": concl, "end": len(self.text), "section": "conclusion",
             "kind": "finding", "finding_codes": ["endometrial_polyp", "dvt"], "signs": [], "attributes": {}, "uncertain": True},
        ]

    def analyze(self):
        concl = self.text.index("Полип эндометрия.")
        findings = [{"code": "endometrial_polyp", "label": "Полип эндометрия", "negated": False,
                     "span_start": concl, "span_end": concl + 17, "evidence_quote": "Полип эндометрия.", "confidence": 0.95}]
        matches = [{"rule_code": "polyp_hysteroscopy", "rule_version": 3, "rule_title": "Полип → гистероскопия",
                    "template_code": "gyn_surgical", "template_title": "Гинекологический маршрут",
                    "specialty_code": "gyn_surgeon", "finding": findings[0]}]
        return analyze_protocol(self.text, self.segments(), findings, DICTIONARY, route_matches=matches)

    def test_markers_have_exact_spans_rule_and_confidence(self):
        analysis = self.analyze()
        for m in analysis["markers"]:
            self.assertEqual(self.text[m["start"]:m["end"]], m["text"])
            self.assertIn(m["type"], MARKER_TYPES)
            self.assertTrue(m["rule_id"])
            self.assertTrue(0 < m["confidence"] < 1)
        negated = [m for m in analysis["markers"] if m["type"] == "negation"]
        self.assertTrue(any(m["code"] == "dvt" for m in negated))        # «тромбоз не выявлен» — не триггер
        uncertain_dvt = next(m for m in analysis["markers"] if m["type"] == "emergency")
        self.assertIn("uncertain", [f["code"] for f in uncertain_dvt["factors"]])

    def test_triggers_have_all_fields_and_are_deduplicated(self):
        triggers = self.analyze()["triggers"]
        route = next(t for t in triggers if t["type"] == "route")
        for key in ("id", "type", "start", "end", "evidence", "confidence", "rule_id", "number"):
            self.assertIn(key, route)
        self.assertEqual(route["rule_id"], "matrix:polyp_hysteroscopy@v3")
        self.assertEqual(self.text[route["start"]:route["end"]], route["evidence"])
        self.assertEqual(route["section"], "conclusion")
        # Полип есть и в описании, и в заключении: уверенность чуть выше, причина записана.
        self.assertIn("elsewhere", [f["code"] for f in route["factors"]])
        emergency = [t for t in triggers if t["type"] == "emergency"]
        self.assertEqual(len(emergency), 1)
        self.assertEqual(emergency[0]["rule_id"], "dictionary:dvt@v1")
        self.assertEqual(len({t["id"] for t in triggers}), len(triggers))

    def test_duplicate_trigger_from_two_places_becomes_one_with_also(self):
        text = "Тромбоз. Тромбоз."
        markers = [_marker("m1", "emergency", "dvt", 0, 7, text, rule="dictionary:dvt@v1#1"),
                   _marker("m2", "emergency", "dvt", 9, 16, text, rule="dictionary:dvt@v1#1")]
        triggers = TriggerDetector().detect(text, markers, [], [], [])
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0].also, [{"start": 9, "end": 16, "text": "Тромбоз"}])

    def test_layered_html_keeps_text_and_every_marker(self):
        analysis = self.analyze()
        markup = layered_evidence_html(self.text, analysis)
        self.assertEqual(_strip(markup), self.text)                  # текст символ в символ
        shown = set(" ".join(re.findall(r'data-m="([^"]*)"', markup)).split())
        self.assertEqual(shown, {m["id"] for m in analysis["markers"]})  # ни один маркер не «съеден»
        self.assertIn('data-badge="Т', markup)
        self.assertNotIn("Т1", _strip(markup))                        # номер триггера не в тексте, а в CSS


class PaletteTests(SimpleTestCase):
    def test_wcag_aa_contrast(self):
        for kind, (bg, line) in PALETTE.items():
            with self.subTest(kind=kind):
                self.assertGreaterEqual(_contrast(TEXT_COLOR, bg), 4.5)   # текст на фоне маркера
                self.assertGreaterEqual(_contrast(line, bg), 3.0)         # линия подчёркивания как элемент интерфейса
        for kind, color in TRIGGER_PALETTE.items():
            with self.subTest(trigger=kind):
                self.assertGreaterEqual(_contrast("#ffffff", color), 4.5)  # номер триггера белым на цвете типа
        self.assertEqual(len({bg for bg, _line in PALETTE.values()}), len(PALETTE))  # у каждого типа свой цвет


class QualitySummaryTests(SimpleTestCase):
    def test_trigger_totals_and_share(self):
        rows = [
            {"file": "a", "rules": ["r1"], "findings": [], "highlight_types": {}, "audit_verdict": "sufficient",
             "audit_issues": [], "has_conclusion": True, "not_in_conclusion": 0, "emergency": 0, "error": "",
             "triggers": 2, "trigger_types": {"route": 1, "review": 1}, "trigger_confidence": [0.9, 0.7],
             "markers": 5, "marker_types": {"finding": 2}, "overlaps": 1, "corrected": 0},
            {"file": "b", "rules": [], "findings": [], "highlight_types": {}, "audit_verdict": "sufficient",
             "audit_issues": [], "has_conclusion": True, "not_in_conclusion": 0, "emergency": 0, "error": "",
             "triggers": 0, "trigger_types": {}, "trigger_confidence": [], "markers": 1, "marker_types": {}, "overlaps": 0,
             "corrected": 0},
        ]
        s = summarize(rows)
        self.assertEqual(s["triggers_total"], 2)
        self.assertEqual(dict(s["triggers_by_type"]), {"route": 1, "review": 1})
        self.assertEqual(s["any_trigger_rate"], 50.0)
        self.assertEqual(s["trigger_confidence_avg"], 0.8)

    def test_summary_import_refuses_rows_with_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text(json.dumps({"title": "x", "summary": {"protocols": 1, "labelled": {"mismatches": []}}}))
            with self.assertRaises(CommandError):
                import_summary(path)


class CaseAnalysisIntegrationTests(TransactionTestCase):
    """Загрузка синтетического протокола -> маркеры и триггеры в БД -> страница протокола и API."""

    def setUp(self):
        seed()
        clock.reset()
        self.api = APIClient()
        self.patient = Patient.objects.get(external_mis_id="AK-0001")
        r = self.api.post("/api/v1/processing/documents/", {
            "patient_id": self.patient.id, "location_code": "vdnh",
            "file": SimpleUploadedFile("g.docx", make_protocol_docx(GALLSTONE_PROTOCOL))}, format="multipart")
        self.doc_id = r.json()["id"]

    def test_analysis_is_stored_and_rendered(self):
        result = ExtractionResult.objects.get(document_id=self.doc_id)
        self.assertEqual(result.analysis["version"], ANALYSIS_VERSION)
        types = {t["type"] for t in result.analysis["triggers"]}
        self.assertEqual(types, {"route", "review"})       # ЖКБ -> маршрут; образование печени не в заключении
        page = staff_client().get(f"/coordinator/cases/{self.doc_id}/")
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()
        self.assertIn("Выявленные триггеры", body)
        self.assertIn('id="ev-data"', body)
        for t in result.analysis["triggers"]:
            self.assertIn(f'id="trig-{t["id"]}"', body)
            self.assertIn(f'data-goto="{t["id"]}"', body)
        pre = re.search(r'<pre class="protocol ev-text[^>]*>(.*?)</pre>', body, re.S).group(1)
        self.assertEqual(_strip(pre), StudyDocument.objects.get(pk=self.doc_id).raw_text)
        sorted_page = staff_client().get(f"/coordinator/cases/{self.doc_id}/?tsort=confidence").content.decode()
        self.assertIn("<b>по уверенности</b>", sorted_page)

    def test_api_is_backward_compatible_and_extended(self):
        data = self.api.get(f"/api/v1/processing/documents/{self.doc_id}/").json()
        result = data["result"]
        for key in ("id", "engine", "dictionary_version", "conclusion", "summary_for_patient", "payload", "findings"):
            self.assertIn(key, result)                      # прежние поля на месте
        finding = result["findings"][0]
        for key in ("code", "label", "evidence_quote", "negated", "confidence", "span_start", "span_end", "rule_id"):
            self.assertIn(key, finding)
        self.assertTrue(finding["rule_id"].startswith("dictionary:"))
        self.assertIn("parent_id", result["analysis"]["markers"][0])
        self.assertEqual(result["qwen_advice"]["label"], "рекомендация ИИ, требует проверки координатором")
        analysis = self.api.get(f"/api/v1/processing/documents/{self.doc_id}/analysis/").json()["analysis"]
        self.assertEqual(analysis["triggers"], result["analysis"]["triggers"])

    def test_old_document_gets_analysis_lazily(self):
        ExtractionResult.objects.filter(document_id=self.doc_id).update(analysis={})
        analysis = ProcessingFacade.get_analysis(self.doc_id)
        self.assertTrue(analysis["triggers"])
        self.assertTrue(ExtractionResult.objects.get(document_id=self.doc_id).analysis)

    def test_features_page_counts_triggers(self):
        stats = ProcessingFacade.trigger_stats()
        self.assertEqual(stats["documents"], 1)
        self.assertEqual(stats["with_triggers"], 1)
        self.assertEqual(stats["share"], 100.0)
        page = staff_client().get("/coordinator/analytics/features/").content.decode()
        self.assertIn("протоколов с триггером", page)


class AdviceReviewTests(TransactionTestCase):
    def setUp(self):
        seed()
        clock.reset()
        api = APIClient()
        patient = Patient.objects.get(external_mis_id="AK-0001")
        r = api.post("/api/v1/processing/documents/", {
            "patient_id": patient.id, "file": SimpleUploadedFile("p.docx", make_docx("Эхографические признаки полипа эндометрия."))},
            format="multipart")
        self.doc_id = r.json()["id"]

    def test_demo_advice_is_separate_and_labelled(self):
        advice = RoutingAdvice.objects.filter(document_id=self.doc_id)
        self.assertTrue(advice.exists())                   # сформирован фоном после разбора
        a = advice.first()
        self.assertEqual(a.engine, "demo")
        self.assertTrue(a.grounding["quote_found"])
        page = staff_client().get(f"/coordinator/cases/{self.doc_id}/").content.decode()
        self.assertIn("Рекомендации агента по маршрутизации", page)
        self.assertIn("рекомендация ИИ, требует проверки координатором", page)
        self.assertIn("Демо-режим", page)

    def test_qwen_advice_is_grounded_before_saving(self):
        """Ответ модели (подменённой) проходит проверку: совет без цитаты из протокола не сохраняется."""
        from langchain_core.runnables import RunnableLambda

        from apps.processing.services.routing_advice import AdviceItem, AdviceList, QwenRoutingAdvisor, RoutingAdviceService
        from apps.routing.facade import RoutingFacade

        route = next(r for r in RoutingFacade.route_catalog() if "endometrial_polyp" in r["finding_codes"])
        answer = AdviceList(advice=[
            AdviceItem(text="Направить к оперирующему гинекологу", rationale="Полип эндометрия в заключении",
                       evidence_quote="признаки полипа эндометрия", confidence=0.8, route_code=route["code"],
                       specialty_code=route["specialty_code"], executor="Гинеколог"),
            AdviceItem(text="Срочно к онкологу", rationale="Подозрение на опухоль",
                       evidence_quote="подозрение на злокачественный процесс", confidence=0.9),
        ])

        class FakeQwen:
            def with_structured_output(self, schema, **kwargs):
                return RunnableLambda(lambda _prompt: answer)   # без include_raw: только разобранный объект

        result_id = StudyDocument.objects.get(pk=self.doc_id).latest_result.id
        saved = RoutingAdviceService(QwenRoutingAdvisor(llm=FakeQwen())).generate(result_id)
        self.assertEqual(saved, 1)
        advice = RoutingAdvice.objects.get(document_id=self.doc_id)
        self.assertEqual((advice.engine, advice.target_route_code), ("qwen", route["code"]))
        self.assertTrue(advice.matches_matrix)
        meta = ExtractionResult.objects.get(pk=result_id).advice_meta
        self.assertEqual(meta["status"], "ready")
        self.assertIn("quote_found", meta["rejected"][0]["failed"])   # выдуманная цитата отброшена
        self.assertIn("Направить к оперирующему гинекологу", meta["raw"])  # ответ модели виден координатору
        self.assertEqual(meta["proposed"], 2)

    def test_review_is_stored_in_its_own_table(self):
        a = RoutingAdvice.objects.filter(document_id=self.doc_id).first()
        url = f"/coordinator/cases/{self.doc_id}/advice/{a.id}/review/"
        client = staff_client()
        client.post(url, {"verdict": "reject", "comment": ""})
        self.assertEqual(AdviceReview.objects.count(), 0)          # отклонение без комментария не принимается
        client.post(url, {"verdict": "correct", "comment": "Сначала гинеколог", "route_code": "nope"})
        self.assertEqual(AdviceReview.objects.count(), 0)          # маршрута нет в справочнике
        client.post(url, {"verdict": "accept", "comment": ""})
        review = AdviceReview.objects.get()
        self.assertEqual((review.verdict, review.user_id, str(review.advice_id)), ("accept", "coordinator", str(a.id)))
        self.assertIsNotNone(review.timestamp)
        self.assertEqual(review.advice_snapshot["text"], a.text)
        api = APIClient()
        r = api.post("/api/v1/coordinator/advice-reviews/", {"advice_id": str(a.id), "verdict": "reject",
                                                             "comment": "Нет показаний"}, format="json")
        self.assertEqual(r.status_code, 201)
        stats = api.get("/api/v1/coordinator/advice-reviews/stats/").json()
        self.assertEqual(stats["reviewed"], 1)                      # последняя оценка совета
        self.assertEqual(stats["by_verdict"]["reject"], 1)
        listed = api.get(f"/api/v1/coordinator/advice-reviews/?document_id={self.doc_id}").json()
        self.assertEqual(len(listed["results"] if isinstance(listed, dict) else listed), 2)
