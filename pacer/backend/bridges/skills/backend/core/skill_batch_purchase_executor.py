"""Page-grouped selector for a SHA-bound skill batch plan and traversal."""
from __future__ import annotations
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from core.skill_batch_purchase import skill_selection_groups, validate_skill_batch_plan, validate_skill_inventory_packet
SKILL_BATCH_PURCHASE_SAFETY_TOKEN = 'UG_PARENT_SKILL_BATCH_PURCHASE'
MAX_TOP_REWIND_SWIPES = 24
MAX_FRESH_PAGE_READS = 4
FRESH_PAGE_RETRY_DELAY_SECONDS = 0.4

class SkillBatchAdapter(Protocol):

    def capture_page(self, active_item_ids: tuple[str, ...]) -> Mapping[str, Any]:
        """Capture and parse one fresh skill page using only the active row scope."""

    def rewind_to_top(self, max_swipes: int) -> bool:
        """Return True only after bounded, adapter-verified return to list top."""

    def begin_reverse_selection(self, shifts: list[float], max_swipes: int) -> bool:
        """Bind the reverse route and verify the physical bottom before moving."""

    def swipe_backward(self) -> None:
        """Move towards the preceding captured page, including a clamped step."""

    def tap_plus(self, control: Mapping[str, int], action_id: str) -> None:
        """Tap one already-validated plus control."""

    def swipe_forward(self) -> None:
        """Move one fixed capture-profile page forward."""

    def reveal_bottom_after_rewind(self) -> bool:
        """Perform one adapter-guarded navigation-only final-row reveal."""

    def open_confirmation(self, control: Mapping[str, int]) -> None:
        """Open the single purchase confirmation dialog."""

    def persist_checkpoint(self, checkpoint: Mapping[str, Any]) -> None:
        """Persist compact progress after every page or partial failure."""

@dataclass(frozen=True)
class SkillBatchExecutionResult:
    stop_reason: str
    selected_action_ids: tuple[str, ...]
    remaining_action_ids: tuple[str, ...]
    confirmation_opened: bool = False
    tap_count: int = 0
    swipe_count: int = 0
    error_type: str | None = None
    error_detail: str | None = None

