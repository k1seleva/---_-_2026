"""Страницы модуля обработки: массовая загрузка протоколов (файлы, папка, zip, слежение за папкой)
и закрепление фрагментов протокола врачом. Карта протокола живёт в рабочем месте координатора."""
from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from apps.doctors.facade import DoctorsFacade
from common.identity import actor, focus_owner

from .facade import ProcessingFacade
from .models import QualityRun, UploadBatch
from .services.pipeline import BatchIngestService, IncomingFile
from .services.presenter import evidence_css
from .services.quality import QualityRunService, rows_to_csv

UPLOAD_SOURCES = {UploadBatch.Source.MANUAL, UploadBatch.Source.BROWSER_FOLDER}


def upload(request):
    """GET — страница загрузки. POST — пачка файлов (обычная форма или XHR со страницы)."""
    if request.method == "POST":
        files = [IncomingFile(name=f.name, data=f.read()) for f in request.FILES.getlist("files")]
        location = request.POST.get("location_code", "")
        source = request.POST.get("source") if request.POST.get("source") in UPLOAD_SOURCES else UploadBatch.Source.MANUAL
        report = BatchIngestService().ingest(files, location_code=location, source=source, created_by=actor(request))
        batch = report.batch
        target = f"/coordinator/inbox/?batch={batch.id}&category=all"
        if request.headers.get("X-Requested-With") == "fetch":
            return JsonResponse({"batch_id": str(batch.id), "accepted": batch.accepted, "duplicates": batch.duplicates,
                                 "rejected": batch.rejected, "url": target})
        return redirect(target)
    clinic = request.session.get("clinic", "")
    return render(request, "processing/upload.html", {
        "clinics": DoctorsFacade.location_titles(), "clinic": clinic, "batches": ProcessingFacade.list_batches(8, clinic),
        "max_mb": settings.UPLOAD_MAX_BYTES // (1024 * 1024), "extensions": settings.UPLOAD_ALLOWED_EXTENSIONS,
        "inbox_dir": settings.PROTOCOL_INBOX_DIR,
    })


def batch_progress(request, pk):
    """Ход разбора пачки (JSON для «Входящих»: страница обновляет полосу, пока очередь не опустеет)."""
    return JsonResponse(ProcessingFacade.batch_progress(pk))


@require_POST
def batch_retry(request, pk):
    """«Повторить с Qwen» (модель не ответила) или «Повторить неудавшиеся»: протоколы снова в очередь."""
    what = "failed" if request.POST.get("what") == "failed" else "llm"
    count = ProcessingFacade.retry_batch(pk, what=what)
    messages.success(request, f"Снова в очереди: {count}" if count else "Повторять нечего")
    return redirect(f"/coordinator/inbox/?batch={pk}&category=all")


def result(request, pk):
    """Старая ссылка на результат разбора ведёт в карту протокола рабочего места."""
    return redirect(f"/coordinator/cases/{pk}/")


@require_POST
def pin(request, pk):
    """Закрепить фрагмент в «В фокусе» этого протокола (или открепить). Видно только этому врачу."""
    pinned = ProcessingFacade.toggle_pin(pk, int(request.POST["seq"]), focus_owner(request))
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({"pinned": pinned})
    target = request.POST.get("next") or f"/coordinator/cases/{pk}/"
    return redirect(target if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()})
                    else f"/coordinator/cases/{pk}/")


# ------------------------------------------------------------------ проверка качества аналитики
QUALITY_FILTERS = {
    "all": ("Все", lambda r, labels: True),
    "trigger": ("Сработал триггер", lambda r, labels: bool(r["rules"])),
    "no_trigger": ("Без триггера", lambda r, labels: not r["rules"] and not r["error"]),
    "review": ("Замечания к рекомендациям", lambda r, labels: r["audit_verdict"] in ("needs_review", "insufficient")),
    "gaps": ("Не вынесено или нет заключения", lambda r, labels: bool(r["not_in_conclusion"]) or r["has_conclusion"] is False),
    "errors": ("Ошибки", lambda r, labels: bool(r["error"])),
    "mismatch": ("Расхождения с разметкой", lambda r, labels: r["file"] in labels and sorted(r["rules"]) != labels[r["file"]]),
}


def quality(request):
    """Проверка качества аналитики: загрузить до QUALITY_MAX_PROTOCOLS протоколов и получить отчёт.
    Прогон «всухую»: пациенты, маршруты и уведомления не создаются, файлы удаляются после прогона."""
    if request.method == "POST":
        files = [IncomingFile(name=f.name, data=f.read()) for f in request.FILES.getlist("files")]
        labels = request.FILES.get("labels")
        run = QualityRunService().start(files, labels=labels.read() if labels else None,
                                        title=request.POST.get("title", ""), created_by=actor(request))
        if run.status == QualityRun.Status.FAILED:
            messages.error(request, run.error)
            return redirect("processing:quality")
        return redirect("processing:quality_run", pk=run.pk)
    return render(request, "processing/quality.html", {
        "runs": QualityRun.objects.defer("rows")[:12], "limit": settings.QUALITY_MAX_PROTOCOLS,
        "extensions": settings.UPLOAD_ALLOWED_EXTENSIONS,
    })


def quality_run(request, pk):
    run = get_object_or_404(QualityRun, pk=pk)
    code = request.GET.get("show", "all") if request.GET.get("show") in QUALITY_FILTERS else "all"
    labels = run.labels or {}
    tabs = [{"code": c, "title": t, "count": sum(1 for r in run.rows if f(r, labels))}
            for c, (t, f) in QUALITY_FILTERS.items() if c != "mismatch" or labels]
    rows = [r for r in run.rows if QUALITY_FILTERS[code][1](r, labels)]
    for r in rows:
        r["expected"] = labels.get(r["file"])
    s = run.summary or {}
    return render(request, "processing/quality_run.html", {
        "run": run, "s": s, "rows": rows, "tabs": tabs, "show": code, "labelled": s.get("labelled"),
        "progress": int(100 * run.processed / run.files_total) if run.files_total else 0,
        "rule_max": max([n for _, n in s.get("rules", [])] or [1]),
        "finding_max": max([n for _, n in s.get("findings", [])] or [1]),
        "evidence_css": mark_safe(evidence_css()),
    })


def quality_csv(request, pk):
    """Отчёт для Excel. Колонка expected_rules: разметка эксперта или пустая — как шаблон для неё."""
    run = get_object_or_404(QualityRun, pk=pk)
    response = HttpResponse("\ufeff" + rows_to_csv(run.rows, run.labels), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="quality-{run.created_at:%Y%m%d-%H%M}.csv"'
    return response
