"""Action-attributed expectation samples; missing attribution never invents income."""
from math import isfinite
from statistics import median

from core.auto_trainer.larc_calendar import is_overseas, period_ordinal

TRANSITION_SCHEMA = 'larc.expectation_transition.v1'


def _number(value):
    return type(value) in {int, float} and isfinite(value) and 0 <= value <= 200


def with_current_transition(reference, snapshot, turn, run_id, transition):
    """Attach an optional already-executed receipt to current factual snapshot.

    Only the attribution reader receives this extra row. It is not a future
    training layout, does not replace an existing receipt, and cannot use a
    selected-but-unexecuted action as evidence. Conflicting receipts collapse
    to an unknown point in `_points` rather than choosing one.
    """
    if not isinstance(transition, dict) or not _number(snapshot.get('expectation')):
        return reference
    row = {'forecast_metadata': {'period': snapshot.get('period'),
        'source_turn_index': turn, 'run_session_id': run_id,
        'expectation': snapshot['expectation'], 'expectation_transition': transition}}
    return {**reference, 'phase_layouts': [*reference.get('phase_layouts', []), [row]]}


def _points(reference, as_of_turn, as_of_day, run_id):
    """Collapse tabs of one observed layout, not outcomes of suggested actions."""
    grouped = {}
    ignored = []
    for layout in reference.get('phase_layouts', []):
        if not isinstance(layout, (list, tuple)):
            continue
        for row in layout:
            metadata = row.get('forecast_metadata', {}) if isinstance(row, dict) else {}
            if not isinstance(metadata, dict):
                continue
            period = metadata.get('period', {})
            day = period_ordinal(period) if isinstance(period, dict) else None
            value = metadata.get('expectation')
            turn = metadata.get('source_turn_index')
            if day is None or not _number(value):
                continue
            if ((type(as_of_turn) is int and type(turn) is int and turn > as_of_turn)
                    or (as_of_day is not None and day > as_of_day)):
                ignored.append('future_snapshot')
                continue
            key = (day, turn if type(turn) is int else None)
            point = grouped.setdefault(key, {'day': day, 'turn': key[1], 'period': period,
                'values': set(), 'transitions': [], 'run_ids': set()})
            point['values'].add(float(value))
            stated = metadata.get('run_session_id')
            if isinstance(stated, str) and stated:
                point['run_ids'].add(stated)
            transition = metadata.get('expectation_transition')
            if isinstance(transition, dict) and transition not in point['transitions']:
                point['transitions'].append(transition)
    result = []
    for point in grouped.values():
        point['valid'] = len(point['values']) == 1 and len(point['transitions']) <= 1
        point['value'] = next(iter(point['values'])) if len(point['values']) == 1 else None
        point['transition'] = point['transitions'][0] if len(point['transitions']) == 1 else None
        point['run_id'] = run_id or (next(iter(point['run_ids'])) if len(point['run_ids']) == 1 else None)
        if len(point['run_ids']) > 1 or (run_id and point['run_ids'] and point['run_ids'] != {run_id}):
            point['valid'] = False
        result.append(point)
    result.sort(key=lambda p: (p['turn'] if p['turn'] is not None else -1, p['day']))
    return result, ignored


def _action(transition, before, after, expected_run):
    if not isinstance(transition, dict):
        return None, 'action_attribution_missing'
    if transition.get('schema_version') != TRANSITION_SCHEMA:
        return None, 'action_attribution_schema_unknown'
    if transition.get('execution_confirmed') is not True or transition.get('calendar_advanced') is not True:
        return None, 'action_not_confirmed_executed'
    source = transition.get('source')
    if (not isinstance(source, dict) or source.get('kind') != 'executed_action_receipt'
            or not any(isinstance(source.get(k), str) and source[k] for k in ('path', 'receipt_id'))):
        return None, 'execution_receipt_source_unknown'
    run = transition.get('run_session_id')
    if not isinstance(run, str) or not run or not expected_run or run != expected_run:
        return None, 'action_run_binding_unknown_or_mismatch'
    if (type(transition.get('from_turn_index')) is not int or type(transition.get('to_turn_index')) is not int
            or before['turn'] is None or after['turn'] is None
            or transition.get('from_turn_index') != before['turn']
            or transition.get('to_turn_index') != after['turn']
            or after['turn'] != before['turn'] + 1):
        return None, 'action_turn_binding_unknown_or_mismatch'
    if ((source.get('run_session_id') is not None and source['run_session_id'] != run)
            or (source.get('turn_index') is not None and source['turn_index'] != before['turn'])):
        return None, 'receipt_source_scope_mismatch'
    first, last = transition.get('from_period'), transition.get('to_period')
    if (not isinstance(first, dict) or not isinstance(last, dict)
            or period_ordinal(first) != before['day'] or period_ordinal(last) != after['day']):
        return None, 'action_period_binding_unknown_or_mismatch'
    kinds = transition.get('action_types', [transition.get('action_type')])
    if not isinstance(kinds, list) or not kinds or any(not isinstance(k, str) for k in kinds):
        return None, 'executed_action_type_unknown'
    aliases = {'training': 'train', 'sss': 'ss', 'fixed_race': 'race', 'race_fixed': 'race',
        'fan_race': 'race', 'optional_race': 'race', 'normal_outing': 'outing', 'mei_outing': 'outing',
        'friend_outing': 'outing'}
    kinds = {aliases.get(k.lower(), k.lower()) for k in kinds}
    if len(kinds) != 1:
        return 'composite', 'multiple_executed_actions_not_decomposed'
    kind = next(iter(kinds))
    return (kind, None) if kind in {'train', 'ss', 'race', 'rest', 'outing'} else (None, 'executed_action_type_unknown')


