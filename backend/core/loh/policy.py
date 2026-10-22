"""Small deterministic LOH policy; observations own all input coordinates.

Ticket/score OCR is metadata, not a prerequisite for using an existing ticket.
No purchase permission exists in this policy.
"""
from __future__ import annotations
ENTRY_ACTIONS = frozenset({'enter_loh', 'choose_opponent', 'confirm_entry', 'reselect_opponent'})
PAID_RESOURCES = frozenset({'paid', 'jewels', 'jewel', 'carrot', 'carrots', 'purchase', 'money', 'premium'})
RESULT_PAGES = frozenset({'loh_result', 'loh_rewards'})
PROGRESS_PAGES = frozenset({'loh_matching', 'loh_preview', 'loh_race', 'loh_items', 'loh_hero_skill'})
ENTRY_PAGES = frozenset({'loh_hub', 'loh_opponents', 'loh_team_confirm', 'loh_entry_confirm'})
_PRIORITY = {'home': ('open_races',), 'races': ('open_events',), 'events': ('open_loh', 'scroll_events'), 'notice': ('close_notice', 'back_loh'), 'loh_hub': ('enter_loh', 'choose_opponent'), 'loh_opponents': ('choose_opponent', 'confirm_entry'), 'loh_team_confirm': ('confirm_team', 'confirm_entry'), 'loh_entry_confirm': ('confirm_entry',), 'loh_items': ('skip_items', 'confirm_items'), 'loh_hero_skill': ('confirm_hero_skill', 'select_hero_skill'), 'loh_matching': (), 'loh_preview': ('view_result', 'start_race', 'skip_race'), 'loh_race': ('view_result', 'skip_race'), 'loh_result': ('continue_result', 'claim_reward', 'close_reward', 'reselect_opponent', 'choose_opponent', 'back_loh'), 'loh_rewards': ('claim_reward', 'close_reward', 'continue_result', 'back_loh', 'reselect_opponent'), 'loh_purchase': ('cancel_purchase',)}

def normalize_options(options=None):
    if options is None:
        return {'max_entries': None}
    if not isinstance(options, dict):
        raise ValueError('LOH options must be an object')
    extra = set(options) - {'max_entries'}
    if extra:
        raise ValueError('Unknown LOH option: ' + sorted(extra)[0])
    limit = options.get('max_entries')
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError('max_entries must be a non-negative integer or null')
    return {'max_entries': limit}

def _state(value):
    return value.data if hasattr(value, 'data') else value or {}

def has_loh_context(state):
    state = _state(state)
    return bool(state.get('loh_context_verified') or state.get('pending'))

def entry_continuation_available(pending, action_id, page):
    """A known ticket-confirmation step, never a second entry reservation."""
    return bool(pending and (not pending.get('adopted')) and (not pending.get('result_seen')) and (pending.get('input_status') == 'submitted') and (action_id == 'confirm_entry') and (page == 'loh_entry_confirm') and ((pending.get('entry_confirmation') or {}).get('status') == 'available'))

def _decision(kind, reason, **fields):
    return {'kind': kind, 'reason': reason, 'action_id': None, 'consuming': False, **fields}

def _finish(status, reason):
    return _decision('finish', reason, status=status)

