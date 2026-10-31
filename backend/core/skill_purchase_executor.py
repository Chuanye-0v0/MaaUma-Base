"""Fail-closed executor for a U1-promoted skill purchase plan.

This module deliberately owns no device connection and never creates one.  A
caller must inject both the screenshot provider and the fresh-state parser.
The final confirmation tap is intentionally not implemented until a verified
dialog fixture exists.
"""
from __future__ import annotations
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol
from core.name_match import name_similarity
SKILL_PURCHASE_SAFETY_TOKEN = 'UG_PARENT_SKILL_PURCHASE_SANDBOX'
SKILL_MULTI_CONFIRMATION_CAPTURE_TOKEN = 'UG_PARENT_SKILL_MULTI_CONFIRMATION_CAPTURE'
_ALLOWED_REMAINING_SEMANTICS = {'post_selection', 'current_before_confirmation'}
_ALLOWED_EXECUTION_STAGES = {'full', 'select_plus_probe', 'open_confirmation_probe', 'multi_confirmation_capture_probe', 'final_confirmation_probe', 'batch_final_confirmation_probe', 'batch_close_success_probe', 'close_success_probe', 'return_after_purchase_probe', 'batch_return_after_purchase_probe'}
_SELECT_PLUS_ACTION_ID = 'skill_purchase.select_plus'
_OPEN_CONFIRMATION_ACTION_ID = 'skill_purchase.open_confirmation'
_MULTI_CONFIRMATION_CAPTURE_ACTION_ID = 'skill_purchase.capture_multi_confirmation'
_MULTI_CAPTURE_SELECT_ACTION_ID = 'skill_purchase.capture_multi_select_plus'
_FINAL_CONFIRMATION_ACTION_ID = 'skill_purchase.final_confirm'
_CLOSE_SUCCESS_ACTION_ID = 'skill_purchase.close_success'
_RETURN_AFTER_PURCHASE_ACTION_ID = 'skill_purchase.return_after_purchase'
_REQUIRED_OCR_CONTRACT_FIELDS = ('page_kind', 'screen_kind', 'row_complete', 'name_resolution_status', 'name_resolution_confidence_min', 'observed_name', 'observed_final_cost', 'cost_confidence_min', 'hint_level', 'discount_percent', 'hint_confidence_min', 'plus_available', 'skill_type', 'rarity_or_icon_type')
_REQUIRED_LIVE_ROW_FIELDS = ('page_kind', 'screen_kind', 'row_complete', 'name_resolution_status', 'name_resolution_confidence', 'observed_name', 'observed_final_cost', 'cost_confidence', 'hint_level', 'discount_percent', 'hint_confidence', 'plus_available', 'skill_type', 'rarity_or_icon_type')

@dataclass(frozen=True)
class SkillPurchaseGateResult:
    allowed: bool
    reason: str

@dataclass(frozen=True)
class DialogVerification:
    verified: bool
    reason: str

class ConfirmationDialogVerifier(Protocol):
    """Validate a freshly parsed confirmation dialog without clicking it."""

    def verify(self, dialog_state: Mapping[str, Any], plan: Mapping[str, Any]) -> DialogVerification:
        """Return evidence status for the displayed confirmation dialog."""

class MissingConfirmationDialogVerifier:
    """Safe default until U1 contributes a real confirmation-dialog fixture."""

    def verify(self, dialog_state: Mapping[str, Any], plan: Mapping[str, Any]) -> DialogVerification:
        return DialogVerification(False, 'confirmation_dialog_fixture_missing')

@dataclass(frozen=True)
class SkillPurchaseExecutionResult:
    stop_reason: str
    actions: tuple[str, ...] = field(default_factory=tuple)

    @property
    def tap_count(self) -> int:
        return len(self.actions)

def validate_skill_purchase_gate(mode: str, execute: bool, safety_token: str | None) -> SkillPurchaseGateResult:
    """Allow an executor run only with all three explicit live controls."""
    if mode != 'live':
        return SkillPurchaseGateResult(False, 'mode=live is required')
    if not execute:
        return SkillPurchaseGateResult(False, 'execute=true is required')
    if safety_token != SKILL_PURCHASE_SAFETY_TOKEN:
        return SkillPurchaseGateResult(False, 'safety token mismatch')
    return SkillPurchaseGateResult(True, 'live_execution_authorized')

def validate_multi_confirmation_capture_gate(mode: str, execute: bool, safety_token: str | None) -> SkillPurchaseGateResult:
    """Authorize only the terminal capture stage, never a purchase stage."""
    if mode != 'live':
        return SkillPurchaseGateResult(False, 'mode=live is required')
    if not execute:
        return SkillPurchaseGateResult(False, 'execute=true is required')
    if safety_token != SKILL_MULTI_CONFIRMATION_CAPTURE_TOKEN:
        return SkillPurchaseGateResult(False, 'capture safety token mismatch')
    return SkillPurchaseGateResult(True, 'capture_only_authorized')

