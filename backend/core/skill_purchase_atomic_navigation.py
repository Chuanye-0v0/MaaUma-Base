"""Pure one-gesture navigation state for final skill purchase routing."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Any, Mapping
SCHEMA_VERSION = 'uma.skill_purchase_navigation_session.v1'
SCROLL_UP_ACTION_ID = 'skill_purchase.scroll_up'
REVEAL_BOTTOM_PARTIAL_ACTION_ID = 'skill_purchase.reveal_bottom_partial'
RECOVER_SELECTED_TARGET_ACTION_ID = 'skill_purchase.recover_selected_target'
SCROLL_UP_GESTURE = {'x1': 360, 'y1': 610, 'x2': 360, 'y2': 900, 'duration_ms': 450, 'scaled': False}
REVEAL_BOTTOM_PARTIAL_GESTURE = {'x1': 360, 'y1': 900, 'x2': 360, 'y2': 760, 'duration_ms': 350, 'scaled': False}
RECOVER_SELECTED_TARGET_GESTURE = {'x1': 360, 'y1': 900, 'x2': 360, 'y2': 610, 'duration_ms': 450, 'scaled': False}

@dataclass(frozen=True)
class SkillPurchaseNavigationDecision:
    action_id: str | None
    reason: str
    gesture: dict[str, Any] | None = None

@dataclass
class SkillPurchaseNavigationSession:
    plan_id: str
    session_id: str
    capture_batch_id: str
    max_swipes: int
    swipe_count: int = 0
    seen_frame_sha256: set[str] = field(default_factory=set)

    @classmethod
    def start(cls, *, plan_id: str, session_id: str, capture_batch_id: str, max_swipes: int) -> 'SkillPurchaseNavigationSession':
        session = cls(plan_id=plan_id, session_id=session_id, capture_batch_id=capture_batch_id, max_swipes=max_swipes)
        session._validate()
        return session

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> 'SkillPurchaseNavigationSession':
        if not isinstance(payload, Mapping) or payload.get('schema_version') != SCHEMA_VERSION:
            raise ValueError('navigation_session_schema_invalid')
        seen = payload.get('seen_frame_sha256')
        if not isinstance(seen, list) or not all((isinstance(item, str) for item in seen)):
            raise ValueError('navigation_session_seen_frames_invalid')
        session = cls(plan_id=payload.get('plan_id'), session_id=payload.get('session_id'), capture_batch_id=payload.get('capture_batch_id'), max_swipes=payload.get('max_swipes'), swipe_count=payload.get('swipe_count'), seen_frame_sha256=set(seen))
        session._validate()
        if len(session.seen_frame_sha256) != len(seen):
            raise ValueError('navigation_session_duplicate_seen_frame')
        if session.swipe_count != len(session.seen_frame_sha256):
            raise ValueError('navigation_session_count_mismatch')
        return session

    def to_dict(self) -> dict[str, Any]:
        self._validate()
        if self.swipe_count != len(self.seen_frame_sha256):
            raise ValueError('navigation_session_count_mismatch')
        return {'schema_version': SCHEMA_VERSION, 'plan_id': self.plan_id, 'session_id': self.session_id, 'capture_batch_id': self.capture_batch_id, 'max_swipes': self.max_swipes, 'swipe_count': self.swipe_count, 'seen_frame_sha256': sorted(self.seen_frame_sha256)}

    def consume_swipe(self, frame_sha256: str) -> None:
        if not _valid_sha(frame_sha256):
            raise ValueError('navigation_frame_sha_invalid')
        if frame_sha256 in self.seen_frame_sha256:
            raise ValueError('navigation_frame_sha_replayed')
        if self.swipe_count >= self.max_swipes:
            raise ValueError('navigation_swipe_limit_reached')
        self.seen_frame_sha256.add(frame_sha256)
        self.swipe_count += 1

    def _validate(self) -> None:
        for name, value in (('plan_id', self.plan_id), ('session_id', self.session_id), ('capture_batch_id', self.capture_batch_id)):
            if not isinstance(value, str) or not value:
                raise ValueError(f'navigation_{name}_invalid')
        if not isinstance(self.max_swipes, int) or isinstance(self.max_swipes, bool) or (not 1 <= self.max_swipes <= 16):
            raise ValueError('navigation_max_swipes_invalid')
        if not isinstance(self.swipe_count, int) or isinstance(self.swipe_count, bool) or (not 0 <= self.swipe_count <= self.max_swipes):
            raise ValueError('navigation_swipe_count_invalid')
        if not isinstance(self.seen_frame_sha256, set) or any((not _valid_sha(item) for item in self.seen_frame_sha256)):
            raise ValueError('navigation_seen_frames_invalid')

def plan_navigation_once(*, state: Mapping[str, Any], plan: Mapping[str, Any], session: SkillPurchaseNavigationSession, allow_recover_selected_target: bool=False) -> SkillPurchaseNavigationDecision:
    """Plan no more than one reverse skill-list swipe from fresh evidence."""
    if not isinstance(state, Mapping):
        return _stop('fresh_state_invalid')
    if not isinstance(plan, Mapping):
        return _stop('plan_invalid')
    try:
        session._validate()
    except ValueError as exc:
        return _stop(str(exc))
    purchases = plan.get('purchases')
    if not isinstance(purchases, list) or len(purchases) != 1:
        return _stop('planned_target_not_unique')
    target = purchases[0]
    if not isinstance(target, Mapping) or not isinstance(target.get('item_id'), str):
        return _stop('planned_target_invalid')
    promotion = plan.get('execution_promotion')
    if not isinstance(promotion, Mapping):
        return _stop('plan_promotion_missing')
    if plan.get('plan_id') != session.plan_id or promotion.get('session_id') != session.session_id or promotion.get('fresh_capture_id') != session.capture_batch_id:
        return _stop('plan_session_binding_mismatch')
    if state.get('fresh') is not True:
        return _stop('fresh_capture_required')
    if state.get('page_kind') != state.get('screen_kind'):
        return _stop('page_screen_kind_conflict')
    if state.get('page_kind') != 'skill_acquisition':
        return _stop('page_kind_mismatch')
    if state.get('plan_id') != session.plan_id or state.get('session_id') != session.session_id or state.get('capture_batch_id') != session.capture_batch_id:
        return _stop('fresh_binding_mismatch')
    if state.get('currency_scope') != 'skill_points_only':
        return _stop('currency_scope_mismatch')
    if state.get('observed_currency') != 'skill_points':
        return _stop('observed_currency_mismatch')
    flags = state.get('dangerous_resource_flags')
    if not isinstance(flags, list) or flags:
        return _stop('dangerous_resource_signal')
    frame_sha = state.get('frame_sha256')
    if not _valid_sha(frame_sha):
        return _stop('frame_sha_invalid')
    if frame_sha in session.seen_frame_sha256:
        return _stop('frame_sha_replayed')
    selected_item_ids = state.get('selected_item_ids')
    if isinstance(selected_item_ids, list) and selected_item_ids:
        if selected_item_ids == [target['item_id']]:
            return _stop('target_selected_visible')
        return _stop('unexpected_selected_items')
    rows = state.get('rows')
    if not isinstance(rows, list):
        return _stop('rows_unreadable')
    matches = [row for row in rows if isinstance(row, Mapping) and row.get('item_id') == target['item_id']]
    if len(matches) == 1:
        return _stop('target_visible')
    if len(matches) > 1:
        return _stop('target_row_not_unique')
    candidates = state.get('target_candidates', [])
    if not isinstance(candidates, list):
        return _stop('target_candidates_unreadable')
    target_candidates = [candidate for candidate in candidates if isinstance(candidate, Mapping) and candidate.get('item_id') == target['item_id']]
    if len(target_candidates) == 1:
        candidate = target_candidates[0]
        bbox = candidate.get('row_bbox')
        if candidate.get('reason') == 'row_partially_visible_bottom' and isinstance(bbox, list) and (len(bbox) == 4) and all((isinstance(value, int) for value in bbox)) and (bbox[3] >= 1029):
            return SkillPurchaseNavigationDecision(action_id=REVEAL_BOTTOM_PARTIAL_ACTION_ID, reason='target_bottom_partial', gesture=dict(REVEAL_BOTTOM_PARTIAL_GESTURE))
        return _stop('target_candidate_requires_review')
    if len(target_candidates) > 1:
        return _stop('target_candidate_not_unique')
    expected_remaining = target.get('expected_skill_points_after')
    confirm_candidate = state.get('selection_confirm_candidate')
    if allow_recover_selected_target is True and session.swipe_count > 0 and isinstance(expected_remaining, int) and (not isinstance(expected_remaining, bool)) and (state.get('current_skill_points') == expected_remaining) and isinstance(confirm_candidate, Mapping) and isinstance(confirm_candidate.get('x'), int) and isinstance(confirm_candidate.get('y'), int):
        return SkillPurchaseNavigationDecision(action_id=RECOVER_SELECTED_TARGET_ACTION_ID, reason='selected_target_hidden_after_navigation', gesture=dict(RECOVER_SELECTED_TARGET_GESTURE))
    if session.swipe_count >= session.max_swipes:
        return _stop('navigation_swipe_limit_reached')
    return SkillPurchaseNavigationDecision(action_id=SCROLL_UP_ACTION_ID, reason='target_not_visible', gesture=dict(SCROLL_UP_GESTURE))

def _stop(reason: str) -> SkillPurchaseNavigationDecision:
    return SkillPurchaseNavigationDecision(None, reason, None)

def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch('[0-9a-f]{64}', value))
__all__ = ['SCHEMA_VERSION', 'SCROLL_UP_ACTION_ID', 'SCROLL_UP_GESTURE', 'REVEAL_BOTTOM_PARTIAL_ACTION_ID', 'REVEAL_BOTTOM_PARTIAL_GESTURE', 'RECOVER_SELECTED_TARGET_ACTION_ID', 'RECOVER_SELECTED_TARGET_GESTURE', 'SkillPurchaseNavigationDecision', 'SkillPurchaseNavigationSession', 'plan_navigation_once']