def authorize(action_id, observed, state=None, options=None):
    """Authorize only an observed action, without inferring money from OCR text.

    This is also called on the fresh executor frame. Page/target freshness and
    input receipts are the live adapter's responsibility, not reconstructed here.
    """
    state = _state(state)
    if observed.get('requires_loh_context') is True and (not has_loh_context(state)):
        return {'ok': False, 'reason': 'loh_context_unverified', 'consuming': False}
    candidate = (observed.get('actions') or {}).get(action_id)
    if not isinstance(candidate, dict) or candidate.get('enabled') is False:
        return {'ok': False, 'reason': 'loh_action_unavailable', 'consuming': False}
    page = observed.get('page', 'unknown')
    allowed = _PRIORITY.get(page, ())
    if action_id not in allowed:
        return {'ok': False, 'reason': 'loh_action_out_of_page', 'consuming': False}
    effect = candidate.get('effect')
    resource = candidate.get('resource_kind')
    if action_id in ENTRY_ACTIONS and effect == 'admission' and ('resource_kind' not in candidate):
        resource = (observed.get('entry_resource') or {}).get('kind')
    if action_id == 'cancel_purchase':
        return {'ok': True, 'reason': 'loh_cancel_purchase', 'consuming': False}
    if effect == 'purchase' or resource in PAID_RESOURCES:
        return {'ok': False, 'reason': 'loh_paid_consumption_not_allowed', 'consuming': False}
    if resource not in {None, '', 'none', 'free'} and (not (action_id in ENTRY_ACTIONS and effect == 'admission' and (resource == 'ticket'))):
        return {'ok': False, 'reason': 'loh_resource_consumption_not_allowed', 'consuming': False}
    if action_id in ENTRY_ACTIONS:
        if effect not in {'navigation', 'progress', 'admission'}:
            return {'ok': False, 'reason': 'loh_entry_effect_unverified', 'consuming': False}
        if effect == 'admission':
            if resource not in {'ticket', 'free'}:
                return {'ok': False, 'reason': 'loh_entry_resource_unverified', 'consuming': False}
            pending = state.get('pending')
            reserved = False
            continuation = False
            if pending:
                reserved = pending.get('input_status') == 'reserved' and pending.get('action_id') == action_id and (pending.get('source_page') == page) and (pending.get('dispatch_id') == (state.get('last_action') or {}).get('dispatch_id'))
                continuation = entry_continuation_available(pending, action_id, page)
                if reserved:
                    continuation = (state.get('last_action') or {}).get('entry_continuation') is True
                if not reserved and (not continuation):
                    return {'ok': False, 'reason': 'loh_pending_admission_unresolved', 'consuming': True}
            limit = normalize_options(options if options is not None else state.get('options'))['max_entries']
            count = state.get('entries_started', 0)
            if not continuation and limit is not None and (count > limit if reserved else count >= limit):
                return {'ok': False, 'reason': 'loh_entry_limit_reached', 'consuming': True}
            return {'ok': True, 'reason': 'loh_same_entry_confirmation' if continuation else 'loh_existing_ticket_admission', 'consuming': True, 'entry_continuation': continuation}
    elif effect == 'admission':
        return {'ok': False, 'reason': 'loh_unexpected_admission_action', 'consuming': False}
    if action_id == 'confirm_items' and (candidate.get('uses_items') is True or not (candidate.get('uses_items') is False or resource in {'none', 'free'})):
        return {'ok': False, 'reason': 'loh_item_consumption_not_allowed', 'consuming': False}
    if action_id == 'confirm_team' and observed.get('team_complete') is False:
        return {'ok': False, 'reason': 'loh_team_not_configured', 'consuming': False}
    return {'ok': True, 'reason': 'loh_observed_neutral_action', 'consuming': False}

def choose(observed, state=None, options=None):
    state = _state(state)
    options = normalize_options(options if options is not None else state.get('options'))
    page = observed.get('page', 'unknown')
    pending = state.get('pending')
    if observed.get('requires_loh_context') is True and (not has_loh_context(state)):
        return _finish('failed', 'loh_context_unverified')
    if page in {'connection_conflict', 'connection_interrupted'}:
        return _finish('failed', page)
    if page == 'loh_unconfigured' and pending:
        return _finish('failed', 'loh_pending_admission_unresolved')
    if page == 'loh_unconfigured':
        return _finish('needs_configuration', 'loh_team_not_configured')
    if page in {'loh_no_tickets', 'loh_closed'}:
        if pending:
            return _finish('failed', 'loh_pending_admission_unresolved')
        status = 'done' if state.get('entries_completed', 0) else 'skipped'
        return _finish(status, page)
    if page in {'unknown', 'loading', 'loh_matching'}:
        return _decision('wait', observed.get('reason', 'loh_waiting'))
    if page in RESULT_PAGES and (not (pending and pending.get('result_seen'))):
        if not state.get('last_completed_entry'):
            return _decision('wait', 'loh_result_ownership_unverified')
        if not state.get('result_boundary_ready'):
            return _decision('wait', 'loh_duplicate_result')
    limit = options['max_entries']
    at_limit = limit is not None and state.get('entries_started', 0) >= limit
    if at_limit and (not pending) and (page in ENTRY_PAGES):
        return _finish('done', 'loh_entry_limit_reached')
    rejected = None
    actions = observed.get('actions') or {}
    priorities = list(_PRIORITY.get(page, ()))
    for action_id in priorities:
        if action_id not in actions or actions[action_id].get('enabled') is False:
            continue
        verdict = authorize(action_id, observed, state, options)
        if verdict['ok']:
            continuation = bool(verdict.get('entry_continuation') and entry_continuation_available(pending, action_id, page))
            if verdict['consuming'] and pending and (not continuation):
                rejected = 'loh_pending_admission_unresolved'
                continue
            if verdict['consuming'] and at_limit and (not continuation):
                return _finish('done', 'loh_entry_limit_reached')
            return _decision('action', verdict['reason'], action_id=action_id, consuming=verdict['consuming'], entry_continuation=continuation)
        rejected = verdict['reason']
    if pending and page in ENTRY_PAGES:
        return _finish('failed', 'loh_pending_admission_unresolved')
    if rejected == 'loh_entry_limit_reached':
        return _finish('done', rejected)
    if rejected in {'loh_paid_consumption_not_allowed', 'loh_item_consumption_not_allowed', 'loh_resource_consumption_not_allowed'}:
        return _finish('failed', rejected)
    return _decision('wait', rejected or 'loh_no_action_yet')
