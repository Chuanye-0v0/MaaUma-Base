"""Harvest-only skill-learning policy and transaction coordinator.

Harvest has no confirmed JJC distance, so ``harvest_unbound`` is a local
sentinel, never a claimed race distance. General/current-style skills take
priority; other verified skills remain available for spending leftover PT.
"""
from __future__ import annotations
from desktop.umaa_public_context import endpoint as _public_endpoint
import hashlib
import json
import time
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any
import cv2
from core.autonomous_training.confirmation import confirm_final_skill_learning
from core.autonomous_training.finish import canonical_finish_evidence, finish_control_visible
from core.autonomous_training.final_issuer import _expected_rows, issue_final_confirmation_artifacts
from core.autonomous_training.purchase import select_and_open_purchase_confirmation
from core.autonomous_training.success_close import close_skill_purchase_success
from core.autonomous_training.return_after_learning import return_after_skill_learning
from core.autonomous_training.skills import prepare_skills
from core.skill_batch_purchase import validate_skill_batch_plan, validate_skill_inventory_packet
from core.skill_planner.catalog import SkillCatalog
from core.skill_purchase_dialog import inspect_verified_selection_confirmation
from core.skill_purchase_executor import SKILL_PURCHASE_SAFETY_TOKEN
from core.skill_scroll import skill_list_frames_match
from core.autonomous_training.purchase import SKILL_BATCH_PURCHASE_SAFETY_TOKEN
_STYLE_MAP = {'领跑': 'leader', '跟前': 'front', '居中': 'betweener', '后追': 'chaser'}
_GRADE_ORDER = {grade: index for index, grade in enumerate(('G', 'F', 'E', 'D', 'C', 'B', 'A', 'S', 'SS'))}
_UNBOUND_DISTANCE = 'harvest_unbound'
_ALLOWED_ENDPOINT = _public_endpoint()
_DEFAULT_PLANNER_CATALOG = Path(__file__).resolve().parents[2] / 'data' / 'skills' / 'cn-20260916' / 'cn_skill_planner_catalog.json'

def build_harvest_context(summary: dict, turn_id: str) -> dict:
    """Build a fail-closed planner context from an observed finish summary."""
    if not isinstance(turn_id, str) or not turn_id:
        return {'reason': 'turn_id_required'}
    styles = summary.get('run_styles') if isinstance(summary, Mapping) else None
    if not isinstance(styles, Mapping) or set(styles) != set(_STYLE_MAP):
        return {'reason': 'run_styles_invalid'}
    grades: dict[str, int] = {}
    for name in _STYLE_MAP:
        grade = styles.get(name)
        if not isinstance(grade, str) or grade not in _GRADE_ORDER:
            return {'reason': 'run_style_unknown'}
        grades[name] = _GRADE_ORDER[grade]
    best = max(grades.values())
    candidates = [name for name, value in grades.items() if value == best]
    if len(candidates) > 1 and summary.get('name') in {'樱花进王', '大和赤骥'} and ('领跑' in candidates):
        candidates = ['领跑']
    return {'turn_id': turn_id, 'target_mode': 'race', 'race_profile': {'distance_type': _UNBOUND_DISTANCE, 'style': _STYLE_MAP[candidates[0]]}, 'policy': {'harvest_only': True}, **({'harvest_style_candidates': [_STYLE_MAP[name] for name in candidates]} if len(candidates) > 1 else {}), **({'finish_identity': canonical_finish_evidence({'finish_summary': summary, 'actions': {'finish_training': {'point': [500, 1070]}}})} if isinstance(summary.get('name'), str) and summary['name'] else {})}

def harvest_eligible_skill_ids(planner_catalog: str | Path, build_context: Mapping[str, Any]) -> set[str]:
    """Keep all verified skills available; suitability is a planner priority."""
    profile = build_context.get('race_profile') if isinstance(build_context, Mapping) else None
    if not isinstance(profile, Mapping) or profile.get('distance_type') != _UNBOUND_DISTANCE or profile.get('style') not in set(_STYLE_MAP.values()):
        return set()
    return {skill_id for skill_id, row in SkillCatalog.load(planner_catalog).rows.items() if row.is_verified}

