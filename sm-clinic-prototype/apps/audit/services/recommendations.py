"""
Нормализация того, что рекомендовал врач, в единый список пунктов.

Источники:
* протокол исследования — рекомендации диагноста («Рекомендовано: консультация гинеколога»);
* приём врача — назначения (консультации, обследования), направление в другой профиль.

«Лечащий врач», «профильный специалист» без указания профиля — расплывчатая рекомендация (vague):
она не закрывает показание к конкретному специалисту, но учитывается в пояснении координатору.
"""
import re
from dataclasses import asdict, dataclass

VAGUE_RE = re.compile(
    r"лечащ\w*\s+врач\w*|наблюдающ\w*\s+врач\w*|профильн\w*\s+(специалист|врач)\w*|"
    r"консультаци\w*\s+(специалиста|врача)(?![\s-]*[а-я]*(лог|хирург|терапевт|педиатр))|своему\s+врачу",
    re.IGNORECASE,
)
# Подпись блока без содержания: «Рекомендовано:», «Назначенные услуги».
HEADING_ONLY_RE = re.compile(r"^\W*(Рекомендован\w*|Рекомендаци\w*|Назначени\w*|Назначенные\s+услуги)\W*$", re.IGNORECASE)
INTERVAL_RE = re.compile(r"(\d+)\s*(дн|нед|мес|год|лет)", re.IGNORECASE)
INTERVAL_DAYS = {"дн": 1, "нед": 7, "мес": 30, "год": 365, "лет": 365}


@dataclass
class RecommendationItem:
    source: str               # protocol | visit
    text: str
    kind: str = "consultation"
    specialty_code: str = ""
    service: str = ""
    interval_days: int | None = None
    vague: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def _interval(text: str) -> int | None:
    m = INTERVAL_RE.search(text or "")
    return int(m.group(1)) * INTERVAL_DAYS[m.group(2).lower()] if m else None


def from_protocol(recommendations: list[dict], segments: list[dict]) -> list[RecommendationItem]:
    """recommendations — пункты экстрактора; segments — размеченные фрагменты-рекомендации
    (дословный текст: из него берём расплывчатые формулировки, которые экстрактор не превращает в пункт)."""
    items = [
        RecommendationItem(source="protocol", text=r.get("text", ""), kind=r.get("kind") or "consultation",
                           specialty_code=r.get("specialty_code") or "", service=r.get("service") or "",
                           interval_days=r.get("interval_days"))
        for r in recommendations
    ]
    known = {i.specialty_code for i in items if i.specialty_code}
    for seg in segments:
        if seg.get("kind") != "recommendation":
            continue
        text = seg.get("text", "")
        for code in (seg.get("attributes") or {}).get("specialties", []):
            if code not in known:
                known.add(code)
                items.append(RecommendationItem(source="protocol", text=text, specialty_code=code, interval_days=_interval(text)))
        if VAGUE_RE.search(text):
            items.append(RecommendationItem(source="protocol", text=text, vague=True, interval_days=_interval(text)))
        elif not (seg.get("attributes") or {}).get("specialties") and HEADING_ONLY_RE.sub("", text).strip():
            # Пункт без профиля специалиста («ТАБ узла», «биопсия») — возможное обследование: показание
            # к обследованию сверяется с его дословным текстом, чтобы не заявить «пропущено» зря.
            items.append(RecommendationItem(source="protocol", text=text, kind="diagnostics", interval_days=_interval(text)))
    return items


def from_visit(payload: dict) -> list[RecommendationItem]:
    items = [
        RecommendationItem(source="visit", text=p.get("title", ""), kind=p.get("kind") or "consultation",
                           specialty_code=p.get("specialty_code") or "", service=p.get("service_code") or p.get("title", ""),
                           interval_days=p.get("due_in_days"))
        for p in payload.get("prescriptions", [])
    ]
    if payload.get("next_specialty_code"):
        items.append(RecommendationItem(source="visit", text="Направление в другой профиль",
                                        specialty_code=payload["next_specialty_code"]))
    return items
