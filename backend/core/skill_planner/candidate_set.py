"""Stable, provenance-preserving aggregation for observed skill candidates."""
from __future__ import annotations
import hashlib
import json
import math
import re
from dataclasses import dataclass
from core.skill_planner.models import AvailableSkill, SkillPurchaseContext
_SHA256_PATTERN = re.compile('[0-9a-f]{64}')

@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    source_page: str
    page_index: int
    row_index: int | None
    fragment_only: bool

@dataclass(frozen=True, slots=True)
class AggregatedSkillCandidate:
    skill_id: str
    display_cost: int
    recognition_confidence: float
    cost_basis: str
    observed_hint_level: int | None
    observed_discount_percent: int | None
    identity_binding_sha256: str
    evidence: tuple[CandidateEvidence, ...]

@dataclass(frozen=True, slots=True)
class CandidateAbstention:
    skill_id: str
    reason_code: str
    evidence_count: int

@dataclass(frozen=True, slots=True)
class SkillCandidateSet:
    candidates: tuple[AggregatedSkillCandidate, ...]
    abstained: tuple[CandidateAbstention, ...]
    profile_sha256: str
    source_capture_sha256: str | None
    candidate_set_sha256: str
    risk_flags: tuple[str, ...]

def build_skill_candidate_set(context: SkillPurchaseContext, *, profile_sha256: str) -> SkillCandidateSet:
    """Aggregate equivalent observations or abstain whenever evidence conflicts."""
    source_capture_sha256 = _validate_sha(context.source_capture_sha256, 'source_capture_sha256_invalid', allow_none=True)
    _validate_sha(profile_sha256, 'profile_sha256_invalid', allow_none=False)
    grouped: dict[str, list[AvailableSkill]] = {}
    for available in context.available_skills:
        grouped.setdefault(available.skill_id, []).append(available)
    candidates: list[AggregatedSkillCandidate] = []
    abstained: list[CandidateAbstention] = []
    risk_flags: set[str] = set()
    for skill_id in sorted(grouped):
        group = grouped[skill_id]
        evidence_count = sum((len(item.source_evidence) for item in group))
        identity_binding_hashes = [_trusted_identity_binding_sha(item.identity_binding) for item in group]
        if any((item is None for item in identity_binding_hashes)):
            abstained.append(CandidateAbstention(skill_id, 'identity_binding_untrusted', evidence_count))
            continue
        binding_hashes = {item for item in identity_binding_hashes if item is not None}
        if len(binding_hashes) > 1:
            abstained.append(CandidateAbstention(skill_id, 'identity_binding_conflict', evidence_count))
            continue
        parsed_evidence = _parse_group_evidence(group)
        if parsed_evidence is None:
            abstained.append(CandidateAbstention(skill_id, 'candidate_provenance_invalid', evidence_count))
            continue
        if len(group) > 1 and (not _has_complete_duplicate_provenance(parsed_evidence)):
            abstained.append(CandidateAbstention(skill_id, 'duplicate_candidate_provenance_incomplete', evidence_count))
            continue
        if _candidate_values_conflict(group):
            abstained.append(CandidateAbstention(skill_id, 'cross_page_candidate_conflict', evidence_count))
            continue
        if len(group) == 1 and (not parsed_evidence):
            risk_flags.add('legacy_candidate_without_page_provenance')
        first = group[0]
        candidates.append(AggregatedSkillCandidate(skill_id=skill_id, display_cost=first.display_cost, recognition_confidence=first.recognition_confidence, cost_basis=first.cost_basis, observed_hint_level=first.observed_hint_level, observed_discount_percent=first.observed_discount_percent, identity_binding_sha256=next(iter(binding_hashes)), evidence=tuple(sorted(parsed_evidence, key=_evidence_sort_key))))
    if source_capture_sha256 is None:
        risk_flags.add('source_capture_binding_missing')
    ordered_candidates = tuple(sorted(candidates, key=lambda item: item.skill_id))
    ordered_abstained = tuple(sorted(abstained, key=lambda item: item.skill_id))
    ordered_risks = tuple(sorted(risk_flags))
    candidate_set_sha256 = _candidate_set_sha(profile_sha256=profile_sha256, source_capture_sha256=source_capture_sha256, candidates=ordered_candidates, abstained=ordered_abstained, risk_flags=ordered_risks)
    return SkillCandidateSet(candidates=ordered_candidates, abstained=ordered_abstained, profile_sha256=profile_sha256, source_capture_sha256=source_capture_sha256, candidate_set_sha256=candidate_set_sha256, risk_flags=ordered_risks)

def candidate_set_content_sha256(candidate_set: SkillCandidateSet) -> str:
    """Return the canonical content digest, excluding its stored digest field."""
    return _candidate_set_sha(profile_sha256=candidate_set.profile_sha256, source_capture_sha256=candidate_set.source_capture_sha256, candidates=candidate_set.candidates, abstained=candidate_set.abstained, risk_flags=candidate_set.risk_flags)

def candidate_set_sha_matches(candidate_set: SkillCandidateSet) -> bool:
    """Fail closed when any candidate-set payload differs from its stored digest."""
    if type(candidate_set.candidate_set_sha256) is not str:
        return False
    try:
        return candidate_set.candidate_set_sha256 == candidate_set_content_sha256(candidate_set)
    except (TypeError, ValueError):
        return False

def _parse_group_evidence(group: list[AvailableSkill]) -> list[CandidateEvidence] | None:
    parsed: list[CandidateEvidence] = []
    for available in group:
        for raw_evidence in available.source_evidence:
            evidence = _parse_evidence(raw_evidence)
            if evidence is None:
                return None
            parsed.append(evidence)
    return parsed

