"""Pure, fail-closed derivation for a final-confirmation purchase child plan."""
from __future__ import annotations
from collections.abc import Mapping
from copy import deepcopy
from typing import Any
FINAL_AUTHORIZATION_SCHEMA = 'uma.skill_final_authorization.v1'
FINAL_CONFIRMATION_EVIDENCE_SCHEMA = 'uma.skill_final_confirmation_evidence.v1'
FINAL_CONFIRMATION_GATE = 'current_run_confirmation_closed'
_MAX_DIALOG_FRAME_AGE_MS = 30000

def derive_final_authorized_child(*, parent_plan: Mapping[str, Any], review: Mapping[str, Any], checkpoint: Mapping[str, Any], ledger: Mapping[str, Any], final_evidence: Mapping[str, Any], parent_plan_sha256: str, final_evidence_sha256: str, promotion_review_sha256: str, promotion_evidence_sha256: str, selection_checkpoint_sha256: str, selection_ledger_sha256: str, selection_ledger_frame_sha256: str, top_frame_sha256: str, current_frame_sha256: str, now_unix_ms: int) -> tuple[dict[str, Any] | None, str | None]:
    """Return the only permitted authorized child, or a precise rejection."""
    evidence_error = _validate_final_evidence(parent_plan=parent_plan, review=review, checkpoint=checkpoint, ledger=ledger, final_evidence=final_evidence, parent_plan_sha256=parent_plan_sha256, promotion_review_sha256=promotion_review_sha256, promotion_evidence_sha256=promotion_evidence_sha256, selection_checkpoint_sha256=selection_checkpoint_sha256, selection_ledger_sha256=selection_ledger_sha256, selection_ledger_frame_sha256=selection_ledger_frame_sha256, top_frame_sha256=top_frame_sha256, current_frame_sha256=current_frame_sha256, now_unix_ms=now_unix_ms)
    if evidence_error:
        return (None, evidence_error)
    if not _is_sha256(final_evidence_sha256):
        return (None, 'final_evidence_sha256_invalid')
    parent_error = _validate_parent_plan(parent_plan)
    if parent_error:
        return (None, parent_error)
    child = deepcopy(dict(parent_plan))
    child['execution_allowed'] = True
    child['multi_skill_live_gate'] = FINAL_CONFIRMATION_GATE
    child['final_authorization'] = {'schema_version': FINAL_AUTHORIZATION_SCHEMA, 'parent_plan_sha256': parent_plan_sha256, 'final_evidence_sha256': final_evidence_sha256}
    if final_evidence['dialog'].get('confirmation_policy') == 'verified_selection':
        child['final_authorization']['confirmation_policy'] = 'verified_selection'
    return (child, None)

def validate_final_authorized_child(*, child_plan: Mapping[str, Any], parent_plan: Mapping[str, Any], review: Mapping[str, Any], checkpoint: Mapping[str, Any], ledger: Mapping[str, Any], final_evidence: Mapping[str, Any], parent_plan_sha256: str, final_evidence_sha256: str, promotion_review_sha256: str, promotion_evidence_sha256: str, selection_checkpoint_sha256: str, selection_ledger_sha256: str, selection_ledger_frame_sha256: str, top_frame_sha256: str, current_frame_sha256: str, now_unix_ms: int) -> str | None:
    """Validate the parent, evidence, and child as one non-circular chain."""
    evidence_error = _validate_final_evidence(parent_plan=parent_plan, review=review, checkpoint=checkpoint, ledger=ledger, final_evidence=final_evidence, parent_plan_sha256=parent_plan_sha256, promotion_review_sha256=promotion_review_sha256, promotion_evidence_sha256=promotion_evidence_sha256, selection_checkpoint_sha256=selection_checkpoint_sha256, selection_ledger_sha256=selection_ledger_sha256, selection_ledger_frame_sha256=selection_ledger_frame_sha256, top_frame_sha256=top_frame_sha256, current_frame_sha256=current_frame_sha256, now_unix_ms=now_unix_ms)
    if evidence_error:
        return evidence_error
    if not _is_sha256(final_evidence_sha256):
        return 'final_evidence_sha256_invalid'
    parent_error = _validate_parent_plan(parent_plan)
    if parent_error:
        return parent_error
    if not isinstance(child_plan, Mapping):
        return 'child_plan_invalid'
    expected = deepcopy(dict(parent_plan))
    expected['execution_allowed'] = True
    expected['multi_skill_live_gate'] = FINAL_CONFIRMATION_GATE
    expected['final_authorization'] = {'schema_version': FINAL_AUTHORIZATION_SCHEMA, 'parent_plan_sha256': parent_plan_sha256, 'final_evidence_sha256': final_evidence_sha256}
    if final_evidence['dialog'].get('confirmation_policy') == 'verified_selection':
        expected['final_authorization']['confirmation_policy'] = 'verified_selection'
    if dict(child_plan) != expected:
        return 'child_plan_unauthorized_diff'
    return None

