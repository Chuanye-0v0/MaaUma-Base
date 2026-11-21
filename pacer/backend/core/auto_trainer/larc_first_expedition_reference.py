"""Optional pure first-expedition joint-layout memory, not action outcomes.

No filesystem, strategy scoring, model calls, future income promises or changes
to the independent recent12 ring. Caller owns persistence if later authorized.
"""
from copy import deepcopy
import math

SCHEMA = 'larc.first_expedition_reference.v1'
TABS = ('speed', 'stamina', 'power', 'guts', 'wisdom')
RAW_DEFINITION = 'opportunity.raw_gain(current_display, observed_display_increment); above1200 displayed20 becomes raw_equivalent40; not actual success gain'


def _number(x): return type(x) in (int, float) and math.isfinite(x)


def _complete_gains(gains):
    return (isinstance(gains, dict) and set(gains) == set(TABS)
        and all(isinstance(gains[t], dict) and not set(TABS) - set(gains[t])
            and all(_number(gains[t][k]) and gains[t][k] >= 0 for k in TABS) for t in TABS))


def _empty(run):
    return {'schema_version': SCHEMA, 'run_session_id': run, 'records': [],
        'basis': 'past observed joint five-page success previews, not realized training outcomes',
        'raw_gain_definition': RAW_DEFINITION, 'future_same_yield_guaranteed': False}


def _period_and_free(period):
    from core.auto_trainer.larc_calendar import period_ordinal, is_overseas, free_action_days
    day = period_ordinal(period or {})
    return day, (day is not None and period.get('year_index') == 2 and is_overseas(day)
                 and day in free_action_days(day, day + 1))


def _from_state(state):
    """Match history_sample's raw normalization without its progress scorer."""
    from core.auto_trainer.larc_opportunity_forecast import raw_gain
    turn = getattr(state, 'turn', None)
    context = getattr(state, 'scenario_state', {}) or {}
    period = context.get('larc_period') or {}
    if period.get('status') != 'verified':
        return None, 'period_unverified'
    options = getattr(state, 'training_options', None) or []
    if len(options) != 5 or {getattr(c, 'training_type', None) for c in options} != set(TABS):
        return None, 'joint_five_pages_unavailable'
    captures = [(getattr(c, 'raw_data', {}) or {}).get('capture') or {} for c in options]
    runs = {s.get('run_session_id') for s in captures}
    if len(runs) != 1 or None in runs or any(s.get('turn_index') != turn for s in captures):
        return None, 'current_page_run_or_turn_mismatch'
    raw, points = {}, {}
    current = getattr(state, 'current_stats', {}) or {}
    for candidate in options:
        tab = candidate.training_type
        gains = getattr(candidate, 'stats_gain', {}) or {}
        if ((getattr(candidate, 'raw_data', {}) or {}).get('numeric_evidence') or {}).get('usable') is not True:
            return None, 'state_numeric_acceptance_unknown_use_accepted_history_sample'
        if any(not _number(current.get(k)) for k in TABS):
            return None, 'current_stat_unknown'
        # A missing attribute is unknown here, unlike treating it as zero.
        # The existing accepted history_sample path may explicitly supply zero.
        if any(not _number(gains.get(k)) or gains[k] < 0 for k in TABS):
            return None, 'display_gain_vector_incomplete_use_accepted_history_sample'
        raw[tab] = {k: raw_gain(current[k], gains[k]) for k in TABS}
        points[tab] = gains.get('skill_pt') if _number(gains.get('skill_pt')) else None
    return {'status': 'accepted', 'run_session_id': next(iter(runs)), 'period_status': 'verified',
        'source_kind': 'accepted_current_state', 'sample': {'turn_index': turn, 'period': deepcopy(period.get('value') or {}),
            'raw_gains': raw, 'skill_points': points}}, None


def _packet(current):
    if not isinstance(current, dict):
        return _from_state(current)
    if current.get('status') != 'accepted' or current.get('period_status') != 'verified':
        return None, 'accepted_verified_history_sample_required'
    if not isinstance(current.get('sample'), dict):
        return None, 'history_sample_missing'
    return current, None


def _owned(packet, optional):
    if optional is None:
        return None, 'owned_levels_unknown'
    if (not isinstance(optional, dict) or optional.get('status') != 'accepted'
        or optional.get('run_session_id') != packet['run_session_id']
        or optional.get('turn_index') != packet['sample']['turn_index']):
        return None, 'owned_effects_unbound_or_unaccepted'
    levels = optional.get('levels')
    if not isinstance(levels, list) or len(levels) != 10:
        return None, 'owned_levels_unknown'
    result = [v if type(v) is int and 0 <= v <= 3 else None for v in levels]
    return result, 'owned_current_levels_only' if all(v is not None for v in result) else 'owned_levels_partially_unknown'


