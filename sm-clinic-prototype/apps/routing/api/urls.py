from rest_framework.routers import DefaultRouter

from .views import PatientRouteViewSet, TriggerRuleViewSet

router = DefaultRouter()
router.register("routes", PatientRouteViewSet, basename="routing-route")
router.register("rules", TriggerRuleViewSet, basename="routing-rule")
urlpatterns = router.urls