def _validate_parent_plan(parent_plan: Mapping[str, Any]) -> str | None:
    fixture = parent_plan.get('confirmation_fixture_capture')
    purchases = parent_plan.get('purchases')
    supported_single_purchase = parent_plan.get('execution_allowed') is True and parent_plan.get('multi_skill_live_gate') == 'single_skill_fixture_supported' and isinstance(purchases, list) and (len(purchases) == 1)
    capture_ready_pair = isinstance(fixture, Mapping) and fixture.get('allowed') is True and (fixture.get('gate_reason') == 'same_frame_two_skill_capture_ready') and (fixture.get('required_purchase_count') == 2) and isinstance(purchases, list) and (len(purchases) == 2)
    if parent_plan.get('schema_version') != 'uma.skill_purchase_plan.v1' or parent_plan.get('mode') != 'live' or (parent_plan.get('execution_allowed') is not False and (not supported_single_purchase)) or (not isinstance(fixture, Mapping)) or (fixture.get('allowed') is not False and (not capture_ready_pair)) or (fixture.get('final_confirmation_allowed') is not False):
        return 'parent_plan_not_eligible'
    return None

def _validate_final_evidence(*, parent_plan: Mapping[str, Any], review: Mapping[str, Any], checkpoint: Mapping[str, Any], ledger: Mapping[str, Any], final_evidence: Mapping[str, Any], parent_plan_sha256: str, promotion_review_sha256: str, promotion_evidence_sha256: str, selection_checkpoint_sha256: str, selection_ledger_sha256: str, selection_ledger_frame_sha256: str, top_frame_sha256: str, current_frame_sha256: str, now_unix_ms: int) -> str | None:
    expected_evidence_keys = {'schema_version', 'parent_plan_sha256', 'promotion_review_sha256', 'promotion_evidence_sha256', 'selection_checkpoint_sha256', 'selection_ledger_sha256', 'session_id', 'capture_binding', 'dialog'}
    if not isinstance(final_evidence, Mapping) or set(final_evidence) != expected_evidence_keys or final_evidence.get('schema_version') != FINAL_CONFIRMATION_EVIDENCE_SCHEMA:
        return 'final_evidence_invalid'
    supplied_hashes = (parent_plan_sha256, promotion_review_sha256, promotion_evidence_sha256, selection_checkpoint_sha256, selection_ledger_sha256, selection_ledger_frame_sha256, top_frame_sha256, current_frame_sha256)
    if not all((_is_sha256(value) for value in supplied_hashes)):
        return 'final_evidence_sha256_invalid'
    if any((final_evidence.get(key) != value for key, value in (('parent_plan_sha256', parent_plan_sha256), ('promotion_review_sha256', promotion_review_sha256), ('promotion_evidence_sha256', promotion_evidence_sha256), ('selection_checkpoint_sha256', selection_checkpoint_sha256), ('selection_ledger_sha256', selection_ledger_sha256)))):
        return 'final_evidence_artifact_binding_mismatch'
    promotion = parent_plan.get('execution_promotion')
    capture_binding = review.get('capture_binding')
    if not isinstance(promotion, Mapping) or not isinstance(capture_binding, Mapping) or (not isinstance(final_evidence.get('session_id'), str)) or (final_evidence['session_id'] != promotion.get('session_id')) or (review.get('session_id') != promotion.get('session_id')) or (final_evidence.get('capture_binding') != dict(capture_binding)):
        return 'final_evidence_session_or_capture_binding_mismatch'
    capture_error = _validate_capture_timing(capture_binding, now_unix_ms)
    if capture_error:
        return capture_error
    purchases = parent_plan.get('purchases')
    if not isinstance(purchases, list) or not purchases:
        return 'parent_plan_purchases_invalid'
    purchase_ids: list[str] = []
    purchase_costs: list[int] = []
    for purchase in purchases:
        if not isinstance(purchase, Mapping):
            return 'parent_plan_purchases_invalid'
        item_id = purchase.get('item_id')
        cost = purchase.get('observed_final_cost')
        if not isinstance(item_id, str) or not item_id or type(cost) is not int or (cost <= 0):
            return 'parent_plan_purchases_invalid'
        purchase_ids.append(item_id)
        purchase_costs.append(cost)
    if len(purchase_ids) != len(set(purchase_ids)):
        return 'parent_plan_purchases_invalid'
    checkpoint_error = validate_completed_selection(checkpoint, purchase_ids)
    if checkpoint_error:
        return checkpoint_error
    dialog_error = _validate_dialog(final_evidence.get('dialog'), purchase_ids, capture_binding, top_frame_sha256, current_frame_sha256, now_unix_ms)
    if dialog_error:
        return dialog_error
    ledger_error = _validate_ledger(ledger, parent_plan, purchase_ids, purchase_costs, capture_binding, selection_ledger_frame_sha256, final_evidence['dialog'], now_unix_ms)
    if ledger_error:
        return ledger_error
    return None