def learn_harvest_skills(device: Any, output_dir: Path, build_context: dict, stop_event: Any=None, *, execute: bool=False, purchase_allowed: bool=True, expected_endpoint: str=_ALLOWED_ENDPOINT) -> dict:
    """Run only a verified harvest skill transaction; never retry a stage."""
    base = {'ok': False, 'remaining_skill_points': None, 'child_plan_path': None, 'return_after_learning_report_path': None, 'no_purchase_evidence_path': None, 'no_purchase_evidence_sha256': None, 'device_action_count': 0, 'device_action_count_complete': True}
    if execute is not True:
        return {**base, 'reason': 'dry_run_no_device_actions'}
    if _stop_requested(stop_event):
        return {**base, 'reason': 'stop_requested'}
    catalog_path = build_context.get('planner_catalog', _DEFAULT_PLANNER_CATALOG) if isinstance(build_context, Mapping) else _DEFAULT_PLANNER_CATALOG
    if not isinstance(catalog_path, (str, Path)) or not Path(catalog_path).is_file():
        return {**base, 'reason': 'harvest_planner_catalog_missing'}
    planner_context = dict(build_context)
    planner_context['planner_catalog'] = str(catalog_path)
    planner_context['harvest_planner_allowed_skill_ids'] = sorted(harvest_eligible_skill_ids(catalog_path, build_context))
    prepared = prepare_skills(device, Path(output_dir) / 'prepare', planner_context, expected_endpoint=expected_endpoint)
    base = _include_prepare_actions(base, prepared)
    if not isinstance(prepared, Mapping) or prepared.get('status') != 'ready':
        return {**base, 'reason': 'skill_prepare_not_ready'}
    plan = _read_mapping(prepared.get('batch_plan_path'))
    if plan is None:
        return {**base, 'reason': 'prepared_plan_unreadable'}
    raw_remaining = plan.get('expected_remaining_skill_points')
    remaining = raw_remaining if type(raw_remaining) is int and raw_remaining >= 0 else None
    actions = plan.get('selected_action_ids')
    if not isinstance(actions, list) or any((not isinstance(item, str) or not item for item in actions)):
        return {**base, 'reason': 'prepared_plan_invalid', 'remaining_skill_points': remaining}
    inventory = _read_mapping(prepared.get('inventory_path'))
    if inventory is None:
        return {**base, 'reason': 'prepared_inventory_unreadable', 'remaining_skill_points': remaining}
    plan_valid, _plan_reason = validate_skill_batch_plan(plan, inventory)
    if not plan_valid:
        return {**base, 'reason': 'prepared_plan_invalid', 'remaining_skill_points': remaining}
    items = plan.get('items')
    if not isinstance(items, list) or len(items) != len(actions) or any((not isinstance(item, Mapping) or item.get('action_id') != action or (not isinstance(item.get('skill_id'), str)) or (not item['skill_id']) for action, item in zip(actions, items))):
        return {**base, 'reason': 'prepared_plan_action_mapping_invalid', 'remaining_skill_points': remaining}
    selected_skill_ids = {str(item['skill_id']) for item in items}
    if not selected_skill_ids <= harvest_eligible_skill_ids(catalog_path, build_context):
        return {**base, 'reason': 'harvest_plan_policy_violation', 'remaining_skill_points': remaining}
    if not actions:
        planner_report = _read_mapping(prepared.get('planner_report_path'))
        if _has_affordable_unresolved_skill(planner_report, remaining):
            return {**base, 'reason': 'affordable_skill_rows_unresolved', 'remaining_skill_points': remaining}
        evidence = _issue_no_purchase_evidence(Path(output_dir), prepared, plan, catalog_path, build_context, remaining)
        if evidence is None:
            return {**base, 'reason': 'no_harvest_eligible_skills_evidence_incomplete', 'remaining_skill_points': remaining}
        return {**base, 'reason': 'no_harvest_eligible_skills_return_required', 'remaining_skill_points': remaining, 'no_purchase_evidence_path': str(evidence), 'no_purchase_evidence_sha256': _sha256(evidence)}
    if purchase_allowed is not True:
        return {**base, 'reason': 'harvest_skill_purchase_limit_reached', 'remaining_skill_points': plan.get('initial_skill_points'), 'planned_remaining_skill_points': remaining, 'planned_skill_count': len(actions)}
    if _stop_requested(stop_event):
        return {**base, 'reason': 'stop_requested', 'remaining_skill_points': remaining}
    external_db = build_context.get('external_skill_db_path')
    if not isinstance(external_db, (str, Path)) or not Path(external_db).is_file():
        return {**base, 'reason': 'harvest_external_skill_db_missing', 'remaining_skill_points': remaining}
    turn_id = build_context.get('turn_id')
    if not isinstance(turn_id, str) or not turn_id:
        return {**base, 'reason': 'harvest_turn_id_missing', 'remaining_skill_points': remaining}
    batch_nonce = f'{turn_id}-{time.time_ns()}'
    selection_root = Path(output_dir) / 'selection'
    selection = select_and_open_purchase_confirmation(device=device, inventory_path=prepared.get('inventory_path'), plan_path=prepared.get('batch_plan_path'), review_path=None, output_dir=selection_root, mode='live', execute=True, expected_inventory_sha256=_mapping_field(prepared.get('inventory_path'), 'packet_sha256'), expected_plan_sha256=_mapping_field(prepared.get('batch_plan_path'), 'plan_sha256'), planner_report_path=prepared.get('planner_report_path'), capture_manifest_path=prepared.get('capture_manifest_path'), external_skill_db_path=external_db, session_id=f'harvest-session-{batch_nonce}', capture_batch_id=f'harvest-capture-{batch_nonce}', safety_token=SKILL_BATCH_PURCHASE_SAFETY_TOKEN, expected_endpoint=expected_endpoint)
    base = _include_report_actions(base, selection)
    if not (isinstance(selection, Mapping) and selection.get('stop_reason') == 'confirmation_opened' and (selection.get('confirmation_opened') is True)):
        return {**base, 'reason': 'selection_confirmation_not_opened', 'remaining_skill_points': remaining}
    if _stop_requested(stop_event):
        return {**base, 'reason': 'stop_requested', 'remaining_skill_points': remaining}
    return complete_harvest_selection(device, output_dir, selection_root, selection, base, remaining, len(actions), stop_event, expected_endpoint)

