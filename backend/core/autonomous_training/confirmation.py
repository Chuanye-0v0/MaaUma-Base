"""Fail-closed final confirmation for one already selected skill batch."""
from __future__ import annotations
from desktop.umaa_public_context import endpoint as _public_endpoint
import hashlib
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from core.autonomous_training.final_authorization import validate_completed_selection, validate_final_authorized_child
from core.autonomous_training.purchase import ExplicitDeviceTransportFacade
from core.skill_purchase_dialog import CurrentRunMultiPageSkillDialogVerifier, VerifiedSelectionDialogVerifier
from core.skill_purchase_executor import SKILL_PURCHASE_SAFETY_TOKEN, SkillPurchaseExecutor
from scripts.ug_parent_skill_purchase_atomic import _FileBackedADB
from scripts.ug_parent_skill_purchase_batch_final_confirm import FINAL_CONFIRMATION_ACTION_ID, PROJECT_ROOT as _CLOSURE_PROJECT_ROOT, build_live_session_receipt, build_multi_page_dialog_state, validate_closure_contract, write_live_session_receipt_exclusive
_ALLOWED_ENDPOINT = _public_endpoint()
_FINAL_CONFIRMATION_GATE = 'current_run_confirmation_closed'
_REPORT_NAME = 'final_confirmation_report.json'
_RESOURCE_CHECKS = {'uses_skill_points_only': True, 'uses_carrots': False, 'uses_clocks': False, 'uses_paid_currency': False}

