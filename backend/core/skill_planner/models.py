from __future__ import annotations
from dataclasses import asdict, dataclass, field
import math
import re
from typing import Any
from core.skill_planner.semantics import SkillMechanics, SkillSemanticProvenance
_SHA256_PATTERN = re.compile('[0-9a-f]{64}')

def _copy_json_object(value: object, error: str) -> dict[str, object]:
    copied = _copy_json_value(value, error)
    if type(copied) is not dict:
        raise ValueError(error)
    return copied

def _copy_json_value(value: object, error: str) -> object:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(error)
        return value
    if type(value) is list:
        return [_copy_json_value(item, error) for item in value]
    if type(value) is dict:
        copied: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(error)
            copied[key] = _copy_json_value(item, error)
        return copied
    raise ValueError(error)

def _parse_source_capture_sha256(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError('source_capture_sha256_invalid')
    return value

def _parse_base_utility(data: dict[str, Any]) -> float | None:
    value = data.get('base_utility')
    if value is None:
        return None
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        raise ValueError('base_utility_invalid')
    return float(value)

@dataclass(frozen=True)
class AvailableSkill:
    skill_id: str
    display_cost: int
    recognition_confidence: float = 1.0
    cost_basis: str = 'base'
    observed_hint_level: int | None = None
    observed_discount_percent: int | None = None
    source_evidence: tuple[dict[str, object], ...] = ()
    identity_binding: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'AvailableSkill':
        raw_observed_hint = data.get('observed_hint_level')
        raw_observed_discount = data.get('observed_discount_percent')
        raw_evidence = data.get('source_evidence', [])
        if type(raw_evidence) is not list:
            raise ValueError('source_evidence_invalid')
        source_evidence: list[dict[str, object]] = []
        for item in raw_evidence:
            if type(item) is not dict:
                raise ValueError('source_evidence_invalid')
            source_evidence.append(_copy_json_object(item, 'source_evidence_invalid'))
        raw_identity_binding = data.get('identity_binding', {})
        if type(raw_identity_binding) is not dict:
            raise ValueError('identity_binding_invalid')
        return cls(skill_id=str(data.get('skill_id', '')), display_cost=int(data.get('display_cost', 0) or 0), recognition_confidence=float(data.get('recognition_confidence', 0.0) or 0.0), cost_basis=str(data.get('cost_basis', 'base')), observed_hint_level=int(raw_observed_hint) if raw_observed_hint is not None else None, observed_discount_percent=int(raw_observed_discount) if raw_observed_discount is not None else None, source_evidence=tuple(source_evidence), identity_binding=_copy_json_object(raw_identity_binding, 'identity_binding_invalid'))

@dataclass(frozen=True)
class PlannerPolicy:
    reserve_skill_pt: int = 0
    trigger_budget: int | None = None
    scheduled_learning_mode: str = 'conditional'
    allow_skip_purchase: bool = True
    allow_gold_without_lower: bool = False
    require_condition_match: bool = True
    allow_unverified_skills: bool = False
    max_purchase_count: int | None = None
    min_confidence: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> 'PlannerPolicy':
        data = data or {}
        raw_budget = data.get('trigger_budget')
        raw_max_purchase_count = data.get('max_purchase_count')
        return cls(reserve_skill_pt=int(data.get('reserve_skill_pt', 0) or 0), trigger_budget=int(raw_budget) if raw_budget is not None else None, scheduled_learning_mode=str(data.get('scheduled_learning_mode', 'conditional')), allow_skip_purchase=bool(data.get('allow_skip_purchase', True)), allow_gold_without_lower=bool(data.get('allow_gold_without_lower', False)), require_condition_match=bool(data.get('require_condition_match', True)), allow_unverified_skills=bool(data.get('allow_unverified_skills', False)), max_purchase_count=int(raw_max_purchase_count) if raw_max_purchase_count is not None else None, min_confidence=float(data.get('min_confidence', 0.0) or 0.0))

@dataclass(frozen=True)
class SkillCatalogRow:
    skill_id: str
    name_zh_cn: str
    type: str
    rarity: str
    tags: tuple[str, ...] = ()
    base_utility: float | None = None
    conditions: dict[str, Any] = field(default_factory=dict)
    base_cost: int = 0
    relations: dict[str, list[str]] = field(default_factory=dict)
    requires_manual_verification: bool = True
    manual_status: str = 'needs_review'
    source_urls: list[str] = field(default_factory=list)
    mechanics: SkillMechanics | None = None
    semantic_provenance: SkillSemanticProvenance | None = None
    knowledge: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'SkillCatalogRow':
        raw_tags = data.get('tags', [])
        if type(raw_tags) is not list:
            raise ValueError('skill_catalog_tags_invalid')
        tags: list[str] = []
        for raw_tag in raw_tags:
            if type(raw_tag) is not str or not raw_tag:
                raise ValueError('skill_catalog_tags_invalid')
            if raw_tag in tags:
                raise ValueError('skill_catalog_tags_duplicate')
            tags.append(raw_tag)
        relations: dict[str, list[str]] = {}
        for key, value in dict(data.get('relations', {})).items():
            if isinstance(value, list):
                relations[str(key)] = [str(item) for item in value]
        manual_status = str(data.get('manual_status') or '')
        if not manual_status:
            identity_status = data.get('identity_status')
            metadata_status = data.get('metadata_status')
            if identity_status is not None or metadata_status is not None:
                manual_status = 'manual_verified' if identity_status == 'manual_verified' and metadata_status == 'manual_verified' else 'needs_review'
            else:
                manual_status = 'needs_review' if data.get('requires_manual_verification', True) else 'manual_verified'
        return cls(skill_id=str(data.get('skill_id', '')), name_zh_cn=str(data.get('name_zh_cn', '')), type=str(data.get('type', '')), rarity=str(data.get('rarity', '')), tags=tuple(tags), base_utility=_parse_base_utility(data), conditions=dict(data.get('conditions', {})), base_cost=int(data.get('base_cost', 0) or 0), relations=relations, requires_manual_verification=bool(data.get('requires_manual_verification', True)), manual_status=manual_status, source_urls=[str(item) for item in data.get('source_urls', [])], knowledge=dict(data.get('knowledge', {})), mechanics=SkillMechanics.from_dict(data['mechanics']) if data.get('mechanics') is not None else None, semantic_provenance=SkillSemanticProvenance.from_dict(data['semantic_provenance']) if data.get('semantic_provenance') is not None else None)

    @property
    def is_verified(self) -> bool:
        return self.manual_status == 'manual_verified' or not self.requires_manual_verification

@dataclass(frozen=True)
class PlannedPurchase:
    skill_id: str
    name: str
    type: str
    rarity: str
    display_cost: int
    final_cost: int
    hint_level: int
    score: float
    priority_bucket: str
    reasons: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    marginal_utility: float | None = None
    utility_per_pt: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.marginal_utility is None:
            payload.pop('marginal_utility')
        if self.utility_per_pt is None:
            payload.pop('utility_per_pt')
        return payload

@dataclass(frozen=True)
class SkippedSkill:
    skill_id: str
    name: str
    reason_code: str
    details: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True)
