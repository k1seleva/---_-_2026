"""
Публичный интерфейс модуля обработки для других модулей.

Правило архитектуры: другие модули импортируют только facade.py и никогда models.py.
При выносе в микросервис реализация фасада заменится HTTP-клиентом с тем же интерфейсом.
Возвращаются простые словари (DTO), а не ORM-объекты.
"""
from collections import Counter
from uuid import UUID

from .models import (ExtractionResult, FindingDefinition, FocusProfile, ProtocolSegment, QualityRun, RoutingAdvice,
                     SegmentPin, StudyDocument, UploadBatch)
from .models import ProcessingJob
from .services.annotation import HIGHLIGHT_TYPES, SIGNIFICANT_TYPES
from .services.markers import ANALYSIS_VERSION, MARKER_TYPES, SOURCE_TITLES, TRIGGER_TYPES
from .services.presenter import SCALE_TITLES, build_doctor_view, evidence_html, focus_key_title, layered_evidence_html


def _segment_dto(s: ProtocolSegment) -> dict:
    types = list(dict.fromkeys(h["type"] for h in s.highlights))
    return {
        "id": s.seq, "section": s.section, "organ": s.organ, "text": s.text,
        "start": s.span_start, "end": s.span_end, "kind": s.kind, "emergency": s.emergency,
        "finding_codes": s.finding_codes, "signs": s.signs, "negated_codes": s.negated_codes,
        "attributes": s.attributes, "highlights": s.highlights, "highlight_types": types,
        "significant": bool(set(types) & SIGNIFICANT_TYPES), "uncertain": s.uncertain, "linked_to": s.linked_to,
        "link_reason": s.link_reason, "not_in_conclusion": s.not_in_conclusion, "sources": s.sources,
        "llm_labels": s.llm_labels,
    }


def _document_dto(doc: StudyDocument) -> dict:
    result = doc.latest_result
    return {
        "id": str(doc.id),
        "patient_id": str(doc.patient_id or ""),
        "identity": doc.identity,
        "card_number": doc.card_number,
        "filename": doc.original_filename,
        "batch_id": str(doc.batch_id or ""),
        "location_code": doc.location_code,
        "study_type": doc.study_type,
        "study_date": doc.study_date.isoformat() if doc.study_date else None,
        "performed_by": doc.performed_by,
        "status": doc.status,
        "version": doc.version,
        "uploaded_at": doc.created_at,
        "conclusion": result.conclusion if result else "",
        "summary_for_patient": result.summary_for_patient if result else "",
        "engine": result.engine if result else "",
        "dictionary_version": result.dictionary_version if result else "",
        "error": (doc.jobs.order_by("-created_at").values_list("error", flat=True).first() or "") if doc.status == "failed" else "",
        "findings": [
            {
                "code": f.code, "label": f.label, "evidence_quote": f.evidence_quote,
                "negated": f.negated, "uncertain": f.uncertain, "attributes": f.attributes,
                "confidence": f.confidence, "span_start": f.span_start, "span_end": f.span_end, "severity": f.severity,
                "rule_id": f.rule_id, "source": f.source,
            }
            for f in (result.findings.all() if result else [])
        ],
        "recommendations": result.payload.get("recommendations", []) if result else [],
        "annotation_summary": result.annotation_summary if result else {},
    }


