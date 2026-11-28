"""A soft preference for using a weak layout for concentrated recovery.

This is a policy preference, not an extra promised training or energy reward.
It addresses the short horizon's tendency to postpone concentrated recovery
for a little wisdom heal or an ordinary, risky consuming training.
"""

from core.auto_trainer.rejection_policy import is_hard_rejected
from core.auto_trainer.larc_participant_evidence import training_rows
import math

from core.auto_trainer.larc_recovery import PLANNING_START_ENERGY


# Ordinary timing credit is below one future training value. A risky training
# immediately followed by recovery can use up to one such value. This never
# requires/rejects an action or assigns a rest quota.
RECOVERY_TIMING_CREDIT = .75
MAX_RECOVERY_TIMING_CREDIT = 1.
PROGRESS_COMPONENTS = (
    "effective_bond", "training_hint_bond", "friend_startup",
    "friend_coverage_preparation", "net_charge", "newly_full", "ss_preparation",
    "cycle_reward_preparation", "charming_charge_progress", "future_recovery_cycle_progress",
)


def _unit(value):
    return min(1., max(0., value))


def _entry_recovery_already_priced(horizon, components, *, kinds=('expedition_entry',)):
    """A complete equal-budget route has already paid this recovery's value."""
    steps = horizon.get('steps')
    if (horizon.get('active') is not True or horizon.get('kind') not in kinds
            or type(steps) is not int or steps <= 0):
        return False
    for key in ('future_route_before', 'future_route_after'):
        route = horizon.get(key)
        if not isinstance(route, (list, tuple)) or len(route) != steps:
            return False
        if kinds != ('expedition_entry',) and any(not isinstance(step, str) or not step for step in route):
            return False
    before, after = horizon.get('future_value_before'), horizon.get('future_value_after')
    delta = horizon.get('continuation_value_delta')
    recovery, continuity = components.get('recovery'), components.get('energy_continuity')
    if any(type(value) not in {int, float} or not math.isfinite(value)
           for value in (before, after, delta, recovery, continuity)):
        return False
    # Rounded logs may differ by a final decimal. This is a value-coverage
    # check, not another bonus or a new prohibition on a recovery action.
    return (abs(after - before - delta) <= .001
            and abs(recovery + continuity - delta) <= .001)


def _friendship_evidence(candidate):
    if candidate.friendship is not None:
        return "observed" if candidate.friendship else "observed_absent"
    view = training_rows(candidate)
    if view is not None:
        for person in view[0]:
            role, bond = person.get("role") or {}, person.get("bond") or {}
            if (role.get("status") == "accepted" and role.get("kind") == "support"
                    and role.get("icon_type") == candidate.training_type and bond.get("status") == "accepted"
                    and (bond.get("is_max") is True or bond.get("color") in {"orange", "yellow"})):
                # Eligible partner is useful evidence, not a newly observed
                # rainbow flag. Keep this distinction in the decision log.
                return "eligible_partner_observed"
    return "unknown"


def _imminent_progress(candidate, parts):
    """Keep imminent friendship/SS/recovery reasons to take the current page."""
    value=sum(max(0.,parts.get(key,0.)) for key in PROGRESS_COMPONENTS
              if key not in {"effective_bond","net_charge"})
    view = training_rows(candidate)
    bonds=[]
    if view is not None:
        for person in view[0]:
            role,bond=person.get("role") or {},person.get("bond") or {}
            ratio=bond.get("fill_ratio_estimate")
            if (role.get("status")=="accepted" and role.get("kind")=="support"
                    and bond.get("status")=="accepted" and type(ratio) in {int,float} and ratio<.8):
                bonds.append(ratio)
    if bonds:
        # One-contact estimate, not a promised friendship proc next turn.
        value+=max(0.,parts.get("effective_bond",0.))*sum(.73<=b<.8 for b in bonds)/len(bonds)
    return value