def ordinary_expectation_report(reference, *, run_id=None, as_of_turn=None, as_of_period=None):
    """Estimate only confirmed contiguous domestic TRAIN-associated growth.

    SS/races/rest/outings are reported under their own executed action, not
    borrowed as an unconditional income of future training. This is observed
    net interval change, not proof of a separate natural-reward component.
    """
    reference = reference if isinstance(reference, dict) else {}
    day = period_ordinal(as_of_period) if isinstance(as_of_period, dict) else None
    points, ignored = _points(reference, as_of_turn, day, run_id)
    edges = []
    segment = 0
    high_water = None
    recovering = False
    for before, after in zip(points, points[1:]):
        entry = {'from_turn_index': before['turn'], 'to_turn_index': after['turn'],
            'from_ordinal': before['day'], 'to_ordinal': after['day'], 'action_type': None,
            'observed_delta': None, 'credited_to_training_growth': False, 'segment': segment}
        if not before['valid'] or not after['valid']:
            entry['reason'] = 'conflicting_snapshot_or_attribution';segment += 1;recovering = False;high_water = None
        elif (after['day'] != before['day'] + 1
                or after['period'].get('year_index') != before['period'].get('year_index')
                or is_overseas(before['day']) != is_overseas(after['day'])):
            entry['reason'] = 'noncontiguous_or_phase_boundary';segment += 1;recovering = False;high_water = after['value']
        else:
            delta = after['value'] - before['value'];entry['observed_delta'] = delta
            high_water = before['value'] if high_water is None else max(high_water, before['value'])
            if delta < 0:
                entry['reason'] = 'expectation_drop_quarantined';segment += 1;recovering = True
            elif recovering:
                entry['reason'] = 'rebound_not_credited_reanchor_only'
                if after['value'] >= high_water:
                    recovering = False;high_water = after['value']
            else:
                expected_run = run_id or before['run_id']
                kind, reason = _action(after['transition'], before, after, expected_run)
                entry['action_type'] = kind
                if reason:
                    entry['reason'] = reason
                elif is_overseas(before['day']):
                    entry['reason'] = 'overseas_interval_not_domestic_training_income'
                elif kind != 'train':
                    entry['reason'] = 'income_bound_to_' + kind + '_action'
                else:
                    entry['credited_to_training_growth'] = True;entry['reason'] = 'confirmed_contiguous_domestic_train'
                high_water = max(high_water, after['value'])
        entry['segment'] = segment;edges.append(entry)
    # Do not carry an earlier phase/ambiguous reset's slope into the latest
    # ordinary segment. Recovery from a dropped read only establishes an anchor.
    rates = [e['observed_delta'] for e in edges
        if e['credited_to_training_growth'] and e['segment'] == segment]
    status = 'observed_training_intervals' if len(rates) >= 2 else 'unknown_insufficient_attributed_training_intervals'
    estimate = min(1.25, median(rates) * .5) if len(rates) >= 2 else 0.
    return {'schema_version': 'larc.expectation_attribution.v1', 'status': status,
        'training_growth_per_action_estimate': estimate, 'credited_training_interval_count': len(rates),
        'training_interval_deltas': rates, 'forecast_credit': .5, 'forecast_cap': 1.25,
        'no_credit_is_not_a_measurement_of_zero_natural_growth': True,
        'no_credit_due_to_unknown_attribution': len(rates) < 2,
        'basis': 'executed-receipt-bound contiguous domestic training intervals; SS/race changes never naturalized',
        'other_action_income_is_not_added_to_future_training': True,
        'missing_or_ambiguous_attribution_does_not_block_execution': True,
        'action_associated_changes': {kind: [e['observed_delta'] for e in edges
            if e['action_type'] == kind and e['observed_delta'] is not None]
            for kind in ('train', 'ss', 'race', 'rest', 'outing', 'composite')},
        'ignored_snapshots': ignored, 'intervals': edges}