class ProcessingFacade:
    @staticmethod
    def get_document(document_id: UUID | str) -> dict | None:
        doc = StudyDocument.objects.filter(pk=document_id).first()
        return _document_dto(doc) if doc else None

    @staticmethod
    def get_annotation(document_id: UUID | str, *, owner: str = "", focus: list[str] | None = None) -> dict | None:
        """Разметка для врача: «В фокусе» по приоритетам врача и закреплённым фрагментам, затем порядок протокола."""
        doc = StudyDocument.objects.filter(pk=document_id).first()
        result = doc.latest_result if doc else None
        if not result:
            return None
        segments = [_segment_dto(s) for s in result.segments.all()]
        titles = dict(FindingDefinition.objects.values_list("code", "title"))
        profile = ProcessingFacade.focus_profile(owner) if owner else []
        pinned = set(SegmentPin.objects.filter(document=doc, owner=owner).values_list("seq", flat=True)) if owner else set()
        grounding = (result.annotation_summary or {}).get("grounding", {})
        parts = [grounding.get("extraction") or {}, grounding.get("segments") or {}]
        return {
            "document_id": str(doc.id), "summary": result.annotation_summary, "segments": segments,
            "view": build_doctor_view(segments, titles, focus=(focus if focus is not None else profile), pinned=pinned)
            if segments else None,
            "profile": [{"key": k, "title": focus_key_title(k, titles)} for k in profile],
            "case_focus": focus is not None,
            "integrity": {
                "llm_used": any(p.get("llm_used") for p in parts),
                "rejected": sum(p.get("rejected_count", 0) for p in parts),
                "hidden_ignored": sum(p.get("hidden_ignored", 0) for p in parts),
                "missing_segments": sum(len(p.get("missing_segments", [])) for p in parts),
            },
        }

    # ------------------------------------------------------------ приоритеты врача (без системного ранжирования)
    @staticmethod
    def focus_profile(owner: str) -> list[str]:
        return list(FocusProfile.objects.filter(owner=owner).values_list("items", flat=True).first() or [])

    @staticmethod
    def save_focus_profile(owner: str, items: list[str]) -> list[str]:
        items = [i for i in dict.fromkeys(items) if i][:30]
        FocusProfile.objects.update_or_create(owner=owner, defaults={"items": items})
        return items

    @staticmethod
    def focus_catalog() -> list[dict]:
        """Из чего врач собирает свои приоритеты: находки словаря, признаки, причины подсветки."""
        from .services.annotation import SIGN_TITLES

        items = [{"key": code, "title": title, "group": "Находки из словаря"}
                 for code, title in FindingDefinition.objects.filter(is_active=True).values_list("code", "title")]
        items += [{"key": code, "title": title, "group": "Шкалы риска"} for code, title in SCALE_TITLES.items()]
        items += [{"key": f"sign:{code}", "title": title, "group": "Признаки в тексте"}
                  for code, title in SIGN_TITLES.items() if code != "us_signs"]
        items += [{"key": f"hl:{code}", "title": title, "group": "Причины подсветки"} for code, title in HIGHLIGHT_TYPES.items()]
        return items

    @staticmethod
    def toggle_pin(document_id, seq: int, owner: str) -> bool:
        pin = SegmentPin.objects.filter(document_id=document_id, seq=seq, owner=owner).first()
        if pin:
            pin.delete()
            return False
        SegmentPin.objects.create(document_id=document_id, seq=seq, owner=owner)
        return True

    # ------------------------------------------------------------ подписи для пациента и интерфейса
    @staticmethod
    def finding_titles(codes=None) -> dict[str, str]:
        qs = FindingDefinition.objects.all()
        if codes is not None:
            qs = qs.filter(code__in=list(codes))
        titles = dict(qs.values_list("code", "title"))
        for code in codes or SCALE_TITLES:
            if code in SCALE_TITLES:
                titles[code] = SCALE_TITLES[code]
        return titles

    @staticmethod
    def patient_texts(codes) -> list[dict]:
        """Утверждённые тексты для пациента по кодам находок. Экстренные находки пациенту не объясняются."""
        rows = FindingDefinition.objects.filter(code__in=list(codes)).exclude(severity="emergency")
        return [{"code": f.code, "title": f.patient_title or f.title, "explanation": f.patient_explanation}
                for f in rows]

    # ------------------------------------------------------------ пачки
    @staticmethod
    def list_batches(limit: int = 20, location_code: str = "") -> list[dict]:
        qs = UploadBatch.objects.all()
        if location_code:
            qs = qs.filter(location_code=location_code)
        return [ProcessingFacade._batch_dto(b) for b in qs[:limit]]

    @staticmethod
    def get_batch(batch_id) -> dict | None:
        b = UploadBatch.objects.filter(pk=batch_id).first()
        if not b:
            return None
        data = ProcessingFacade._batch_dto(b)
        data["documents"] = [{"id": str(d.id), "filename": d.original_filename, "status": d.status, "identity": d.identity,
                              "patient_id": str(d.patient_id or ""), "study_type": d.study_type}
                             for d in b.documents.order_by("original_filename")]
        data["progress"] = ProcessingFacade.batch_progress(b.id)
        return data

    @staticmethod
    def batch_progress(batch_id) -> dict:
        """Ход разбора пачки для «Входящих»: очередь, готово, ошибки, среднее время, оценка остатка."""
        from .services.jobs import batch_progress

        return batch_progress(batch_id)

    @staticmethod
    def queue_summary() -> dict:
        """Идёт ли разбор сейчас (для шапки рабочего места): сколько протоколов ждут и пачка, которую открыть."""
        active = ProcessingJob.objects.filter(status__in=[ProcessingJob.Status.QUEUED, ProcessingJob.Status.RUNNING])
        count = active.count()
        if not count:
            return {"active": 0}
        batch_id = active.exclude(document__batch_id=None).order_by("-created_at").values_list(
            "document__batch_id", flat=True).first()
        return {"active": count, "batch_id": str(batch_id) if batch_id else ""}

    @staticmethod
    def retry_batch(batch_id, *, what: str) -> int:
        """Повторить разбор протоколов пачки: what=llm — где модель не ответила, failed — где разбор упал."""
        from .services.jobs import ProcessingQueue

        docs = StudyDocument.objects.filter(batch_id=batch_id)
        if what == "failed":
            return ProcessingQueue.reprocess(docs.filter(status=StudyDocument.Status.FAILED).values_list("id", flat=True))
        return ProcessingQueue.reprocess(docs.values_list("id", flat=True), only_llm_errors=True)

    @staticmethod
    def _batch_dto(b: UploadBatch) -> dict:
        statuses = Counter(b.documents.values_list("status", flat=True))
        return {"id": str(b.id), "created_at": b.created_at, "source": b.source, "source_display": b.get_source_display(),
                "location_code": b.location_code, "created_by": b.created_by, "files_total": b.files_total,
                "accepted": b.accepted, "duplicates": b.duplicates, "rejected": b.rejected,
                "processing": statuses.get("uploaded", 0) + statuses.get("processing", 0),
                "failed": statuses.get("failed", 0), "processed": statuses.get("processed", 0)}

    @staticmethod
    def annotation_stats(location_code: str = "") -> dict:
        """Аналитика поступивших признаков по актуальным версиям протоколов (без уровней важности)."""
        results = ExtractionResult.objects.filter(document__status=StudyDocument.Status.PROCESSED)
        if location_code:
            results = results.filter(document__location_code=location_code)
        latest_ids = {}
        for r in results.order_by("document_id", "-created_at").values("id", "document_id"):
            latest_ids.setdefault(r["document_id"], r["id"])
        segments = ProtocolSegment.objects.filter(result_id__in=latest_ids.values())
        type_segments, type_docs, codes = Counter(), Counter(), Counter()
        highlighted = 0
        for result_id, highlights, finding_codes in segments.values_list("result_id", "highlights", "finding_codes"):
            types = set(h["type"] for h in highlights)
            highlighted += bool(types)
            type_segments.update(types)
            if types & SIGNIFICANT_TYPES:
                codes.update(finding_codes)
        docs_by_type = {}
        for result_id, highlights in segments.values_list("result_id", "highlights"):
            for t in {h["type"] for h in highlights}:
                docs_by_type.setdefault(t, set()).add(result_id)
        type_docs = {t: len(ids) for t, ids in docs_by_type.items()}
        not_in_conclusion = uncertain = rejected = missing = hidden = llm_docs = emergency = 0
        titles = dict(FindingDefinition.objects.values_list("code", "title"))
        for r in ExtractionResult.objects.filter(id__in=latest_ids.values()):
            summary = r.annotation_summary or {}
            not_in_conclusion += bool(summary.get("not_in_conclusion"))
            uncertain += bool(summary.get("uncertain"))
            emergency += bool(summary.get("emergency"))
            grounding = summary.get("grounding", {})
            for part in (grounding.get("extraction") or {}, grounding.get("segments") or {}):
                rejected += part.get("rejected_count", 0)
                hidden += part.get("hidden_ignored", 0)
                llm_docs += bool(part.get("llm_used"))
                missing += len(part.get("missing_segments", []))
        return {
            "documents": len(latest_ids),
            "segments": segments.count(),
            "highlighted": highlighted,
            "types": [{"type": t, "title": HIGHLIGHT_TYPES[t], "segments": type_segments.get(t, 0),
                       "documents": type_docs.get(t, 0)} for t in HIGHLIGHT_TYPES if type_segments.get(t)],
            "top_findings": [{"code": code, "title": titles.get(code) or SCALE_TITLES.get(code, code), "count": n}
                             for code, n in codes.most_common(10)],
            "not_in_conclusion_documents": not_in_conclusion,
            "uncertain_documents": uncertain,
            "emergency_documents": emergency,
            "grounding": {"llm_runs": llm_docs, "rejected": rejected, "hidden_ignored": hidden, "missing_segments": missing},
        }

    @staticmethod
    def trigger_stats(location_code: str = "") -> dict:
        """Триггеры по актуальным версиям протоколов: всего, по типам, доля протоколов с триггером (3.2)."""
        docs = StudyDocument.objects.filter(status=StudyDocument.Status.PROCESSED)
        if location_code:
            docs = docs.filter(location_code=location_code)
        by_type, markers, total, with_triggers, documents = Counter(), Counter(), 0, 0, 0
        by_source, marker_sources = Counter(), Counter()
        for doc_id in docs.values_list("id", flat=True):
            analysis = ProcessingFacade.get_analysis(doc_id)
            if not analysis:
                continue
            documents += 1
            triggers = analysis.get("triggers") or []
            total += len(triggers)
            with_triggers += bool(triggers)
            by_type.update(t["type"] for t in triggers)
            by_source.update(t.get("source", "rules") for t in triggers)
            markers.update((analysis.get("stats") or {}).get("markers_by_type") or {})
            marker_sources.update((analysis.get("stats") or {}).get("markers_by_source") or {})
        return {
            "documents": documents, "triggers": total, "with_triggers": with_triggers,
            "share": round(100 * with_triggers / documents, 1) if documents else None,
            "by_type": [{"type": k, "title": t, "count": by_type.get(k, 0)} for k, t in TRIGGER_TYPES.items()],
            "markers": [{"type": k, "title": t, "count": markers[k]} for k, t in MARKER_TYPES.items() if markers.get(k)],
            "by_source": [{"source": k, "title": t, "triggers": by_source.get(k, 0), "markers": marker_sources.get(k, 0)}
                          for k, t in SOURCE_TITLES.items()],
            "ai_used": bool(by_source.get("llm") or by_source.get("both") or marker_sources.get("llm")
                            or marker_sources.get("both")),
        }

    @staticmethod
    def latest_quality_summary() -> dict | None:
        """Последний завершённый прогон «Качества разбора» (реальные счётчики) — для дашборда."""
        run = QualityRun.objects.filter(status=QualityRun.Status.DONE).exclude(summary={}).order_by("-created_at").first()
        if not run:
            return None
        s = run.summary
        return {"id": str(run.id), "title": run.title or "Прогон протоколов", "when": run.created_by,
                "protocols": s.get("read_ok", 0), "trigger_rate": s.get("trigger_rate"),
                "any_trigger_rate": s.get("any_trigger_rate"), "sufficient": dict(s.get("verdicts") or []).get("sufficient", 0)}

    @staticmethod
    def other_versions(document_id: UUID | str) -> list[str]:
        """Другие версии того же протокола МИС (для снятия проверок со старых версий)."""
        doc = StudyDocument.objects.filter(pk=document_id).first()
        if not doc or not doc.external_id:
            return []
        return [str(pk) for pk in StudyDocument.objects.filter(external_id=doc.external_id).exclude(pk=doc.pk)
                .values_list("id", flat=True)]

    @staticmethod
    def parse_recommendations(text: str) -> list[dict]:
        """Разбор рекомендаций врача тем же экстрактором, что и протокол (справочник специальностей)."""
        from .services.ai_agent import RuleBasedFindingExtractor
        from .services.dictionary import load_dictionary

        return [r.model_dump() for r in RuleBasedFindingExtractor._recommendations(text, load_dictionary())]

    @staticmethod
    def evidence_html(document_id: UUID | str) -> str:
        """Текст протокола с подсветкой цитат-доказательств (HTML уже экранирован)."""
        doc = StudyDocument.objects.filter(pk=document_id).first()
        if not doc:
            return ""
        return evidence_html(doc.raw_text, _document_dto(doc)["findings"])

    # ------------------------------------------------------------ маркеры и триггеры (точная разметка)
    @staticmethod
    def get_analysis(document_id: UUID | str) -> dict | None:
        """Маркеры, перекрытия и триггеры актуального разбора. Для протоколов, разобранных до появления
        маркеров, разбор строится при первом обращении по сохранённым фрагментам и находкам."""
        doc = StudyDocument.objects.filter(pk=document_id).first()
        result = doc.latest_result if doc else None
        if not result:
            return None
        if (result.analysis or {}).get("version") != ANALYSIS_VERSION:
            result.analysis = ProcessingFacade._rebuild_analysis(doc, result)
            result.save(update_fields=["analysis", "updated_at"])
        return result.analysis

    @staticmethod
    def _rebuild_analysis(doc: StudyDocument, result: ExtractionResult) -> dict:
        from .services.dictionary import load_dictionary
        from .services.pipeline import build_analysis

        segments = [_segment_dto(s) for s in result.segments.all()]
        if not segments:
            return {}
        findings = _document_dto(doc)["findings"]
        return build_analysis(doc.raw_text, segments, findings, load_dictionary(), study_type=doc.study_type)

    @staticmethod
    def engines(document_id: UUID | str) -> dict:
        """Кто размечал протокол: словарь всегда; ИИ-агент (Qwen) — извлечение находок и разметка фрагментов.
        status: ok | error | off; при ошибке — причина, чтобы не гадать, почему нет находок ИИ."""
        from .services.ai_agent import model_label

        doc = StudyDocument.objects.filter(pk=document_id).first()
        result = doc.latest_result if doc else None
        if not result:
            return {}
        extraction = ((result.payload or {}).get("engines") or {}).get("llm") or {"status": "off"}
        markup = (result.annotation_summary or {}).get("llm") or {"status": "off"}
        statuses = {extraction.get("status"), markup.get("status")}
        overall = "error" if "error" in statuses else ("ok" if "ok" in statuses else "off")
        return {"llm": overall, "model": extraction.get("model") or markup.get("model") or model_label(),
                "extraction": extraction, "markup": markup, "configured": bool(model_label())}

    @staticmethod
    def evidence_layers_html(document_id: UUID | str) -> str:
        """Текст протокола с многослойной подсветкой маркеров и триггеров (HTML экранирован)."""
        doc = StudyDocument.objects.filter(pk=document_id).first()
        if not doc:
            return ""
        analysis = ProcessingFacade.get_analysis(document_id) or {}
        return layered_evidence_html(doc.raw_text, analysis)

    @staticmethod
    def evidence_view(document_id: UUID | str) -> dict:
        """Всё для блока «Исходный текст с подсветкой доказательств»: HTML, легенда, данные для подсказок."""
        from .services.presenter import evidence_css
        from .services.scoring import LEVEL_TITLES

        doc = StudyDocument.objects.filter(pk=document_id).first()
        analysis = (ProcessingFacade.get_analysis(document_id) or {}) if doc else {}
        markers = analysis.get("markers") or []
        counts = Counter(m["type"] for m in markers)
        keep = ("id", "type", "type_title", "title", "text", "confidence", "level", "rule_id", "rule_ids", "rule_title",
                "factors", "negated", "corrected", "subsumed_by", "parent_id", "layer", "value", "source", "source_title")
        return {
            "html": layered_evidence_html(doc.raw_text, analysis) if doc else "",
            "css": evidence_css(),
            "legend": [{"key": k, "title": t, "count": counts[k]} for k, t in MARKER_TYPES.items() if counts.get(k)],
            "ai_count": sum(1 for m in markers if m.get("source") == "llm" and not m.get("subsumed_by")),
            "data": {
                "markers": [{k: m.get(k) for k in keep} for m in markers],
                "triggers": [{k: t.get(k) for k in ("id", "number", "type", "type_title", "title", "evidence", "confidence",
                                                    "level", "rule_id", "rule_title", "factors", "source", "source_title")}
                             for t in analysis.get("triggers") or []],
                "levels": LEVEL_TITLES,
            },
            "stats": analysis.get("stats") or {},
        }

    @staticmethod
    def evidence_css() -> str:
        from .services.presenter import evidence_css

        return evidence_css()

    @staticmethod
    def marker_types() -> dict[str, str]:
        return dict(MARKER_TYPES)

    @staticmethod
    def trigger_types() -> dict[str, str]:
        return dict(TRIGGER_TYPES)

    # ------------------------------------------------------------ советы ИИ-агента (отдельно от аналитики)
    @staticmethod
    def routing_advice(document_id: UUID | str) -> dict:
        """Советы агента по маршрутизации для актуального разбора и их статус."""
        from .services.routing_advice import configured_engine, configured_model

        doc = StudyDocument.objects.filter(pk=document_id).first()
        result = doc.latest_result if doc else None
        # configured — что включено сейчас: если советы собраны демо-режимом до подключения Qwen,
        # на странице появляется кнопка «Получить ответ Qwen».
        configured = {"configured_engine": configured_engine(), "configured_model": configured_model()}
        if not result:
            return {"items": [], "meta": {}, **configured}
        items = [_advice_dto(a) for a in RoutingAdvice.objects.filter(result=result)]
        return {"items": items, "meta": result.advice_meta or {}, **configured}

    @staticmethod
    def get_advice(advice_id: UUID | str) -> dict | None:
        a = RoutingAdvice.objects.filter(pk=advice_id).first()
        return _advice_dto(a) if a else None

    @staticmethod
    def generate_routing_advice(document_id: UUID | str) -> int:
        """Сформировать советы заново (кнопка на странице протокола): синхронно, без очереди."""
        from .services.routing_advice import RoutingAdviceService

        doc = StudyDocument.objects.filter(pk=document_id).first()
        result = doc.latest_result if doc else None
        return RoutingAdviceService().generate(result.id) if result else 0

    @staticmethod
    def get_full_text(document_id: UUID | str) -> str:
        return StudyDocument.objects.filter(pk=document_id).values_list("raw_text", flat=True).first() or ""

    @staticmethod
    def list_patient_documents(patient_id: UUID | str) -> list[dict]:
        return [
            {"id": str(d.id), "study_type": d.study_type, "study_date": d.study_date, "status": d.status,
             "filename": d.original_filename, "uploaded_at": d.created_at}
            for d in StudyDocument.objects.filter(patient_id=patient_id).exclude(
                status__in=[StudyDocument.Status.SUPERSEDED, StudyDocument.Status.ANNULLED]
            )
        ]

    @staticmethod
    def documents_brief(document_ids) -> dict[str, dict]:
        """Короткие сведения о протоколах для списков (без текста)."""
        return {str(d["id"]): {**d, "id": str(d["id"]), "patient_id": str(d["patient_id"] or "")}
                for d in StudyDocument.objects.filter(pk__in=list(document_ids)).values(
                    "id", "original_filename", "study_type", "study_date", "status", "location_code", "card_number",
                    "patient_id", "identity", "created_at")}


def _advice_dto(a: RoutingAdvice) -> dict:
    return {
        "id": str(a.id), "document_id": str(a.document_id), "seq": a.seq, "text": a.text, "rationale": a.rationale,
        "evidence_quote": a.evidence_quote, "confidence": a.confidence, "target_route_code": a.target_route_code,
        "target_route_title": a.target_route_title, "target_specialty_code": a.target_specialty_code,
        "target_executor": a.target_executor, "engine": a.engine, "model_name": a.model_name,
        "prompt_version": a.prompt_version, "grounding": a.grounding, "matches_matrix": a.matches_matrix,
        "created_at": a.created_at,
    }
