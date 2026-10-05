"""Матрица маршрутизации: детерминированное решение «находка -> маршрут».

Матрица хранится в БД (TriggerRule) — новый триггер добавляется без изменения кода.
"""
from dataclasses import dataclass, field

from common import labels
from common.conditions import conditions_met

from ..models import TriggerRule


@dataclass
class RuleMatch:
    rule: TriggerRule
    finding: dict
    also_found: list = field(default_factory=list)

    @property
    def explanation(self) -> str:
        """Человекочитаемое основание: какое правило матрицы, какой версии, по какой находке и при каком условии.
        Код правила хранится отдельно (trigger_code, rule_version) — здесь только текст для людей."""
        finding = self.finding.get("label") or labels.title(self.finding.get("code", ""), "finding")
        condition = f", условие: {labels.conditions_text(self.rule.conditions)}" if self.rule.conditions else ""
        return f"Правило «{self.rule.title}», версия {self.rule.version}: {finding}{condition}"


class RoutingMatrix:
    def __init__(self, rules: list[TriggerRule] | None = None) -> None:
        self.rules = rules if rules is not None else list(
            TriggerRule.objects.filter(is_active=True).select_related("template")
        )

    @staticmethod
    def _conditions_met(rule: TriggerRule, finding: dict) -> bool:
        return conditions_met(rule.conditions, finding.get("attributes") or {})

    def match(self, findings: list[dict]) -> list[RuleMatch]:
        """Находки с отрицанием не запускают маршрут никогда.
        В одной клинической группе — один маршрут по самому приоритетному правилу,
        остальные находки группы сохраняются как сопутствующие (видны врачу)."""
        matches: dict[str, RuleMatch] = {}
        for finding in findings:
            if finding.get("negated"):
                continue
            for rule in self.rules:
                if rule.finding_code != finding.get("code") or not self._conditions_met(rule, finding):
                    continue
                key = rule.route_group or rule.template.code
                current = matches.get(key)
                if current is None:
                    matches[key] = RuleMatch(rule=rule, finding=finding)
                elif rule.priority < current.rule.priority:
                    matches[key] = RuleMatch(rule=rule, finding=finding,
                                             also_found=current.also_found + [current.rule.title])
                else:
                    current.also_found.append(rule.title)
        return sorted(matches.values(), key=lambda m: m.rule.priority)

    def explain_non_triggers(self, findings: list[dict]) -> list[str]:
        """Объяснение для нормы: почему триггер не сработал."""
        reasons = []
        for f in findings:
            if f.get("negated"):
                reasons.append(f"{f.get('label')}: упомянуто с отрицанием («{f.get('evidence_quote', '')[:80]}»)")
            elif not any(r.finding_code == f.get("code") and self._conditions_met(r, f) for r in self.rules):
                reasons.append(f"{f.get('label')}: нет правила в матрице или не выполнены пороги")
        return reasons