def _parse_evidence(raw_evidence: dict[str, object]) -> CandidateEvidence | None:
    source_page = raw_evidence.get('source_page')
    page_index = raw_evidence.get('page_index')
    row_index = raw_evidence.get('row_index')
    fragment_only = raw_evidence.get('fragment_only', False)
    if type(source_page) is not str or not source_page:
        return None
    if type(page_index) is not int:
        return None
    if row_index is not None and type(row_index) is not int:
        return None
    if type(fragment_only) is not bool:
        return None
    return CandidateEvidence(source_page, page_index, row_index, fragment_only)

def _has_complete_duplicate_provenance(evidence: list[CandidateEvidence]) -> bool:
    return bool(evidence) and all((item.row_index is not None and (not item.fragment_only) for item in evidence))

def _trusted_identity_binding_sha(binding: object) -> str | None:
    if type(binding) is not dict or not _is_json_safe(binding):
        return None
    status = binding.get('status')
    confidence = binding.get('confidence')
    margin = binding.get('margin')
    evidence_dimensions = binding.get('evidence_dimensions')
    if type(status) is not str or status != 'auto_confirmed':
        return None
    if not _is_finite_probability(confidence):
        return None
    if not _is_finite_probability(margin):
        return None
    if type(evidence_dimensions) is not int or evidence_dimensions < 3:
        return None
    confidence_value = float(confidence)
    margin_value = float(margin)
    standard_binding = confidence_value >= 0.88 and margin_value >= 0.1
    unique_high_margin_binding = confidence_value >= 0.82 and margin_value >= 0.5
    current_run_multi_evidence_binding = _is_current_run_multi_evidence_binding(binding)
    if not (standard_binding or unique_high_margin_binding or current_run_multi_evidence_binding):
        return None
    return hashlib.sha256(_canonical_json(binding).encode('utf-8')).hexdigest()

def _is_current_run_multi_evidence_binding(binding: dict[str, object]) -> bool:
    evidence = binding.get('evidence')
    contract = binding.get('current_run_contract')
    if type(evidence) is not dict or type(contract) is not dict:
        return False
    local_db_id = contract.get('local_db_id')
    identity_status = contract.get('identity_status')
    metadata_status = contract.get('metadata_status')
    return binding.get('binding_policy') == 'current_run_multi_evidence' and float(binding.get('confidence', 0.0)) >= 0.7 and (binding.get('evidence_dimensions') in {3, 4}) and (evidence.get('cost') == 1.0) and (evidence.get('rarity') == 1.0) and (evidence.get('icon_color') == 1.0) and (contract.get('version') == 'current_run_multi_evidence.v1') and (type(local_db_id) is str) and local_db_id.isdigit() and (identity_status in {'manual_verified', 'exact_local_db_id_join'}) and (metadata_status in {'manual_verified', 'external_cross_checked'}) and (contract.get('description_anchor_exact') is True or contract.get('name_score_threshold_met') is True)

def _is_json_safe(value: object) -> bool:
    if value is None or type(value) in {str, bool, int}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all((_is_json_safe(item) for item in value))
    if type(value) is dict:
        return all((type(key) is str and _is_json_safe(item) for key, item in value.items()))
    return False

def _is_finite_probability(value: object) -> bool:
    return type(value) in {int, float} and math.isfinite(float(value)) and (0.0 <= float(value) <= 1.0)

def _candidate_values_conflict(group: list[AvailableSkill]) -> bool:
    values = {(item.display_cost, item.recognition_confidence, item.cost_basis, item.observed_hint_level, item.observed_discount_percent) for item in group}
    return len(values) > 1

def _evidence_sort_key(item: CandidateEvidence) -> tuple[int, int, str, bool]:
    return (item.page_index, item.row_index if item.row_index is not None else -1, item.source_page, item.fragment_only)

def _candidate_set_sha(*, profile_sha256: str, source_capture_sha256: str | None, candidates: tuple[AggregatedSkillCandidate, ...], abstained: tuple[CandidateAbstention, ...], risk_flags: tuple[str, ...]) -> str:
    material = {'profile_sha256': profile_sha256, 'source_capture_sha256': source_capture_sha256, 'candidates': [{'skill_id': item.skill_id, 'display_cost': item.display_cost, 'recognition_confidence': item.recognition_confidence, 'cost_basis': item.cost_basis, 'observed_hint_level': item.observed_hint_level, 'observed_discount_percent': item.observed_discount_percent, 'identity_binding_sha256': item.identity_binding_sha256, 'evidence': [{'source_page': evidence.source_page, 'page_index': evidence.page_index, 'row_index': evidence.row_index, 'fragment_only': evidence.fragment_only} for evidence in item.evidence]} for item in candidates], 'abstained': [{'skill_id': item.skill_id, 'reason_code': item.reason_code, 'evidence_count': item.evidence_count} for item in abstained], 'risk_flags': list(risk_flags)}
    return hashlib.sha256(_canonical_json(material).encode('utf-8')).hexdigest()

def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def _validate_sha(value: object, error: str, *, allow_none: bool) -> str | None:
    if value is None and allow_none:
        return None
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(error)
    return value
__all__ = ['AggregatedSkillCandidate', 'CandidateAbstention', 'CandidateEvidence', 'SkillCandidateSet', 'build_skill_candidate_set', 'candidate_set_content_sha256', 'candidate_set_sha_matches']
