"""Small deterministic LOH policy; observations own all input coordinates.

Ticket/score OCR is metadata, not a prerequisite for using an existing ticket.
No purchase permission exists in this policy.
"""
from __future__ import annotations


ENTRY_ACTIONS = frozenset({"enter_loh", "choose_opponent", "confirm_entry", "reselect_opponent"})
PAID_RESOURCES = frozenset({"paid", "jewels", "jewel", "carrot", "carrots", "purchase", "money", "premium"})
RESULT_PAGES = frozenset({"loh_result", "loh_rewards"})
PROGRESS_PAGES = frozenset({"loh_matching", "loh_preview", "loh_race", "loh_items", "loh_hero_skill"})
ENTRY_PAGES = frozenset({"loh_hub", "loh_opponents", "loh_team_confirm", "loh_entry_confirm"})

_PRIORITY = {
    "loh_spectator_hub": ("spectator_open", "back_loh"),
    "loh_spectator_list": ("spectator_vote", "spectator_a", "spectator_b", "back_loh"),
    "loh_spectator_reward": ("spectator_close_reward",),
    "home": ("open_races",),
    "races": ("open_events",),
    "events": ("open_loh", "scroll_events", "back_loh"),
    "notice": ("close_notice", "back_loh"),
    "loh_network_retry": ("retry_network",),
    "loh_hub": ("enter_loh", "choose_opponent"),
    "loh_opponents": ("choose_opponent", "confirm_entry"),
    "loh_team_confirm": ("confirm_team", "confirm_entry", "back_loh"),
    "loh_unconfigured": ("auto_team",),
    "loh_auto_team": ("auto_team_confirm", "back_loh"),
    "loh_entry_confirm": ("enable_hero_items", "confirm_entry"),
    "loh_items": ("enable_hero_items", "skip_items", "confirm_items"),
    "loh_hero_skill": ("confirm_hero_skill", "select_hero_skill"),
    "loh_title_selection": ("confirm_title", "select_title_candidate"),
    "loh_title_confirm": ("confirm_title_choice",),
    "loh_matching": (),
    "loh_preview": ("enable_short", "view_result", "skip_race", "start_race"),
    "loh_race": ("view_result", "skip_race"),
    "loh_result": ("continue_result", "claim_reward", "close_reward", "reselect_opponent", "choose_opponent", "back_loh"),
    "loh_rewards": ("claim_reward", "close_reward", "continue_result", "back_loh", "reselect_opponent"),
    "loh_purchase": ("cancel_purchase",),
    "loh_no_tickets": ("continue_result", "back_loh"),
    "loh_closed": ("back_loh",),
}


def normalize_options(options=None):
    if options is None:
        return {"max_entries": None, "fill_empty_team": False}
    if not isinstance(options, dict):
        raise ValueError("LOH options must be an object")
    extra = set(options) - {"max_entries", "fill_empty_team"}
    if extra:
        raise ValueError("Unknown LOH option: " + sorted(extra)[0])
    limit = options.get("max_entries")
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError("max_entries must be a non-negative integer or null")
    fill = options.get('fill_empty_team', False)
    if type(fill) is not bool:
        raise ValueError('fill_empty_team must be a boolean')
    return {"max_entries": limit, "fill_empty_team": fill}


def _state(value):
    return value.data if hasattr(value, "data") else value or {}


def has_loh_context(state):
    state = _state(state)
    return bool(state.get("loh_context_verified") or state.get("pending"))


def entry_continuation_available(pending, action_id, page):
    """A known ticket-confirmation step, never a second entry reservation."""
    return bool(pending and not pending.get("adopted") and not pending.get("result_seen")
        and pending.get("input_status") == "submitted"
        and action_id == "confirm_entry" and page == "loh_entry_confirm"
        and ((pending.get("entry_confirmation") or {}).get("status") == "available"
             or ((pending.get("entry_confirmation") or {}).get("status") == "expired"
                 and pending.get('action_id') == 'choose_opponent'
                 and pending.get('source_page') in {'loh_hub', 'loh_result'})))


def _decision(kind, reason, **fields):
    return {"kind": kind, "reason": reason, "action_id": None, "consuming": False, **fields}


def _finish(status, reason):
    return _decision("finish", reason, status=status)


