from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from apps.doctors.facade import DoctorsFacade
from apps.processing.facade import ProcessingFacade
from apps.routing.facade import RoutingFacade

from common import clock

from ..models import Patient
from ..services.auth import PatientAuthService
from ..services.booking import PatientBookingError, PatientBookingService
from ..services.preferences import PreferenceService
from .serializers import (NotificationPreferenceSerializer, NotificationSerializer, PatientBookSerializer,
                          PatientSerializer, PushSubscriptionSerializer, RouteActionSerializer)


class PatientAccess(BasePermission):
    """Пациент видит только себя (вход в кабинет по номеру карты и паролю); сотрудник клиники (is_staff) — любого.
    Списка всех пациентов пациенту не отдаём."""

    @staticmethod
    def _staff(request) -> bool:
        return bool(request.user and request.user.is_authenticated and request.user.is_staff)

    def has_permission(self, request, view):
        if self._staff(request):
            return True
        return view.action != "list" and bool(PatientAuthService.current(request))

    def has_object_permission(self, request, view, obj):
        return self._staff(request) or PatientAuthService.current(request) == str(obj.pk)


class PatientViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Личный кабинет пациента. Доступ только после входа (сессия кабинета) и только к своему {id};
    сотрудник клиники с правами персонала видит любого пациента. В пилоте вместо сессии — токен ЛК СМ-Клиники:
    GET  /api/v1/patients/{id}/routes/                    — мои маршруты
    GET  /api/v1/patients/{id}/notifications/             — уведомления для колокольчика (+ число непрочитанных)
    POST /api/v1/patients/{id}/notifications/read/        — прочитать одно или все
    GET|PUT /api/v1/patients/{id}/preferences/            — каналы связи по типам событий
    POST /api/v1/patients/{id}/push-subscriptions/        — подписка на push
    GET  /api/v1/patients/{id}/results/                   — исследования (выжимка)
    GET  /api/v1/patients/{id}/results/{doc_id}/full/     — полный текст исходного протокола
    GET  /api/v1/patients/{id}/steps/{step_id}/slots/     — слоты строго под этап маршрута
    POST /api/v1/patients/{id}/book/                      — запись на этап маршрута
    POST /api/v1/patients/{id}/route-action/              — «уже обратился», «не планирую», «перезвоните»
    """

    queryset = Patient.objects.filter(is_anonymous=False)
    serializer_class = PatientSerializer
    permission_classes = [PatientAccess]

    @action(detail=True, methods=["get"])
    def routes(self, request, pk=None):
        return Response(RoutingFacade.list_patient_routes(self.get_object().id))

    @action(detail=True, methods=["get"])
    def notifications(self, request, pk=None):
        """Список для колокольчика. Чтение списка уведомления прочитанными не делает."""
        patient = self.get_object()
        qs = patient.notifications.filter(channel="lk")
        return Response({"unread": qs.filter(read_at__isnull=True).count(),
                         "items": NotificationSerializer(qs[:50], many=True).data})

    @action(detail=True, methods=["post"], url_path="notifications/read")
    def notifications_read(self, request, pk=None):
        """Прочитать одно ({"id": …}) или все уведомления."""
        qs = self.get_object().notifications.filter(channel="lk", read_at__isnull=True)
        if request.data.get("id"):
            qs = qs.filter(pk=request.data["id"])
        return Response({"marked": qs.update(read_at=clock.now())})

    @action(detail=True, methods=["get", "put"])
    def preferences(self, request, pk=None):
        """Каналы по типам событий. PUT: [{"group": "results", "push": true, "sms": true, "call": false}, …]."""
        patient = self.get_object()
        service = PreferenceService()
        if request.method == "PUT":
            rows = NotificationPreferenceSerializer(data=request.data, many=True)
            rows.is_valid(raise_exception=True)
            current = {r.group: set(r.channels) for r in service.matrix(patient)}
            for row in rows.validated_data:
                current[row["group"]] = {c for c in ("push", "sms", "call") if row[c]}
            service.save(patient, current)
        return Response({"rows": [r.__dict__ for r in service.matrix(patient)],
                         "sms_recommended": service.sms_recommended(patient), "lk_always_on": True})

    @action(detail=True, methods=["post"], url_path="push-subscriptions")
    def push_subscriptions(self, request, pk=None):
        serializer = PushSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(patient=self.get_object())
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def results(self, request, pk=None):
        patient = self.get_object()
        docs = ProcessingFacade.list_patient_documents(patient.id)
        return Response([
            {**d, "summary": (ProcessingFacade.get_document(d["id"]) or {}).get("summary_for_patient", "")} for d in docs
        ])

    @action(detail=True, methods=["get"], url_path=r"results/(?P<doc_id>[^/.]+)/full")
    def result_full(self, request, pk=None, doc_id=None):
        patient = self.get_object()
        doc = ProcessingFacade.get_document(doc_id)
        if not doc or doc["patient_id"] != str(patient.id):
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response({**doc, "text": ProcessingFacade.get_full_text(doc_id)})

    @action(detail=True, methods=["get"], url_path=r"steps/(?P<step_id>[^/.]+)/slots")
    def step_slots(self, request, pk=None, step_id=None):
        try:
            step, slots = PatientBookingService().available_slots(self.get_object(), step_id)
        except PatientBookingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response({"step": step, "slots": slots})

    @action(detail=True, methods=["post"])
    def book(self, request, pk=None):
        data = PatientBookSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            appointment = PatientBookingService().book(self.get_object(), data.validated_data["route_step_id"],
                                                       data.validated_data["slot_id"])
        except PatientBookingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(appointment, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="route-action")
    def route_action(self, request, pk=None):
        data = RouteActionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        PatientBookingService().route_action(self.get_object(), **data.validated_data)
        return Response({"status": "ok"})

    @action(detail=True, methods=["get"])
    def appointments(self, request, pk=None):
        return Response(DoctorsFacade.patient_appointments(self.get_object().id))
