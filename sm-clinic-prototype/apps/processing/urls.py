from django.urls import path

from . import views

app_name = "processing"
urlpatterns = [
    path("", views.upload, name="upload"),
    path("<uuid:pk>/", views.result, name="result"),
    path("<uuid:pk>/pin/", views.pin, name="pin"),
    path("batches/<uuid:pk>/progress/", views.batch_progress, name="batch_progress"),
    path("batches/<uuid:pk>/retry/", views.batch_retry, name="batch_retry"),
    path("quality/", views.quality, name="quality"),
    path("quality/<uuid:pk>/", views.quality_run, name="quality_run"),
    path("quality/<uuid:pk>/csv/", views.quality_csv, name="quality_csv"),
]
