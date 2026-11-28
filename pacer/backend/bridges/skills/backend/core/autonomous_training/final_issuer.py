"""Issue final-confirmation artifacts from already observed dialog evidence."""
from __future__ import annotations
import hashlib
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from core.autonomous_training.final_authorization import derive_final_authorized_child
from scripts.ug_parent_skill_purchase_batch_final_confirm import PROJECT_ROOT

def issue_final_confirmation_artifacts(*, parent_plan_path: str | Path, review_path: str | Path, promotion_evidence_path: str | Path, selection_checkpoint_path: str | Path, selection_ledger_path: str | Path, selection_ledger_frame_path: str | Path, top_frame_path: str | Path, current_frame_path: str | Path, detector_state: Mapping[str, Any], top_captured_at_unix_ms: int, current_captured_at_unix_ms: int, output_dir: str | Path, now_unix_ms: int | None=None, project_root: Path=PROJECT_ROOT, intermediate_frames: list[dict[str, str]] | None=None) -> dict[str, Any]:
    """Write a child/evidence/closure trio only from a closed observed dialog."""
    paths = {'parent_plan': Path(parent_plan_path), 'review': Path(review_path), 'promotion_evidence': Path(promotion_evidence_path), 'selection_checkpoint': Path(selection_checkpoint_path), 'selection_ledger': Path(selection_ledger_path), 'selection_ledger_frame': Path(selection_ledger_frame_path), 'top_frame': Path(top_frame_path), 'current_frame': Path(current_frame_path)}
    if any((not path.is_file() for path in paths.values())):
        return _result('issuer_input_unreadable')
    parent_plan = _read_mapping(paths['parent_plan'])
    review = _read_mapping(paths['review'])
    checkpoint = _read_mapping(paths['selection_checkpoint'])
    ledger = _read_mapping(paths['selection_ledger'])
    if any((value is None for value in (parent_plan, review, checkpoint, ledger))):
        return _result('issuer_input_invalid')
    assert parent_plan is not None and review is not None
    assert checkpoint is not None and ledger is not None
    expected_rows, row_error = _expected_rows(parent_plan, review)
    if row_error:
        return _result(row_error)
    assert expected_rows is not None
    selection_verified = detector_state.get('confirmation_policy') == 'verified_selection'
    if selection_verified:
        from core.skill_purchase_dialog import VerifiedSelectionDialogVerifier
        result = VerifiedSelectionDialogVerifier().verify(detector_state, {'final_authorization': {'confirmation_policy': 'verified_selection'}})
        dialog_error = None if result.verified else result.reason
    else:
        dialog_error = _validate_detector_state(detector_state, expected_rows)
    if dialog_error:
        return _result(dialog_error)
    if type(top_captured_at_unix_ms) is not int or type(current_captured_at_unix_ms) is not int:
        return _result('dialog_capture_timing_invalid')
    current_time = int(time.time() * 1000) if now_unix_ms is None else now_unix_ms
    if type(current_time) is not int:
        return _result('now_unix_ms_invalid')
    evidence = {'schema_version': 'uma.skill_final_confirmation_evidence.v1', 'parent_plan_sha256': _sha256(paths['parent_plan']), 'promotion_review_sha256': _sha256(paths['review']), 'promotion_evidence_sha256': _sha256(paths['promotion_evidence']), 'selection_checkpoint_sha256': _sha256(paths['selection_checkpoint']), 'selection_ledger_sha256': _sha256(paths['selection_ledger']), 'session_id': review.get('session_id'), 'capture_binding': review.get('capture_binding'), 'dialog': {'top_frame_sha256': _sha256(paths['top_frame']), 'top_captured_at_unix_ms': top_captured_at_unix_ms, 'current_frame_sha256': _sha256(paths['current_frame']), 'current_captured_at_unix_ms': current_captured_at_unix_ms, 'layout_closed': True, 'row_count': len(expected_rows), 'rows': [{'item_id': row['item_id']} for row in expected_rows]}}
    evidence_bytes = _json_bytes(evidence)
    if selection_verified:
        evidence['dialog'].update(confirmation_policy='verified_selection', layout_closed=False, row_count=0, rows=[])
        evidence_bytes = _json_bytes(evidence)
    child, authorization_error = derive_final_authorized_child(parent_plan=parent_plan, review=review, checkpoint=checkpoint, ledger=ledger, final_evidence=evidence, parent_plan_sha256=evidence['parent_plan_sha256'], final_evidence_sha256=hashlib.sha256(evidence_bytes).hexdigest(), promotion_review_sha256=evidence['promotion_review_sha256'], promotion_evidence_sha256=evidence['promotion_evidence_sha256'], selection_checkpoint_sha256=evidence['selection_checkpoint_sha256'], selection_ledger_sha256=evidence['selection_ledger_sha256'], selection_ledger_frame_sha256=_sha256(paths['selection_ledger_frame']), top_frame_sha256=evidence['dialog']['top_frame_sha256'], current_frame_sha256=evidence['dialog']['current_frame_sha256'], now_unix_ms=current_time)
    if authorization_error:
        return _result(authorization_error)
    assert child is not None
    root = Path(output_dir)
    child_path = root / 'promotion_plan.final_child.json'
    evidence_path = root / 'final_confirmation_evidence.json'
    contract_path = root / 'closure_contract.json'
    if any((path.exists() for path in (child_path, evidence_path, contract_path))):
        return _result('issuer_output_already_exists')
    try:
        checkpoint_relative = paths['selection_checkpoint'].resolve().relative_to(project_root.resolve())
    except ValueError:
        return _result('selection_checkpoint_outside_project_root')
    child_bytes = _json_bytes(child)
    contract = {'schema_version': 'uma.skill_multi_page_confirmation_contract.v1', 'final_confirmation_allowed': True, 'plan_sha256': hashlib.sha256(child_bytes).hexdigest(), 'top_frame_sha256': evidence['dialog']['top_frame_sha256'], 'expected_rows': expected_rows, 'selection_checkpoint_path': str(checkpoint_relative).replace('\\', '/'), 'selection_checkpoint_sha256': evidence['selection_checkpoint_sha256'], 'expected_remaining_skill_points': child.get('expected_remaining_skill_points')}
    if intermediate_frames:
        if any((not Path(frame['path']).is_file() or _sha256(Path(frame['path'])) != frame['sha256'] for frame in intermediate_frames)):
            return _result('issuer_intermediate_frame_mismatch')
        contract['intermediate_frames'] = intermediate_frames
    if selection_verified:
        contract['confirmation_policy'] = 'verified_selection'
    try:
        root.mkdir(parents=True, exist_ok=True)
        _write_exclusive(evidence_path, evidence_bytes)
        _write_exclusive(child_path, child_bytes)
        _write_exclusive(contract_path, _json_bytes(contract))
    except OSError:
        return _result('issuer_artifact_write_failed')
    return {'schema_version': 'uma.final_confirmation_issuer.v1', 'stop_reason': 'final_authorization_issued', 'parent_plan_path': str(paths['parent_plan']), 'child_plan_path': str(child_path), 'final_evidence_path': str(evidence_path), 'closure_contract_path': str(contract_path), 'expected_closure_contract_sha256': _sha256(contract_path), 'selection_ledger_path': str(paths['selection_ledger']), 'selection_ledger_frame_path': str(paths['selection_ledger_frame']), 'confirmation_inputs': {'promotion_plan_path': str(child_path), 'parent_promotion_plan_path': str(paths['parent_plan']), 'promotion_review_path': str(paths['review']), 'promotion_evidence_path': str(paths['promotion_evidence']), 'selection_checkpoint_path': str(paths['selection_checkpoint']), 'selection_ledger_path': str(paths['selection_ledger']), 'selection_ledger_frame_path': str(paths['selection_ledger_frame']), 'final_confirmation_evidence_path': str(evidence_path), 'closure_contract_path': str(contract_path), 'expected_closure_contract_sha256': _sha256(contract_path), 'capture_binding': review['capture_binding'], 'top_frame_path': str(paths['top_frame']), 'current_frame_path': str(paths['current_frame'])}}

