"""Close one freshly verified multi-skill success dialog, and stop."""
from __future__ import annotations
from desktop.umaa_public_context import endpoint as _public_endpoint
import hashlib
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
import cv2
from core.image_io import read_image
from core.autonomous_training.purchase import ExplicitDeviceTransportFacade
from core.skill_purchase_dialog import inspect_skill_multi_purchase_success_dialog, inspect_skill_purchase_success_dialog
from core.skill_purchase_executor import SKILL_PURCHASE_SAFETY_TOKEN, SkillPurchaseExecutor
from scripts.ug_parent_skill_purchase_atomic import _FileBackedADB
_ALLOWED_ENDPOINT = _public_endpoint()
_FINAL_ACTION = 'skill_purchase.final_confirm'
_CLOSE_ACTION = 'skill_purchase.close_success'
_REPORT_NAME = 'success_close_report.json'

def close_skill_purchase_success(*, device: Any, child_plan_path: str | Path, final_confirmation_report_path: str | Path, expected_final_confirmation_report_sha256: str, output_dir: str | Path, promotion_review_path: str | Path | None=None, mode: str='dry-run', execute: bool=False, safety_token: str | None=None, expected_endpoint: str=_ALLOWED_ENDPOINT) -> dict[str, Any]:
    """Verify the success modal, close it once, and never return/finish."""
    if expected_endpoint not in {_ALLOWED_ENDPOINT, _public_endpoint()} or getattr(device, 'endpoint', None) != expected_endpoint:
        return _result('device_endpoint_not_allowed', mode, execute)
    if mode not in {'dry-run', 'live'} or type(execute) is not bool:
        return _result('execution_mode_invalid', mode, execute)
    child_path = Path(child_plan_path)
    report_path = Path(final_confirmation_report_path)
    child = _read_mapping(child_path)
    report = _read_mapping(report_path)
    if child is None or report is None:
        return _result('success_close_artifact_unreadable', mode, execute)
    child_ids = _child_purchase_ids(child)
    if child_ids is None:
        return _result('final_authorized_child_invalid', mode, execute)
    if not _is_sha256(expected_final_confirmation_report_sha256):
        return _result('final_confirmation_report_sha256_invalid', mode, execute)
    if _sha256_file(report_path) != expected_final_confirmation_report_sha256:
        return _result('final_confirmation_report_sha256_mismatch', mode, execute)
    report_error = _validate_final_report(report, child_path, child)
    if report_error:
        return _result(report_error, mode, execute, child=child)
    base = {**_result('success_close_preflight_ready', mode, execute, child=child), 'child_plan_sha256': _sha256_file(child_path)}
    if mode == 'dry-run' or execute is not True:
        return _write_report(Path(output_dir), {**base, 'stop_reason': 'dry_run_no_device_actions'})
    if safety_token != SKILL_PURCHASE_SAFETY_TOKEN:
        return _write_report(Path(output_dir), _result('execution_gate_rejected', mode, execute, child=child))
    if not callable(getattr(device, 'command', None)):
        return _write_report(Path(output_dir), _result('explicit_device_command_required', mode, execute, child=child))
    root = Path(output_dir)
    reviewed_rows: Mapping[str, Any] | None = None
    if len(child_ids) == 1:
        if promotion_review_path is None:
            return _write_report(root, _result('promotion_review_required', mode, execute, child=child))
        review = _read_mapping(Path(promotion_review_path))
        candidate_rows = review.get('reviewed_rows') if isinstance(review, Mapping) else None
        if not isinstance(candidate_rows, Mapping):
            return _write_report(root, _result('promotion_review_unreadable', mode, execute, child=child))
        reviewed_rows = candidate_rows
    facade = ExplicitDeviceTransportFacade(device, expected_endpoint=expected_endpoint)
    adb = _FileBackedADB(facade, root / 'capture')
    frame_sequence = 0

    def parse_fresh_state(frame: Any) -> dict[str, Any]:
        nonlocal frame_sequence
        frame_sequence += 1
        image = read_image(str(frame), cv2.IMREAD_COLOR)
        state = dict(inspect_skill_purchase_success_dialog(image, reviewed_rows=reviewed_rows) if reviewed_rows is not None else inspect_skill_multi_purchase_success_dialog(image, confirmed_item_ids=child_ids, verified_selection=child.get('final_authorization', {}).get('confirmation_policy') == 'verified_selection'))
        promotion = child['execution_promotion']
        state.update({'fresh': True, 'plan_id': child['plan_id'], 'sample_id': promotion['sample_id'], 'session_id': promotion['session_id'], 'capture_batch_id': promotion['fresh_capture_id'], 'frame_capture_id': f'success-close-{frame_sequence}', 'frame_sequence': frame_sequence, 'frame_sha256': _sha256_file(Path(frame)), 'planned_item_ids': child_ids, 'auto_included_item_ids': []})
        return state
    for attempt in range(4):
        attempt_frame_start = len(adb.captured_paths)
        execution = SkillPurchaseExecutor(child, adb=adb, parse_fresh_state=parse_fresh_state, mode='live', execute=True, safety_token=SKILL_PURCHASE_SAFETY_TOKEN, stage=_close_stage(child_ids), expected_action_id=_CLOSE_ACTION).run()
        if execution.actions or execution.tap_count or execution.stop_reason not in {'success_dialog_page_mismatch', 'batch_success_dialog_page_mismatch'} or (attempt == 3):
            break
        time.sleep(0.5)
    performed = execution.stop_reason == _close_complete_stop(child_ids) and execution.actions == (_CLOSE_ACTION,) and (execution.tap_count == 1)
    frames = list(adb.captured_paths)
    attempt_frames = frames[attempt_frame_start:]
    return _write_report(root, {**base, 'stop_reason': execution.stop_reason, 'actions': list(execution.actions), 'device_action_count': execution.tap_count, 'device_command_count': adb.device_command_count, 'close_success_performed': performed, 'final_confirmation_report_sha256': expected_final_confirmation_report_sha256, 'source_frame_sha256': _sha256_file(attempt_frames[0]) if attempt_frames else None, 'post_frame_sha256': _sha256_file(attempt_frames[1]) if len(attempt_frames) > 1 else None, 'captured_frames': [str(path) for path in frames]})

