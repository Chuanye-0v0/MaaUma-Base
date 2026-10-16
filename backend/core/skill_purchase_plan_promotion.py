"""Current-run-scoped promotion for a final skill purchase plan.

The promoter is deliberately narrow.  It corroborates one already parsed row
and emits a U1-bound plan without changing global catalog verification state.
It performs no OCR, ADB, file I/O, or device action.
"""
from __future__ import annotations
import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Mapping
TARGET_SKILL_ID = 'tempo_up'
TARGET_NAME = '加快节奏'
TARGET_COST = 112
INITIAL_SKILL_POINTS = 2119
EXPECTED_REMAINING = INITIAL_SKILL_POINTS - TARGET_COST
MIN_IDENTITY_CONFIDENCE = 0.9
GENERIC_MIN_IDENTITY_CONFIDENCE = 0.88
GENERIC_MIN_IDENTITY_MARGIN = 0.1
GENERIC_MIN_IDENTITY_DIMENSIONS = 3
GENERIC_HIGH_MARGIN_IDENTITY_CONFIDENCE = 0.82
GENERIC_HIGH_MARGIN_IDENTITY_MARGIN = 0.5
GENERIC_EXACT_NAME_REBIND_CONFIDENCE = 0.8
CATALOG_ALIAS_MIN_OCR_CONFIDENCE = 0.45
CATALOG_ALIAS_MIN_IDENTITY_MARGIN = 0.25
CURRENT_RUN_FUZZY_MIN_OCR_CONFIDENCE = 0.25
CURRENT_RUN_FUZZY_REQUIRED_EVIDENCE = {'name', 'cost', 'rarity', 'icon_color'}
CATALOG_ALIAS_REQUIRED_EVIDENCE = {'name', 'description', 'cost', 'rarity', 'icon_color'}
NORMAL_EXTERNAL_RARITY = 'ノーマル'
GOLD_EXTERNAL_RARITY = 'レア'
SUPPORTED_EXTERNAL_RARITIES = {'normal': NORMAL_EXTERNAL_RARITY, 'gold': GOLD_EXTERNAL_RARITY}