def _expected_rows(parent_plan: Mapping[str, Any], review: Mapping[str, Any]) -> tuple[list[dict[str, str]] | None, str | None]:
    purchases = parent_plan.get('purchases')
    reviewed_rows = review.get('reviewed_rows')
    if not isinstance(purchases, list) or not isinstance(reviewed_rows, Mapping):
        return (None, 'issuer_review_rows_invalid')
    rows: list[dict[str, str]] = []
    descriptions: set[str] = set()
    for purchase in purchases:
        item_id = purchase.get('item_id') if isinstance(purchase, Mapping) else None
        matches = [row for row in reviewed_rows.values() if isinstance(row, Mapping) and row.get('item_id') == item_id]
        if not isinstance(item_id, str) or len(matches) != 1:
            return (None, 'issuer_review_rows_invalid')
        row = matches[0]
        name = row.get('observed_name')
        description = row.get('observed_description')
        if not isinstance(name, str) or not name or (not isinstance(description, str)) or (not description) or (description in descriptions):
            return (None, 'issuer_review_rows_invalid')
        descriptions.add(description)
        rows.append({'item_id': item_id, 'name': name, 'description': description, 'kind': 'planned', 'reviewed_row': dict(row)})
    return (rows, None) if rows else (None, 'issuer_review_rows_invalid')

