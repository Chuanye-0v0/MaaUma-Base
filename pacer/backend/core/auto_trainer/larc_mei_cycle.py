"""First-year Mei setup opportunity, not a guaranteed future training reward.

The encounter rates and seven-bond touch are bounded planning priors. Sixty
bond unlocks the second appearance; it is NOT a gate for random charging.
"""

from core.auto_trainer.larc_participant_evidence import training_rows
from math import comb, ceil

from core.auto_trainer.larc_calendar import domestic_action_days, period_ordinal
from core.auto_trainer.larc_recovery import period_value


SINGLE_ENCOUNTER_PRIOR = .35
DOUBLE_ENCOUNTER_PRIOR = .55
TOUCH_RATIO_PRIOR = .07


def enabled(state, goal):
    return (getattr(goal, 'scenario', None) == 'larc'
        and getattr(goal, 'larc_policy', {}).get('variant') == 'baseline'
        and period_value(state).get('year_index') == 1)


def contact_stage(state, source):
    record = state.scenario_state.get('larc_mei_contact') or {}
    if not isinstance(record, dict) or record.get('run_session_id') != source.get('run_session_id'):
        return 'unknown'
    status = record.get('status')
    if status == 'unrecorded':
        # Absence of a record is not proof about manual play. Give the first
        # contact preference, rather than also promising the subsequent proc.
        return 'first_contact_unconfirmed'
    if status == 'unlock_event_seen':
        return 'subsequent'
    turn = record.get('turn_index')
    if status == 'training_selected' and type(turn) is int and 0 <= turn < state.turn:
        return 'subsequent'
    return 'unknown'


def mei_bond(state, candidate, source):
    if (not isinstance(source, dict) or source.get('turn_index') != state.turn
            or not source.get('run_session_id') or not (source.get('absolute_path') or source.get('path'))
            or not (source.get('sha256') or source.get('source_sha256'))):
        return None
    view = training_rows(candidate, typed=True)
    if view is None:
        return None
    ratios = []
    for person in view[0]:
        identity, role, bond = (person.get(key) or {} for key in ('identity', 'role', 'bond'))
        ratio = bond.get('fill_ratio_estimate')
        if (identity.get('status') == 'known' and identity.get('character_id') == 'satake_mei'
                and role.get('status') == 'accepted' and role.get('kind') == 'friend'
                and bond.get('status') == 'accepted' and type(ratio) in {int, float} and 0 <= ratio <= 1):
            ratios.append(ratio)
    return ratios[0] if ratios and all(ratio == ratios[0] for ratio in ratios) else None


def encounter_prior(state, source):
    """Same-run identity/bond needed; duplicate pages are not extra procs."""
    ratios = []
    for option in state.training_options:
        capture = option.raw_data.get('capture', {})
        if capture.get('run_session_id') != source.get('run_session_id'):
            continue
        ratio = mei_bond(state, option, capture)
        if ratio is not None:
            ratios.append(ratio)
    if not ratios or max(ratios) - min(ratios) > .08:
        return None
    return DOUBLE_ENCOUNTER_PRIOR if min(ratios) >= .6 else SINGLE_ENCOUNTER_PRIOR


def startup_value(state, candidate, source, weights, *, existing_score):
    result = {'score': existing_score, 'threshold_ratio': .6, 'touch_ratio_prior': TOUCH_RATIO_PRIOR,
        'basis': 'existing_startup_credit', 'score_kind': 'setup_opportunity_estimate_not_game_reward',
        'separate_startup_credit': 0.}
    ratio = mei_bond(state, candidate, source)
    if ratio is None or ratio >= .6:
        return dict(result, reason='mei_bond_unknown_or_coverage_already_started')
    period = period_value(state)
    stop = 23 if state.scenario_state.get('larc_ss_planning_horizon') == 'junior_cutoff_discard' else 24
    now = period_ordinal(period)
    if now is not None:
        remaining = len(domestic_action_days(now, stop))
    elif period.get('pre_debut') is True:
        pre = state.scenario_state.get('larc_remaining_turns')
        remaining = len(domestic_action_days(12, stop)) + (pre if type(pre) is int and pre >= 0 else 0)
    else:
        return dict(result, reason='first_year_calendar_unknown')
    # Exclude this contact and the final SS action. An extra appearance on the
    # final charging action must still have somewhere to redeem its battery.
    horizon = max(0, min(22, remaining - 2))
    needed = max(1, ceil((.6 - ratio - 1e-9) / TOUCH_RATIO_PRIOR))
    # One contact now saves the (needed-th) later hit. At future action j the
    # two routes differ only when exactly needed-1 previous hits occurred.
    extra = sum(comb(j, needed - 1) * SINGLE_ENCOUNTER_PRIOR ** (needed - 1)
        * (1 - SINGLE_ENCOUNTER_PRIOR) ** (j - needed + 1)
        * (DOUBLE_ENCOUNTER_PRIOR - SINGLE_ENCOUNTER_PRIOR)
        for j in range(needed - 1, horizon))
    unit = weights.get('friend_contact', weights['effective_bond'])
    # Replaces the existing preparation credit; it is not stacked on top.
    score = max(existing_score if horizon else 0., min(unit, extra * unit))
    return dict(result, score=round(score, 4), previous_score=existing_score,
        threshold_ratio=.6, current_ratio=ratio, contacts_needed_estimate=needed,
        remaining_first_year_actions=remaining, future_charge_actions=horizon,
        extra_mei_opportunities_estimate=round(extra, 6), opportunity_unit_value=unit,
        encounter_single_prior=SINGLE_ENCOUNTER_PRIOR, encounter_double_prior=DOUBLE_ENCOUNTER_PRIOR,
        basis='marginal_earlier_double_appearance_replaces_existing_startup_credit',
        reason='bounded_first_year_coverage_startup', separate_startup_credit=0.)