def promote_current_final_skill_plan(*, report: Mapping[str, Any], build_context: Mapping[str, Any], external_skill_db: Mapping[str, Any], source_report_sha256: str, session_id: str, capture_batch_id: str) -> dict[str, Any]:
    """Return one fail-closed current-run plan plus reviewed-row binding."""
    _require_mapping(report, 'report')
    _require_mapping(build_context, 'build_context')
    _require_mapping(external_skill_db, 'external_skill_db')
    if not re.fullmatch('[0-9a-f]{64}', source_report_sha256):
        raise ValueError('source_report_sha256_invalid')
    if not isinstance(session_id, str) or not session_id:
        raise ValueError('session_id_missing')
    if not isinstance(capture_batch_id, str) or not capture_batch_id:
        raise ValueError('capture_batch_id_missing')
    race_profile = _required_mapping(build_context, 'race_profile')
    if build_context.get('target_mode') != 'parent':
        raise ValueError('target_mode_mismatch')
    if race_profile.get('distance_type') != 'middle':
        raise ValueError('build_distance_mismatch')
    if race_profile.get('style') != 'betweener':
        raise ValueError('build_style_mismatch')
    verdict = _required_mapping(report, 'verdict')
    if verdict.get('parser_verdict') != 'PARSER_READY':
        raise ValueError('parser_not_ready')
    if verdict.get('planner_verdict') != 'PLANNER_READY':
        raise ValueError('planner_not_ready')
    if verdict.get('capture_binding_accepted') is not True:
        raise ValueError('capture_binding_not_accepted')
    observation = _required_mapping(report, 'planner_observation')
    if observation.get('current_skill_pt') != INITIAL_SKILL_POINTS:
        raise ValueError('planner_skill_points_mismatch')
    if observation.get('pricing_contract') != 'display_cost_is_observed_final; do_not_apply_hint_discount_again':
        raise ValueError('pricing_contract_mismatch')
    candidates = observation.get('available_skills')
    if not isinstance(candidates, list):
        raise ValueError('available_skills_invalid')
    target_candidates = [item for item in candidates if isinstance(item, Mapping) and item.get('skill_id') == TARGET_SKILL_ID]
    if len(target_candidates) != 1 or len(candidates) != 1:
        raise ValueError('target_candidate_not_unique')
    candidate = target_candidates[0]
    if candidate.get('display_cost') != TARGET_COST:
        raise ValueError('target_cost_mismatch')
    if candidate.get('cost_basis') != 'observed_final':
        raise ValueError('target_cost_basis_mismatch')
    confidence = candidate.get('recognition_confidence')
    if not _confidence_at_least(confidence, MIN_IDENTITY_CONFIDENCE):
        raise ValueError('target_identity_confidence_low')
    if candidate.get('observed_hint_level') is not None:
        raise ValueError('target_hint_level_must_remain_unknown')
    if candidate.get('observed_discount_percent') != 30:
        raise ValueError('target_discount_mismatch')
    candidate_source = _unique_source(candidate)
    parser_manifest = _required_mapping(report, 'parser_manifest')
    if parser_manifest.get('current_skill_points') != INITIAL_SKILL_POINTS:
        raise ValueError('parser_skill_points_mismatch')
    parser_skills = parser_manifest.get('skills')
    if not isinstance(parser_skills, list):
        raise ValueError('parser_skills_invalid')
    matched_rows = [skill for skill in parser_skills if isinstance(skill, Mapping) and _has_matching_unique_source(skill, candidate_source)]
    if len(matched_rows) != 1:
        raise ValueError('parser_row_not_unique')
    row = matched_rows[0]
    _validate_target_row(row)
    corroboration = _find_corroboration(external_skill_db)
    fingerprint = row.get('row_fingerprint')
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError('row_fingerprint_missing')
    name_confidence = row.get('confidence')
    if not _confidence_at_least(name_confidence, MIN_IDENTITY_CONFIDENCE):
        raise ValueError('row_identity_confidence_low')
    field_confidence = _required_mapping(row, 'field_confidence')
    if not _confidence_at_least(field_confidence.get('cost'), 0.9):
        raise ValueError('row_cost_confidence_low')
    sample_id = f'current-final-skill-{source_report_sha256[:16]}'
    plan_seed = {'sample_id': sample_id, 'source_report_sha256': source_report_sha256, 'session_id': session_id, 'capture_batch_id': capture_batch_id, 'item_id': TARGET_SKILL_ID, 'cost': TARGET_COST}
    plan_id = 'u1-final-skill-' + _canonical_sha(plan_seed)[:20]
    must_satisfy_ocr = {'page_kind': 'skill_acquisition', 'screen_kind': 'skill_acquisition', 'row_complete': True, 'name_resolution_status': 'auto_confirmed', 'name_resolution_confidence_min': MIN_IDENTITY_CONFIDENCE, 'observed_name': TARGET_NAME, 'observed_final_cost': TARGET_COST, 'cost_confidence_min': 0.9, 'hint_level': None, 'discount_percent': 30, 'hint_confidence_min': 0.0, 'plus_available': True, 'skill_type': 'speed', 'rarity_or_icon_type': 'normal'}
    purchase = {'order': 1, 'item_id': TARGET_SKILL_ID, 'name': TARGET_NAME, 'type': 'speed', 'rarity': 'normal', 'observed_final_cost': TARGET_COST, 'expected_skill_points_after': EXPECTED_REMAINING, 'expected_selected_total_after': TARGET_COST, 'hint': {'level': None, 'discount_percent': 30}, 'must_satisfy_ocr': must_satisfy_ocr, 'source_report_sha256': source_report_sha256, 'source_row_fingerprint': fingerprint, 'current_run_scope_only': True}
    plan = {'schema_version': 'uma.skill_purchase_plan.v1', 'mode': 'live', 'execution_allowed': True, 'plan_id': plan_id, 'input': {'sample_id': sample_id, 'current_skill_points': INITIAL_SKILL_POINTS, 'source_report_sha256': source_report_sha256}, 'policy': {'currency_scope': 'skill_points_only', 'allow_fallback_skill': False, 'allow_partial_confirmation': False, 'dangerous_resource_flags': []}, 'preflight_must_satisfy_ocr': {'page_kind': 'skill_acquisition', 'current_skill_points': INITIAL_SKILL_POINTS, 'current_skill_points_confidence_min': 0.95, 'planned_selection_count': 1}, 'purchases': [purchase], 'spent_skill_points': TARGET_COST, 'expected_remaining_skill_points': EXPECTED_REMAINING, 'execution_promotion': {'promoted_by': 'U1', 'plan_id': plan_id, 'sample_id': sample_id, 'session_id': session_id, 'fresh_capture_id': capture_batch_id, 'current_skill_points': INITIAL_SKILL_POINTS, 'planned_selection_count': 1, 'current_run_scope_only': True}}
    review = {'schema_version': 'uma.skill_purchase_current_run_review.v1', 'plan_id': plan_id, 'sample_id': sample_id, 'session_id': session_id, 'capture_batch_id': capture_batch_id, 'reviewed_rows': {fingerprint: {'item_id': TARGET_SKILL_ID, 'observed_name': TARGET_NAME, 'name_resolution_confidence': float(name_confidence), 'rarity_or_icon_type': 'normal', 'skill_type': 'speed', 'hint_level': None, 'discount_percent': 30, 'hint_confidence': 0.0, 'observed_final_cost': TARGET_COST, 'required_description_token': '中距离', 'expected_initial_skill_points': INITIAL_SKILL_POINTS, 'expected_remaining_skill_points': EXPECTED_REMAINING, 'expected_selected_total': TARGET_COST, 'current_run_scope_only': True, 'source_report_sha256': source_report_sha256}}}
    evidence = {'schema_version': 'uma.skill_purchase_promotion_evidence.v1', 'plan_id': plan_id, 'source_report_sha256': source_report_sha256, 'build_context': deepcopy(build_context), 'external_corroboration': {'name': corroboration['name'], 'rare': corroboration['rare'], 'db_id': corroboration['db_id'], 'icon_id': corroboration['icon_id'], 'type': corroboration.get('type'), 'authority': 'corroborating_only'}, 'economics': {'initial_skill_points': INITIAL_SKILL_POINTS, 'spend': TARGET_COST, 'expected_remaining': EXPECTED_REMAINING, 'cost_basis': 'observed_final'}, 'resource_checks': {'uses_skill_points_only': True, 'uses_carrots': False, 'uses_clocks': False, 'uses_paid_currency': False}}
    return {'plan': plan, 'review': review, 'evidence': evidence}

