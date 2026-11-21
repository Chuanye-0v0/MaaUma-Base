"""Optional 1.8.1 SS asset inputs from accepted observations and receipts.

Current production reads participant batteries and direct display offers, but
does not yet produce raw stat caps or actual per-member SS result receipts.
Those omissions deliberately leave B unavailable; neither expected win badges
nor a confirmed entry substitutes for an observed result. A/C do not depend on
this module. There are no reads, writes, captures, or execution requests here.
"""
from copy import deepcopy
import math

ATTRS = ('speed', 'stamina', 'power', 'guts', 'wisdom')
SCHEMA = 'larc.strategy181.observation.v1'


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _source(source, run_id, turn, sha=None):
    return (isinstance(source, dict) and source.get('run_session_id') == run_id
            and source.get('turn_index') == turn and isinstance(source.get('sha256'), str)
            and bool(source['sha256']) and (sha is None or source['sha256'] == sha)
            and not any(source.get(k) is True for k in ('simulation', 'planned', 'projected', 'hypothetical')))


def _vector(value, *, integer=False):
    return (isinstance(value, list) and len(value) == 5
            and all(_number(v) and v >= 0 and (not integer or type(v) is int) for v in value))


def _prior(roster, run_id, through_turn):
    """Project original persisted receipts only; do not invent result coverage.

    ``member_results`` follows the accepted result receipt shape already read
    by larc_action_history.ss_wins_context. A direct raw quote is optional and
    currently unproduced; when absent, no asset sample is manufactured from
    the pre-match badges or mixed post-action attribute differences.
    """
    result = {'available': False, 'sample_count': 0, 'sample_action_turns': [],
              'reason': 'ordinary_full_five_result_and_direct_raw_quote_unavailable'}
    if not isinstance(roster, dict) or roster.get('run_session_id') != run_id:
        return {**result, 'reason': 'same_run_roster_unavailable'}
    rows = roster.get('ss_execution_participants')
    if not isinstance(rows, list):
        return result
    accepted = {}
    conflicts = set()
    seen_ids = {}
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        receipt = entry.get('receipt') or {}
        if not isinstance(receipt, dict):
            continue
        turn = receipt.get('turn_index')
        key = entry.get('source_sha256')
        if (type(turn) is not int or not 0 <= turn < through_turn or not key
                or receipt.get('run_session_id') != run_id
                or receipt.get('tap_performed') is not True
                or receipt.get('action_id') != 'safe_advance.larc_ss_confirm'
                or receipt.get('source_sha256') != key
                or key not in (roster.get('ss_executions') or [])):
            continue
        if turn in accepted and accepted[turn]['entry'] == entry:
            continue
        if turn in accepted or key in seen_ids and seen_ids[key] != entry:
            conflicts.add(turn)
            if key in seen_ids:
                conflicts.add(seen_ids[key]['receipt']['turn_index'])
            continue
        seen_ids[key] = entry
        observation = entry.get('participants_observation') or {}
        expected = observation.get('participants') if isinstance(observation, dict) else None
        source = observation.get('source') or {} if isinstance(observation, dict) else {}
        if (not isinstance(expected, list) or len(expected) != 5
                or observation.get('status') != 'accepted'
                or observation.get('schema_version') != 'larc_ss_participants.v1'
                or source.get('run_session_id') != run_id or source.get('turn_index') != turn
                or source.get('source_sha256') != key):
            continue
        member_ids = [(p.get('member_ref') or {}).get('member_id')
                      for p in expected if isinstance(p, dict)]
        if len(member_ids) != 5 or any(not isinstance(v, str) or not v for v in member_ids) or len(set(member_ids)) != 5:
            continue
        actual = receipt.get('member_results') or {}
        members = actual.get('participants') if isinstance(actual, dict) else None
        if (not isinstance(actual, dict) or actual.get('status') != 'accepted'
                or actual.get('stage') != 'result' or actual.get('kind') != 'SS'
                or any(actual.get(k) is True for k in ('simulation', 'planned', 'projected', 'hypothetical'))
                or not _source(actual.get('source'), run_id, turn)
                or not isinstance(members, list) or len(members) != 5
                or any(not isinstance(p, dict) or p.get('outcome') != 'win' for p in members)
                or {p.get('member_id') for p in members} != set(member_ids)):
            continue
        quote = receipt.get('direct_reward_quote') or {}
        day = quote.get('calendar_ordinal') if isinstance(quote, dict) else None
        raw = quote.get('stats_raw_applied_gain') if isinstance(quote, dict) else None
        pt = quote.get('skill_pt_gain') if isinstance(quote, dict) else None
        hp = quote.get('energy_delta_points') if isinstance(quote, dict) else None
        if (not isinstance(quote, dict) or quote.get('status') != 'accepted'
                or quote.get('stage') != 'preview' or quote.get('kind') != 'SS'
                or quote.get('ready_count') != 5 or quote.get('units') != 'observed_applied_raw'
                or quote.get('basis') != 'direct_pre_action_quote'
                or not _source(quote.get('source'), run_id, turn, key)
                or any(quote.get(k) is True for k in ('simulation', 'planned', 'projected', 'hypothetical'))
                or type(day) is not int or not 43 <= day <= 55
                or not _vector(raw, integer=True) or type(pt) is not int or pt < 0
                or not _number(hp) or hp < 0
                or quote.get('energy_basis') != 'applied_clipped_lower_bound'):
            continue
        accepted[turn] = {'entry': deepcopy(entry), 'id': key,
                          'stats': raw, 'pt': pt, 'hp': hp, 'calendar': day}
    usable = [accepted[t] for t in sorted(accepted) if t not in conflicts][-3:]
    if not usable:
        return {**result, 'conflicting_action_turns': sorted(conflicts)}
    def median(values):
        values = sorted(values)
        n = len(values)
        return values[n // 2] if n % 2 else math.floor((values[n // 2 - 1] + values[n // 2]) / 2)
    return {**result, 'available': True, 'reason': 'strict_past_original_receipts_and_direct_raw_quotes',
            'stats_raw_applied_gain': [median([row['stats'][i] for row in usable]) for i in range(5)],
            'skill_pt_gain': median([row['pt'] for row in usable]),
            'energy_delta_points': median([row['hp'] for row in usable]),
            'sample_count': len(usable), 'sample_action_turns': [row['entry']['receipt']['turn_index'] for row in usable],
            'sample_ids': [row['id'] for row in usable], 'conflicting_action_turns': sorted(conflicts),
            'source_kind': 'original_confirmed_ss_result_and_direct_quote',
            'future_reward_observed': False}


def observation_context(run_id, turn, *, roster=None):
    """Rebuild an optional view from the already-loaded original roster.

    No new history file or main-action clock: reloading the same persisted
    receipt is idempotent, and confirmation-only entries remain ineligible.
    """
    try:
        prior = _prior(roster, run_id, turn)
    except (TypeError, ValueError, KeyError, AttributeError):
        prior = {'available': False, 'reason': 'original_receipt_projection_unavailable'}
    return {'schema_version': SCHEMA, 'run_session_id': run_id, 'through_turn': turn,
            'status': 'partial', 'prior': prior,
            'current_public': {'available': False, 'reason': 'observed_raw_stat_caps_producer_unavailable'},
            'coverage': {'current_raw_caps_observed': False,
                         'entry_receipt_is_not_result': True,
                         'blue_badge_is_not_observed_win': True,
                         'mixed_post_action_gains_not_used': True}}


def asset_inputs(state, candidate):
    """Supply normalized B inputs or a named old-score fallback, never stop.

    An upstream accepted raw/cap packet may be consumed once a real producer
    exists. This adapter itself never fills that packet from goals, simulator
    fields, or unbounded raw-equivalent valuation assumptions.
    """
    result = {'available': False, 'reason': 'strategy181_observation_unavailable',
              'prior': {'available': False}, 'current_public': {}, 'fallback': 'original_1_8_score'}
    context = state.scenario_state.get('larc_strategy181_observation') or {}
    source = (candidate.raw_data or {}).get('capture') or {}
    run_id, turn = source.get('run_session_id'), state.turn
    if (not isinstance(context, dict) or context.get('schema_version') != SCHEMA
            or not isinstance(run_id, str) or not run_id or type(turn) is not int
            or context.get('run_session_id') != run_id or context.get('through_turn') != turn
            or not _source(source, run_id, turn)):
        return result
    from core.auto_trainer.larc_calendar import period_ordinal
    from core.auto_trainer.larc_recovery import period_value
    day = period_ordinal(period_value(state))
    if type(day) is not int or not 43 <= day <= 55:
        return {**result, 'reason': 'outside_ordinary_domestic_scope'}
    prior = context.get('prior') or {}
    units = context.get('current_public') or {}
    reasons = []
    if (not isinstance(prior, dict) or prior.get('available') is not True
            or prior.get('source_kind') != 'original_confirmed_ss_result_and_direct_quote'
            or not _vector(prior.get('stats_raw_applied_gain'), integer=True)
            or type(prior.get('skill_pt_gain')) is not int or prior['skill_pt_gain'] < 0
            or not _number(prior.get('energy_delta_points')) or prior['energy_delta_points'] < 0
            or not isinstance(prior.get('sample_action_turns'), list)
            or not 1 <= len(prior['sample_action_turns']) <= 3
            or any(type(t) is not int or not 0 <= t < turn for t in prior['sample_action_turns'])):
        reasons.append('actual_full_five_result_and_raw_quote_unavailable')
    raw = units.get('stats_raw') if isinstance(units, dict) else None
    caps = units.get('stat_caps_raw') if isinstance(units, dict) else None
    if (not isinstance(units, dict) or units.get('available') is not True
            or units.get('basis') != 'accepted_observed_game_raw_and_caps'
            or any(units.get(k) is True for k in ('simulation', 'planned', 'projected', 'hypothetical'))
            or not _source(units.get('source'), run_id, turn, source['sha256'])
            or not _vector(raw) or not _vector(caps)
            or any(a > b for a, b in zip(raw or [], caps or []))):
        reasons.append('observed_raw_stat_caps_unavailable')
    else:
        # One display point above 1200 represents two raw points; odd raw
        # values are allowed, but current units must map to the observed bar.
        display = [v if v <= 1200 else 1200 + math.floor((v - 1200) / 2) for v in raw]
        if any(not _number(state.current_stats.get(k)) or state.current_stats[k] != v
               for k, v in zip(ATTRS, display)):
            reasons.append('current_raw_display_units_mismatch')
    if reasons:
        return {**result, 'reason': ';'.join(reasons), 'coverage_reasons': reasons,
                'prior': deepcopy(prior) if isinstance(prior, dict) else {'available': False}}
    return {**result, 'available': True, 'reason': 'accepted_same_run_observed_units_and_strict_past_receipts',
            'prior': deepcopy(prior), 'current_public': deepcopy(units),
            'calendar_ordinal': day, 'future_reward_observed': False}
