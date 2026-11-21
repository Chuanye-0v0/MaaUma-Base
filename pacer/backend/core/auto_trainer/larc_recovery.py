"""Shared short-horizon energy valuation, not a forecast of random events.

Vitality in TrainingTurnState is bar percent. Reward amounts are game points.
The two 20-point SS icon rules come from the user's teaching run. Missing
training costs use labelled policy estimates until perception supplies a delta.
"""
from __future__ import annotations

from statistics import median

from core.auto_trainer.larc_vitality_preview import accepted_preview
from core.auto_trainer.larc_capacity import planning_capacity, recovery_delta, capacity_bounds
from core.auto_trainer.larc_calendar import FORCED_RACES, free_action_days, is_overseas, period_ordinal

POLICY_VERSION = "larc_local_v1_18_2"
RECOVERY_KINDS = {"motivation_and_vitality", "max_vitality_and_recovery"}

# Soft planning preferences, not game failure thresholds or mandatory reserves.
PLANNING_START_ENERGY = {"low": 45., "medium": 35., "high": 25.}
# Planning preference only: today's strong wisdom layout is not a promise
# about the next one. Keep some opportunity cost for needing recovery later.
FUTURE_WISDOM_VALUE_CREDIT = .5
FUTURE_WISDOM_RECOVERY_CREDIT = .65


def critical_window(state):
    value = period_value(state)
    year, month, half = value.get("year_index"), value.get("month"), value.get("half")
    now = period_ordinal(value)
    if year not in {2, 3} or now is None:
        return None
    entry, end = (year - 1) * 24 + 12, (year - 1) * 24 + 18
    ahead = free_action_days(now + 1, end)
    # As soon as the rolling four-action window reaches overseas, include
    # its five real training slots. Forced races never become spare SS turns.
    if month <= 6 and any(is_overseas(day) for day in ahead[:4]):
        domestic = free_action_days(now + 1, entry)
        return {"kind": "expedition_entry", "free_actions_including_current": 1 + len(domestic),
                "future_trainings": len(free_action_days(entry, end)),
                "race_before_entry": any(now < day < entry for day in FORCED_RACES)}
    if 7 <= month <= 9 and ahead:
        return {"kind": "expedition_continuity", "free_actions_including_current": 1,
                "future_trainings": len(ahead), "race_before_entry": False}
    return None


def period_value(state):
    period = state.scenario_state.get("larc_period", {})
    return period.get("value", {}) if period.get("status") == "verified" else {}


def points_to_percent(state, points):
    # One capacity for every branch, including consumption. An unknown cap is
    # a labelled midpoint of the same range, never 100 for costs/120 for heals.
    return points * 100 / planning_capacity(state)


def scheduled_recovery_after(state, period):
    """Classic December's fixed 30 points arrive AFTER that day's action."""
    return (points_to_percent(state, 30) if period.get('year_index') == 2
            and period.get('month') == 12 and period.get('half') == 'second' else 0.)


def ss_recovery(preview):
    from core.auto_trainer.larc_ss_visible import visible_ss_participants, ss_victory_evidence
    rows = visible_ss_participants(preview)
    if not rows or len(rows) != preview.get("ready_count") or not ss_victory_evidence(preview)['guaranteed']:
        return {"points": 0, "observed_points": 0, "rule_points": 0, "max_increase": 0}
    observed = rule = maximum = 0
    for row in rows:
        reward = row.get("current_reward", {})
        if reward.get("status") != "accepted" or reward.get("kind") not in RECOVERY_KINDS:
            continue
        if reward.get("kind") == "max_vitality_and_recovery":
            maximum += 4
        amount = reward.get("amount")
        if type(amount) is int and amount >= 0:
            observed += amount
        elif amount is None:
            rule += 20
    return {"points": observed + rule, "observed_points": observed, "rule_points": rule, "max_increase": maximum}


