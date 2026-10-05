from django.db import transaction

from common import clock

from .models import Notification, Patient


def _dto(p: Patient) -> dict:
    return {"id": str(p.id), "display_name": p.display_name, "external_mis_id": p.external_mis_id or "",
            "birth_year": p.birth_year, "sex": p.sex, "is_anonymous": p.is_anonymous,
            "placeholder_code": p.placeholder_code, "card_hint": p.card_hint, "location_code": p.location_code,
            "hints": p.hints, "merged_into": str(p.merged_into or ""), "created_at": p.created_at}


class PatientsFacade:
    @staticmethod
    def get_display(patient_id) -> dict | None:
        p = Patient.objects.filter(pk=patient_id).first() if patient_id else None
        return _dto(p) if p else None

    @staticmethod
    def list_all(*, include_anonymous: bool = False) -> list[dict]:
        qs = Patient.objects.order_by("display_name")
        if not include_anonymous:
            qs = qs.filter(is_anonymous=False)
        return [_dto(p) for p in qs]

    @staticmethod
    def names(patient_ids) -> dict[str, str]:
        rows = Patient.objects.filter(pk__in=[i for i in patient_ids if i]).values_list("id", "display_name", "is_anonymous",
                                                                                     "placeholder_code")
        return {str(pk): (f"Обезличенный пациент {code}" if anon else name) for pk, name, anon, code in rows}

    @staticmethod
    def search(query: str, limit: int = 20) -> list[dict]:
        from django.db.models import Q

        qs = Patient.objects.filter(is_anonymous=False).filter(
            Q(display_name__icontains=query) | Q(external_mis_id__icontains=query))
        return [_dto(p) for p in qs[:limit]]

    @staticmethod
    def find_by_mis_id(external_mis_id: str) -> str | None:
        """Пациент по номеру карты (точное совпадение без учёта регистра и пробелов)."""
        key = " ".join(str(external_mis_id).split())
        if not key:
            return None
        pk = Patient.objects.filter(external_mis_id__iexact=key, is_anonymous=False).values_list("id", flat=True).first()
        return str(pk) if pk else None

    @staticmethod
    def ensure_by_mis_id(external_mis_id: str, *, display_name: str = "", birth_year=None, sex: str = "") -> str:
        """Пациент из события МИС: находим по номеру карты или создаём запись."""
        if existing := PatientsFacade.find_by_mis_id(external_mis_id):
            return existing
        patient = Patient.objects.create(
            external_mis_id=" ".join(str(external_mis_id).split()),
            display_name=display_name or f"Пациент {external_mis_id}", birth_year=birth_year, sex=sex)
        return str(patient.id)

    @staticmethod
    @transaction.atomic
    def ensure_placeholder(*, card_number: str = "", location_code: str = "", hint: dict | None = None) -> str:
        """Обезличенная карточка для протокола без определённого пациента.
        Протоколы с тем же нераспознанным номером карты собираются в одну карточку."""
        patient = None
        if card_number:
            patient = Patient.objects.select_for_update().filter(is_anonymous=True, merged_into__isnull=True,
                                                                 card_hint__iexact=card_number).first()
        if patient is None:
            number = Patient.objects.filter(is_anonymous=True).count() + 1
            code = f"ОП-{number:04d}"
            patient = Patient.objects.create(is_anonymous=True, placeholder_code=code, display_name=f"Обезличенный пациент {code}",
                                             card_hint=card_number[:64], location_code=location_code)
        if hint:
            patient.hints = [*patient.hints, hint][-20:]
            patient.save(update_fields=["hints", "updated_at"])
        return str(patient.id)

    @staticmethod
    def list_placeholders(*, open_only: bool = True) -> list[dict]:
        qs = Patient.objects.filter(is_anonymous=True).order_by("-created_at")
        if open_only:
            qs = qs.filter(merged_into__isnull=True)
        return [_dto(p) for p in qs]

    @staticmethod
    def mark_merged(placeholder_id, patient_id) -> None:
        Patient.objects.filter(pk=placeholder_id, is_anonymous=True).update(merged_into=patient_id, merged_at=clock.now())

    @staticmethod
    def notification_stats(patient_ids=None) -> dict:
        qs = Notification.objects.all()
        if patient_ids is not None:
            qs = qs.filter(patient_id__in=list(patient_ids))
        return {"sent": qs.filter(status="sent").count(), "suppressed": qs.filter(status="suppressed").count()}
