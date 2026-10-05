from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.routing.facade import RoutingFacade

from ..models import Appointment, Doctor, ScheduleSlot
from ..services.booking import BookingError, BookingService, PrescriptionInput, SlotFinder, VisitCompletion, VisitService
from ..services.context import build_visit_context
from .serializers import AppointmentSerializer, BookSerializer, CompleteVisitSerializer, SlotSerializer


class SlotViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """GET /api/v1/doctors/slots/?specialty=gyn_surgeon&location=tekstilshchiki — ближайшие свободные слоты."""

    serializer_class = SlotSerializer

    def get_queryset(self):
        params = self.request.query_params
        if specialty := params.get("specialty"):
            return SlotFinder().find(specialty, limit=int(params.get("limit", 20)), location_code=params.get("location"))
        return ScheduleSlot.objects.filter(status=ScheduleSlot.Status.FREE).select_related("doctor", "location")[:50]


class AppointmentViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    POST /api/v1/doctors/appointments/                     — запись (координатор/врач)
    POST /api/v1/doctors/appointments/{id}/cancel/         — отмена
    POST /api/v1/doctors/appointments/{id}/no-show/        — отметка «неявка»
    POST /api/v1/doctors/appointments/{id}/complete/       — завершить приём (тактика обязательна)
    GET  /api/v1/doctors/appointments/{id}/context/        — результаты исследования + маршрут для врача
    """

    serializer_class = AppointmentSerializer

    def get_queryset(self):
        qs = Appointment.objects.select_related("slot__doctor", "slot__location")
        if doctor_id := self.request.query_params.get("doctor_id"):
            qs = qs.filter(slot__doctor_id=doctor_id)
        if patient_id := self.request.query_params.get("patient_id"):
            qs = qs.filter(patient_id=patient_id)
        return qs

    def create(self, request, *args, **kwargs):
        data = BookSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        route_id = None
        if step_id := v.get("route_step_id"):
            slot = ScheduleSlot.objects.filter(pk=v["slot_id"]).first()
            ok, reason = RoutingFacade.validate_booking(step_id, v["patient_id"], slot.specialty_id if slot else "")
            if not ok:
                return Response({"detail": reason}, status=status.HTTP_400_BAD_REQUEST)
            route_id = RoutingFacade.get_step(step_id)["route_id"]
        try:
            appointment = BookingService().book(slot_id=v["slot_id"], patient_id=v["patient_id"], route_id=route_id,
                                                route_step_id=v.get("route_step_id"), booked_via="coordinator")
        except BookingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(AppointmentSerializer(appointment).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        try:
            BookingService().cancel(self.get_object(), by=request.data.get("by", "doctor"))
        except BookingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"status": "cancelled"})

    @action(detail=True, methods=["post"], url_path="no-show")
    def no_show(self, request, pk=None):
        BookingService().mark_no_show(self.get_object())
        return Response({"status": "no_show"})

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        data = CompleteVisitSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        doctor = Doctor.objects.get(pk=v["doctor_id"])
        try:
            outcome = VisitService().complete(self.get_object(), doctor, VisitCompletion(
                tactic=v["tactic"], prescriptions=[PrescriptionInput(**p) for p in v["prescriptions"]],
                next_specialty_code=v["next_specialty_code"], agrees_with_ai_route=v["agrees_with_ai_route"],
                disagreement_reason=v["disagreement_reason"], comment=v["comment"],
            ))
        except BookingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"outcome_id": str(outcome.id), "tactic": outcome.tactic}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def context(self, request, pk=None):
        return Response(build_visit_context(self.get_object()))