def apply_recovery_timing_preference(state, goal, scored):
    trainings = [row for row in scored if row.get("training_type") and not is_hard_rejected(row)]
    recoveries = [row for row in scored if row.get("action") in {"outing", "rest"} and not is_hard_rejected(row)]
    if not trainings or not recoveries:
        return
    best = max(trainings, key=lambda row: row["score"])
    plan = best["planning"]
    is_wisdom = best["training_type"] == "wisdom"
    failure = best.get("failure_rate") or 0.
    if not is_wisdom and (failure <= 0 or plan["training_vitality_percent"] >= 0):
        return
    horizon = plan["energy_horizon"]
    if not horizon.get("active"):
        return
    from core.auto_trainer.larc_risk import strategy_v4_enabled
    if strategy_v4_enabled(state, goal) and horizon.get('steps', 0) <= 0:
        # An explicitly ending career has no later training that can spend
        # extra energy. Keep outing attributes separate from unused recovery.
        return
    # The current route already prices its final overseas training, unless
    # the best current option still fails risk-free readiness before it.
    from core.auto_trainer.larc_policy import training_balance_v2_enabled
    reference = horizon.get("future_numeric_training_value", horizon.get("future_training_value", 0.))
    cost = horizon.get("estimated_training_cost_percent", 0.)
    if reference <= 0 or cost <= 0:
        return
    heal = max(0., plan["training_vitality_percent"])
    buffer = plan["scheduled_recovery_percent"] + plan["reserve_discount"] * plan["held_ss_recovery_percent"]
    overseas_low_hp_wisdom = (
        training_balance_v2_enabled(goal)
        and is_wisdom
        and horizon.get("kind") == "expedition_continuity"
        and horizon.get("steps", 0) >= 1
        and 0 < heal < cost
        and state.vitality + heal + buffer < PLANNING_START_ENERGY[goal.risk_tolerance]
    )
    risky_pre_final_overseas = (
        horizon.get("kind") == "expedition_continuity"
        and horizon.get("steps") == 1
        and failure > 0.0
        and state.vitality + heal + buffer < PLANNING_START_ENERGY[goal.risk_tolerance]
    )
    if horizon.get("kind") == "expedition_continuity" and horizon["steps"] <= 1 and not risky_pre_final_overseas and not overseas_low_hp_wisdom:
        return
    # Keep the ordinary timing preference and weak-yield check on their
    # numeric scale. The non-bridging small-heal case below uses a separate
    # opportunity scale without changing any forecast rewards.
    # One useful consuming training followed by another choice is the local
    # readiness reference, not a new training failure-rate restriction.
    need = _unit((PLANNING_START_ENERGY[goal.risk_tolerance] + cost - state.vitality - buffer) / cost)
    small_heal = _unit(1. - heal / cost)
    parts = best["components"]
    direct = sum(parts.get(key, 0.) for key in ("stats", "skill_pt", "high_stat_target_scarcity"))
    # A high, possibly unreachable soft target must not by itself turn a weak
    # layout into a good one. The deficit premium is deliberately excluded.
    weak_yield = _unit(2. - direct / reference)
    progress = sum(max(0., parts.get(key, 0.)) for key in PROGRESS_COMPONENTS)
    # Preserve the original bond/charge scores. Their progress attenuates this
    # timing preference smoothly instead of switching it off near one average
    # training's value, even when no member becomes ready or restores energy.
    low_progress = reference / (reference + progress)
    candidate = next(item for item in state.training_options if item.training_type == best["training_type"])
    friendship = _friendship_evidence(candidate)
    friendship_factor = .5 if friendship in {"observed", "eligible_partner_observed"} else 1.
    from core.auto_trainer.larc_recovery import period_value
    route=horizon.get("future_route_after") or []
    period=period_value(state) if isinstance(getattr(state,'scenario_state',None),dict) else {}
    opening_bridge=(training_balance_v2_enabled(goal) and is_wisdom and period.get('year_index')==1
        and period.get('pre_debut') is True and horizon.get('kind')=='rolling_recovery'
        and 0<heal<cost and state.vitality+heal+buffer<PLANNING_START_ENERGY[goal.risk_tolerance]+cost
        and any(step in {'rest','outing'} for step in route[:2]))
    still_needs_recovery=(training_balance_v2_enabled(goal) and is_wisdom
        and horizon.get("kind")=="rolling_recovery" and 0<heal<cost
        and (state.vitality+heal+buffer<PLANNING_START_ENERGY[goal.risk_tolerance] or opening_bridge)
        and any(step in {"rest","outing"} for step in route[:2]))
    risky_consuming_before_recovery=(training_balance_v2_enabled(goal) and not is_wisdom
        and failure>0 and plan["training_vitality_percent"]<0
        and horizon.get("kind")=="rolling_recovery" and route and route[0] in {"rest","outing"}
        and state.vitality+plan["training_vitality_percent"]+buffer<PLANNING_START_ENERGY[goal.risk_tolerance])
    timing_reference=reference
    imminent_progress=progress
    if still_needs_recovery or risky_consuming_before_recovery:
        # Taking this page has not removed the next recovery. Ordinary bond
        # and partial charge already earn their training value; they should
        # not also suppress moving that recovery ahead of a weak risky page.
        # Use the full observed-layout opportunity scale for the timing
        # preference; imminent friendship/SS progress still attenuates it.
        # Keep weak_yield on its numeric scale so strong wisdom stays strong.
        timing_reference=max(reference,horizon.get("future_training_value",reference))
        imminent_progress=_imminent_progress(candidate,parts)
        low_progress=timing_reference/(timing_reference+imminent_progress)
    # This is a bounded timing preference, not a second expected failure loss.
    # Respect the configured risk budget; do not penalize risk-free consuming
    # options or force an outing just because a training has some failure risk.
    risk_budget_fraction = _unit(failure / max(1., best["failure_rate_limit"]))
    risky_small_heal = still_needs_recovery and failure > 0
    risk_factor = 1. if is_wisdom else risk_budget_fraction
    if risky_small_heal or risky_consuming_before_recovery:
        # Spending half the configured failure budget just before an already
        # needed recovery gets the strongest timing multiplier.
        # This is risk aversion about WHEN to recover, not another prediction
        # of failed-training losses (already present in the training score).
        risk_factor = 1. + _unit(2. * risk_budget_fraction)
    bridge_gap=_unit((PLANNING_START_ENERGY[goal.risk_tolerance]+cost-state.vitality-heal-buffer)/cost)
    delay_factor=1.+.5*bridge_gap if opening_bridge and failure==0 else 1.
    from core.auto_trainer.larc_policy import policy_branch
    predebut_recovery_bridge = policy_branch(goal) == 'baseline' and opening_bridge
    measured_small_heal_factor = small_heal
    if predebut_recovery_bridge:
        # This small wisdom heal is already valued by energy_continuity, yet
        # the next two actions still include dedicated recovery. It has not
        # substituted that recovery. Do not reduce the timing preference a
        # second time merely because some fraction of one training cost was
        # healed. Numeric quality and imminent startup/SS progress still
        # attenuate it, and the ordinary one-opportunity cap still applies.
        small_heal = 1.
        # First verified Mei contact is startup, not ordinary weak numerical
        # yield. Keep that existing bounded credit relevant to timing too.
        imminent_progress += max(0., parts.get('mei_initial_contact_preference', 0.))
        low_progress = timing_reference / (timing_reference + imminent_progress)
    preference = (RECOVERY_TIMING_CREDIT * timing_reference * need * small_heal
                  * weak_yield * low_progress * friendship_factor * risk_factor * delay_factor)
    preference = min(preference, MAX_RECOVERY_TIMING_CREDIT * timing_reference)
    if period.get('year_index') == 2 and period.get('month') == 9 and period.get('half') == 'second':
        from core.auto_trainer.larc_strategy_extension import active_provider
        from core.auto_trainer.larc_strategy_params import params_for_goal
        timing_scale = float(active_provider().last_overseas_timing_scale(params_for_goal(goal)))
        if timing_scale != 1.0:
            preference *= max(0.0, timing_scale)
    for row in recoveries:
        # Use the same clipped, capacity-adjusted recovery as the route model.
        # Don't spend a recovery turn if it provides no additional training room.
        horizon = row["planning"]["energy_horizon"]
        outcomes = horizon.get("rest_outcomes") or [{
            "after_vitality_percent": horizon.get("after_vitality_percent", state.vitality),
            "planning_weight": 1.}]
        # Clip and value each 30/50/70-point rest separately. Rest earns no
        # mood recovery or friend-chain credit by borrowing the outing path.
        extra_room = sum(outcome["planning_weight"] * _unit(
            (outcome["after_vitality_percent"] - min(100., state.vitality + heal)) / cost)
            for outcome in outcomes)
        value = round(preference * extra_room, 4)
        uncapped_value = value
        energy_use_cap = None
        if (strategy_v4_enabled(state, goal) and period.get('year_index') in {2, 3}
                and period.get('month') in {7, 8, 9}):
            # Extra bar room is not extra usable training. In a finite
            # expedition both branches may already realize the same future
            # training value; a timing preference cannot revive that zero
            # marginal benefit merely because 30/50/70 points fit in the bar.
            delta = horizon.get('continuation_value_delta')
            if type(delta) not in {int, float}:
                before, after = horizon.get('future_value_before'), horizon.get('future_value_after')
                delta = after - before if type(before) in {int, float} and type(after) in {int, float} else 0.
            energy_use_cap = max(0., delta)
            value = round(min(value, energy_use_cap), 4)
        from core.auto_trainer.larc_strategy_params import unified_larc_enabled
        general_route = unified_larc_enabled(goal)
        priced_kinds = (('rolling_recovery', 'expedition_entry', 'expedition_continuity')
                        if general_route else ('expedition_entry',))
        route_credit_deduplicated = (general_route
            and period.get('year_index') in ({1, 2, 3} if general_route else {2, 3})
            and not opening_bridge and not still_needs_recovery
            and not risky_small_heal and not risky_consuming_before_recovery
            and not risky_pre_final_overseas
            and not overseas_low_hp_wisdom
            and _entry_recovery_already_priced(horizon, row['components'], kinds=priced_kinds))
        before_route_dedup = value
        if route_credit_deduplicated:
            # Keep the real recovery, continuation, mood and outing-chain
            # components. Only this generic readiness preference is redundant.
            value = 0.
        row["components"]["recovery_timing_preference"] = value
        row["score"] = round(sum(row["components"].values()), 4)
        row["planning"]["recovery_timing"] = {
            "score": value, "compared_training": best["action_id"],
            "future_training_reference": reference, "training_direct_value": round(direct, 4),
            "timing_opportunity_reference": round(timing_reference,4),
            "imminent_progress_value": round(imminent_progress,4),
            "recovery_still_needed_after_wisdom": bool(still_needs_recovery),
            "overseas_low_hp_wisdom": bool(overseas_low_hp_wisdom),
            'opening_small_heal_bridge':bool(opening_bridge),'recovery_delay_factor':round(delay_factor,4),
            "training_progress_value": round(progress, 4), "friendship_evidence": friendship,
            "readiness_need_factor": round(need, 4), "small_heal_factor": round(small_heal, 4),
            "weak_yield_factor": round(weak_yield, 4), "low_progress_factor": round(low_progress, 4),
            "risk_timing_factor": round(risk_factor, 4),
            "risky_small_heal_timing": bool(risky_small_heal),
            "risky_consuming_before_recovery": bool(risky_consuming_before_recovery),
            "failure_budget_fraction": round(risk_budget_fraction, 4),
            "timing_credit_cap": round(MAX_RECOVERY_TIMING_CREDIT * timing_reference, 4),
            "additional_training_room_factor": round(extra_room, 4),
            **({'predebut_recovery_bridge': True,
                'observed_small_heal_factor': round(measured_small_heal_factor, 4),
                'recovery_bridge_basis': 'small_heal_priced_by_route; dedicated_recovery_still_within_two_actions'}
               if predebut_recovery_bridge else {}),
            **({'uncapped_timing_score': uncapped_value,
                'future_realizable_recovery_gain_cap': round(energy_use_cap, 4)}
               if energy_use_cap is not None else {}),
            **({'generic_timing_before_route_dedup': before_route_dedup,
                'route_credit_deduplicated': True,
                'route_credit_basis': 'recovery_and_continuity_already_equal_full_route_delta'}
               if route_credit_deduplicated else {}),
            **({'route_credit_kind': horizon.get('kind')}
               if route_credit_deduplicated and general_route else {}),
            "basis": "bounded recovery timing preference; not extra forecast yield or an action quota",
        }
