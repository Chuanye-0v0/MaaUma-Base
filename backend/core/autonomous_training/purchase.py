"""Fail-closed preparation for the skill purchase-confirmation step.

This module intentionally owns no ADB connection.  It only accepts the
already-connected autonomous device supplied by the caller and leaves live
selection blocked until a semantic review is SHA-bound to the batch plan.
"""
from __future__ import annotations
from desktop.umaa_public_context import endpoint as _public_endpoint
import json
import hashlib
import tempfile
import time
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
import cv2
import numpy as np
from core.skill_batch_purchase import validate_skill_batch_plan, validate_skill_inventory_packet
from core.skill_batch_purchase_executor import SKILL_BATCH_PURCHASE_SAFETY_TOKEN, SkillBatchPurchaseExecutor
from core.skill_purchase_live_adapter import SkillPurchaseLiveCaptureSession
from core.skill_ocr_performance import harvest_skill_ocr
from core.skill_purchase_plan_promotion import promote_planner_final_skill_plan
from core.skill_scroll import SKILL_OVERLAP_SWIPE, skill_overlap_shell
_ALLOWED_DEVICE_ENDPOINT = _public_endpoint()
_RESOURCE_CHECKS = {'uses_skill_points_only': True, 'uses_carrots': False, 'uses_clocks': False, 'uses_paid_currency': False}

