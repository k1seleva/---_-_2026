from rest_framework.routers import DefaultRouter

from .views import AppointmentViewSet, SlotViewSet

router = DefaultRouter()
router.register("slots", SlotViewSet, basename="doctors-slot")
router.register("appointments", AppointmentViewSet, basename="doctors-appointment")
urlpatterns = router.urls
