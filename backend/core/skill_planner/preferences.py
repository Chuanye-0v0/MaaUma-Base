"""Strict, immutable skill preference profiles for offline planning."""
from __future__ import annotations
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
PROFILE_SCHEMA = 'uma.skill_preference_profile.v1'
VALID_BASE_PROFILES = frozenset({'parent', 'race'})
VALID_FILLER_POLICIES = frozenset({'safe_useful_only', 'spend_all_safe'})
VALID_RULE_MODES = frozenset({'must_buy_if_safe', 'prefer', 'avoid'})
VALID_SELECTOR_KINDS = frozenset({'skill_id', 'category', 'tag'})
_PROFILE_KEYS = frozenset({'schema_version', 'profile_id', 'base_profile', 'reserve_skill_pt', 'filler_policy', 'max_batch_size', 'rules'})
_RULE_REQUIRED_KEYS = frozenset({'selector', 'mode', 'priority'})
_RULE_OPTIONAL_KEYS = frozenset({'weight'})
_SELECTOR_KEYS = frozenset({'kind', 'value'})

@dataclass(frozen=True, slots=True)
class PreferenceSelector:
    kind: str
    value: str

@dataclass(frozen=True, slots=True)
class PreferenceRule:
    selector: PreferenceSelector
    mode: str
    priority: int
    weight: float
    ordinal: int

@dataclass(frozen=True, slots=True)
class SkillPreferenceProfile:
    schema_version: str
    profile_id: str
    base_profile: str
    reserve_skill_pt: int
    filler_policy: str
    max_batch_size: int
    rules: tuple[PreferenceRule, ...]
    warnings: tuple[str, ...]
    profile_sha256: str

def parse_skill_preference_profile(data: Mapping[str, object], *, legacy_skill_priority: Sequence[str]=()) -> SkillPreferenceProfile:
    """Validate, migrate, and hash one preference profile without I/O."""
    if not isinstance(data, Mapping):
        raise ValueError('preference_profile_not_object')
    _require_exact_keys(data, _PROFILE_KEYS, 'preference_profile_fields_invalid')
    schema_version = _require_nonempty_string(data['schema_version'], 'schema_version')
    if schema_version != PROFILE_SCHEMA:
        raise ValueError('preference_profile_schema_invalid')
    profile_id = _require_nonempty_string(data['profile_id'], 'profile_id')
    base_profile = _require_nonempty_string(data['base_profile'], 'base_profile')
    if base_profile not in VALID_BASE_PROFILES:
        raise ValueError('base_profile_invalid')
    reserve_skill_pt = _require_int(data['reserve_skill_pt'], 'reserve_skill_pt')
    if reserve_skill_pt < 0:
        raise ValueError('reserve_skill_pt_invalid')
    filler_policy = _require_nonempty_string(data['filler_policy'], 'filler_policy')
    if filler_policy not in VALID_FILLER_POLICIES:
        raise ValueError('filler_policy_invalid')
    max_batch_size = _require_int(data['max_batch_size'], 'max_batch_size')
    if not 1 <= max_batch_size <= 48:
        raise ValueError('max_batch_size_invalid')
    raw_rules = data['rules']
    if type(raw_rules) is not list:
        raise ValueError('preference_rules_invalid')
    rules = [_parse_rule(raw_rule, ordinal) for ordinal, raw_rule in enumerate(raw_rules)]
    legacy_values = _parse_legacy_skill_priority(legacy_skill_priority)
    for legacy_value in legacy_values:
        rules.append(PreferenceRule(selector=PreferenceSelector(kind='tag', value=legacy_value), mode='prefer', priority=0, weight=1.0, ordinal=len(rules)))
    _validate_rule_conflicts(rules)
    warnings = ('legacy_skill_priority_migrated',) if legacy_values else ()
    canonical = _canonical_profile_material(schema_version=schema_version, profile_id=profile_id, base_profile=base_profile, reserve_skill_pt=reserve_skill_pt, filler_policy=filler_policy, max_batch_size=max_batch_size, rules=tuple(rules), warnings=warnings)
    return SkillPreferenceProfile(schema_version=schema_version, profile_id=profile_id, base_profile=base_profile, reserve_skill_pt=reserve_skill_pt, filler_policy=filler_policy, max_batch_size=max_batch_size, rules=tuple(rules), warnings=warnings, profile_sha256=_canonical_sha(canonical))

def load_skill_preference_profile(path: str | Path, *, legacy_skill_priority: Sequence[str]=()) -> SkillPreferenceProfile:
    """Read a profile JSON file while rejecting duplicate object keys."""
    try:
        text = Path(path).read_text(encoding='utf-8')
    except (OSError, TypeError) as error:
        raise ValueError('preference_profile_read_failed') from error
    try:
        payload = json.loads(text, object_pairs_hook=_no_duplicate_json_object)
    except json.JSONDecodeError as error:
        raise ValueError('preference_profile_json_invalid') from error
    return parse_skill_preference_profile(payload, legacy_skill_priority=legacy_skill_priority)

