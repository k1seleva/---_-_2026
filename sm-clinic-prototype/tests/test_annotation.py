"""Подсветка значимого: ничего не теряется, система не ранжирует, ИИ не может придумывать и скрывать."""
from django.test import TestCase

from apps.processing.services.ai_agent import LangChainFindingExtractor
from apps.processing.services.annotation import AttentionThreshold, ProtocolAnnotator, load_thresholds
from apps.processing.services.dictionary import load_dictionary
from apps.processing.services.presenter import build_doctor_view
from apps.processing.services.schemas import ExtractedFinding, ExtractionPayload, SegmentLabel
from apps.processing.services.segmentation import Segmenter, SegmentationError, verify_coverage
from apps.processing.services.text_extraction import DocxTextExtractor

from .helpers import GALLSTONE_PROTOCOL, make_protocol_docx, seed


def texts(annotation) -> list[str]:
    """Тексты подсветок фрагмента: то, что врач видит под цитатой."""
    return [h["text"] for h in annotation.highlights]


class AnnotationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()

    def setUp(self):
        self.text = DocxTextExtractor().extract(make_protocol_docx(GALLSTONE_PROTOCOL)).text
        self.dictionary = load_dictionary()

    def annotate(self, llm=None):
        return ProtocolAnnotator(self.dictionary, thresholds=load_thresholds(), llm_classifier=llm).annotate(self.text)

    def seg(self, annotation, fragment: str):
        return next(a for a in annotation.segments if fragment in a.segment.text)

    def view(self, annotation, **kwargs):
        return build_doctor_view([a.as_dict() for a in annotation.segments], {"gallstones": "Желчнокаменная болезнь"}, **kwargs)

    def test_every_character_is_kept(self):
        segments = Segmenter().split(self.text)
        self.assertEqual(verify_coverage(self.text, segments), 1.0)
        restored = "".join(self.text[s.start:s.end] for s in segments)
        self.assertEqual("".join(restored.split()), "".join(self.text.split()))
        broken = segments[:3] + segments[4:]
        with self.assertRaises(SegmentationError):
            verify_coverage(self.text, broken)

    def test_highlights_explain_without_ranking(self):
        ann = self.annotate()
        conclusion = self.seg(ann, "холецистолитиаза")
        stones = self.seg(ann, "Конкременты множественные")
        # У фрагмента нет «уровня» или «ранга»: только причины подсветки.
        self.assertFalse(hasattr(conclusion, "level") or hasattr(conclusion, "rank"))
        self.assertIn("finding", conclusion.highlight_types)
        # Деталь описания привязана к пункту заключения и несёт свои пояснения.
        self.assertEqual(stones.linked_to, conclusion.id)
        self.assertIn("Размер: 14 мм", texts(stones))
        self.assertIn("Количество: множественные", texts(stones))
        # Норма не подсвечивается.
        self.assertEqual(self.seg(ann, "Очаговых образований не выявлено").highlights, [])
        # Подсвеченное в сводке — в порядке протокола.
        ids = [h["id"] for h in ann.summary()["highlights"]]
        self.assertEqual(ids, sorted(ids))

    def test_description_finding_missing_from_conclusion_is_flagged(self):
        ann = self.annotate()
        liver = self.seg(ann, "анэхогенное образование 8 мм")
        self.assertTrue(liver.not_in_conclusion)
        self.assertIn("not_in_conclusion", liver.highlight_types)
        self.assertIn("Есть в описании, но не вынесено в заключение", texts(liver))
        self.assertEqual(self.seg(ann, "Очаговых образований не выявлено").kind, "norm")

    def test_doctor_view_keeps_protocol_order_and_shows_each_fragment_once(self):
        ann = self.annotate()
        view = self.view(ann)
        self.assertEqual(view["shown"], view["total"])
        self.assertEqual(view["focus"]["cards"], [])                 # без приоритетов врача фокус пуст
        card = view["conclusion"][0]
        self.assertEqual(card["title"], "Желчнокаменная болезнь, конкременты")
        self.assertIn("Конкременты", card["details"][0]["text"])
        self.assertEqual([g["organ"] for g in view["description_groups"]], ["Печень"])
        self.assertTrue(any("Рекомендовано" in s["text"] for s in view["recommendations"]))

    def test_focus_and_pins_only_reorder(self):
        ann = self.annotate()
        default = self.view(ann)
        focused = self.view(ann, focus=["sign:mass"])
        self.assertEqual([c["title"] for c in focused["focus"]["cards"]], ["Образование"])
        self.assertEqual(focused["focus"]["cards"][0]["focus_reason"], "образование")
        self.assertEqual(focused["description_groups"], [])          # карточка переехала, а не скопирована
        self.assertEqual((focused["shown"], focused["total"]), (default["shown"], default["total"]))
        stones = self.seg(ann, "Конкременты множественные")
        pinned = self.view(ann, pinned={stones.id})
        self.assertTrue(pinned["focus"]["cards"][0]["pinned"])      # закреплена вся карточка пункта заключения
        self.assertEqual(pinned["conclusion"], [])

    def test_threshold_adds_explanation_not_rank(self):
        text = "ЗАКЛЮЧЕНИЕ: Полип желчного пузыря 12 мм."
        threshold = AttentionThreshold(code="t", finding_code="gallbladder_polyp",
                                       conditions=({"attr": "size_mm", "op": "gte", "value": 10},),
                                       message="Полип {value} мм (порог 10 мм)")
        annotator = ProtocolAnnotator(self.dictionary, thresholds=[threshold])
        polyp = annotator.annotate(text).segments[0]
        self.assertIn("rule", polyp.highlight_types)
        self.assertIn("Полип 12 мм (порог 10 мм)", texts(polyp))
        small = annotator.annotate("ЗАКЛЮЧЕНИЕ: Полип желчного пузыря 6 мм.").segments[0]
        self.assertNotIn("rule", small.highlight_types)


