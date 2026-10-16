"""Deterministic preference-rule resolution for one catalog skill."""
from __future__ import annotations
from dataclasses import dataclass
from core.skill_planner.models import SkillCatalogRow
from core.skill_planner.preferences import PreferenceRule, SkillPreferenceProfile
_SPECIFICITY = {'tag': 1, 'category': 1, 'skill_id': 2}
_HARD_MODES = frozenset({'must_buy_if_safe', 'avoid'})

@dataclass(frozen=True, slots=True)
class EffectiveSkillRule:
    mode: str | None
    must_priority: int | None
    prefer_weight: float
    matched_rule_ordinals: tuple[int, ...]
    reasons: tuple[str, ...]

def resolve_effective_skill_rule(row: SkillCatalogRow, profile: SkillPreferenceProfile) -> EffectiveSkillRule:
    """Resolve profile rules for one row without changing planner behavior."""
    matches = [rule for rule in profile.rules if _matches(rule, row)]
    hard_rules = _highest_specificity([rule for rule in matches if rule.mode in _HARD_MODES])
    prefer_rules = _highest_specificity([rule for rule in matches if rule.mode == 'prefer'])
    mode, must_priority = _resolve_hard_mode(hard_rules)
    selected = tuple(sorted((*hard_rules, *prefer_rules), key=lambda rule: rule.ordinal))
    return EffectiveSkillRule(mode=mode, must_priority=must_priority, prefer_weight=sum((rule.weight for rule in prefer_rules)), matched_rule_ordinals=tuple((rule.ordinal for rule in selected)), reasons=tuple((_reason(rule) for rule in selected)))

def _matches(rule: PreferenceRule, row: SkillCatalogRow) -> bool:
    if rule.selector.kind == 'skill_id':
        return rule.selector.value == row.skill_id
    if rule.selector.kind == 'category':
        return rule.selector.value == row.type
    if rule.selector.kind == 'tag':
        return rule.selector.value in row.tags
    return False

def _highest_specificity(rules: list[PreferenceRule]) -> tuple[PreferenceRule, ...]:
    if not rules:
        return ()
    highest = max((_SPECIFICITY[rule.selector.kind] for rule in rules))
    return tuple((rule for rule in rules if _SPECIFICITY[rule.selector.kind] == highest))

def _resolve_hard_mode(rules: tuple[PreferenceRule, ...]) -> tuple[str | None, int | None]:
    modes = {rule.mode for rule in rules}
    if len(modes) > 1:
        raise ValueError('effective_preference_rule_conflict')
    if not rules:
        return (None, None)
    mode = rules[0].mode
    if mode == 'must_buy_if_safe':
        return (mode, max((rule.priority for rule in rules)))
    return (mode, None)

def _reason(rule: PreferenceRule) -> str:
    return f'rule:{rule.ordinal}:{rule.selector.kind}:{rule.selector.value}:{rule.mode}'
__all__ = ['EffectiveSkillRule', 'resolve_effective_skill_rule']