class _SelectionLedgerRecorder:
    """Record only live row observations already validated by the executor."""

    def __init__(self, *, plan: Mapping[str, Any], session_id: str, output_dir: Path, inventory_packet_sha256: str, prior_observations: Mapping[str, Mapping[str, Any]] | None=None) -> None:
        self.plan = plan
        self.session_id = session_id
        self.output_dir = output_dir
        self.inventory_packet_sha256 = inventory_packet_sha256
        self._latest: tuple[dict[str, Any], Path, int] | None = None
        self._observations = {item_id: dict(observation) for item_id, observation in prior_observations.items()} if prior_observations is not None else {}
        self._emitted: dict[str, str] | None = None

    @property
    def receipt_path(self) -> Path:
        return self.output_dir / 'selection_pre_tap_receipts.json'

    def record_capture(self, state: Mapping[str, Any], screenshot_path: Path) -> None:
        if not screenshot_path.is_file():
            self._latest = None
            return
        frame_sha256 = state.get('frame_sha256')
        actual_sha256 = hashlib.sha256(screenshot_path.read_bytes()).hexdigest()
        if frame_sha256 != actual_sha256:
            self._latest = None
            return
        self._latest = (dict(state), screenshot_path, screenshot_path.stat().st_mtime_ns // 1000000)

    def record_pre_tap(self, action_id: str) -> None:
        if self._latest is None or action_id in self._observations:
            return
        state, screenshot_path, captured_at_unix_ms = self._latest
        matches = [row for row in state.get('rows', []) if isinstance(row, Mapping) and row.get('item_id') == action_id]
        if len(matches) != 1:
            return
        cost = matches[0].get('observed_final_cost')
        if type(cost) is not int or cost <= 0:
            return
        self._observations[action_id] = {'item_id': action_id, 'observed_final_cost': cost, 'frame_sha256': state['frame_sha256'], 'captured_at_unix_ms': captured_at_unix_ms, 'screenshot_path': str(screenshot_path.resolve()), 'page_index': _plan_action_page_indexes(self.plan).get(action_id)}
        self._write_receipts()

    def _write_receipts(self) -> None:
        action_ids = self.plan.get('selected_action_ids')
        plan_sha256 = self.plan.get('plan_sha256')
        if not isinstance(action_ids, list) or not isinstance(plan_sha256, str):
            return
        observations = [self._observations[action_id] for action_id in action_ids if action_id in self._observations]
        if not observations:
            return
        _atomic_write_json(self.receipt_path, {'schema_version': 'uma.skill_pre_tap_cost_receipts.v1', 'plan_sha256': plan_sha256, 'inventory_packet_sha256': self.inventory_packet_sha256, 'session_id': self.session_id, 'observations': observations})

    def emit_if_complete(self) -> dict[str, str] | None:
        if self._emitted is not None or self._latest is None:
            return self._emitted
        action_ids = self.plan.get('selected_action_ids')
        initial_points = self.plan.get('initial_skill_points')
        if not isinstance(action_ids, list) or not action_ids or any((not isinstance(action_id, str) or not action_id for action_id in action_ids)) or (len(action_ids) != len(set(action_ids))) or (type(initial_points) is not int) or (set(action_ids) != set(self._observations)):
            return None
        rows = [{'item_id': action_id, 'observed_final_cost': self._observations[action_id]['observed_final_cost']} for action_id in action_ids]
        selected_total = sum((row['observed_final_cost'] for row in rows))
        state, frame_path, captured_at_unix_ms = self._latest
        if state.get('current_skill_points') != initial_points - selected_total:
            return None
        ledger_path = self.output_dir / 'selection_ledger.json'
        state_path = self.output_dir / 'selection_ledger.state.json'
        ledger = {'schema_version': 'uma.skill_verified_selection_ledger.v1', 'session_id': self.session_id, 'frame_sha256': state['frame_sha256'], 'captured_at_unix_ms': captured_at_unix_ms, 'selected_action_ids': action_ids, 'rows': rows, 'selected_total': selected_total, 'remaining_skill_points': initial_points - selected_total}
        state_receipt = {'schema_version': 'uma.skill_verified_selection_state.v1', 'frame_sha256': state['frame_sha256'], 'captured_at_unix_ms': captured_at_unix_ms, 'screenshot_path': str(frame_path), 'state': state, 'row_observations': [self._observations[action_id] for action_id in action_ids]}
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
        state_path.write_text(json.dumps(state_receipt, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
        self._emitted = {'ledger_path': str(ledger_path), 'state_path': str(state_path), 'frame_path': str(frame_path)}
        return self._emitted

def _plan_action_page_indexes(plan: Mapping[str, Any]) -> dict[str, int]:
    indexes: dict[str, int] = {}
    groups = plan.get('page_groups')
    if not isinstance(groups, list):
        return indexes
    for group in groups:
        if not isinstance(group, Mapping) or type(group.get('page_index')) is not int:
            return {}
        items = group.get('items')
        if not isinstance(items, list):
            return {}
        for item in items:
            action_id = item.get('action_id') if isinstance(item, Mapping) else None
            if not isinstance(action_id, str) or action_id in indexes:
                return {}
            indexes[action_id] = group['page_index']
    return indexes

def _plan_action_costs(plan: Mapping[str, Any]) -> dict[str, int]:
    costs: dict[str, int] = {}
    groups = plan.get('page_groups')
    if not isinstance(groups, list):
        return costs
    for group in groups:
        items = group.get('items') if isinstance(group, Mapping) else None
        if not isinstance(items, list):
            return {}
        for item in items:
            action_id = item.get('action_id') if isinstance(item, Mapping) else None
            cost = item.get('display_cost') if isinstance(item, Mapping) else None
            if not isinstance(action_id, str) or type(cost) is not int or cost <= 0 or (action_id in costs):
                return {}
            costs[action_id] = cost
    return costs

def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary_path = Path(handle.name)
        handle.write(payload)
    temporary_path.replace(path)

def _read_resume_receipts(path: str | Path, inventory: Mapping[str, Any], plan: Mapping[str, Any], session_id: str, checkpoint: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    receipts, reason = _read_json_mapping(path, 'resume_receipts')
    if reason:
        return (None, 'resume_receipts_invalid')
    assert receipts is not None
    if set(receipts) != {'schema_version', 'plan_sha256', 'inventory_packet_sha256', 'session_id', 'observations'} or receipts.get('schema_version') != 'uma.skill_pre_tap_cost_receipts.v1' or receipts.get('plan_sha256') != plan.get('plan_sha256') or (receipts.get('inventory_packet_sha256') != inventory.get('packet_sha256')) or (receipts.get('session_id') != session_id) or (not isinstance(receipts.get('observations'), list)) or (not receipts['observations']):
        return (None, 'resume_receipts_invalid')
    selected = checkpoint.get('selected_action_ids')
    pending = checkpoint.get('pending_group_ids')
    if not isinstance(selected, list) or not isinstance(pending, list):
        return (None, 'resume_receipts_invalid')
    allowed_ids = set(selected) | set(pending)
    expected_pages = _plan_action_page_indexes(plan)
    expected_costs = _plan_action_costs(plan)
    observations: dict[str, dict[str, Any]] = {}
    for observation in receipts['observations']:
        if not isinstance(observation, Mapping):
            return (None, 'resume_receipts_invalid')
        if set(observation) != {'item_id', 'observed_final_cost', 'frame_sha256', 'captured_at_unix_ms', 'screenshot_path', 'page_index'}:
            return (None, 'resume_receipts_invalid')
        action_id = observation.get('item_id')
        cost = observation.get('observed_final_cost')
        frame_sha256 = observation.get('frame_sha256')
        captured_at = observation.get('captured_at_unix_ms')
        screenshot_path = observation.get('screenshot_path')
        if not isinstance(action_id, str) or action_id not in allowed_ids or action_id not in expected_costs or (action_id in observations) or (cost != expected_costs[action_id]) or (observation.get('page_index') != expected_pages.get(action_id)) or (type(captured_at) is not int) or (not isinstance(screenshot_path, str)) or (not isinstance(frame_sha256, str)):
            return (None, 'resume_receipts_invalid')
        frame_path = Path(screenshot_path)
        if not frame_path.is_file() or hashlib.sha256(frame_path.read_bytes()).hexdigest() != frame_sha256:
            return (None, 'resume_receipts_invalid')
        observations[action_id] = dict(observation)
    return (observations, None)

def promote_fresh_skill_batch(*, source_report: Mapping[str, Any], source_report_sha256: str, inventory: Mapping[str, Any], batch_plan: Mapping[str, Any], capture_manifest: Mapping[str, Any], external_skill_db: Mapping[str, Any], session_id: str, capture_batch_id: str) -> dict[str, Any]:
    """Create a semantic review only when it is bound to one batch plan."""
    if source_report.get('schema_version') != 'uma.fresh_skill_acquisition_dry_run.v1':
        raise ValueError('source_report_schema_invalid')
    inventory_ok, inventory_reason = validate_skill_inventory_packet(inventory)
    if not inventory_ok:
        raise ValueError(f'inventory_invalid:{inventory_reason}')
    plan_ok, plan_reason = validate_skill_batch_plan(batch_plan, inventory)
    if not plan_ok:
        raise ValueError(f'plan_invalid:{plan_reason}')
    if source_report.get('inventory_packet') != inventory or source_report.get('batch_plan') != batch_plan or (not _capture_bindings_match(source_report.get('capture_binding'), inventory.get('capture_binding'))):
        raise ValueError('source_report_artifact_mismatch')
    build_context = inventory.get('build_context')
    if not isinstance(build_context, Mapping) or not build_context:
        raise ValueError('build_context_missing')
    _validate_capture_manifest(capture_manifest, inventory['capture_binding'])
    promoted = promote_planner_final_skill_plan(report=source_report, build_context=build_context, external_skill_db=external_skill_db, source_report_sha256=source_report_sha256, session_id=session_id, capture_batch_id=capture_batch_id)
    purchases = promoted['plan'].get('purchases')
    if not isinstance(purchases, list):
        raise ValueError('promotion_purchases_invalid')
    selected_ids = batch_plan.get('selected_action_ids')
    purchase_ids = [item.get('item_id') if isinstance(item, Mapping) else None for item in purchases]
    if purchase_ids != selected_ids:
        raise ValueError('promotion_selected_ids_mismatch')
    if promoted['plan'].get('spent_skill_points') != batch_plan.get('spent_skill_points') or promoted['plan'].get('expected_remaining_skill_points') != batch_plan.get('expected_remaining_skill_points'):
        raise ValueError('promotion_economics_mismatch')
    review = dict(promoted['review'])
    reviewed_rows = review.get('reviewed_rows')
    fingerprints = [item.get('source_row_fingerprint') for item in purchases if isinstance(item, Mapping)]
    if not isinstance(reviewed_rows, Mapping) or len(fingerprints) != len(purchases) or set(reviewed_rows) != set(fingerprints) or any((not isinstance(row, Mapping) or row.get('source_report_sha256') != source_report_sha256 for row in reviewed_rows.values())):
        raise ValueError('promotion_row_binding_mismatch')
    review.update(source_report_sha256=source_report_sha256, inventory_packet_sha256=inventory['packet_sha256'], batch_plan_sha256=batch_plan['plan_sha256'], capture_sha256=inventory['capture_binding']['capture_sha256'], capture_binding=deepcopy(source_report['capture_binding']))
    return {**promoted, 'review': review}

def _capture_bindings_match(report_binding: Any, inventory_binding: Any) -> bool:
    shared = ('turn_id', 'capture_sha256', 'captured_at_unix_ms', 'expires_at_unix_ms', 'page_hashes')
    return isinstance(report_binding, Mapping) and report_binding.get('schema_version') == 'uma.skill_capture_binding.v1' and isinstance(inventory_binding, Mapping) and all((report_binding.get(key) == inventory_binding.get(key) for key in shared))

def _validate_capture_manifest(manifest: Mapping[str, Any], capture_binding: Mapping[str, Any]) -> str:
    evidence = manifest.get('page_evidence')
    expected = capture_binding.get('page_hashes')
    swipe_profiles = {SKILL_OVERLAP_SWIPE['name']: SKILL_OVERLAP_SWIPE, 'full-page': {'name': 'full-page', 'start': [360, 900], 'end': [360, 610], 'duration_ms': 450}, 'reveal-bottom': {'name': 'reveal-bottom', 'start': [360, 900], 'end': [360, 760], 'duration_ms': 350}}
    swipe_profile = manifest.get('swipe_profile')
    if not isinstance(swipe_profile, Mapping) or swipe_profile not in swipe_profiles.values():
        raise ValueError('capture_manifest_swipe_profile_invalid')
    if swipe_profile == SKILL_OVERLAP_SWIPE:
        overlaps = manifest.get('overlap_evidence')
        if not isinstance(overlaps, list) or not isinstance(evidence, list) or len(overlaps) != max(0, len(evidence) - 1) or any((not isinstance(item, Mapping) or item.get('verified') is not True for item in overlaps)):
            raise ValueError('capture_manifest_overlap_unverified')
    if manifest.get('schema_version') != 'uma.skill_acquisition_capture.v1' or manifest.get('captured_at_unix_ms') != capture_binding.get('captured_at_unix_ms') or manifest.get('expires_at_unix_ms') != capture_binding.get('expires_at_unix_ms') or (not isinstance(evidence, list)) or (not isinstance(expected, list)):
        raise ValueError('capture_manifest_pages_missing')
    indexed = {Path(item.get('path', '')).name: item for item in evidence if isinstance(item, Mapping) and isinstance(item.get('path'), str)}
    for page in expected:
        if not isinstance(page, Mapping) or not isinstance(page.get('name'), str):
            raise ValueError('capture_binding_pages_invalid')
        item = indexed.get(page['name'])
        path = Path(item['path']) if isinstance(item, Mapping) else None
        if item is None or item.get('raw_sha256') != page.get('sha256') or (not path.is_file()) or (hashlib.sha256(path.read_bytes()).hexdigest() != page.get('sha256')):
            raise ValueError('capture_manifest_sha_mismatch')
    return str(swipe_profile['name'])

class ExplicitDeviceTransportFacade:
    """ADB-shaped transport over one already-connected autonomous device."""

    def __init__(self, device: Any, *, expected_endpoint: str=_ALLOWED_DEVICE_ENDPOINT) -> None:
        if expected_endpoint not in {_ALLOWED_DEVICE_ENDPOINT, _public_endpoint()} or getattr(device, 'endpoint', None) != expected_endpoint:
            raise ValueError('device_endpoint_not_allowed')
        if not callable(getattr(device, 'command', None)):
            raise ValueError('explicit_device_command_required')
        self._device = device
        self._endpoint = expected_endpoint

    def screenshot(self, save_path: str) -> Any | None:
        raw = self._device.command('-s', self._endpoint, 'exec-out', 'screencap', '-p')
        if not isinstance(raw, bytes) or not raw:
            return None
        path = Path(save_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)

    def tap(self, x: int, y: int, *, scaled: bool=True) -> bool:
        if scaled or not _native_point(x, y):
            return False
        self._device.command('-s', self._endpoint, 'shell', 'input', 'tap', str(x), str(y))
        return True

    def swipe(self, x1: int, y1: int, x2: int, y2: int, *, duration_ms: int=300, scaled: bool=True) -> bool:
        if scaled or not _native_point(x1, y1) or (not _native_point(x2, y2)) or (type(duration_ms) is not int) or (duration_ms <= 0):
            return False
        self._device.command('-s', self._endpoint, 'shell', 'input', 'swipe', str(x1), str(y1), str(x2), str(y2), str(duration_ms))
        return True

    def close(self) -> None:
        """The caller owns the shared device connection."""

    def swipe_skill_overlap(self, *, reverse=False, travel_px=490) -> bool:
        self._device.command('-s', self._endpoint, 'shell', skill_overlap_shell(reverse=reverse, travel_px=travel_px))
        return True

def _native_point(x: Any, y: Any) -> bool:
    return type(x) is int and type(y) is int and (0 <= x < 720) and (0 <= y < 1280)

@harvest_skill_ocr()
def select_and_open_purchase_confirmation(*, device: Any, inventory_path: str | Path, plan_path: str | Path, review_path: str | Path | None, output_dir: str | Path, mode: str='dry-run', execute: bool=False, expected_inventory_sha256: str | None=None, expected_plan_sha256: str | None=None, resume_checkpoint_path: str | Path | None=None, resume_receipts_path: str | Path | None=None, now_unix_ms: int | None=None, planner_report_path: str | Path | None=None, capture_manifest_path: str | Path | None=None, external_skill_db_path: str | Path | None=None, session_id: str | None=None, capture_batch_id: str | None=None, safety_token: str | None=None, expected_endpoint: str=_ALLOWED_DEVICE_ENDPOINT) -> dict[str, Any]:
    """Validate immutable purchase inputs without creating a device transport.

    The prepared batch-plan schema does not contain the live review session
    identity, and the existing review schema does not bind the batch-plan
    digest.  That absence is reported rather than guessed.  Consequently this
    function performs zero device actions, including for a live request.
    """
    if expected_endpoint not in {_ALLOWED_DEVICE_ENDPOINT, _public_endpoint()} or getattr(device, 'endpoint', None) != expected_endpoint:
        return _result('device_endpoint_not_allowed', mode=mode, execute=execute)
    inventory, reason = _read_json_mapping(inventory_path, 'inventory')
    if reason:
        return _write_result(output_dir, _result(reason, mode=mode, execute=execute))
    plan, reason = _read_json_mapping(plan_path, 'plan')
    if reason:
        return _write_result(output_dir, _result(reason, mode=mode, execute=execute))
    assert inventory is not None and plan is not None
    inventory_valid, inventory_reason = validate_skill_inventory_packet(inventory)
    if not inventory_valid:
        return _write_result(output_dir, _result(f'inventory_invalid:{inventory_reason}', mode=mode, execute=execute))
    plan_valid, plan_reason = validate_skill_batch_plan(plan, inventory)
    if not plan_valid:
        return _write_result(output_dir, _result(f'plan_invalid:{plan_reason}', mode=mode, execute=execute))
    if inventory.get('resource_checks') != _RESOURCE_CHECKS or plan.get('resource_checks') != _RESOURCE_CHECKS:
        return _write_result(output_dir, _result('resource_gate_rejected', mode=mode, execute=execute))
    if expected_inventory_sha256 is not None and expected_inventory_sha256 != inventory['packet_sha256'] or (expected_plan_sha256 is not None and expected_plan_sha256 != plan['plan_sha256']):
        return _write_result(output_dir, _result('expected_sha_mismatch', mode=mode, execute=execute))
    current_time = int(time.time() * 1000) if mode == 'live' and execute is True else int(time.time() * 1000) if now_unix_ms is None else now_unix_ms
    if type(current_time) is not int:
        return _write_result(output_dir, _result('now_unix_ms_invalid', mode=mode, execute=execute))
    if current_time < inventory['captured_at_unix_ms'] - 30000:
        return _write_result(output_dir, _result('capture_not_yet_valid', mode=mode, execute=execute))
    if current_time > inventory['expires_at_unix_ms']:
        return _write_result(output_dir, _result('capture_expired', mode=mode, execute=execute))
    if mode not in {'dry-run', 'live'} or type(execute) is not bool:
        return _write_result(output_dir, _result('execution_mode_invalid', mode=mode, execute=execute))
    resume_checkpoint: dict[str, Any] | None = None
    if resume_checkpoint_path is not None:
        resume_checkpoint, resume_reason = _read_resume_checkpoint(resume_checkpoint_path, inventory, plan)
        if resume_reason:
            return _write_result(output_dir, _result(resume_reason, mode=mode, execute=execute, inventory_sha256=inventory['packet_sha256'], plan_sha256=plan['plan_sha256']))
    if resume_receipts_path is not None and resume_checkpoint is None:
        return _write_result(output_dir, _result('resume_receipts_requires_checkpoint', mode=mode, execute=execute, inventory_sha256=inventory['packet_sha256'], plan_sha256=plan['plan_sha256']))
    manifest: Mapping[str, Any] | None = None
    bridge_inputs = (planner_report_path, capture_manifest_path, external_skill_db_path, session_id, capture_batch_id)
    if any((value is not None for value in bridge_inputs)):
        if any((value is None for value in bridge_inputs)):
            return _write_result(output_dir, _result('promotion_inputs_incomplete', mode=mode, execute=execute))
        source_report, error = _read_json_mapping(planner_report_path, 'source_report')
        manifest, manifest_error = _read_json_mapping(capture_manifest_path, 'capture_manifest')
        external_db, db_error = _read_json_mapping(external_skill_db_path, 'external_skill_db')
        if error or manifest_error or db_error:
            return _write_result(output_dir, _result(error or manifest_error or db_error or 'promotion_input_invalid', mode=mode, execute=execute))
        assert source_report is not None and manifest is not None and (external_db is not None)
        source_sha = hashlib.sha256(Path(planner_report_path).read_bytes()).hexdigest()
        try:
            promotion = promote_fresh_skill_batch(source_report=source_report, source_report_sha256=source_sha, inventory=inventory, batch_plan=plan, capture_manifest=manifest, external_skill_db=external_db, session_id=str(session_id), capture_batch_id=str(capture_batch_id))
        except ValueError as exc:
            return _write_result(output_dir, _result(f'promotion_blocked:{exc}', mode=mode, execute=execute))
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        for name in ('plan', 'review', 'evidence'):
            (root / f'promotion_{name}.json').write_text(json.dumps(promotion[name], ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
        review = promotion['review']
    else:
        review = None
    if review is None and review_path is None:
        return _write_result(output_dir, _result('semantic_review_required', mode=mode, execute=execute, inventory_sha256=inventory['packet_sha256'], plan_sha256=plan['plan_sha256']))
    if review is None:
        review, review_reason = _read_json_mapping(review_path, 'review')
    else:
        review_reason = None
    if review_reason or review is None:
        return _write_result(output_dir, _result('semantic_review_required', mode=mode, execute=execute, inventory_sha256=inventory['packet_sha256'], plan_sha256=plan['plan_sha256']))
    if review.get('inventory_packet_sha256') != inventory['packet_sha256'] or review.get('batch_plan_sha256') != plan['plan_sha256'] or review.get('capture_sha256') != inventory['capture_binding']['capture_sha256']:
        return _write_result(output_dir, _result('review_batch_binding_missing', mode=mode, execute=execute, inventory_sha256=inventory['packet_sha256'], plan_sha256=plan['plan_sha256'], missing_review_bindings=('inventory_packet_sha256', 'batch_plan_sha256', 'capture_sha256')))
    prior_observations: dict[str, dict[str, Any]] | None = None
    if resume_receipts_path is not None:
        assert resume_checkpoint is not None
        prior_observations, receipts_reason = _read_resume_receipts(resume_receipts_path, inventory, plan, str(review.get('session_id')), resume_checkpoint)
        if receipts_reason:
            return _write_result(output_dir, _result(receipts_reason, mode=mode, execute=execute, inventory_sha256=inventory['packet_sha256'], plan_sha256=plan['plan_sha256']))
    if mode != 'live' or execute is not True:
        return _write_result(output_dir, _result('dry_run_no_device_actions', mode=mode, execute=execute, inventory_sha256=inventory['packet_sha256'], plan_sha256=plan['plan_sha256']))
    if manifest is None:
        return _write_result(output_dir, _result('capture_manifest_required_for_swipe_profile', mode=mode, execute=execute, inventory_sha256=inventory['packet_sha256'], plan_sha256=plan['plan_sha256']))
    try:
        swipe_profile = _validate_capture_manifest(manifest, inventory['capture_binding'])
        if 'reverse_scroll_shifts' in plan:
            from scripts.plan_fresh_skill_acquisition import _harvest_reverse_shifts
            if plan['reverse_scroll_shifts'] != _harvest_reverse_shifts(manifest, inventory.get('build_context')):
                raise ValueError('reverse_scroll_capture_mismatch')
    except ValueError:
        return _write_result(output_dir, _result('capture_manifest_swipe_profile_invalid', mode=mode, execute=execute, inventory_sha256=inventory['packet_sha256'], plan_sha256=plan['plan_sha256']))
    if safety_token != SKILL_BATCH_PURCHASE_SAFETY_TOKEN:
        return _write_result(output_dir, _result('execution_gate_rejected', mode=mode, execute=execute, inventory_sha256=inventory['packet_sha256'], plan_sha256=plan['plan_sha256']))
    try:
        from scripts.ug_parent_skill_purchase_batch import _LiveBatchAdapter

        class _LedgerRecordingLiveBatchAdapter(_LiveBatchAdapter):

            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self._ledger_recorder = _SelectionLedgerRecorder(plan=plan, session_id=str(review['session_id']), output_dir=Path(output_dir), inventory_packet_sha256=inventory['packet_sha256'], prior_observations=prior_observations)
                self.selection_ledger_paths: dict[str, str] | None = None

            def capture_page(self, active_item_ids: tuple[str, ...]) -> dict[str, Any]:
                state = super().capture_page(active_item_ids)
                screenshot_path = self.screenshot_dir / f'batch_{self.capture_index:03d}.png'
                self._ledger_recorder.record_capture(state, screenshot_path)
                return state

            def recapture_page(self, active_item_ids: tuple[str, ...]) -> dict[str, Any]:
                state = super().recapture_page(active_item_ids)
                screenshot_path = self.screenshot_dir / f'batch_{self.capture_index:03d}.png'
                self._ledger_recorder.record_capture(state, screenshot_path)
                return state

            def tap_plus(self, control: Mapping[str, int], action_id: str) -> None:
                self._ledger_recorder.record_pre_tap(action_id)
                super().tap_plus(control, action_id)

            def open_confirmation(self, control: Mapping[str, int]) -> None:
                self.selection_ledger_paths = self._ledger_recorder.emit_if_complete()
                super().open_confirmation(control)
        facade = ExplicitDeviceTransportFacade(device, expected_endpoint=expected_endpoint)
        session = SkillPurchaseLiveCaptureSession(review=review, root=Path.cwd(), plan_id=review['plan_id'], sample_id=review['sample_id'], session_id=review['session_id'], capture_batch_id=review['capture_batch_id'], selection_plan=plan)
        adapter = _LedgerRecordingLiveBatchAdapter(adb=facade, session=session, review=review, screenshot_dir=Path(output_dir) / 'live_capture', checkpoint=Path(output_dir) / 'selection_checkpoint.json', swipe_profile=swipe_profile)
        execution = SkillBatchPurchaseExecutor(inventory=inventory, plan=plan, adapter=adapter, mode=mode, execute=execute, safety_token=safety_token, expected_inventory_sha256=expected_inventory_sha256, expected_plan_sha256=expected_plan_sha256, resume_checkpoint=resume_checkpoint, now_unix_ms=None if mode == 'live' and execute is True else now_unix_ms).run_selection()
    except (KeyError, ValueError):
        return _write_result(output_dir, _result('live_adapter_binding_invalid', mode=mode, execute=execute, inventory_sha256=inventory['packet_sha256'], plan_sha256=plan['plan_sha256']))
    ledger_paths = adapter.selection_ledger_paths
    receipt_path = adapter._ledger_recorder.receipt_path
    return _write_result(output_dir, {**_result(execution.stop_reason, mode=mode, execute=execute, inventory_sha256=inventory['packet_sha256'], plan_sha256=plan['plan_sha256']), 'confirmation_opened': execution.confirmation_opened, 'device_action_count': execution.tap_count + execution.swipe_count, 'selection_ledger_path': None if ledger_paths is None else ledger_paths['ledger_path'], 'selection_ledger_state_path': None if ledger_paths is None else ledger_paths['state_path'], 'selection_ledger_frame_path': None if ledger_paths is None else ledger_paths['frame_path'], 'pre_tap_receipts_path': str(receipt_path) if receipt_path.is_file() else None})

def _read_json_mapping(path: str | Path, label: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return (None, f'{label}_unreadable')
    if not isinstance(value, Mapping):
        return (None, f'{label}_invalid')
    return (dict(value), None)

def _read_resume_checkpoint(path: str | Path, inventory: Mapping[str, Any], plan: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    checkpoint, reason = _read_json_mapping(path, 'resume_checkpoint')
    if reason:
        return (None, reason)
    assert checkpoint is not None
    validator = SkillBatchPurchaseExecutor(inventory=inventory, plan=plan, adapter=object())
    if not validator.validate_resume_checkpoint(checkpoint):
        return (None, 'resume_checkpoint_invalid')
    return (checkpoint, None)

def _result(stop_reason: str, *, mode: str, execute: bool, inventory_sha256: str | None=None, plan_sha256: str | None=None, missing_review_bindings: tuple[str, ...]=()) -> dict[str, Any]:
    return {'schema_version': 'uma.autonomous_skill_purchase_preflight.v1', 'mode': mode, 'execute': execute, 'stop_reason': stop_reason, 'inventory_sha256': inventory_sha256, 'plan_sha256': plan_sha256, 'missing_review_bindings': list(missing_review_bindings), 'confirmation_opened': False, 'device_action_count': 0}

def _write_result(output_dir: str | Path, result: dict[str, Any]) -> dict[str, Any]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / 'purchase_preflight.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    return result
__all__ = ['select_and_open_purchase_confirmation']
