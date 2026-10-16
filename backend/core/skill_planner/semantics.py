from __future__ import annotations
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any
SEMANTIC_CATALOG_SCHEMA = 'uma.skill_semantic_catalog.v1'
APPROVED_SEMANTIC_STATUS = 'approved_cross_checked'

def _require_exact_keys(data: dict[str, Any], *, required: frozenset[str], optional: frozenset[str]=frozenset(), error: str) -> None:
    keys = frozenset(data)
    if not required <= keys or keys - required - optional:
        raise ValueError(error)

def _required_string(value: object, error: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(error)
    return value

def _string_tuple(value: object, error: str, *, allow_empty: bool) -> tuple[str, ...]:
    if type(value) is not list:
        raise ValueError(error)
    result: list[str] = []
    for item in value:
        text = _required_string(item, error)
        if text in result:
            raise ValueError(error)
        result.append(text)
    if not allow_empty and (not result):
        raise ValueError(error)
    return tuple(result)

def _finite_number(value: object, error: str, *, minimum: float | None=None) -> float:
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        raise ValueError(error)
    result = float(value)
    if minimum is not None and result < minimum:
        raise ValueError(error)
    return result

def _optional_finite_number(value: object, error: str, *, minimum: float | None=None) -> float | None:
    if value is None:
        return None
    return _finite_number(value, error, minimum=minimum)

@dataclass(frozen=True)
class SkillEffect:
    kind: str
    value: float
    duration_seconds: float | None
    cooldown_seconds: float | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'SkillEffect':
        if type(data) is not dict:
            raise ValueError('skill_effect_invalid')
        _require_exact_keys(data, required=frozenset({'kind', 'value'}), optional=frozenset({'duration_seconds', 'cooldown_seconds'}), error='skill_effect_keys_invalid')
        return cls(kind=_required_string(data.get('kind'), 'skill_effect_kind_invalid'), value=_finite_number(data.get('value'), 'skill_effect_value_invalid'), duration_seconds=_optional_finite_number(data.get('duration_seconds'), 'skill_effect_duration_invalid', minimum=0.0), cooldown_seconds=_optional_finite_number(data.get('cooldown_seconds'), 'skill_effect_cooldown_invalid', minimum=0.0))

@dataclass(frozen=True)
class SkillMechanics:
    trigger_code: str
    phase: tuple[int, ...]
    effects: tuple[SkillEffect, ...]
    per_owned_skill_bonus_percent: float | None
    per_owned_skill_bonus_cap_percent: float | None
    evaluation_score: int | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'SkillMechanics':
        if type(data) is not dict:
            raise ValueError('skill_mechanics_invalid')
        _require_exact_keys(data, required=frozenset({'trigger_code', 'phase', 'effects'}), optional=frozenset({'per_owned_skill_bonus_percent', 'per_owned_skill_bonus_cap_percent', 'evaluation_score'}), error='skill_mechanics_keys_invalid')
        raw_phase = data.get('phase')
        if type(raw_phase) is not list or not raw_phase or any((type(item) is not int or item < 0 for item in raw_phase)) or (len(set(raw_phase)) != len(raw_phase)):
            raise ValueError('skill_mechanics_phase_invalid')
        raw_effects = data.get('effects')
        if type(raw_effects) is not list or not raw_effects:
            raise ValueError('skill_mechanics_effects_invalid')
        raw_evaluation = data.get('evaluation_score')
        if raw_evaluation is not None and (type(raw_evaluation) is not int or raw_evaluation < 0):
            raise ValueError('skill_mechanics_evaluation_invalid')
        bonus = _optional_finite_number(data.get('per_owned_skill_bonus_percent'), 'skill_mechanics_bonus_invalid', minimum=0.0)
        cap = _optional_finite_number(data.get('per_owned_skill_bonus_cap_percent'), 'skill_mechanics_bonus_cap_invalid', minimum=0.0)
        if (bonus is None) != (cap is None):
            raise ValueError('skill_mechanics_bonus_pair_incomplete')
        return cls(trigger_code=_required_string(data.get('trigger_code'), 'skill_mechanics_trigger_invalid'), phase=tuple(raw_phase), effects=tuple((SkillEffect.from_dict(item) for item in raw_effects)), per_owned_skill_bonus_percent=bonus, per_owned_skill_bonus_cap_percent=cap, evaluation_score=raw_evaluation)

@dataclass(frozen=True)
class SkillSemanticProvenance:
    semantic_status: str
    source_refs: tuple[str, ...]
    reviewed_by: tuple[str, ...]
    live_eligible: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'SkillSemanticProvenance':
        if type(data) is not dict:
            raise ValueError('skill_semantic_provenance_invalid')
        _require_exact_keys(data, required=frozenset({'semantic_status', 'source_refs', 'reviewed_by', 'live_eligible'}), error='skill_semantic_provenance_keys_invalid')
        live_eligible = data.get('live_eligible')
        if type(live_eligible) is not bool:
            raise ValueError('skill_semantic_live_eligible_invalid')
        status = _required_string(data.get('semantic_status'), 'skill_semantic_status_invalid')
        if live_eligible and status != APPROVED_SEMANTIC_STATUS:
            raise ValueError('semantic_live_provenance_unapproved')
        return cls(semantic_status=status, source_refs=_string_tuple(data.get('source_refs'), 'skill_semantic_source_refs_invalid', allow_empty=False), reviewed_by=_string_tuple(data.get('reviewed_by'), 'skill_semantic_reviewers_invalid', allow_empty=True), live_eligible=live_eligible)

def load_skill_semantic_catalog(path: str | Path, *, allow_shadow: bool=False) -> dict[str, Any]:
    from core.skill_planner.models import SkillCatalogRow
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if type(payload) is not dict:
        raise ValueError('skill_semantic_catalog_invalid')
    _require_exact_keys(payload, required=frozenset({'schema_version', 'dataset_version', 'skills'}), error='skill_semantic_catalog_keys_invalid')
    if payload.get('schema_version') != SEMANTIC_CATALOG_SCHEMA:
        raise ValueError('skill_semantic_catalog_schema_invalid')
    _required_string(payload.get('dataset_version'), 'skill_semantic_dataset_invalid')
    raw_skills = payload.get('skills')
    if type(raw_skills) is not list:
        raise ValueError('skill_semantic_skills_invalid')
    rows: dict[str, SkillCatalogRow] = {}
    for raw in raw_skills:
        if type(raw) is not dict:
            raise ValueError('skill_semantic_record_invalid')
        row = SkillCatalogRow.from_dict(raw)
        if not row.skill_id:
            raise ValueError('skill_semantic_id_invalid')
        if row.skill_id in rows:
            raise ValueError('skill_semantic_id_duplicate')
        if row.semantic_provenance is None:
            raise ValueError('skill_semantic_provenance_missing')
        if allow_shadow or row.semantic_provenance.live_eligible:
            rows[row.skill_id] = row
    return rows