def planning_context(state, *, include_held_ss=True):
    value = period_value(state)
    year, month, half = value.get("year_index"), value.get("month"), value.get("half")
    abroad = year in {2, 3} and type(month) is int and 7 <= month <= 9
    final_training = year == 3 and month == 9 and half == "second"
    june = year in {2, 3} and month == 6
    urgency = .8 if june and half == "second" else .4 if june else 0.
    # The December recovery discussed in the teaching run is automatic after
    # the classic-year final turn. Unlike held SS, it consumes no later action.
    scheduled = points_to_percent(state, 30) if year == 2 and month == 12 and half == "second" else 0.
    preview = state.scenario_state.get("larc_ss_preview", {})
    # The live SS callback already binds its participants to the current
    # frame; saved packets carry the same binding at preview level instead.
    source = preview.get("source") or preview.get("participants_observation", {}).get("source", {})
    captures = [c.raw_data.get("capture", {}) for c in state.training_options]
    # A held SS cannot bridge the last domestic training into an expedition:
    # SS is unavailable there, even though its preview is visible today.
    window = critical_window(state)
    last_domestic = (window is not None and window["kind"] == "expedition_entry"
                     and window["free_actions_including_current"] == 1)
    bound = (include_held_ss and not abroad and not last_domestic
             and source.get("turn_index") == state.turn
             and bool(source.get("path") or source.get("absolute_path"))
             and bool(source.get("sha256") or source.get("source_sha256"))
             and bool(source.get("run_session_id")) and len(captures) == 5
             and all(c.get("run_session_id") == source["run_session_id"]
                     and c.get("turn_index") == state.turn for c in captures))
    reward = ss_recovery(preview) if bound else {"points": 0, "max_increase": 0}
    from core.auto_trainer.larc_ss_visible import ss_victory_evidence
    count=preview.get('ready_count')
    available = bound and count==5 and ss_victory_evidence(preview)['guaranteed']
    # A partial group's visible reward remains evidence, not an immediately
    # usable future action. Filling its missing slots and the later roster are
    # unknown until a fresh full preview is read.
    unready = ({'ready_count':count,'points':reward['points'],'max_increase':reward['max_increase'],
                'reason':'full_group_required; fill_time_and_future_roster_unknown'}
               if bound and type(count) is int and 0<count<5 else None)
    if not available:reward={'points':0,'max_increase':0}
    # Preserve the future resource before today's overflow. The route applies
    # both points and the cap increase when that SS is actually used.
    held = points_to_percent(state, reward["points"])
    return {"held_ss_recovery_percent": round(held, 4),
            "held_ss_available": available,
            "held_ss_ready_count":count if available else None,
            "held_ss_reward": {"points": reward["points"], "max_increase": reward["max_increase"]},
            "unready_ss_reward":unready,
            "scheduled_recovery_percent": round(scheduled, 4),
            "reserve_discount": .5, "pre_expedition_weight": urgency,
            "final_training": final_training, "capacity_bounds": list(capacity_bounds(state)),
            "planning_capacity": planning_capacity(state),
            "basis": "one-turn resource reserve; SS still costs a later turn; no random heal assumed"}


def recovery_weight(state, *, include_held_ss=True):
    context = planning_context(state, include_held_ss=include_held_ss)
    if context["final_training"]:
        return 0., 0.
    buffer = context["scheduled_recovery_percent"] + context["reserve_discount"] * context["held_ss_recovery_percent"]
    available = min(100., max(0., state.vitality) + buffer)
    urgency = context["pre_expedition_weight"]
    return .4 + 1.6 * (1 - available / 100) + urgency, urgency


def recovery_value(state, delta_percent, *, include_held_ss=True):
    """Value actual usable energy, with diminishing returns and overflow clipped.

    A held SS only changes the marginal need for energy. Its rewards are never
    added to a training action. On consuming that SS, exclude it from reserve.
    """
    context = planning_context(state, include_held_ss=include_held_ss)
    if context["final_training"]:
        return 0.
    before = min(100., max(0., state.vitality))
    after = min(100., max(0., before + delta_percent))
    buffer = context["scheduled_recovery_percent"] + context["reserve_discount"] * context["held_ss_recovery_percent"]
    before, after = min(100., before + buffer), min(100., after + buffer)
    urgency = context["pre_expedition_weight"]
    def potential(energy):
        return (2. + urgency) * energy - .008 * energy * energy
    return round(potential(after) - potential(before), 4)


def estimated_training_cost(state):
    """Net observed costs already contain Mei/adaptation reductions."""
    costs = []
    for candidate in state.training_options:
        if candidate.training_type == "wisdom":
            continue
        capture = candidate.raw_data.get("capture", {})
        larc = candidate.scenario_mechanics.get("larc")
        preview = larc.get("vitality_preview", {}) if isinstance(larc, dict) else {}
        source = preview.get("source", {}) if isinstance(preview, dict) else {}
        if (accepted_preview(preview) and preview.get("training_type") == candidate.training_type
                and capture.get("turn_index") == state.turn and capture.get("run_session_id")
                and capture.get("sha256") and isinstance(source, dict)
                and all(source.get(k) == capture.get(k) for k in ("run_session_id", "turn_index", "sha256"))
                and preview["delta_percent"] < 0 and preview["after_percent"] > 0):
            costs.append(-preview["delta_percent"])
    return (median(costs), "median_observed_net_cost") if costs else (
        points_to_percent(state, 20), "policy_estimate_20_points")


