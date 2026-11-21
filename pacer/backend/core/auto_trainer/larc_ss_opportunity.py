"""Calendar feasibility and discounted chances to spend further SS cycles.

Action shares are policy priors, not observed event probabilities or SS quotas.
Only an impossible calendar makes a reward categorically worthless.
"""
from core.auto_trainer.larc_recovery import period_value
from core.auto_trainer.larc_calendar import domestic_action_days, period_at, period_ordinal
from dataclasses import replace


WAIT_DISCOUNT = .8


def with_planning_horizon(state,goal):
    horizon=getattr(goal,'larc_policy',{}).get('ss_planning_horizon','full_career')
    from core.auto_trainer.runtime_control import current
    configured=getattr(current(),'ss_planning_horizon',None)
    if configured in {'full_career','junior_cutoff_discard'}:horizon=configured
    from core.auto_trainer.larc_policy import strategy_v4_enabled
    v4 = strategy_v4_enabled(goal)
    if (state.scenario_state.get('larc_ss_planning_horizon') == horizon
            and bool(state.scenario_state.get('larc_strategy_v4')) == v4):
        return state
    context = {**state.scenario_state, 'larc_ss_planning_horizon': horizon}
    if v4:
        context['larc_strategy_v4'] = True
    else:
        context.pop('larc_strategy_v4', None)
    return replace(state, scenario_state=context)


def _share(year, month):
    if year == 1 or year == 2 and month <= 6:
        return .25
    return .12 if year == 2 else .10


def ss_opportunity(state, *, ss_steps=1, earliest_actions=2, consumed_current=False):
    period = period_value(state)
    year, month, half = period.get("year_index"), period.get("month"), period.get("half")
    now = period_ordinal(period)
    redeem_period, overseas_delay, calendar_wait = None, 0, 0
    horizon=state.scenario_state.get('larc_ss_planning_horizon','full_career')
    cutoff=23 if horizon=='junior_cutoff_discard' else None
    if now is not None:
        # Last ordinary domestic action is senior June first (ordinal 58).
        # Derby (33), first Arc (42), and both expeditions cannot redeem SS.
        # Forced race timing matches the existing joint-trial race flow.
        slots = domestic_action_days(now,cutoff) if cutoff is not None else domestic_action_days(now)
        remaining = len(slots)
        expected = sum(_share(day // 24 + 1, day % 24 // 2 + 1) for day in slots)
        if consumed_current and now in slots:
            expected -= _share(year, month)
        if type(earliest_actions) is int and 1 <= earliest_actions <= remaining:
            destination = slots[earliest_actions - 1]
            redeem_period = period_at(destination)
            # Only unavailable race/overseas turns are extra waiting.
            # Ordinary training and SS actions have their own progress discount.
            overseas_delay = max(0, min(destination, 42) - max(now, 36) + 1)
            calendar_wait = destination - now + 1 - earliest_actions
    else:
        # Pre-debut has no exact half-month in the input. Do not invent a
        # deadline; an early-run prior only discounts very distant cycles.
        remaining, expected = None, 8. if year == 1 else 0.
        if cutoff is not None and period.get('pre_debut') is True:
            pre=state.scenario_state.get('larc_remaining_turns')
            known_late=len(domestic_action_days(12,cutoff))
            remaining=pre+known_late if type(pre) is int and pre>=0 else None
            expected=.25*(remaining if remaining is not None else known_late)
    impossible = remaining is not None and earliest_actions > remaining
    factor = (0. if impossible or ss_steps <= 0 else
              min(1., max(0., expected) / ss_steps) ** ss_steps)
    return {"factor": round(factor, 6), "remaining_domestic_actions": remaining,
            "expected_ss_opportunities": round(max(0., expected), 4),
            "ss_steps_required": ss_steps, "earliest_actions_required": earliest_actions,
            "redemption_period_estimate": redeem_period, "overseas_delay": overseas_delay,
            "calendar_wait": calendar_wait,
            "planning_horizon":horizon,"exclusive_end_period":period_at(cutoff) if cutoff is not None else None,
            "impossible_before_final_expedition": impossible,
            "basis": "calendar bound plus phase-dependent SS opportunity prior; not guaranteed openings"}


def ss_redemption(state, *, ss_steps=1, earliest_actions=2, consumed_current=False):
    """Shared waiting and destination-phase discounts for future SS value.

    The date is conditional on the supplied action estimate, never a scheduled
    SS or a guarantee of its participants. Unknown dates retain the old prior.
    Current SS payouts do not call this function; only future progress does.
    """
    from core.auto_trainer.larc_phase import PHASE_WEIGHTS, phase, phase_for_period

    opportunity = ss_opportunity(state, ss_steps=ss_steps, earliest_actions=earliest_actions,
                                 consumed_current=consumed_current)
    current_phase = phase(state)
    future_phase = (phase_for_period(opportunity["redemption_period_estimate"])
                    if opportunity["redemption_period_estimate"] else current_phase)
    current, future = PHASE_WEIGHTS[current_phase], PHASE_WEIGHTS[future_phase]
    ratios = {key: min(1., future[key] / current[key]) if current[key] > 0 else 0.
              for key in ("net_charge", "newly_full", "ss_preparation")}
    ratios["reward_preparation"] = ratios["net_charge"]
    # Cycle utility already has a capped phase factor. Scale that factor, not
    # uncapped charge points, so it is applied exactly once by the cycle scorer.
    current_cycle, future_cycle = min(1., current["net_charge"] / 5.), min(1., future["net_charge"] / 5.)
    ratios["recovery_cycle"] = min(1., future_cycle / current_cycle) if current_cycle > 0 else 0.
    wait = WAIT_DISCOUNT ** opportunity["calendar_wait"]
    factor = opportunity["factor"] * wait
    return {**opportunity, "opportunity_factor": opportunity["factor"],
            "wait_factor": round(wait, 6), "factor": round(factor, 6),
            "current_phase": current_phase, "redemption_phase_estimate": future_phase,
            "phase_factors": {key: round(value, 6) for key, value in ratios.items()},
            "value_factors": {key: round(factor * value, 6) for key, value in ratios.items()},
            "basis": "conditional SS calendar; waiting and destination-phase value; not a fixed SS schedule"}


def preparation_delay(ready, newly_full):
    # Average two newly ready members per further training is a policy
    # estimate. A real SS candidate is still evaluated from its actual roster.
    missing = max(0, 5 - ready - newly_full) if ready is not None else 2
    return .8 ** (1 + missing / 2.)