def promote_planner_final_skill_plan(*, report: Mapping[str, Any], build_context: Mapping[str, Any], external_skill_db: Mapping[str, Any], source_report_sha256: str, session_id: str, capture_batch_id: str) -> dict[str, Any]:
    """Promote every safe planner purchase with per-row current-run bindings.

    This generic path deliberately supports only normal-rarity rows for which
    the planner emitted verified type/rarity metadata.  The APK database is
    identity corroboration only; its opaque ``type`` value is never converted
    into planner semantics.
    """
    _require_mapping(report, 'report')
    _require_mapping(build_context, 'build_context')
    _require_mapping(external_skill_db, 'external_skill_db')
    if not isinstance(source_report_sha256, str) or not re.fullmatch('[0-9a-f]{64}', source_report_sha256):
        raise ValueError('source_report_sha256_invalid')
    if not isinstance(session_id, str) or not session_id:
        raise ValueError('session_id_missing')
    if not isinstance(capture_batch_id, str) or not capture_batch_id:
        raise ValueError('capture_batch_id_missing')
    race_profile = _required_mapping(build_context, 'race_profile')
    report_inventory = report.get('inventory_packet')
    if isinstance(report_inventory, Mapping):
        if report_inventory.get('build_context') != dict(build_context):
            raise ValueError('build_context_inventory_mismatch')
        if build_context.get('target_mode') not in {'parent', 'race'}:
            raise ValueError('target_mode_mismatch')
    elif build_context.get('target_mode') != 'parent':
        raise ValueError('target_mode_mismatch')
    if not isinstance(report_inventory, Mapping) and race_profile.get('distance_type') != 'middle':
        raise ValueError('build_distance_mismatch')
    if not isinstance(report_inventory, Mapping) and race_profile.get('style') != 'betweener':
        raise ValueError('build_style_mismatch')
    if not isinstance(race_profile.get('distance_type'), str) or not race_profile['distance_type']:
        raise ValueError('build_distance_mismatch')
    if not isinstance(race_profile.get('style'), str) or not race_profile['style']:
        raise ValueError('build_style_mismatch')
    verdict = _required_mapping(report, 'verdict')
    if verdict.get('parser_verdict') != 'PARSER_READY':
        raise ValueError('parser_not_ready')
    if verdict.get('planner_verdict') != 'PLANNER_READY':
        raise ValueError('planner_not_ready')
    if verdict.get('capture_binding_accepted') is not True:
        raise ValueError('capture_binding_not_accepted')
    if report.get('build_context_provided') is not True:
        raise ValueError('build_context_not_provided')
    observation = _required_mapping(report, 'planner_observation')
    initial_points = observation.get('current_skill_pt')
    if not _is_positive_int(initial_points):
        raise ValueError('planner_skill_points_invalid')
    if verdict.get('current_skill_points') != initial_points:
        raise ValueError('verdict_skill_points_mismatch')
    if observation.get('pricing_contract') != 'display_cost_is_observed_final; do_not_apply_hint_discount_again':
        raise ValueError('pricing_contract_mismatch')
    candidates = observation.get('available_skills')
    if not isinstance(candidates, list):
        raise ValueError('available_skills_invalid')
    parser_manifest = _required_mapping(report, 'parser_manifest')
    if parser_manifest.get('current_skill_points') != initial_points:
        raise ValueError('parser_skill_points_mismatch')
    parser_rows = parser_manifest.get('skills')
    if not isinstance(parser_rows, list):
        raise ValueError('parser_skills_invalid')
    dry_run = _required_mapping(report, 'planner_dry_run')
    if dry_run.get('executed') is not True or dry_run.get('status') != 'ok':
        raise ValueError('planner_dry_run_not_ready')
    if dry_run.get('target_mode') != build_context.get('target_mode'):
        raise ValueError('planner_target_mode_mismatch')
    if dry_run.get('trigger') != 'final_build':
        raise ValueError('planner_trigger_mismatch')
    if dry_run.get('risk_flags') != []:
        raise ValueError('planner_risk_flags_present')
    _validate_resource_checks(_required_mapping(dry_run, 'resource_checks'))
    planned = dry_run.get('purchases')
    if not isinstance(planned, list) or not planned:
        raise ValueError('planner_purchases_missing')
    if not all((isinstance(item, Mapping) for item in planned)):
        raise ValueError('planner_purchase_invalid')

    def execution_order_key(purchase: Mapping[str, Any]) -> tuple[int, int, str]:
        item_id = _required_nonempty_string(purchase, 'skill_id')
        candidate = _select_unique_planner_candidate(candidates, item_id)
        source, _row = _select_complete_source_row(candidate, parser_rows, item_id)
        return (source['page_index'], source['row_index'], item_id)
    planned = sorted(planned, key=execution_order_key)
    reviewed_rows: dict[str, dict[str, Any]] = {}
    live_purchases: list[dict[str, Any]] = []
    corroborations: list[dict[str, Any]] = []
    selected_total = 0
    seen_ids: set[str] = set()
    seen_fingerprints: set[str] = set()
    seen_sources: set[str] = set()
    all_names_directly_observed = True
    all_names_live_bindable = True
    for order, purchase in enumerate(planned, start=1):
        item_id = _required_nonempty_string(purchase, 'skill_id')
        name = _required_nonempty_string(purchase, 'name')
        skill_type = _required_nonempty_string(purchase, 'type')
        rarity = _required_nonempty_string(purchase, 'rarity')
        if rarity not in SUPPORTED_EXTERNAL_RARITIES:
            raise ValueError(f'unsupported_purchase_rarity:{item_id}')
        if purchase.get('risk_flags') != []:
            raise ValueError(f'purchase_risk_flags_present:{item_id}')
        if item_id in seen_ids:
            raise ValueError('planner_purchase_id_duplicate')
        seen_ids.add(item_id)
        cost = purchase.get('final_cost')
        if not _is_positive_int(cost) or purchase.get('display_cost') != cost:
            raise ValueError(f'planner_purchase_cost_invalid:{item_id}')
        candidate = _select_unique_planner_candidate(candidates, item_id)
        if candidate.get('display_cost') != cost or candidate.get('cost_basis') != 'observed_final':
            raise ValueError(f'planner_candidate_cost_mismatch:{item_id}')
        binding = _required_mapping(candidate, 'identity_binding')
        _validate_identity_binding(binding, item_id)
        candidate_confidence = candidate.get('recognition_confidence')
        standard_candidate_confidence = _confidence_at_least(candidate_confidence, GENERIC_MIN_IDENTITY_CONFIDENCE)
        high_margin_candidate_confidence = _confidence_at_least(candidate_confidence, GENERIC_HIGH_MARGIN_IDENTITY_CONFIDENCE) and _high_margin_identity_binding(binding)
        if not (standard_candidate_confidence or high_margin_candidate_confidence):
            raise ValueError(f'planner_candidate_confidence_low:{item_id}')
        source_binding, row = _select_complete_source_row(candidate, parser_rows, item_id)
        source_key = _canonical_sha(source_binding)
        if source_key in seen_sources:
            raise ValueError('planner_purchase_source_duplicate')
        seen_sources.add(source_key)
        _validate_generic_row(row, purchase, candidate)
        direct_name = row.get('name')
        observed_name_aliases = _reviewed_name_aliases(row)
        catalog_aliases = _reviewed_name_aliases(row, min_confidence=CATALOG_ALIAS_MIN_OCR_CONFIDENCE)
        current_run_fuzzy_aliases = _reviewed_name_aliases(row, min_confidence=CURRENT_RUN_FUZZY_MIN_OCR_CONFIDENCE)
        current_run_multi_evidence = _current_run_multi_evidence_identity_binding(binding)
        if current_run_multi_evidence:
            observed_name = name
            observed_name_aliases = current_run_fuzzy_aliases
            name_binding_source = 'current_run_multi_evidence'
            all_names_directly_observed = False
        elif isinstance(direct_name, str) and direct_name:
            observed_name = direct_name
            name_binding_source = 'direct_parser_name'
        elif _catalog_corroborated_alias_binding(binding) and catalog_aliases:
            observed_name = name
            observed_name_aliases = catalog_aliases
            name_binding_source = 'catalog_corroborated_ocr_alias'
            all_names_directly_observed = False
        elif _high_margin_identity_binding(binding) and observed_name_aliases:
            observed_name = name
            name_binding_source = 'unique_high_margin_resolver'
            all_names_directly_observed = False
        elif _current_run_fuzzy_identity_binding(binding) and current_run_fuzzy_aliases:
            observed_name = name
            observed_name_aliases = current_run_fuzzy_aliases
            name_binding_source = 'current_run_fuzzy_name_description'
            all_names_directly_observed = False
        else:
            observed_name = name
            name_binding_source = 'strong_identity_binding_fallback'
            all_names_directly_observed = False
            all_names_live_bindable = False
        fingerprint = _required_nonempty_string(row, 'row_fingerprint')
        if fingerprint in seen_fingerprints:
            raise ValueError('parser_row_fingerprint_duplicate')
        seen_fingerprints.add(fingerprint)
        description_token = _description_binding_token(row, item_id)
        description = _required_nonempty_string(row, 'description')
        description_sha256 = hashlib.sha256(description.encode('utf-8')).hexdigest()
        corroboration = _find_generic_corroboration(external_skill_db, name=name, rarity=rarity, allow_inherited_unique_rarity=item_id.endswith('_inherited'))
        if name_binding_source == 'catalog_corroborated_ocr_alias' and corroboration.get('match_status') not in {'exact_unicode_name_and_rarity_candidate', 'exact_unicode_name_and_inherited_rarity_candidate'}:
            corroboration = _find_generic_alias_corroboration(external_skill_db, aliases=[alias['text'] for alias in observed_name_aliases if isinstance(alias.get('text'), str)], rarity=rarity)
        if name_binding_source in {'unique_high_margin_resolver', 'catalog_corroborated_ocr_alias', 'current_run_fuzzy_name_description', 'current_run_multi_evidence'} and corroboration.get('match_status') not in {'exact_unicode_name_and_rarity_candidate', 'exact_unicode_name_and_inherited_rarity_candidate', 'exact_ocr_alias_and_rarity_candidate'}:
            raise ValueError(f'resolver_external_corroboration_missing:{item_id}')
        selected_total += int(cost)
        remaining = int(initial_points) - selected_total
        if remaining < 0:
            raise ValueError('planner_spend_exceeds_skill_points')
        observed_hint = candidate.get('observed_hint_level')
        observed_discount = candidate.get('observed_discount_percent')
        purchase_binding = {'item_id': item_id, 'catalog_name': name, 'observed_name': observed_name, 'name_binding_source': name_binding_source, 'observed_name_aliases': observed_name_aliases, 'description_sha256': description_sha256, 'source_binding': source_binding, 'source_row_fingerprint': fingerprint, 'source_report_sha256': source_report_sha256, 'skill_type': skill_type, 'rarity': rarity, 'observed_final_cost': int(cost), 'expected_selected_total_after': selected_total, 'expected_skill_points_after': remaining}
        purchase_binding_sha256 = _canonical_sha(purchase_binding)
        must_satisfy_ocr = {'page_kind': 'skill_acquisition', 'screen_kind': 'skill_acquisition', 'row_complete': True, 'name_resolution_status': 'auto_confirmed', 'name_resolution_confidence_min': (CATALOG_ALIAS_MIN_OCR_CONFIDENCE if name_binding_source == 'catalog_corroborated_ocr_alias' else CURRENT_RUN_FUZZY_MIN_OCR_CONFIDENCE if name_binding_source in {'current_run_fuzzy_name_description', 'current_run_multi_evidence'} else GENERIC_EXACT_NAME_REBIND_CONFIDENCE) if name_binding_source in {'unique_high_margin_resolver', 'catalog_corroborated_ocr_alias', 'current_run_fuzzy_name_description', 'current_run_multi_evidence'} else GENERIC_MIN_IDENTITY_CONFIDENCE, 'observed_name': observed_name, 'observed_final_cost': int(cost), 'cost_confidence_min': 0.9, 'hint_level': observed_hint, 'discount_percent': observed_discount, 'hint_confidence_min': 0.0, 'plus_available': True, 'skill_type': skill_type, 'rarity_or_icon_type': rarity}
        live_purchases.append({'order': order, 'item_id': item_id, 'name': name, 'type': skill_type, 'rarity': rarity, 'observed_final_cost': int(cost), 'expected_skill_points_after': remaining, 'expected_selected_total_after': selected_total, 'hint': {'level': observed_hint, 'discount_percent': observed_discount}, 'must_satisfy_ocr': must_satisfy_ocr, 'source_report_sha256': source_report_sha256, 'source_row_fingerprint': fingerprint, 'purchase_binding_sha256': purchase_binding_sha256, 'purchase_binding': purchase_binding, 'name_binding_source': name_binding_source, 'observed_name_aliases': observed_name_aliases, 'current_run_scope_only': True})
        reviewed_rows[fingerprint] = {'item_id': item_id, 'observed_name': observed_name, 'name_binding_source': name_binding_source, 'observed_name_aliases': observed_name_aliases, 'name_resolution_confidence': float(binding['confidence']), 'name_resolution_margin': float(binding['margin']), 'name_resolution_evidence_dimensions': int(binding['evidence_dimensions']), 'name_resolution_evidence': deepcopy(binding.get('evidence', {})), **({'current_run_contract': deepcopy(binding['current_run_contract'])} if current_run_multi_evidence else {}), 'rarity_or_icon_type': rarity, 'skill_type': skill_type, 'hint_level': observed_hint, 'discount_percent': observed_discount, 'hint_confidence': _review_hint_confidence(row), 'observed_final_cost': int(cost), 'required_description_token': description_token, 'observed_description': description, 'description_sha256': description_sha256, 'source_binding': source_binding, 'source_row_fingerprint': fingerprint, 'purchase_binding_sha256': purchase_binding_sha256, 'expected_initial_skill_points': int(initial_points), 'expected_remaining_skill_points': remaining, 'expected_selected_total': selected_total, 'current_run_scope_only': True, 'source_report_sha256': source_report_sha256}
        corroborations.append({'item_id': item_id, **corroboration, 'authority': 'candidate_only'})
    if dry_run.get('spent_skill_pt') != selected_total:
        raise ValueError('planner_spend_mismatch')
    if dry_run.get('remaining_skill_pt') != int(initial_points) - selected_total:
        raise ValueError('planner_remaining_mismatch')
    sample_id = f'current-final-skills-{source_report_sha256[:16]}'
    plan_seed = {'sample_id': sample_id, 'source_report_sha256': source_report_sha256, 'session_id': session_id, 'capture_batch_id': capture_batch_id, 'purchases': [(item['item_id'], item['observed_final_cost']) for item in live_purchases]}
    plan_id = 'u1-final-skills-' + _canonical_sha(plan_seed)[:20]
    count = len(live_purchases)
    if count > 1:
        multi_skill_gate = 'real_multi_skill_confirmation_and_success_fixture_missing'
    elif not all_names_live_bindable:
        multi_skill_gate = 'direct_name_observation_missing'
    else:
        multi_skill_gate = 'single_skill_fixture_supported'
    capture_sources = [review['source_binding'] for review in reviewed_rows.values()]
    capture_same_frame = len({(source['source_page'], source['page_index']) for source in capture_sources}) == 1
    capture_bboxes_non_overlapping = all((not _bboxes_overlap(left['row_bbox'], right['row_bbox']) for index, left in enumerate(capture_sources) for right in capture_sources[index + 1:]))
    capture_allowed = count == 2 and all_names_directly_observed and capture_same_frame and capture_bboxes_non_overlapping
    if count != 2:
        capture_gate_reason = 'capture_requires_exactly_two_skills'
    elif not all_names_directly_observed:
        capture_gate_reason = 'direct_name_observation_missing'
    elif not capture_same_frame:
        capture_gate_reason = 'planned_rows_not_same_frame'
    elif not capture_bboxes_non_overlapping:
        capture_gate_reason = 'planned_row_bboxes_overlap'
    else:
        capture_gate_reason = 'same_frame_two_skill_capture_ready'
    plan = {'schema_version': 'uma.skill_purchase_plan.v1', 'mode': 'live', 'execution_allowed': count == 1 and all_names_live_bindable, 'confirmation_fixture_capture': {'allowed': capture_allowed, 'gate_reason': capture_gate_reason, 'stage': 'multi_confirmation_capture_probe', 'action_id': 'skill_purchase.capture_multi_confirmation', 'required_purchase_count': count, 'visibility_scope': 'single_fresh_frame_only', 'terminal_state': 'confirmation_dialog_screenshot_before_final_confirm', 'final_confirmation_allowed': False, 'terminal_handoff_required': True, 'source_report_sha256': source_report_sha256, 'purchase_binding_sha256': [purchase['purchase_binding_sha256'] for purchase in live_purchases]}, 'multi_skill_live_gate': multi_skill_gate, 'plan_id': plan_id, 'input': {'sample_id': sample_id, 'current_skill_points': int(initial_points), 'source_report_sha256': source_report_sha256}, 'policy': {'currency_scope': 'skill_points_only', 'allow_fallback_skill': False, 'allow_partial_confirmation': False, 'dangerous_resource_flags': []}, 'preflight_must_satisfy_ocr': {'page_kind': 'skill_acquisition', 'current_skill_points': int(initial_points), 'current_skill_points_confidence_min': 0.95, 'planned_selection_count': count}, 'purchases': live_purchases, 'spent_skill_points': selected_total, 'expected_remaining_skill_points': int(initial_points) - selected_total, 'execution_promotion': {'promoted_by': 'U1', 'plan_id': plan_id, 'sample_id': sample_id, 'session_id': session_id, 'fresh_capture_id': capture_batch_id, 'current_skill_points': int(initial_points), 'planned_selection_count': count, 'current_run_scope_only': True}}
    review = {'schema_version': 'uma.skill_purchase_current_run_review.v1', 'plan_id': plan_id, 'sample_id': sample_id, 'session_id': session_id, 'capture_batch_id': capture_batch_id, 'reviewed_rows': reviewed_rows}
    evidence = {'schema_version': 'uma.skill_purchase_promotion_evidence.v2', 'plan_id': plan_id, 'source_report_sha256': source_report_sha256, 'build_context': deepcopy(build_context), 'external_corroboration': corroborations, 'economics': {'initial_skill_points': int(initial_points), 'spend': selected_total, 'expected_remaining': int(initial_points) - selected_total, 'cost_basis': 'observed_final'}, 'resource_checks': {'uses_skill_points_only': True, 'uses_carrots': False, 'uses_clocks': False, 'uses_paid_currency': False}}
    return {'plan': plan, 'review': review, 'evidence': evidence}

