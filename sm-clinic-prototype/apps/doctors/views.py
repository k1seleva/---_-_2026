"""Рабочее место врача. Экран приёма свёрстан как форма МИС 1С — это прототип доработок на стороне 1С:
баннер маршрута, баннер незавершённого маршрута и обязательный выбор тактики."""
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.patients.facade import PatientsFacade
from common import clock
from common.identity import focus_owner

from .models import Appointment, Doctor, Specialty, VisitOutcome
from .services.booking import BookingError, BookingService, PrescriptionInput, VisitCompletion, VisitService
from .services.context import build_visit_context

# Врач, вошедший под своей учётной записью (gateway/staff_auth.py кладёт его id в сессию).
DOCTOR_KEY = "doctor_id"


def _own_doctor(request) -> str:
    return request.session.get(DOCTOR_KEY, "")


def _foreign(request, doctor_id) -> bool:
    """Чужое расписание или чужой приём. Суперпользователь без карточки врача видит всех (демо)."""
    own = _own_doctor(request)
    return bool(own) and own != str(doctor_id)


def choose(request):
    if own := _own_doctor(request):
        return redirect("doctors:schedule", pk=own)
    return render(request, "doctors/choose.html", {"doctors": Doctor.objects.prefetch_related("specialties", "locations")})


def schedule(request, pk):
    if _foreign(request, pk):
        return redirect("doctors:schedule", pk=_own_doctor(request))
    doctor = get_object_or_404(Doctor, pk=pk)
    appointments = list(Appointment.objects.select_related("slot__location", "slot__specialty")
                        .filter(slot__doctor=doctor).order_by("slot__starts_at"))
    names = PatientsFacade.names({a.patient_id for a in appointments})
    upcoming = doctor.slots.filter(starts_at__gte=clock.now()).select_related("location", "specialty")[:12]
    return render(request, "doctors/schedule.html", {
        "doctor": doctor, "appointments": [(a, names.get(str(a.patient_id), "—")) for a in appointments],
        "upcoming": upcoming,
    })


def appointment(request, pk):
    appt = get_object_or_404(Appointment.objects.select_related("slot__doctor", "slot__location", "slot__specialty"), pk=pk)
    if _foreign(request, appt.slot.doctor_id):
        messages.error(request, "Это приём другого врача")
        return redirect("doctors:choose")
    return render(request, "doctors/appointment.html", {
        "appt": appt, "ctx": build_visit_context(appt, owner=focus_owner(request)), "patient": PatientsFacade.get_display(appt.patient_id),
        "tactics": VisitOutcome.Tactic.choices, "specialties": Specialty.objects.all(),
        "outcome": getattr(appt, "outcome", None),
    })


@require_POST
def complete(request, pk):
    appt = get_object_or_404(Appointment.objects.select_related("slot"), pk=pk)
    if _foreign(request, appt.slot.doctor_id):
        messages.error(request, "Это приём другого врача")
        return redirect("doctors:choose")
    prescriptions = []
    for i in range(3):
        title = request.POST.get(f"p{i}_title", "").strip()
        if title:
            due = request.POST.get(f"p{i}_due")
            prescriptions.append(PrescriptionInput(kind=request.POST.get(f"p{i}_kind", "diagnostics"), title=title,
                                                   specialty_code=request.POST.get(f"p{i}_specialty", ""),
                                                   due_in_days=int(due) if due else None))
    try:
        VisitService().complete(appt, appt.slot.doctor, VisitCompletion(
            tactic=request.POST.get("tactic", ""), prescriptions=prescriptions,
            next_specialty_code=request.POST.get("next_specialty", ""),
            agrees_with_ai_route=request.POST.get("agrees") == "on",
            disagreement_reason=request.POST.get("disagreement_reason", ""), comment=request.POST.get("comment", "")))
    except BookingError as exc:
        messages.error(request, str(exc))
        return redirect("doctors:appointment", pk=pk)
    messages.success(request, "Приём завершён, маршрут пациента обновлён")
    return redirect("doctors:appointment", pk=pk)


@require_POST
def no_show(request, pk):
    appt = get_object_or_404(Appointment.objects.select_related("slot"), pk=pk)
    if _foreign(request, appt.slot.doctor_id):
        messages.error(request, "Это приём другого врача")
        return redirect("doctors:choose")
    BookingService().mark_no_show(appt)
    messages.success(request, "Отмечена неявка: пациенту уйдёт предложение перезаписаться")
    return redirect("doctors:appointment", pk=pk)
