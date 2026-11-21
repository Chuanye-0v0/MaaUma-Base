"""Calendar-node preferences among existing optional adaptation candidates.

This module only selects a plan. Prices, locks, target levels, race preparation
and transaction/session execution remain owned by the existing planner.
"""
from core.auto_trainer.rejection_policy import is_hard_rejected
from copy import deepcopy
import json
import math


PHASES = ('guts_expedition', 'stamina_expedition', 'power_expedition',
          'speed_expedition', 'french_three', 'expedition_skill_pt',
          'courage_three', 'willpower_three')


def matched_effect(state, goal):
    """Unknown optional context does not add a game/OCR failure path."""
    if state is None or goal is None:
        return None
    from core.auto_trainer.larc_timeline_workflow import resolve
    trace = resolve(state, goal)
    if trace.get('status') != 'matched':
        return None
    effect = trace.get('effects', {}).get('adaptation')
    if not isinstance(effect, dict):
        return None
    # Normal saved profiles already pass the shared compiler; keep this pure
    # hook tolerant of older read-only callers that supply partial objects.
    mode, priorities = effect.get('mode', 'auto'), effect.get('priorities', [])
    reserve = effect.get('reserve_points', 0)
    if (mode not in ('auto', 'prefer', 'defer') or not isinstance(priorities, list)
            or any(phase not in PHASES for phase in priorities)
            or type(reserve) is not int or reserve < 0):
        return None
    return {'stage_id': trace.get('stage_id'), 'rule_id': trace.get('rule_id'),
            'effect': {'mode': mode, 'priorities': list(priorities), 'reserve_points': reserve}}


def cache_signature(state, goal):
    if state is None or goal is None:
        return None
    from core.auto_trainer.larc_timeline_workflow import resolve
    trace = resolve(state, goal)
    if not trace.get('active'):
        return None
    effects = trace.get('effects') or {}
    match = {'status': trace.get('status'), 'stage_id': trace.get('stage_id'),
        'rule_id': trace.get('rule_id'), 'effects': {key: effects[key] for key in
            ('attribute_weights', 'after_target_credit', 'adaptation') if key in effects}}
    return json.dumps(match, sort_keys=True, separators=(',', ':'))


def _parts(row):
    from core.auto_trainer.larc_adaptation import BUNDLE_PHASES
    phase = row.get('phase')
    parts = (phase,) if phase in PHASES else BUNDLE_PHASES.get(phase, ())
    # French Lv2 is an existing model candidate, not a new configurable Lv3
    # target. Keep it available to auto/reserve/fallback without ranking it as
    # french_three when the user specifically prefers the wisdom upgrade.
    if not parts or any(part not in (*PHASES, 'french_two') for part in parts):
        return ()
    declared = row.get('purchase_phases')
    if declared is not None and (not isinstance(declared, list) or set(declared) != set(parts)):
        return ()
    return parts


def _eligible(rows, observation, reserve):
    from core.auto_trainer.larc_adaptation import build_adaptation_plan
    eligible, skipped = [], []
    for row in rows:
        if not isinstance(row, dict):
            continue
        phase, score = row.get('phase'), row.get('score')
        if (is_hard_rejected(row) or (row.get('budget') or {}).get('affordable_at_deadline') is False
                or (row.get('current_training_purchase_coupling') or {}).get('allowed') is False
                or (row.get('joint_global_current_action_binding') or {}).get('allowed') is False):
            skipped.append({'phase': phase, 'reason': 'original_candidate_rejected'})
            continue
        if not _parts(row) or type(score) not in (int, float) or not math.isfinite(score):
            skipped.append({'phase': phase, 'reason': 'unsupported_optional_candidate'})
            continue
        try:
            plan = build_adaptation_plan(observation, phase=phase)
        except (ValueError, TypeError, KeyError):
            skipped.append({'phase': phase, 'reason': 'existing_plan_unavailable'})
            continue
        if type(row.get('cost')) is not int or plan['cost'] != row['cost']:
            skipped.append({'phase': phase, 'reason': 'existing_plan_cost_mismatch'})
            continue
        if row.get('targets') is not None and row['targets'] != plan['targets']:
            skipped.append({'phase': phase, 'reason': 'existing_plan_targets_mismatch'})
            continue
        if observation['points'] - plan['cost'] < reserve:
            skipped.append({'phase': phase, 'reason': 'workflow_points_reserve'})
            continue
        eligible.append(row)
    return eligible, skipped