class GroundingTests(TestCase):
    """Агент работает только с данными протокола: всё, чего нет в тексте, отбрасывается."""

    @classmethod
    def setUpTestData(cls):
        seed()

    def setUp(self):
        self.text = DocxTextExtractor().extract(make_protocol_docx(GALLSTONE_PROTOCOL)).text
        self.dictionary = load_dictionary()

    def test_llm_can_only_add_a_quoted_highlight(self):
        segments = {s.text: s.id for s in Segmenter().split(self.text)}
        stones = next(i for t, i in segments.items() if "Конкременты" in t)
        contour = next(i for t, i in segments.items() if t.startswith("Контуры ровные"))
        size = next(i for t, i in segments.items() if "Размеры не увеличены" in t)
        wall = next(i for t, i in segments.items() if t.startswith("Стенка"))

        class FakeSegmentLLM:
            def label(self, segments, **kwargs):
                return [
                    SegmentLabel(segment_id=999, kind="finding", highlight=True),                  # нет такого фрагмента
                    SegmentLabel(segment_id=contour, kind="finding", highlight=True,
                                 evidence_quote="опухоль печени"),                                 # цитаты нет в тексте
                    SegmentLabel(segment_id=stones, kind="norm", highlight=False),                 # попытка снять подсветку
                    SegmentLabel(segment_id=wall, kind="abnormal", highlight=True),                # подсветка без цитаты
                    SegmentLabel(segment_id=size, kind="abnormal", highlight=True, finding_codes=["made_up"],
                                 attributes={"size_mm": 99}, evidence_quote="Размеры не увеличены"),  # число и код выдуманы
                ]

        ann = ProtocolAnnotator(self.dictionary, thresholds=load_thresholds(), llm_classifier=FakeSegmentLLM()).annotate(self.text)
        by_id = ann.by_id()
        report = ann.report.as_dict()
        reasons = {r["reason"] for r in report["rejected"]}
        self.assertIn("ссылка на несуществующий фрагмент", reasons)
        self.assertIn("цитата не найдена во фрагменте", reasons)
        self.assertIn("подсветка без цитаты", reasons)
        self.assertIn("size_mm=99: числа нет в тексте протокола", reasons)
        self.assertIn("код made_up вне словаря", reasons)
        # Снять подсветку правил нельзя: фрагмент остаётся отмеченным, попытка учтена.
        self.assertIn("change", by_id[stones].highlight_types)
        self.assertEqual(report["hidden_ignored"], 1)
        self.assertEqual(by_id[contour].highlights, [])               # выдуманное основание не принято
        self.assertEqual(by_id[wall].highlights, [])
        # Добавить подсветку с дословной цитатой можно, и она подписана как ИИ.
        self.assertEqual(by_id[size].highlight_types, ["llm"])
        self.assertIn("«Размеры не увеличены»", by_id[size].highlights[0]["text"])
        self.assertNotIn(99, by_id[size].attributes.values())
        self.assertIn("llm", by_id[size].sources)
        # Пропущенные агентом фрагменты сохранили разметку правил и попали в отчёт.
        self.assertTrue(report["missing_segments"])
        self.assertEqual(len(ann.segments), len(segments))

    def test_extractor_drops_invented_findings_numbers_and_free_text(self):
        payload = ExtractionPayload(
            findings=[
                ExtractedFinding(code="gallstones", label="ЖКБ", evidence_quote="холецистолитиаза", attributes={"size_mm": 25}),
                ExtractedFinding(code="hernia", label="Грыжа", evidence_quote="грыжа белой линии живота"),
                ExtractedFinding(code="cancer", label="Рак", evidence_quote="образование 8 мм"),
            ],
            summary_for_patient="У вас камни, нужна операция",
        )
        grounded = LangChainFindingExtractor.ground(payload, self.text, self.dictionary)
        self.assertEqual([f.code for f in grounded.findings], ["gallstones"])
        self.assertEqual(grounded.findings[0].attributes, {})            # 25 мм в цитате нет
        self.assertNotIn("операция", grounded.summary_for_patient)       # только утверждённый шаблон
        self.assertEqual(grounded.grounding["rejected_count"], 3)