def default_skill_preference_profile(base_profile: str='parent') -> SkillPreferenceProfile:
    """Return the deterministic, empty default profile for one base mode."""
    if type(base_profile) is not str or base_profile not in VALID_BASE_PROFILES:
        raise ValueError('base_profile_invalid')
    return parse_skill_preference_profile({'schema_version': PROFILE_SCHEMA, 'profile_id': f'{base_profile}-default', 'base_profile': base_profile, 'reserve_skill_pt': 0, 'filler_policy': 'safe_useful_only', 'max_batch_size': 5, 'rules': []})

def default_terminal_skill_preference_profile(base_profile: str='parent') -> SkillPreferenceProfile:
    """Return the terminal profile that spends down all safe, useful skills."""
    if type(base_profile) is not str or base_profile not in VALID_BASE_PROFILES:
        raise ValueError('base_profile_invalid')
    return parse_skill_preference_profile({'schema_version': PROFILE_SCHEMA, 'profile_id': f'{base_profile}-terminal-spenddown', 'base_profile': base_profile, 'reserve_skill_pt': 0, 'filler_policy': 'spend_all_safe', 'max_batch_size': 12, 'rules': []})

def _parse_rule(raw_rule: object, ordinal: int) -> PreferenceRule:
    if not isinstance(raw_rule, Mapping):
        raise ValueError('preference_rule_invalid')
    keys = set(raw_rule)
    if not _RULE_REQUIRED_KEYS.issubset(keys) or not keys.issubset(_RULE_REQUIRED_KEYS | _RULE_OPTIONAL_KEYS):
        raise ValueError('preference_rule_fields_invalid')
    raw_selector = raw_rule['selector']
    if not isinstance(raw_selector, Mapping):
        raise ValueError('preference_selector_invalid')
    _require_exact_keys(raw_selector, _SELECTOR_KEYS, 'preference_selector_fields_invalid')
    kind = _require_nonempty_string(raw_selector['kind'], 'selector_kind')
    value = _require_nonempty_string(raw_selector['value'], 'selector_value')
    if kind not in VALID_SELECTOR_KINDS:
        raise ValueError('selector_kind_invalid')
    mode = _require_nonempty_string(raw_rule['mode'], 'rule_mode')
    if mode not in VALID_RULE_MODES:
        raise ValueError('rule_mode_invalid')
    priority = _require_int(raw_rule['priority'], 'rule_priority')
    weight = _require_finite_number(raw_rule.get('weight', 0.0), 'rule_weight')
    return PreferenceRule(selector=PreferenceSelector(kind=kind, value=value), mode=mode, priority=priority, weight=weight, ordinal=ordinal)

def _parse_legacy_skill_priority(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError('legacy_skill_priority_invalid')
    parsed: list[str] = []
    for value in values:
        parsed.append(_require_nonempty_string(value, 'legacy_skill_priority_invalid'))
    return tuple(parsed)

def _validate_rule_conflicts(rules: Sequence[PreferenceRule]) -> None:
    seen_exact: set[tuple[str, str, str]] = set()
    hard_modes: dict[tuple[str, str], str] = {}
    for rule in rules:
        exact_key = (rule.selector.kind, rule.selector.value, rule.mode)
        if exact_key in seen_exact:
            raise ValueError('duplicate_preference_rule')
        seen_exact.add(exact_key)
        selector_key = (rule.selector.kind, rule.selector.value)
        prior_hard_mode = hard_modes.get(selector_key)
        if rule.mode in {'must_buy_if_safe', 'avoid'}:
            if prior_hard_mode is not None and prior_hard_mode != rule.mode:
                raise ValueError('preference_rule_conflict')
            hard_modes[selector_key] = rule.mode

def _canonical_profile_material(*, schema_version: str, profile_id: str, base_profile: str, reserve_skill_pt: int, filler_policy: str, max_batch_size: int, rules: tuple[PreferenceRule, ...], warnings: tuple[str, ...]) -> dict[str, object]:
    return {'schema_version': schema_version, 'profile_id': profile_id, 'base_profile': base_profile, 'reserve_skill_pt': reserve_skill_pt, 'filler_policy': filler_policy, 'max_batch_size': max_batch_size, 'rules': [{'selector': {'kind': rule.selector.kind, 'value': rule.selector.value}, 'mode': rule.mode, 'priority': rule.priority, 'weight': rule.weight, 'ordinal': rule.ordinal} for rule in rules], 'warnings': list(warnings)}

def _canonical_sha(value: Mapping[str, object]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()

def _no_duplicate_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result

def _require_exact_keys(data: Mapping[str, object], expected: frozenset[str], error: str) -> None:
    if set(data) != expected:
        raise ValueError(error)

def _require_nonempty_string(value: object, error: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(error)
    return value

def _require_int(value: object, error: str) -> int:
    if type(value) is not int:
        raise ValueError(error)
    return value

def _require_finite_number(value: object, error: str) -> float:
    if type(value) not in {int, float}:
        raise ValueError(error)
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(error)
    return number
__all__ = ['PROFILE_SCHEMA', 'PreferenceRule', 'PreferenceSelector', 'SkillPreferenceProfile', 'default_skill_preference_profile', 'default_terminal_skill_preference_profile', 'load_skill_preference_profile', 'parse_skill_preference_profile']
