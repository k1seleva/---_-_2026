"""Снимок словарей (находки, специальности) для экстракторов.

Экстракторы не ходят в ORM напрямую: получают неизменяемый снимок.
Так их легко тестировать и переносить в отдельный ML-сервис.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class FindingPattern:
    code: str
    title: str
    patterns: tuple[re.Pattern, ...]
    exclude: tuple[re.Pattern, ...] = ()
    study_types: tuple[str, ...] = ()
    severity: str = "routine"
    version: int = 1

    def rule_id(self, pattern_index: int | None = None) -> str:
        """Ссылка на правило для объяснимости: «dictionary:gallstones@v2#1» (шаблон №1 версии 2)."""
        suffix = f"#{pattern_index + 1}" if pattern_index is not None else ""
        return f"dictionary:{self.code}@v{self.version}{suffix}"


@dataclass(frozen=True)
class Dictionary:
    findings: tuple[FindingPattern, ...]
    specialties: tuple[tuple[re.Pattern, str], ...]
    version: str = "empty"
    raw: dict = field(default_factory=dict, compare=False)

    def finding_codes_for_prompt(self) -> str:
        return "\n".join(f"- {f.code}: {f.title}" for f in self.findings)

    def specialty_codes_for_prompt(self) -> str:
        return ", ".join(sorted({code for _, code in self.specialties}))


def _compile(items) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in items or ())


def load_dictionary() -> Dictionary:
    from apps.processing.models import FindingDefinition, SpecialtyAlias

    defs = list(FindingDefinition.objects.filter(is_active=True))
    aliases = list(SpecialtyAlias.objects.all())
    raw = {
        "findings": [[d.code, d.version, d.patterns, d.exclude_patterns] for d in defs],
        "aliases": [[a.pattern, a.specialty_code] for a in aliases],
    }
    version = hashlib.sha1(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:10]
    return Dictionary(
        findings=tuple(
            FindingPattern(
                code=d.code, title=d.title, patterns=_compile(d.patterns), exclude=_compile(d.exclude_patterns),
                study_types=tuple(d.study_types or ()), severity=d.severity, version=d.version,
            )
            for d in defs
        ),
        specialties=tuple((re.compile(a.pattern, re.IGNORECASE), a.specialty_code) for a in aliases),
        version=version,
        raw=raw,
    )