def _validate_identity_binding(binding: Mapping[str, Any], item_id: str) -> None:
    if binding.get('status') != 'auto_confirmed':
        raise ValueError(f'identity_binding_status_invalid:{item_id}')
    standard_binding = _confidence_at_least(binding.get('confidence'), GENERIC_MIN_IDENTITY_CONFIDENCE) and _confidence_at_least(binding.get('margin'), GENERIC_MIN_IDENTITY_MARGIN)
    if not (standard_binding or _high_margin_identity_binding(binding) or _current_run_multi_evidence_identity_binding(binding)):
        raise ValueError(f'identity_binding_confidence_low:{item_id}')
    dimensions = binding.get('evidence_dimensions')
    if not isinstance(dimensions, int) or isinstance(dimensions, bool) or dimensions < GENERIC_MIN_IDENTITY_DIMENSIONS:
        raise ValueError(f'identity_binding_dimensions_low:{item_id}')

def _high_margin_identity_binding(binding: Mapping[str, Any]) -> bool:
    dimensions = binding.get('evidence_dimensions')
    return binding.get('status') == 'auto_confirmed' and _confidence_at_least(binding.get('confidence'), GENERIC_HIGH_MARGIN_IDENTITY_CONFIDENCE) and _confidence_at_least(binding.get('margin'), GENERIC_HIGH_MARGIN_IDENTITY_MARGIN) and isinstance(dimensions, int) and (not isinstance(dimensions, bool)) and (dimensions >= GENERIC_MIN_IDENTITY_DIMENSIONS)