def confirm_final_skill_learning(*, device: Any, promotion_plan_path: str | Path, promotion_review_path: str | Path, promotion_evidence_path: str | Path, selection_checkpoint_path: str | Path, closure_contract_path: str | Path, expected_closure_contract_sha256: str, capture_binding: Mapping[str, Any], top_frame_path: str | Path, current_frame_path: str | Path, output_dir: str | Path, parent_promotion_plan_path: str | Path | None=None, final_confirmation_evidence_path: str | Path | None=None, selection_ledger_path: str | Path | None=None, selection_ledger_frame_path: str | Path | None=None, mode: str='dry-run', execute: bool=False, safety_token: str | None=None, now_unix_ms: int | None=None, expected_endpoint: str=_ALLOWED_ENDPOINT) -> dict[str, Any]:
    """Confirm exactly one selected batch, or return a fail-closed report.

    The caller owns the connected device.  This function never opens or closes
    an ADB connection and deliberately does not inspect or close the success
    dialog after the one final-confirmation probe.
    """
    if expected_endpoint not in {_ALLOWED_ENDPOINT, _public_endpoint()} or getattr(device, 'endpoint', None) != expected_endpoint:
        return _result('device_endpoint_not_allowed', mode, execute)
    if mode not in {'dry-run', 'live'} or type(execute) is not bool:
        return _result('execution_mode_invalid', mode, execute)
    paths = {'promotion_plan': Path(promotion_plan_path), 'promotion_review': Path(promotion_review_path), 'promotion_evidence': Path(promotion_evidence_path), 'selection_checkpoint': Path(selection_checkpoint_path), 'closure_contract': Path(closure_contract_path), 'top_frame': Path(top_frame_path), 'current_frame': Path(current_frame_path)}
    values: dict[str, dict[str, Any]] = {}
    for label in ('promotion_plan', 'promotion_review', 'promotion_evidence', 'selection_checkpoint', 'closure_contract'):
        value = _read_mapping(paths[label])
        if value is None:
            return _result(f'{label}_unreadable', mode, execute)
        values[label] = value
    if not paths['top_frame'].is_file() or not paths['current_frame'].is_file():
        return _result('confirmation_frame_unreadable', mode, execute)
    if not _is_sha256(expected_closure_contract_sha256):
        return _result('closure_contract_sha256_invalid', mode, execute)
    if _sha256_file(paths['closure_contract']) != expected_closure_contract_sha256:
        return _result('closure_contract_sha256_mismatch', mode, execute)
    plan = values['promotion_plan']
    review = values['promotion_review']
    evidence = values['promotion_evidence']
    checkpoint = values['selection_checkpoint']
    contract = values['closure_contract']
    declared_checkpoint_path = _declared_checkpoint_path(contract)
    if contract.get('selection_checkpoint_path') is not None and declared_checkpoint_path is None:
        return _result('closure_selection_checkpoint_invalid', mode, execute)
    if declared_checkpoint_path is not None and paths['selection_checkpoint'].resolve() != declared_checkpoint_path:
        return _result('selection_checkpoint_path_mismatch', mode, execute)
    if declared_checkpoint_path is None:
        declared_checkpoint_path = paths['selection_checkpoint'].resolve()
    if contract.get('selection_checkpoint_sha256') != _sha256_file(paths['selection_checkpoint']):
        return _result('selection_checkpoint_sha256_mismatch', mode, execute)
    artifact_error = _validate_artifact_binding(plan=plan, review=review, evidence=evidence, checkpoint=checkpoint)
    if artifact_error:
        return _result(artifact_error, mode, execute, plan=plan, paths=paths)
    authorization_error = _validate_live_authorization(plan, mode, execute)
    if authorization_error:
        return _result(authorization_error, mode, execute, plan=plan, paths=paths)
    capture_error = _validate_capture_binding(capture_binding, review, plan, mode, execute, now_unix_ms)
    if capture_error:
        return _result(capture_error, mode, execute, plan=plan, paths=paths)
    if mode == 'live' and execute is True:
        final_paths = (parent_promotion_plan_path, final_confirmation_evidence_path, selection_ledger_path, selection_ledger_frame_path)
        if any((path is None for path in final_paths)):
            return _result('final_confirmation_authorization_missing', mode, execute, plan=plan, paths=paths)
        parent_path, final_evidence_path, ledger_path, ledger_frame_path = (Path(path) for path in final_paths)
        parent_plan = _read_mapping(parent_path)
        final_evidence = _read_mapping(final_evidence_path)
        ledger = _read_mapping(ledger_path)
        if parent_plan is None or final_evidence is None or ledger is None or (not ledger_frame_path.is_file()):
            return _result('final_confirmation_authorization_missing', mode, execute, plan=plan, paths=paths)
        parent_artifact_error = _validate_artifact_binding(plan=parent_plan, review=review, evidence=evidence, checkpoint=checkpoint)
        if parent_artifact_error:
            return _result(parent_artifact_error, mode, execute, plan=plan, paths=paths)
        final_authorization_error = validate_final_authorized_child(child_plan=plan, parent_plan=parent_plan, review=review, checkpoint=checkpoint, ledger=ledger, final_evidence=final_evidence, parent_plan_sha256=_sha256_file(parent_path), final_evidence_sha256=_sha256_file(final_evidence_path), promotion_review_sha256=_sha256_file(paths['promotion_review']), promotion_evidence_sha256=_sha256_file(paths['promotion_evidence']), selection_checkpoint_sha256=_sha256_file(paths['selection_checkpoint']), selection_ledger_sha256=_sha256_file(ledger_path), selection_ledger_frame_sha256=_sha256_file(ledger_frame_path), top_frame_sha256=_sha256_file(paths['top_frame']), current_frame_sha256=_sha256_file(paths['current_frame']), now_unix_ms=int(time.time() * 1000))
        if final_authorization_error:
            return _result(final_authorization_error, mode, execute, plan=plan, paths=paths)
    contract_error = validate_closure_contract(contract, plan=plan, plan_path=paths['promotion_plan'], top_frame=paths['top_frame'])
    if contract_error:
        return _result(contract_error, mode, execute, plan=plan, paths=paths)
    base = _result('confirmation_preflight_ready', mode, execute, plan=plan, paths=paths)
    verifier = VerifiedSelectionDialogVerifier() if contract.get('confirmation_policy') == 'verified_selection' else CurrentRunMultiPageSkillDialogVerifier(contract['expected_rows'])
    stage = _confirmation_stage(plan)
    if stage is None:
        return _result('promotion_plan_invalid', mode, execute, plan=plan, paths=paths)
    if mode == 'dry-run' or execute is not True:
        state = build_multi_page_dialog_state(plan=plan, contract=contract, top_frame=paths['top_frame'], current_frame=paths['current_frame'], frame_sequence=1)
        executor = SkillPurchaseExecutor(plan, adb=object(), parse_fresh_state=lambda frame: frame, dialog_verifier=verifier, stage=stage)
        preview = executor.preview_final_confirmation_state(state) if stage == 'final_confirmation_probe' else executor.preview_batch_final_confirmation_state(state)
        return _write_report(Path(output_dir), {**base, 'stop_reason': 'dry_run_no_device_actions', 'preflight_stop_reason': preview.stop_reason, 'current_frame_sha256': _sha256_file(paths['current_frame'])})
    if safety_token != SKILL_PURCHASE_SAFETY_TOKEN:
        return _write_report(Path(output_dir), _result('execution_gate_rejected', mode, execute, plan=plan, paths=paths))
    if not callable(getattr(device, 'command', None)):
        return _write_report(Path(output_dir), _result('explicit_device_command_required', mode, execute, plan=plan, paths=paths))
    root = Path(output_dir)
    intent_path = _intent_path(declared_checkpoint_path, plan)
    receipt = build_live_session_receipt(plan=plan, plan_path=paths['promotion_plan'], contract_path=paths['closure_contract'], contract=contract, top_frame=paths['top_frame'])
    try:
        intent_sha256 = write_live_session_receipt_exclusive(intent_path, receipt)
    except FileExistsError:
        return _write_report(root, _result('live_session_already_consumed', mode, execute, plan=plan, paths=paths))
    facade = ExplicitDeviceTransportFacade(device, expected_endpoint=expected_endpoint)
    file_adb = _FileBackedADB(facade, root / 'capture')
    frame_sequence = 0

    def parse_fresh_state(frame: Any) -> dict[str, Any]:
        nonlocal frame_sequence
        frame_sequence += 1
        return build_multi_page_dialog_state(plan=plan, contract=contract, top_frame=paths['top_frame'], current_frame=Path(frame), frame_sequence=frame_sequence)
    execution = SkillPurchaseExecutor(plan, adb=file_adb, parse_fresh_state=parse_fresh_state, dialog_verifier=verifier, mode='live', execute=True, safety_token=SKILL_PURCHASE_SAFETY_TOKEN, stage=stage, expected_action_id=FINAL_CONFIRMATION_ACTION_ID).run()
    performed = execution.stop_reason == _confirmation_complete_stop(stage) and execution.actions == (FINAL_CONFIRMATION_ACTION_ID,) and (execution.tap_count == 1)
    return _write_report(root, {**base, 'stop_reason': execution.stop_reason, 'actions': list(execution.actions), 'device_action_count': execution.tap_count, 'device_command_count': file_adb.device_command_count, 'final_confirmation_performed': performed, 'live_session_intent_sha256': intent_sha256, 'captured_frames': [str(path) for path in file_adb.captured_paths]})

