from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from common.identity import actor

from ..models import AdviceReview, CoordinatorTask, RouteReview
from ..services.advice import AdviceReviewError, AdviceReviewService, ReviewCommand
from ..services.analytics import AnalyticsService, EconomicAssumptions, economic_effect
from ..services.disputes import DisputeService
from .serializers import (AdviceReviewCreateSerializer, AdviceReviewSerializer, CoordinatorTaskSerializer,
                          OpenReviewSerializer, ResolveReviewSerializer, RouteReviewSerializer)


class CoordinatorTaskViewSet(mixins.ListModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    """GET/PATCH /api/v1/coordinator/tasks/ — рабочий список координатора (звонки, даты госпитализации)."""

    serializer_class = CoordinatorTaskSerializer

    def get_queryset(self):
        qs = CoordinatorTask.objects.all()
        if s := self.request.query_params.get("status"):
            qs = qs.filter(status=s)
        return qs


class RouteReviewViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    POST /api/v1/coordinator/reviews/               — открыть спор по маршруту
    GET  /api/v1/coordinator/reviews/compare/?route_id= — сравнение маркеров ИИ и маршрута врача
    POST /api/v1/coordinator/reviews/{id}/resolve/  — подтвердить / скорректировать маршрут
    """

    queryset = RouteReview.objects.all()
    serializer_class = RouteReviewSerializer

    def create(self, request):
        data = OpenReviewSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        review = DisputeService().open_review(data.validated_data["route_id"], source="coordinator",
                                              reason=data.validated_data["reason"])
        return Response(RouteReviewSerializer(review).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"])
    def compare(self, request):
        try:
            return Response(DisputeService().compare(request.query_params["route_id"]))
        except (KeyError, ValueError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        data = ResolveReviewSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        review = DisputeService().resolve(self.get_object(), user=request.user if request.user.is_authenticated else None,
                                          **data.validated_data)
        return Response(RouteReviewSerializer(review).data)


class AnalyticsView(APIView):
    """GET /api/v1/coordinator/analytics/ — воронка, закономерности, KPI, экономический эффект."""

    def get(self, request):
        service = AnalyticsService()
        if stage := request.query_params.get("stage"):
            return Response({"stage": stage, "patients": service.stage_patients(stage)})
        return Response({
            "funnel": service.funnel(), "kpis": service.kpis(), "by_trigger": service.by_trigger(),
            "by_location": service.by_location(), "economic_effect": economic_effect(EconomicAssumptions()),
        })


class AdviceReviewViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    GET  /api/v1/coordinator/advice-reviews/?document_id=&advice_id= — оценки советов ИИ-агента
    POST /api/v1/coordinator/advice-reviews/  {advice_id, verdict: accept|reject|correct, comment, corrected_route_code}
    GET  /api/v1/coordinator/advice-reviews/stats/ — принято / отклонено / скорректировано
    Оценка маршрут не меняет.
    """

    serializer_class = AdviceReviewSerializer

    def get_queryset(self):
        qs = AdviceReview.objects.all()
        for key in ("document_id", "advice_id", "verdict"):
            if value := self.request.query_params.get(key):
                qs = qs.filter(**{key: value})
        return qs

    def create(self, request, *args, **kwargs):
        data = AdviceReviewCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            review = AdviceReviewService().review(ReviewCommand(
                advice_id=str(data.validated_data["advice_id"]), user_id=actor(request),
                verdict=data.validated_data["verdict"], comment=data.validated_data.get("comment", ""),
                corrected_route_code=data.validated_data.get("corrected_route_code", "")))
        except AdviceReviewError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(AdviceReviewSerializer(review).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"])
    def stats(self, request):
        return Response(AdviceReviewService.stats())
