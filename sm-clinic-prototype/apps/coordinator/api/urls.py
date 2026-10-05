from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import AdviceReviewViewSet, AnalyticsView, CoordinatorTaskViewSet, RouteReviewViewSet

router = DefaultRouter()
router.register("tasks", CoordinatorTaskViewSet, basename="coordinator-task")
router.register("reviews", RouteReviewViewSet, basename="coordinator-review")
router.register("advice-reviews", AdviceReviewViewSet, basename="coordinator-advice-review")
urlpatterns = router.urls + [path("analytics/", AnalyticsView.as_view(), name="coordinator-analytics")]