def complete_harvest_selection(device, output_dir, selection_root, selection, base, remaining, action_count, stop_event, expected_endpoint):
    if _stop_requested(stop_event):
        return {**base, 'reason': 'stop_requested', 'remaining_skill_points': remaining}
    issued = _issue_final_confirmation(device, selection_root, selection, stop_event=stop_event)
    if not isinstance(issued, Mapping) or not isinstance(issued.get('child_plan_path'), str):
        reason = issued.get('stop_reason') if isinstance(issued, Mapping) else None
        if reason == 'issuer_review_rows_invalid' and action_count == 1:
            reason = 'final_issuer_single_purchase_unsupported'
        return {**base, 'reason': reason or 'final_issuer_not_ready', 'remaining_skill_points': remaining}
    child_path = Path(issued['child_plan_path'])
    confirmation_root = Path(output_dir) / 'confirmation'
    confirmation_inputs = issued.get('confirmation_inputs')
    if not isinstance(confirmation_inputs, Mapping):
        return {**base, 'reason': 'final_issuer_confirmation_inputs_invalid', 'remaining_skill_points': remaining}
    confirmation = confirm_final_skill_learning(device=device, output_dir=confirmation_root, mode='live', execute=True, safety_token=SKILL_PURCHASE_SAFETY_TOKEN, expected_endpoint=expected_endpoint, **confirmation_inputs)
    base = _include_report_actions(base, confirmation)
    final_report = _report_path(confirmation, confirmation_root / 'final_confirmation_report.json')
    if not (isinstance(confirmation, Mapping) and confirmation.get('stop_reason') in {'final_confirmation_probe_complete', 'batch_final_confirmation_probe_complete'} and (confirmation.get('final_confirmation_performed') is True) and (final_report is not None)):
        return {**base, 'reason': 'final_confirmation_not_verified', 'remaining_skill_points': remaining}
    if _stop_requested(stop_event):
        return {**base, 'reason': 'stop_requested', 'remaining_skill_points': remaining}
    return close_and_return_harvest_selection(device, output_dir, selection_root, child_path, final_report, base, remaining, stop_event, expected_endpoint)

def resume_unclosed_harvest_success(device, previous, output_dir, remaining, stop_event, endpoint):
    """Resume only a verified purchase whose failed close attempt sent zero actions."""
    previous = Path(previous).resolve()
    matches = list((previous / 'training_status').glob('harvest-skills-*/batch-*/success_close/success_close_report.json'))
    if len(matches) != 1 or type(remaining) is not int or (not 0 <= remaining < 60):
        raise RuntimeError('unclosed_skill_purchase_evidence_invalid')
    close_path = matches[0]
    batch = close_path.parent.parent
    close = json.loads(close_path.read_text(encoding='utf8'))
    final_report = batch / 'confirmation/final_confirmation_report.json'
    final = json.loads(final_report.read_text(encoding='utf8'))
    child_path = (batch / 'selection/issued/promotion_plan.final_child.json').resolve()
    if endpoint != _public_endpoint() or close.get('close_success_performed') is not False or close.get('actions') != [] or (close.get('device_action_count') != 0) or (close.get('stop_reason') != 'batch_success_dialog_page_mismatch') or (close.get('final_confirmation_report_sha256') != _sha256(final_report)) or (close.get('child_plan_sha256') != _sha256(child_path)) or (final.get('final_confirmation_performed') is not True) or (final.get('stop_reason') != 'batch_final_confirmation_probe_complete') or (final.get('actions') != ['skill_purchase.final_confirm']) or (final.get('device_action_count') != 1) or (Path(final.get('promotion_plan_path', '')).resolve() != child_path) or (final.get('promotion_plan_sha256') != _sha256(child_path)):
        raise RuntimeError('unclosed_skill_purchase_evidence_invalid')
    return close_and_return_harvest_selection(device, output_dir, batch / 'selection', child_path, final_report, dict(device_action_count=0, device_action_count_complete=True), remaining, stop_event, endpoint)

def close_and_return_harvest_selection(device, output_dir, selection_root, child_path, final_report, base, remaining, stop_event, expected_endpoint, previous_close_report=None):
    """Only repeat verified non-consuming close/return, never final purchase."""
    for attempt in range(3):
        destination = Path(output_dir) if attempt == 0 else Path(output_dir) / f'close-return-retry-{attempt}'
        result = _close_and_return_harvest_selection_once(device, destination, selection_root, child_path, final_report, base, remaining, stop_event, expected_endpoint, previous_close_report)
        if result.get('ok') or result.get('reason') != 'return_after_learning_not_verified':
            return result
        report = _read_mapping(result.get('failed_return_report_path'))
        if not report or report.get('stop_reason') != 'direct_success_page_mismatch' or report.get('return_performed') is not False or (report.get('actions') != []) or (report.get('device_action_count') != 0):
            return result
        previous_close_report = result.get('success_close_report_path')
        base = {**base, 'device_action_count': result.get('device_action_count'), 'device_action_count_complete': result.get('device_action_count_complete')}
        if _stop_requested(stop_event):
            return {**result, 'reason': 'stop_requested'}
    return result

