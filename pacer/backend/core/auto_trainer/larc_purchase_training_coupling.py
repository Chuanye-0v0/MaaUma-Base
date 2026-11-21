"""Use-specific facility purchases follow today's projected action ranking.

This only defers a facility payment; it never commits or executes a training,
rewrites receipts/pending, or turns future income into cash. Existing current
candidate scores supply the comparison, excluding the purchase-route tail.
"""
from core.auto_trainer.rejection_policy import is_hard_rejected
from math import isfinite

from core.auto_trainer.larc_adaptation import TRAINING_UPGRADES
from core.auto_trainer.larc_timeline_workflow import selection_key

TRAINING_PHASES = {phase: tab for tab, (phase, _) in TRAINING_UPGRADES.items()}


def _number(value):
    return type(value) in (int, float) and isfinite(value)


def _action(row):
    action = row.get('after_action_id')
    if isinstance(action, str) and action:
        return action
    tab = row.get('training')
    return 'train:' + tab if tab in TRAINING_UPGRADES else None


def _current_score(row):
    score = row.get('training_score_after_purchase')
    if score is None:
        score = row.get('action_score_after_purchase')
    return score if _number(score) else None


def current_action_coupling(row, *, purchase_rows, current_rows, goal):
    """Compare the same global basket, adding the local facility only to T.

    A global-only basket benefits all eligible actions and is never blocked by
    this facility-use rule. For a basket with T's Lv3, the legacy joint rows
    already supply each action's current score under the same global basket;
    the T row supplies T's additional projected facility gain. The finite
    career-route utility is intentionally not a current-action score.
    """
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    result = {'enabled': unified_larc_enabled(goal), 'active': False,
              'allowed': True, 'reason': 'revision_disabled',
              'training_action_forced': False, 'purchase_deferral_only': True,
              'future_route_tail_used_as_current_score': False,
              'cash_or_receipts_modified': False}
    if not result['enabled']:
        return result
    phases = set(row.get('purchase_phases') or [])
    facilities = phases & set(TRAINING_PHASES)
    if not facilities:
        return {**result, 'reason': 'global_upgrade_not_subject_to_facility_use_gate'}
    if len(facilities) != 1:
        return {**result, 'active': True, 'allowed': False,
                'reason': 'specific_facility_identity_unknown'}
    phase = next(iter(facilities));tab = TRAINING_PHASES[phase]
    action = 'train:' + tab
    globals_only = phases - facilities
    result.update(active=True, facility_phase=phase, facility_training=tab,
                  candidate_after_action_id=_action(row), global_basket=sorted(globals_only))
    if _action(row) != action:
        return {**result, 'allowed': False, 'reason': 'facility_and_projected_action_mismatch'}
    target_score = _current_score(row)
    if target_score is None:
        return {**result, 'allowed': False, 'reason': 'projected_current_facility_score_unknown'}
    eligible = {key: value for key, value in current_rows.items()
                if isinstance(value, dict) and not is_hard_rejected(value)
                and _number(value.get('score'))}
    if action not in eligible:
        return {**result, 'allowed': False, 'reason': 'facility_training_not_currently_eligible'}
    compared = []
    for key, base in eligible.items():
        if key == action:
            score = target_score
        elif not globals_only:
            score = base['score']
        else:
            same = [r for r in purchase_rows
                    if set(r.get('purchase_phases') or []) == globals_only
                    and _action(r) == key and _current_score(r) is not None]
            if not same:
                return {**result, 'allowed': False,
                        'reason': 'same_global_basket_current_comparison_unknown',
                        'missing_action_id': key}
            # Duplicate forecast rows must not hide a stronger current action.
            score = max(_current_score(r) for r in same)
        compared.append({**base, 'action_id': key, 'score': score})
    best = min(compared, key=selection_key)
    return {**result, 'allowed': best['action_id'] == action,
            'reason': ('facility_used_by_projected_current_best_action'
                       if best['action_id'] == action else 'specific_upgrade_not_used_by_current_best_action'),
            'projected_current_best_action_id': best['action_id'],
            'projected_current_best_score': best['score'],
            'facility_projected_current_score': target_score,
            'current_comparison': [{'action_id': r['action_id'], 'score': r['score'],
                                    'priority_rank': (r.get('score_workflow') or {}).get('priority_rank', 0)}
                                   for r in sorted(compared, key=selection_key)],
            'score_basis': 'same_current_action_pool_and_global_basket; no_extra_scorer_or_future_tail',
            'normal_post_purchase_observation_still_selects_actual_action': True}


def couple_purchase_rows(rows, *, purchase_rows, current_rows, goal):
    """Add bounded diagnostics and defer mismatched facility rows in a new arm."""
    result = []
    for row in rows:
        detail = current_action_coupling(row, purchase_rows=purchase_rows,
                                         current_rows=current_rows, goal=goal)
        new = {**row, 'current_training_purchase_coupling': detail}
        if detail['active'] and not detail['allowed']:
            if not is_hard_rejected(row) and not row.get('v3_rejection_reason'):
                new['v3_rejection_reason'] = detail['reason']
                new['v3_rejection_message'] = '本次升级对应训练并非购买后的当前首选，等实际使用该训练时再支付。'
            new['rejected'] = True
        result.append(new)
    return result
