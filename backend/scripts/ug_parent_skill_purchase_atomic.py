"""Atomic final-skill navigation, plus, and open-confirmation probes."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from config.settings import ADB_ENDPOINT, ADB_PORT
from core.skill_acquisition_capture import CAPTURE_SAFETY_TOKEN
from core.skill_purchase_atomic_navigation import RECOVER_SELECTED_TARGET_ACTION_ID, REVEAL_BOTTOM_PARTIAL_ACTION_ID, SCROLL_UP_ACTION_ID, SkillPurchaseNavigationSession, plan_navigation_once
from core.skill_purchase_executor import SKILL_MULTI_CONFIRMATION_CAPTURE_TOKEN, SKILL_PURCHASE_SAFETY_TOKEN, SkillPurchaseExecutor
from core.skill_purchase_live_adapter import SkillPurchaseLiveCaptureSession
from core.skill_purchase_dialog import CurrentRunSkillDialogVerifier
_SELECT_PLUS_ACTION_ID = 'skill_purchase.select_plus'
_OPEN_CONFIRMATION_ACTION_ID = 'skill_purchase.open_confirmation'
_MULTI_CONFIRMATION_CAPTURE_ACTION_ID = 'skill_purchase.capture_multi_confirmation'
OPEN_CONFIRMATION_SAFETY_TOKEN = 'UG_PARENT_SKILL_PURCHASE_OPEN_CONFIRMATION'
FINAL_CONFIRMATION_SAFETY_TOKEN = 'UG_PARENT_SKILL_PURCHASE_FINAL_CONFIRM'
_FINAL_CONFIRMATION_ACTION_ID = 'skill_purchase.final_confirm'
CLOSE_SUCCESS_SAFETY_TOKEN = 'UG_PARENT_SKILL_PURCHASE_CLOSE_SUCCESS'
_CLOSE_SUCCESS_ACTION_ID = 'skill_purchase.close_success'
RETURN_AFTER_PURCHASE_SAFETY_TOKEN = 'UG_PARENT_SKILL_PURCHASE_RETURN_AFTER_PURCHASE'
_RETURN_AFTER_PURCHASE_ACTION_ID = 'skill_purchase.return_after_purchase'
LIVE_SESSION_RECEIPT_NAME = 'atomic_live_session.json'
FINAL_REPORT_NAME = 'atomic_report.json'
_ALLOWED_STAGES = {'locate', 'select-plus-probe', 'open-confirmation-probe', 'multi-confirmation-capture-probe', 'final-confirmation-probe', 'batch-close-success-probe', 'close-success-probe', 'return-after-purchase-probe'}

def parse_args(argv: list[str] | None=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='One-action final skill purchase controller.')
    parser.add_argument('--stage', choices=sorted(_ALLOWED_STAGES), required=True)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--review', required=True)
    parser.add_argument('--session-state', required=True)
    parser.add_argument('--screenshot-dir', required=True)
    parser.add_argument('--input-image')
    parser.add_argument('--adb-port', type=int, required=True)
    parser.add_argument('--mode', choices=('dry-run', 'live'), default='dry-run')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--safety-token')
    parser.add_argument('--expected-action-id')
    parser.add_argument('--final-confirmation-report')
    parser.add_argument('--success-report')
    parser.add_argument('--max-swipes', type=int, default=8)
    parser.add_argument('--allow-recover-selected-target', action='store_true')
    return parser.parse_args(argv)

def main(argv: list[str] | None=None) -> int:
    try:
        report, code = run(parse_args(argv))
    except Exception as exc:
        report, code = ({'is_valid': False, 'performed': False, 'tap_count': 0, 'device_command_count': 0, 'stop_reason': f'{type(exc).__name__}: {exc}'}, 2)
    print(json.dumps(report, ensure_ascii=True, separators=(',', ':')))
    return code

def run(args: argparse.Namespace, *, adb_factory: Callable[[], Any] | None=None, capture_session_factory: Callable[..., Any]=SkillPurchaseLiveCaptureSession) -> tuple[dict[str, Any], int]:
    plan = _read_json(Path(args.plan))
    review = _read_json(Path(args.review))
    base = {'is_valid': True, 'stage': args.stage, 'mode': args.mode, 'performed': False, 'action_id': None, 'tap_count': 0, 'device_command_count': 0, 'plan_id': plan.get('plan_id')}
    if args.mode == 'dry-run' and (not args.input_image):
        return ({**base, 'stop_reason': 'input_image_required_for_dry_run'}, 0)
    gate_error = _validate_live_gate(args) if args.mode == 'live' else None
    if gate_error:
        return ({**base, 'is_valid': False, 'stop_reason': gate_error}, 2)
    if args.mode == 'dry-run' and args.execute:
        return ({**base, 'is_valid': False, 'stop_reason': 'dry_run_cannot_execute'}, 2)
    promotion = plan.get('execution_promotion')
    input_data = plan.get('input')
    if not isinstance(promotion, dict) or not isinstance(input_data, dict):
        return ({**base, 'is_valid': False, 'stop_reason': 'plan_binding_missing'}, 2)
    final_confirmation_report_sha256: str | None = None
    success_report_sha256: str | None = None
    if args.stage == 'return-after-purchase-probe':
        final_confirmation_report_sha256, report_error = _validate_final_confirmation_report(args.final_confirmation_report, plan_id=plan.get('plan_id'))
        if report_error:
            return ({**base, 'is_valid': False, 'stop_reason': report_error}, 2)
        success_report_sha256, success_report_error = _validate_success_report(args.success_report, plan_id=plan.get('plan_id'))
        if success_report_error:
            return ({**base, 'is_valid': False, 'stop_reason': success_report_error}, 2)
    output_dir = Path(args.screenshot_dir)
    report_path = output_dir / FINAL_REPORT_NAME
    if args.mode == 'live':
        if report_path.exists():
            return ({**base, 'is_valid': False, 'stop_reason': 'atomic_report_already_exists'}, 2)
        receipt = build_live_session_receipt(args=args, plan=plan, final_confirmation_report_sha256=final_confirmation_report_sha256, success_report_sha256=success_report_sha256)
        try:
            live_session_receipt_sha256 = write_json_exclusive(output_dir / LIVE_SESSION_RECEIPT_NAME, receipt)
        except FileExistsError:
            return ({**base, 'is_valid': False, 'stop_reason': 'live_session_already_consumed'}, 2)
        base = {**base, 'live_session_receipt_sha256': live_session_receipt_sha256}
    session = capture_session_factory(review=review, root=ROOT, plan_id=plan.get('plan_id'), sample_id=input_data.get('sample_id'), session_id=promotion.get('session_id'), capture_batch_id=promotion.get('fresh_capture_id'))
    navigation = _load_or_start_navigation(args, plan)
    if args.mode == 'dry-run':
        state = session.capture(Path(args.input_image))
        if final_confirmation_report_sha256 is not None and success_report_sha256 is not None:
            state = _bind_purchase_success_evidence(state, final_confirmation_report_sha256, success_report_sha256)
        report = _run_offline_stage(args, base, plan, navigation, state)
        return (report, 0 if report['is_valid'] else 2)
    create_adb = adb_factory or _default_adb_factory(args.adb_port)
    adb = create_adb()
    try:
        if getattr(adb, 'address', None) != ADB_ENDPOINT:
            return ({**base, 'is_valid': False, 'stop_reason': 'adb_address_mismatch'}, 2)
        if adb.connect() is not True:
            return ({**base, 'is_valid': False, 'stop_reason': 'adb_connect_failed'}, 2)
        file_adb = _FileBackedADB(adb, output_dir)
        if args.stage == 'locate':
            report, code = _run_live_locate(args, base, plan, session, navigation, file_adb)
        elif args.stage == 'select-plus-probe':
            report, code = _run_live_plus_probe(args, base, plan, session, file_adb)
        elif args.stage == 'open-confirmation-probe':
            report, code = _run_live_open_confirmation_probe(args, base, plan, session, file_adb)
        elif args.stage == 'multi-confirmation-capture-probe':
            report, code = _run_live_multi_confirmation_capture_probe(args, base, plan, session, file_adb)
        elif args.stage == 'final-confirmation-probe':
            report, code = _run_live_final_confirmation_probe(args, base, plan, session, file_adb)
        elif args.stage == 'return-after-purchase-probe':
            assert final_confirmation_report_sha256 is not None
            assert success_report_sha256 is not None
            report, code = _run_live_return_after_purchase_probe(args, base, plan, session, file_adb, final_confirmation_report_sha256, success_report_sha256)
        elif args.stage == 'batch-close-success-probe':
            report, code = _run_live_batch_close_success_probe(args, base, plan, session, file_adb)
        else:
            report, code = _run_live_close_success_probe(args, base, plan, session, file_adb)
        write_json_exclusive(report_path, report)
        return (report, code)
    finally:
        try:
            adb.disconnect()
        except Exception:
            pass

def _run_offline_stage(args: argparse.Namespace, base: dict[str, Any], plan: dict[str, Any], navigation: SkillPurchaseNavigationSession, state: dict[str, Any]) -> dict[str, Any]:
    if args.stage == 'locate':
        decision = plan_navigation_once(state=state, plan=plan, session=navigation, allow_recover_selected_target=args.allow_recover_selected_target)
        return {**base, 'stop_reason': decision.reason, 'action_id': decision.action_id, 'proposed_gesture': decision.gesture, 'source_frame_sha256': state.get('frame_sha256')}
    if args.stage == 'select-plus-probe':
        executor = SkillPurchaseExecutor(plan, adb=_NoDeviceADB(), parse_fresh_state=lambda frame: frame, stage='select_plus_probe')
        result = executor.preview_select_plus_state(state)
        ready_reason = 'select_plus_ready'
        action_id = _SELECT_PLUS_ACTION_ID
    elif args.stage == 'open-confirmation-probe':
        executor = SkillPurchaseExecutor(plan, adb=_NoDeviceADB(), parse_fresh_state=lambda frame: frame, stage='open_confirmation_probe')
        result = executor.preview_open_confirmation_state(state)
        ready_reason = 'open_confirmation_ready'
        action_id = _OPEN_CONFIRMATION_ACTION_ID
    elif args.stage == 'multi-confirmation-capture-probe':
        executor = SkillPurchaseExecutor(plan, adb=_NoDeviceADB(), parse_fresh_state=lambda frame: frame, stage='multi_confirmation_capture_probe')
        result = executor.preview_multi_confirmation_capture_state(state)
        ready_reason = 'multi_confirmation_capture_ready'
        action_id = _MULTI_CONFIRMATION_CAPTURE_ACTION_ID
    elif args.stage == 'final-confirmation-probe':
        executor = SkillPurchaseExecutor(plan, adb=_NoDeviceADB(), parse_fresh_state=lambda frame: frame, dialog_verifier=CurrentRunSkillDialogVerifier(), stage='final_confirmation_probe')
        result = executor.preview_final_confirmation_state(state)
        ready_reason = 'final_confirmation_ready'
        action_id = _FINAL_CONFIRMATION_ACTION_ID
    elif args.stage == 'return-after-purchase-probe':
        executor = SkillPurchaseExecutor(plan, adb=_NoDeviceADB(), parse_fresh_state=lambda frame: frame, stage='return_after_purchase_probe')
        result = executor.preview_return_after_purchase_state(state)
        ready_reason = 'return_after_purchase_ready'
        action_id = _RETURN_AFTER_PURCHASE_ACTION_ID
    elif args.stage == 'batch-close-success-probe':
        executor = SkillPurchaseExecutor(plan, adb=_NoDeviceADB(), parse_fresh_state=lambda frame: frame, stage='batch_close_success_probe')
        result = executor.preview_batch_close_success_state(state)
        ready_reason = 'batch_close_success_ready'
        action_id = _CLOSE_SUCCESS_ACTION_ID
    else:
        executor = SkillPurchaseExecutor(plan, adb=_NoDeviceADB(), parse_fresh_state=lambda frame: frame, stage='close_success_probe')
        result = executor.preview_close_success_state(state)
        ready_reason = 'close_success_ready'
        action_id = _CLOSE_SUCCESS_ACTION_ID
    return {**base, 'is_valid': result.stop_reason == ready_reason, 'stop_reason': result.stop_reason, 'action_id': action_id if result.stop_reason == ready_reason else None, 'source_frame_sha256': state.get('frame_sha256')}

def _run_live_locate(args: argparse.Namespace, base: dict[str, Any], plan: dict[str, Any], session: Any, navigation: SkillPurchaseNavigationSession, adb: '_FileBackedADB') -> tuple[dict[str, Any], int]:
    pre_path = adb.screenshot()
    state = session.capture(pre_path)
    decision = plan_navigation_once(state=state, plan=plan, session=navigation, allow_recover_selected_target=args.allow_recover_selected_target)
    if decision.action_id is None:
        return ({**base, 'stop_reason': decision.reason, 'source_frame_path': str(pre_path), 'source_frame_sha256': state.get('frame_sha256'), 'device_command_count': adb.device_command_count}, 0)
    gesture = decision.gesture
    if decision.action_id != args.expected_action_id or not isinstance(gesture, dict):
        return ({**base, 'is_valid': False, 'stop_reason': 'fresh_expected_action_id_mismatch', 'device_command_count': adb.device_command_count}, 2)
    adb.swipe(**gesture)
    navigation.consume_swipe(state['frame_sha256'])
    _write_json_atomic(Path(args.session_state), navigation.to_dict())
    post_path = adb.screenshot()
    post_state = session.capture(post_path)
    post_decision = plan_navigation_once(state=post_state, plan=plan, session=navigation, allow_recover_selected_target=args.allow_recover_selected_target)
    report = {**base, 'performed': True, 'action_id': decision.action_id, 'stop_reason': 'navigation_action_complete', 'pre_frame_path': str(pre_path), 'pre_frame_sha256': state.get('frame_sha256'), 'post_frame_path': str(post_path), 'post_frame_sha256': post_state.get('frame_sha256'), 'post_decision_reason': post_decision.reason, 'tap_count': adb.tap_count, 'device_command_count': adb.device_command_count, 'navigation_swipe_count': navigation.swipe_count}
    return (report, 0)

def _run_live_plus_probe(args: argparse.Namespace, base: dict[str, Any], plan: dict[str, Any], session: Any, adb: '_FileBackedADB') -> tuple[dict[str, Any], int]:
    result = SkillPurchaseExecutor(plan, adb=adb, parse_fresh_state=session.capture, mode='live', execute=True, safety_token=args.safety_token, stage='select_plus_probe', expected_action_id=args.expected_action_id).run()
    report = {**base, 'performed': result.tap_count == 1, 'action_id': result.actions[0] if result.actions else None, 'stop_reason': result.stop_reason, 'actions': list(result.actions), 'tap_count': result.tap_count, 'device_command_count': adb.device_command_count, 'captured_frames': [str(path) for path in adb.captured_paths]}
    return (report, 0 if result.stop_reason == 'select_plus_probe_complete' else 2)

def _run_live_open_confirmation_probe(args: argparse.Namespace, base: dict[str, Any], plan: dict[str, Any], session: Any, adb: '_FileBackedADB') -> tuple[dict[str, Any], int]:
    result = SkillPurchaseExecutor(plan, adb=adb, parse_fresh_state=session.capture, mode='live', execute=True, safety_token=SKILL_PURCHASE_SAFETY_TOKEN, stage='open_confirmation_probe', expected_action_id=args.expected_action_id).run()
    frames = list(adb.captured_paths)
    report = {**base, 'performed': result.tap_count == 1, 'action_id': result.actions[0] if result.actions else None, 'stop_reason': result.stop_reason, 'actions': list(result.actions), 'tap_count': result.tap_count, 'device_command_count': adb.device_command_count, 'captured_frames': [str(path) for path in frames], 'pre_frame_path': str(frames[0]) if len(frames) >= 1 else None, 'pre_frame_sha256': _sha256_file(frames[0]) if len(frames) >= 1 else None, 'post_frame_path': str(frames[1]) if len(frames) >= 2 else None, 'post_frame_sha256': _sha256_file(frames[1]) if len(frames) >= 2 else None, 'final_confirmation_performed': False}
    return (report, 0 if result.stop_reason == 'open_confirmation_probe_complete' else 2)

def _run_live_multi_confirmation_capture_probe(args: argparse.Namespace, base: dict[str, Any], plan: dict[str, Any], session: Any, adb: '_FileBackedADB') -> tuple[dict[str, Any], int]:
    marker_path = adb.output_dir / 'multi_confirmation_capture_state.json'
    capture_contract = plan.get('confirmation_fixture_capture')
    _write_json_atomic(marker_path, {'schema_version': 'uma.skill_multi_confirmation_capture_state.v1', 'status': 'capture_in_progress', 'plan_id': plan.get('plan_id'), 'session_id': plan.get('execution_promotion', {}).get('session_id'), 'capture_batch_id': plan.get('execution_promotion', {}).get('fresh_capture_id'), 'purchase_binding_sha256': capture_contract.get('purchase_binding_sha256') if isinstance(capture_contract, dict) else None, 'final_confirmation_performed': False, 'terminal_handoff_required': True})
    result = SkillPurchaseExecutor(plan, adb=adb, parse_fresh_state=session.capture, mode='live', execute=True, safety_token=args.safety_token, stage='multi_confirmation_capture_probe', expected_action_id=args.expected_action_id).run()
    frames = list(adb.captured_paths)
    completed = result.stop_reason == 'multi_confirmation_capture_probe_complete'
    terminal_state = 'post_green_capture_saved_unverified' if completed else 'selection_mutated_incomplete' if result.tap_count > 0 else 'stopped'
    _write_json_atomic(marker_path, {'schema_version': 'uma.skill_multi_confirmation_capture_state.v1', 'status': terminal_state, 'plan_id': plan.get('plan_id'), 'session_id': plan.get('execution_promotion', {}).get('session_id'), 'capture_batch_id': plan.get('execution_promotion', {}).get('fresh_capture_id'), 'purchase_binding_sha256': capture_contract.get('purchase_binding_sha256') if isinstance(capture_contract, dict) else None, 'stop_reason': result.stop_reason, 'captured_frames': [str(path) for path in frames], 'final_confirmation_performed': False, 'dialog_state_verified': False, 'terminal_handoff_required': True})
    report = {**base, 'performed': result.tap_count > 0, 'completed': completed, 'action_id': _MULTI_CONFIRMATION_CAPTURE_ACTION_ID if completed else None, 'stop_reason': result.stop_reason, 'actions': list(result.actions), 'tap_count': result.tap_count, 'device_command_count': adb.device_command_count, 'captured_frames': [str(path) for path in frames], 'capture_only': True, 'terminal_state': terminal_state, 'final_confirmation_performed': False, 'final_confirm_control_resolved': False, 'dialog_state_verified': False, 'terminal_handoff_required': True, 'capture_state_path': str(marker_path)}
    return (report, 0 if completed else 2)

def _run_live_final_confirmation_probe(args: argparse.Namespace, base: dict[str, Any], plan: dict[str, Any], session: Any, adb: '_FileBackedADB') -> tuple[dict[str, Any], int]:
    result = SkillPurchaseExecutor(plan, adb=adb, parse_fresh_state=session.capture, dialog_verifier=CurrentRunSkillDialogVerifier(), mode='live', execute=True, safety_token=SKILL_PURCHASE_SAFETY_TOKEN, stage='final_confirmation_probe', expected_action_id=args.expected_action_id).run()
    frames = list(adb.captured_paths)
    report = {**base, 'performed': result.tap_count == 1, 'action_id': result.actions[0] if result.actions else None, 'stop_reason': result.stop_reason, 'actions': list(result.actions), 'tap_count': result.tap_count, 'device_command_count': adb.device_command_count, 'captured_frames': [str(path) for path in frames], 'pre_frame_path': str(frames[0]) if len(frames) >= 1 else None, 'pre_frame_sha256': _sha256_file(frames[0]) if len(frames) >= 1 else None, 'post_frame_path': str(frames[1]) if len(frames) >= 2 else None, 'post_frame_sha256': _sha256_file(frames[1]) if len(frames) >= 2 else None, 'final_confirmation_performed': result.tap_count == 1}
    return (report, 0 if result.stop_reason == 'final_confirmation_probe_complete' else 2)

def _run_live_close_success_probe(args: argparse.Namespace, base: dict[str, Any], plan: dict[str, Any], session: Any, adb: '_FileBackedADB') -> tuple[dict[str, Any], int]:
    result = SkillPurchaseExecutor(plan, adb=adb, parse_fresh_state=session.capture, mode='live', execute=True, safety_token=SKILL_PURCHASE_SAFETY_TOKEN, stage='close_success_probe', expected_action_id=args.expected_action_id).run()
    frames = list(adb.captured_paths)
    report = {**base, 'performed': result.tap_count == 1, 'action_id': result.actions[0] if result.actions else None, 'stop_reason': result.stop_reason, 'actions': list(result.actions), 'tap_count': result.tap_count, 'device_command_count': adb.device_command_count, 'captured_frames': [str(path) for path in frames], 'pre_frame_path': str(frames[0]) if len(frames) >= 1 else None, 'pre_frame_sha256': _sha256_file(frames[0]) if len(frames) >= 1 else None, 'post_frame_path': str(frames[1]) if len(frames) >= 2 else None, 'post_frame_sha256': _sha256_file(frames[1]) if len(frames) >= 2 else None}
    return (report, 0 if result.stop_reason == 'close_success_probe_complete' else 2)

def _run_live_batch_close_success_probe(args: argparse.Namespace, base: dict[str, Any], plan: dict[str, Any], session: Any, adb: '_FileBackedADB') -> tuple[dict[str, Any], int]:
    result = SkillPurchaseExecutor(plan, adb=adb, parse_fresh_state=session.capture, mode='live', execute=True, safety_token=SKILL_PURCHASE_SAFETY_TOKEN, stage='batch_close_success_probe', expected_action_id=args.expected_action_id).run()
    frames = list(adb.captured_paths)
    report = {**base, 'performed': result.tap_count == 1, 'action_id': result.actions[0] if result.actions else None, 'stop_reason': result.stop_reason, 'actions': list(result.actions), 'tap_count': result.tap_count, 'device_command_count': adb.device_command_count, 'captured_frames': [str(path) for path in frames], 'pre_frame_path': str(frames[0]) if len(frames) >= 1 else None, 'pre_frame_sha256': _sha256_file(frames[0]) if len(frames) >= 1 else None, 'post_frame_path': str(frames[1]) if len(frames) >= 2 else None, 'post_frame_sha256': _sha256_file(frames[1]) if len(frames) >= 2 else None}
    return (report, 0 if result.stop_reason == 'batch_close_success_probe_complete' else 2)

def _run_live_return_after_purchase_probe(args: argparse.Namespace, base: dict[str, Any], plan: dict[str, Any], session: Any, adb: '_FileBackedADB', final_confirmation_report_sha256: str, success_report_sha256: str) -> tuple[dict[str, Any], int]:

    def capture_with_confirmation_evidence(frame: Any) -> dict[str, Any]:
        return _bind_purchase_success_evidence(session.capture(frame), final_confirmation_report_sha256, success_report_sha256)
    result = SkillPurchaseExecutor(plan, adb=adb, parse_fresh_state=capture_with_confirmation_evidence, mode='live', execute=True, safety_token=SKILL_PURCHASE_SAFETY_TOKEN, stage='return_after_purchase_probe', expected_action_id=args.expected_action_id).run()
    frames = list(adb.captured_paths)
    report = {**base, 'performed': result.tap_count == 1, 'action_id': result.actions[0] if result.actions else None, 'stop_reason': result.stop_reason, 'actions': list(result.actions), 'tap_count': result.tap_count, 'device_command_count': adb.device_command_count, 'captured_frames': [str(path) for path in frames], 'pre_frame_path': str(frames[0]) if len(frames) >= 1 else None, 'pre_frame_sha256': _sha256_file(frames[0]) if len(frames) >= 1 else None, 'post_frame_path': str(frames[1]) if len(frames) >= 2 else None, 'post_frame_sha256': _sha256_file(frames[1]) if len(frames) >= 2 else None, 'final_confirmation_report_sha256': final_confirmation_report_sha256, 'success_report_sha256': success_report_sha256}
    return (report, 0 if result.stop_reason == 'return_after_purchase_probe_complete' else 2)

def _validate_live_gate(args: argparse.Namespace) -> str | None:
    if args.adb_port != ADB_PORT:
        return f'adb_port_must_be_{ADB_PORT}'
    if not args.execute:
        return 'execute_required'
    if args.stage == 'locate':
        if args.safety_token != CAPTURE_SAFETY_TOKEN:
            return 'capture_safety_token_mismatch'
        if args.expected_action_id not in {SCROLL_UP_ACTION_ID, REVEAL_BOTTOM_PARTIAL_ACTION_ID, RECOVER_SELECTED_TARGET_ACTION_ID}:
            return 'expected_action_id_mismatch'
    elif args.stage == 'select-plus-probe':
        if args.safety_token != SKILL_PURCHASE_SAFETY_TOKEN:
            return 'skill_purchase_safety_token_mismatch'
        if args.expected_action_id != _SELECT_PLUS_ACTION_ID:
            return 'expected_action_id_mismatch'
    elif args.stage == 'open-confirmation-probe':
        if args.safety_token != OPEN_CONFIRMATION_SAFETY_TOKEN:
            return 'open_confirmation_safety_token_mismatch'
        if args.expected_action_id != _OPEN_CONFIRMATION_ACTION_ID:
            return 'expected_action_id_mismatch'
    elif args.stage == 'multi-confirmation-capture-probe':
        if args.safety_token != SKILL_MULTI_CONFIRMATION_CAPTURE_TOKEN:
            return 'multi_confirmation_capture_safety_token_mismatch'
        if args.expected_action_id != _MULTI_CONFIRMATION_CAPTURE_ACTION_ID:
            return 'expected_action_id_mismatch'
    elif args.stage == 'final-confirmation-probe':
        if args.safety_token != FINAL_CONFIRMATION_SAFETY_TOKEN:
            return 'final_confirmation_safety_token_mismatch'
        if args.expected_action_id != _FINAL_CONFIRMATION_ACTION_ID:
            return 'expected_action_id_mismatch'
    elif args.stage in {'close-success-probe', 'batch-close-success-probe'}:
        if args.safety_token != CLOSE_SUCCESS_SAFETY_TOKEN:
            return 'close_success_safety_token_mismatch'
        if args.expected_action_id != _CLOSE_SUCCESS_ACTION_ID:
            return 'expected_action_id_mismatch'
    elif args.stage == 'return-after-purchase-probe':
        if args.safety_token != RETURN_AFTER_PURCHASE_SAFETY_TOKEN:
            return 'return_after_purchase_safety_token_mismatch'
        if args.expected_action_id != _RETURN_AFTER_PURCHASE_ACTION_ID:
            return 'expected_action_id_mismatch'
    else:
        return 'stage_invalid'
    return None

def _bind_purchase_success_evidence(state: dict[str, Any], report_sha256: str, success_report_sha256: str) -> dict[str, Any]:
    return {**state, 'final_confirmation_evidence_verified': True, 'final_confirmation_report_sha256': report_sha256, 'success_evidence_verified': True, 'success_report_sha256': success_report_sha256}

def _validate_final_confirmation_report(value: str | None, *, plan_id: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value:
        return (None, 'final_confirmation_report_required')
    path = Path(value).resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError:
        return (None, 'final_confirmation_report_outside_root')
    if not path.is_file():
        return (None, 'final_confirmation_report_missing')
    try:
        payload = path.read_bytes()
        report = json.loads(payload.decode('utf-8-sig'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return (None, 'final_confirmation_report_invalid')
    if not isinstance(report, dict) or report.get('plan_id') != plan_id or report.get('performed') is not True or (report.get('action_id') != _FINAL_CONFIRMATION_ACTION_ID) or (report.get('actions') != [_FINAL_CONFIRMATION_ACTION_ID]) or (report.get('tap_count') != 1) or (report.get('stop_reason') != 'final_confirmation_probe_complete') or (report.get('final_confirmation_performed') is not True):
        return (None, 'final_confirmation_report_contract_mismatch')
    for prefix in ('pre', 'post'):
        frame_path = Path(str(report.get(f'{prefix}_frame_path') or ''))
        if not frame_path.is_absolute():
            frame_path = ROOT / frame_path
        expected_sha = report.get(f'{prefix}_frame_sha256')
        if not frame_path.is_file() or not isinstance(expected_sha, str) or _sha256_file(frame_path) != expected_sha:
            return (None, f'final_confirmation_report_{prefix}_frame_mismatch')
    return (hashlib.sha256(payload).hexdigest(), None)

def _validate_success_report(value: str | None, *, plan_id: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value:
        return (None, 'success_report_required')
    path = Path(value).resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError:
        return (None, 'success_report_outside_root')
    if not path.is_file():
        return (None, 'success_report_missing')
    try:
        payload = path.read_bytes()
        report = json.loads(payload.decode('utf-8-sig'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return (None, 'success_report_invalid')
    if not isinstance(report, dict) or report.get('plan_id') != plan_id or report.get('performed') is not True or (report.get('action_id') != _CLOSE_SUCCESS_ACTION_ID) or (report.get('actions') != [_CLOSE_SUCCESS_ACTION_ID]) or (report.get('tap_count') != 1) or (report.get('stop_reason') != 'close_success_probe_complete'):
        return (None, 'success_report_contract_mismatch')
    for prefix in ('pre', 'post'):
        frame_path = Path(str(report.get(f'{prefix}_frame_path') or ''))
        if not frame_path.is_absolute():
            frame_path = ROOT / frame_path
        expected_sha = report.get(f'{prefix}_frame_sha256')
        if not frame_path.is_file() or not isinstance(expected_sha, str) or _sha256_file(frame_path) != expected_sha:
            return (None, f'success_report_{prefix}_frame_mismatch')
    return (hashlib.sha256(payload).hexdigest(), None)

def _load_or_start_navigation(args: argparse.Namespace, plan: dict[str, Any]) -> SkillPurchaseNavigationSession:
    path = Path(args.session_state)
    if path.is_file():
        return SkillPurchaseNavigationSession.from_dict(_read_json(path))
    promotion = plan['execution_promotion']
    return SkillPurchaseNavigationSession.start(plan_id=plan['plan_id'], session_id=promotion['session_id'], capture_batch_id=promotion['fresh_capture_id'], max_swipes=args.max_swipes)

class _FileBackedADB:

    def __init__(self, adb: Any, output_dir: Path) -> None:
        self._adb = adb
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.captured_paths: list[Path] = []
        self.tap_count = 0
        self.device_command_count = 0

    def screenshot(self) -> Path:
        index = len(list(self.output_dir.glob('frame_*.png')))
        path = self.output_dir / f'frame_{index:03d}.png'
        image = self._adb.screenshot(str(path))
        self.device_command_count += 1
        if image is None or not path.is_file():
            raise RuntimeError('fresh_screenshot_failed')
        self.captured_paths.append(path)
        return path

    def swipe(self, **gesture: Any) -> None:
        ok = self._adb.swipe(gesture['x1'], gesture['y1'], gesture['x2'], gesture['y2'], duration_ms=gesture['duration_ms'], scaled=gesture['scaled'])
        self.device_command_count += 1
        if ok is False:
            raise RuntimeError('navigation_swipe_failed')

    def tap(self, x: int, y: int, *, scaled: bool=False) -> None:
        ok = self._adb.tap(x, y, scaled=scaled)
        self.device_command_count += 1
        self.tap_count += 1
        if ok is False:
            raise RuntimeError('skill_plus_tap_failed')

class _NoDeviceADB:

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f'device capability forbidden in dry-run: {name}')

def _default_adb_factory(adb_port: int) -> Callable[[], Any]:

    def create() -> Any:
        from core.adb import ADB
        return ADB(host='127.0.0.1', port=adb_port)
    return create

def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8-sig'))

def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def build_live_session_receipt(*, args: argparse.Namespace, plan: dict[str, Any], final_confirmation_report_sha256: str | None=None, success_report_sha256: str | None=None) -> dict[str, Any]:
    promotion = plan['execution_promotion']
    receipt = {'schema_version': 'uma.skill_purchase_atomic_live_session.v1', 'status': 'consumed_before_device_connect', 'stage': args.stage, 'action_id': args.expected_action_id, 'plan_id': plan.get('plan_id'), 'plan_sha256': _sha256_file(Path(args.plan)), 'review_sha256': _sha256_file(Path(args.review)), 'session_id': promotion.get('session_id'), 'capture_batch_id': promotion.get('fresh_capture_id')}
    if args.stage == 'return-after-purchase-probe':
        if not isinstance(final_confirmation_report_sha256, str) or not isinstance(success_report_sha256, str):
            raise ValueError('return_evidence_sha256_required')
        receipt.update({'final_confirmation_report_sha256': final_confirmation_report_sha256, 'success_report_sha256': success_report_sha256})
    return receipt

def write_json_exclusive(path: Path, payload: Any) -> str:
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as handle:
        handle.write(data)
    if path.read_bytes() != data:
        raise OSError(f'artifact_write_verification_failed:{path.name}')
    return hashlib.sha256(data).hexdigest()

def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp', delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
if __name__ == '__main__':
    raise SystemExit(main())