def _catalog_corroborated_alias_binding(binding: Mapping[str, Any]) -> bool:
    evidence = binding.get('evidence')
    return binding.get('status') == 'auto_confirmed' and _confidence_at_least(binding.get('confidence'), 0.95) and _confidence_at_least(binding.get('margin'), CATALOG_ALIAS_MIN_IDENTITY_MARGIN) and isinstance(binding.get('evidence_dimensions'), int) and (not isinstance(binding.get('evidence_dimensions'), bool)) and (int(binding['evidence_dimensions']) >= len(CATALOG_ALIAS_REQUIRED_EVIDENCE)) and isinstance(evidence, Mapping) and all((_confidence_at_least(evidence.get(key), 0.95) for key in CATALOG_ALIAS_REQUIRED_EVIDENCE))

def _current_run_fuzzy_identity_binding(binding: Mapping[str, Any]) -> bool:
    evidence = binding.get('evidence')
    return binding.get('status') == 'auto_confirmed' and _confidence_at_least(binding.get('confidence'), 0.95) and _confidence_at_least(binding.get('margin'), 0.25) and isinstance(binding.get('evidence_dimensions'), int) and (not isinstance(binding.get('evidence_dimensions'), bool)) and (int(binding['evidence_dimensions']) >= len(CURRENT_RUN_FUZZY_REQUIRED_EVIDENCE)) and isinstance(evidence, Mapping) and all((_confidence_at_least(evidence.get(key), 0.94) for key in CURRENT_RUN_FUZZY_REQUIRED_EVIDENCE))