def _validate_capture_timing(capture_binding: Mapping[str, Any], now_unix_ms: int) -> str | None:
    captured_at = capture_binding.get('captured_at_unix_ms')
    expires_at = capture_binding.get('expires_at_unix_ms')
    if capture_binding.get('schema_version') != 'uma.skill_capture_binding.v1' or not _is_sha256(capture_binding.get('capture_sha256')) or type(captured_at) is not int or (type(expires_at) is not int) or (expires_at <= captured_at) or (type(now_unix_ms) is not int):
        return 'final_evidence_capture_binding_invalid'
    if now_unix_ms > expires_at:
        return 'final_evidence_capture_expired'
    return None

def validate_completed_selection(checkpoint: Mapping[str, Any], purchase_ids: list[str]) -> str | None:
    selected = checkpoint.get('selected_action_ids')
    if checkpoint.get('schema_version') != 'uma.skill_batch_selection_checkpoint.v1' or checkpoint.get('status') != 'page_verified' or (not isinstance(selected, list)) or any((not isinstance(item, str) for item in selected)) or (len(selected) != len(purchase_ids)) or (len(set(selected)) != len(selected)) or (set(selected) != set(purchase_ids)) or (checkpoint.get('pending_group_ids') != []) or (checkpoint.get('remaining_action_ids') != []):
        return 'selection_checkpoint_binding_mismatch'
    return None

