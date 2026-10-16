"""Fail-closed final confirmation for a closed multi-page skill batch."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping
import cv2
ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parents[1] if ROOT.parent.name == '.worktrees' else ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from config.settings import ADB_ENDPOINT, ADB_PORT
from core.autonomous_training.final_authorization import validate_completed_selection
from core.skill_purchase_dialog import CurrentRunMultiPageSkillDialogVerifier, inspect_skill_multi_page_purchase_confirmation_dialog, inspect_verified_selection_confirmation
from core.skill_purchase_executor import SKILL_PURCHASE_SAFETY_TOKEN, SkillPurchaseExecutor
from scripts.ug_parent_skill_purchase_atomic import _FileBackedADB, _default_adb_factory
BATCH_FINAL_CONFIRMATION_SAFETY_TOKEN = 'UG_PARENT_SKILL_PURCHASE_BATCH_FINAL_CONFIRM'
FINAL_CONFIRMATION_ACTION_ID = 'skill_purchase.final_confirm'
LIVE_SESSION_RECEIPT_NAME = 'batch_final_confirmation_live_session.json'
FINAL_REPORT_NAME = 'batch_final_confirmation_report.json'

def parse_args(argv: list[str] | None=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Verify a closed multi-page skill dialog, then tap once.')
    parser.add_argument('--plan', required=True)
    parser.add_argument('--closure-contract', required=True)
    parser.add_argument('--top-frame', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--input-image')
    parser.add_argument('--adb-port', type=int, required=True)
    parser.add_argument('--mode', choices=('dry-run', 'live'), default='dry-run')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--safety-token')
    parser.add_argument('--expected-action-id')
    return parser.parse_args(argv)

def validate_live_gate(*, adb_port: int, execute: bool, safety_token: str | None, expected_action_id: str | None) -> str | None:
    if adb_port != ADB_PORT:
        return f'adb_port_must_be_{ADB_PORT}'
    if not execute:
        return 'execute_required'
    if safety_token != BATCH_FINAL_CONFIRMATION_SAFETY_TOKEN:
        return 'batch_final_confirmation_safety_token_mismatch'
    if expected_action_id != FINAL_CONFIRMATION_ACTION_ID:
        return 'expected_action_id_mismatch'
    return None

def validate_closure_contract(contract: Any, *, plan: dict[str, Any], plan_path: Path, top_frame: Path) -> str | None:
    if not isinstance(contract, dict) or contract.get('schema_version') != 'uma.skill_multi_page_confirmation_contract.v1' or contract.get('final_confirmation_allowed') is not True:
        return 'closure_contract_invalid'
    if contract.get('plan_sha256') != _sha256_file(plan_path):
        return 'closure_plan_sha256_mismatch'
    if contract.get('confirmation_policy') != plan.get('final_authorization', {}).get('confirmation_policy'):
        return 'closure_confirmation_policy_mismatch'
    if contract.get('top_frame_sha256') != _sha256_file(top_frame):
        return 'closure_top_frame_sha256_mismatch'
    rows = contract.get('expected_rows')
    verifier = CurrentRunMultiPageSkillDialogVerifier(rows)
    if not verifier.expected_rows:
        return 'closure_expected_rows_invalid'
    purchases = plan.get('purchases')
    if not isinstance(purchases, list):
        return 'closure_plan_invalid'
    plan_ids = [str(purchase.get('item_id')) for purchase in purchases if isinstance(purchase, dict)]
    expected_ids = [str(row['item_id']) for row in verifier.expected_rows if row['kind'] == 'planned']
    if plan_ids != expected_ids:
        return 'closure_planned_item_ids_mismatch'
    checkpoint_value = contract.get('selection_checkpoint_path')
    checkpoint_sha256 = contract.get('selection_checkpoint_sha256')
    if not isinstance(checkpoint_value, str) or not checkpoint_value:
        return 'closure_selection_checkpoint_invalid'
    checkpoint_path = (PROJECT_ROOT / checkpoint_value).resolve()
    try:
        checkpoint_path.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return 'closure_selection_checkpoint_invalid'
    if not checkpoint_path.is_file() or checkpoint_sha256 != _sha256_file(checkpoint_path):
        return 'closure_selection_checkpoint_sha256_mismatch'
    checkpoint = _read_json(checkpoint_path)
    if not isinstance(checkpoint, dict) or validate_completed_selection(checkpoint, plan_ids):
        return 'closure_selection_checkpoint_state_mismatch'
    if contract.get('expected_remaining_skill_points') != plan.get('expected_remaining_skill_points'):
        return 'closure_remaining_skill_points_mismatch'
    return None

def build_multi_page_dialog_state(*, plan: dict[str, Any], contract: dict[str, Any], top_frame: Path, current_frame: Path, frame_sequence: int) -> dict[str, Any]:
    current = cv2.imread(str(current_frame), cv2.IMREAD_COLOR)
    frames = [current]
    selection_verified = contract.get('confirmation_policy') == 'verified_selection'
    if len(contract['expected_rows']) > 5 and (not selection_verified):
        top = cv2.imread(str(top_frame), cv2.IMREAD_COLOR)
        middle = contract.get('intermediate_frames', [])
        if any((not Path(item['path']).is_file() or _sha256_file(Path(item['path'])) != item['sha256'] for item in middle)):
            return {'accepted': False, 'page_kind': 'unknown', 'reason': 'intermediate_frame_mismatch'}
        frames = [top, *[cv2.imread(item['path'], cv2.IMREAD_COLOR) for item in middle], current]
    state = inspect_verified_selection_confirmation(current) if selection_verified else inspect_skill_multi_page_purchase_confirmation_dialog(frames, expected_rows=contract['expected_rows'])
    promotion = plan['execution_promotion']
    frame_sha256 = _sha256_file(current_frame)
    return {**state, 'fresh': True, 'plan_id': plan.get('plan_id'), 'sample_id': promotion.get('sample_id'), 'session_id': promotion.get('session_id'), 'capture_batch_id': promotion.get('fresh_capture_id'), 'frame_capture_id': f'batch-final-{frame_sequence}-{frame_sha256[:16]}', 'frame_sequence': frame_sequence, 'frame_sha256': frame_sha256}

def build_live_session_receipt(*, plan: dict[str, Any], plan_path: Path, contract_path: Path, contract: dict[str, Any], top_frame: Path) -> dict[str, Any]:
    """Build an immutable, pre-action receipt that consumes one live session.

    The receipt is intentionally written before ADB is constructed.  If the
    process crashes at any later point, a repeated invocation fails closed
    instead of risking a second confirmation tap.
    """
    return {'schema_version': 'uma.skill_batch_final_confirmation_session.v1', 'status': 'consumed_before_device_connect', 'action_id': FINAL_CONFIRMATION_ACTION_ID, 'plan_id': plan.get('plan_id'), 'plan_sha256': _sha256_file(plan_path), 'closure_contract_sha256': _sha256_file(contract_path), 'top_frame_sha256': _sha256_file(top_frame), 'selection_checkpoint_sha256': contract.get('selection_checkpoint_sha256'), 'expected_remaining_skill_points': plan.get('expected_remaining_skill_points')}

def write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> str:
    if sys.platform == 'win32':
        absolute = str(path.resolve())
        if not absolute.startswith('\\\\?\\'):
            absolute = '\\\\?\\UNC\\' + absolute[2:] if absolute.startswith('\\\\') else '\\\\?\\' + absolute
        path = Path(absolute)
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as handle:
        handle.write(data)
    if path.read_bytes() != data:
        raise OSError(f'artifact_write_verification_failed:{path.name}')
    return hashlib.sha256(data).hexdigest()

def write_live_session_receipt_exclusive(path: Path, receipt: Mapping[str, Any]) -> str:
    try:
        return write_json_exclusive(path, receipt)
    except FileExistsError as exc:
        raise FileExistsError('live_session_already_consumed') from exc

def run(args: argparse.Namespace, *, adb_factory: Callable[[], Any] | None=None) -> tuple[dict[str, Any], int]:
    plan_path = _inside_root_file(args.plan)
    contract_path = _inside_root_file(args.closure_contract)
    top_frame = _inside_root_file(args.top_frame)
    output_dir = _inside_root_output(args.output_dir)
    plan = _read_json(plan_path)
    contract = _read_json(contract_path)
    error = validate_closure_contract(contract, plan=plan, plan_path=plan_path, top_frame=top_frame)
    base = {'schema_version': 'uma.skill_batch_final_confirmation_report.v1', 'is_valid': error is None, 'performed': False, 'plan_id': plan.get('plan_id'), 'plan_sha256': _sha256_file(plan_path), 'closure_contract_sha256': _sha256_file(contract_path), 'top_frame_sha256': _sha256_file(top_frame), 'expected_remaining_skill_points': plan.get('expected_remaining_skill_points'), 'final_confirmation_performed': False}
    if error:
        return ({**base, 'stop_reason': error}, 2)
    verifier = CurrentRunMultiPageSkillDialogVerifier(contract['expected_rows'])
    frame_sequence = 0
    last_state: dict[str, Any] = {}

    def parse_current(frame: Any) -> dict[str, Any]:
        nonlocal frame_sequence, last_state
        frame_sequence += 1
        last_state = build_multi_page_dialog_state(plan=plan, contract=contract, top_frame=top_frame, current_frame=Path(frame), frame_sequence=frame_sequence)
        return last_state
    if args.mode == 'dry-run':
        if args.execute:
            return ({**base, 'is_valid': False, 'stop_reason': 'dry_run_cannot_execute'}, 2)
        if not args.input_image:
            return ({**base, 'is_valid': False, 'stop_reason': 'input_image_required'}, 2)
        state = parse_current(_inside_root_file(args.input_image))
        result = SkillPurchaseExecutor(plan, adb=object(), parse_fresh_state=lambda frame: frame, dialog_verifier=verifier, stage='batch_final_confirmation_probe').preview_batch_final_confirmation_state(state)
        report = _report_from_state(base, state, stop_reason=result.stop_reason, performed=False, captured_frames=[], device_command_count=0)
        write_json_exclusive(output_dir / FINAL_REPORT_NAME, report)
        return (report, 0 if result.stop_reason == 'batch_final_confirmation_ready' else 2)
    gate_error = validate_live_gate(adb_port=args.adb_port, execute=args.execute, safety_token=args.safety_token, expected_action_id=args.expected_action_id)
    if gate_error:
        return ({**base, 'is_valid': False, 'stop_reason': gate_error}, 2)
    report_path = output_dir / FINAL_REPORT_NAME
    if report_path.exists():
        return ({**base, 'is_valid': False, 'stop_reason': 'final_confirmation_report_already_exists'}, 2)
    receipt = build_live_session_receipt(plan=plan, plan_path=plan_path, contract_path=contract_path, contract=contract, top_frame=top_frame)
    try:
        receipt_sha256 = write_live_session_receipt_exclusive(output_dir / LIVE_SESSION_RECEIPT_NAME, receipt)
    except FileExistsError:
        return ({**base, 'is_valid': False, 'stop_reason': 'live_session_already_consumed'}, 2)
    create_adb = adb_factory or _default_adb_factory(args.adb_port)
    adb = create_adb()
    if getattr(adb, 'address', None) != ADB_ENDPOINT:
        return ({**base, 'is_valid': False, 'stop_reason': 'adb_address_mismatch'}, 2)
    try:
        if adb.connect() is not True:
            return ({**base, 'is_valid': False, 'stop_reason': 'adb_connect_failed', 'live_session_receipt_sha256': receipt_sha256}, 2)
        file_adb = _FileBackedADB(adb, output_dir / 'capture')
        result = SkillPurchaseExecutor(plan, adb=file_adb, parse_fresh_state=parse_current, dialog_verifier=verifier, mode='live', execute=True, safety_token=SKILL_PURCHASE_SAFETY_TOKEN, stage='batch_final_confirmation_probe', expected_action_id=args.expected_action_id).run()
        frames = list(file_adb.captured_paths)
        execution_complete = bool(result.stop_reason == 'batch_final_confirmation_probe_complete' and result.tap_count == 1 and (tuple(result.actions) == (FINAL_CONFIRMATION_ACTION_ID,)))
        effective_stop_reason = result.stop_reason if execution_complete or result.stop_reason != 'batch_final_confirmation_probe_complete' else 'final_confirmation_execution_incomplete'
        report = _report_from_state(base, last_state, stop_reason=effective_stop_reason, performed=execution_complete, captured_frames=frames, device_command_count=file_adb.device_command_count)
        report['actions'] = list(result.actions)
        report['tap_count'] = result.tap_count
        report['post_frame_sha256'] = _sha256_file(frames[1]) if len(frames) >= 2 else None
        report['final_confirmation_performed'] = execution_complete
        report['live_session_receipt_sha256'] = receipt_sha256
        write_json_exclusive(report_path, report)
        return (report, 0 if execution_complete else 2)
    finally:
        try:
            adb.disconnect()
        except Exception:
            pass

def _report_from_state(base: dict[str, Any], state: dict[str, Any], *, stop_reason: str, performed: bool, captured_frames: list[Path], device_command_count: int) -> dict[str, Any]:
    return {**base, 'is_valid': stop_reason in {'batch_final_confirmation_ready', 'batch_final_confirmation_probe_complete'}, 'performed': performed, 'stop_reason': stop_reason, 'confirmed_item_ids': state.get('confirmed_item_ids', []), 'planned_item_ids': state.get('planned_item_ids', []), 'auto_included_item_ids': state.get('auto_included_item_ids', []), 'page_row_indexes': state.get('page_row_indexes', []), 'dangerous_resource_flags': state.get('dangerous_resource_flags', []), 'current_frame_sha256': state.get('frame_sha256'), 'captured_frames': [str(path) for path in captured_frames], 'device_command_count': device_command_count}

def _inside_root_file(value: str) -> Path:
    path = Path(value).resolve()
    path.relative_to(PROJECT_ROOT.resolve())
    if not path.is_file():
        raise FileNotFoundError(path)
    return path

def _inside_root_output(value: str) -> Path:
    path = Path(value).resolve()
    path.relative_to(PROJECT_ROOT.resolve())
    path.mkdir(parents=True, exist_ok=True)
    return path

def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8-sig'))

def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main(argv: list[str] | None=None) -> int:
    try:
        report, code = run(parse_args(argv))
    except Exception as exc:
        report, code = ({'schema_version': 'uma.skill_batch_final_confirmation_report.v1', 'is_valid': False, 'performed': False, 'final_confirmation_performed': False, 'stop_reason': f'{type(exc).__name__}: {exc}'}, 2)
    print(json.dumps(report, ensure_ascii=True, separators=(',', ':')))
    return code
if __name__ == '__main__':
    raise SystemExit(main())