def _close_and_return_harvest_selection_once(device, output_dir, selection_root, child_path, final_report, base, remaining, stop_event, expected_endpoint, previous_close_report=None):
    if _stop_requested(stop_event):
        return {**base, 'ok': False, 'reason': 'stop_requested', 'remaining_skill_points': remaining}
    close_root = Path(output_dir) / 'success_close'
    close = close_skill_purchase_success(device=device, child_plan_path=child_path, final_confirmation_report_path=final_report, expected_final_confirmation_report_sha256=_sha256(final_report), output_dir=close_root, promotion_review_path=selection_root / 'promotion_review.json', mode='live', execute=True, safety_token=SKILL_PURCHASE_SAFETY_TOKEN, expected_endpoint=expected_endpoint)
    base = _include_report_actions(base, close)
    close_report = _report_path(close, close_root / 'success_close_report.json')
    if not (isinstance(close, Mapping) and close.get('stop_reason') in {'close_success_probe_complete', 'batch_close_success_probe_complete'} and (close.get('close_success_performed') is True) and (close_report is not None)):
        if previous_close_report is not None and isinstance(close, Mapping) and (close.get('actions') == []) and (close.get('device_action_count') == 0) and (close.get('stop_reason') in {'success_dialog_page_mismatch', 'batch_success_dialog_page_mismatch'}):
            close_report = Path(previous_close_report)
        else:
            return {**base, 'reason': 'success_close_not_verified', 'remaining_skill_points': remaining}
    if _stop_requested(stop_event):
        return {**base, 'reason': 'stop_requested', 'remaining_skill_points': remaining}
    returned_root = Path(output_dir) / 'return_after_learning'
    returned = return_after_skill_learning(device=device, child_plan_path=child_path, promotion_review_path=selection_root / 'promotion_review.json', final_confirmation_report_path=final_report, expected_final_confirmation_report_sha256=_sha256(final_report), success_close_report_path=close_report, expected_success_close_report_sha256=_sha256(close_report), output_dir=returned_root, mode='live', execute=True, safety_token=SKILL_PURCHASE_SAFETY_TOKEN, expected_endpoint=expected_endpoint)
    base = _include_report_actions(base, returned)
    returned_report = _report_path(returned, returned_root / 'return_after_learning_report.json')
    if not (isinstance(returned, Mapping) and returned.get('stop_reason') in {'return_after_purchase_probe_complete', 'batch_return_after_purchase_probe_complete'} and (returned.get('return_performed') is True) and (returned.get('verified_current_skill_points') == remaining) and (returned_report is not None)):
        return {**base, 'reason': 'return_after_learning_not_verified', 'remaining_skill_points': remaining, 'success_close_report_path': str(close_report), 'failed_return_report_path': str(returned_report) if returned_report else None}
    return {**base, 'ok': True, 'reason': 'harvest_skill_learning_verified', 'remaining_skill_points': remaining, 'child_plan_path': str(child_path), 'return_after_learning_report_path': str(returned_report)}

def resume_completed_purchase_scan(device, previous, pending_skill, output, stop, endpoint):
    """Leave a read-only reopened scan using the original successful purchase chain."""
    previous = Path(previous)
    root = previous / 'training_status'
    prepared = list(root.glob('harvest-skills-*/batch-0/prepare/skill_prepare.report.json'))
    records = [json.loads(p.read_text(encoding='utf8')) for p in root.glob('*-receipt.json')]
    skill = json.loads(Path(pending_skill).read_text(encoding='utf8'))
    points = skill.get('remaining_skill_points')
    if endpoint != _public_endpoint() or skill.get('ok') is not True or type(points) is not int or (not 0 <= points < 60) or (len(prepared) != 1) or list(root.glob('harvest-skills-*/batch-*/selection')) or (not records) or any((r.get('action_id') not in {'open_skills', 'scroll_skill_overlap', 'scroll_skill_up'} or r.get('performed') is not True or r.get('endpoint') != endpoint for r in records)):
        raise RuntimeError('completed_purchase_scan_resume_invalid')
    prepare = json.loads(prepared[0].read_text(encoding='utf8'))
    if prepare.get('purchase_executed') is not False or prepare.get('tap_count') != 0:
        raise RuntimeError('completed_purchase_scan_not_readonly')
    child = Path(skill['child_plan_path']).resolve()
    returned = Path(skill['return_after_learning_report_path']).resolve()
    if not child.is_relative_to(previous.parent) or not returned.is_relative_to(previous.parent):
        raise RuntimeError('completed_purchase_scan_source_outside_account')
    proof = json.loads(returned.read_text(encoding='utf8'))
    close = returned.parent.parent / 'success_close/success_close_report.json'
    final = child.parents[2] / 'confirmation/final_confirmation_report.json'
    if proof.get('return_performed') is not True or proof.get('device_endpoint') != endpoint or proof.get('verified_current_skill_points') != points or (proof.get('child_plan_sha256') != _sha256(child)) or (proof.get('success_close_report_sha256') != _sha256(close)) or (proof.get('final_confirmation_report_sha256') != _sha256(final)):
        raise RuntimeError('completed_purchase_scan_proof_invalid')
    if _stop_requested(stop):
        raise RuntimeError('operator_stop')
    result = return_after_skill_learning(device=device, child_plan_path=child, promotion_review_path=child.parent.parent / 'promotion_review.json', final_confirmation_report_path=final, expected_final_confirmation_report_sha256=_sha256(final), success_close_report_path=close, expected_success_close_report_sha256=_sha256(close), output_dir=output, mode='live', execute=True, safety_token=SKILL_PURCHASE_SAFETY_TOKEN, expected_endpoint=endpoint)
    if result.get('return_performed') is not True or result.get('verified_current_skill_points') != points:
        raise RuntimeError('completed_purchase_scan_return_unverified')
    return {**skill, 'return_after_learning_report_path': str(Path(output) / 'return_after_learning_report.json')}