def authorize(action_id, observed, state=None, options=None):
    """Authorize only an observed action, without inferring money from OCR text.

    This is also called on the fresh executor frame. Page/target freshness and
    input receipts are the live adapter's responsibility, not reconstructed here.
    """
    state = _state(state)
    from .final_watch import authorize as authorize_watch, requirement as watch_requirement, complete as watch_complete
    watching=watch_requirement(state)
    verdict=authorize_watch(action_id,observed,state)
    if verdict is not None:return verdict
    if watching and not state.get('pending') and action_id in ENTRY_ACTIONS:
        return {'ok':False,'reason':'loh_final_watch_no_ticket_admission','consuming':False}
    if watching and not state.get('pending_like') and action_id=='spectator_vote':
        return {'ok':False,'reason':'loh_final_watch_no_new_like','consuming':False}
    if (watching and action_id=='back_loh' and observed.get('page')=='events'
            and watch_complete(state) and not state.get('pending_watch')):
        return {'ok':True,'reason':'loh_final_watch_return_home','consuming':False}
    if observed.get("requires_loh_context") is True and not has_loh_context(state):
        return {"ok": False, "reason": "loh_context_unverified", "consuming": False}
    if observed.get('spectator_playback_cancel') and (state.get('pending') or state.get('pending_like')):
        return {'ok':False,'reason':'loh_original_submission_preserved','consuming':False}
    candidate = (observed.get("actions") or {}).get(action_id)
    if not isinstance(candidate, dict) or candidate.get("enabled") is False:
        return {"ok": False, "reason": "loh_action_unavailable", "consuming": False}
    if state.get('pending_like') and candidate.get('effect') == 'admission':
        return {'ok': False, 'reason': 'loh_like_submission_unresolved', 'consuming': False}
    page = observed.get("page", "unknown")
    if page=='events' and action_id=='back_loh' and not (
            observed.get('spectator_playback_period') is True
            and state.get('spectator_playback_cancel_verified') and not state.get('pending')
            and not state.get('pending_like')):
        return {'ok':False,'reason':'loh_spectator_period_not_verified','consuming':False}
    allowed = _PRIORITY.get(page, ())
    if action_id not in allowed:
        return {"ok": False, "reason": "loh_action_out_of_page", "consuming": False}
    if action_id in {'auto_team', 'auto_team_confirm'}:
        options = normalize_options(options if options is not None else state.get('options'))
        if not options['fill_empty_team'] or (action_id == 'auto_team' and observed.get('team_empty') is not True):
            return {"ok": False, "reason": "loh_empty_team_not_selected", "consuming": False}
        if action_id == 'auto_team_confirm' and not state.get('empty_team_auto_requested'):
            return {"ok": False, "reason": "loh_auto_team_request_missing", "consuming": False}
    if action_id == 'enable_short' and not state.get('prefer_short_mode'):
        return {'ok':False,'reason':'loh_short_mode_not_selected','consuming':False}
    if action_id == 'enable_hero_items' and not state.get('auto_hero_items'):
        return {'ok':False,'reason':'loh_auto_hero_items_not_selected','consuming':False}
    if action_id == 'confirm_entry' and candidate.get('uses_items') is True:
        if not (state.get('auto_hero_items') and observed.get('auto_hero_items') is True
                and candidate.get('item_policy') == 'existing_hero_corresponding'):
            return {'ok':False,'reason':'loh_item_consumption_not_allowed','consuming':True}
    effect = candidate.get("effect")
    resource = candidate.get("resource_kind")
    if action_id in ENTRY_ACTIONS and effect == "admission" and "resource_kind" not in candidate:
        resource = (observed.get("entry_resource") or {}).get("kind")
    if action_id == "cancel_purchase":
        return {"ok": True, "reason": "loh_cancel_purchase", "consuming": False}
    if effect == "purchase" or resource in PAID_RESOURCES:
        return {"ok": False, "reason": "loh_paid_consumption_not_allowed", "consuming": False}
    if resource not in {None, "", "none", "free"} and not (
            action_id in ENTRY_ACTIONS and effect == "admission" and resource == "ticket"):
        return {"ok": False, "reason": "loh_resource_consumption_not_allowed", "consuming": False}
    from .spectator import authorize as authorize_spectator
    spectator = authorize_spectator(action_id, observed, state)
    if spectator is not None:
        return spectator
    if action_id in ENTRY_ACTIONS:
        if effect not in {"navigation", "progress", "admission"}:
            return {"ok": False, "reason": "loh_entry_effect_unverified", "consuming": False}
        if effect == "admission":
            if resource not in {"ticket", "free"}:
                return {"ok": False, "reason": "loh_entry_resource_unverified", "consuming": False}
            pending = state.get("pending")
            reserved = False
            continuation = False
            if pending:
                reserved = (pending.get("input_status") == "reserved"
                    and pending.get("action_id") == action_id
                    and pending.get("source_page") == page
                    and pending.get("dispatch_id") == (state.get("last_action") or {}).get("dispatch_id"))
                continuation = entry_continuation_available(pending, action_id, page)
                if reserved:
                    continuation = (state.get("last_action") or {}).get("entry_continuation") is True
                if not reserved and not continuation:
                    return {"ok": False, "reason": "loh_pending_admission_unresolved", "consuming": True}
            limit = normalize_options(options if options is not None else state.get("options"))["max_entries"]
            count = state.get("entries_started", 0)
            if not continuation and limit is not None and (count > limit if reserved else count >= limit):
                return {"ok": False, "reason": "loh_entry_limit_reached", "consuming": True}
            return {"ok": True, "reason": "loh_same_entry_confirmation" if continuation else "loh_existing_ticket_admission",
                    "consuming": True, "entry_continuation": continuation}
    elif effect == "admission":
        return {"ok": False, "reason": "loh_unexpected_admission_action", "consuming": False}
    if action_id == "confirm_items" and (candidate.get("uses_items") is True or not (
            candidate.get("uses_items") is False or resource in {"none", "free"})):
        return {"ok": False, "reason": "loh_item_consumption_not_allowed", "consuming": False}
    if action_id == "confirm_team" and observed.get("team_complete") is False:
        return {"ok": False, "reason": "loh_team_not_configured", "consuming": False}
    return {"ok": True, "reason": "loh_observed_neutral_action", "consuming": False}