def _child_purchase_ids(child: Mapping[str, Any]) -> list[str] | None:
    authorization = child.get('final_authorization')
    purchases = child.get('purchases')
    promotion = child.get('execution_promotion')
    if child.get('schema_version') != 'uma.skill_purchase_plan.v1' or child.get('mode') != 'live' or child.get('execution_allowed') is not True or (child.get('multi_skill_live_gate') != 'current_run_confirmation_closed') or (not isinstance(authorization, Mapping)) or (authorization.get('schema_version') != 'uma.skill_final_authorization.v1') or (not _is_sha256(authorization.get('parent_plan_sha256'))) or (not _is_sha256(authorization.get('final_evidence_sha256'))) or (not isinstance(child.get('plan_id'), str)) or (not isinstance(promotion, Mapping)) or (not all((isinstance(promotion.get(key), str) and promotion[key] for key in ('sample_id', 'session_id', 'fresh_capture_id')))) or (not isinstance(purchases, list)) or (not purchases):
        return None
    ids = [item.get('item_id') if isinstance(item, Mapping) else None for item in purchases]
    if any((not isinstance(item_id, str) or not item_id for item_id in ids)) or len(ids) != len(set(ids)):
        return None
    return list(ids)

def _validate_final_report(report: Mapping[str, Any], child_path: Path, child: Mapping[str, Any]) -> str | None:
    if report.get('schema_version') != 'uma.autonomous_final_skill_confirmation.v1' or report.get('mode') != 'live' or report.get('execute') is not True or (report.get('stop_reason') != _confirmation_complete_stop(child)) or (report.get('final_confirmation_performed') is not True) or (report.get('actions') != [_FINAL_ACTION]) or (report.get('device_action_count') != 1) or (report.get('plan_id') != child.get('plan_id')) or (report.get('promotion_plan_path') != str(child_path.resolve())) or (report.get('promotion_plan_sha256') != _sha256_file(child_path)):
        return 'final_confirmation_report_binding_invalid'
    return None

def _confirmation_complete_stop(child: Mapping[str, Any]) -> str:
    purchases = child.get('purchases')
    return 'final_confirmation_probe_complete' if isinstance(purchases, list) and len(purchases) == 1 else 'batch_final_confirmation_probe_complete'

def _close_stage(child_ids: list[str]) -> str:
    return 'close_success_probe' if len(child_ids) == 1 else 'batch_close_success_probe'

def _close_complete_stop(child_ids: list[str]) -> str:
    return 'close_success_probe_complete' if len(child_ids) == 1 else 'batch_close_success_probe_complete'

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
    return {'schema_version': 'uma.autonomous_skill_success_close.v1', 'mode': mode, 'execute': execute, 'stop_reason': stop_reason, 'plan_id': child.get('plan_id') if isinstance(child, Mapping) else None, 'child_plan_sha256': None, 'close_success_performed': False, 'actions': [], 'device_action_count': 0}

def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all((character in '0123456789abcdef' for character in value))