def _validate_artifact_binding(*, plan: Mapping[str, Any], review: Mapping[str, Any], evidence: Mapping[str, Any], checkpoint: Mapping[str, Any]) -> str | None:
    promotion = plan.get('execution_promotion')
    plan_input = plan.get('input')
    purchases = plan.get('purchases')
    if plan.get('schema_version') != 'uma.skill_purchase_plan.v1' or plan.get('mode') != 'live' or type(plan.get('execution_allowed')) is not bool or (not isinstance(promotion, Mapping)) or (not isinstance(plan_input, Mapping)) or (not isinstance(purchases, list)) or (not purchases):
        return 'promotion_plan_invalid'
    plan_id = plan.get('plan_id')
    source_sha = plan_input.get('source_report_sha256')
    if not isinstance(plan_id, str) or not plan_id or (not _is_sha256(source_sha)) or (promotion.get('plan_id') != plan_id) or (promotion.get('current_skill_points', plan_input.get('current_skill_points')) != plan_input.get('current_skill_points')) or (promotion.get('planned_selection_count', len(purchases)) != len(purchases)):
        return 'promotion_plan_binding_invalid'
    if review.get('schema_version') != 'uma.skill_purchase_current_run_review.v1' or review.get('plan_id') != plan_id or any((review.get(key) != promotion.get(plan_key) for key, plan_key in (('sample_id', 'sample_id'), ('session_id', 'session_id'), ('capture_batch_id', 'fresh_capture_id')))):
        return 'review_plan_binding_mismatch'
    if review.get('source_report_sha256') != source_sha:
        return 'review_source_report_sha256_mismatch'
    if not all((_is_sha256(review.get(key)) for key in ('batch_plan_sha256', 'inventory_packet_sha256', 'capture_sha256'))):
        return 'review_batch_binding_invalid'
    if evidence.get('schema_version') != 'uma.skill_purchase_promotion_evidence.v2' or evidence.get('plan_id') != plan_id or evidence.get('source_report_sha256') != source_sha or (evidence.get('resource_checks') != _RESOURCE_CHECKS):
        return 'evidence_source_report_sha256_mismatch'
    economics = evidence.get('economics')
    if not isinstance(economics, Mapping) or economics.get('initial_skill_points') != plan_input.get('current_skill_points') or economics.get('spend') != plan.get('spent_skill_points') or (economics.get('expected_remaining') != plan.get('expected_remaining_skill_points')):
        return 'evidence_economics_mismatch'
    item_ids = [item.get('item_id') for item in purchases if isinstance(item, Mapping)]
    if len(item_ids) != len(purchases) or any((not isinstance(item_id, str) for item_id in item_ids)):
        return 'promotion_purchase_ids_invalid'
    if validate_completed_selection(checkpoint, item_ids):
        return 'selection_checkpoint_binding_mismatch'
    if checkpoint.get('plan_sha256') != review.get('batch_plan_sha256'):
        return 'selection_checkpoint_plan_sha256_mismatch'
    if checkpoint.get('inventory_packet_sha256') != review.get('inventory_packet_sha256'):
        return 'selection_checkpoint_inventory_sha256_mismatch'
    initial_points = plan_input.get('current_skill_points')
    spent = plan.get('spent_skill_points')
    remaining = plan.get('expected_remaining_skill_points')
    costs = [item.get('observed_final_cost') for item in purchases if isinstance(item, Mapping)]
    if type(initial_points) is not int or type(spent) is not int or type(remaining) is not int or any((type(cost) is not int or cost <= 0 for cost in costs)) or (sum(costs) != spent) or (initial_points - spent != remaining):
        return 'promotion_plan_economics_invalid'
    cumulative = 0
    for purchase in purchases:
        assert isinstance(purchase, Mapping)
        cumulative += purchase['observed_final_cost']
        if purchase.get('expected_selected_total_after') != cumulative or purchase.get('expected_skill_points_after') != initial_points - cumulative:
            return 'promotion_purchase_economics_invalid'
    reviewed_rows = review.get('reviewed_rows')
    fingerprints = [item.get('source_row_fingerprint') for item in purchases if isinstance(item, Mapping)]
    if not isinstance(reviewed_rows, Mapping) or set(reviewed_rows) != set(fingerprints):
        return 'reviewed_rows_binding_mismatch'
    for purchase in purchases:
        if not isinstance(purchase, Mapping):
            return 'promotion_purchase_invalid'
        row = reviewed_rows.get(purchase.get('source_row_fingerprint'))
        if not isinstance(row, Mapping) or row.get('item_id') != purchase.get('item_id') or row.get('observed_final_cost') != purchase.get('observed_final_cost') or (row.get('expected_selected_total') != purchase.get('expected_selected_total_after')) or (row.get('expected_remaining_skill_points') != purchase.get('expected_skill_points_after')) or (row.get('source_report_sha256') != source_sha):
            return 'reviewed_row_economics_mismatch'
    return None

