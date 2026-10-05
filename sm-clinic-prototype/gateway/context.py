"""Контекст рабочего пространства (BFF): меню со счётчиками, выбранная клиника, колокольчик, активный раздел."""
import logging

from apps.coordinator.facade import CoordinatorFacade
from apps.doctors.facade import DoctorsFacade
from apps.processing.facade import ProcessingFacade

from .staff_auth import ROLES, current_role

log = logging.getLogger(__name__)
WORKSPACE_PREFIXES = ("/coordinator", "/processing", "/doctor")
# Раздел меню по началу адреса: самый длинный подходящий префикс.
SECTIONS = [
    ("/coordinator/inbox", "inbox"), ("/coordinator/cases", "inbox"), ("/processing", "upload"),
    ("/processing/quality", "quality"),
    ("/coordinator/unmatched", "unmatched"), ("/coordinator/routes", "routes"), ("/coordinator/tasks", "tasks"),
    ("/coordinator/audits", "audits"), ("/doctor", "doctor"), ("/coordinator/analytics/clinics", "clinics"),
    ("/coordinator/analytics/features", "features"), ("/coordinator/analytics", "analytics"),
    ("/coordinator/stage", "analytics"), ("/coordinator/settings/tags", "tags"),
    ("/coordinator/settings/priorities", "priorities"), ("/coordinator/search", "home"), ("/coordinator", "home"),
]


def section_for(path: str) -> str:
    return max(((p, s) for p, s in SECTIONS if path.startswith(p)), key=lambda x: len(x[0]), default=("", ""))[1]


def workspace(request):
    if not request.path.startswith(WORKSPACE_PREFIXES):
        return {}
    try:
        role = current_role(request)
        base = {
            "ws_section": section_for(request.path),
            "ws_role": role,
            "ws_role_title": ROLES[role].title if role else "",
            "ws_user": (request.user.get_full_name() or request.user.username) if request.user.is_authenticated else "",
        }
        if role != "coordinator":
            # Врачу — только его приём: без входящих, колокольчика координатора и выбора клиники.
            return base
        location = request.session.get("clinic", "")
        return {
            **base,
            "ws_counts": CoordinatorFacade.workspace_counts(location),
            "ws_bell": CoordinatorFacade.bell_items(location),
            "ws_queue": ProcessingFacade.queue_summary(),
            "ws_clinic": location,
            "ws_clinics": DoctorsFacade.location_titles(),
        }
    except Exception:  # БД ещё не мигрирована
        log.exception("Контекст рабочего пространства недоступен")
        return {}
