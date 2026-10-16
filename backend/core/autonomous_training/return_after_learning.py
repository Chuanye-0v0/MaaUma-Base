"""Return from the verified skill page after a completed batch learning flow."""
from __future__ import annotations
from desktop.umaa_public_context import endpoint as _public_endpoint
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from core.autonomous_training.purchase import ExplicitDeviceTransportFacade
from core.autonomous_training.success_close import _child_purchase_ids, _validate_final_report
from core.skill_purchase_executor import SKILL_PURCHASE_SAFETY_TOKEN, SkillPurchaseExecutor
from core.skill_purchase_live_adapter import SkillPurchaseLiveCaptureSession
from scripts.ug_parent_skill_purchase_atomic import _FileBackedADB
_ALLOWED_ENDPOINT = _public_endpoint()
_RETURN_ACTION = 'skill_purchase.return_after_purchase'
_CLOSE_ACTION = 'skill_purchase.close_success'
_REPORT_NAME = 'return_after_learning_report.json'

def return_after_skill_learning(*, device: Any, child_plan_path: str | Path, promotion_review_path: str | Path, final_confirmation_report_path: str | Path, expected_final_confirmation_report_sha256: str, success_close_report_path: str | Path, expected_success_close_report_sha256: str, output_dir: str | Path, mode: str='dry-run', execute: bool=False, safety_token: str | None=None, expected_endpoint: str=_ALLOWED_ENDPOINT) -> dict[str, Any]:
    """Tap only the verified skills-page back control, then stop."""
    if expected_endpoint not in {_ALLOWED_ENDPOINT, _public_endpoint()} or getattr(device, 'endpoint', None) != expected_endpoint:
        return _result('device_endpoint_not_allowed', mode, execute)
    if mode not in {'dry-run', 'live'} or type(execute) is not bool:
        return _result('execution_mode_invalid', mode, execute)
    child_path = Path(child_plan_path)
    final_path = Path(final_confirmation_report_path)
    close_path = Path(success_close_report_path)
    child = _read_mapping(child_path)
    final_report = _read_mapping(final_path)
    close_report = _read_mapping(close_path)
    if child is None or final_report is None or close_report is None:
        return _result('return_after_learning_artifact_unreadable', mode, execute)
    child_ids = _child_purchase_ids(child)
    if child_ids is None:
        return _result('final_authorized_child_invalid', mode, execute)
    if not _is_sha256(expected_final_confirmation_report_sha256):
        return _result('final_confirmation_report_sha256_invalid', mode, execute)
    if not _is_sha256(expected_success_close_report_sha256):
        return _result('success_close_report_sha256_invalid', mode, execute)
    if _sha256_file(final_path) != expected_final_confirmation_report_sha256:
        return _result('final_confirmation_report_sha256_mismatch', mode, execute)
    if _sha256_file(close_path) != expected_success_close_report_sha256:
        return _result('success_close_report_sha256_mismatch', mode, execute)
    final_error = _validate_final_report(final_report, child_path, child)
    if final_error:
        return _result(final_error, mode, execute, child=child)
    close_error = _validate_success_close_report(close_report, child, _sha256_file(child_path), expected_final_confirmation_report_sha256)
    if close_error:
        return _result(close_error, mode, execute, child=child)
    base = {**_result('return_after_learning_preflight_ready', mode, execute, child=child), 'child_plan_sha256': _sha256_file(child_path), 'final_confirmation_report_sha256': expected_final_confirmation_report_sha256, 'success_close_report_sha256': expected_success_close_report_sha256}
    if mode == 'dry-run' or execute is not True:
        return _write_report(Path(output_dir), {**base, 'stop_reason': 'dry_run_no_device_actions'})
    if safety_token != SKILL_PURCHASE_SAFETY_TOKEN:
        return _write_report(Path(output_dir), _result('execution_gate_rejected', mode, execute, child=child))
    if not callable(getattr(device, 'command', None)):
        return _write_report(Path(output_dir), _result('explicit_device_command_required', mode, execute, child=child))
    review = _read_mapping(Path(promotion_review_path))
    if review is None:
        return _write_report(Path(output_dir), _result('promotion_review_unreadable', mode, execute, child=child))
    promotion = child['execution_promotion']
    try:
        session = SkillPurchaseLiveCaptureSession(review=review, root=Path.cwd(), plan_id=child['plan_id'], sample_id=promotion['sample_id'], session_id=promotion['session_id'], capture_batch_id=promotion['fresh_capture_id'])
        session.bind_verified_purchase_item_ids(child_ids)
    except ValueError:
        return _write_report(Path(output_dir), _result('skill_session_binding_invalid', mode, execute, child=child))
    root = Path(output_dir)
    facade = ExplicitDeviceTransportFacade(device, expected_endpoint=expected_endpoint)
    adb = _FileBackedADB(facade, root / 'capture')
    verified_points: dict[str, Any] = {}

    def parse_fresh_state(frame: Any) -> dict[str, Any]:
        state = dict(session.capture(Path(frame), post_purchase_return=True))
        verified_acquired = state.get('purchase_terminal_state') == 'acquired' and state.get('acquired_item_ids') == child_ids and (state.get('remaining_points_semantics') == 'post_purchase') and (state.get('selected_item_ids') == []) and (state.get('selected_total') == 0)
        if state.get('verified_return_targets_absent') is not True and (not verified_acquired):
            state['page_kind'] = 'unknown'
            state['screen_kind'] = 'unknown'
        state.update({'final_confirmation_evidence_verified': True, 'success_evidence_verified': True, 'final_confirmation_report_sha256': expected_final_confirmation_report_sha256, 'success_report_sha256': expected_success_close_report_sha256})
        frame_sha256 = _sha256_file(Path(frame))
        if not verified_points and type(state.get('current_skill_points')) is int and (state['current_skill_points'] == child['expected_remaining_skill_points']) and (state.get('frame_sha256') == frame_sha256):
            verified_points.update(current_skill_points=state['current_skill_points'], frame_sha256=frame_sha256, selected_item_ids=state.get('selected_item_ids'), selected_total=state.get('selected_total'), green_confirm_control=state.get('green_confirm_control'), observed_acquired_item_ids=state.get('acquired_item_ids', []), purchase_terminal_state=state.get('purchase_terminal_state'), verified_return_targets_absent=state.get('verified_return_targets_absent') is True, acquired_semantics='fresh_visible_rows' if verified_acquired else 'purchase_receipts_and_targets_absent')
        return state
    execution = SkillPurchaseExecutor(child, adb=adb, parse_fresh_state=parse_fresh_state, mode='live', execute=True, safety_token=SKILL_PURCHASE_SAFETY_TOKEN, stage=_return_stage(child_ids), expected_action_id=_RETURN_ACTION).run()
    performed = execution.stop_reason == _return_complete_stop(child_ids) and execution.actions == (_RETURN_ACTION,) and (execution.tap_count == 1)
    frames = list(adb.captured_paths)
    source_frame_sha256 = _sha256_file(frames[0]) if frames else None
    verified = performed and verified_points.get('frame_sha256') == source_frame_sha256
    return _write_report(root, {**base, 'stop_reason': execution.stop_reason, 'actions': list(execution.actions), 'device_action_count': execution.tap_count, 'device_command_count': adb.device_command_count, 'return_performed': performed, 'source_frame_sha256': source_frame_sha256, 'post_frame_sha256': _sha256_file(frames[1]) if len(frames) > 1 else None, 'captured_frames': [str(path) for path in frames], **({'verified_current_skill_points': verified_points['current_skill_points'], 'verified_skill_points_frame_sha256': verified_points['frame_sha256'], 'device_endpoint': device.endpoint, 'pre_return_observation': verified_points} if verified else {})})