def _issue_final_confirmation(device: Any, selection_root: Path, selection: Mapping[str, Any], *, stop_event=None) -> dict[str, Any]:
    """Use the completed selection ledger, without rereading modal rows."""
    capture = getattr(device, 'capture', None)
    if not callable(capture):
        return {'stop_reason': 'explicit_device_capture_required'}
    parent = _read_mapping(selection_root / 'promotion_plan.json')
    review = _read_mapping(selection_root / 'promotion_review.json')
    if parent is None or review is None:
        return {'stop_reason': 'selection_artifacts_invalid'}
    expected_rows, row_error = _expected_rows(parent, review)
    if row_error:
        return {'stop_reason': row_error}
    ledger_path = selection.get('selection_ledger_path')
    ledger_frame_path = selection.get('selection_ledger_frame_path')
    if not isinstance(ledger_path, str) or not isinstance(ledger_frame_path, str):
        return {'stop_reason': 'selection_ledger_artifacts_missing'}
    for attempt in range(2):
        if _stop_requested(stop_event):
            return {'stop_reason': 'stop_requested'}
        observation = capture()
        frame_path = observation.get('screenshot_path') if isinstance(observation, Mapping) else None
        captured_at = observation.get('captured_at_unix_ms') if isinstance(observation, Mapping) else None
        reason = None
        if not isinstance(frame_path, str) or type(captured_at) is not int:
            reason = 'confirmation_capture_invalid'
        else:
            image = cv2.imread(frame_path, cv2.IMREAD_COLOR)
            if image is None:
                reason = 'confirmation_frame_unreadable'
            else:
                state = inspect_verified_selection_confirmation(image)
                if state.get('accepted') is not True:
                    reason = 'confirmation_modal_unrecognized'
        if reason:
            known_page = observation.get('page') if isinstance(observation, Mapping) else None
            if attempt == 0 and known_page in (None, 'unknown', 'loading', 'skill_confirmation') and (not (isinstance(observation, Mapping) and observation.get('dangerous_resource_flags'))):
                continue
            return dict(stop_reason=reason, confirmation_capture_count=attempt + 1)
        if _stop_requested(stop_event):
            return {'stop_reason': 'stop_requested'}
        result = issue_final_confirmation_artifacts(parent_plan_path=selection_root / 'promotion_plan.json', review_path=selection_root / 'promotion_review.json', promotion_evidence_path=selection_root / 'promotion_evidence.json', selection_checkpoint_path=selection_root / 'selection_checkpoint.json', selection_ledger_path=ledger_path, selection_ledger_frame_path=ledger_frame_path, top_frame_path=frame_path, current_frame_path=frame_path, detector_state=state, top_captured_at_unix_ms=captured_at, current_captured_at_unix_ms=captured_at, output_dir=selection_root / 'issued')
        if attempt == 0 and result.get('stop_reason') == 'final_evidence_dialog_timing_invalid' and (int(time.time() * 1000) - captured_at > 30000):
            continue
        return {**result, 'confirmation_capture_count': attempt + 1}
    raise AssertionError('confirmation_capture_budget_unreachable')

def _stop_requested(stop_event: Any) -> bool:
    return bool(stop_event is not None and callable(getattr(stop_event, 'is_set', None)) and stop_event.is_set())

def _include_prepare_actions(base: Mapping[str, Any], prepared: Any) -> dict[str, Any]:
    """Count authorized list scrolls from the capture manifest when present."""
    if not isinstance(prepared, Mapping):
        return {**base, 'device_action_count_complete': False}
    manifest = prepared.get('capture_manifest')
    if not isinstance(manifest, Mapping):
        return {**base, 'device_action_count_complete': False}
    counts = (manifest.get('rewind_scroll_count'), manifest.get('forward_scroll_count'))
    if any((type(count) is not int or count < 0 for count in counts)):
        return {**base, 'device_action_count_complete': False}
    return {**base, 'device_action_count': int(base['device_action_count']) + sum(counts)}

def _include_report_actions(base: Mapping[str, Any], report: Any) -> dict[str, Any]:
    if not isinstance(report, Mapping):
        return {**base, 'device_action_count_complete': False}
    count = report.get('device_action_count')
    if type(count) is not int or count < 0:
        return {**base, 'device_action_count_complete': False}
    return {**base, 'device_action_count': int(base['device_action_count']) + count}

def _mapping_field(path: Any, field: str) -> Any:
    value = _read_mapping(path)
    return value.get(field) if value is not None else None

def _report_path(report: Any, default: Path) -> Path | None:
    explicit = report.get('report_path') if isinstance(report, Mapping) else None
    path = Path(explicit) if isinstance(explicit, str) else default
    return path if path.is_file() else None

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all((character in '0123456789abcdef' for character in value))

def _read_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, (str, Path)):
        return None
    try:
        import json
        loaded = json.loads(Path(value).read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return dict(loaded) if isinstance(loaded, Mapping) else None

def _issue_no_purchase_evidence(output_dir: Path, prepared: Mapping[str, Any], plan: Mapping[str, Any], catalog_path: str | Path, build_context: Mapping[str, Any], remaining: int | None) -> Path | None:
    """Bind a complete, empty fresh plan without imitating a purchase receipt."""
    artifact_paths = {'batch_plan': prepared.get('batch_plan_path'), 'inventory': prepared.get('inventory_path'), 'planner_report': prepared.get('planner_report_path'), 'capture_manifest': prepared.get('capture_manifest_path')}
    resolved: dict[str, Path] = {}
    for name, value in artifact_paths.items():
        if not isinstance(value, str) or not Path(value).is_file():
            return None
        resolved[name] = Path(value)
    identity = build_context.get('finish_identity')
    manifest = _read_mapping(resolved['capture_manifest'])
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get('page_evidence'), list) or (not manifest['page_evidence']) or (not isinstance(manifest['page_evidence'][-1], Mapping)):
        return None
    source_skill_list_sha256 = skill_list_region_sha256(Path(manifest['page_evidence'][-1]['path']))
    if not _complete_no_purchase_artifacts(resolved, plan, build_context, remaining) or remaining is None or (not _is_sha256(source_skill_list_sha256)) or (not isinstance(identity, Mapping)) or (canonical_finish_evidence({'finish_summary': identity, 'actions': {'finish_training': {'point': [500, 1070]}}}) != dict(identity)):
        return None
    payload = {'schema_version': 'uma.harvest_no_purchase_evidence.v1', 'status': 'complete_no_harvest_eligible_purchase', 'turn_id': build_context.get('turn_id'), 'current_skill_points': remaining, 'selected_action_ids': [], 'harvest_policy': dict(build_context.get('policy', {})), 'finish_identity': dict(identity), 'catalog_path': str(Path(catalog_path).resolve()), 'catalog_sha256': _sha256(Path(catalog_path)), 'batch_plan_path': str(resolved['batch_plan'].resolve()), 'batch_plan_sha256': _sha256(resolved['batch_plan']), 'inventory_path': str(resolved['inventory'].resolve()), 'inventory_sha256': _sha256(resolved['inventory']), 'planner_report_path': str(resolved['planner_report'].resolve()), 'planner_report_sha256': _sha256(resolved['planner_report']), 'capture_manifest_path': str(resolved['capture_manifest'].resolve()), 'capture_manifest_sha256': _sha256(resolved['capture_manifest']), 'source_skill_page_sha256': manifest_page_sha256(resolved['capture_manifest']), 'source_skill_list_sha256': source_skill_list_sha256}
    path = output_dir / 'no_purchase' / 'no_purchase_evidence.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    return path

