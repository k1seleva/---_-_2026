from rest_framework import serializers

from ..models import ExtractionResult, Finding, StudyDocument


class DocumentUploadSerializer(serializers.Serializer):
    patient_id = serializers.UUIDField(required=False, allow_null=True,
                                       help_text="Пусто — пациента определит обработка по номеру карты в протоколе")
    file = serializers.FileField()
    external_id = serializers.CharField(required=False, allow_blank=True, help_text="ID протокола в МИС")
    location_code = serializers.CharField(required=False, allow_blank=True)


class FindingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Finding
        # rule_id и source (словарь, ИИ, оба) — расширения: прежние поля и их смысл не менялись.
        fields = ["code", "label", "evidence_quote", "negated", "uncertain", "attributes",
                  "confidence", "severity", "span_start", "span_end", "rule_id", "source"]


class ExtractionResultSerializer(serializers.ModelSerializer):
    findings = FindingSerializer(many=True, read_only=True)
    analysis = serializers.SerializerMethodField(help_text="Маркеры (с parent_id, layer, subsumed_by), перекрытия, триггеры")
    qwen_advice = serializers.SerializerMethodField(help_text="Советы ИИ-агента: рекомендация, требует проверки координатором")

    class Meta:
        model = ExtractionResult
        # Новые поля analysis и qwen_advice добавлены в конец: старые клиенты их просто не читают.
        fields = ["id", "engine", "dictionary_version", "conclusion", "summary_for_patient",
                  "payload", "findings", "created_at", "analysis", "qwen_advice"]

    def get_analysis(self, obj):
        from ..facade import ProcessingFacade
        from ..services.markers import ANALYSIS_VERSION

        if (obj.analysis or {}).get("version") == ANALYSIS_VERSION:
            return obj.analysis
        # Протокол разобран до появления маркеров: разбор строится один раз и сохраняется.
        return ProcessingFacade.get_analysis(obj.document_id) if obj.document.latest_result == obj else None

    def get_qwen_advice(self, obj):
        return advice_payload(obj)


def advice_payload(result) -> dict:
    from ..facade import _advice_dto

    meta = result.advice_meta or {}
    return {
        "label": "рекомендация ИИ, требует проверки координатором",
        "status": meta.get("status", "pending"), "engine": meta.get("engine", ""), "model": meta.get("model", ""),
        "prompt_version": meta.get("prompt_version", ""), "duration_ms": meta.get("duration_ms"),
        "error": meta.get("error", ""), "raw_answer": meta.get("raw", ""),
        "items": [_advice_dto(a) for a in result.routing_advice.all()],
    }


class StudyDocumentSerializer(serializers.ModelSerializer):
    result = serializers.SerializerMethodField()
    processing_status = serializers.SerializerMethodField()

    class Meta:
        model = StudyDocument
        fields = ["id", "patient_id", "external_id", "version", "original_filename", "file_format",
                  "study_type", "study_date", "status", "processing_status", "result", "created_at"]

    def get_result(self, obj):
        result = obj.latest_result
        return ExtractionResultSerializer(result).data if result else None

    def get_processing_status(self, obj):
        job = obj.jobs.order_by("-created_at").first()
        return {"status": job.status, "engine": job.engine, "error": job.error} if job else None
