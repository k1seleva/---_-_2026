"""
Проверка качества аналитики на пачке протоколов: тот же конвейер, что при обычной загрузке
(текст, находки, триггеры маршрута, подсветка значимого, проверка рекомендаций), но «всухую»:
без пациентов, маршрутов и уведомлений.

    ProtocolEvaluator  — один протокол: строка отчёта + проверка инвариантов разметки
    summarize          — сводка по пачке; с разметкой эксперта — точность, полнота и расхождения
    QualityRunService  — запуск из интерфейса или API: до settings.QUALITY_MAX_PROTOCOLS файлов,
                         файлы хранятся только на время прогона и удаляются после него

Команда evaluate_protocols использует те же классы для папки на диске.
"""
import csv
import io
import re
import shutil
import time
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.db import transaction

from apps.audit.facade import AuditFacade
from apps.routing.facade import RoutingFacade
from common import clock

from ..models import QualityRun
from .ai_agent import get_finding_extractor
from .annotation import get_annotator, load_thresholds
from .dictionary import load_dictionary
from .markers import analyze_protocol
from .pipeline import IncomingFile, expand_zip
from .presenter import build_doctor_view
from .text_extraction import TextExtractionError, get_extractor

QUOTE_LIMIT = 160
CSV_FIELDS = ["file", "study_type", "findings", "rules", "expected_rules", "triggers", "trigger_types", "trigger_sources", "markers", "overlaps",
              "corrected", "highlighted", "highlight_types", "emergency", "front", "not_in_conclusion", "audit_verdict",
              "audit_issues", "audit_gaps", "llm_extraction", "llm_markup", "conclusion", "error"]


class ProtocolEvaluator:
    """Прогон одного протокола без записи в БД. Ошибка любого шага — в строке отчёта, а не исключением."""

    def __init__(self, extractor=None, dictionary=None) -> None:
        self.dictionary = dictionary or load_dictionary()
        self.extractor = extractor or get_finding_extractor()
        self.annotator = get_annotator(self.dictionary)
        self.thresholds = load_thresholds()
        self.titles = {f.code: f.title for f in self.dictionary.findings}

    def evaluate(self, name: str, data: bytes, *, with_text: bool = False) -> dict:
        started = time.monotonic()
        row = {"file": name, "study_type": "", "findings": [], "rules": [], "highlighted": 0, "highlight_types": {},
               "emergency": 0, "front": [], "not_in_conclusion": 0, "has_conclusion": None, "audit_verdict": "",
               "audit_issues": [], "audit_gaps": [], "conclusion": "", "error": "", "error_stage": ""}
        try:
            text = get_extractor(name).extract(data)
        except TextExtractionError as exc:
            return {**row, "error": str(exc), "error_stage": "read", "ms": self._ms(started)}
        payload = self.extractor.extract(text.text, study_type=text.study_type, dictionary=self.dictionary)
        findings = [f.model_dump() for f in payload.findings]
        row.update({
            "study_type": payload.study_type or text.study_type or "",
            "findings": [{"code": f["code"], "quote": (f.get("evidence_quote") or "")[:QUOTE_LIMIT],
                          "negated": f.get("negated", False), "uncertain": f.get("uncertain", False)} for f in findings],
            "rules": sorted(set(RoutingFacade.matched_rules(findings))),
            "conclusion": (payload.conclusion or "")[:300].replace("\n", " ") if with_text else "",
        })
        try:
            row.update(self._annotate_and_audit(text.text, payload, findings))
        except Exception as exc:  # noqa: BLE001 — нарушение инварианта разметки фиксируется в отчёте
            row.update(error=f"Разметка: {exc}", error_stage="markup")
        row["ms"] = self._ms(started)
        return row

    def _annotate_and_audit(self, text: str, payload, findings: list[dict]) -> dict:
        annotation = self.annotator.annotate(text, study_type=payload.study_type)  # verify_coverage внутри
        segments = [{**a.as_dict(), "significant": a.significant} for a in annotation.segments]
        view = build_doctor_view(segments, self.titles)  # PresentationError, если фрагмент потерян или задвоен
        summary = annotation.summary()
        outcome = AuditFacade.check_protocol({
            "extraction": payload.model_dump(),
            "annotation": {"summary": summary,
                           "segments": [s for s in segments if s["significant"] or s["kind"] == "recommendation"]},
        }, routes=RoutingFacade.preview_routes(findings))
        analysis = analyze_protocol(text, segments, findings, self.dictionary, thresholds=self.thresholds,
                                    route_matches=RoutingFacade.match_rules_detail(findings), study_type=payload.study_type)
        stats = analysis["stats"]
        return {
            "triggers": stats["triggers"],
            "trigger_types": stats["triggers_by_type"],
            "trigger_confidence": [t["confidence"] for t in analysis["triggers"]],
            "markers": stats["markers_counted"],
            "marker_types": stats["markers_by_type"],
            # Кто нашёл: словарь, оба или только ИИ-агент; как отработала модель (ok, error, off).
            "trigger_sources": stats["triggers_by_source"],
            "marker_sources": stats["markers_by_source"],
            "llm_extraction": (payload.engines.get("llm") or {}).get("status", "off"),
            "llm_markup": (summary.get("llm") or {}).get("status", "off"),
            "overlaps": stats["overlaps"],
            "corrected": stats["corrected"],
            "highlighted": summary["highlighted"],
            "highlight_types": summary["highlight_types"],
            "emergency": len(summary["emergency"]),
            "front": [c["title"] for c in (view["focus"]["cards"] or view["conclusion"])],
            "not_in_conclusion": len(summary["not_in_conclusion"]),
            "has_conclusion": summary["has_conclusion"],
            "audit_verdict": outcome["verdict"],
            "audit_issues": [{"type": i["issue_type"], "severity": i["severity"]} for i in outcome["issues"]],
            "audit_gaps": [i["short"] for i in outcome["issues"] if i["short"]],
        }

    @staticmethod
    def _ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)