def _has_affordable_unresolved_skill(planner: Mapping[str, Any] | None, remaining: int | None) -> bool:
    """An unresolved purchasable row is not proof that skill spending is done."""
    if not isinstance(planner, Mapping) or type(remaining) is not int:
        return False
    if any((type(row.get('display_cost')) is int and 0 < row['display_cost'] <= remaining for row in planner.get('planner_candidate_filter', {}).get('deferred_gold_until_lower_owned', []))):
        return True
    resolved_names = {item.get('identity_binding', {}).get('observed_name') for item in planner.get('planner_observation', {}).get('available_skills', []) if isinstance(item, Mapping) and isinstance(item.get('identity_binding'), Mapping)} - {None, ''}
    resolved_sources = {(source.get('source_page'), source.get('row_index')) for item in planner.get('planner_observation', {}).get('available_skills', []) if isinstance(item, Mapping) and item.get('identity_binding', {}).get('status') == 'auto_confirmed' for source in item.get('source_evidence', []) if source.get('source_page') and type(source.get('row_index')) is int}
    return any((isinstance(row, Mapping) and row.get('acquired') is False and (row.get('plus_available') is True) and (type(row.get('cost')) is int) and (0 < row['cost'] <= remaining) and (row.get('name') not in resolved_names) and (not any(((source.get('source_page'), source.get('row_index')) in resolved_sources for source in row.get('source_evidence', [])))) for row in planner.get('parser_manifest', {}).get('skills', [])))