class SkillPurchaseContext:
    schema_version: str
    trigger: str
    target_mode: str
    race_profile: dict[str, Any]
    current_skill_pt: int
    owned_skills: list[str] = field(default_factory=list)
    hint_levels: dict[str, int] = field(default_factory=dict)
    available_skills: list[AvailableSkill] = field(default_factory=list)
    current_stats: dict[str, int] = field(default_factory=dict)
    character: dict[str, Any] = field(default_factory=dict)
    support_sources: list[dict[str, Any]] = field(default_factory=list)
    policy: PlannerPolicy = field(default_factory=PlannerPolicy)
    source_capture_sha256: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'SkillPurchaseContext':
        source_capture_sha256 = _parse_source_capture_sha256(data.get('source_capture_sha256'))
        return cls(schema_version=str(data.get('schema_version', '')), trigger=str(data.get('trigger', 'final_build')), target_mode=str(data.get('target_mode', '')), race_profile=dict(data.get('race_profile', {})), current_skill_pt=int(data.get('current_skill_pt', 0) or 0), owned_skills=[str(item) for item in data.get('owned_skills', [])], hint_levels={str(key): int(value) for key, value in dict(data.get('hint_levels', {})).items()}, available_skills=[AvailableSkill.from_dict(item) for item in data.get('available_skills', []) if isinstance(item, dict)], current_stats={str(key): int(value) for key, value in dict(data.get('current_stats', {})).items() if isinstance(value, (int, float)) and (not isinstance(value, bool))}, character=dict(data.get('character', {})), support_sources=[dict(item) for item in data.get('support_sources', []) if isinstance(item, dict)], policy=PlannerPolicy.from_dict(data.get('policy')), source_capture_sha256=source_capture_sha256)

@dataclass(frozen=True)
class SkillPurchasePlan:
    schema_version: str
    target_mode: str
    trigger: str
    status: str
    purchases: list[PlannedPurchase] = field(default_factory=list)
    skipped: list[SkippedSkill] = field(default_factory=list)
    reserved_skill_pt: int = 0
    spent_skill_pt: int = 0
    remaining_skill_pt: int = 0
    warnings: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    explain: str = ''
    profile_id: str = ''
    profile_sha256: str = ''
    candidate_set_sha256: str = ''
    plan_sha256: str = ''
    unmet_required_skill_ids: list[str] = field(default_factory=list)
    optimizer_status: str = 'not_run'
    optimizer_explored_states: int = 0
    execution_allowed: bool = False

    def __post_init__(self) -> None:
        if type(self.execution_allowed) is not bool or self.execution_allowed is not False:
            raise ValueError('execution_allowed_invalid')

    def to_dict(self) -> dict[str, Any]:
        return {'schema_version': self.schema_version, 'target_mode': self.target_mode, 'trigger': self.trigger, 'status': self.status, 'purchases': [item.to_dict() for item in self.purchases], 'skipped': [item.to_dict() for item in self.skipped], 'reserved_skill_pt': self.reserved_skill_pt, 'spent_skill_pt': self.spent_skill_pt, 'remaining_skill_pt': self.remaining_skill_pt, 'warnings': list(self.warnings), 'risk_flags': list(self.risk_flags), 'explain': self.explain, 'profile_id': self.profile_id, 'profile_sha256': self.profile_sha256, 'candidate_set_sha256': self.candidate_set_sha256, 'plan_sha256': self.plan_sha256, 'unmet_required_skill_ids': list(self.unmet_required_skill_ids), 'optimizer_status': self.optimizer_status, 'optimizer_explored_states': self.optimizer_explored_states, 'execution_allowed': self.execution_allowed}