def _validate_ledger(ledger: Mapping[str, Any], parent_plan: Mapping[str, Any], purchase_ids: list[str], purchase_costs: list[int], capture_binding: Mapping[str, Any], selection_ledger_frame_sha256: str, dialog: Mapping[str, Any], now_unix_ms: int) -> str | None:
    expected_keys = {'schema_version', 'session_id', 'frame_sha256', 'captured_at_unix_ms', 'selected_action_ids', 'rows', 'selected_total', 'remaining_skill_points'}
    if not isinstance(ledger, Mapping) or set(ledger) != expected_keys or ledger.get('schema_version') != 'uma.skill_verified_selection_ledger.v1' or (ledger.get('session_id') != parent_plan.get('execution_promotion', {}).get('session_id')) or (not _is_sha256(ledger.get('frame_sha256'))) or (type(ledger.get('captured_at_unix_ms')) is not int) or (ledger.get('selected_action_ids') != purchase_ids):
        return 'selection_ledger_invalid'
    if ledger.get('frame_sha256') != selection_ledger_frame_sha256:
        return 'selection_ledger_frame_sha256_mismatch'
    captured_at = capture_binding['captured_at_unix_ms']
    expires_at = capture_binding['expires_at_unix_ms']
    ledger_at = ledger['captured_at_unix_ms']
    top_at = dialog['top_captured_at_unix_ms']
    current_at = dialog['current_captured_at_unix_ms']
    if not captured_at <= ledger_at <= top_at <= current_at <= expires_at:
        return 'selection_ledger_timing_invalid'
    rows = ledger.get('rows')
    expected_rows = [{'item_id': item_id, 'observed_final_cost': cost} for item_id, cost in zip(purchase_ids, purchase_costs)]
    if rows != expected_rows:
        return 'selection_ledger_rows_mismatch'
    initial = parent_plan.get('input', {}).get('current_skill_points')
    spent = parent_plan.get('spent_skill_points')
    remaining = parent_plan.get('expected_remaining_skill_points')
    if type(initial) is not int or type(spent) is not int or type(remaining) is not int or (sum(purchase_costs) != spent) or (initial - spent != remaining) or (ledger.get('selected_total') != spent) or (ledger.get('remaining_skill_points') != remaining):
        return 'selection_ledger_economics_mismatch'
    return None

def _validate_dialog(dialog: Any, purchase_ids: list[str], capture_binding: Mapping[str, Any], top_frame_sha256: str, current_frame_sha256: str, now_unix_ms: int) -> str | None:
    expected_keys = {'top_frame_sha256', 'top_captured_at_unix_ms', 'current_frame_sha256', 'current_captured_at_unix_ms', 'layout_closed', 'row_count', 'rows'}
    selection_verified = isinstance(dialog, Mapping) and dialog.get('confirmation_policy') == 'verified_selection'
    if selection_verified:
        expected_keys.add('confirmation_policy')
    if not isinstance(dialog, Mapping) or set(dialog) != expected_keys:
        return 'final_evidence_dialog_invalid'
    if dialog.get('top_frame_sha256') != top_frame_sha256 or dialog.get('current_frame_sha256') != current_frame_sha256:
        return 'final_evidence_current_frame_sha256_mismatch'
    rows = dialog.get('rows')
    expected_rows = [{'item_id': item_id} for item_id in purchase_ids]
    if selection_verified:
        if dialog.get('layout_closed') is not False or dialog.get('row_count') != 0 or rows != [] or (top_frame_sha256 != current_frame_sha256):
            return 'final_evidence_dialog_policy_mismatch'
    elif dialog.get('layout_closed') is not True or dialog.get('row_count') != len(purchase_ids) or rows != expected_rows:
        return 'final_evidence_dialog_rows_mismatch'
    top_at = dialog.get('top_captured_at_unix_ms')
    current_at = dialog.get('current_captured_at_unix_ms')
    captured_at = capture_binding['captured_at_unix_ms']
    expires_at = capture_binding['expires_at_unix_ms']
    if type(top_at) is not int or type(current_at) is not int or (not captured_at <= top_at <= current_at <= expires_at) or (current_at > now_unix_ms) or (now_unix_ms - top_at > _MAX_DIALOG_FRAME_AGE_MS) or (now_unix_ms - current_at > _MAX_DIALOG_FRAME_AGE_MS) or (current_at - top_at > _MAX_DIALOG_FRAME_AGE_MS):
        return 'final_evidence_dialog_timing_invalid'
    return None

def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all((character in '0123456789abcdef' for character in value))
__all__ = ['FINAL_AUTHORIZATION_SCHEMA', 'FINAL_CONFIRMATION_EVIDENCE_SCHEMA', 'FINAL_CONFIRMATION_GATE', 'derive_final_authorized_child', 'validate_final_authorized_child']