def parse_labels(data: bytes) -> dict[str, list[str]]:
    """Разметка эксперта: CSV «file,expected_rules» (правила через «;», пусто — норма). Кодировка UTF-8 или 1251."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1251")
    dialect = csv.Sniffer().sniff(text.splitlines()[0]) if text.strip() else csv.excel
    labels = {}
    for r in csv.DictReader(io.StringIO(text), dialect=dialect):
        name = (r.get("file") or r.get("файл") or "").strip()
        if name:
            raw = r.get("expected_rules") or r.get("правила") or ""
            labels[name] = sorted({x.strip() for x in re.split(r"[;,]", raw) if x.strip()})
    return labels


def summarize(rows: list[dict], labels: dict[str, list[str]] | None = None) -> dict:
    """Сводка прогона. Доли — от протоколов, которые удалось прочитать."""
    read = [r for r in rows if r.get("error_stage") != "read"]
    ok = [r for r in read if not r.get("error")]
    n = len(read) or 1
    highlights, verdicts, issues, rules, findings = Counter(), Counter(), Counter(), Counter(), Counter()
    trigger_types, marker_types = Counter(), Counter()
    trigger_sources, marker_sources, llm_extraction, llm_markup = Counter(), Counter(), Counter(), Counter()
    for r in ok:
        highlights.update(r["highlight_types"])
        trigger_types.update(r.get("trigger_types") or {})
        marker_types.update(r.get("marker_types") or {})
        trigger_sources.update(r.get("trigger_sources") or {})
        marker_sources.update(r.get("marker_sources") or {})
        llm_extraction[r.get("llm_extraction", "off")] += 1
        llm_markup[r.get("llm_markup", "off")] += 1
        verdicts[r["audit_verdict"]] += 1
        issues.update(i["type"] for i in r["audit_issues"])
    for r in read:
        rules.update(r["rules"])
        findings.update({f["code"] for f in r["findings"] if not f["negated"]})
    summary = {
        "protocols": len(rows),
        "read_ok": len(read),
        "read_errors": len(rows) - len(read),
        "markup_errors": sum(1 for r in read if r.get("error_stage") == "markup"),
        "markup_ok": len(ok),
        "with_trigger": sum(1 for r in read if r["rules"]),
        "trigger_rate": round(100 * sum(1 for r in read if r["rules"]) / n, 1),
        "no_conclusion": sum(1 for r in ok if r["has_conclusion"] is False),
        "not_in_conclusion": sum(1 for r in ok if r["not_in_conclusion"]),
        "emergency": sum(1 for r in ok if r["emergency"]),
        "uncertain": sum(1 for r in read if any(f["uncertain"] for f in r["findings"])),
        "avg_ms": int(sum(r.get("ms", 0) for r in rows) / (len(rows) or 1)),
        "rules": rules.most_common(),
        "findings": findings.most_common(),
        "highlights": highlights.most_common(),
        "verdicts": verdicts.most_common(),
        "issues": issues.most_common(),
        # Триггеры (маркеры с позицией и правилом): всего, по типам, доля протоколов с триггером.
        "triggers_total": sum(r.get("triggers", 0) for r in ok),
        "triggers_by_type": trigger_types.most_common(),
        "with_any_trigger": sum(1 for r in ok if r.get("triggers")),
        "any_trigger_rate": round(100 * sum(1 for r in ok if r.get("triggers")) / n, 1),
        "markers_total": sum(r.get("markers", 0) for r in ok),
        "markers_by_type": marker_types.most_common(),
        "overlaps_total": sum(r.get("overlaps", 0) for r in ok),
        "corrected_total": sum(r.get("corrected", 0) for r in ok),
        "trigger_confidence_avg": _avg([c for r in ok for c in r.get("trigger_confidence") or []]),
        # Гибридный разбор: сколько нашёл словарь, сколько подтвердил ИИ-агент, сколько нашёл только он.
        "triggers_by_source": dict(trigger_sources),
        "markers_by_source": dict(marker_sources),
        "with_ai_only_trigger": sum(1 for r in ok if (r.get("trigger_sources") or {}).get("llm")),
        "llm": {"extraction": dict(llm_extraction), "markup": dict(llm_markup)},
    }
    if labels:
        summary["labelled"] = _compare(rows, labels)
    return summary


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _compare(rows: list[dict], labels: dict[str, list[str]]) -> dict:
    """Сравнение сработавших правил с разметкой эксперта: точность, полнота, F1 и список расхождений."""
    tp = fp = fn = 0
    mismatches, per_rule = [], {}
    for r in rows:
        if r["file"] not in labels or r.get("error_stage") == "read":
            continue
        got, expected = set(r["rules"]), set(labels[r["file"]])
        tp, fp, fn = tp + len(got & expected), fp + len(got - expected), fn + len(expected - got)
        for code in got | expected:
            stat = per_rule.setdefault(code, {"tp": 0, "fp": 0, "fn": 0})
            stat["tp" if code in got & expected else "fp" if code in got else "fn"] += 1
        if got != expected:
            mismatches.append({"file": r["file"], "missing": sorted(expected - got), "extra": sorted(got - expected)})
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else None
    names = {r["file"] for r in rows}
    return {
        "files": sum(1 for name in labels if name in names), "not_found": sorted(set(labels) - names)[:20],
        "tp": tp, "fp": fp, "fn": fn,
        "precision": None if precision is None else round(100 * precision, 1),
        "recall": None if recall is None else round(100 * recall, 1),
        "f1": None if f1 is None else round(100 * f1, 1),
        "mismatches": mismatches, "per_rule": sorted(per_rule.items()),
    }


def rows_to_csv(rows: list[dict], labels: dict[str, list[str]] | None = None) -> str:
    """Отчёт для Excel: одна строка на протокол. Пустая колонка expected_rules — шаблон для разметки эксперта."""
    labels = labels or {}
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({
            **r,
            "findings": "; ".join(f"{f['code']}{'(¬)' if f['negated'] else ''}{'(?)' if f['uncertain'] else ''}"
                                  for f in r["findings"]),
            "rules": "; ".join(r["rules"]),
            "trigger_types": " ".join(f"{k}={v}" for k, v in sorted((r.get("trigger_types") or {}).items())),
            "trigger_sources": " ".join(f"{k}={v}" for k, v in sorted((r.get("trigger_sources") or {}).items())),
            "expected_rules": "; ".join(labels.get(r["file"], [])),
            "highlight_types": " ".join(f"{k}={v}" for k, v in sorted(r["highlight_types"].items())),
            "front": "; ".join(r["front"]),
            "audit_issues": "; ".join(f"{i['type']}/{i['severity']}" for i in r["audit_issues"]),
            "audit_gaps": "; ".join(r["audit_gaps"]),
        })
    return out.getvalue()


class QualityRunService:
    """Запуск проверки качества из интерфейса или API."""

    def __init__(self, limit: int | None = None) -> None:
        self.limit = limit or settings.QUALITY_MAX_PROTOCOLS

    @staticmethod
    def workdir(run: QualityRun) -> Path:
        return Path(settings.MEDIA_ROOT) / "quality" / str(run.id)

    def start(self, files: list[IncomingFile], *, labels: bytes | None = None, title: str = "",
              created_by: str = "") -> QualityRun:
        accepted, rejected = self._collect(files)
        if len(accepted) > self.limit:
            # Порядок файлов — по имени: повторный запуск той же пачки даёт тот же набор.
            accepted.sort(key=lambda f: f.name)
            rejected += [{"file": f.name, "error": f"Не вошёл в прогон: за один раз проверяется до {self.limit} протоколов"}
                         for f in accepted[self.limit:]]
            accepted = accepted[:self.limit]
        run = QualityRun.objects.create(title=title[:200], created_by=created_by[:150], files_total=len(accepted),
                                        rejected=rejected, labels=parse_labels(labels) if labels else {})
        if not accepted:
            run.status, run.error, run.finished_at = QualityRun.Status.FAILED, "Нет протоколов для проверки", clock.now()
            run.save()
            return run
        folder = self.workdir(run)
        folder.mkdir(parents=True, exist_ok=True)
        for index, f in enumerate(accepted):
            # Номер в начале имени: одинаковые имена из разных папок архива не затрут друг друга.
            (folder / f"{index:03d}__{Path(f.name).name}").write_bytes(f.data)
        from ..tasks import run_quality_check
        from .jobs import in_background

        # Без Celery проверка идёт в фоновом потоке: страница загрузки не ждёт разбор сотни протоколов.
        transaction.on_commit(lambda: in_background(run_quality_check, str(run.id)))
        return run

    @staticmethod
    def _collect(files: list[IncomingFile]) -> tuple[list[IncomingFile], list[dict]]:
        accepted, rejected = [], []
        for f in files:
            suffix = Path(f.name).suffix.lower()
            if suffix == ".zip":
                inner, errors = expand_zip(f.name, f.data)
                accepted += inner
                rejected += errors
            elif suffix in settings.UPLOAD_ALLOWED_EXTENSIONS:
                accepted.append(f)
            elif not Path(f.name).name.startswith("."):
                rejected.append({"file": f.name, "error": "Формат не поддерживается"})
        return accepted, rejected

    def run(self, run_id) -> QualityRun:
        run = QualityRun.objects.get(pk=run_id)
        if run.status == QualityRun.Status.DONE:
            return run  # повторная доставка задачи
        run.status, run.processed, run.rows = QualityRun.Status.RUNNING, 0, []
        run.save(update_fields=["status", "processed", "rows", "updated_at"])
        folder = self.workdir(run)
        try:
            evaluator = ProtocolEvaluator()
            rows = []
            for path in sorted(folder.iterdir()):
                rows.append(evaluator.evaluate(path.name.split("__", 1)[-1], path.read_bytes()))
                run.processed = len(rows)
                run.save(update_fields=["processed", "updated_at"])
            run.rows, run.summary, run.status = rows, summarize(rows, run.labels), QualityRun.Status.DONE
        except Exception as exc:  # noqa: BLE001 — прогон помечается прерванным, причина видна в отчёте
            run.status, run.error = QualityRun.Status.FAILED, f"{type(exc).__name__}: {exc}"
        finally:
            shutil.rmtree(folder, ignore_errors=True)  # файлы протоколов не храним
        run.finished_at = clock.now()
        run.save()
        return run