def return_after_no_purchase_skills(device: Any, output_dir: Path, no_purchase_evidence_path: str | Path, expected_no_purchase_evidence_sha256: str, stop_event: Any=None, *, execute: bool=False, expected_endpoint: str=_ALLOWED_ENDPOINT) -> dict:
    """Return from a fully scanned empty plan through one fresh safe action."""
    base = {'ok': False, 'return_performed': False, 'no_purchase_return_report_path': None, 'finish_ready_observation_path': None, 'device_action_count': 0, 'device_action_count_complete': True}
    evidence_path = Path(no_purchase_evidence_path)
    evidence = _read_mapping(evidence_path)
    if evidence is None or not _is_sha256(expected_no_purchase_evidence_sha256) or (not evidence_path.is_file()) or (_sha256(evidence_path) != expected_no_purchase_evidence_sha256) or (not _valid_no_purchase_evidence(evidence)):
        return {**base, 'reason': 'no_purchase_evidence_invalid'}
    if execute is not True:
        return {**base, 'reason': 'dry_run_no_device_actions'}
    if _stop_requested(stop_event):
        return {**base, 'reason': 'stop_requested'}
    if expected_endpoint not in {_ALLOWED_ENDPOINT, _public_endpoint()} or getattr(device, 'endpoint', None) != expected_endpoint:
        return {**base, 'reason': 'device_endpoint_not_allowed'}
    step = getattr(device, 'step', None)
    if not callable(step):
        return {**base, 'reason': 'explicit_device_step_required'}
    context = getattr(device, 'context', None)
    if not isinstance(context, MutableMapping):
        return {**base, 'reason': 'explicit_device_context_required'}
    pending = context.get('harvest_no_purchase_return')
    if pending is None:
        context['harvest_no_purchase_return'] = {'expected_endpoint': expected_endpoint, 'evidence_path': str(evidence_path), 'evidence_sha256': expected_no_purchase_evidence_sha256, 'submitted': False}
    elif not isinstance(pending, Mapping) or pending.get('evidence_path') != str(evidence_path) or pending.get('evidence_sha256') != expected_no_purchase_evidence_sha256 or (pending.get('submitted') is not False):
        return {**base, 'reason': 'no_purchase_return_not_pending'}
    try:
        receipt = step('return_harvest_skills', stop_event=stop_event)
    except Exception:
        return {**base, 'reason': 'no_purchase_return_dispatch_uncertain', 'device_action_count_complete': False}
    validated = _validated_no_purchase_return(receipt, evidence, expected_no_purchase_evidence_sha256)
    if validated is None:
        return {**base, 'reason': 'no_purchase_return_not_verified', 'device_action_count': 1}
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    finish_path = root / 'finish_ready_after_no_purchase.json'
    finish_path.write_text(json.dumps(validated, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    report_path = root / 'no_purchase_return_report.json'
    report = {'schema_version': 'uma.harvest_no_purchase_return.v1', 'mode': 'live', 'execute': True, 'stop_reason': 'return_harvest_skills_probe_complete', 'return_performed': True, 'actions': ['return_harvest_skills'], 'device_action_count': 1, 'no_purchase_evidence_sha256': expected_no_purchase_evidence_sha256, 'verified_current_skill_points': evidence['current_skill_points'], 'finish_ready_observation_path': str(finish_path), 'finish_ready_observation_sha256': _sha256(finish_path)}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    return {'ok': True, 'reason': 'no_purchase_return_verified', 'return_performed': True, 'remaining_skill_points': evidence['current_skill_points'], 'no_purchase_return_report_path': str(report_path), 'finish_ready_observation_path': str(finish_path), 'device_action_count': 1, 'device_action_count_complete': True}

def authorize_no_purchase_return(observed: Mapping[str, Any], context: Mapping[str, Any]) -> None:
    """Authorize one skills-page return only from a fresh complete empty scan."""
    request = context.get('harvest_no_purchase_return') if isinstance(context, Mapping) else None
    if not isinstance(request, Mapping) or request.get('submitted') is not False:
        raise RuntimeError('no_purchase_return_not_pending')
    path, sha = (request.get('evidence_path'), request.get('evidence_sha256'))
    evidence = _read_mapping(path)
    if not isinstance(path, str) or not _is_sha256(sha) or evidence is None or (_sha256(Path(path)) != sha) or (not _valid_no_purchase_evidence(evidence)):
        raise RuntimeError('no_purchase_evidence_invalid')
    if request.get('expected_endpoint', _ALLOWED_ENDPOINT) not in {_ALLOWED_ENDPOINT, _public_endpoint()} or observed.get('endpoint') != request.get('expected_endpoint', _ALLOWED_ENDPOINT) or observed.get('page') != 'skills' or (observed.get('current_skill_points') != evidence['current_skill_points']) or (not isinstance(observed.get('screenshot_path'), str)) or (not _skill_list_matches_no_purchase_evidence(Path(str(observed.get('screenshot_path'))), evidence)) or (not _observed_fresh_frame(observed)):
        raise RuntimeError('no_purchase_return_source_unverified')

def manifest_page_sha256(manifest_path: Path) -> str | None:
    manifest = _read_mapping(manifest_path)
    pages = manifest.get('page_evidence') if isinstance(manifest, Mapping) else None
    if not isinstance(pages, list) or not pages or (not isinstance(pages[-1], Mapping)):
        return None
    sha = pages[-1].get('raw_sha256')
    return sha if _is_sha256(sha) else None

def skill_list_region_sha256(path: Path) -> str | None:
    """Bind the original rows/footer; live animation matching is separate."""
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None or image.shape != (1280, 720, 3):
        return None
    digest = hashlib.sha256()
    digest.update(image[470:1026, 30:690].tobytes())
    digest.update(image[1040:1125, 210:710].tobytes())
    return digest.hexdigest()

def _skill_list_matches_no_purchase_evidence(path: Path, evidence: Mapping[str, Any]) -> bool:
    expected = evidence.get('source_skill_list_sha256')
    if _is_sha256(expected) and skill_list_region_sha256(path) == expected:
        return True
    manifest = _read_mapping(evidence.get('capture_manifest_path'))
    pages = manifest.get('page_evidence') if manifest else None
    if not isinstance(pages, list) or not pages or (not isinstance(pages[-1], Mapping)):
        return False
    source_path = pages[-1].get('path')
    if not isinstance(source_path, str) or skill_list_region_sha256(Path(source_path)) != expected:
        return False
    source, current = (cv2.imread(source_path), cv2.imread(str(path)))
    return skill_list_frames_match(source, current)

def _observed_fresh_frame(observed: Mapping[str, Any]) -> bool:
    path, sha = (observed.get('screenshot_path'), observed.get('frame_sha256'))
    now = int(time.time() * 1000)
    return isinstance(path, str) and Path(path).is_file() and _is_sha256(sha) and (_sha256(Path(path)) == sha) and (type(observed.get('captured_at_unix_ms')) is int) and (type(observed.get('expires_at_unix_ms')) is int) and (observed['captured_at_unix_ms'] <= now <= observed['expires_at_unix_ms'])

def _valid_no_purchase_evidence(evidence: Mapping[str, Any]) -> bool:
    if evidence.get('schema_version') != 'uma.harvest_no_purchase_evidence.v1' or evidence.get('status') != 'complete_no_harvest_eligible_purchase' or evidence.get('selected_action_ids') != [] or (type(evidence.get('current_skill_points')) is not int) or (evidence['current_skill_points'] < 0) or (evidence.get('harvest_policy') not in ({'harvest_only': True}, {'harvest_only': True, 'max_purchase_count': 1}, {'harvest_only': True, 'max_purchase_count': 3}, {'harvest_only': True, 'max_purchase_count': 4}, {'harvest_only': True, 'max_purchase_count': 5})) or (not _is_sha256(evidence.get('source_skill_page_sha256'))) or (not _is_sha256(evidence.get('source_skill_list_sha256'))):
        return False
    identity = evidence.get('finish_identity')
    canonical_identity = canonical_finish_evidence({'finish_summary': identity, 'actions': {'finish_training': {'point': [500, 1070]}}}) if isinstance(identity, Mapping) else None
    if not isinstance(identity, Mapping) or canonical_identity is None or canonical_identity != dict(identity):
        return False
    resolved: dict[str, Path] = {}
    for name in ('catalog', 'batch_plan', 'inventory', 'planner_report', 'capture_manifest'):
        raw_path, sha = (evidence.get(f'{name}_path'), evidence.get(f'{name}_sha256'))
        if not isinstance(raw_path, str) or not _is_sha256(sha) or (not Path(raw_path).is_file()):
            return False
        if _sha256(Path(raw_path)) != sha:
            return False
        resolved[name] = Path(raw_path)
    plan = _read_mapping(resolved['batch_plan'])
    return plan is not None and _complete_no_purchase_artifacts(resolved, plan, {'turn_id': evidence.get('turn_id'), 'policy': evidence.get('harvest_policy')}, evidence['current_skill_points'])

def _complete_no_purchase_artifacts(paths: Mapping[str, Path], plan: Mapping[str, Any], build_context: Mapping[str, Any], remaining: int | None) -> bool:
    """Prove empty planning came from one complete, fresh, ready scan."""
    inventory = _read_mapping(paths['inventory'])
    planner = _read_mapping(paths['planner_report'])
    manifest = _read_mapping(paths['capture_manifest'])
    if inventory is None or planner is None or manifest is None or (remaining is None):
        return False
    inventory_valid, _reason = validate_skill_inventory_packet(inventory)
    plan_valid, _reason = validate_skill_batch_plan(plan, inventory)
    turn_id = build_context.get('turn_id')
    if not inventory_valid or not plan_valid or plan.get('selected_action_ids') != [] or (plan.get('items') != []) or (plan.get('expected_remaining_skill_points') != remaining) or (manifest.get('schema_version') != 'uma.skill_acquisition_capture.v1') or (manifest.get('capture_mode') != 'passed_device_screenshot_and_authorized_vertical_scroll_only') or (manifest.get('captured_page_count') != len(manifest.get('captured_pages', []))) or (manifest.get('captured_page_count') != len(manifest.get('page_evidence', []))) or (not isinstance(manifest.get('captured_pages'), list)) or (not manifest['captured_pages']) or (not isinstance(turn_id, str)) or (not turn_id) or (inventory.get('capture_binding', {}).get('turn_id') != turn_id) or (inventory.get('current_skill_points') != remaining) or (planner.get('schema_version') != 'uma.fresh_skill_acquisition_dry_run.v1') or (planner.get('mode') != 'offline_dry_run_only') or (planner.get('execution_allowed') is not False) or (planner.get('verdict', {}).get('planner_verdict') != 'PLANNER_READY') or (planner.get('parser_manifest', {}).get('current_skill_points') != remaining) or (planner.get('planner_observation', {}).get('current_skill_pt') != remaining) or (planner.get('inventory_packet') != inventory) or (planner.get('batch_plan') != plan) or (planner.get('batch_plan_omitted_action_ids') != []) or (planner.get('planner_dry_run', {}).get('executed') is not True) or (planner.get('planner_dry_run', {}).get('purchases') != []) or (planner.get('planner_dry_run', {}).get('remaining_skill_pt') != remaining) or _has_affordable_unresolved_skill(planner, remaining) or (planner.get('capture_binding', {}).get('turn_id') != turn_id) or (planner.get('capture_binding', {}).get('capture_sha256') != inventory.get('capture_binding', {}).get('capture_sha256')):
        return False
    if inventory.get('captured_at_unix_ms') != manifest.get('captured_at_unix_ms') or inventory.get('expires_at_unix_ms') != manifest.get('expires_at_unix_ms') or plan.get('captured_at_unix_ms') != manifest.get('captured_at_unix_ms') or (plan.get('expires_at_unix_ms') != manifest.get('expires_at_unix_ms')) or (type(manifest.get('captured_at_unix_ms')) is not int) or (type(manifest.get('expires_at_unix_ms')) is not int) or (not manifest['captured_at_unix_ms'] <= int(time.time() * 1000) <= manifest['expires_at_unix_ms']):
        return False
    for expected_path, page in zip(manifest['captured_pages'], manifest['page_evidence']):
        path = page.get('path') if isinstance(page, Mapping) else None
        sha = page.get('raw_sha256') if isinstance(page, Mapping) else None
        if not isinstance(expected_path, str) or path != expected_path or (not isinstance(path, str)) or (not _is_sha256(sha)) or (not Path(path).is_file()) or (_sha256(Path(path)) != sha):
            return False
    bound_pages = inventory.get('capture_binding', {}).get('page_hashes')
    manifest_pages = [{'name': Path(str(page['path'])).name, 'sha256': page['raw_sha256']} for page in manifest['page_evidence'] if isinstance(page, Mapping)]
    if bound_pages != manifest_pages:
        return False
    return True

def _validated_no_purchase_return(receipt: Any, evidence: Mapping[str, Any], evidence_sha256: str) -> dict[str, Any] | None:
    if not isinstance(receipt, Mapping) or receipt.get('action_id') != 'return_harvest_skills' or receipt.get('performed') is not True:
        return None
    source_path = receipt.get('source_screenshot_path')
    source_sha = receipt.get('source_frame_sha256')
    source_captured = receipt.get('source_captured_at_unix_ms')
    source_expires = receipt.get('source_expires_at_unix_ms')
    if not isinstance(source_path, str) or not Path(source_path).is_file() or (not _is_sha256(source_sha)) or (_sha256(Path(source_path)) != source_sha) or (not _skill_list_matches_no_purchase_evidence(Path(source_path), evidence)) or (type(source_captured) is not int) or (type(source_expires) is not int) or (not source_captured <= int(time.time() * 1000) <= source_expires) or (receipt.get('source_current_skill_points') != evidence['current_skill_points']) or (receipt.get('no_purchase_evidence_sha256') != evidence_sha256):
        return None
    observed = receipt.get('observation')
    if not isinstance(observed, Mapping) or observed.get('page') != 'finish_ready':
        return None
    path, sha = (observed.get('screenshot_path'), observed.get('frame_sha256'))
    if not isinstance(path, str) or not Path(path).is_file() or (not _is_sha256(sha)) or (_sha256(Path(path)) != sha):
        return None
    if not finish_control_visible(observed):
        return None
    return dict(observed)
__all__ = ['build_harvest_context', 'harvest_eligible_skill_ids', 'learn_harvest_skills', 'return_after_no_purchase_skills', 'authorize_no_purchase_return']