def _current_run_multi_evidence_identity_binding(binding: Mapping[str, Any]) -> bool:
    evidence = binding.get('evidence')
    contract = binding.get('current_run_contract')
    local_db_id = contract.get('local_db_id') if isinstance(contract, Mapping) else None
    dimensions = binding.get('evidence_dimensions')
    return binding.get('status') == 'auto_confirmed' and binding.get('binding_policy') == 'current_run_multi_evidence' and _confidence_at_least(binding.get('confidence'), 0.7) and isinstance(dimensions, int) and (not isinstance(dimensions, bool)) and (dimensions in {3, 4}) and isinstance(evidence, Mapping) and (evidence.get('cost') == 1.0) and (evidence.get('rarity') == 1.0) and (evidence.get('icon_color') == 1.0) and isinstance(contract, Mapping) and (contract.get('version') == 'current_run_multi_evidence.v1') and isinstance(local_db_id, str) and local_db_id.isdigit() and (contract.get('identity_status') in {'manual_verified', 'exact_local_db_id_join'}) and (contract.get('metadata_status') in {'manual_verified', 'external_cross_checked'}) and (contract.get('description_anchor_exact') is True or contract.get('name_score_threshold_met') is True)

def _validate_generic_row(row: Mapping[str, Any], purchase: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
    item_id = str(purchase.get('skill_id'))
    exact = {'rarity': purchase.get('rarity'), 'acquired': False, 'hint_level': candidate.get('observed_hint_level'), 'discount_percent': candidate.get('observed_discount_percent'), 'cost': purchase.get('final_cost'), 'plus_available': True, 'minus_state': 'disabled'}
    for field, expected in exact.items():
        if row.get(field) != expected:
            raise ValueError(f'parser_row_{field}_mismatch:{item_id}')
    unknown = row.get('unknown_reason')
    allowed_unknown = {'name_low_confidence_requires_review'}
    if not isinstance(unknown, list) or any((reason not in allowed_unknown for reason in unknown)):
        raise ValueError(f'parser_row_has_unsafe_unknown:{item_id}')
    field_confidence = _required_mapping(row, 'field_confidence')
    if not _confidence_at_least(field_confidence.get('structure'), 0.9):
        raise ValueError(f'parser_row_structure_confidence_low:{item_id}')
    if not _confidence_at_least(field_confidence.get('cost'), 0.9):
        raise ValueError(f'parser_row_cost_confidence_low:{item_id}')

def _description_binding_token(row: Mapping[str, Any], item_id: str) -> str:
    description = row.get('description')
    if not isinstance(description, str):
        raise ValueError(f'parser_row_description_missing:{item_id}')
    compact = re.sub('\\s+', '', description)
    matches = re.findall('<([^<>]{2,40})>', compact)
    if matches:
        return matches[-1]
    semantic = re.sub('[\\s，,。．.!！?？:：;；]+', '', description)
    if not 4 <= len(semantic) <= 80 or not re.search('[\\u4e00-\\u9fff]', semantic):
        raise ValueError(f'parser_row_description_token_missing:{item_id}')
    return semantic

def _review_hint_confidence(row: Mapping[str, Any]) -> float:
    value = _required_mapping(row, 'field_confidence').get('hint')
    return float(value) if _confidence_at_least(value, 0.0) else 0.0

def _reviewed_name_aliases(row: Mapping[str, Any], *, min_confidence: float=0.6) -> list[dict[str, Any]]:
    ocr_raw = row.get('ocr_raw')
    candidates = ocr_raw.get('name_candidates') if isinstance(ocr_raw, Mapping) else None
    if not isinstance(candidates, list):
        return []
    aliases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        text = candidate.get('text')
        confidence = candidate.get('confidence')
        if not isinstance(text, str) or not text or text in seen or (not _confidence_at_least(confidence, min_confidence)):
            continue
        seen.add(text)
        aliases.append({'text': text, 'confidence': round(float(confidence), 4)})
    return aliases

def _find_generic_corroboration(database: Mapping[str, Any], *, name: str, rarity: str, allow_inherited_unique_rarity: bool=False) -> Mapping[str, Any]:
    skills = database.get('skills')
    if not isinstance(skills, list):
        raise ValueError('external_skills_invalid')
    exact_name = [item for item in skills if isinstance(item, Mapping) and (item.get('name') == name or (allow_inherited_unique_rarity and item.get('name_zh_cn') == name and (item.get('identity_status') == 'manual_verified') and (item.get('metadata_status') == 'manual_verified')))]
    expected_external_rarity = SUPPORTED_EXTERNAL_RARITIES.get(rarity)
    if expected_external_rarity is None:
        raise ValueError(f'unsupported_external_rarity:{rarity}')
    matching_rarity = [item for item in exact_name if item.get('rare') == expected_external_rarity or (item.get('rarity') == rarity and item.get('identity_status') == 'exact_local_db_id_join' and (item.get('metadata_status') == 'external_cross_checked') and (item.get('requires_manual_verification') is False)) or (allow_inherited_unique_rarity and item.get('rarity') == rarity and (item.get('identity_status') == 'manual_verified') and (item.get('metadata_status') == 'manual_verified'))]
    if len(matching_rarity) == 1:
        match = matching_rarity[0]
        return {'match_status': 'exact_unicode_name_and_rarity_candidate', 'name': match.get('name'), 'rare': match.get('rare', match.get('rarity')), 'db_id': match.get('db_id') or (match.get('external_truth', {}).get('local_db_id') if isinstance(match.get('external_truth'), Mapping) else None) or match.get('skill_id'), 'icon_id': match.get('icon_id'), 'opaque_type': match.get('type'), 'candidate_count': 1}
    if len(matching_rarity) > 1:
        return {'match_status': 'ambiguous_exact_unicode_name', 'name': name, 'candidate_count': len(matching_rarity)}
    if allow_inherited_unique_rarity and rarity == 'normal' and (len(exact_name) == 1) and (exact_name[0].get('rare') == '固有'):
        match = exact_name[0]
        return {'match_status': 'exact_unicode_name_and_inherited_rarity_candidate', 'name': match.get('name'), 'rare': match.get('rare'), 'db_id': match.get('db_id'), 'icon_id': match.get('icon_id'), 'opaque_type': match.get('type'), 'candidate_count': 1}
    if exact_name:
        return {'match_status': 'rarity_conflict', 'name': name, 'candidate_count': len(exact_name)}
    return {'match_status': 'no_exact_unicode_name', 'name': name, 'candidate_count': 0}

def _find_generic_alias_corroboration(database: Mapping[str, Any], *, aliases: list[str], rarity: str) -> Mapping[str, Any]:
    skills = database.get('skills')
    if not isinstance(skills, list):
        raise ValueError('external_skills_invalid')
    expected_external_rarity = SUPPORTED_EXTERNAL_RARITIES.get(rarity)
    if expected_external_rarity is None:
        raise ValueError(f'unsupported_external_rarity:{rarity}')
    unique_aliases = {alias for alias in aliases if alias}
    matching = [item for item in skills if isinstance(item, Mapping) and item.get('name') in unique_aliases and (item.get('rare') == expected_external_rarity)]
    if len(matching) == 1:
        match = matching[0]
        return {'match_status': 'exact_ocr_alias_and_rarity_candidate', 'name': match.get('name'), 'rare': match.get('rare'), 'db_id': match.get('db_id'), 'icon_id': match.get('icon_id'), 'opaque_type': match.get('type'), 'candidate_count': 1}
    return {'match_status': 'ambiguous_exact_ocr_alias' if len(matching) > 1 else 'no_exact_ocr_alias', 'candidate_count': len(matching)}

def _validate_resource_checks(checks: Mapping[str, Any]) -> None:
    expected = {'uses_skill_points_only': True, 'uses_carrots': False, 'uses_clocks': False, 'uses_paid_currency': False}
    if any((checks.get(key) is not value for key, value in expected.items())):
        raise ValueError('planner_resource_checks_invalid')

def _required_nonempty_string(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f'required_string_missing:{key}')
    return value

def _normalized_source_binding(source: Mapping[str, Any], item_id: str) -> dict[str, Any]:
    source_page = source.get('source_page')
    page_index = source.get('page_index')
    row_index = source.get('row_index')
    row_bbox = source.get('row_bbox')
    if not isinstance(source_page, str) or not source_page:
        raise ValueError(f'source_page_invalid:{item_id}')
    if not isinstance(page_index, int) or isinstance(page_index, bool) or page_index < 0:
        raise ValueError(f'source_page_index_invalid:{item_id}')
    if not isinstance(row_index, int) or isinstance(row_index, bool) or row_index < 0:
        raise ValueError(f'source_row_index_invalid:{item_id}')
    if not isinstance(row_bbox, list) or len(row_bbox) != 4 or (not all((isinstance(value, int) and (not isinstance(value, bool)) for value in row_bbox))) or (row_bbox[2] <= row_bbox[0]) or (row_bbox[3] <= row_bbox[1]):
        raise ValueError(f'source_row_bbox_invalid:{item_id}')
    return {'source_page': source_page, 'page_index': page_index, 'row_index': row_index, 'row_bbox': list(row_bbox)}

def _bboxes_overlap(left: list[int], right: list[int]) -> bool:
    return min(left[2], right[2]) > max(left[0], right[0]) and min(left[3], right[3]) > max(left[1], right[1])

def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and (not isinstance(value, bool)) and (value > 0)

def _validate_target_row(row: Mapping[str, Any]) -> None:
    exact = {'name': TARGET_NAME, 'rarity': 'normal', 'acquired': False, 'hint_level': None, 'discount_percent': 30, 'cost': TARGET_COST, 'plus_available': True, 'minus_state': 'disabled'}
    for field, expected in exact.items():
        if row.get(field) != expected:
            raise ValueError(f'target_row_{field}_mismatch')
    unknown = row.get('unknown_reason')
    if not isinstance(unknown, list) or unknown:
        raise ValueError('target_row_has_unknown_reason')
    description = row.get('description')
    if not isinstance(description, str) or '中距离' not in re.sub('\\s+', '', description):
        raise ValueError('target_row_distance_description_mismatch')

def _find_corroboration(database: Mapping[str, Any]) -> Mapping[str, Any]:
    skills = database.get('skills')
    if not isinstance(skills, list):
        raise ValueError('external_skills_invalid')
    matches = [item for item in skills if isinstance(item, Mapping) and item.get('name') == TARGET_NAME and (item.get('rare') == 'ノーマル') and (item.get('db_id') == '200722') and (item.get('icon_id') == '20011') and (item.get('type') == 7)]
    if len(matches) != 1:
        raise ValueError('external_corroboration_mismatch')
    return matches[0]

def _unique_source(item: Mapping[str, Any]) -> Mapping[str, Any]:
    evidence = item.get('source_evidence')
    if not isinstance(evidence, list) or len(evidence) != 1:
        raise ValueError('source_evidence_not_unique')
    return _require_mapping(evidence[0], 'source_evidence_item')

def _select_unique_planner_candidate(candidates: list[Any], item_id: str) -> Mapping[str, Any]:
    """Coalesce only semantically identical overlap observations.

    Continuous-scroll capture may emit the same complete skill on adjacent
    pages.  Every field other than ``source_evidence`` must remain exactly
    equal; otherwise the ambiguity stays fail-closed.
    """
    matches = [candidate for candidate in candidates if isinstance(candidate, Mapping) and candidate.get('skill_id') == item_id]
    if not matches:
        raise ValueError(f'planner_candidate_not_unique:{item_id}')
    baseline = {key: value for key, value in matches[0].items() if key != 'source_evidence'}
    merged = deepcopy(dict(matches[0]))
    merged_evidence = merged.get('source_evidence')
    if not isinstance(merged_evidence, list) or not merged_evidence:
        raise ValueError(f'planner_candidate_source_invalid:{item_id}')
    for candidate in matches[1:]:
        semantic = {key: value for key, value in candidate.items() if key != 'source_evidence'}
        if semantic != baseline:
            raise ValueError(f'planner_candidate_conflict:{item_id}')
        evidence = candidate.get('source_evidence')
        if not isinstance(evidence, list) or not evidence:
            raise ValueError(f'planner_candidate_source_invalid:{item_id}')
        merged_evidence.extend(deepcopy(evidence))
    return merged

def _select_complete_source_row(item: Mapping[str, Any], parser_rows: list[Any], item_id: str) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """Choose one deterministic complete row from an overlapping capture.

    Overlap capture intentionally observes some skills on adjacent pages.  A
    fragment can never authorize a purchase; equivalent complete rows may.
    """
    evidence = item.get('source_evidence')
    if not isinstance(evidence, list) or not evidence:
        raise ValueError('source_evidence_not_unique')
    candidates: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
    seen_sources: set[str] = set()
    for raw in evidence:
        source = _require_mapping(raw, 'source_evidence_item')
        if source.get('fragment_only') is True or source.get('row_index') is None:
            continue
        normalized = _normalized_source_binding(source, item_id)
        source_sha = _canonical_sha(normalized)
        if source_sha in seen_sources:
            raise ValueError('source_evidence_duplicate')
        seen_sources.add(source_sha)
        matched_rows = [row for row in parser_rows if isinstance(row, Mapping) and _has_single_matching_source(row, source)]
        if len(matched_rows) != 1:
            raise ValueError(f'parser_row_not_unique:{item_id}')
        row = matched_rows[0]
        if 'row_partially_visible' in set(row.get('unknown_reason') or []):
            continue
        candidates.append((normalized, row))
    if not candidates:
        raise ValueError(f'parser_complete_row_missing:{item_id}')
    return min(candidates, key=lambda pair: (pair[0]['page_index'], pair[0]['row_index'], pair[0]['source_page']))

def _same_source(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all((left.get(field) == right.get(field) for field in ('source_page', 'page_index', 'row_index', 'row_bbox')))

def _has_matching_unique_source(item: Mapping[str, Any], candidate_source: Mapping[str, Any]) -> bool:
    evidence = item.get('source_evidence')
    return isinstance(evidence, list) and len(evidence) == 1 and isinstance(evidence[0], Mapping) and _same_source(evidence[0], candidate_source)

def _has_single_matching_source(item: Mapping[str, Any], candidate_source: Mapping[str, Any]) -> bool:
    evidence = item.get('source_evidence')
    if not isinstance(evidence, list):
        return False
    matches = [raw for raw in evidence if isinstance(raw, Mapping) and _same_source(raw, candidate_source)]
    return len(matches) == 1

def _require_mapping(mapping: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    if not isinstance(mapping, Mapping):
        raise ValueError(f'{name}_invalid')
    return mapping

def _required_mapping(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    return _require_mapping(mapping.get(key), key)

def _confidence_at_least(value: Any, minimum: float) -> bool:
    return isinstance(value, (int, float)) and (not isinstance(value, bool)) and (0.0 <= float(value) <= 1.0) and (float(value) >= minimum)

def _canonical_sha(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()
__all__ = ['EXPECTED_REMAINING', 'INITIAL_SKILL_POINTS', 'TARGET_COST', 'TARGET_NAME', 'TARGET_SKILL_ID', 'promote_current_final_skill_plan', 'promote_planner_final_skill_plan']
