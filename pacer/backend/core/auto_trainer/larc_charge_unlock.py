"""Price access to charging after the first two actual game actions."""

from core.auto_trainer.larc_participant_evidence import training_rows
from core.auto_trainer.larc_recovery import period_value


def charge_unlock_window(state,goal):
    from core.auto_trainer.larc_policy import opening_charge_enabled
    if not opening_charge_enabled(goal):return None
    period=period_value(state)
    remaining=state.scenario_state.get('larc_remaining_turns')
    if (state.scenario_state.get('scenario')!='larc' or period.get('year_index')!=1
            or period.get('pre_debut') is not True or type(remaining) is not int or remaining not in {10,11}):
        return None
    for candidate in state.training_options:
        view = training_rows(candidate)
        if view is None:continue
        for person in view[0]:
            charge=person.get('charge') or {}
            if charge.get('status')=='accepted' and any(type(charge.get(key)) is int and charge[key]>0 for key in ('filled_cells','preview_cells')):
                return None  # Real charging evidence takes precedence over the opening prior.
    units=goal.larc_policy.get('opening_charge_prior_units',3.)
    from core.auto_trainer.larc_phase import phase_weights
    credit=units*phase_weights(state,goal)['net_charge']*.75
    return {'kind':'opening_charge_unlock','game_action':12-remaining,
        'remaining_to_debut':remaining,'actions_until_unlock':remaining-9,
        'unlock_game_action':3,'estimated_charge_units':units,'forecast_credit':round(credit,4),
        'timing_basis':'user_scenario_rule_and_observed_pre_debut_remaining_turns',
        'gain_basis':'discounted_charge_opportunity_prior; future_layout_unknown'}