class AnnotationEdgeCaseTests(TestCase):
    """Случаи, найденные прогоном реальных протоколов (здесь — синтетические формулировки того же вида)."""

    @classmethod
    def setUpTestData(cls):
        seed()

    def annotate(self, text: str, study_type: str = ""):
        return ProtocolAnnotator(load_dictionary(), thresholds=load_thresholds()).annotate(text, study_type=study_type)

    @staticmethod
    def seg(annotation, fragment: str):
        return next(a for a in annotation.segments if fragment in a.segment.text)

    def test_negations_with_typos_lists_and_trailing_no(self):
        ann = self.annotate(
            "Свободная жидкость: не выявляется.\n"
            "Свободная жидкость в малом тазу: не вуизуализируется.\n"
            "Наличие объемных образований с учетом ЦДК :не\n"
            "Рефлюкс на клапанах ствола МПВ не регестируется.\n"
            "Желчные протоки не расширены, кисты , дополнительные образования – не определяются.\n"
            "Конкременты в просвете, полипы не выявлены.\n"
            "Заключение: диффузные изменения миометрия, миомы матки малых размеров без признаков роста.\n")
        for fragment in ("не выявляется", "не вуизуализируется", "ЦДК :не", "не регестируется", "кисты ,"):
            self.assertEqual(self.seg(ann, fragment).kind, "norm", fragment)
        # Отрицаются полипы, но не конкременты в просвете.
        self.assertIn("stone", self.seg(ann, "Конкременты в просвете").signs)
        # «Без признаков роста» относится к росту, а не к миоме.
        self.assertIn("myoma", self.seg(ann, "миомы матки").signs)

    def test_related_sign_links_only_within_the_same_organ(self):
        ann = self.annotate(
            "Матка: размеры обычные.\n"
            "По задней стенке интрамуральный узел 26х38 мм.\n"
            "Шейка матки: неоднородная за счет анэхогенных образований до 9 мм.\n"
            "Заключение: Миома матки.\n", study_type="УЗИ органов малого таза")
        node = self.seg(ann, "интрамуральный узел")
        cervix = self.seg(ann, "Шейка матки")
        conclusion = self.seg(ann, "Миома матки")
        self.assertEqual(node.linked_to, conclusion.id)          # узел описания = миома из заключения
        self.assertFalse(node.not_in_conclusion)
        self.assertTrue(cervix.not_in_conclusion)                # образования шейки в заключение не вынесены

    def test_same_organ_does_not_hide_an_omitted_focal_lesion(self):
        ann = self.annotate("Печень: в правой доле киста 15 мм.\nЗаключение: Диффузные изменения печени.\n")
        cyst = self.seg(ann, "киста 15 мм")
        self.assertEqual(cyst.linked_to, self.seg(ann, "Диффузные изменения").id)  # сгруппирована по органу
        self.assertTrue(cyst.not_in_conclusion)                                     # но пропуск виден
        self.assertIn("Есть в описании, но не вынесено в заключение", texts(cyst))

    def test_lymph_nodes_are_not_nodules(self):
        ann = self.annotate("Узловые образования: нет, подмышечные лимфатические узлы не увеличены, толщина узлов 5-6 мм.\n"
                            "Заключение: Без патологии.\n")
        self.assertNotIn("nodule", self.seg(ann, "толщина узлов").signs)

    def test_sentence_broken_across_lines_is_one_fragment(self):
        ann = self.annotate("В правой доле лоцируется изоэхогенное образование с четкими\n"
                            "ровными контурами размерами 8,5*6,3 мм.\nЗаключение: Узел правой доли щитовидной железы.\n",
                            study_type="УЗИ щитовидной железы")
        nodule = self.seg(ann, "изоэхогенное образование")
        self.assertIn("ровными контурами", nodule.segment.text)
        self.assertIn("Размер: 8,5×6,3 мм", texts(nodule))

    def test_1c_prescriptions_block_is_a_recommendation(self):
        ann = self.annotate("Заключение: Миома матки.\nДиагноз\nОсновной: D25.1, Лейомиома матки\n"
                            "Назначенные услуги\nПрием (осмотр, консультация) врача-акушера-гинеколога повторный\n")
        self.assertEqual(self.seg(ann, "Диагноз").kind, "heading")
        visit = self.seg(ann, "врача-акушера-гинеколога")
        self.assertEqual(visit.kind, "recommendation")
        self.assertIn("gynecologist", visit.attributes["specialties"])
