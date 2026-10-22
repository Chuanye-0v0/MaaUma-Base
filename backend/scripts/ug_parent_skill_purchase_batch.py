"""One-scan skill planning and SHA-bound batch selection.

The default phase is offline planning.  The live selection phase is a
separate, explicit operation and intentionally stops after opening the single
purchase confirmation dialog.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from config.settings import ADB_ENDPOINT, ADB_PORT
from core.image_io import read_image
from core.skill_batch_purchase import build_skill_batch_plan, select_local_skill_action_ids, validate_skill_batch_plan, validate_skill_inventory_packet
from core.skill_batch_purchase_executor import MAX_TOP_REWIND_SWIPES, SKILL_BATCH_PURCHASE_SAFETY_TOKEN, SkillBatchPurchaseExecutor
from core.skill_planner.preferences import load_skill_preference_profile
from core.skill_purchase_live_adapter import SkillPurchaseLiveCaptureSession
from core.skill_scroll import SKILL_OVERLAP_SWIPE
from scripts.plan_fresh_skill_acquisition import _read_context, run_fresh_dry_run
_ALLOWED_ADB_ENDPOINT = ADB_ENDPOINT

def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(value), ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8') + b'\n'
    path.write_bytes(payload)

def _read_json(path: Path | None, *, label: str) -> dict[str, Any]:
    if path is None:
        raise ValueError(f'{label}_path_required')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError(f'{label}_must_be_object')
    return value

def parse_args(argv: list[str] | None=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Build or execute one SHA-bound final-skill batch.')
    parser.add_argument('--phase', choices=('plan', 'select'), default='plan')
    parser.add_argument('--pages', nargs='+', type=Path)
    parser.add_argument('--resolver-catalog', type=Path, default=ROOT / 'data/skills/cn_skill_resolver_catalog.json')
    parser.add_argument('--planner-catalog', type=Path, default=ROOT / 'data/skills/cn_skill_planner_catalog.json')
    parser.add_argument('--turn-id')
    parser.add_argument('--build-context', type=Path)
    parser.add_argument('--preference-profile', type=Path)
    parser.add_argument('--capture-manifest', type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--inventory', type=Path)
    parser.add_argument('--batch-plan', type=Path)
    parser.add_argument('--review', type=Path)
    parser.add_argument('--checkpoint', type=Path)
    parser.add_argument('--resume-checkpoint', type=Path)
    parser.add_argument('--screenshot-dir', type=Path)
    parser.add_argument('--mode', choices=('dry-run', 'live'), default='dry-run')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--adb-endpoint', default=_ALLOWED_ADB_ENDPOINT)
    parser.add_argument('--safety-token')
    parser.add_argument('--expected-inventory-sha')
    parser.add_argument('--expected-plan-sha')
    parser.add_argument('--prior-purchase-result', type=Path)
    parser.add_argument('--swipe-profile', choices=('full-page', 'reveal-bottom', SKILL_OVERLAP_SWIPE['name']), default='full-page', help='Match live list movement to the capture profile used for this inventory.')
    return parser.parse_args(argv)

def run(args: argparse.Namespace, *, planning_runner: Callable[..., dict[str, Any]]=run_fresh_dry_run, adapter_factory: Callable[..., Any] | None=None) -> tuple[dict[str, Any], int]:
    base = {'schema_version': 'uma.skill_batch_cli_summary.v1', 'phase': args.phase, 'mode': args.mode, 'performed': False, 'tap_count': 0, 'swipe_count': 0, 'confirmation_opened': False}
    try:
        if args.phase == 'plan':
            return _run_plan(args, base, planning_runner=planning_runner)
        return _run_select(args, base, adapter_factory=adapter_factory)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return ({**base, 'is_valid': False, 'stop_reason': str(error)}, 2)

def _load_skill_relations(catalog_path: Path | None) -> dict[str, list[str]]:
    """Build upper -> lower skill id map from the planner catalog relations."""
    if catalog_path is None or not catalog_path.is_file():
        return {}
    payload = json.loads(catalog_path.read_text(encoding='utf-8'))
    skills = payload.get('skills') if isinstance(payload, Mapping) else None
    if not isinstance(skills, list):
        return {}
    relations: dict[str, list[str]] = {}
    for item in skills:
        if not isinstance(item, Mapping):
            continue
        raw = item.get('relations')
        if not isinstance(raw, Mapping):
            continue
        lower = raw.get('lower')
        if not isinstance(lower, list) or not lower:
            continue
        skill_id = item.get('skill_id')
        if isinstance(skill_id, str) and skill_id:
            relations[skill_id] = [str(value) for value in lower if isinstance(value, str)]
    return relations

def _run_plan(args: argparse.Namespace, base: dict[str, Any], *, planning_runner: Callable[..., dict[str, Any]]) -> tuple[dict[str, Any], int]:
    if args.mode != 'dry-run' or args.execute:
        return ({**base, 'is_valid': False, 'stop_reason': 'plan_phase_is_offline_only'}, 2)
    if not args.pages or args.resolver_catalog is None or args.turn_id is None or (args.output_dir is None):
        return ({**base, 'is_valid': False, 'stop_reason': 'plan_inputs_missing'}, 2)
    prior_context = _load_prior_purchase_context(args)
    profile = load_skill_preference_profile(args.preference_profile) if args.preference_profile is not None else None
    fresh_report = planning_runner(pages=list(args.pages), resolver_catalog=args.resolver_catalog, planner_catalog=args.planner_catalog, output_dir=args.output_dir, turn_id=args.turn_id, build_context=_read_context(args.build_context), preference_profile=profile, prior_owned_skill_ids=list(prior_context['acquired_action_ids']) if prior_context is not None else None, capture_manifest=_read_json(args.capture_manifest, label='capture_manifest') if getattr(args, 'capture_manifest', None) is not None else None)
    inventory = fresh_report.get('inventory_packet')
    batch_plan = fresh_report.get('batch_plan')
    if not isinstance(inventory, Mapping) or not isinstance(batch_plan, Mapping):
        return ({**base, 'is_valid': False, 'stop_reason': 'batch_artifacts_missing'}, 2)
    if prior_context is not None:
        prior_error = _post_purchase_rescan_error(inventory, prior_context=prior_context)
        if prior_error is not None:
            return ({**base, 'is_valid': False, 'stop_reason': prior_error}, 2)
    inventory_valid, inventory_reason = validate_skill_inventory_packet(inventory)
    plan_valid, plan_reason = validate_skill_batch_plan(batch_plan, inventory)
    if not inventory_valid or not plan_valid:
        return ({**base, 'is_valid': False, 'stop_reason': f'inventory_invalid:{inventory_reason}' if not inventory_valid else f'plan_invalid:{plan_reason}'}, 2)
    batch_plan = build_skill_batch_plan(inventory=inventory, selected_action_ids=select_local_skill_action_ids(inventory), decision_source='local_auto', skill_relations=_load_skill_relations(args.planner_catalog))
    _write_json(args.output_dir / 'skill_inventory.packet.json', inventory)
    _write_json(args.output_dir / 'skill_batch.plan.json', batch_plan)
    summary = {**base, 'is_valid': True, 'stop_reason': 'plan_ready', 'inventory': dict(inventory), 'batch_plan': dict(batch_plan), 'inventory_sha256': inventory['packet_sha256'], 'plan_sha256': batch_plan['plan_sha256'], 'selected_action_ids': list(batch_plan['selected_action_ids']), 'initial_skill_points': batch_plan['initial_skill_points'], 'spent_skill_points': batch_plan['spent_skill_points'], 'expected_remaining_skill_points': batch_plan['expected_remaining_skill_points'], 'resource_checks': dict(inventory['resource_checks'])}
    _write_json(args.output_dir / 'skill_batch.summary.json', summary)
    return (summary, 0)

def _load_prior_purchase_context(args: argparse.Namespace) -> dict[str, Any] | None:
    result_path = getattr(args, 'prior_purchase_result', None)
    if result_path is None:
        return None
    prior_inventory = _read_json(args.inventory, label='prior_inventory')
    prior_plan = _read_json(args.batch_plan, label='prior_batch_plan')
    inventory_valid, inventory_reason = validate_skill_inventory_packet(prior_inventory)
    plan_valid, plan_reason = validate_skill_batch_plan(prior_plan, prior_inventory)
    if not inventory_valid:
        raise ValueError(f'prior_inventory_invalid:{inventory_reason}')
    if not plan_valid:
        raise ValueError(f'prior_plan_invalid:{plan_reason}')
    result = _read_json(result_path, label='prior_purchase_result')
    result_error = _prior_purchase_result_error(result, inventory=prior_inventory, batch_plan=prior_plan)
    if result_error is not None:
        raise ValueError(result_error)
    if result['purchase_succeeded'] is not True:
        raise ValueError('prior_purchase_result_not_successful')
    return {'actual_remaining_skill_points': result['actual_remaining_skill_points'], 'acquired_action_ids': list(result['acquired_action_ids']), 'prior_capture_sha256': prior_inventory['capture_binding']['capture_sha256']}

def _post_purchase_rescan_error(inventory: Mapping[str, Any], *, prior_context: Mapping[str, Any]) -> str | None:
    if inventory.get('current_skill_points') != prior_context['actual_remaining_skill_points']:
        return 'post_purchase_rescan_points_mismatch'
    capture_binding = inventory.get('capture_binding')
    if not isinstance(capture_binding, Mapping) or capture_binding.get('capture_sha256') == prior_context['prior_capture_sha256']:
        return 'post_purchase_rescan_fresh_capture_required'
    owned = inventory.get('owned_skill_ids')
    if not isinstance(owned, list) or not set(prior_context['acquired_action_ids']).issubset(owned):
        return 'post_purchase_rescan_owned_carry_forward_missing'
    return None

def _run_select(args: argparse.Namespace, base: dict[str, Any], *, adapter_factory: Callable[..., Any] | None) -> tuple[dict[str, Any], int]:
    gate_error = _select_gate_error(args)
    if gate_error:
        return ({**base, 'is_valid': False, 'stop_reason': gate_error}, 2)
    inventory = _read_json(args.inventory, label='inventory')
    batch_plan = _read_json(args.batch_plan, label='batch_plan')
    inventory_valid, inventory_reason = validate_skill_inventory_packet(inventory)
    plan_valid, plan_reason = validate_skill_batch_plan(batch_plan, inventory)
    if not inventory_valid or not plan_valid:
        reason = f'inventory_invalid:{inventory_reason}' if not inventory_valid else f'plan_invalid:{plan_reason}'
        return ({**base, 'is_valid': False, 'stop_reason': reason}, 2)
    if args.expected_inventory_sha != inventory['packet_sha256'] or args.expected_plan_sha != batch_plan['plan_sha256']:
        return ({**base, 'is_valid': False, 'stop_reason': 'expected_sha_mismatch'}, 2)
    prior_purchase_result_path = getattr(args, 'prior_purchase_result', None)
    if prior_purchase_result_path is not None:
        prior_result = _read_json(prior_purchase_result_path, label='prior_purchase_result')
        prior_error = _prior_purchase_result_error(prior_result, inventory=inventory, batch_plan=batch_plan)
        if prior_error is not None:
            return ({**base, 'is_valid': False, 'stop_reason': prior_error}, 2)
        if prior_result['purchase_succeeded']:
            return ({**base, 'is_valid': False, 'stop_reason': 'post_purchase_rescan_required'}, 2)
    create_adapter = adapter_factory or _build_live_adapter
    adapter = create_adapter(args, inventory, batch_plan)
    resume_checkpoint_path = getattr(args, 'resume_checkpoint', None)
    resume_checkpoint = _read_json(resume_checkpoint_path, label='resume_checkpoint') if resume_checkpoint_path is not None else None
    try:
        result = SkillBatchPurchaseExecutor(inventory=inventory, plan=batch_plan, adapter=adapter, mode=args.mode, execute=args.execute, safety_token=args.safety_token, expected_inventory_sha256=args.expected_inventory_sha, expected_plan_sha256=args.expected_plan_sha, resume_checkpoint=resume_checkpoint).run_selection()
    finally:
        close = getattr(adapter, 'close', None)
        if callable(close):
            close()
    report = {**base, 'is_valid': result.stop_reason == 'confirmation_opened', 'performed': result.tap_count > 0 or result.swipe_count > 0, 'stop_reason': result.stop_reason, 'selected_action_ids': list(result.selected_action_ids), 'remaining_action_ids': list(result.remaining_action_ids), 'confirmation_opened': result.confirmation_opened, 'tap_count': result.tap_count, 'swipe_count': result.swipe_count, 'alignment_swipe_count': int(getattr(adapter, 'alignment_swipe_count', 0)), 'inventory_sha256': inventory['packet_sha256'], 'plan_sha256': batch_plan['plan_sha256']}
    if args.checkpoint is not None:
        _write_json(args.checkpoint.with_suffix('.result.json'), report)
    return (report, 0 if report['is_valid'] else 2)

def _prior_purchase_result_error(result: Mapping[str, Any], *, inventory: Mapping[str, Any], batch_plan: Mapping[str, Any]) -> str | None:
    required_fields = {'schema_version', 'purchase_succeeded', 'inventory_packet_sha256', 'plan_sha256', 'actual_remaining_skill_points', 'acquired_action_ids'}
    if set(result) != required_fields:
        return 'prior_purchase_result_fields_invalid'
    if result.get('schema_version') != 'uma.skill_batch_purchase_result.v1':
        return 'prior_purchase_result_schema_invalid'
    if not isinstance(result.get('purchase_succeeded'), bool):
        return 'prior_purchase_result_success_invalid'
    if result.get('inventory_packet_sha256') != inventory['packet_sha256'] or result.get('plan_sha256') != batch_plan['plan_sha256']:
        return 'prior_purchase_result_sha_mismatch'
    actual_remaining = result.get('actual_remaining_skill_points')
    if not isinstance(actual_remaining, int) or isinstance(actual_remaining, bool) or actual_remaining < 0 or (actual_remaining != batch_plan['expected_remaining_skill_points']):
        return 'prior_purchase_result_remaining_points_invalid'
    acquired = result.get('acquired_action_ids')
    if not isinstance(acquired, list) or any((not isinstance(item, str) or not item for item in acquired)) or len(acquired) != len(set(acquired)) or (acquired != batch_plan['selected_action_ids']):
        return 'prior_purchase_result_actions_invalid'
    return None

def _select_gate_error(args: argparse.Namespace) -> str | None:
    if args.adb_endpoint != _ALLOWED_ADB_ENDPOINT:
        return 'adb_endpoint_not_allowed'
    if args.mode != 'live' or args.execute is not True or args.safety_token != SKILL_BATCH_PURCHASE_SAFETY_TOKEN:
        return 'execution_gate_rejected'
    if not isinstance(args.expected_inventory_sha, str) or not isinstance(args.expected_plan_sha, str):
        return 'expected_sha_required'
    return None

class _LiveBatchAdapter:

    def __init__(self, *, adb: Any, session: SkillPurchaseLiveCaptureSession, review: Mapping[str, Any], screenshot_dir: Path, checkpoint: Path, swipe_profile: str='full-page') -> None:
        if swipe_profile not in {'full-page', 'reveal-bottom', SKILL_OVERLAP_SWIPE['name']}:
            raise ValueError('skill_batch_swipe_profile_invalid')
        self.adb = adb
        self.session = session
        self.review = review
        self.screenshot_dir = screenshot_dir
        self.checkpoint = checkpoint
        self.swipe_profile = swipe_profile
        self.capture_index = 0
        self.alignment_swipe_count = 0
        self._geometry_alignment_item_ids: set[str] = set()
        self._reverse_shifts: list[float] | None = None
        self._reverse_page_index = 0
        self._reverse_first_step = False
        self._last_skill_points = None
        self._rewound_to_top = False

    def capture_page(self, active_item_ids: tuple[str, ...]) -> dict[str, Any]:
        try:
            state = self._capture_page_once(active_item_ids)
        except ValueError as error:
            if self.swipe_profile != 'reveal-bottom' or str(error) != 'multi review target missing' or self.alignment_swipe_count != 0:
                raise
            return self._nudge_and_capture(active_item_ids)
        selected_ids = set(state.get('selected_item_ids') or [])
        unbound_item_ids = set(active_item_ids) - selected_ids
        partial_candidates = state.get('target_candidates')
        partial_candidate_ids = [candidate.get('item_id') for candidate in partial_candidates] if isinstance(partial_candidates, list) and all((isinstance(candidate, Mapping) for candidate in partial_candidates)) else []
        visible_row_ids = {row.get('item_id') for row in state.get('rows', []) if isinstance(row, Mapping)}
        if self.swipe_profile in ('full-page', SKILL_OVERLAP_SWIPE['name']) and self.alignment_swipe_count == 0 and unbound_item_ids and (len(partial_candidate_ids) == len(unbound_item_ids)) and (set(partial_candidate_ids) == unbound_item_ids) and (len(set(partial_candidate_ids)) == len(partial_candidate_ids)) and all((candidate.get('reason') == 'row_partially_visible_bottom' for candidate in partial_candidates)) and (not visible_row_ids.intersection(unbound_item_ids)):
            return self._nudge_and_capture(active_item_ids, require_full_rows=True)
        geometry_state = self._maybe_align_source_geometry(state, active_item_ids)
        if geometry_state is not state:
            return geometry_state
        if self.swipe_profile == 'reveal-bottom' and self.alignment_swipe_count == 0 and active_item_ids and (not set(active_item_ids).intersection({row.get('item_id') for row in state.get('rows', []) if isinstance(row, Mapping)} | set(state.get('selected_item_ids') or []))):
            return self._nudge_and_capture(active_item_ids, retry_partial_evidence=_reveal_bottom_partial_evidence(state, active_item_ids))
        return state

    def _align_geometry_and_capture(self, active_item_ids: tuple[str, ...], evidence: tuple[str, int, int]) -> dict[str, Any]:
        item_id, points, delta_y = evidence
        start_y, end_y = (900, 860) if delta_y > 0 else (860, 900)
        self.adb.swipe(360, start_y, 360, end_y, duration_ms=250, scaled=False)
        self.alignment_swipe_count += 1
        time.sleep(0.8)
        state = self._capture_page_once(active_item_ids)
        rows = state.get('rows')
        selected = state.get('selected_item_ids')
        if state.get('fresh') is not True or state.get('current_skill_points') != points or (not isinstance(rows, list)) or ([row.get('item_id') for row in rows if isinstance(row, Mapping)] != [item_id]) or (not isinstance(selected, list)) or (item_id in selected):
            raise ValueError('source geometry alignment not verified')
        return state

    def _nudge_and_capture(self, active_item_ids: tuple[str, ...], *, require_full_rows: bool=False, retry_partial_evidence: tuple[str, int, int, int | None, int | None] | None=None) -> dict[str, Any]:
        self.adb.swipe(360, 900, 360, 860, duration_ms=250, scaled=False)
        self.alignment_swipe_count += 1
        time.sleep(0.8)
        state = self._capture_page_once(active_item_ids)
        if retry_partial_evidence is not None and self.alignment_swipe_count == 1 and (_reveal_bottom_partial_evidence(state, active_item_ids) == retry_partial_evidence):
            self.adb.swipe(360, 900, 360, 860, duration_ms=250, scaled=False)
            self.alignment_swipe_count += 1
            time.sleep(0.8)
            state = self._capture_page_once(active_item_ids)
        if require_full_rows:
            selected_ids = set(state.get('selected_item_ids') or [])
            unbound_item_ids = set(active_item_ids) - selected_ids
            visible_row_ids = {row.get('item_id') for row in state.get('rows', []) if isinstance(row, Mapping)}
            if state.get('fresh') is not True or not unbound_item_ids.issubset(visible_row_ids):
                raise ValueError('partial target not fully revealed')
        return self._maybe_align_source_geometry(state, active_item_ids)

    def _maybe_align_source_geometry(self, state: dict[str, Any], active_item_ids: tuple[str, ...]) -> dict[str, Any]:
        if self.swipe_profile != 'reveal-bottom':
            return state
        geometry_evidence = _source_geometry_alignment_evidence(state, active_item_ids)
        if geometry_evidence is None or geometry_evidence[0] in self._geometry_alignment_item_ids:
            return state
        self._geometry_alignment_item_ids.add(geometry_evidence[0])
        return self._align_geometry_and_capture(active_item_ids, geometry_evidence)

    def recapture_page(self, active_item_ids: tuple[str, ...]) -> dict[str, Any]:
        # Settling reads must not invoke geometry alignment or navigation.
        return self._capture_page_once(active_item_ids)

    def _capture_page_once(self, active_item_ids: tuple[str, ...]) -> dict[str, Any]:
        self.capture_index += 1
        path = self.screenshot_dir / f'batch_{self.capture_index:03d}.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        image = self.adb.screenshot(str(path))
        if image is None:
            raise RuntimeError('adb_screenshot_failed')
        if not active_item_ids:
            state = self.session.capture(path, active_item_ids=set(), navigation_only=True)
        else:
            state = self.session.capture(path, active_item_ids=set(active_item_ids))
        selected_ids = set(state.get('selected_item_ids') or [])
        normalized_rows: list[dict[str, Any]] = []
        for raw in state.get('rows', []):
            if not isinstance(raw, Mapping):
                continue
            evidence = raw.get('source_evidence')
            first = evidence[0] if isinstance(evidence, list) and evidence else {}
            row = dict(raw)
            row['skill_id'] = row.get('item_id')
            row['observed_discount_percent'] = row.get('discount_percent')
            row['selected'] = row.get('item_id') in selected_ids
            row['row_index'] = first.get('row_index')
            row['row_bbox'] = first.get('row_bbox')
            normalized_rows.append(row)
        present = {row.get('item_id') for row in normalized_rows}
        reviewed = self.review.get('reviewed_rows', {})
        for raw_review in reviewed.values() if isinstance(reviewed, Mapping) else ():
            if not isinstance(raw_review, Mapping):
                continue
            item_id = raw_review.get('item_id')
            if item_id not in selected_ids or item_id in present:
                continue
            normalized_rows.append({'item_id': item_id, 'skill_id': item_id, 'selected': True, 'plus_available': False, 'plus_control': None})
        state['rows'] = normalized_rows
        self._last_skill_points = state.get('current_skill_points')
        return state

    def tap_plus(self, control: Mapping[str, int], action_id: str) -> None:
        del action_id
        if self.adb.tap(control['x'], control['y'], scaled=False) is not True:
            raise RuntimeError('adb_tap_failed')
        time.sleep(0.25)

    def swipe_forward(self) -> None:
        if self.swipe_profile == SKILL_OVERLAP_SWIPE['name']:
            if self.adb.swipe_skill_overlap() is not True:
                raise RuntimeError('adb_skill_overlap_swipe_failed')
            time.sleep(0.15)
            return
        swipe_end_y, duration_ms = (760, 350) if self.swipe_profile == 'reveal-bottom' else (610, 450)
        self.adb.swipe(360, 900, 360, swipe_end_y, duration_ms=duration_ms, scaled=False)
        time.sleep(1.8)

    def begin_reverse_selection(self, shifts: list[float], max_swipes: int) -> bool:
        if self.swipe_profile != SKILL_OVERLAP_SWIPE['name'] or type(max_swipes) is not int or (not 1 <= max_swipes <= MAX_TOP_REWIND_SWIPES) or (not isinstance(shifts, list)) or any((type(shift) not in (int, float) or not 1 <= shift <= 386 for shift in shifts)):
            return False
        self._reverse_shifts = list(shifts)
        self._reverse_page_index = len(shifts)
        self._reverse_first_step = True
        state = self.capture_page(())
        points = state.get('current_skill_points')
        for attempt in range(max_swipes + 1):
            if self._top_probe_sha(state) is None or type(points) is not int or state.get('current_skill_points') != points or (type(state.get('list_at_bottom')) is not bool):
                return False
            if state['list_at_bottom']:
                return True
            if attempt == max_swipes or self.adb.swipe_skill_overlap() is not True:
                return False
            time.sleep(0.15)
            state = self.capture_page(())
        return False

    def swipe_backward(self) -> None:
        if self._reverse_shifts is None or self._reverse_page_index <= 0:
            raise ValueError('reverse_route_not_bound')
        expected_shift = self._reverse_shifts[self._reverse_page_index - 1]
        travel = 490
        if self._reverse_first_step and len(self._reverse_shifts) > 1:
            nominal_shift = max(self._reverse_shifts[:-1])
            travel = min(490, max(12, round(490 * expected_shift / nominal_shift)))
        before_path = self.screenshot_dir / f'batch_{self.capture_index:03d}.png'
        before_points = self._last_skill_points
        if self.adb.swipe_skill_overlap(reverse=True, travel_px=travel) is not True:
            raise RuntimeError('adb_skill_reverse_swipe_failed')
        time.sleep(0.15)
        if self._reverse_first_step:
            import cv2
            from core.skill_scroll import measure_skill_overlap
            before = read_image(str(before_path))
            state = self.capture_page(())
            after = read_image(str(self.screenshot_dir / f'batch_{self.capture_index:03d}.png'))
            overlap = measure_skill_overlap(after, before)
            for _ in range(2):
                if self._top_probe_sha(state) is None or state.get('current_skill_points') != before_points or overlap.get('verified') is not True:
                    break
                delta = overlap['shift'] - expected_shift
                if not 2 < abs(round(overlap['shift']) - round(expected_shift)) <= 25:
                    break
                correction = min(40, max(18, round(abs(delta)) + 16))
                if self.adb.swipe_skill_overlap(reverse=delta < 0, travel_px=correction) is not True:
                    break
                time.sleep(0.15)
                state = self.capture_page(())
                after = read_image(str(self.screenshot_dir / f'batch_{self.capture_index:03d}.png'))
                overlap = measure_skill_overlap(after, before)
            if self._top_probe_sha(state) is None or type(before_points) is not int or state.get('current_skill_points') != before_points or (overlap.get('verified') is not True) or (abs(round(overlap['shift']) - round(expected_shift)) > 2):
                raise ValueError('reverse_bottom_step_unverified')
            self._reverse_first_step = False
        self._reverse_page_index -= 1

    def reveal_bottom_after_rewind(self) -> bool:
        if self.swipe_profile != 'full-page' or not self._rewound_to_top or self.alignment_swipe_count != 0:
            return False
        self.adb.swipe(360, 900, 360, 760, duration_ms=350, scaled=False)
        self.alignment_swipe_count += 1
        time.sleep(1.8)
        return True

    def rewind_to_top(self, max_swipes: int) -> bool:
        """Return to the physical list top only when a bounded probe verifies it."""
        self._rewound_to_top = False
        if type(max_swipes) is not int or not 1 <= max_swipes <= MAX_TOP_REWIND_SWIPES:
            return False
        try:
            state = self.capture_page(())
        except Exception:
            return False
        previous_sha = self._top_probe_sha(state)
        if previous_sha is None:
            return False
        if state.get('list_at_top') is True:
            self._rewound_to_top = True
            return True
        for _ in range(max_swipes):
            try:
                self.adb.swipe(360, 610, 360, 900, duration_ms=450, scaled=False)
                time.sleep(1.0)
                state = self.capture_page(())
            except Exception:
                return False
            current_sha = self._top_probe_sha(state)
            if current_sha is None:
                return False
            if state.get('list_at_top') is True or current_sha == previous_sha:
                self._rewound_to_top = True
                return True
            previous_sha = current_sha
        return False

    @staticmethod
    def _top_probe_sha(state: Mapping[str, Any]) -> str | None:
        frame_sha = state.get('frame_sha256')
        if state.get('fresh') is not True or state.get('page_kind') != 'skill_acquisition' or state.get('screen_kind') != 'skill_acquisition' or (state.get('currency_scope') != 'skill_points_only') or (state.get('dangerous_resource_flags') != []) or (not isinstance(frame_sha, str)) or (len(frame_sha) != 64) or any((character not in '0123456789abcdef' for character in frame_sha)):
            return None
        parser_manifest = state.get('parser_manifest')
        content_sha = parser_manifest.get('content_signature_sha256') if isinstance(parser_manifest, Mapping) else None
        if isinstance(content_sha, str) and len(content_sha) == 64 and all((character in '0123456789abcdef' for character in content_sha)):
            return content_sha
        return frame_sha

    def open_confirmation(self, control: Mapping[str, int]) -> None:
        if self.adb.tap(control['x'], control['y'], scaled=False) is not True:
            raise RuntimeError('adb_tap_failed')
        time.sleep(0.8)

    def persist_checkpoint(self, checkpoint: Mapping[str, Any]) -> None:
        _write_json(self.checkpoint, checkpoint)

    def close(self) -> None:
        self.adb.disconnect()

def _reveal_bottom_partial_evidence(state: Mapping[str, Any], active_item_ids: tuple[str, ...]) -> tuple[str, int, int, int | None, int | None] | None:
    if state.get('fresh') is not True or len(active_item_ids) != 1:
        return None
    item_id = active_item_ids[0]
    if not isinstance(item_id, str) or not item_id:
        return None
    selected = state.get('selected_item_ids')
    rows = state.get('rows')
    candidates = state.get('target_candidates')
    points = state.get('current_skill_points')
    if not isinstance(selected, list) or item_id in selected or (not isinstance(rows, list)) or any((isinstance(row, Mapping) and row.get('item_id') == item_id for row in rows)) or (not isinstance(candidates, list)) or (len(candidates) != 1) or (type(points) is not int):
        return None
    candidate = candidates[0]
    if not isinstance(candidate, Mapping):
        return None
    cost = candidate.get('observed_final_cost')
    hint = candidate.get('hint_level')
    discount = candidate.get('discount_percent')
    discount_evidence_valid = type(hint) is int and type(discount) is int or ('hint_level' in candidate and 'discount_percent' in candidate and (hint is None) and (discount is None))
    if candidate.get('item_id') != item_id or candidate.get('reason') != 'row_partially_visible_bottom' or type(cost) is not int or (not discount_evidence_valid):
        return None
    return (item_id, points, cost, hint, discount)

def _source_geometry_alignment_evidence(state: Mapping[str, Any], active_item_ids: tuple[str, ...]) -> tuple[str, int, int] | None:
    if state.get('fresh') is not True or len(active_item_ids) != 1:
        return None
    item_id = active_item_ids[0]
    if not isinstance(item_id, str) or not item_id:
        return None
    rows = state.get('rows')
    selected = state.get('selected_item_ids')
    candidates = state.get('target_candidates')
    points = state.get('current_skill_points')
    if not isinstance(rows, list) or any((isinstance(row, Mapping) and row.get('item_id') == item_id for row in rows)) or (not isinstance(selected, list)) or (item_id in selected) or (not isinstance(candidates, list)) or (len(candidates) != 1) or (type(points) is not int):
        return None
    candidate = candidates[0]
    if not isinstance(candidate, Mapping):
        return None
    row_bbox = candidate.get('row_bbox')
    source_row_bbox = candidate.get('source_row_bbox')
    delta_y = candidate.get('geometry_delta_y')
    if candidate.get('item_id') != item_id or candidate.get('reason') != 'source_geometry_alignment_needed' or (not _valid_row_bbox(row_bbox)) or (not _valid_row_bbox(source_row_bbox)) or (type(delta_y) is not int) or (delta_y == 0) or (row_bbox[1] - source_row_bbox[1] != delta_y) or (row_bbox[3] - source_row_bbox[3] != delta_y) or (row_bbox[0] != source_row_bbox[0]) or (row_bbox[2] != source_row_bbox[2]):
        return None
    return (item_id, points, delta_y)

def _valid_row_bbox(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 4 and all((type(part) is int for part in value)) and (value[0] < value[2]) and (value[1] < value[3])

def _build_live_adapter(args: argparse.Namespace, inventory: Mapping[str, Any], batch_plan: Mapping[str, Any]) -> _LiveBatchAdapter:
    del inventory
    if args.review is None or args.screenshot_dir is None or args.checkpoint is None:
        raise ValueError('live_adapter_inputs_missing')
    review = _read_json(args.review, label='review')
    binding_fields = ('plan_id', 'sample_id', 'session_id', 'capture_batch_id')
    if any((not isinstance(review.get(field), str) for field in binding_fields)):
        raise ValueError('review_binding_missing')
    from core.adb import ADB
    adb = ADB(host='127.0.0.1', port=ADB_PORT, skill_scroll_endpoint=args.adb_endpoint)
    if adb.address != _ALLOWED_ADB_ENDPOINT:
        raise ValueError('adb_endpoint_not_allowed')
    if adb.connect() is not True:
        raise ValueError('adb_connect_failed')
    session = SkillPurchaseLiveCaptureSession(review=review, root=ROOT, plan_id=review['plan_id'], sample_id=review['sample_id'], session_id=review['session_id'], capture_batch_id=review['capture_batch_id'], selection_plan=batch_plan)
    return _LiveBatchAdapter(adb=adb, session=session, review=review, screenshot_dir=args.screenshot_dir, checkpoint=args.checkpoint, swipe_profile=getattr(args, 'swipe_profile', 'full-page'))

def main(argv: list[str] | None=None) -> int:
    report, code = run(parse_args(argv))
    compact = {key: report.get(key) for key in ('phase', 'is_valid', 'stop_reason', 'inventory_sha256', 'plan_sha256', 'selected_action_ids', 'initial_skill_points', 'spent_skill_points', 'expected_remaining_skill_points', 'tap_count', 'swipe_count', 'alignment_swipe_count', 'confirmation_opened') if key in report}
    print(json.dumps(compact, ensure_ascii=False, separators=(',', ':')))
    return code
if __name__ == '__main__':
    raise SystemExit(main())