def _validate_success_close_report(report: Mapping[str, Any], child: Mapping[str, Any], child_plan_sha256: str, final_report_sha256: str) -> str | None:
    if report.get('schema_version') != 'uma.autonomous_skill_success_close.v1' or report.get('mode') != 'live' or report.get('execute') is not True or (report.get('stop_reason') != _close_complete_stop(child)) or (report.get('plan_id') != child.get('plan_id')) or (report.get('child_plan_sha256') != child_plan_sha256) or (report.get('close_success_performed') is not True) or (report.get('actions') != [_CLOSE_ACTION]) or (report.get('device_action_count') != 1) or (report.get('final_confirmation_report_sha256') != final_report_sha256):
        return 'success_close_report_binding_invalid'
    return None

def _return_stage(child_ids: list[str]) -> str:
    return 'return_after_purchase_probe' if len(child_ids) == 1 else 'batch_return_after_purchase_probe'

def _return_complete_stop(child_ids: list[str]) -> str:
    return 'return_after_purchase_probe_complete' if len(child_ids) == 1 else 'batch_return_after_purchase_probe_complete'

def _close_complete_stop(child: Mapping[str, Any]) -> str:
    purchases = child.get('purchases')
    return 'close_success_probe_complete' if isinstance(purchases, list) and len(purchases) == 1 else 'batch_close_success_probe_complete'

def _read_mapping(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None

def _write_report(root: Path, report: dict[str, Any]) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    (root / _REPORT_NAME).write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    return report

def _result(stop_reason: str, mode: str, execute: bool, *, child: Mapping[str, Any] | None=None) -> dict[str, Any]:
    return {'schema_version': 'uma.autonomous_return_after_learning.v1', 'mode': mode, 'execute': execute, 'stop_reason': stop_reason, 'plan_id': child.get('plan_id') if isinstance(child, Mapping) else None, 'child_plan_sha256': None, 'return_performed': False, 'actions': [], 'device_action_count': 0}

def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all((character in '0123456789abcdef' for character in value))