def _forecast_selection(result, selected):
    """Keep the model forecast and the actual workflow selection distinct."""
    plan = result.get('purchase_plan')
    if not isinstance(plan, dict):
        return
    if 'selected_route' in plan and 'model_selected_route' not in plan:
        plan['model_selected_route'] = deepcopy(plan['selected_route'])
    routes = (selected or {}).get('purchase_routes_v3') if selected else plan.get('waiting_routes')
    if isinstance(routes, list) and routes:
        plan['selected_route'] = deepcopy(next((route for route in routes
            if route.get('income_branch') == 'ordinary' and route.get('current_training_success')), routes[-1]))
    else:
        plan['selected_route'] = {'actions': [], 'status': 'workflow_forecast_unavailable'}
    plan['selection_source'] = 'calendar_workflow'


def record_selection(result, selected):
    detail = result.get('workflow_adaptation')
    if isinstance(detail, dict):
        detail['selected_phase'] = (selected or {}).get('phase')
    _forecast_selection(result, selected)


def apply(recommendation, observation, state, goal, *, race_day=False):
    """Apply user preferences without creating or reviving a purchase option."""
    # Race-day/fixed preparation has a separate owner and is never deferred by
    # this optional-growth preference, even if a calendar rule matches.
    if race_day:
        return recommendation
    match = matched_effect(state, goal)
    if match is None:
        return recommendation
    effect = match['effect']
    mode, priorities, reserve = effect['mode'], effect['priorities'], effect['reserve_points']
    if mode == 'auto' and reserve == 0:
        return recommendation
    if (not isinstance(observation, dict) or observation.get('errors')
            or observation.get('pending_cost') != 0 or type(observation.get('points')) is not int
            or observation['points'] < 0):
        result = deepcopy(recommendation)
        result['workflow_adaptation'] = {**match, 'restricted': False,
            'status': 'unavailable', 'reason': 'current_points_or_grid_unknown'}
        return result
    rows = recommendation.get('considered')
    if not isinstance(rows, list):
        result = deepcopy(recommendation)
        result['workflow_adaptation'] = {**match, 'restricted': False,
            'status': 'unavailable', 'reason': 'existing_growth_candidates_unavailable'}
        return result
    available, skipped = _eligible(rows, observation, reserve)
    preferred = False
    if mode == 'defer':
        available = []
    elif mode == 'prefer' and priorities:
        rank = {phase: index for index, phase in enumerate(priorities)}
        ranks = [(min((rank[part] for part in _parts(row) if part in rank), default=len(rank)), row)
                 for row in available]
        highest = min((value for value, _ in ranks), default=len(rank))
        if highest < len(rank):
            available = [row for value, row in ranks if value == highest]
            preferred = True
    result = deepcopy(recommendation)
    result['workflow_candidates'] = deepcopy(available)
    result['workflow_adaptation'] = {**match, 'restricted': True, 'status': 'applied',
        'original_phase': recommendation.get('phase'), 'skipped': skipped,
        'preferred_candidate_found': preferred}
    selected = None
    if available:
        # Explicit user priority can prefer a lower-scoring, but still valid,
        # existing purchase. Otherwise preserve the original model's choice.
        original = [row for row in available if row['phase'] == recommendation.get('phase')]
        if preferred:
            selected = max(available, key=lambda row: (row['score'], -row['cost']))
        elif original:
            selected = max(original, key=lambda row: row['score'])
        elif recommendation.get('phase') is not None:
            baseline = recommendation.get('baseline_score')
            improving = [row for row in available if type(baseline) in (int, float) and row['score'] > baseline]
            if improving:
                selected = max(improving, key=lambda row: (row['score'], -row['cost']))
    if selected is None:
        result.update(phase=None, cost=0, training=None, purchase_phases=[],
            points_after_purchase=observation['points'],
            reason='workflow_defer_optional_adaptation' if mode == 'defer' else 'workflow_keep_adaptation_points',
            budget={'purchase_selected': False, 'workflow_reserve_points': reserve})
        result.pop('after_action_id', None)
        result.pop('score', None)
    else:
        result.update(deepcopy(selected))
        result.update(phase=selected['phase'], cost=selected['cost'],
            purchase_phases=list(_parts(selected)), points_after_purchase=observation['points'] - selected['cost'],
            after_action_id=selected.get('after_action_id') or 'train:' + str(selected.get('training')),
            reserved_race_points=selected.get('imminent_required', (selected.get('budget') or {}).get('deadline_total', 0)),
            reason='workflow_optional_adaptation_preferred' if preferred else 'workflow_optional_adaptation_reserved')
        if 'french_three' in _parts(selected):
            result.update(language_debuff_deferred=False, language_deferral_basis=None)
    record_selection(result, selected)
    return result