def choose(observed, state=None, options=None):
    state = _state(state)
    options = normalize_options(options if options is not None else state.get("options"))
    page = observed.get("page", "unknown")
    pending = state.get("pending")
    if observed.get("requires_loh_context") is True and not has_loh_context(state):
        return _finish("failed", "loh_context_unverified")
    if page in {"connection_conflict", "connection_interrupted"}:
        return _finish("failed", page)
    from .final_watch import choose as choose_watch, requirement as watch_requirement
    viewing=choose_watch(observed,state)
    if viewing is not None:return viewing
    if watch_requirement(state) and not pending and page in ENTRY_PAGES|PROGRESS_PAGES|RESULT_PAGES:
        return _finish('failed','loh_final_watch_page_not_verified')
    from .spectator import choose as choose_spectator, complete as spectator_complete
    if page == 'home' and state.get('spectator_active') and not pending:
        if state.get('pending_like') or not spectator_complete(state):
            return _finish('failed', 'loh_like_submission_unresolved')
        return _finish('done', 'loh_spectator_groups_home_verified')
    spectator = choose_spectator(observed, state)
    if spectator is not None:
        return spectator
    if page == "loh_unconfigured" and pending:
        return _finish("failed", "loh_pending_admission_unresolved")
    if page == "loh_unconfigured" and not (options['fill_empty_team'] and observed.get('team_empty') is True):
        return _finish("needs_configuration", "loh_team_not_configured")
    if page == 'home' and state.get('tickets_exhausted') and not pending:
        return _finish('done' if state.get('entries_completed',0) else 'skipped',
                       'loh_tickets_exhausted_home_verified')
    if page == 'home' and state.get('ranking_period_verified') and not pending:
        return _finish('done' if state.get('entries_completed', 0) else 'skipped',
                       'loh_ranking_period_home_verified')
    if page=='events' and state.get('spectator_playback_cancel_verified'):
        if (observed.get('spectator_playback_period') is True and not pending
                and not state.get('pending_like') and 'back_loh' in observed.get('actions',{})):
            verdict=authorize('back_loh',observed,state,options)
            if verdict['ok']:
                return _decision('action','loh_spectator_replay_period_return_home',action_id='back_loh')
        return _finish('failed','loh_spectator_replay_period_unverified')
    if page in {"loh_no_tickets", "loh_closed"}:
        if pending:
            return _finish("failed", "loh_pending_admission_unresolved")
        if page == 'loh_closed' and observed.get('unavailable_verified') is True:
            if 'back_loh' in observed.get('actions', {}):
                verdict = authorize('back_loh', observed, state, options)
                if verdict['ok']:
                    return _decision('action', 'loh_ranking_period_return_home', action_id='back_loh')
            return _decision('wait', 'loh_ranking_period_exit_control_loading')
        if page == 'loh_no_tickets':
            for action in ('continue_result', 'back_loh'):
                if action in observed.get('actions', {}):
                    verdict = authorize(action, observed, state, options)
                    if verdict['ok']:
                        return _decision('action', 'loh_exhausted_return_home', action_id=action)
            if observed.get('exhaustion_verified') is True:
                return _decision('wait', 'loh_exhausted_exit_control_loading')
        status = "done" if state.get("entries_completed", 0) else "skipped"
        return _finish(status, page)
    if page in {"unknown", "loading", "loh_matching"}:
        return _decision("wait", observed.get("reason", "loh_waiting"))
    if page in RESULT_PAGES and not (pending and pending.get("result_seen")):
        if page == 'loh_rewards' and not pending and state.get('team_config_reward_pending'):
            for reward_action in ('close_reward', 'claim_reward', 'continue_result', 'back_loh'):
                if reward_action in observed.get('actions', {}):
                    verdict = authorize(reward_action, observed, state, options)
                    if verdict['ok'] and not verdict['consuming']:
                        return _decision('action', 'loh_team_configuration_reward', action_id=reward_action)
        if not state.get("last_completed_entry"):
            return _decision("wait", "loh_result_ownership_unverified")
        if not state.get("result_boundary_ready"):
            return _decision("wait", "loh_duplicate_result")
    limit = options["max_entries"]
    at_limit = limit is not None and state.get("entries_started", 0) >= limit
    if at_limit and not pending and page in ENTRY_PAGES:
        return _finish("done", "loh_entry_limit_reached")
    rejected = None
    actions = observed.get("actions") or {}
    priorities = list(_PRIORITY.get(page, ()))
    if page == 'loh_result' and observed.get('reason') == 'settled_points_summary':
        priorities = ['choose_opponent'] + [a for a in priorities if a != 'choose_opponent']
    if not state.get('prefer_short_mode'):
        priorities = [a for a in priorities if a != 'enable_short']
    if not state.get('auto_hero_items'):
        priorities = [a for a in priorities if a != 'enable_hero_items']
    if page == 'loh_team_confirm' and state.get('team_ready') and not observed.get('team_confirmation_modal'):
        priorities = ['back_loh']
    if page == 'loh_hub' and state.get('team_ready'):
        priorities = [action for action in priorities if action != 'enter_loh']
    for action_id in priorities:
        if action_id not in actions or actions[action_id].get("enabled") is False:
            continue
        verdict = authorize(action_id, observed, state, options)
        if verdict["ok"]:
            continuation = bool(verdict.get("entry_continuation") and
                                entry_continuation_available(pending, action_id, page))
            if verdict["consuming"] and pending and not continuation:
                rejected = "loh_pending_admission_unresolved"
                continue
            if verdict["consuming"] and at_limit and not continuation:
                return _finish("done", "loh_entry_limit_reached")
            return _decision("action", verdict["reason"], action_id=action_id,
                             consuming=verdict["consuming"], entry_continuation=continuation)
        rejected = verdict["reason"]
    if pending and page in ENTRY_PAGES:
        if ((page == pending.get('source_page')
                or page == 'loh_hub' and pending.get('source_page') == 'loh_result'
                   and pending.get('action_id') == 'choose_opponent')
                and pending.get('input_status') == 'submitted'
                and not pending.get('result_seen')):
            # Matching may briefly return to its original hub even after the
            # team preview. Observe this submitted entry; never resubmit it.
            return _decision('wait', 'loh_submitted_entry_transition')
        # A submitted/uncertain admission returning to an entrance is not proof
        # that the ticket was unspent. Explicit neutral progress remains usable.
        return _finish("failed", "loh_pending_admission_unresolved")
    if rejected == "loh_entry_limit_reached":
        return _finish("done", rejected)
    if rejected in {"loh_paid_consumption_not_allowed", "loh_item_consumption_not_allowed",
                    "loh_resource_consumption_not_allowed"}:
        return _finish("failed", rejected)
    return _decision("wait", rejected or "loh_no_action_yet")
