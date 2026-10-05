from django.urls import path
from rest_framework.routers import DefaultRouter

from .batches import BatchDetailView, BatchUploadView
from .quality import QualityRunDetailView, QualityRunView
from .views import StudyDocumentViewSet

router = DefaultRouter()
router.register("documents", StudyDocumentViewSet, basename="processing-document")
urlpatterns = [
    path("batches/", BatchUploadView.as_view(), name="processing-batches"),
    path("batches/<uuid:batch_id>/", BatchDetailView.as_view(), name="processing-batch"),
    path("quality-runs/", QualityRunView.as_view(), name="processing-quality-runs"),
    path("quality-runs/<uuid:run_id>/", QualityRunDetailView.as_view(), name="processing-quality-run"),
    *router.urls,
]