def _confirmation_stage(plan: Mapping[str, Any]) -> str | None:
    purchases = plan.get('purchases')
    if not isinstance(purchases, list) or not purchases:
        return None
    return 'final_confirmation_probe' if len(purchases) == 1 else 'batch_final_confirmation_probe'

def _confirmation_complete_stop(stage: str) -> str:
    return 'final_confirmation_probe_complete' if stage == 'final_confirmation_probe' else 'batch_final_confirmation_probe_complete'

def _validate_live_authorization(plan: Mapping[str, Any], mode: str, execute: bool) -> str | None:
    if mode != 'live' or execute is not True:
        return None
    if plan.get('execution_allowed') is not True or plan.get('multi_skill_live_gate') != _FINAL_CONFIRMATION_GATE:
        return 'final_confirmation_authorization_missing'
    return None

def _validate_capture_binding(capture_binding: Mapping[str, Any], review: Mapping[str, Any], plan: Mapping[str, Any], mode: str, execute: bool, now_unix_ms: int | None) -> str | None:
    if not isinstance(capture_binding, Mapping) or capture_binding.get('schema_version') != 'uma.skill_capture_binding.v1' or (not _is_sha256(capture_binding.get('capture_sha256'))):
        return 'capture_binding_invalid'
    if review.get('capture_sha256') != capture_binding.get('capture_sha256') or review.get('capture_binding') != dict(capture_binding):
        return 'capture_review_binding_mismatch'
    current = int(time.time() * 1000) if mode == 'live' and execute is True else int(time.time() * 1000) if now_unix_ms is None else now_unix_ms
    if type(current) is not int:
        return 'now_unix_ms_invalid'
    captured_at = capture_binding.get('captured_at_unix_ms')
    expires_at = capture_binding.get('expires_at_unix_ms')
    if type(captured_at) is not int or type(expires_at) is not int or expires_at <= captured_at:
        return 'capture_binding_timing_invalid'
    if current < captured_at - 30000:
        return 'capture_not_yet_valid'
    if current > expires_at:
        return 'capture_expired'
    if plan.get('input', {}).get('source_report_sha256') is None:
        return 'promotion_plan_binding_invalid'
    return None

