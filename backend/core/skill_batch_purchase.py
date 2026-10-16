"""Immutable inventory and one-shot planning contracts for final skill purchase.

This module is deliberately device-free.  It converts one completed capture
and planner dry-run into a compact, SHA-bound inventory packet, and then
rebuilds a batch plan from allowed skill IDs.  Coordinates and live clicks are
owned by a later execution layer.
"""
from __future__ import annotations
import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import PurePath
from typing import Any
_SHA256 = re.compile('[0-9a-f]{64}')
_DECISION_SOURCES = {'local_auto', 'ai_advisor', 'review_only'}
_AI_RESPONSE_FIELDS = {'inventory_packet_sha256', 'action_ids'}
_UNSAFE_RISK_FLAGS = frozenset({'disabled', 'forbidden', 'negative_return'})
_MIN_IDENTITY_CONFIDENCE = 0.75
_MIN_IDENTITY_MARGIN = 0.1
MAX_CAPTURE_TTL_UNIX_MS = 1800000
_NATIVE_CLOSED_ROW_HEIGHT = 165

def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')

def canonical_packet_sha256(packet: Mapping[str, Any]) -> str:
    """Return the content digest without trusting the stored digest field."""
    material = {key: value for key, value in packet.items() if key != 'packet_sha256'}
    return hashlib.sha256(_canonical_json(material)).hexdigest()

def _canonical_plan_sha256(plan: Mapping[str, Any]) -> str:
    material = {key: value for key, value in plan.items() if key != 'plan_sha256'}
    return hashlib.sha256(_canonical_json(material)).hexdigest()

def _is_int(value: Any) -> bool:
    return type(value) is int

def _is_probability(value: Any) -> bool:
    return type(value) in {int, float} and (not isinstance(value, bool)) and math.isfinite(float(value)) and (0.0 <= float(value) <= 1.0)

def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None

def _capture_timing_error(value: Mapping[str, Any]) -> str | None:
    captured = value.get('captured_at_unix_ms')
    expires = value.get('expires_at_unix_ms')
    if captured is None or expires is None:
        return 'capture_timing_missing'
    if not _is_int(captured) or not _is_int(expires):
        return 'capture_timing_invalid'
    if not 0 < expires - captured <= MAX_CAPTURE_TTL_UNIX_MS:
        return 'capture_timing_window_invalid'
    return None