class SkillPurchaseExecutor:
    """Perform only verified planned plus taps, then stop before final confirm."""

    def __init__(self, plan: Mapping[str, Any], *, adb: Any, parse_fresh_state: Callable[[Any], Mapping[str, Any]], dialog_verifier: ConfirmationDialogVerifier | None=None, mode: str='dry_run', execute: bool=False, safety_token: str | None=None, stage: str='full', expected_action_id: str | None=None) -> None:
        self.plan = plan
        self.adb = adb
        self.parse_fresh_state = parse_fresh_state
        self.dialog_verifier = dialog_verifier or MissingConfirmationDialogVerifier()
        self.mode = mode
        self.execute = execute
        self.safety_token = safety_token
        self.stage = stage
        self.expected_action_id = expected_action_id
        self._actions: list[str] = []
        self._remaining_semantics: str | None = None
        self._seen_frame_capture_ids: set[str] = set()
        self._seen_frame_sha256: set[str] = set()
        self._last_frame_sequence: int | None = None
        self._last_frame_sha256: str | None = None

    def run(self) -> SkillPurchaseExecutionResult:
        if self.stage == 'multi_confirmation_capture_probe':
            gate = validate_multi_confirmation_capture_gate(self.mode, self.execute, self.safety_token)
        else:
            gate = validate_skill_purchase_gate(self.mode, self.execute, self.safety_token)
        if not gate.allowed:
            return self._stop('execution_gate_rejected')
        if self.stage not in _ALLOWED_EXECUTION_STAGES:
            return self._stop('execution_stage_invalid')
        if self.stage == 'select_plus_probe' and self.expected_action_id != _SELECT_PLUS_ACTION_ID:
            return self._stop('expected_action_id_mismatch')
        if self.stage == 'open_confirmation_probe' and self.expected_action_id != _OPEN_CONFIRMATION_ACTION_ID:
            return self._stop('expected_action_id_mismatch')
        if self.stage == 'multi_confirmation_capture_probe' and self.expected_action_id != _MULTI_CONFIRMATION_CAPTURE_ACTION_ID:
            return self._stop('expected_action_id_mismatch')
        if self.stage in {'final_confirmation_probe', 'batch_final_confirmation_probe'} and self.expected_action_id != _FINAL_CONFIRMATION_ACTION_ID:
            return self._stop('expected_action_id_mismatch')
        if self.stage in {'close_success_probe', 'batch_close_success_probe'} and self.expected_action_id != _CLOSE_SUCCESS_ACTION_ID:
            return self._stop('expected_action_id_mismatch')
        if self.stage in {'return_after_purchase_probe', 'batch_return_after_purchase_probe'} and self.expected_action_id != _RETURN_AFTER_PURCHASE_ACTION_ID:
            return self._stop('expected_action_id_mismatch')
        plan_error = self._validate_capture_only_plan() if self.stage == 'multi_confirmation_capture_probe' else self._validate_batch_final_confirmation_plan() if self.stage in {'batch_final_confirmation_probe', 'batch_close_success_probe', 'batch_return_after_purchase_probe'} else self._validate_plan()
        if plan_error:
            return self._stop(plan_error)
        if self.stage == 'select_plus_probe':
            return self._run_select_plus_probe()
        if self.stage == 'open_confirmation_probe':
            return self._run_open_confirmation_probe()
        if self.stage == 'multi_confirmation_capture_probe':
            return self._run_multi_confirmation_capture_probe()
        if self.stage == 'final_confirmation_probe':
            return self._run_final_confirmation_probe()
        if self.stage == 'batch_final_confirmation_probe':
            return self._run_final_confirmation_probe()
        if self.stage == 'batch_close_success_probe':
            return self._run_batch_close_success_probe()
        if self.stage == 'close_success_probe':
            return self._run_close_success_probe()
        if self.stage == 'return_after_purchase_probe':
            return self._run_return_after_purchase_probe()
        if self.stage == 'batch_return_after_purchase_probe':
            return self._run_return_after_purchase_probe()
        state, state_error = self._fresh_state()
        if state_error:
            return self._stop(state_error)
        assert state is not None
        initial_error = self._validate_initial_state(state)
        if initial_error:
            return self._stop(initial_error)
        purchases = self.plan['purchases']
        selected_ids: list[str] = []
        for purchase in purchases:
            row, row_error = self._planned_row(state, purchase)
            if row_error:
                return self._stop(row_error)
            assert row is not None
            self._tap_control(row['plus_control'], 'planned_plus')
            selected_ids.append(str(purchase['item_id']))
            state, state_error = self._fresh_state(expect_ui_change=True)
            if state_error:
                return self._stop(state_error)
            assert state is not None
            selection_error = self._validate_selection_after_purchase(state, purchase, selected_ids)
            if selection_error:
                return self._stop(selection_error)
        state, state_error = self._fresh_state(expect_ui_change=False)
        if state_error:
            return self._stop(state_error)
        assert state is not None
        confirm_error = self._validate_green_confirm_preconditions(state, selected_ids)
        if confirm_error:
            return self._stop(confirm_error)
        control = state.get('green_confirm_control')
        if not self._valid_control(control):
            return self._stop('green_confirm_control_invalid')
        self._tap_control(control, 'green_confirm')
        dialog_state, state_error = self._fresh_state(expect_ui_change=True)
        if state_error:
            return self._stop(state_error)
        assert dialog_state is not None
        dialog_binding_error = self._validate_fresh_binding(dialog_state)
        if dialog_binding_error:
            return self._stop(f'dialog_{dialog_binding_error}')
        dialog_safety_error = self._validate_live_state_safety(dialog_state)
        if dialog_safety_error:
            return self._stop(f'dialog_{dialog_safety_error}')
        if dialog_state.get('page_kind') != 'confirmation_dialog':
            return self._stop('dialog_page_kind_mismatch')
        dialog_result = self.dialog_verifier.verify(dialog_state, self.plan)
        if not dialog_result.verified:
            return self._stop(dialog_result.reason)
        return self._stop('final_confirmation_disabled')

    def _run_select_plus_probe(self) -> SkillPurchaseExecutionResult:
        purchases = self.plan['purchases']
        if len(purchases) != 1:
            return self._stop('select_plus_probe_requires_one_purchase')
        state, state_error = self._fresh_state()
        if state_error:
            return self._stop(state_error)
        assert state is not None
        initial_error = self._validate_initial_state(state)
        if initial_error:
            return self._stop(initial_error)
        purchase = purchases[0]
        row, row_error = self._planned_row(state, purchase)
        if row_error:
            return self._stop(row_error)
        assert row is not None
        self._tap_control(row['plus_control'], _SELECT_PLUS_ACTION_ID)
        post_state, state_error = self._fresh_state(expect_ui_change=True)
        if state_error:
            return self._stop(state_error)
        assert post_state is not None
        page_error = self._validate_skill_acquisition_page(post_state, phase='post_plus_probe')
        if page_error:
            return self._stop(page_error)
        safety_error = self._validate_live_state_safety(post_state)
        if safety_error:
            return self._stop(safety_error)
        return self._stop('select_plus_probe_complete')

    def _run_open_confirmation_probe(self) -> SkillPurchaseExecutionResult:
        purchases = self.plan['purchases']
        if len(purchases) != 1:
            return self._stop('open_confirmation_probe_requires_one_purchase')
        state, state_error = self._fresh_state()
        if state_error:
            return self._stop(state_error)
        assert state is not None
        expected_ids = [str(purchases[0]['item_id'])]
        confirm_error = self._validate_green_confirm_preconditions(state, expected_ids)
        if confirm_error:
            return self._stop(confirm_error)
        control = state.get('green_confirm_control')
        if not self._valid_control(control):
            return self._stop('green_confirm_control_invalid')
        self._tap_control(control, _OPEN_CONFIRMATION_ACTION_ID)
        try:
            post_frame = self.adb.screenshot()
        except Exception:
            return self._stop('post_confirmation_screenshot_failed')
        if post_frame is None:
            return self._stop('post_confirmation_screenshot_failed')
        return self._stop('open_confirmation_probe_complete')

    def _run_multi_confirmation_capture_probe(self) -> SkillPurchaseExecutionResult:
        """Select an exact same-frame multi plan and capture its dialog only.

        This stage deliberately stops after the screenshot produced by opening
        the green confirmation control.  It never parses or taps the dialog,
        so a capture-only plan cannot become a purchase authorization.
        """
        purchases = self.plan['purchases']
        if len(purchases) < 2:
            return self._stop('multi_capture_requires_multiple_purchases')
        state, state_error = self._fresh_state()
        if state_error:
            return self._stop(state_error)
        assert state is not None
        initial_error = self._validate_initial_state(state)
        if initial_error:
            return self._stop(initial_error)
        initial_controls: set[tuple[int, int]] = set()
        for purchase in purchases:
            row, row_error = self._planned_row(state, purchase)
            if row_error:
                return self._stop(row_error)
            assert row is not None
            control = row['plus_control']
            control_key = (control['x'], control['y'])
            if control_key in initial_controls:
                return self._stop('capture_plus_controls_not_unique')
            initial_controls.add(control_key)
        selected_ids: list[str] = []
        for purchase in purchases:
            row, row_error = self._planned_row(state, purchase)
            if row_error:
                return self._stop(row_error)
            assert row is not None
            self._tap_control(row['plus_control'], _MULTI_CAPTURE_SELECT_ACTION_ID)
            selected_ids.append(str(purchase['item_id']))
            state, state_error = self._fresh_state(expect_ui_change=True)
            if state_error:
                return self._stop(state_error)
            assert state is not None
            selection_error = self._validate_selection_after_purchase(state, purchase, selected_ids)
            if selection_error:
                return self._stop(selection_error)
        confirm_error = self._validate_green_confirm_preconditions(state, selected_ids)
        if confirm_error:
            return self._stop(confirm_error)
        control = state.get('green_confirm_control')
        if not self._valid_control(control):
            return self._stop('green_confirm_control_invalid')
        self._tap_control(control, _MULTI_CONFIRMATION_CAPTURE_ACTION_ID)
        try:
            post_frame = self.adb.screenshot()
        except Exception:
            return self._stop('post_confirmation_screenshot_failed')
        if post_frame is None:
            return self._stop('post_confirmation_screenshot_failed')
        return self._stop('multi_confirmation_capture_probe_complete')

    def _run_final_confirmation_probe(self) -> SkillPurchaseExecutionResult:
        purchases = self.plan['purchases']
        if self.stage == 'final_confirmation_probe' and len(purchases) != 1:
            return self._stop('final_confirmation_probe_requires_one_purchase')
        if self.stage == 'batch_final_confirmation_probe' and len(purchases) < 2:
            return self._stop('batch_final_confirmation_probe_requires_multiple_purchases')
        for _ in range(3):
            state, state_error = self._fresh_state()
            if state_error not in {'fresh_screenshot_or_parse_failed', 'fresh_state_invalid'}:
                break
        if state_error:
            return self._stop(state_error)
        assert state is not None
        dialog_error = self._validate_final_dialog_state(state)
        if dialog_error:
            return self._stop(dialog_error)
        control = state.get('final_confirm_control')
        if not self._valid_control(control):
            return self._stop('final_confirm_control_invalid')
        self._tap_control(control, _FINAL_CONFIRMATION_ACTION_ID)
        try:
            post_frame = self.adb.screenshot()
        except Exception:
            return self._stop('post_final_confirmation_screenshot_failed')
        if post_frame is None:
            return self._stop('post_final_confirmation_screenshot_failed')
        return self._stop('batch_final_confirmation_probe_complete' if self.stage == 'batch_final_confirmation_probe' else 'final_confirmation_probe_complete')

    def _run_close_success_probe(self) -> SkillPurchaseExecutionResult:
        if len(self.plan['purchases']) != 1:
            return self._stop('close_success_probe_requires_one_purchase')
        state, state_error = self._fresh_state()
        if state_error:
            return self._stop(state_error)
        assert state is not None
        success_error = self._validate_success_dialog_state(state)
        if success_error:
            return self._stop(success_error)
        self._tap_control(state['close_control'], _CLOSE_SUCCESS_ACTION_ID)
        try:
            post_frame = self.adb.screenshot()
        except Exception:
            return self._stop('post_close_success_screenshot_failed')
        if post_frame is None:
            return self._stop('post_close_success_screenshot_failed')
        return self._stop('close_success_probe_complete')

    def _run_batch_close_success_probe(self) -> SkillPurchaseExecutionResult:
        if len(self.plan['purchases']) < 2:
            return self._stop('batch_close_success_probe_requires_multiple_purchases')
        state, state_error = self._fresh_state()
        if state_error:
            return self._stop(state_error)
        assert state is not None
        success_error = self._validate_batch_success_dialog_state(state)
        if success_error:
            return self._stop(success_error)
        self._tap_control(state['close_control'], _CLOSE_SUCCESS_ACTION_ID)
        try:
            post_frame = self.adb.screenshot()
        except Exception:
            return self._stop('post_batch_close_success_screenshot_failed')
        if post_frame is None:
            return self._stop('post_batch_close_success_screenshot_failed')
        return self._stop('batch_close_success_probe_complete')

    def _run_return_after_purchase_probe(self) -> SkillPurchaseExecutionResult:
        if self.stage == 'return_after_purchase_probe' and len(self.plan['purchases']) != 1:
            return self._stop('return_after_purchase_requires_one_purchase')
        if self.stage == 'batch_return_after_purchase_probe' and len(self.plan['purchases']) < 2:
            return self._stop('batch_return_after_purchase_requires_multiple_purchases')
        state, state_error = self._fresh_state()
        if state_error:
            return self._stop(state_error)
        assert state is not None
        success_error = self._validate_direct_success_state(state)
        if success_error:
            return self._stop(success_error)
        self._tap_control(state['post_purchase_back_control'], _RETURN_AFTER_PURCHASE_ACTION_ID)
        try:
            post_frame = self.adb.screenshot()
        except Exception:
            return self._stop('post_return_after_purchase_screenshot_failed')
        if post_frame is None:
            return self._stop('post_return_after_purchase_screenshot_failed')
        return self._stop('batch_return_after_purchase_probe_complete' if self.stage == 'batch_return_after_purchase_probe' else 'return_after_purchase_probe_complete')

    def preview_select_plus_state(self, state: Mapping[str, Any]) -> SkillPurchaseExecutionResult:
        """Validate one already parsed fresh state without device authority."""
        if self.stage != 'select_plus_probe':
            return self._stop('preview_requires_select_plus_probe_stage')
        plan_error = self._validate_plan()
        if plan_error:
            return self._stop(plan_error)
        purchases = self.plan['purchases']
        if len(purchases) != 1:
            return self._stop('select_plus_probe_requires_one_purchase')
        if not isinstance(state, Mapping):
            return self._stop('fresh_state_invalid')
        binding_error = self._validate_fresh_binding(state)
        if binding_error:
            return self._stop(binding_error)
        evidence_error = self._validate_frame_evidence(state, expect_ui_change=False)
        if evidence_error:
            return self._stop(evidence_error)
        initial_error = self._validate_initial_state(state)
        if initial_error:
            return self._stop(initial_error)
        _row, row_error = self._planned_row(state, purchases[0])
        if row_error:
            return self._stop(row_error)
        return self._stop('select_plus_ready')

    def preview_open_confirmation_state(self, state: Mapping[str, Any]) -> SkillPurchaseExecutionResult:
        """Validate a parsed already-selected state without device authority."""
        if self.stage != 'open_confirmation_probe':
            return self._stop('preview_requires_open_confirmation_probe_stage')
        plan_error = self._validate_plan()
        if plan_error:
            return self._stop(plan_error)
        purchases = self.plan['purchases']
        if len(purchases) != 1:
            return self._stop('open_confirmation_probe_requires_one_purchase')
        if not isinstance(state, Mapping):
            return self._stop('fresh_state_invalid')
        binding_error = self._validate_fresh_binding(state)
        if binding_error:
            return self._stop(binding_error)
        evidence_error = self._validate_frame_evidence(state, expect_ui_change=False)
        if evidence_error:
            return self._stop(evidence_error)
        expected_ids = [str(purchases[0]['item_id'])]
        confirm_error = self._validate_green_confirm_preconditions(state, expected_ids)
        if confirm_error:
            return self._stop(confirm_error)
        if not self._valid_control(state.get('green_confirm_control')):
            return self._stop('green_confirm_control_invalid')
        return self._stop('open_confirmation_ready')

    def preview_multi_confirmation_capture_state(self, state: Mapping[str, Any]) -> SkillPurchaseExecutionResult:
        """Validate a same-frame capture-only start state without a device."""
        if self.stage != 'multi_confirmation_capture_probe':
            return self._stop('preview_requires_multi_confirmation_capture_probe_stage')
        plan_error = self._validate_capture_only_plan()
        if plan_error:
            return self._stop(plan_error)
        if not isinstance(state, Mapping):
            return self._stop('fresh_state_invalid')
        binding_error = self._validate_fresh_binding(state)
        if binding_error:
            return self._stop(binding_error)
        evidence_error = self._validate_frame_evidence(state, expect_ui_change=False)
        if evidence_error:
            return self._stop(evidence_error)
        initial_error = self._validate_initial_state(state)
        if initial_error:
            return self._stop(initial_error)
        initial_controls: set[tuple[int, int]] = set()
        for purchase in self.plan['purchases']:
            row, row_error = self._planned_row(state, purchase)
            if row_error:
                return self._stop(row_error)
            assert row is not None
            control = row['plus_control']
            control_key = (control['x'], control['y'])
            if control_key in initial_controls:
                return self._stop('capture_plus_controls_not_unique')
            initial_controls.add(control_key)
        return self._stop('multi_confirmation_capture_ready')

    def preview_final_confirmation_state(self, state: Mapping[str, Any]) -> SkillPurchaseExecutionResult:
        """Validate one same-frame dialog state without device authority."""
        if self.stage != 'final_confirmation_probe':
            return self._stop('preview_requires_final_confirmation_probe_stage')
        plan_error = self._validate_plan()
        if plan_error:
            return self._stop(plan_error)
        if len(self.plan['purchases']) != 1:
            return self._stop('final_confirmation_probe_requires_one_purchase')
        if not isinstance(state, Mapping):
            return self._stop('fresh_state_invalid')
        binding_error = self._validate_fresh_binding(state)
        if binding_error:
            return self._stop(binding_error)
        evidence_error = self._validate_frame_evidence(state, expect_ui_change=False)
        if evidence_error:
            return self._stop(evidence_error)
        dialog_error = self._validate_final_dialog_state(state)
        if dialog_error:
            return self._stop(dialog_error)
        return self._stop('final_confirmation_ready')

    def preview_batch_final_confirmation_state(self, state: Mapping[str, Any]) -> SkillPurchaseExecutionResult:
        """Validate a closed multi-page dialog without device authority."""
        if self.stage != 'batch_final_confirmation_probe':
            return self._stop('preview_requires_batch_final_confirmation_probe_stage')
        plan_error = self._validate_batch_final_confirmation_plan()
        if plan_error:
            return self._stop(plan_error)
        if len(self.plan['purchases']) < 2:
            return self._stop('batch_final_confirmation_probe_requires_multiple_purchases')
        if not isinstance(state, Mapping):
            return self._stop('fresh_state_invalid')
        binding_error = self._validate_fresh_binding(state)
        if binding_error:
            return self._stop(binding_error)
        evidence_error = self._validate_frame_evidence(state, expect_ui_change=False)
        if evidence_error:
            return self._stop(evidence_error)
        dialog_error = self._validate_final_dialog_state(state)
        if dialog_error:
            return self._stop(dialog_error)
        return self._stop('batch_final_confirmation_ready')

    def preview_batch_close_success_state(self, state: Mapping[str, Any]) -> SkillPurchaseExecutionResult:
        """Validate an exact multi-skill success modal without device authority."""
        if self.stage != 'batch_close_success_probe':
            return self._stop('preview_requires_batch_close_success_probe_stage')
        plan_error = self._validate_batch_final_confirmation_plan()
        if plan_error:
            return self._stop(plan_error)
        if len(self.plan['purchases']) < 2:
            return self._stop('batch_close_success_probe_requires_multiple_purchases')
        if not isinstance(state, Mapping):
            return self._stop('fresh_state_invalid')
        binding_error = self._validate_fresh_binding(state)
        if binding_error:
            return self._stop(binding_error)
        evidence_error = self._validate_frame_evidence(state, expect_ui_change=False)
        if evidence_error:
            return self._stop(evidence_error)
        success_error = self._validate_batch_success_dialog_state(state)
        if success_error:
            return self._stop(success_error)
        return self._stop('batch_close_success_ready')

    def preview_close_success_state(self, state: Mapping[str, Any]) -> SkillPurchaseExecutionResult:
        """Validate the post-purchase success modal without device authority."""
        if self.stage != 'close_success_probe':
            return self._stop('preview_requires_close_success_probe_stage')
        plan_error = self._validate_plan()
        if plan_error:
            return self._stop(plan_error)
        if len(self.plan['purchases']) != 1:
            return self._stop('close_success_probe_requires_one_purchase')
        if not isinstance(state, Mapping):
            return self._stop('fresh_state_invalid')
        binding_error = self._validate_fresh_binding(state)
        if binding_error:
            return self._stop(binding_error)
        evidence_error = self._validate_frame_evidence(state, expect_ui_change=False)
        if evidence_error:
            return self._stop(evidence_error)
        success_error = self._validate_success_dialog_state(state)
        if success_error:
            return self._stop(success_error)
        return self._stop('close_success_ready')

    def preview_return_after_purchase_state(self, state: Mapping[str, Any]) -> SkillPurchaseExecutionResult:
        """Validate a direct post-confirm skill page without device authority."""
        if self.stage != 'return_after_purchase_probe':
            return self._stop('preview_requires_return_after_purchase_stage')
        plan_error = self._validate_plan()
        if plan_error:
            return self._stop(plan_error)
        if len(self.plan['purchases']) != 1:
            return self._stop('return_after_purchase_requires_one_purchase')
        if not isinstance(state, Mapping):
            return self._stop('fresh_state_invalid')
        binding_error = self._validate_fresh_binding(state)
        if binding_error:
            return self._stop(binding_error)
        evidence_error = self._validate_frame_evidence(state, expect_ui_change=False)
        if evidence_error:
            return self._stop(evidence_error)
        success_error = self._validate_direct_success_state(state)
        if success_error:
            return self._stop(success_error)
        return self._stop('return_after_purchase_ready')

    def _validate_final_dialog_state(self, state: Mapping[str, Any]) -> str | None:
        if state.get('page_kind') != 'confirmation_dialog' or state.get('screen_kind') != 'confirmation_dialog':
            return 'dialog_page_kind_mismatch'
        safety_error = self._validate_live_state_safety(state)
        if safety_error:
            return f'dialog_{safety_error}'
        result = self.dialog_verifier.verify(state, self.plan)
        if not result.verified:
            return result.reason
        if not self._valid_control(state.get('final_confirm_control')):
            return 'final_confirm_control_invalid'
        return None

    def _validate_success_dialog_state(self, state: Mapping[str, Any]) -> str | None:
        if state.get('page_kind') != 'skill_purchase_success' or state.get('screen_kind') != 'skill_purchase_success' or state.get('accepted') is not True or (state.get('dialog_title') != '成功获得技能') or (state.get('status_text') != '已获得技能'):
            return 'success_dialog_page_mismatch'
        safety_error = self._validate_live_state_safety(state)
        if safety_error:
            return f'success_dialog_{safety_error}'
        expected_ids = [self.plan['purchases'][0]['item_id']]
        if state.get('confirmed_item_ids') != expected_ids:
            return 'success_dialog_identity_mismatch'
        if not self._valid_control(state.get('close_control')):
            return 'success_dialog_close_control_invalid'
        return None

    def _validate_batch_success_dialog_state(self, state: Mapping[str, Any]) -> str | None:
        if state.get('page_kind') != 'skill_purchase_success' or state.get('screen_kind') != 'skill_purchase_success' or state.get('accepted') is not True or (state.get('dialog_title') != '成功获得技能') or (state.get('status_text') != '已获得技能'):
            return 'batch_success_dialog_page_mismatch'
        safety_error = self._validate_live_state_safety(state)
        if safety_error:
            return f'batch_success_dialog_{safety_error}'
        planned = [str(purchase.get('item_id')) for purchase in self.plan['purchases']]
        confirmed = state.get('confirmed_item_ids')
        auto = state.get('auto_included_item_ids')
        if state.get('planned_item_ids') != planned or not isinstance(confirmed, list) or (not isinstance(auto, list)) or (len(confirmed) != len(planned) + len(auto)) or (len(set(confirmed)) != len(confirmed)) or any((item_id not in confirmed for item_id in planned + auto)) or (state.get('icon_count') != len(confirmed) and (not (self.plan.get('final_authorization', {}).get('confirmation_policy') == 'verified_selection' and state.get('count_source') == 'verified_selection_receipt' and (state.get('icon_count') is None) and (state.get('visible_icon_count') == 5) and (len(planned) > 10)))):
            return 'batch_success_dialog_identity_mismatch'
        if not self._valid_control(state.get('close_control')):
            return 'batch_success_dialog_close_control_invalid'
        return None

    def _validate_direct_success_state(self, state: Mapping[str, Any]) -> str | None:
        if state.get('page_kind') != 'skill_acquisition' or state.get('screen_kind') != 'skill_acquisition' or state.get('final_confirmation_evidence_verified') is not True or (state.get('success_evidence_verified') is not True):
            return 'direct_success_page_mismatch'
        safety_error = self._validate_live_state_safety(state)
        if safety_error:
            return f'direct_success_{safety_error}'
        expected_remaining = self.plan.get('expected_remaining_skill_points')
        report_sha = state.get('final_confirmation_report_sha256')
        success_report_sha = state.get('success_report_sha256')
        if state.get('current_skill_points') != expected_remaining or not isinstance(report_sha, str) or re.fullmatch('[0-9a-f]{64}', report_sha) is None or (not isinstance(success_report_sha, str)) or (re.fullmatch('[0-9a-f]{64}', success_report_sha) is None):
            return 'direct_success_purchase_binding_mismatch'
        if not self._valid_control(state.get('post_purchase_back_control')):
            return 'direct_success_back_control_invalid'
        return None

    def _validate_plan(self) -> str | None:
        if self.plan.get('mode') != 'live' or self.plan.get('execution_allowed') is not True:
            return 'plan_execution_not_allowed'
        return self._validate_plan_structure()

    def _validate_batch_final_confirmation_plan(self) -> str | None:
        if self.plan.get('mode') != 'live':
            return 'plan_execution_not_allowed'
        execution_allowed = self.plan.get('execution_allowed')
        if execution_allowed is True:
            return self._validate_plan_structure()
        if execution_allowed is not False or self.plan.get('multi_skill_live_gate') != 'real_multi_skill_confirmation_and_success_fixture_missing':
            return 'batch_final_confirmation_plan_not_promotable'
        return self._validate_plan_structure()

    def _validate_capture_only_plan(self) -> str | None:
        purchases = self.plan.get('purchases')
        capture = self.plan.get('confirmation_fixture_capture')
        input_data = self.plan.get('input')
        binding_shas = [purchase.get('purchase_binding_sha256') for purchase in purchases] if isinstance(purchases, list) and all((isinstance(purchase, Mapping) for purchase in purchases)) else None
        if self.plan.get('mode') != 'live' or self.plan.get('execution_allowed') is not False or (not isinstance(purchases, list)) or (len(purchases) != 2) or (not isinstance(capture, Mapping)) or (capture.get('allowed') is not True) or (capture.get('gate_reason') != 'same_frame_two_skill_capture_ready') or (capture.get('stage') != 'multi_confirmation_capture_probe') or (capture.get('action_id') != _MULTI_CONFIRMATION_CAPTURE_ACTION_ID) or (capture.get('required_purchase_count') != len(purchases)) or (capture.get('visibility_scope') != 'single_fresh_frame_only') or (capture.get('terminal_state') != 'confirmation_dialog_screenshot_before_final_confirm') or (capture.get('final_confirmation_allowed') is not False) or (capture.get('terminal_handoff_required') is not True) or (not isinstance(input_data, Mapping)) or (capture.get('source_report_sha256') != input_data.get('source_report_sha256')) or (capture.get('purchase_binding_sha256') != binding_shas) or (not all((isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) for value in binding_shas or []))):
            return 'capture_only_contract_invalid'
        assert isinstance(purchases, list)
        assert isinstance(input_data, Mapping)
        expected_source_sha = input_data.get('source_report_sha256')
        source_frames: set[tuple[str, int]] = set()
        source_bboxes: list[list[int]] = []
        for purchase in purchases:
            binding = purchase.get('purchase_binding')
            expected_ocr = purchase.get('must_satisfy_ocr')
            source_binding = binding.get('source_binding') if isinstance(binding, Mapping) else None
            bbox = source_binding.get('row_bbox') if isinstance(source_binding, Mapping) else None
            if purchase.get('name_binding_source') != 'direct_parser_name' or purchase.get('source_report_sha256') != expected_source_sha or (not isinstance(binding, Mapping)) or (not isinstance(expected_ocr, Mapping)) or (self._canonical_sha(binding) != purchase.get('purchase_binding_sha256')) or (binding.get('item_id') != purchase.get('item_id')) or (binding.get('catalog_name') != purchase.get('name')) or (binding.get('observed_name') != expected_ocr.get('observed_name')) or (binding.get('name_binding_source') != 'direct_parser_name') or (binding.get('source_row_fingerprint') != purchase.get('source_row_fingerprint')) or (binding.get('source_report_sha256') != expected_source_sha) or (binding.get('skill_type') != purchase.get('type')) or (binding.get('rarity') != purchase.get('rarity')) or (binding.get('observed_final_cost') != purchase.get('observed_final_cost')) or (binding.get('expected_selected_total_after') != purchase.get('expected_selected_total_after')) or (binding.get('expected_skill_points_after') != purchase.get('expected_skill_points_after')) or (not isinstance(binding.get('description_sha256'), str)) or (not re.fullmatch('[0-9a-f]{64}', binding['description_sha256'])) or (not isinstance(source_binding, Mapping)) or (not isinstance(source_binding.get('source_page'), str)) or (not source_binding['source_page']) or (not self._is_int(source_binding.get('page_index'))) or (not self._valid_bbox(bbox)):
                return 'capture_only_contract_invalid'
            source_frames.add((source_binding['source_page'], source_binding['page_index']))
            source_bboxes.append(list(bbox))
        if len(source_frames) != 1 or any((self._bboxes_overlap(left, right) for index, left in enumerate(source_bboxes) for right in source_bboxes[index + 1:])):
            return 'capture_only_contract_invalid'
        return self._validate_plan_structure()

    def _validate_plan_structure(self) -> str | None:
        policy = self.plan.get('policy')
        if not isinstance(policy, Mapping):
            return 'plan_policy_missing'
        if policy.get('currency_scope') != 'skill_points_only':
            return 'currency_scope_not_skill_points_only'
        if policy.get('allow_fallback_skill') is not False:
            return 'fallback_skill_policy_not_disabled'
        if policy.get('allow_partial_confirmation') is not False:
            return 'partial_confirmation_policy_not_disabled'
        if policy.get('dangerous_resource_flags') != []:
            return 'dangerous_resource_policy_present'
        purchases = self.plan.get('purchases')
        preflight = self.plan.get('preflight_must_satisfy_ocr')
        input_data = self.plan.get('input')
        promotion = self.plan.get('execution_promotion')
        if not isinstance(purchases, list) or not purchases:
            return 'planned_purchases_missing'
        if not all((isinstance(item, Mapping) for item in purchases)):
            return 'planned_purchase_invalid'
        if not isinstance(preflight, Mapping) or not isinstance(input_data, Mapping):
            return 'plan_preflight_missing'
        if not isinstance(promotion, Mapping) or promotion.get('promoted_by') != 'U1':
            return 'u1_promotion_missing'
        if promotion.get('plan_id') != self.plan.get('plan_id'):
            return 'promotion_plan_id_mismatch'
        if promotion.get('sample_id') != input_data.get('sample_id'):
            return 'promotion_sample_id_mismatch'
        if not promotion.get('session_id') or not promotion.get('fresh_capture_id'):
            return 'promotion_fresh_binding_missing'
        if promotion.get('current_skill_points') != input_data.get('current_skill_points'):
            return 'promotion_current_skill_points_mismatch'
        if promotion.get('planned_selection_count') != len(purchases):
            return 'promotion_planned_count_mismatch'
        if preflight.get('planned_selection_count') != len(purchases):
            return 'preflight_planned_count_mismatch'
        if preflight.get('current_skill_points') != input_data.get('current_skill_points'):
            return 'preflight_current_skill_points_mismatch'
        economics_error = self._validate_plan_economics(purchases, input_data)
        if economics_error:
            return economics_error
        for purchase in purchases:
            ocr_error = self._validate_ocr_contract(purchase)
            if ocr_error:
                return ocr_error
        return None

    def _validate_plan_economics(self, purchases: list[Mapping[str, Any]], input_data: Mapping[str, Any]) -> str | None:
        initial_points = input_data.get('current_skill_points')
        if not self._is_int(initial_points):
            return 'plan_economics_inconsistent'
        cumulative_cost = 0
        for purchase in purchases:
            cost = purchase.get('observed_final_cost')
            if not self._is_int(cost):
                return 'plan_economics_inconsistent'
            cumulative_cost += cost
            if purchase.get('expected_selected_total_after') != cumulative_cost:
                return 'plan_economics_inconsistent'
            if purchase.get('expected_skill_points_after') != initial_points - cumulative_cost:
                return 'plan_economics_inconsistent'
        if self.plan.get('spent_skill_points') != cumulative_cost:
            return 'plan_economics_inconsistent'
        if self.plan.get('expected_remaining_skill_points') != initial_points - cumulative_cost:
            return 'plan_economics_inconsistent'
        return None

    def _validate_ocr_contract(self, purchase: Mapping[str, Any]) -> str | None:
        item_id = purchase.get('item_id')
        expected = purchase.get('must_satisfy_ocr')
        if not isinstance(expected, Mapping):
            return f'planned_ocr_contract_missing:{item_id}:must_satisfy_ocr'
        for field in _REQUIRED_OCR_CONTRACT_FIELDS:
            if field not in expected:
                return f'planned_ocr_contract_missing:{item_id}:{field}'
        if expected['page_kind'] != 'skill_acquisition':
            return f'planned_ocr_contract_invalid:{item_id}:page_kind'
        if expected['screen_kind'] != 'skill_acquisition':
            return f'planned_ocr_contract_invalid:{item_id}:screen_kind'
        if expected['row_complete'] is not True:
            return f'planned_ocr_contract_invalid:{item_id}:row_complete'
        if expected['name_resolution_status'] != 'auto_confirmed':
            return f'planned_ocr_contract_invalid:{item_id}:name_resolution_status'
        if expected['plus_available'] is not True:
            return f'planned_ocr_contract_invalid:{item_id}:plus_available'
        if not isinstance(expected['observed_name'], str) or not expected['observed_name']:
            return f'planned_ocr_contract_invalid:{item_id}:observed_name'
        if not self._is_int(expected['observed_final_cost']):
            return f'planned_ocr_contract_invalid:{item_id}:observed_final_cost'
        if expected['observed_final_cost'] != purchase.get('observed_final_cost'):
            return f'planned_ocr_economics_mismatch:{item_id}:observed_final_cost'
        for purchase_field, expected_field in (('type', 'skill_type'), ('rarity', 'rarity_or_icon_type')):
            value = purchase.get(purchase_field)
            if not isinstance(value, str) or not value:
                return f'planned_row_identity_missing:{item_id}:{purchase_field}'
            if expected[expected_field] != value:
                return f'planned_row_identity_mismatch:{item_id}:{expected_field}'
        for field in ('name_resolution_confidence_min', 'cost_confidence_min', 'hint_confidence_min'):
            if not self._is_confidence(expected[field]):
                return f'planned_ocr_contract_invalid:{item_id}:{field}'
        hint = purchase.get('hint')
        if not isinstance(hint, Mapping):
            return f'planned_hint_declaration_missing:{item_id}'
        for field in ('level', 'discount_percent'):
            if field not in hint:
                return f'planned_hint_declaration_missing:{item_id}:{field}'
        if not self._is_hint_level(hint['level']):
            return f'planned_hint_declaration_invalid:{item_id}:level'
        if not self._is_discount_percent(hint['discount_percent']):
            return f'planned_hint_declaration_invalid:{item_id}:discount_percent'
        if expected['hint_level'] != hint['level']:
            return f'planned_hint_binding_mismatch:{item_id}:level'
        if expected['discount_percent'] != hint['discount_percent']:
            return f'planned_hint_binding_mismatch:{item_id}:discount_percent'
        return None

    def _fresh_state(self, *, expect_ui_change: bool=False) -> tuple[Mapping[str, Any] | None, str | None]:
        try:
            screenshot = self.adb.screenshot()
            state = self.parse_fresh_state(screenshot)
        except Exception:
            return (None, 'fresh_screenshot_or_parse_failed')
        if not isinstance(state, Mapping):
            return (None, 'fresh_state_invalid')
        binding_error = self._validate_fresh_binding(state)
        if binding_error:
            return (None, binding_error)
        evidence_error = self._validate_frame_evidence(state, expect_ui_change=expect_ui_change)
        if evidence_error:
            return (None, evidence_error)
        return (state, None)

    def _validate_initial_state(self, state: Mapping[str, Any]) -> str | None:
        binding_error = self._validate_fresh_binding(state)
        if binding_error:
            return binding_error
        page_error = self._validate_skill_acquisition_page(state, phase='fresh')
        if page_error:
            return page_error
        preflight = self.plan['preflight_must_satisfy_ocr']
        if state.get('page_kind') != preflight.get('page_kind'):
            return 'fresh_page_kind_mismatch'
        if state.get('current_skill_points') != preflight.get('current_skill_points'):
            return 'fresh_current_skill_points_mismatch'
        if not self._at_least(state.get('current_skill_points_confidence'), preflight.get('current_skill_points_confidence_min')):
            return 'fresh_current_skill_points_confidence_low'
        safety_error = self._validate_live_state_safety(state)
        if safety_error:
            return safety_error
        return self._validate_selection_state(state, [], 0, self.plan['input']['current_skill_points'])

    def _validate_fresh_binding(self, state: Mapping[str, Any]) -> str | None:
        promotion = self.plan['execution_promotion']
        if state.get('fresh') is not True:
            return 'fresh_capture_required'
        if state.get('plan_id') != self.plan.get('plan_id'):
            return 'fresh_plan_id_mismatch'
        if state.get('sample_id') != promotion.get('sample_id'):
            return 'fresh_sample_id_mismatch'
        if state.get('session_id') != promotion.get('session_id'):
            return 'fresh_session_mismatch'
        if state.get('capture_batch_id') != promotion.get('fresh_capture_id'):
            return 'fresh_capture_batch_mismatch'
        return None

    def _validate_frame_evidence(self, state: Mapping[str, Any], *, expect_ui_change: bool) -> str | None:
        frame_capture_id = state.get('frame_capture_id')
        if not isinstance(frame_capture_id, str) or not frame_capture_id:
            return 'frame_capture_id_missing'
        if frame_capture_id in self._seen_frame_capture_ids:
            return 'frame_capture_id_replayed'
        frame_sequence = state.get('frame_sequence')
        if not self._is_int(frame_sequence):
            return 'frame_sequence_invalid'
        if self._last_frame_sequence is not None and frame_sequence <= self._last_frame_sequence:
            return 'frame_sequence_not_monotonic'
        frame_sha256 = state.get('frame_sha256')
        if not isinstance(frame_sha256, str) or not re.fullmatch('[0-9a-f]{64}', frame_sha256):
            return 'frame_sha256_invalid'
        if frame_sha256 in self._seen_frame_sha256:
            return 'frame_sha256_replayed'
        self._seen_frame_capture_ids.add(frame_capture_id)
        self._seen_frame_sha256.add(frame_sha256)
        self._last_frame_sequence = frame_sequence
        self._last_frame_sha256 = frame_sha256
        return None

    def _planned_row(self, state: Mapping[str, Any], purchase: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, str | None]:
        expected = purchase.get('must_satisfy_ocr')
        if not isinstance(expected, Mapping):
            return (None, f"planned_row_ocr_missing:{purchase.get('item_id')}")
        item_id = str(purchase.get('item_id'))
        if state.get('screen_kind') != 'skill_acquisition':
            return (None, f'planned_row_safety_violation:{item_id}:screen_kind')
        rows = state.get('rows')
        if not isinstance(rows, list):
            return (None, 'fresh_rows_missing')
        expected_name = expected.get('observed_name')
        same_name = [row for row in rows if isinstance(row, Mapping) and row.get('item_id') == purchase.get('item_id') and (name_similarity(row.get('observed_name'), expected_name) >= 0.6)]
        if not same_name:
            return (None, f"planned_row_missing:{purchase.get('item_id')}")
        if len(same_name) != 1:
            return (None, f"planned_row_name_not_unique:{purchase.get('item_id')}")
        row = same_name[0]
        for field in _REQUIRED_LIVE_ROW_FIELDS:
            if field not in row:
                return (None, f'planned_row_ocr_missing:{item_id}:{field}')
        if state.get('page_kind') != expected['page_kind']:
            return (None, f"planned_row_ocr_mismatch:{purchase.get('item_id')}:page_kind")
        if state.get('screen_kind') != expected['screen_kind']:
            return (None, f"planned_row_ocr_mismatch:{purchase.get('item_id')}:screen_kind")
        for field in ('page_kind', 'screen_kind'):
            if row[field] != state[field] or row[field] != expected[field]:
                return (None, f"planned_row_ocr_mismatch:{purchase.get('item_id')}:{field}")
        if row.get('item_id') != purchase.get('item_id'):
            return (None, f"planned_row_item_id_mismatch:{purchase.get('item_id')}")
        for field, required_value in (('row_complete', True), ('name_resolution_status', 'auto_confirmed'), ('plus_available', True)):
            if row[field] != required_value:
                return (None, f'planned_row_safety_violation:{item_id}:{field}')
        for field in ('name_resolution_confidence', 'cost_confidence', 'hint_confidence'):
            if not self._is_confidence(row[field]):
                return (None, f'planned_row_ocr_invalid:{item_id}:{field}')
        if not self._is_hint_level(row['hint_level']):
            return (None, f'planned_row_ocr_invalid:{item_id}:hint_level')
        if not self._is_discount_percent(row['discount_percent']):
            return (None, f'planned_row_ocr_invalid:{item_id}:discount_percent')
        exact_fields = ('row_complete', 'name_resolution_status', 'observed_name', 'observed_final_cost', 'plus_available', 'discount_percent', 'skill_type', 'rarity_or_icon_type')
        for field in exact_fields:
            if name_similarity(row[field], expected[field]) < 0.6 if field == 'observed_name' else row[field] != expected[field]:
                return (None, f"planned_row_ocr_mismatch:{purchase.get('item_id')}:{field}")
        if row['hint_level'] is not None and expected['hint_level'] is not None and (row['hint_level'] != expected['hint_level']):
            return (None, f"planned_row_ocr_mismatch:{purchase.get('item_id')}:hint_level")
        minimum_fields = (('name_resolution_confidence', 'name_resolution_confidence_min'), ('cost_confidence', 'cost_confidence_min'), ('hint_confidence', 'hint_confidence_min'))
        for actual, minimum in minimum_fields:
            if not self._at_least(row[actual], expected[minimum]):
                return (None, f"planned_row_ocr_mismatch:{purchase.get('item_id')}:{actual}")
        if not self._valid_control(row.get('plus_control')):
            return (None, f"planned_plus_control_invalid:{purchase.get('item_id')}")
        return (row, None)

    def _validate_selection_after_purchase(self, state: Mapping[str, Any], purchase: Mapping[str, Any], selected_ids: list[str]) -> str | None:
        binding_error = self._validate_fresh_binding(state)
        if binding_error:
            return binding_error
        page_error = self._validate_skill_acquisition_page(state, phase='selection')
        if page_error:
            return page_error
        safety_error = self._validate_live_state_safety(state)
        if safety_error:
            return safety_error
        expected_total = purchase.get('expected_selected_total_after')
        expected_remaining = purchase.get('expected_skill_points_after')
        return self._validate_selection_state(state, selected_ids, expected_total, expected_remaining, purchase_id=str(purchase.get('item_id')))

    def _validate_green_confirm_preconditions(self, state: Mapping[str, Any], selected_ids: list[str]) -> str | None:
        binding_error = self._validate_fresh_binding(state)
        if binding_error:
            return binding_error
        page_error = self._validate_skill_acquisition_page(state, phase='green_confirm')
        if page_error:
            return page_error
        safety_error = self._validate_live_state_safety(state)
        if safety_error:
            return safety_error
        purchases = self.plan['purchases']
        last = purchases[-1]
        return self._validate_selection_state(state, selected_ids, last.get('expected_selected_total_after'), last.get('expected_skill_points_after'), purchase_id='all_planned_rows')

    def _validate_selection_state(self, state: Mapping[str, Any], expected_ids: list[str], expected_total: Any, expected_remaining: Any, *, purchase_id: str='initial') -> str | None:
        selected = state.get('selected_item_ids')
        if not isinstance(selected, list) or not all((isinstance(item, str) for item in selected)):
            return 'selected_items_unreadable'
        planned_ids = {str(item['item_id']) for item in self.plan['purchases']}
        if any((item not in planned_ids for item in selected)):
            return 'unexpected_selected_items'
        if selected != expected_ids:
            return f'planned_selection_mismatch:{purchase_id}'
        if state.get('selected_total') != expected_total:
            return f'selected_total_mismatch:{purchase_id}'
        semantics = state.get('remaining_points_semantics')
        if semantics not in _ALLOWED_REMAINING_SEMANTICS:
            return 'remaining_points_semantics_ambiguous'
        if self._remaining_semantics is None:
            self._remaining_semantics = str(semantics)
        elif semantics != self._remaining_semantics:
            return 'remaining_points_semantics_changed'
        required_remaining = expected_remaining if semantics == 'post_selection' else self.plan['input']['current_skill_points']
        if state.get('remaining_skill_points') != required_remaining:
            return f'remaining_skill_points_mismatch:{purchase_id}'
        return None

    @staticmethod
    def _validate_skill_acquisition_page(state: Mapping[str, Any], *, phase: str) -> str | None:
        if 'page_kind' not in state:
            return f'{phase}_page_kind_missing'
        if 'screen_kind' not in state:
            return f'{phase}_screen_kind_missing'
        page_kind = state['page_kind']
        screen_kind = state['screen_kind']
        if page_kind != screen_kind:
            return f'{phase}_page_screen_kind_conflict'
        if page_kind != 'skill_acquisition':
            return f'{phase}_page_kind_mismatch'
        return None

    @staticmethod
    def _validate_live_state_safety(state: Mapping[str, Any]) -> str | None:
        if state.get('currency_scope') != 'skill_points_only':
            return 'fresh_currency_scope_mismatch'
        if state.get('observed_currency') != 'skill_points':
            return 'fresh_observed_currency_mismatch'
        flags = state.get('dangerous_resource_flags')
        if not isinstance(flags, list):
            return 'dangerous_resource_flags_unreadable'
        if flags:
            return 'dangerous_resource_signal_detected'
        return None

    def _tap_control(self, control: Mapping[str, Any], action: str) -> None:
        self.adb.tap(control['x'], control['y'], scaled=False)
        self._actions.append(action)

    def _stop(self, reason: str) -> SkillPurchaseExecutionResult:
        return SkillPurchaseExecutionResult(reason, tuple(self._actions))

    @staticmethod
    def _at_least(value: Any, minimum: Any) -> bool:
        try:
            return float(value) >= float(minimum)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _is_int(value: Any) -> bool:
        return isinstance(value, int) and (not isinstance(value, bool))

    @staticmethod
    def _is_confidence(value: Any) -> bool:
        return isinstance(value, (int, float)) and (not isinstance(value, bool)) and (0.0 <= float(value) <= 1.0)

    @classmethod
    def _is_hint_level(cls, value: Any) -> bool:
        return value is None or (cls._is_int(value) and 0 <= value <= 5)

    @classmethod
    def _is_discount_percent(cls, value: Any) -> bool:
        return value is None or (cls._is_int(value) and 0 <= value <= 100)

    @staticmethod
    def _valid_control(control: Any) -> bool:
        return isinstance(control, Mapping) and isinstance(control.get('x'), int) and (not isinstance(control.get('x'), bool)) and isinstance(control.get('y'), int) and (not isinstance(control.get('y'), bool))

    @staticmethod
    def _valid_bbox(value: Any) -> bool:
        return isinstance(value, list) and len(value) == 4 and all((isinstance(item, int) and (not isinstance(item, bool)) for item in value)) and (value[2] > value[0]) and (value[3] > value[1])

    @staticmethod
    def _bboxes_overlap(left: list[int], right: list[int]) -> bool:
        return min(left[2], right[2]) > max(left[0], right[0]) and min(left[3], right[3]) > max(left[1], right[1])

    @staticmethod
    def _canonical_sha(value: Any) -> str:
        payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        return hashlib.sha256(payload).hexdigest()
__all__ = ['ConfirmationDialogVerifier', 'DialogVerification', 'MissingConfirmationDialogVerifier', 'SKILL_MULTI_CONFIRMATION_CAPTURE_TOKEN', 'SKILL_PURCHASE_SAFETY_TOKEN', 'SkillPurchaseExecutionResult', 'SkillPurchaseExecutor', 'SkillPurchaseGateResult', 'validate_skill_purchase_gate', 'validate_multi_confirmation_capture_gate']