class SkillBatchPurchaseExecutor:
    """Select a precomputed batch without re-planning or rescanning the list."""

    def __init__(self, *, inventory: Mapping[str, Any], plan: Mapping[str, Any], adapter: SkillBatchAdapter, mode: str='dry-run', execute: bool=False, safety_token: str | None=None, expected_inventory_sha256: str | None=None, expected_plan_sha256: str | None=None, resume_checkpoint: Mapping[str, Any] | None=None, now_unix_ms: int | None=None) -> None:
        self.inventory = inventory
        self.plan = plan
        self.adapter = adapter
        self.mode = mode
        self.execute = execute
        self.safety_token = safety_token
        self.expected_inventory_sha256 = expected_inventory_sha256
        self.expected_plan_sha256 = expected_plan_sha256
        self.resume_checkpoint = resume_checkpoint
        self.now_unix_ms = now_unix_ms
        self._resume_pending_page_index: int | None = None
        self._seen_frame_sha256: set[str] = set()
        self._selected: list[str] = []
        self._tap_count = 0
        self._swipe_count = 0
        self._last_group_reveal_retry_used = False

    def run_selection(self) -> SkillBatchExecutionResult:
        gate_error = self._validate_gate()
        if gate_error:
            return self._result(gate_error)
        page_groups = skill_selection_groups(self.plan)
        if not isinstance(page_groups, list) or not page_groups:
            return self._result('batch_plan_empty')
        reverse = 'reverse_scroll_shifts' in self.plan
        direction = -1 if reverse else 1
        boundary_error = 'bottom_page_unverified' if reverse else 'top_page_unverified'
        try:
            at_boundary = self.adapter.begin_reverse_selection(self.plan['reverse_scroll_shifts'], MAX_TOP_REWIND_SWIPES) if reverse else self.adapter.rewind_to_top(MAX_TOP_REWIND_SWIPES)
        except Exception as exc:
            return self._result(boundary_error, exc=exc)
        if at_boundary is not True:
            return self._result(boundary_error)
        current_page_index = len(self.plan['reverse_scroll_shifts']) if reverse else 0
        cumulative_cost = self._selected_cost()
        post_state: Mapping[str, Any] | None = None
        last_items: list[Mapping[str, Any]] | None = None
        for group_index, raw_group in enumerate(page_groups):
            if not isinstance(raw_group, Mapping):
                return self._result('page_group_invalid')
            page_index = raw_group.get('page_index')
            items = raw_group.get('items')
            if type(page_index) is not int or direction * (page_index - current_page_index) < 0 or (not isinstance(items, list)) or (not items) or any((not isinstance(item, Mapping) for item in items)):
                return self._result('page_group_invalid')
            last_items = items
            while current_page_index != page_index:
                try:
                    if reverse:
                        self.adapter.swipe_backward()
                    else:
                        self.adapter.swipe_forward()
                except Exception as exc:
                    return self._result('page_swipe_failed', exc=exc)
                self._swipe_count += 1
                current_page_index += direction
            active_ids = tuple((str(item['action_id']) for item in items))
            if self._resume_pending_page_index is not None and direction * (page_index - self._resume_pending_page_index) < 0:
                continue
            state, error = self._fresh_page(active_ids)
            if error:
                return self._result(error)
            assert state is not None
            group_cost = sum((int(item['display_cost']) for item in items))
            selected_after = self._selected + [str(item['action_id']) for item in items]
            cumulative_after = cumulative_cost + group_cost
            base_error = self._validate_page_base(state, expected_current_skill_points=int(self.plan['initial_skill_points']) - cumulative_cost)
            recovered = False
            rows: dict[str, Mapping[str, Any]] | None = None
            nudged_this_page = False
            if base_error:
                recovered_base_error = self._validate_page_base(state, expected_current_skill_points=int(self.plan['initial_skill_points']) - cumulative_after)
                recovered_selected_error = self._validate_selected_group(state, items, selected_after=selected_after, cumulative_cost=cumulative_after)
                if recovered_base_error is None and recovered_selected_error is None:
                    self._selected = selected_after
                    cumulative_cost = cumulative_after
                    post_state = state
                    recovered = True
                    self._persist_checkpoint(status='page_recovered', page_index=page_index, pending_group_ids=())
                else:
                    return self._result(base_error)
            if not recovered:
                rows, row_error = self._validate_planned_rows(state, items)
                if row_error is not None and (not reverse) and (not self._last_group_reveal_retry_used) and (self.resume_checkpoint is not None) and (self._resume_pending_page_index == page_index) and bool(self._selected) and (group_index + 1 == len(page_groups)) and (len(items) == 1) and (state.get('rows') == []) and (state.get('target_candidates') == []):
                    self._last_group_reveal_retry_used = True
                    try:
                        revealed = self.adapter.reveal_bottom_after_rewind()
                    except Exception as exc:
                        return self._result('last_group_reveal_failed', exc=exc)
                    if revealed is not True:
                        return self._result('last_group_reveal_failed')
                    self._swipe_count += 1
                    state, error = self._fresh_page(active_ids)
                    if error:
                        return self._result(error)
                    assert state is not None
                    base_error = self._validate_page_base(state, expected_current_skill_points=int(self.plan['initial_skill_points']) - cumulative_cost)
                    if base_error:
                        return self._result(base_error)
                    rows, row_error = self._validate_planned_rows(state, items)
                if row_error and row_error.startswith(('planned_row_identity_mismatch:', 'planned_row_price_mismatch:', 'planned_row_discount_mismatch:')):
                    time.sleep(0.6)
                    retry_state, retry_err = self._fresh_page(active_ids)
                    if not retry_err and retry_state is not None:
                        if self._validate_page_base(retry_state, expected_current_skill_points=int(self.plan['initial_skill_points']) - cumulative_cost) is None:
                            retry_rows, retry_row_err = self._validate_planned_rows(retry_state, items)
                            if retry_row_err is None:
                                state = retry_state
                                rows, row_error = retry_rows, None
                if row_error and row_error.startswith('planned_row_identity_mismatch:') and callable(getattr(self.adapter, 'nudge_scroll', None)):
                    nudge_state, nudge_rows, nudge_err, nudge_exc = self._recover_missing_planned_rows(
                        items=items,
                        active_ids=active_ids,
                        initial_row_error=row_error,
                        cumulative_cost=cumulative_cost,
                        reverse_traversal=reverse,
                    )
                    if nudge_rows is not None and nudge_state is not None:
                        state = nudge_state
                        rows, row_error = nudge_rows, None
                        nudged_this_page = True
                    elif nudge_exc is not None:
                        return self._result(nudge_err or 'page_swipe_failed', exc=nudge_exc)
                    else:
                        row_error = nudge_err or row_error
                if row_error:
                    return self._result(row_error)
                assert rows is not None
                ordered_items = sorted(items, key=lambda item: int(rows[str(item['action_id'])]['row_index']), reverse=True)
                for item in ordered_items:
                    action_id = str(item['action_id'])
                    row = rows[action_id]
                    try:
                        self.adapter.tap_plus(row['plus_control'], action_id)
                    except Exception as exc:
                        self._persist_checkpoint(status='tap_failed', page_index=page_index, pending_group_ids=active_ids)
                        return self._result(f'planned_plus_tap_failed:{action_id}', exc=exc)
                    self._tap_count += 1
                post_state, error = self._fresh_page(active_ids)
                if error:
                    self._persist_checkpoint(status='selection_unverified', page_index=page_index, pending_group_ids=active_ids)
                    return self._result(error)
                assert post_state is not None
                base_error = self._validate_page_base(post_state, expected_current_skill_points=int(self.plan['initial_skill_points']) - cumulative_after)
                if base_error:
                    time.sleep(1.2)
                    post_state, error = self._fresh_page(active_ids)
                    if error:
                        self._persist_checkpoint(status='selection_unverified', page_index=page_index, pending_group_ids=active_ids)
                        return self._result(error)
                    assert post_state is not None
                    base_error = self._validate_page_base(post_state, expected_current_skill_points=int(self.plan['initial_skill_points']) - cumulative_after)
                if base_error:
                    self._persist_checkpoint(status='selection_unverified', page_index=page_index, pending_group_ids=active_ids)
                    return self._result(base_error)
                selected_error = self._validate_selected_group(post_state, items, selected_after=selected_after, cumulative_cost=cumulative_after)
                if selected_error:
                    time.sleep(1.0)
                    retry_state, retry_err = self._fresh_page(active_ids)
                    if not retry_err and retry_state is not None:
                        retry_base_err = self._validate_page_base(retry_state, expected_current_skill_points=int(self.plan['initial_skill_points']) - cumulative_after)
                        if not retry_base_err:
                            retry_sel_err = self._validate_selected_group(retry_state, items, selected_after=selected_after, cumulative_cost=cumulative_after)
                            if not retry_sel_err:
                                post_state = retry_state
                                selected_error = None
                if selected_error:
                    self._persist_checkpoint(status='selection_unverified', page_index=page_index, pending_group_ids=active_ids)
                    return self._result(selected_error)
                self._selected = selected_after
                cumulative_cost = cumulative_after
            self._persist_checkpoint(status='page_verified', page_index=page_index, pending_group_ids=())
            if page_index == self._resume_pending_page_index:
                self._resume_pending_page_index = None
            if group_index + 1 < len(page_groups):
                next_group = page_groups[group_index + 1]
                if not isinstance(next_group, Mapping):
                    return self._result('page_group_invalid')
                next_index = next_group.get('page_index')
                if type(next_index) is not int or direction * (next_index - page_index) <= 0:
                    return self._result('page_group_order_invalid')
                if nudged_this_page and rows is not None and callable(getattr(self.adapter, 'nudge_scroll', None)):
                    self._recalibrate_after_nudge(items=items, live_rows=rows)
        if post_state is None:
            if not last_items:
                return self._result('batch_plan_empty')
            active_ids = tuple((str(item['action_id']) for item in last_items))
            post_state, error = self._fresh_page(active_ids)
            if error:
                return self._result(error)
            assert post_state is not None
            base_error = self._validate_page_base(post_state, expected_current_skill_points=int(self.plan['initial_skill_points']) - cumulative_cost)
            if base_error:
                return self._result(base_error)
            selected_error = self._validate_selected_group(post_state, last_items, selected_after=self._selected, cumulative_cost=cumulative_cost)
            if selected_error:
                return self._result(selected_error)
        confirm_control = post_state.get('green_confirm_control')
        if not self._valid_control(confirm_control) and last_items:
            active_ids = tuple((str(item['action_id']) for item in last_items))
            for _ in range(2):
                time.sleep(0.8)
                retry_state, retry_err = self._fresh_page(active_ids)
                if retry_err or retry_state is None:
                    continue
                if self._validate_page_base(retry_state, expected_current_skill_points=int(self.plan['initial_skill_points']) - cumulative_cost) is not None:
                    continue
                candidate_ctrl = retry_state.get('green_confirm_control')
                if self._valid_control(candidate_ctrl):
                    confirm_control = candidate_ctrl
                    break
        if not self._valid_control(confirm_control):
            return self._result('green_confirm_control_invalid')
        try:
            self.adapter.open_confirmation(confirm_control)
        except Exception as exc:
            return self._result('open_confirmation_failed', exc=exc)
        self._tap_count += 1
        return self._result('confirmation_opened', confirmation_opened=True)

    def _validate_gate(self) -> str | None:
        if self.mode != 'live' or self.execute is not True or self.safety_token != SKILL_BATCH_PURCHASE_SAFETY_TOKEN:
            return 'execution_gate_rejected'
        inventory_valid, inventory_reason = validate_skill_inventory_packet(self.inventory)
        if not inventory_valid:
            return f'inventory_invalid:{inventory_reason}'
        plan_valid, plan_reason = validate_skill_batch_plan(self.plan, self.inventory)
        if not plan_valid:
            return f'plan_invalid:{plan_reason}'
        if self.expected_inventory_sha256 != self.inventory.get('packet_sha256') or self.expected_plan_sha256 != self.plan.get('plan_sha256'):
            return 'expected_sha_mismatch'
        now_unix_ms = int(time.time() * 1000) if self.now_unix_ms is None else self.now_unix_ms
        if type(now_unix_ms) is not int:
            return 'now_unix_ms_invalid'
        captured_at = self.inventory.get('captured_at_unix_ms')
        expires_at = self.inventory.get('expires_at_unix_ms')
        if now_unix_ms < captured_at - 30000:
            return 'capture_not_yet_valid'
        if now_unix_ms > expires_at:
            return 'capture_expired'
        if self.resume_checkpoint is not None:
            if not self._apply_resume_checkpoint(self.resume_checkpoint):
                return 'resume_checkpoint_invalid'
        return None

    def _apply_resume_checkpoint(self, checkpoint: Mapping[str, Any]) -> bool:
        required = {'schema_version', 'inventory_packet_sha256', 'plan_sha256', 'status', 'page_index', 'selected_action_ids', 'pending_group_ids', 'remaining_action_ids', 'tap_count', 'swipe_count'}
        if not isinstance(checkpoint, Mapping) or set(checkpoint) != required or checkpoint.get('schema_version') != 'uma.skill_batch_selection_checkpoint.v1' or (checkpoint.get('inventory_packet_sha256') != self.inventory.get('packet_sha256')) or (checkpoint.get('plan_sha256') != self.plan.get('plan_sha256')):
            return False
        page_index = checkpoint.get('page_index')
        selected = checkpoint.get('selected_action_ids')
        pending = checkpoint.get('pending_group_ids')
        remaining = checkpoint.get('remaining_action_ids')
        if type(page_index) is not int or not isinstance(selected, list) or (not isinstance(pending, list)) or (not isinstance(remaining, list)) or any((not isinstance(item, str) or not item for values in (selected, pending, remaining) for item in values)) or (len(selected) != len(set(selected))) or (len(pending) != len(set(pending))) or (type(checkpoint.get('tap_count')) is not int) or (checkpoint['tap_count'] < 0) or (type(checkpoint.get('swipe_count')) is not int) or (checkpoint['swipe_count'] < 0):
            return False
        groups = skill_selection_groups(self.plan)
        if not isinstance(groups, list):
            return False
        status = checkpoint.get('status')
        prior_ids: list[str] = []
        pending_ids: list[str] | None = None
        verified_ids: list[str] = []
        max_page_index: int | None = None
        direction = -1 if 'reverse_scroll_shifts' in self.plan else 1
        for raw_group in groups:
            if not isinstance(raw_group, Mapping):
                return False
            group_page = raw_group.get('page_index')
            items = raw_group.get('items')
            if type(group_page) is not int or not isinstance(items, list):
                return False
            group_ids = [str(item.get('action_id')) for item in items if isinstance(item, Mapping)]
            if len(group_ids) != len(items):
                return False
            max_page_index = group_page if max_page_index is None else max(max_page_index, group_page)
            if direction * (group_page - page_index) < 0:
                prior_ids.extend(group_ids)
            elif group_page == page_index:
                pending_ids = group_ids
            if direction * (group_page - page_index) <= 0:
                verified_ids.extend(group_ids)
        planned_actions = self.plan.get('selected_action_ids')
        if not isinstance(planned_actions, list) or pending_ids is None:
            return False
        expected_remaining = [action for action in planned_actions if action not in set(selected)]
        if status == 'selection_unverified':
            if selected != prior_ids or pending != pending_ids or remaining != expected_remaining:
                return False
            self._resume_pending_page_index = page_index
        elif status == 'page_verified':
            if selected != verified_ids or pending != [] or remaining != expected_remaining or (max_page_index is None):
                return False
            next_pages = [group.get('page_index') for group in groups if isinstance(group, Mapping) and type(group.get('page_index')) is int and (direction * (group['page_index'] - page_index) > 0)]
            self._resume_pending_page_index = next_pages[0] if next_pages else -1 if direction < 0 else max_page_index + 1
        else:
            return False
        self._selected = list(selected)
        return True

    def validate_resume_checkpoint(self, checkpoint: Mapping[str, Any]) -> bool:
        """Validate one checkpoint without calling the adapter."""
        return self._apply_resume_checkpoint(checkpoint)

    def _selected_cost(self) -> int:
        selected = set(self._selected)
        total = 0
        for raw_group in self.plan.get('page_groups', []):
            if not isinstance(raw_group, Mapping):
                continue
            for item in raw_group.get('items', []):
                if isinstance(item, Mapping) and item.get('action_id') in selected:
                    total += int(item['display_cost'])
        return total

    def _fresh_page(self, active_ids: tuple[str, ...]) -> tuple[Mapping[str, Any] | None, str | None]:
        for attempt in range(MAX_FRESH_PAGE_READS):
            duplicate = False
            try:
                capture = (getattr(self.adapter, 'recapture_page', self.adapter.capture_page)
                           if attempt else self.adapter.capture_page)
                state = capture(active_ids)
            except ValueError as error:
                if str(error) != 'frame SHA already seen':
                    return (None, 'fresh_page_capture_failed')
                state = None
                duplicate = True
            except Exception:
                return (None, 'fresh_page_capture_failed')
            if not duplicate:
                if not isinstance(state, Mapping) or state.get('fresh') is not True:
                    return (None, 'fresh_page_required')
                frame_sha = state.get('frame_sha256')
                if not isinstance(frame_sha, str) or len(frame_sha) != 64 or any((character not in '0123456789abcdef' for character in frame_sha)):
                    return (None, 'frame_sha_invalid')
                if frame_sha not in self._seen_frame_sha256:
                    self._seen_frame_sha256.add(frame_sha)
                    return (state, None)
            # Rendering can lag an acknowledged input. Only observe again;
            # never repeat a plus tap, swipe, or confirmation here.
            if attempt + 1 < MAX_FRESH_PAGE_READS:
                time.sleep(FRESH_PAGE_RETRY_DELAY_SECONDS)
        return (None, 'frame_sha_replayed')

    def _validate_page_base(self, state: Mapping[str, Any], *, expected_current_skill_points: int) -> str | None:
        if state.get('page_kind') != 'skill_acquisition' or state.get('screen_kind') != 'skill_acquisition':
            return 'skill_page_mismatch'
        if state.get('currency_scope') != 'skill_points_only':
            return 'currency_scope_mismatch'
        if state.get('dangerous_resource_flags') != []:
            return 'dangerous_resource_detected'
        if state.get('current_skill_points') != expected_current_skill_points:
            return 'current_skill_points_drift'
        return None

    def _validate_planned_rows(self, state: Mapping[str, Any], items: list[Mapping[str, Any]]) -> tuple[dict[str, Mapping[str, Any]] | None, str | None]:
        raw_rows = state.get('rows')
        if not isinstance(raw_rows, list):
            return (None, 'structured_rows_missing')
        rows_by_id: dict[str, list[Mapping[str, Any]]] = {}
        for row in raw_rows:
            if not isinstance(row, Mapping):
                continue
            item_id = row.get('item_id', row.get('skill_id'))
            if isinstance(item_id, str) and item_id:
                rows_by_id.setdefault(item_id, []).append(row)
        resolved: dict[str, Mapping[str, Any]] = {}
        for item in items:
            action_id = str(item['action_id'])
            matches = rows_by_id.get(action_id, [])
            if len(matches) != 1:
                return (None, f'planned_row_identity_mismatch:{action_id}')
            row = matches[0]
            if row.get('observed_final_cost') != item.get('display_cost'):
                return (None, f'planned_row_price_mismatch:{action_id}')
            if row.get('observed_discount_percent') is not None and row.get('observed_discount_percent') != item.get('observed_discount_percent'):
                return (None, f'planned_row_discount_mismatch:{action_id}')
            if row.get('selected') is not False or row.get('plus_available') is not True or (not self._valid_control(row.get('plus_control'))) or (type(row.get('row_index')) is not int):
                return (None, f'planned_row_not_selectable:{action_id}')
            resolved[action_id] = row
        return (resolved, None)

    def _validate_selected_group(self, state: Mapping[str, Any], items: list[Mapping[str, Any]], *, selected_after: list[str], cumulative_cost: int) -> str | None:
        del selected_after
        group_ids = [str(item['action_id']) for item in items]
        selected_ids = state.get('selected_item_ids')
        if not isinstance(selected_ids, list) or sorted(selected_ids) != sorted(group_ids) or state.get('selected_total') != cumulative_cost or (state.get('remaining_skill_points') != self.plan['initial_skill_points'] - cumulative_cost) or (state.get('remaining_points_semantics') != 'post_selection'):
            return 'selected_group_economics_mismatch'
        rows = state.get('rows')
        if not isinstance(rows, list):
            return 'selected_rows_missing'
        row_by_id = {row.get('item_id', row.get('skill_id')): row for row in rows if isinstance(row, Mapping)}
        for item in items:
            action_id = str(item['action_id'])
            row = row_by_id.get(action_id)
            if row is None:
                continue
            if row.get('selected') is not True:
                return f'selected_row_state_mismatch:{action_id}'
        return None

    def _persist_checkpoint(self, *, status: str, page_index: int, pending_group_ids: tuple[str, ...]) -> None:
        checkpoint = {'schema_version': 'uma.skill_batch_selection_checkpoint.v1', 'inventory_packet_sha256': self.inventory['packet_sha256'], 'plan_sha256': self.plan['plan_sha256'], 'status': status, 'page_index': page_index, 'selected_action_ids': list(self._selected), 'pending_group_ids': list(pending_group_ids), 'remaining_action_ids': list(self._remaining_actions()), 'tap_count': self._tap_count, 'swipe_count': self._swipe_count}
        try:
            self.adapter.persist_checkpoint(checkpoint)
        except Exception:
            return

    def _remaining_actions(self) -> tuple[str, ...]:
        selected = set(self._selected)
        return tuple((action for action in self.plan.get('selected_action_ids', []) if action not in selected))

    def _result(self, reason: str, *, confirmation_opened: bool=False, exc: Exception | None=None) -> SkillBatchExecutionResult:
        return SkillBatchExecutionResult(stop_reason=reason, selected_action_ids=tuple((action for action in self.plan.get('selected_action_ids', []) if action in self._selected)), remaining_action_ids=self._remaining_actions(), confirmation_opened=confirmation_opened, tap_count=self._tap_count, swipe_count=self._swipe_count, error_type=None if exc is None else type(exc).__name__, error_detail=None if exc is None else str(exc))

    def _recover_missing_planned_rows(
        self,
        *,
        items: list[Mapping[str, Any]],
        active_ids: tuple[str, ...],
        initial_row_error: str,
        cumulative_cost: int,
        reverse_traversal: bool,
    ) -> tuple[Mapping[str, Any] | None, dict[str, Mapping[str, Any]] | None, str | None, Exception | None]:
        missing_id = initial_row_error.split('planned_row_identity_mismatch:', 1)[1]
        target_item = next((item for item in items if str(item.get('action_id')) == missing_id), items[0])
        primary_reverse = self._primary_nudge_reverse(target_item, reverse_traversal=reverse_traversal)
        schedule = (
            (primary_reverse, 140),
            (primary_reverse, 140),
            (not primary_reverse, 200),
            (not primary_reverse, 200),
        )
        for nudge_reverse, travel_px in schedule:
            try:
                nudge_ok = self.adapter.nudge_scroll(reverse=nudge_reverse, travel_px=travel_px)
            except Exception as exc:
                return (None, None, 'page_swipe_failed', exc)
            if nudge_ok is not True:
                break
            self._swipe_count += 1
            nudge_state, nudge_err = self._fresh_page(active_ids)
            if nudge_err == 'frame_sha_replayed':
                continue
            if nudge_err or nudge_state is None:
                return (None, None, nudge_err or 'fresh_page_capture_failed', None)
            nudge_base_err = self._validate_page_base(
                nudge_state,
                expected_current_skill_points=int(self.plan['initial_skill_points']) - cumulative_cost,
            )
            if nudge_base_err is not None:
                return (None, None, nudge_base_err, None)
            nudge_rows, nudge_row_err = self._validate_planned_rows(nudge_state, items)
            if nudge_row_err is None:
                return (nudge_state, nudge_rows, None, None)
            if not nudge_row_err.startswith('planned_row_identity_mismatch:'):
                return (None, None, nudge_row_err, None)
        return (None, None, initial_row_error, None)

    def _recalibrate_after_nudge(
        self,
        *,
        items: list[Mapping[str, Any]],
        live_rows: Mapping[str, Mapping[str, Any]],
    ) -> None:
        deltas: list[int] = []
        for item in items:
            action_id = str(item['action_id'])
            planned_bbox = item.get('row_bbox')
            live_row = live_rows.get(action_id) if isinstance(live_rows, Mapping) else None
            live_bbox = live_row.get('row_bbox') if isinstance(live_row, Mapping) else None
            if self._valid_bbox(planned_bbox) and self._valid_bbox(live_bbox):
                deltas.append(int(live_bbox[1]) - int(planned_bbox[1]))
        if not deltas:
            return
        deltas.sort()
        dy = deltas[len(deltas) // 2]
        if abs(dy) < 40:
            return
        travel_px = min(200, abs(dy))
        try:
            if self.adapter.nudge_scroll(reverse=(dy < 0), travel_px=travel_px) is True:
                self._swipe_count += 1
        except Exception:
            return

    @staticmethod
    def _primary_nudge_reverse(item: Mapping[str, Any], *, reverse_traversal: bool) -> bool:
        row_index = item.get('row_index')
        row_bbox = item.get('row_bbox')
        if row_index == 0 or (SkillBatchPurchaseExecutor._valid_bbox(row_bbox) and row_bbox[1] < 640):
            return True
        if (type(row_index) is int and row_index >= 2) or (SkillBatchPurchaseExecutor._valid_bbox(row_bbox) and row_bbox[3] > 900):
            return False
        return reverse_traversal

    @staticmethod
    def _valid_bbox(value: Any) -> bool:
        return isinstance(value, (list, tuple)) and len(value) == 4 and all(type(part) is int for part in value) and (value[0] < value[2]) and (value[1] < value[3])

    @staticmethod
    def _valid_control(value: Any) -> bool:
        return isinstance(value, Mapping) and type(value.get('x')) is int and (type(value.get('y')) is int) and (0 <= value['x'] < 720) and (0 <= value['y'] < 1280)
__all__ = ['SKILL_BATCH_PURCHASE_SAFETY_TOKEN', 'SkillBatchAdapter', 'SkillBatchExecutionResult', 'SkillBatchPurchaseExecutor']