def _valid_bbox(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 4 and all((_is_int(item) for item in value)) and (value[0] < value[2]) and (value[1] < value[3])

def _normalize_evidence(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError('candidate_evidence_invalid')
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError('candidate_evidence_invalid')
        source_page = raw.get('source_page')
        page_index = raw.get('page_index')
        row_index = raw.get('row_index')
        fragment_only = raw.get('fragment_only', False)
        row_bbox = raw.get('row_bbox')
        if not isinstance(source_page, str) or not source_page or (not _is_int(page_index)) or (page_index < 0) or (row_index is not None and (not _is_int(row_index) or row_index < 0)) or (type(fragment_only) is not bool) or (row_bbox is not None and (not _valid_bbox(row_bbox))):
            raise ValueError('candidate_evidence_invalid')
        item = {'source_page': source_page, 'page_index': page_index, 'row_index': row_index, 'row_bbox': list(row_bbox) if row_bbox is not None else None, 'fragment_only': fragment_only}
        key = (source_page, page_index, row_index, tuple(row_bbox) if row_bbox is not None else None, fragment_only)
        if key not in seen:
            normalized.append(item)
            seen.add(key)
    return sorted(normalized, key=lambda item: (item['page_index'], -1 if item['row_index'] is None else item['row_index'], item['source_page']))

def _require_sha_bound_evidence(evidence: Sequence[Mapping[str, Any]], page_names: Sequence[str]) -> None:
    """Reject rows that are not part of this exact captured scroll sequence."""
    for item in evidence:
        page_index = item.get('page_index')
        source_page = item.get('source_page')
        if not _is_int(page_index) or page_index < 0 or page_index >= len(page_names) or (not isinstance(source_page, str)) or (PurePath(source_page).name != PurePath(page_names[page_index]).name):
            raise ValueError('candidate_page_unbound')

def _candidate_signature(raw: Mapping[str, Any]) -> tuple[Any, ...]:
    binding = raw.get('identity_binding')
    if not isinstance(binding, Mapping):
        raise ValueError('identity_binding_missing')
    current_run_multi_evidence = _is_current_run_multi_evidence_binding(binding)
    if binding.get('status') != 'auto_confirmed' or not _is_probability(binding.get('confidence')) or (not _is_probability(binding.get('margin'))) or (not _is_int(binding.get('evidence_dimensions'))) or (binding['evidence_dimensions'] < 3) or (not current_run_multi_evidence and (float(binding['confidence']) < _MIN_IDENTITY_CONFIDENCE or float(binding['margin']) < _MIN_IDENTITY_MARGIN)):
        raise ValueError('identity_binding_untrusted')
    try:
        binding_sha256 = hashlib.sha256(_canonical_json(binding)).hexdigest()
    except (TypeError, ValueError):
        raise ValueError('identity_binding_untrusted') from None
    return (raw.get('display_cost'), raw.get('cost_basis'), raw.get('observed_hint_level'), raw.get('observed_discount_percent'), binding.get('status'), float(binding['confidence']), float(binding['margin']), binding['evidence_dimensions'], binding_sha256)

def _is_current_run_multi_evidence_binding(binding: Mapping[str, Any]) -> bool:
    evidence = binding.get('evidence')
    contract = binding.get('current_run_contract')
    if not isinstance(evidence, Mapping) or not isinstance(contract, Mapping):
        return False
    confidence = binding.get('confidence')
    dimensions = binding.get('evidence_dimensions')
    local_db_id = contract.get('local_db_id')
    return binding.get('status') == 'auto_confirmed' and binding.get('binding_policy') == 'current_run_multi_evidence' and _is_probability(confidence) and (float(confidence) >= 0.7) and _is_int(dimensions) and (dimensions in {3, 4}) and (evidence.get('cost') == 1.0) and (evidence.get('rarity') == 1.0) and (evidence.get('icon_color') == 1.0) and (contract.get('version') == 'current_run_multi_evidence.v1') and isinstance(local_db_id, str) and local_db_id.isdigit() and (contract.get('identity_status') in {'manual_verified', 'exact_local_db_id_join'}) and (contract.get('metadata_status') in {'manual_verified', 'external_cross_checked'}) and (contract.get('description_anchor_exact') is True or contract.get('name_score_threshold_met') is True)

def _validate_resource_checks(planner_plan: Mapping[str, Any]) -> None:
    checks = planner_plan.get('resource_checks')
    if not isinstance(checks, Mapping):
        raise ValueError('resource_checks_missing')
    if checks != {'uses_skill_points_only': True, 'uses_carrots': False, 'uses_clocks': False, 'uses_paid_currency': False}:
        raise ValueError('resource_checks_invalid')

def _normalized_relations(value: Any) -> dict[str, list[str]]:
    """Keep only explicit, identifier-based upgrade edges from the planner."""
    if value is None:
        return {'lower': [], 'upper': []}
    if not isinstance(value, Mapping):
        raise ValueError('candidate_relations_invalid')
    normalized: dict[str, list[str]] = {}
    for name in ('lower', 'upper'):
        raw = value.get(name, [])
        if not isinstance(raw, list) or any((not isinstance(item, str) or not item for item in raw)):
            raise ValueError('candidate_relations_invalid')
        normalized[name] = sorted(set(raw))
    if set(value) - {'lower', 'upper'}:
        raise ValueError('candidate_relations_invalid')
    return normalized

def build_skill_inventory_packet(*, capture_binding: Mapping[str, Any], planner_observation: Mapping[str, Any], planner_plan: Mapping[str, Any], build_context: Mapping[str, Any]) -> dict[str, Any]:
    """Build one immutable inventory from a complete capture and planner run."""
    if capture_binding.get('schema_version') != 'uma.skill_capture_binding.v1':
        raise ValueError('capture_binding_schema_invalid')
    capture_sha = capture_binding.get('capture_sha256')
    if not _valid_sha(capture_sha):
        raise ValueError('capture_sha_invalid')
    timing_error = _capture_timing_error(capture_binding)
    if timing_error is not None:
        raise ValueError(timing_error)
    page_hashes = capture_binding.get('page_hashes')
    if not isinstance(page_hashes, list) or not page_hashes:
        raise ValueError('capture_page_hashes_missing')
    for page_hash in page_hashes:
        if not isinstance(page_hash, Mapping) or not isinstance(page_hash.get('name'), str) or (not page_hash['name']) or (not _valid_sha(page_hash.get('sha256'))):
            raise ValueError('capture_page_hash_invalid')
    page_names = [item['name'] for item in page_hashes]
    page_digests = [item['sha256'] for item in page_hashes]
    if len(page_names) != len(set(page_names)):
        raise ValueError('capture_page_name_duplicate')
    if len(page_digests) != len(set(page_digests)):
        raise ValueError('capture_page_sha_replayed')
    if planner_observation.get('schema_version') != 'uma.skill_planner_observation.v1':
        raise ValueError('planner_observation_schema_invalid')
    current_points = planner_observation.get('current_skill_pt')
    if not _is_int(current_points) or current_points < 0:
        raise ValueError('current_skill_points_invalid')
    raw_owned = planner_observation.get('owned_skills', [])
    if not isinstance(raw_owned, list):
        raise ValueError('owned_skills_invalid')
    owned_skill_ids = sorted({item for item in raw_owned if isinstance(item, str) and item})
    owned_skill_id_set = set(owned_skill_ids)
    if not isinstance(build_context, Mapping):
        raise ValueError('build_context_invalid')
    _validate_resource_checks(planner_plan)
    planned_by_id: dict[str, Mapping[str, Any]] = {}
    planned_ids: list[str] = []
    purchases = planner_plan.get('purchases')
    if not isinstance(purchases, list):
        raise ValueError('planner_purchases_invalid')
    for raw in purchases:
        if not isinstance(raw, Mapping):
            raise ValueError('planner_purchase_invalid')
        skill_id = raw.get('skill_id')
        if not isinstance(skill_id, str) or not skill_id or skill_id in planned_by_id:
            raise ValueError('planner_purchase_id_invalid')
        planned_by_id[skill_id] = raw
        planned_ids.append(skill_id)
    raw_available = planner_observation.get('available_skills')
    if not isinstance(raw_available, list):
        raise ValueError('available_skills_invalid')
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for raw in raw_available:
        if not isinstance(raw, Mapping):
            raise ValueError('available_skill_invalid')
        skill_id = raw.get('skill_id')
        if not isinstance(skill_id, str) or not skill_id:
            raise ValueError('available_skill_id_invalid')
        grouped[skill_id].append(raw)
    candidates: list[dict[str, Any]] = []
    abstentions: list[dict[str, Any]] = []
    for skill_id in sorted(grouped):
        group = grouped[skill_id]
        if skill_id in owned_skill_id_set:
            abstentions.append({'skill_id': skill_id, 'reason': 'already_owned', 'evidence': _normalize_evidence([evidence for raw in group for evidence in raw.get('source_evidence', [])])})
            continue
        try:
            signatures = {_candidate_signature(raw) for raw in group}
            all_evidence = [evidence for raw in group for evidence in _normalize_evidence(raw.get('source_evidence'))]
            _require_sha_bound_evidence(all_evidence, page_names)
        except ValueError as exc:
            abstentions.append({'skill_id': skill_id, 'reason': str(exc), 'evidence': []})
            continue
        if len(signatures) != 1:
            abstentions.append({'skill_id': skill_id, 'reason': 'candidate_conflict', 'evidence': all_evidence})
            continue
        if not all_evidence:
            abstentions.append({'skill_id': skill_id, 'reason': 'candidate_evidence_missing', 'evidence': []})
            continue
        signature = next(iter(signatures))
        planner_item = planned_by_id.get(skill_id, {})
        candidates.append({'skill_id': skill_id, 'display_name': planner_item.get('name'), 'skill_type': planner_item.get('type'), 'rarity': planner_item.get('rarity'), 'display_cost': signature[0], 'cost_basis': signature[1], 'observed_hint_level': signature[2], 'observed_discount_percent': signature[3], 'recognition_confidence': min((float(raw.get('recognition_confidence', 0.0)) for raw in group)), 'score': planner_item.get('score'), 'priority_bucket': planner_item.get('priority_bucket'), 'reasons': list(planner_item.get('reasons', [])), 'risk_flags': list(planner_item.get('risk_flags', [])), 'category_tags': list(planner_item.get('category_tags', [])), 'relations': _normalized_relations(planner_item.get('relations')), 'evidence': _normalize_evidence(all_evidence)})
    raw_skipped = planner_observation.get('skipped', [])
    if not isinstance(raw_skipped, list):
        raise ValueError('planner_skipped_invalid')
    for index, raw in enumerate(raw_skipped):
        if not isinstance(raw, Mapping):
            raise ValueError('planner_skipped_invalid')
        abstentions.append({'skill_id': raw.get('skill_id'), 'name': raw.get('name'), 'reason': str(raw.get('reason', 'planner_abstained')), 'evidence': _normalize_evidence(raw.get('source_evidence', [])), 'source_index': index})
    candidate_ids = {item['skill_id'] for item in candidates}
    packet: dict[str, Any] = {'schema_version': 'uma.skill_inventory_packet.v1', 'currency_scope': 'skill_points_only', 'execution_allowed': False, 'captured_at_unix_ms': capture_binding['captured_at_unix_ms'], 'expires_at_unix_ms': capture_binding['expires_at_unix_ms'], 'capture_binding': {'turn_id': capture_binding.get('turn_id'), 'capture_sha256': capture_sha, 'captured_at_unix_ms': capture_binding['captured_at_unix_ms'], 'expires_at_unix_ms': capture_binding['expires_at_unix_ms'], 'page_hashes': [{'name': item['name'], 'sha256': item['sha256']} for item in page_hashes]}, 'current_skill_points': current_points, 'owned_skill_ids': owned_skill_ids, 'build_context': json.loads(_canonical_json(build_context).decode('utf-8')), 'decision_modes': ['local_auto', 'ai_advisor', 'review_only'], 'default_decision_mode': 'local_auto', 'candidates': candidates, 'allowed_action_ids': sorted(candidate_ids), 'local_plan_action_ids': [skill_id for skill_id in planned_ids if skill_id in candidate_ids], 'abstentions': sorted(abstentions, key=lambda item: ('' if item.get('skill_id') is None else str(item['skill_id']), str(item.get('reason', '')), int(item.get('source_index', -1)))), 'resource_checks': {'uses_skill_points_only': True, 'uses_carrots': False, 'uses_clocks': False, 'uses_paid_currency': False}}
    packet['packet_sha256'] = canonical_packet_sha256(packet)
    valid, reason = validate_skill_inventory_packet(packet)
    if not valid:
        raise ValueError(reason)
    return packet

def validate_skill_inventory_packet(packet: Mapping[str, Any]) -> tuple[bool, str]:
    if packet.get('schema_version') != 'uma.skill_inventory_packet.v1':
        return (False, 'schema_version_mismatch')
    if packet.get('currency_scope') != 'skill_points_only':
        return (False, 'currency_scope_mismatch')
    timing_error = _capture_timing_error(packet)
    if timing_error is not None:
        return (False, timing_error)
    capture_binding = packet.get('capture_binding')
    if not isinstance(capture_binding, Mapping) or capture_binding.get('captured_at_unix_ms') != packet.get('captured_at_unix_ms') or capture_binding.get('expires_at_unix_ms') != packet.get('expires_at_unix_ms'):
        return (False, 'capture_timing_binding_mismatch')
    if not _valid_sha(packet.get('packet_sha256')):
        return (False, 'packet_sha_invalid')
    try:
        if packet['packet_sha256'] != canonical_packet_sha256(packet):
            return (False, 'packet_sha_mismatch')
    except (TypeError, ValueError):
        return (False, 'packet_not_canonical')
    points = packet.get('current_skill_points')
    if not _is_int(points) or points < 0:
        return (False, 'current_skill_points_invalid')
    candidates = packet.get('candidates')
    allowed = packet.get('allowed_action_ids')
    if not isinstance(candidates, list) or not isinstance(allowed, list):
        return (False, 'candidate_set_invalid')
    candidate_ids = [item.get('skill_id') for item in candidates if isinstance(item, Mapping)]
    if len(candidate_ids) != len(candidates) or any((not isinstance(item, str) or not item for item in candidate_ids)) or len(candidate_ids) != len(set(candidate_ids)) or (allowed != sorted(candidate_ids)):
        return (False, 'allowed_action_ids_mismatch')
    if packet.get('resource_checks') != {'uses_skill_points_only': True, 'uses_carrots': False, 'uses_clocks': False, 'uses_paid_currency': False}:
        return (False, 'resource_checks_invalid')
    return (True, 'ok')

def project_ai_skill_actions(inventory: Mapping[str, Any], response: Mapping[str, Any]) -> tuple[str, ...]:
    valid, reason = validate_skill_inventory_packet(inventory)
    if not valid:
        raise ValueError(f'inventory_invalid:{reason}')
    if not isinstance(response, Mapping) or set(response) != _AI_RESPONSE_FIELDS:
        raise ValueError('ai_response_fields_invalid')
    if response.get('inventory_packet_sha256') != inventory.get('packet_sha256'):
        raise ValueError('inventory_sha_mismatch')
    raw_actions = response.get('action_ids')
    if not isinstance(raw_actions, list) or any((not isinstance(action, str) or not action for action in raw_actions)):
        raise ValueError('action_ids_invalid')
    actions = tuple(raw_actions)
    if len(actions) != len(set(actions)):
        raise ValueError('duplicate_action_id')
    allowed = set(inventory['allowed_action_ids'])
    if any((action not in allowed for action in actions)):
        raise ValueError('action_id_not_allowed')
    return actions

def _local_score(candidate: Mapping[str, Any]) -> float | None:
    score = candidate.get('score')
    if type(score) not in {int, float} or isinstance(score, bool) or (not math.isfinite(float(score))) or (float(score) <= 0.0):
        return None
    return float(score)

def _candidate_is_locally_safe(candidate: Mapping[str, Any]) -> bool:
    cost = candidate.get('display_cost')
    if not _is_int(cost) or cost <= 0 or _local_score(candidate) is None:
        return False
    flags = candidate.get('risk_flags', [])
    if not isinstance(flags, list) or any((not isinstance(flag, str) for flag in flags)):
        return False
    normalized = {flag.strip().lower() for flag in flags}
    return not any((flag in _UNSAFE_RISK_FLAGS or flag.startswith('negative_') for flag in normalized))

def _candidate_tags(candidate: Mapping[str, Any]) -> set[str]:
    tags = candidate.get('category_tags', [])
    if not isinstance(tags, list) or any((not isinstance(tag, str) for tag in tags)):
        return set()
    result = {tag for tag in tags if tag}
    skill_type = candidate.get('skill_type')
    if isinstance(skill_type, str) and skill_type:
        result.add(skill_type)
    return result

def _related_skill_ids(candidate: Mapping[str, Any]) -> set[str]:
    relations = candidate.get('relations', {})
    if not isinstance(relations, Mapping):
        return set()
    related: set[str] = set()
    for name in ('lower', 'upper'):
        raw = relations.get(name, [])
        if isinstance(raw, list):
            related.update((item for item in raw if isinstance(item, str) and item))
    return related

def select_local_skill_action_ids(inventory: Mapping[str, Any]) -> tuple[str, ...]:
    """Choose a safe, deterministic full-spend candidate set from one inventory.

    Exact and category preferences are admitted first.  The remaining budget is
    filled by a bounded dynamic-programming pass that maximizes spent skill
    points, then local score, while never selecting an explicit upgrade/lower
    pair.  Candidates without a positive locally supplied score are withheld:
    absence of scoring evidence is not permission to spend points.
    """
    valid, reason = validate_skill_inventory_packet(inventory)
    if not valid:
        raise ValueError(f'inventory_invalid:{reason}')
    candidates = {item['skill_id']: item for item in inventory['candidates'] if isinstance(item, Mapping) and isinstance(item.get('skill_id'), str) and _candidate_is_locally_safe(item)}
    related: dict[str, set[str]] = {skill_id: _related_skill_ids(candidate) for skill_id, candidate in candidates.items()}
    for skill_id, values in tuple(related.items()):
        for other in values:
            if other in related:
                related[other].add(skill_id)
    priority = inventory.get('build_context', {}).get('skill_priority', {})
    if not isinstance(priority, Mapping):
        priority = {}
    exact = priority.get('exact_skill_ids', [])
    categories = priority.get('category_tags', [])
    if not isinstance(exact, list) or not isinstance(categories, list):
        raise ValueError('skill_priority_invalid')
    if any((not isinstance(item, str) or not item for item in exact + categories)):
        raise ValueError('skill_priority_invalid')
    exact_rank = {skill_id: index for index, skill_id in enumerate(exact)}
    category_rank = {tag: index for index, tag in enumerate(categories)}

    def priority_key(skill_id: str) -> tuple[Any, ...]:
        candidate = candidates[skill_id]
        if skill_id in exact_rank:
            return (0, exact_rank[skill_id], -_local_score(candidate), skill_id)
        matching_tags = _candidate_tags(candidate) & set(category_rank)
        if matching_tags:
            return (1, min((category_rank[tag] for tag in matching_tags)), -_local_score(candidate), skill_id)
        return (2, 0, -_local_score(candidate), skill_id)
    selected: list[str] = []
    remaining = int(inventory['current_skill_points'])
    for skill_id in sorted(candidates, key=priority_key):
        if priority_key(skill_id)[0] == 2:
            break
        if candidates[skill_id]['display_cost'] <= remaining and (not related[skill_id] & set(selected)):
            selected.append(skill_id)
            remaining -= int(candidates[skill_id]['display_cost'])
    fillers = [skill_id for skill_id in sorted(candidates, key=priority_key) if skill_id not in selected and (not related[skill_id] & set(selected))]
    states: dict[int, tuple[float, tuple[str, ...]]] = {0: (0.0, ())}
    for skill_id in fillers:
        candidate = candidates[skill_id]
        cost = int(candidate['display_cost'])
        score = _local_score(candidate)
        assert score is not None
        for spent, (total_score, actions) in list(states.items()):
            new_spent = spent + cost
            if new_spent > remaining or related[skill_id] & set(actions):
                continue
            proposed = (total_score + score, actions + (skill_id,))
            current = states.get(new_spent)
            if current is None or proposed[0] > current[0] or (proposed[0] == current[0] and proposed[1] < current[1]):
                states[new_spent] = proposed
    best_spent = max(states)
    selected.extend(states[best_spent][1])
    return tuple(selected)

def _primary_evidence(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
    evidence = candidate.get('evidence')
    if not isinstance(evidence, list):
        raise ValueError('candidate_evidence_invalid')
    usable = [item for item in evidence if isinstance(item, Mapping) and item.get('fragment_only') is False and _is_int(item.get('row_index')) and _valid_bbox(item.get('row_bbox')) and (item['row_bbox'][3] - item['row_bbox'][1] == _NATIVE_CLOSED_ROW_HEIGHT)]
    if not usable:
        raise ValueError('candidate_closed_row_missing')
    return min(usable, key=lambda item: (item['page_index'], item['row_index'], item['source_page']))

def build_skill_batch_plan(*, inventory: Mapping[str, Any], selected_action_ids: Sequence[str], decision_source: str, skill_relations: Mapping[str, Sequence[str]] | None=None, reverse_scroll_shifts: Sequence[float] | None=None) -> dict[str, Any]:
    valid, reason = validate_skill_inventory_packet(inventory)
    if not valid:
        raise ValueError(f'inventory_invalid:{reason}')
    if decision_source not in _DECISION_SOURCES:
        raise ValueError('decision_source_invalid')
    actions = list(selected_action_ids)
    if any((not isinstance(action, str) or not action for action in actions)):
        raise ValueError('action_ids_invalid')
    if len(actions) != len(set(actions)):
        raise ValueError('duplicate_action_id')
    candidate_by_id = {item['skill_id']: item for item in inventory['candidates'] if isinstance(item, Mapping)}
    if any((action not in candidate_by_id for action in actions)):
        raise ValueError('action_id_not_allowed')
    contained_lower_ids: list[str] = []
    if skill_relations:
        selected_set = set(actions)
        for upper in actions:
            for lower in skill_relations.get(upper, ()):
                if lower in selected_set and lower not in contained_lower_ids:
                    contained_lower_ids.append(lower)
    contained_set = set(contained_lower_ids)
    if contained_set:
        actions = [action for action in actions if action not in contained_set]
    actions.sort(key=lambda action: (_primary_evidence(candidate_by_id[action])['page_index'], _primary_evidence(candidate_by_id[action])['row_index'], action))
    initial_points = inventory['current_skill_points']
    selected_items: list[dict[str, Any]] = []
    spent = 0
    contained_cost_saved = 0
    for lower in contained_lower_ids:
        candidate = candidate_by_id.get(lower)
        cost = candidate.get('display_cost') if isinstance(candidate, Mapping) else None
        if _is_int(cost) and cost > 0:
            contained_cost_saved += cost
    for action in actions:
        candidate = candidate_by_id[action]
        cost = candidate.get('display_cost')
        if not _is_int(cost) or cost <= 0:
            raise ValueError('candidate_cost_invalid')
        spent += cost
        if spent > initial_points:
            raise ValueError('skill_point_budget_exceeded')
        evidence = _primary_evidence(candidate)
        selected_items.append({'action_id': action, 'skill_id': action, 'display_name': candidate.get('display_name'), 'skill_type': candidate.get('skill_type'), 'rarity': candidate.get('rarity'), 'display_cost': cost, 'observed_hint_level': candidate.get('observed_hint_level'), 'observed_discount_percent': candidate.get('observed_discount_percent'), 'recognition_confidence': candidate.get('recognition_confidence'), 'source_page': evidence['source_page'], 'page_index': evidence['page_index'], 'row_index': evidence['row_index'], 'row_bbox': list(evidence['row_bbox']), 'cumulative_cost': spent, 'expected_remaining_skill_points': initial_points - spent, 'reasons': list(candidate.get('reasons', []))})
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in selected_items:
        grouped[item['page_index']].append(item)
    page_groups = [{'page_index': page_index, 'source_page': items[0]['source_page'], 'items': items} for page_index, items in sorted(grouped.items())]
    plan: dict[str, Any] = {'schema_version': 'uma.skill_batch_plan.v1', 'inventory_packet_sha256': inventory['packet_sha256'], 'decision_source': decision_source, 'currency_scope': 'skill_points_only', 'execution_allowed': False, 'captured_at_unix_ms': inventory['captured_at_unix_ms'], 'expires_at_unix_ms': inventory['expires_at_unix_ms'], 'selected_action_ids': actions, 'items': selected_items, 'page_groups': page_groups, 'initial_skill_points': initial_points, 'spent_skill_points': spent, 'expected_remaining_skill_points': initial_points - spent, 'contained_action_ids': contained_lower_ids, 'contained_lower_cost_saved': contained_cost_saved, 'resource_checks': {'uses_skill_points_only': True, 'uses_carrots': False, 'uses_clocks': False, 'uses_paid_currency': False}}
    if reverse_scroll_shifts is not None:
        plan['reverse_scroll_shifts'] = list(reverse_scroll_shifts)
    plan['plan_sha256'] = _canonical_plan_sha256(plan)
    plan_valid, plan_reason = validate_skill_batch_plan(plan, inventory)
    if not plan_valid:
        raise ValueError(plan_reason)
    return plan

def validate_skill_batch_plan(plan: Mapping[str, Any], inventory: Mapping[str, Any]) -> tuple[bool, str]:
    inventory_valid, inventory_reason = validate_skill_inventory_packet(inventory)
    if not inventory_valid:
        return (False, f'inventory_invalid:{inventory_reason}')
    if plan.get('schema_version') != 'uma.skill_batch_plan.v1':
        return (False, 'schema_version_mismatch')
    if plan.get('inventory_packet_sha256') != inventory.get('packet_sha256'):
        return (False, 'inventory_sha_mismatch')
    if plan.get('decision_source') not in _DECISION_SOURCES:
        return (False, 'decision_source_invalid')
    if plan.get('currency_scope') != 'skill_points_only':
        return (False, 'currency_scope_mismatch')
    if plan.get('captured_at_unix_ms') != inventory.get('captured_at_unix_ms') or plan.get('expires_at_unix_ms') != inventory.get('expires_at_unix_ms'):
        return (False, 'plan_capture_timing_mismatch')
    if plan.get('execution_allowed') is not False:
        return (False, 'dry_run_execution_flag_invalid')
    if not _valid_sha(plan.get('plan_sha256')):
        return (False, 'plan_sha_invalid')
    try:
        if plan['plan_sha256'] != _canonical_plan_sha256(plan):
            return (False, 'plan_sha_mismatch')
    except (TypeError, ValueError):
        return (False, 'plan_not_canonical')
    actions = plan.get('selected_action_ids')
    items = plan.get('items')
    if not isinstance(actions, list) or any((not isinstance(action, str) or not action for action in actions)) or len(actions) != len(set(actions)) or (not isinstance(items, list)) or ([item.get('action_id') for item in items if isinstance(item, Mapping)] != actions):
        return (False, 'plan_actions_invalid')
    if any((action not in inventory['allowed_action_ids'] for action in actions)):
        return (False, 'action_id_not_allowed')
    spent = 0
    for item in items:
        if not isinstance(item, Mapping):
            return (False, 'plan_item_invalid')
        cost = item.get('display_cost')
        if not _is_int(cost) or cost <= 0:
            return (False, 'plan_item_cost_invalid')
        spent += cost
        if item.get('cumulative_cost') != spent or item.get('expected_remaining_skill_points') != inventory['current_skill_points'] - spent:
            return (False, 'plan_economics_inconsistent')
    if plan.get('initial_skill_points') != inventory['current_skill_points'] or plan.get('spent_skill_points') != spent or plan.get('expected_remaining_skill_points') != inventory['current_skill_points'] - spent or (spent > inventory['current_skill_points']):
        return (False, 'plan_economics_inconsistent')
    flattened = [item.get('action_id') for group in plan.get('page_groups', []) if isinstance(group, Mapping) for item in group.get('items', []) if isinstance(item, Mapping)]
    if sorted(flattened) != sorted(actions):
        return (False, 'page_groups_incomplete')
    if 'reverse_scroll_shifts' in plan:
        shifts = plan['reverse_scroll_shifts']
        page_count = len(inventory['capture_binding']['page_hashes'])
        if not isinstance(shifts, list) or len(shifts) != page_count - 1 or any((type(shift) not in (int, float) or not 1 <= shift <= 386 for shift in shifts)):
            return (False, 'reverse_scroll_shifts_invalid')
        prior_page = -1
        item_by_id = {item['action_id']: item for item in items}
        for group in plan['page_groups']:
            page = group.get('page_index')
            if type(page) is not int or not prior_page < page < page_count or any((item != item_by_id[item['action_id']] or item.get('page_index') != page for item in group['items'])):
                return (False, 'reverse_page_groups_invalid')
            prior_page = page
    if plan.get('resource_checks') != inventory.get('resource_checks'):
        return (False, 'resource_checks_invalid')
    return (True, 'ok')

def skill_selection_groups(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Selection may run backwards; confirmation keeps canonical source order."""
    groups = list(plan.get('page_groups', []))
    return list(reversed(groups)) if 'reverse_scroll_shifts' in plan else groups
__all__ = ['build_skill_batch_plan', 'build_skill_inventory_packet', 'canonical_packet_sha256', 'project_ai_skill_actions', 'select_local_skill_action_ids', 'skill_selection_groups', 'validate_skill_batch_plan', 'validate_skill_inventory_packet']
