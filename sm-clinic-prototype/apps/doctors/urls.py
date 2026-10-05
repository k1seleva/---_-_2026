from django.urls import path

from . import views

app_name = "doctors"
urlpatterns = [
    path("", views.choose, name="choose"),
    path("<uuid:pk>/", views.schedule, name="schedule"),
    path("appointment/<uuid:pk>/", views.appointment, name="appointment"),
    path("appointment/<uuid:pk>/complete/", views.complete, name="complete"),
    path("appointment/<uuid:pk>/no-show/", views.no_show, name="no_show"),
]