def _declared_checkpoint_path(contract: Mapping[str, Any]) -> Path | None:
    value = contract.get('selection_checkpoint_path')
    if not isinstance(value, str) or not value:
        return None
    candidate = (_CLOSURE_PROJECT_ROOT / value).resolve()
    try:
        candidate.relative_to(_CLOSURE_PROJECT_ROOT.resolve())
    except ValueError:
        return None
    return candidate

def _intent_path(checkpoint_path: Path, plan: Mapping[str, Any]) -> Path:
    promotion = plan['execution_promotion']
    seed = ':'.join((str(plan['plan_id']), str(promotion['session_id']), str(plan['input']['source_report_sha256']))).encode('utf-8')
    return checkpoint_path.parent / '.final_confirmation_intents' / (hashlib.sha256(seed).hexdigest() + '.json')

def _read_mapping(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None

def _result(stop_reason: str, mode: str, execute: bool, *, plan: Mapping[str, Any] | None=None, paths: Mapping[str, Path] | None=None) -> dict[str, Any]:
    return {'schema_version': 'uma.autonomous_final_skill_confirmation.v1', 'mode': mode, 'execute': execute, 'stop_reason': stop_reason, 'plan_id': plan.get('plan_id') if isinstance(plan, Mapping) else None, 'promotion_plan_path': str(paths['promotion_plan'].resolve()) if paths else None, 'promotion_plan_sha256': _sha256_file(paths['promotion_plan']) if paths else None, 'closure_contract_sha256': _sha256_file(paths['closure_contract']) if paths else None, 'final_confirmation_performed': False, 'actions': [], 'device_action_count': 0}

def _write_report(root: Path, report: dict[str, Any]) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    (root / _REPORT_NAME).write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    return report

def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all((character in '0123456789abcdef' for character in value))
__all__ = ['confirm_final_skill_learning']