def critical_energy_plan(state, delta_percent, *, future_training_value, risk_tolerance,
                         wisdom_recovery=None, include_held_ss=True):
    """Calendar demand diagnostics, alongside the shared action-route score."""
    window = critical_window(state)
    if window is None:
        return {"active": False, "score": 0.}
    cost, cost_basis = estimated_training_cost(state)
    context = planning_context(state, include_held_ss=include_held_ss)
    buffer = context["scheduled_recovery_percent"] + context["reserve_discount"] * context["held_ss_recovery_percent"]
    before = min(100., max(0., state.vitality))
    after = min(100., max(0., before + delta_percent))
    reserve = PLANNING_START_ENERGY[risk_tolerance]
    # Total demand can exceed one full bar: that is precisely when a recovery
    # action is needed during the expedition. Don't hide it by clipping at 100.
    target = reserve + cost * (window["future_trainings"] - 1)
    # The same 30/50/70 planning scenarios used by current and future rest
    # have a 50-point midpoint. No individual rest outcome is promised.
    recovery = points_to_percent(state, 50)
    # Replacing a consuming training with recovery both restores energy and
    # avoids that training's cost. Wisdom keeps its numeric yield; spending
    # a future turn on it must not be priced as a zero-yield rest.
    replacement_energy = recovery + cost
    lost_value, recovery_mode = future_training_value, "rest"
    credited_wisdom_value = None
    if wisdom_recovery is not None:
        wisdom_energy, wisdom_value = wisdom_recovery
        wisdom_energy *= FUTURE_WISDOM_RECOVERY_CREDIT
        credited_wisdom_value = min(wisdom_value, future_training_value * FUTURE_WISDOM_VALUE_CREDIT)
        wisdom_loss = max(0., future_training_value - credited_wisdom_value)
        if (wisdom_energy > 0 and
                wisdom_loss / (wisdom_energy + cost) < lost_value / replacement_energy):
            replacement_energy = wisdom_energy + cost
            lost_value, recovery_mode = wisdom_loss, "wisdom"
    def replacement_plan(energy):
        deficit = max(0., target - min(100., energy + buffer))
        # Credit at most one future wisdom layout. Remaining recovery demand
        # keeps a rest-sized opportunity cost instead of repeating today's rainbow.
        wisdom_turns = min(1., deficit / replacement_energy) if recovery_mode == "wisdom" else 0.
        rest_turns = min(window["future_trainings"] - wisdom_turns,
                         max(0., deficit - wisdom_turns * replacement_energy) / (recovery + cost))
        return wisdom_turns + rest_turns, wisdom_turns * lost_value + rest_turns * future_training_value
    def capacity(energy):
        return min(window["future_trainings"], max(0., (energy + buffer - reserve) / max(cost, 1.) + 1))
    before_missed, before_loss = replacement_plan(before)
    after_missed, after_loss = replacement_plan(after)
    urgency = 1. / window["free_actions_including_current"]
    return {"active": True, **window,
            "score": round((before_loss - after_loss) * urgency, 4),
            "after_vitality_percent": round(after, 4), "target_energy_percent": round(target, 4),
            "estimated_training_cost_percent": round(cost, 4),
            "training_cost_basis": cost_basis,
            "planning_start_energy_percent": reserve, "rest_recovery_estimate_percent": round(recovery, 4),
            "future_training_value": round(future_training_value, 4),
            "estimated_recovery_mode": recovery_mode, "recovery_turn_opportunity_cost": round(lost_value, 4),
            "replacement_energy_percent": round(replacement_energy, 4),
            "wisdom_recovery_estimate": wisdom_recovery,
            "future_wisdom_value_credit": credited_wisdom_value,
            "future_wisdom_credit_limit_ratio": FUTURE_WISDOM_VALUE_CREDIT,
            "future_wisdom_recovery_credit_ratio": FUTURE_WISDOM_RECOVERY_CREDIT,
            "future_wisdom_max_credited_turns": 1,
            "future_value_basis": "median_current_reliable_non_wisdom_numeric_value",
            "estimated_trainings_supported": round(capacity(after), 4),
            "estimated_recovery_turns_before": round(before_missed, 4),
            "estimated_recovery_turns_after": round(after_missed, 4),
            "urgency": round(urgency, 4),
            "basis": "soft energy opportunity cost; future layouts, race energy changes and random heals unknown"}