def _validate_detector_state(state: Mapping[str, Any], expected_rows: list[dict[str, str]]) -> str | None:
    expected_ids = [row['item_id'] for row in expected_rows]
    indexes = state.get('page_row_indexes')
    flattened = [index for page in indexes for index in page] if isinstance(indexes, list) and all((isinstance(page, list) for page in indexes)) else []
    evidence = state.get('page_evidence')
    evidence_indexes = [row.get('expected_index') for entry in evidence if isinstance(entry, Mapping) for row in entry.get('rows', [entry])] if isinstance(evidence, list) else []
    control = state.get('final_confirm_control')
    if state.get('accepted') is not True or state.get('page_kind') != 'confirmation_dialog' or state.get('screen_kind') != 'confirmation_dialog' or (state.get('confirmed_item_ids') != expected_ids) or (state.get('planned_item_ids') != expected_ids) or (state.get('auto_included_item_ids') != []) or (state.get('observed_row_count') != len(expected_ids)) or (state.get('no_extra_rows') is not True) or (not isinstance(indexes, list)) or (not indexes) or any((not page for page in indexes)) or (list(dict.fromkeys(flattened)) != list(range(len(expected_ids)))) or (evidence_indexes != flattened) or any((b[0] <= a[0] or b[0] > a[-1] for a, b in zip(indexes, indexes[1:]))) or (state.get('dangerous_resource_flags') != []) or (not isinstance(control, Mapping)) or (type(control.get('x')) is not int) or (type(control.get('y')) is not int) or (not 0 <= control['x'] < 720) or (not 0 <= control['y'] < 1280):
        return 'dialog_closure_not_verified'
    return None

def _read_mapping(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None

def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')

def _write_exclusive(path: Path, data: bytes) -> None:
    with path.open('xb') as handle:
        handle.write(data)
    if path.read_bytes() != data:
        raise OSError('issuer_artifact_write_verification_failed')

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _result(reason: str) -> dict[str, Any]:
    return {'schema_version': 'uma.final_confirmation_issuer.v1', 'stop_reason': reason}
__all__ = ['issue_final_confirmation_artifacts']
