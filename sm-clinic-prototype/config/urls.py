from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from gateway import staff_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("gateway.urls")),
    # Веб-страницы прототипа (серверный рендеринг, фирменный стиль СМ-Клиники).
    path("", include("gateway.pages")),
    # Вход сотрудников: у координатора и врача свои страницы (пациент входит на /patient/).
    path("coordinator/login/", staff_views.login_view, {"role": "coordinator"}, name="coordinator_login"),
    path("doctor/login/", staff_views.login_view, {"role": "doctor"}, name="doctor_login"),
    path("staff/logout/", staff_views.logout_view, name="staff_logout"),
    path("processing/", include("apps.processing.urls")),
    path("patient/", include("apps.patients.urls")),
    path("doctor/", include("apps.doctors.urls")),
    path("coordinator/", include("apps.coordinator.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
