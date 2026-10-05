from django.test import TestCase

from apps.processing.services.ai_agent import HybridFindingExtractor, LangChainFindingExtractor, RuleBasedFindingExtractor, extract_scales
from apps.processing.services.dictionary import load_dictionary
from apps.processing.services.schemas import ExtractedFinding, ExtractionPayload
from apps.processing.services.text_extraction import DocxTextExtractor

from .helpers import make_docx, seed


class ExtractionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()

    def extract(self, conclusion: str) -> ExtractionPayload:
        text = DocxTextExtractor().extract(make_docx(conclusion)).text
        return RuleBasedFindingExtractor().extract(text, dictionary=load_dictionary())

    def test_merged_cells_are_deduplicated_and_header_parsed(self):
        result = DocxTextExtractor().extract(make_docx("Норма"))
        self.assertIn("Амбулаторная карта № | TEST-1", result.text)
        self.assertEqual(result.study_date.isoformat(), "2026-08-26")
        self.assertIn("малого таза", result.study_type.lower())

    def test_polyp_found_with_evidence(self):
        payload = self.extract("Эхографические признаки полипа эндометрия.")
        polyp = next(f for f in payload.findings if f.code == "endometrial_polyp")
        self.assertFalse(polyp.negated)
        self.assertIn("полипа эндометрия", polyp.evidence_quote)

    def test_negation_is_not_trigger(self):
        payload = self.extract("Данных за тромбоз глубоких вен не получено.")
        self.assertTrue(all(f.negated for f in payload.findings if f.code == "dvt"))

    def test_description_section_is_ignored(self):
        # «Объемные образования яичников: не лоцируются» стоит в описании, не в заключении.
        payload = self.extract("УЗ-признаков патологии не выявлено.")
        self.assertFalse([f for f in payload.findings if not f.negated])

    def test_scales(self):
        self.assertEqual(extract_scales("Категория BI-RADS 1 (правая) Категория BI-RADS 4 (левая)"), {"birads": 4})
        self.assertEqual(extract_scales("EU-TIRADS справа 3 , слева 3"), {"tirads": 3})
        self.assertEqual(extract_scales("Категория O-RADS I справа, O-RADS I слева"), {"orads": 1})

    def test_recommendation_with_interval(self):
        payload = self.extract("Без особенностей. Рекомендовано: консультация маммолога и УЗИ через 6 мес")
        kinds = {(r.specialty_code, r.kind, r.interval_days) for r in payload.recommendations}
        self.assertIn(("mammologist", "consultation", None), kinds)
        self.assertIn((None, "diagnostics", 180), kinds)

    def test_llm_hallucinated_quote_is_dropped_and_hybrid_merges(self):
        class FakeLLM:
            """Подмена LLM: LangChain-цепочка вызывает with_structured_output(...).invoke(...)."""

            def with_structured_output(self, schema):
                from langchain_core.runnables import RunnableLambda

                return RunnableLambda(lambda _: ExtractionPayload(findings=[
                    ExtractedFinding(code="hernia", label="Грыжа", evidence_quote="грыжа белой линии живота"),
                    ExtractedFinding(code="endometrial_polyp", label="Полип", evidence_quote="полипа эндометрия"),
                ]))

        text = DocxTextExtractor().extract(make_docx("Эхографические признаки полипа эндометрия.")).text
        llm = LangChainFindingExtractor(llm=FakeLLM())
        payload = HybridFindingExtractor(primary=llm, fallback=RuleBasedFindingExtractor()).extract(text, dictionary=load_dictionary())
        codes = {f.code for f in payload.findings}
        self.assertIn("endometrial_polyp", codes)
        self.assertNotIn("hernia", codes)  # цитаты нет в тексте — отброшено