def update(existing, current, known_owned_effects=None):
    """Return (new_archive, diag). No I/O, mutation or action confirmation.

    accepted history packet: {status, run_session_id, period_status, sample}.
    sample is an existing history_sample result, with complete five raw vectors.
    Same turn/date valid recollection replaces; invalid recollection preserves.
    """
    packet, problem = _packet(current)
    diag = {'updated': False, 'reason': problem, 'no_action_count_added': True,
            'not_a_new_execution_gate': True}
    if packet is None:
        return deepcopy(existing), diag
    run, sample = packet.get('run_session_id'), packet['sample']
    turn, period = sample.get('turn_index'), sample.get('period')
    if not isinstance(run, str) or not run or type(turn) is not int or turn < 1 or not isinstance(period, dict):
        return deepcopy(existing), {**diag, 'reason': 'sample_run_turn_period_unknown'}
    if existing is None:
        archive = _empty(run)
    elif not isinstance(existing, dict) or existing.get('schema_version') != SCHEMA:
        return deepcopy(existing), {**diag, 'reason': 'existing_optional_archive_invalid'}
    elif existing.get('run_session_id') != run:
        return deepcopy(existing), {**diag, 'reason': 'new_run_requires_separate_archive_existing_unchanged'}
    else:
        archive = deepcopy(existing)
    day, eligible = _period_and_free(period)
    if not eligible:
        return archive, {**diag, 'reason': 'not_first_expedition_free_day', 'calendar_ordinal': day}
    gains = sample.get('raw_gains')
    if not _complete_gains(gains):
        return archive, {**diag, 'reason': 'incomplete_joint_raw_vector_not_zero_filled'}
    records = archive.get('records')
    if not isinstance(records, list):
        return archive, {**diag, 'reason': 'existing_records_invalid'}
    for row in records:
        if not isinstance(row, dict):
            return archive, {**diag, 'reason': 'existing_record_invalid'}
        if (row.get('turn_index') == turn) != (row.get('calendar_ordinal') == day):
            return archive, {**diag, 'reason': 'main_turn_calendar_identity_conflict'}
    replaced = next((r for r in records if r.get('turn_index') == turn), None)
    order = packet.get('observation_step')
    if replaced and type(order) is int and type(replaced.get('observation_step')) is int and order < replaced['observation_step']:
        return archive, {**diag, 'reason': 'older_recollection_preserved_latest'}
    owned, owned_basis = _owned(packet, known_owned_effects)
    points = sample.get('skill_points') or {}
    points = points if isinstance(points, dict) else {}
    record = {'run_session_id': run, 'turn_index': turn, 'calendar_ordinal': day, 'period': deepcopy(period),
        'raw_gains': {t: {k: gains[t][k] for k in TABS} for t in TABS},
        'skill_points': {t: points.get(t) if _number(points.get(t)) and points[t] >= 0 else None for t in TABS},
        'known_owned_levels': owned, 'owned_effects_basis': owned_basis,
        'observation_step': order if type(order) is int else None,
        'source_kind': packet.get('source_kind', 'accepted_history_sample'),
        'forecast_metadata': deepcopy(sample.get('forecast_metadata')) if isinstance(sample.get('forecast_metadata'), dict) else None,
        'risk_observations': deepcopy(sample.get('risk_observations', {})),
        'raw_gain_definition': RAW_DEFINITION, 'preview_not_actual_success': True,
        'future_same_yield_guaranteed': False, 'future_purchase_assumed': False}
    records = [r for r in records if r.get('turn_index') != turn]
    records.append(record)
    if len(records) > 5:
        return archive, {**diag, 'reason': 'phase_reference_capacity_conflict_preserved'}
    archive['records'] = sorted(records, key=lambda r: (r['calendar_ordinal'], r['turn_index']))
    return archive, {**diag, 'updated': True, 'reason': 'replaced_last_valid_same_main' if replaced else 'added_observed_phase_layout',
        'record_count': len(records), 'owned_effects_basis': owned_basis,
        'replacement_order_basis': 'observation_step' if type(order) is int else 'last_valid_update_call_order'}


def past(existing, run_session_id, current_turn):
    """Only same run and strict prior action turns; current never reads itself."""
    if (not isinstance(existing, dict) or existing.get('schema_version') != SCHEMA
        or existing.get('run_session_id') != run_session_id or type(current_turn) is not int):
        return [], {'status': 'unavailable', 'reason': 'archive_run_schema_or_turn_unavailable'}
    rows = existing.get('records')
    if not isinstance(rows, list):
        return [], {'status': 'unavailable', 'reason': 'records_unavailable'}
    result = []
    for row in rows:
        if (not isinstance(row, dict) or row.get('run_session_id') != run_session_id
            or type(row.get('turn_index')) is not int or row['turn_index'] >= current_turn or not _complete_gains(row.get('raw_gains'))):
            continue
        period = row.get('period')
        if not isinstance(period, dict):
            continue
        day, eligible = _period_and_free(period)
        if eligible and row.get('calendar_ordinal') == day:
            result.append(deepcopy(row))
    return result[:5], {'status': 'past_observations_only', 'records': len(result[:5]),
        'future_same_yield_guaranteed': False, 'current_excluded': True}
