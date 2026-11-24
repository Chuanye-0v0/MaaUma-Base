"""Local full-run values: current gains, startup progress and energy opportunity cost."""
from __future__ import annotations

from core.auto_trainer.larc_participant_evidence import effective_domestic_charge, training_rows

from statistics import median
from copy import deepcopy
from contextvars import ContextVar
from functools import wraps
from core.auto_trainer.pipeline_timing import timed_stage
from core.auto_trainer.larc_charming_value import charming_value
from core.auto_trainer.larc_policy import policy_version, skill_marginal, training_balance_v2_enabled
from core.auto_trainer.larc_strategy_params import unified_larc_enabled
from core.auto_trainer.larc_strategy_rules import (
    high_hp_wisdom_stamina_trade,
    stamina_deficit_scarcity,
    stamina_lesson_quality_adjustment,
)

from core.auto_trainer.state import STAT_KEYS, TRAINING_TYPES
from core.auto_trainer.larc_recovery import (
    POLICY_VERSION, critical_energy_plan, estimated_training_cost, period_value, planning_context, points_to_percent, recovery_value, recovery_weight,
)
from core.auto_trainer.larc_vitality_preview import accepted_preview
from core.auto_trainer.larc_cycle_value import training_cycle_value
from core.larc_reward_cycle import participant_cycle
from core.auto_trainer.larc_ss_opportunity import ss_redemption, preparation_delay
from core.auto_trainer.larc_attribute_plan import score_attribute_gains
from core.auto_trainer.larc_charge_value import battery_inventory, mei_charge_value
from core.auto_trainer.larc_supporter_goal import urgent_larc_supporter_goal


from core.auto_trainer.larc_phase import PHASE_WEIGHTS, phase as _phase, phase_weights
from core.auto_trainer.larc_user_preferences import values as user_preferences, configured as preferences_configured, head_preference
from core.auto_trainer.larc_gain_evidence import gain_evidence
from core.auto_trainer.thresholds import PARTIAL_FORECAST_CREDIT


_forecast_evaluation = ContextVar('larc_baseline_forecast_evaluation', default=None)


def forecast_evaluation(func):
    """Keep immutable opportunity references within one baseline call only."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        token = _forecast_evaluation.set({})
        try:
            return func(*args, **kwargs)
        finally:
            _forecast_evaluation.reset(token)
    return wrapped


def first_arc_speed_value(state, gains):
    """Bounded preparation preference, not a winning line or a training veto."""
    period = period_value(state)
    if period.get("year_index") != 2 or period.get("month") not in {7, 8, 9}:
        return 0.
    settings = state.scenario_state.get("larc_race_preparation", {})
    target = settings.get("classic_speed_soft_target", 900)
    if type(target) not in {int, float} or target <= 0:
        target = 900
    windows = {(7, "first"): 5, (7, "second"): 4, (8, "first"): 3,
               (8, "second"): 2}.get((period.get("month"), period.get("half")), 1)
    speed = state.current_stats.get("speed")
    if type(speed) not in {int, float}:
        return 0.
    gap = max(0., target - speed)
    weight = min(.6, gap / max(120., windows * 60.))
    return min(gap, max(0., gains.get("speed", 0))) * weight


def gain_value(state, gains, goal, *, project=True):
    """Shared numeric gain scale for real training and SS previews, without fake candidates."""
    phase = _phase(state)
    weights = phase_weights(state,goal)
    attributes = score_attribute_gains(state, gains, goal, project=project)
    points = gains.get("skill_pt")
    unknown = [] if type(points) is int and points >= 0 else ["skill_pt"]
    skill_resource = skill_marginal(state, goal, points if type(points) is int else 0)
    components = {"stats": round(attributes["base"] * weights["stats"], 4),
                  "dynamic_attribute_gap": round(attributes["gap_premium"] * weights["stats"], 4),
                  "skill_pt": round(points * weights["skill_pt"] * skill_resource["factor"], 4) if not unknown else 0.,
                  "first_arc_speed_preparation": round(first_arc_speed_value(state, gains)*user_preferences(goal)['numeric'], 4),
                  "high_stat_target_scarcity": round(attributes["scarcity"] * weights["stats"], 4)}
    return {"score": round(sum(components.values()), 4), "phase": phase, "weights": weights,
            "components": components, "unknown_fields": unknown,
            "training_equivalent_gains": attributes["training_equivalent_gains"],
            "attribute_plan": attributes["plan"], "skill_resource": skill_resource,
            "projected_final_after_action": attributes["projected_final_after_action"],
            "projected_final_after_action_basis": ("if_action_succeeds_then_discounted_future_training"
                                                   if project else "not_requested_for_internal_numeric_comparison"),
            "gain_basis": ("displayed_gains_with_score_only_1200_to_target_restoration"
                           if attributes["plan"].get("utility", {}).get("restore_high_stat_value")
                           else "displayed_gain_plus_dynamic_gap_and_halved_target_scarcity")}


def training_hard_issues(candidate, failure_limit):
    if candidate.failure_rate is not None and candidate.failure_rate > failure_limit:
        return ['failure_rate_exceeds_limit']
    return []


def training_soft_issues(candidate, *, min_confidence=.65, strategy_v4=False):
    issues = []
    if strategy_v4:
        from core.auto_trainer.larc_candidate_quality import usable_numeric_evidence
        if not usable_numeric_evidence(candidate):
            issues.append('numeric_gain_unreliable')
        if candidate.failure_rate is None:
            issues.append('failure_rate_unreadable')
        return issues
    if candidate.confidence < min_confidence:
        issues.append("candidate_confidence_below_minimum")
    if candidate.failure_rate is None:
        issues.append("failure_rate_unreadable")
    if not any(type(value) is int and value > 0 for key, value in candidate.stats_gain.items()
               if key in STAT_KEYS and key != "skill_pt"):
        issues.append("numeric_gain_unreliable")
    if gain_evidence(candidate)['unverified_used_attributes']:
        issues.append("larc_visible_gain_unresolved")
    return issues


# Historical callers use this only to decide whether an estimate is complete.
training_numeric_issues = training_soft_issues


@timed_stage('future_numeric_reference_build')
def _future_numeric_values(state, goal, *, consumed_progress=None):
    """Revalue whole observed layouts with shared, discounted progress units.

    No future charming/full-battery/recovery milestone is copied from today.
    Old samples without progress use a labelled, further-discounted current
    observation prior; they are not relabelled as historical observations.
    """
    from core.auto_trainer.larc_recovery_routes import effective_recovery_window
    from core.auto_trainer.larc_risk import strategy_v4_enabled
    v4 = strategy_v4_enabled(state, goal)
    current = {item.training_type: item for item in state.training_options}
    weights = phase_weights(state,goal)
    progress = {tab: score_training_progress(state, item, goal=goal) for tab, item in current.items()}
    balanced = training_balance_v2_enabled(goal)
    window = effective_recovery_window(state, goal)
    steps = max(1, window["steps"] if window else 1)
    bond_budget, friend_budget = 0., 0.
    seen_people = set()
    for tab, report in progress.items():
        for person in report["participants"]:
            ratio = person["hint_bond_fill_ratio_estimate"]
            if type(ratio) not in {int, float}:
                continue
            if person["role"] == "friend":
                friend_budget = max(friend_budget, max(0., .6 - ratio) / .07)
            elif person["role"] == "support" and person["effective_bond"] != 0:
                key = person.get("member_id") or (tab, person["slot"])
                if key not in seen_people:
                    bond_budget += max(0., .8 - ratio) / .07
                    seen_people.add(key)
    used = consumed_progress or {}
    bond_budget = max(0., bond_budget - used.get("effective_bond", 0.) - used.get("hint_bond", 0.))
    friend_budget = max(0., friend_budget - bool(used.get("friend_startup", 0.)))
    estimated_progress = False
    from core.auto_trainer.larc_attribute_plan import attribute_plan
    from core.auto_trainer.larc_training_forecast import numeric_at_period, abroad
    attribute_context = attribute_plan(state, goal)

    def option(tab, gains, units, *, prior=False, metadata=None):
        # Same phase weights as current progress. The extra credit is only a
        # forecast discount; seven bond points per touch is a planning estimate.
        credit = .25 if prior else .5
        bond = min(bond_budget / steps, max(0., units.get("effective_bond", 0.) + units.get("hint_bond", 0.)))
        friend = min(2.3 * friend_budget / steps, max(0., .8 * units.get("friend_startup", 0.)
                                                     + 1.5 * units.get("friend_coverage", 0.)))
        if balanced:
            # Historical packets retain their observed contact units. Under
            # v2, revalue the same 60-threshold progress once, without the
            # separate generic startup award or its old 2.3x touch budget.
            friend = min(friend_budget / steps, min(1., max(0., units.get("friend_coverage", 0.))))
        metadata = dict(metadata or {})
        if unified_larc_enabled(goal):
            metadata.setdefault('source_turn_index', state.turn)
        return {"tab": tab, "gains": dict(gains), "forecast_metadata": metadata,
                **({'risk_metadata': (metadata or {}).get('risk_metadata', {})} if v4 else {}),
                "numeric": numeric_at_period(state, goal, gains, period_value(state), attribute_context=attribute_context),
                "bond": (bond*weights["effective_bond"] + friend*weights.get('friend_contact',weights['effective_bond'])) * credit,
                "charge": max(0., units.get("net_charge", 0.)) * weights["net_charge"] * credit}

    from core.auto_trainer.larc_expedition_forecast import forecast_metadata
    current_layout = []
    for tab, item in current.items():
        metadata = forecast_metadata(item, state)
        if v4:
            metadata['risk_metadata'] = {'failure_rate': item.failure_rate,
                'vitality_percent': state.vitality, 'basis': 'observed_training_layout'}
        issues = training_soft_issues(item, strategy_v4=v4)
        gains = item.stats_gain
        if issues:
            unverified = set(gain_evidence(item)['unverified_used_attributes'])
            gains = {key: value for key, value in gains.items() if key not in unverified}
        row = option(tab, gains, progress[tab]['forecast_units'], metadata=metadata)
        if issues:
            row['partial_forecast_issues'] = issues
        current_layout.append(row)
    layouts = [current_layout]
    period = period_value(state)
    phase = (period.get("year_index"), abroad(period))
    packet = state.scenario_state.get("larc_attribute_inputs", {})
    runs = {item.raw_data.get("capture", {}).get("run_session_id") for item in state.training_options}
    through = packet.get("through_turn", state.turn) if isinstance(packet, dict) else None
    bound = (isinstance(packet, dict) and runs == {packet.get("run_session_id")}
             and packet.get("run_session_id") and type(through) is int and through <= state.turn)
    seen, estimated_pt = set(), False
    phase_layouts = list(layouts)
    for sample in reversed(packet.get("training_history", []) if bound else []):
        turn = sample.get("turn_index")
        old = sample.get("period", {})
        if (type(turn) is not int or not 0 < state.turn - turn <= 12 or turn in seen
                or old.get("year_index") != period.get("year_index")):
            continue
        seen.add(turn)
        layout = []
        for tab, raw in sample.get("raw_gains", {}).items():
            if tab not in current or not isinstance(raw, dict):
                continue
            gains = {}
            for key in STAT_KEYS:
                if key == "skill_pt":
                    continue
                amount = max(0., raw.get(key, 0.))
                below = min(amount, max(0., 1200 - state.current_stats.get(key, 0)))
                gains[key] = round(below + (amount - below) / 2)
            pt = sample.get("skill_points", {}).get(tab)
            if type(pt) is not int:
                now = current[tab].stats_gain
                ratio = min(2., sum(gains.values()) / max(1, sum(now.get(k, 0) for k in gains)))
                current_pt = now.get("skill_pt")
                pt = round((current_pt if type(current_pt) is int else 0) * ratio)
                estimated_pt = True
            gains["skill_pt"] = pt
            units = sample.get("progress_units", {}).get(tab)
            prior = not isinstance(units, dict)
            if prior:
                units = progress[tab]["forecast_units"]
                estimated_progress = True
            metadata = {"period": old, **sample.get("forecast_metadata", {}).get(tab, {})}
            if unified_larc_enabled(goal):
                metadata['source_turn_index'] = turn
            layout.append(option(tab, gains, units, prior=prior, metadata=metadata))
        if any(row["tab"] != "wisdom" for row in layout):
            phase_layouts.append(layout)
            if (old.get("year_index"), abroad(old)) == phase:
                layouts.append(layout)
        if len(seen) >= 12:
            break
    layouts = [layout for layout in layouts if any(row["tab"] != "wisdom" for row in layout)]
    from core.auto_trainer.larc_policy import overseas_purchase_v2_enabled
    overseas_mix = (overseas_purchase_v2_enabled(goal) and window
        and any(step['kind']=='overseas' for step in window.get('action_calendar',[])))
    discount = .85 if len(phase_layouts if overseas_mix else layouts) >= 4 else .65
    def discounted_layout(layout):
        rows = []
        for row in layout:
            credit = discount * PARTIAL_FORECAST_CREDIT if row.get('partial_forecast_issues') else discount
            rows.append({**row, 'forecast_discount': credit,
                         **{key: round(row[key] * credit, 4) for key in ('numeric', 'bond', 'charge')}})
        return rows

    layouts = [discounted_layout(layout) for layout in layouts]
    phase_layouts = [discounted_layout(layout) for layout in phase_layouts if any(row['tab'] != 'wisdom' for row in layout)]
    first_factor = (ss_redemption(state, earliest_actions=3)["value_factors"]["net_charge"]
                    if window and window["domestic_steps"] else 0.)
    def value(row):
        return row["numeric"] + row["bond"] + first_factor * row["charge"]
    pairs = [(max(value(row) for row in layout if row["tab"] != "wisdom"),
              max((value(row) for row in layout if row["tab"] == "wisdom"), default=0.)) for layout in layouts]
    return {"training": median(pair[0] for pair in pairs) if pairs else 0.,
            "wisdom": median(pair[1] for pair in pairs) if pairs else 0.,
            "numeric_training": median(max(row["numeric"] for row in layout if row["tab"] != "wisdom")
                                       for layout in layouts) if layouts else 0.,
            "layouts": pairs, "opportunity_layouts": layouts, "phase_layouts": phase_layouts, "layout_count": len(layouts),
            "historical_skill_pt_estimated": estimated_pt, "historical_progress_estimated": estimated_progress,
            "progress_credit": .5, "missing_progress_prior_credit": .25,
            "remaining_bond_touch_budget": round(bond_budget, 4),
            "remaining_friend_touch_budget": round(friend_budget, 4),
            **({'layout_sample_basis': 'current_layout_plus_unique_bound_historical_turns',
                'historical_layout_count': max(0, len(layouts) - 1),
                'sample_credit_threshold': 4,
                'sample_credit_status': 'policy_discount_not_statistical_confidence'} if v4 else {}),
            "basis": "same-phase layouts; shared current progress weights; bounded discounted generic progress; no repeated special reward"}


def _energy_forecast(state, goal, reference, window, cost):
    """Prepare equal frozen inputs once for the private read-only route walk.

    route_continuation/stateful routes only read this working table. Public
    candidate diagnostics are copied separately before they leave this scope.
    """
    cache = _forecast_evaluation.get()
    entries = cache.setdefault('forecasts', []) if cache is not None else []
    for old_state, old_goal, old_reference, old_window, old_cost, report in entries:
        if (old_state is state and old_goal is goal and old_cost == cost
                and (old_reference is reference or old_reference == reference)
                and (old_window is window or old_window == window)):
            return report
    from core.auto_trainer.larc_expedition_forecast import expedition_scenarios
    from core.auto_trainer.larc_training_forecast import training_scenarios
    report = expedition_scenarios(state, goal, reference, window, cost)
    if not report.get('active'):
        report = training_scenarios(state, goal, reference, window, cost)
    if cache is not None:
        entries.append((state, goal, reference, window, cost, report))
    return report


def _forecast_diagnostics(report):
    result = {key: value for key, value in report.items() if key != 'scenarios'}
    return deepcopy(result) if _forecast_evaluation.get() is not None else result


def energy_continuity(state, goal, delta_percent, *, include_held_ss=True, consumed_outing=False, consumed_progress=None,
                      future_forecast=None, future_reference=None, consumed_held_ss=False, consumed_skill_pt=0,
                      consumed_gains=None, consumed_training_type=None):
    from core.auto_trainer.larc_ss_energy import applicable, short_energy_continuity
    if applicable(state, goal):
        # One unchanged-state numeric opportunity reference for every current
        # action. No consumed stats/progress or held SS gains enter this path.
        reference = deepcopy(future_reference) if future_reference else _future_numeric_values(state, goal)
        return short_energy_continuity(state, goal, delta_percent, reference,
            consumed_outing=consumed_outing, consumed_held_ss=consumed_held_ss,
            include_held_ss=include_held_ss)
    from core.auto_trainer.larc_recovery_routes import effective_recovery_window, route_continuation
    window = effective_recovery_window(state, goal)
    if window is None:
        return {"active": False, "score": 0.}
    reference = deepcopy(future_reference) if future_reference else _future_numeric_values(state, goal, consumed_progress=consumed_progress)
    held_value = 0.
    held_value_after = None
    held_gains = None
    held_fan_values = ()
    if include_held_ss and planning_context(state)["held_ss_available"]:
        preview = state.scenario_state.get("larc_ss_preview", {})
        gains = preview.get("stats_gain", {})
        observation = preview.get("participants_observation", {})
        from core.auto_trainer.larc_ss_visible import visible_ss_participants
        people = visible_ss_participants(preview)
        if (all(type(gains.get(key)) is int and gains[key] >= 0 for key in (*TRAINING_TYPES, "skill_pt"))
                and len(people) == preview.get('ready_count') and all(p.get("win_guaranteed") is True for p in people)):
            held_gains = dict(gains)
            held_state = state
            from core.auto_trainer.larc_policy import enabled
            if enabled(goal) and goal.larc_policy.get("skill_future_credit", 1.) < 1. and consumed_skill_pt > 0:
                from dataclasses import replace
                held_state = replace(state, current_stats={**state.current_stats,
                    "skill_pt":state.current_stats.get("skill_pt",0)+consumed_skill_pt})
            held_value = gain_value(state, gains, goal, project=False)["score"] * .85
            if held_state is not state:
                held_value_after = gain_value(held_state, gains, goal, project=False)["score"] * .85
            from core.auto_trainer.larc_fan_goal import fan_route_value, fan_action_reference
            reference_value = fan_action_reference(state, goal)
            expectation_delta = len(people) * (.5 if preview.get("kind") == "SS" else 1.)
            held_fan_values = tuple(fan_route_value(state, expectation_delta=expectation_delta,
                earliest_actions=elapsed + 2, reference_value=reference_value)["score"]
                for elapsed in range(window["steps"]))
    wisdom_recovery = None
    from core.auto_trainer.larc_risk import strategy_v4_enabled, expected_success_value
    v4 = strategy_v4_enabled(state, goal)
    wisdom = next((item for item in state.training_options
                   if item.training_type == "wisdom" and not training_numeric_issues(item, strategy_v4=v4)), None)
    if wisdom is not None:
        source = _capture(state, wisdom)
        larc = wisdom.scenario_mechanics.get("larc")
        larc = larc if isinstance(larc, dict) else {}
        preview = larc.get("vitality_preview", {})
        preview_source = preview.get("source", {}) if isinstance(preview, dict) else {}
        bound = (source is not None and accepted_preview(preview) and preview.get("training_type") == "wisdom"
                 and isinstance(preview_source, dict)
                 and all(preview_source.get(k) == source[k] for k in ("run_session_id", "turn_index", "sha256")))
        delta = wisdom.vitality_delta
        effects = larc.get("training_effects", {})
        effect_source = effects.get("source", {}) if isinstance(effects, dict) else {}
        if (source is not None and isinstance(effects, dict) and effects.get("status") == "accepted"
                and effects.get("certainty") == "guaranteed" and isinstance(effect_source, dict)
                and all(effect_source.get(k) == source[k] for k in ("run_session_id", "turn_index", "sha256"))):
            delta = effects.get("vitality_delta", delta)
        amount = preview["delta_percent"] if bound else points_to_percent(state, delta if type(delta) is int else 5)
        raw_numeric = gain_value(state, wisdom.stats_gain, goal, project=False)["score"]
        numeric = (expected_success_value(raw_numeric, wisdom.failure_rate) if v4 else
                   raw_numeric * (1 - wisdom.failure_rate / 100))
        wisdom_recovery = (amount, numeric)
    # A known outing remains a future alternative, and taking one now advances
    # its stage. Do not grant the same limited outing twice.
    from core.auto_trainer.larc_friend_outing import bound_outing_packet, bound_outing_inputs, outing_effects, outing_completion_value
    packet = bound_outing_packet(state)
    outings = []
    if packet:
        bonus = bound_outing_inputs(packet) or {}
        for stage in range(packet["completed"] + 1, 6):
            effects = outing_effects(stage, recovery_bonus=bonus.get("recovery"), event_bonus=bonus.get("event"))
            gains = {key: values[0] for key, values in effects["stats"].items()} | {"skill_pt": 0}
            outings.append({"stage": stage, "recovery_percent": points_to_percent(state, effects["vitality_points"][0]),
                            "stats_gain": dict(gains),
                            "numeric_value": gain_value(state, gains, goal, project=False)["score"]
                                             + outing_completion_value(state, goal, stage, bonus)["score"]})
    cost, basis = estimated_training_cost(state)
    expedition = future_forecast
    if not expedition:
        expedition = _energy_forecast(state, goal, reference, window, cost)
    elif not expedition.get("active"):
        from core.auto_trainer.larc_training_forecast import training_scenarios
        expedition = training_scenarios(state, goal, reference, window, cost)
    diagnostics = critical_energy_plan(state, delta_percent, future_training_value=reference["training"],
        risk_tolerance=goal.risk_tolerance, wisdom_recovery=wisdom_recovery, include_held_ss=include_held_ss)
    stateful_kwargs = {}
    if unified_larc_enabled(goal):
        from core.auto_trainer.larc_attribute_plan import attribute_plan
        stateful_kwargs = {'consumed_gains': consumed_gains, 'held_gains': held_gains,
            'stateful_context': attribute_plan(state, goal),
            'historical_layout_count': reference.get('historical_layout_count', 0)}
    route = route_continuation(state, goal, delta_percent, training_value=reference["training"],
        training_cost=cost, wisdom_value=reference["wisdom"],
        wisdom_recovery=wisdom_recovery[0] if wisdom_recovery else 0., outings=outings,
        layouts=reference["layouts"], opportunity_layouts=reference["opportunity_layouts"],
        expedition=expedition,
        consumed_outing=consumed_outing, include_held_ss=include_held_ss,
        held_numeric_value=held_value, consumed_held_ss=consumed_held_ss,
        held_fan_values=held_fan_values, held_numeric_value_after=held_value_after,
        consumed_training_type=consumed_training_type, **stateful_kwargs)
    return {**diagnostics, **route, "estimated_training_cost_percent": round(cost, 4),
            "training_cost_basis": basis, "future_value_reference": reference,
            "future_numeric_training_value": expedition.get("numeric_training", round(reference["numeric_training"], 4)),
            "expedition_forecast": _forecast_diagnostics(expedition)}


def _capture(state, candidate):
    source = candidate.raw_data.get("capture")
    if (not isinstance(source, dict) or type(source.get("turn_index")) is not int
            or source["turn_index"] != state.turn
            or any(not isinstance(source.get(key), str) or not source[key]
                   for key in ("run_session_id", "absolute_path", "sha256"))):
        return None
    return source


def _role(participant):
    role = participant.get("role", {})
    if isinstance(role, dict) and role.get("status") == "accepted" and role.get("kind") in {"support", "friend", "npc"}:
        return role["kind"]
    identity = participant.get("identity", {})
    if (isinstance(identity, dict) and identity.get("status") == "known"
            and identity.get("entity_kind") == "support_card" and identity.get("matched_card_id")):
        # A matched friend card is not an ordinary training support.
        if identity.get("card_type") in {"friend", "友人"}:
            return "friend"
        if identity.get("card_type") in {"speed", "stamina", "power", "guts", "wisdom", "速度", "耐力", "力量", "根性", "智力"}:
            return "support"
    return None


def _feature(known_value, unknown_slots):
    return {"value": None if unknown_slots else known_value,
            "known_value": known_value, "unknown_slots": unknown_slots}


def _current_ss_reward(participant, training_source):
    """Refine a shared icon using the roster's still-current information row."""
    icon = participant.get("ss_reward")
    icon = icon if isinstance(icon, dict) else {}
    fallback = (dict(icon), {"basis": "training_reward_icon"})
    families = {"positive_condition": {"charming", "practice_good"},
                "numeric_gain": {"stat_gain", "skill_pt"}}
    icon_kind = icon.get("kind") if icon.get("status") == "accepted" else None
    # A specific current icon takes precedence over an older information row.
    if icon.get("earned") is True or (icon_kind and icon_kind not in families):
        return fallback
    if isinstance(participant.get("ss_reward_cycle"), dict):
        cycle = participant_cycle(participant, training_source)
        if cycle is not None:
            return dict(cycle[0]), {"basis": "ss_reward_cycle", "icon_kind": icon_kind,
                                   "status": participant["ss_reward_cycle"]["status"],
                                   "source": dict(participant["ss_reward_cycle"]["source"])}
    information = participant.get("ss_information_preview")
    if not isinstance(information, dict):
        return fallback
    association = information.get("association")
    source = information.get("source")
    current = information.get("current")
    if (information.get("status") != "observed_information"
            or information.get("freshness") not in {"same_turn", "historical"}
            or not isinstance(association, dict) or association.get("status") != "accepted"
            or not isinstance(source, dict)
            or source.get("run_session_id") != training_source["run_session_id"]
            or type(source.get("turn_index")) is not int
            or not 0 <= source["turn_index"] <= training_source["turn_index"]
            or not isinstance(current, dict) or current.get("status") != "accepted"
            or not current.get("kind") or current.get("earned") is True):
        return fallback
    if icon_kind and current["kind"] not in families[icon_kind]:
        return fallback
    # Upcoming entries are not current rewards and are never rotated here.
    return dict(current), {"basis": "ss_information_preview", "icon_kind": icon_kind,
                           "freshness": information["freshness"], "source": dict(source)}


def support_bond_units(bond):
    """Shared contact-value units, not a forecast of exact bond points."""
    if not isinstance(bond, dict) or bond.get("status") != "accepted":
        return None
    if bond.get("is_max") is True or bond.get("color") in {"orange", "yellow"}:
        return 0.
    if bond.get("color") in {"empty", "cyan", "green"}:
        ratio = bond.get("fill_ratio_estimate")
        return 1.25 if type(ratio) in {int, float} and .6 <= ratio < .8 else 1.
    return None


def _training_redemption_actions(state, candidate, source, people, ready, newly_full, charming_plan):
    # Two newly ready members per additional training is the same soft prior
    # used by preparation_delay. It is not a claim about tomorrow's layout.
    minimum = 2 if newly_full else 3
    if ready is None:
        return {"earliest_actions": minimum, "basis": "ready_count_unknown", "ready_after_base": None}
    after = ready + newly_full
    missing = max(0, 5 - after)
    ordinary = max(minimum, 2 + (missing + 1) // 2)
    rewards = [p["current_reward"] for p in people if p["newly_full"] == 1]
    if source is not None:
        for row in battery_inventory(state, source["run_session_id"]).values():
            if row["filled"] + row["previews"].get(candidate.training_type, 0) == 3:
                rewards.append(_current_ss_reward(row["person"], source)[0])
    kinds = {r.get("kind") for r in rewards if r.get("status") == "accepted" and r.get("earned") is not True}
    recovery = after > 0 and bool(kinds & {"max_vitality_and_recovery", "motivation_and_vitality"})
    charming = after >= 3 and "charming" in kinds and charming_plan["score"] > 0
    goal = state.scenario_state.get("larc_supporter_goal", {})
    goal_source = goal.get("source", {}) if isinstance(goal, dict) else {}
    urgent = (after >= 4 and urgent_larc_supporter_goal(goal) and source is not None
              and isinstance(goal_source, dict) and goal_source.get("turn_index") == state.turn
              and goal_source.get("run_session_id") == source["run_session_id"]
              and all(goal_source.get(key) for key in ("path", "sha256")))
    partial = recovery or charming or urgent
    return {"earliest_actions": minimum if partial else ordinary,
            "basis": "known_partial_group_reason" if partial else "five_person_group_progress_prior",
            "partial_group_reason": "recovery" if recovery else "early_charming" if charming else "supporter_deadline" if urgent else None,
            "ready_after_base": after, "additional_group_trainings_estimate": (missing + 1) // 2}


def score_training_progress(state, candidate, weights=None, *, goal=None):
    """Shared observed progress; no numeric gains or recursive energy forecast."""
    weights = dict(phase_weights(state,goal) if weights is None else weights)
    balanced = training_balance_v2_enabled(goal)
    revised_charming = unified_larc_enabled(goal)
    components, unknown = {}, []
    source = _capture(state, candidate)
    view = training_rows(candidate, typed=True) if source is not None else None
    people, complete = view if view is not None else ([], False)
    unknown_rows = [] if complete else ["unresolved_portrait_slots" if view is not None else "observation_unknown"]
    bond_unknown, charge_unknown, hint_bond_unknown = (list(unknown_rows) for _ in range(3))
    hint_bond = 0.
    effective_bond, net_charge, newly_full, friends, npcs = 0., 0, 0, 0, 0
    friend_startup = 0.
    friend_coverage = 0.
    detail = []
    for person in people:
        slot = person.get("slot")
        role = _role(person)
        bond = person.get("bond")
        bond = bond if isinstance(bond, dict) else {}
        bond_units = None
        if role == "support":
            bond_units = support_bond_units(bond)
        elif role in {"friend", "npc"}:
            bond_units = 0.
            friends += role == "friend"
            npcs += role == "npc"
            ratio = bond.get("fill_ratio_estimate")
            if (role == "friend" and bond.get("status") == "accepted"
                    and type(ratio) in {int, float} and 0 <= ratio < .6):
                # A separate startup preference; not ordinary support friendship.
                friend_startup += 1.25 if ratio >= .5 else 1.
                # Soft value for advancing the 60-bond coverage unlock. Seven
                # bond points is a planning step, not a promised event gain.
                # As the remaining gap shrinks, this action advances more of
                # the unlock; phase weights fade its value later in the run.
                friend_coverage += min(1., .07 / max(.07, .6 - ratio))
        if bond_units is None:
            bond_unknown.append(slot)
        else:
            effective_bond += bond_units
        charge = person.get("charge")
        charge = charge if isinstance(charge, dict) else {}
        net = full = charge_basis = None
        resolved = effective_domestic_charge(
            person, state=state, tab_participants=people, candidate=candidate
        )
        if resolved is not None:
            filled, net, charge_basis = resolved
            full = int(filled < 3 and filled + net == 3)
            net_charge += net
            newly_full += full
        else:
            charge_unknown.append(slot)
        hint = person.get("training_hint", {})
        hint = hint if isinstance(hint, dict) else {}
        hint_units = None
        if role in {"friend", "npc"} or bond_units == 0:
            hint_units = 0.
        elif hint.get("status") == "accepted" and hint.get("present") is False:
            hint_units = 0.
        if hint.get("status") == "accepted" and hint.get("present") is True:
            unknown.append(f"participant.{slot}.hint_applicability")
            if role == "support" and bond_units is not None and bond_units > 0:
                ratio = bond.get("fill_ratio_estimate")
                if type(ratio) in {int, float} and 0 <= ratio < .8:
                    # At most one extra policy unit; fade across the last 20%
                    # before friendship. This is not a predicted bond-point
                    # gain or proof that an estimated bar will cross 80%.
                    hint_units = round(min(1., (.8 - ratio) / .2), 4)
        if hint_units is None:
            hint_bond_unknown.append(slot)
        else:
            hint_bond += hint_units
        reward, reward_source = _current_ss_reward(person, source)
        detail.append({"slot": slot, "role": role, "effective_bond": bond_units,
                       "member_id": person.get("member_ref", {}).get("member_id"),
                       "training_hint_bond": hint_units,
                       "hint_bond_fill_ratio_estimate": bond.get("fill_ratio_estimate"),
                       "net_charge": net, "newly_full": full,
                       "charge_preview_basis": charge_basis,
                       "current_reward": reward, "current_reward_source": reward_source,
                       **({'filled_cells': charge.get('filled_cells')} if revised_charming else {})})
    features = {"effective_support_bond": _feature(effective_bond, bond_unknown),
                "training_hint_bond": _feature(hint_bond, hint_bond_unknown),
                "net_charge": _feature(net_charge, charge_unknown), "newly_full": _feature(newly_full, charge_unknown),
                "known_friend_count": friends, "known_npc_count": npcs}
    for name in ("effective_support_bond", "training_hint_bond", "net_charge", "newly_full"):
        if features[name]["value"] is None:
            unknown.append(name)
    components.update(effective_bond=round(effective_bond * weights["effective_bond"], 4),
                      friend_startup=0. if balanced else round(friend_startup * weights.get('friend_contact',weights["effective_bond"]) * .8, 4),
                      friend_coverage_preparation=round((min(1., friend_coverage) if balanced else friend_coverage * 1.5)
                                                        * weights.get('friend_contact',weights["effective_bond"]), 4),
                      training_hint_bond=round(hint_bond * weights["effective_bond"], 4),
                      net_charge=net_charge * weights["net_charge"], newly_full=newly_full * weights["newly_full"])
    from core.auto_trainer.larc_mei_cycle import enabled as mei_cycle_enabled, startup_value
    mei_cycle = mei_cycle_enabled(state, goal)
    startup = None
    if mei_cycle:
        startup = startup_value(state, candidate, source, weights,
                                existing_score=components['friend_coverage_preparation'])
        components['friend_coverage_preparation'] = startup['score']

    entry = state.scenario_state.get("larc_ss_entry", {})
    entry_source = entry.get("source", {}) if isinstance(entry, dict) else {}
    entry_source = entry_source if isinstance(entry_source, dict) else {}
    entry_bound = (source is not None and isinstance(entry, dict) and entry.get("status") == "accepted"
        and entry.get("stage") == "entry" and type(entry.get("ready_count")) is int and 0 <= entry["ready_count"] <= 5
        and type(entry_source.get("turn_index")) is int and entry_source["turn_index"] == state.turn
        and entry_source.get("run_session_id") == source["run_session_id"]
        and all(isinstance(entry_source.get(k), str) and entry_source[k] for k in ("path", "sha256")))
    # This is preparation value only, never a claim that the next SS is executable.
    ready = entry["ready_count"] if entry_bound else None
    reaches_five = ready is not None and ready < 5 and ready + newly_full >= 5
    components["ss_preparation"] = weights["ss_preparation"] if reaches_five else 0.
    features["ss_preparation_entry"] = dict(entry_source) if entry_bound else None
    # Charge beyond the next five remains useful for a later cycle, but does
    # not get the same immediate-readiness bonus twice.
    available_slots = max(0, 5 - ready) if ready is not None else 5
    immediate = min(newly_full, available_slots)
    components["newly_full"] = (immediate + .25 * (newly_full - immediate)) * weights["newly_full"]
    future_rewards = 0.
    charming_progress = 0.
    charming_plan = charming_value(state)
    for person in detail:
        reward = person["current_reward"]
        # An early one-cell step towards charming has value too; only valuing
        # the final cell waits too long to start the first reward cycle.
        if (not revised_charming and reward.get("status") == "accepted" and reward.get("kind") == "charming"
                and reward.get("earned") is not True and person["net_charge"]
                and person["newly_full"] != 1 and weights["net_charge"] > 0):
            charming_progress += weights["effective_bond"] * person["net_charge"] / 3 * charming_plan["factor"]
        if (person["newly_full"] != 1 or reward.get("status") != "accepted"
                or reward.get("earned") is True or weights["net_charge"] == 0):
            continue
        if reward.get("kind") == "charming":
            if not revised_charming:
                future_rewards += weights["effective_bond"] * charming_plan["factor"]
        elif reward.get("kind") in {"max_vitality_and_recovery", "motivation_and_vitality"}:
            amount = reward.get("amount")
            amount = amount if type(amount) is int and amount >= 0 else 20
            future_rewards += max(0., recovery_value(state, points_to_percent(state, amount))) * .35
    # Member order when more than five become ready is not predicted.
    charge_preference=weights.get('charge_preference',1.)
    components["cycle_reward_preparation"] = round(future_rewards * immediate / max(1, newly_full)*charge_preference, 4)
    components["charming_charge_progress"] = round(charming_progress*charge_preference, 4)
    cycle = training_cycle_value(state, candidate, source, weights, ready=ready, newly_full=newly_full)
    components["future_recovery_cycle_progress"] = cycle["score"]
    readiness = _training_redemption_actions(state, candidate, source, detail, ready, newly_full, charming_plan)
    redemption = ss_redemption(state, earliest_actions=readiness["earliest_actions"])
    redemption["group_readiness"] = readiness
    delay = preparation_delay(ready, newly_full)
    base_progress = {name: components[name] for name in ('net_charge', 'newly_full', 'ss_preparation')}
    for name in ("net_charge", "newly_full", "ss_preparation"):
        components[name] = round(components[name] * redemption["value_factors"][name], 4)
    for name in ("cycle_reward_preparation", "charming_charge_progress"):
        components[name] = round(components[name] * redemption["value_factors"]["reward_preparation"] * delay, 4)
    if revised_charming:
        from core.auto_trainer.larc_charming_value import charming_charge_preparation
        marginal = charming_charge_preparation(state, detail, charming_plan,
            ready=ready, newly_full=newly_full, earliest_actions=readiness['earliest_actions'],
            bond_preference=user_preferences(goal)['bond'])
        # Other future rewards retain their original accounting. Charming is
        # represented only here, never again as a whole final-cell reward.
        components['charming_charge_progress'] = round(
            marginal['score'] * charge_preference if weights['net_charge'] > 0 else 0., 4)
        charming_plan = {**charming_plan, 'charge_preparation': marginal}

    random_charge = mei_charge_value(state, candidate, source, weights, cycle_model=mei_cycle,
        ready_after_base=min(5, ready + newly_full) if ready is not None else None,
        base_progress=base_progress, base_actions=readiness['earliest_actions'], goal=goal)
    components["mei_expected_charge"] = random_charge["score"]
    initial_contact=None
    if balanced:
        from core.auto_trainer.larc_mei_contact import initial_contact_value
        initial_contact=initial_contact_value(state,candidate,source,weights)
        components['mei_initial_contact_preference']=initial_contact['score']

    return {"components": components, "features": features, "unknown_fields": unknown,
            **({"friend_startup_value": startup or {"score": components["friend_coverage_preparation"],
                "contact_units": min(1., friend_coverage), "threshold_ratio": .6, "touch_ratio_prior": .07,
                "max_units_per_contact": 1., "separate_startup_credit": 0.,
                "basis": "single_bounded_contact_progress_toward_60; touch_size_is_policy_prior_not_observed_gain"}} if balanced else {}),
            "source": source, "participants": detail, "charming": charming_plan,
            "reward_cycle": cycle, "redemption": redemption, "preparation_delay": delay,
            "mei_charge": random_charge,"mei_initial_contact":initial_contact,
            "ready": ready, "newly_full": newly_full,
            "forecast_units": {"effective_bond": effective_bond, "hint_bond": hint_bond,
                               "friend_startup": friend_startup, "friend_coverage": friend_coverage,
                               "net_charge": net_charge}}


def _replace_identified_current_charge(components, progress, continuity, goal, training_type):
    """Do not add a scalar price to the same cells repriced by the SS route."""
    if not unified_larc_enabled(goal):return None
    cycle = continuity.get('ss_resource_cycle') or {}
    covered = cycle.get('current_training_charge_coverage') or {}
    fraction = covered.get('fraction')
    if (covered.get('training_type') != training_type or type(fraction) not in (int,float)
            or not 0 < fraction <= 1):return None
    detail = {}
    for name, key in (('net_charge','identified_net_cells'),('newly_full','identified_newly_full_members')):
        represented = covered.get(key)
        observed = (progress.get('features',{}).get(name) or {}).get('known_value')
        if type(represented) is not int or represented <= 0 or type(observed) not in (int,float) or observed <= 0:
            continue
        share = min(1., represented / observed) * fraction
        before = components.get(name,0.)
        components[name] = round(before * (1-share),4)
        detail[name] = {'before':before,'after':components[name],'replaced_fraction':share,
                        'known_units':observed,'represented_units':represented}
    if not detail:return None
    return {'status':'applied','training_type':training_type,'components':detail,
            'basis':'only stable identified current cells already repriced in explicit cycle',
            'empirical_lag_does_not_remove_unrepresented_current_charge':True}


def score_training_value(state, candidate, goal, *, future_reference=None):
    from core.auto_trainer.larc_ss_opportunity import with_planning_horizon
    state=with_planning_horizon(state,goal)
    result = gain_value(state, candidate.stats_gain, goal)
    unified = unified_larc_enabled(goal)
    wisdom_reserve = None
    if unified:
        from core.auto_trainer.larc_wisdom_reserve import wisdom_reserve_value
        wisdom_reserve = wisdom_reserve_value(state, candidate, goal,
            result['attribute_plan'], numeric_value=result['score'])
        if wisdom_reserve['score']:
            result['components']['wisdom_reserve_preference'] = wisdom_reserve['score']
    reading=gain_evidence(candidate)
    if reading['unresolved_rows']:result['gain_evidence']=reading
    weights, components, unknown = result["weights"], result["components"], result["unknown_fields"]
    unknown.extend('gain.'+key for key in reading['unscored_unknown_attributes'])
    progress = score_training_progress(state, candidate, weights, goal=goal)
    components.update(progress["components"])
    mei_phase_bonus = None
    if unified:
        from core.auto_trainer.larc_mei_phase_bonus import presence_preference
        mei_phase_bonus = presence_preference(state, candidate, progress['source'], goal)
        components.update(mei_phase_bonus['components'])
    from core.auto_trainer.larc_fan_goal import fan_route_value, fan_action_reference
    # Credit actual newly-full progress, not an already-full held SS again.
    fan = fan_route_value(state, expectation_delta=(min(max(0, 5 - progress["ready"]), progress["newly_full"]) * .5
                              if progress["ready"] is not None else 0.),
                          earliest_actions=progress["redemption"]["earliest_actions_required"],
                          reference_value=fan_action_reference(state, goal))
    components["conditional_fan_route"] = fan["score"]
    unknown.extend(progress["unknown_fields"])
    features, source = progress["features"], progress["source"]
    detail, charming_plan, cycle = progress["participants"], progress["charming"], progress["reward_cycle"]
    ready, newly_full = progress["ready"], progress["newly_full"]
    friends = features["known_friend_count"]
    larc = candidate.scenario_mechanics.get("larc") or {}

    effects = larc.get("training_effects", {})
    effect_source = effects.get("source", {}) if isinstance(effects, dict) else {}
    effect_source = effect_source if isinstance(effect_source, dict) else {}
    effect_bound = (source is not None and isinstance(effects, dict) and effects.get("status") == "accepted"
        and effects.get("certainty") == "guaranteed"
        and all(effect_source.get(k) == source[k] for k in ("run_session_id", "turn_index", "sha256")))
    delta = effects.get("vitality_delta") if effect_bound else candidate.vitality_delta
    delta = delta if type(delta) is int else None
    preview = larc.get("vitality_preview", {})
    preview_source = preview.get("source", {}) if isinstance(preview, dict) else {}
    preview_bound = (source is not None and accepted_preview(preview)
        and preview.get("training_type") == candidate.training_type
        and isinstance(preview_source, dict)
        and all(preview_source.get(k) == source[k] for k in ("run_session_id", "turn_index", "sha256")))
    delta_percent = preview["delta_percent"] if preview_bound else None
    saved_cost = effects.get("vitality_cost_reduction") if effect_bound and friends else None
    saved_cost = saved_cost if type(saved_cost) is int and saved_cost >= 0 else None
    features.update(vitality_delta=delta, vitality_delta_percent=delta_percent, verified_cost_reduction=saved_cost)
    estimate = None
    if delta is None and delta_percent is None:
        unknown.append("vitality_delta")
        # A policy estimate keeps live decisions usable when the optional
        # preview-energy reader abstains. It is never stored as an observation.
        estimate = 5 if candidate.training_type == "wisdom" else -20
        if candidate.training_type != "wisdom":
            estimate += min(20, saved_cost) if saved_cost is not None else 6 if friends else 0
    if friends and saved_cost is None and delta_percent is None and delta is None:
        unknown.append("friend_cost_reduction")
    weights["vitality"] = round(recovery_weight(state)[0], 4)
    energy_points = None if delta_percent is not None else delta if delta is not None else estimate
    energy_percent = delta_percent if delta_percent is not None else points_to_percent(state, energy_points)
    components["vitality"] = recovery_value(state, energy_percent)
    sta_quality = stamina_lesson_quality_adjustment(
        state, candidate, goal, result, components, energy_percent=energy_percent
    )
    if sta_quality["score"]:
        components["stamina_lesson_quality"] = sta_quality["score"]
    sta_deficit = stamina_deficit_scarcity(state, candidate, goal, result)
    if sta_deficit["score"]:
        components["stamina_deficit_scarcity"] = sta_deficit["score"]
    trade_adj = high_hp_wisdom_stamina_trade(state, candidate, goal, features)
    if trade_adj["score"]:
        components["high_hp_stamina_wisdom_trade"] = trade_adj["score"]
    # Net delta already includes cost reduction: never count both.
    components["verified_cost_reduction"] = 0.
    weights["failure_rate"] = .5
    components["failure_penalty"] = -(candidate.failure_rate or 0.) * weights["failure_rate"]
    success_value = sum(max(0., number) for key, number in components.items()
                        if key not in {"vitality", "verified_cost_reduction"})
    components["failure_expected_loss"] = round(-success_value * (candidate.failure_rate or 0.) / 100, 4)
    continuity = energy_continuity(state, goal, energy_percent, consumed_progress=progress["forecast_units"],
        consumed_skill_pt=max(0,candidate.stats_gain.get("skill_pt",0)), consumed_gains=candidate.stats_gain,
        consumed_training_type=candidate.training_type, future_reference=future_reference)
    components["energy_continuity"] = continuity["score"]
    charge_overlap = _replace_identified_current_charge(components, progress, continuity, goal, candidate.training_type)
    if charge_overlap is not None:
        success_value = sum(max(0., number) for key, number in components.items()
                            if key not in {'vitality','verified_cost_reduction','energy_continuity',
                                           'failure_penalty','failure_expected_loss'})
        components['failure_expected_loss'] = round(-success_value * (candidate.failure_rate or 0.) / 100,4)
    support_count = candidate.raw_data.get("effective_support_count")
    components["baseline_support"] = float(support_count) if result["phase"] == "baseline" and type(support_count) is int and support_count >= 0 else 0.
    preferred_heads=head_preference(state,candidate,source,goal)
    components.update(preferred_heads['components'])
    from core.auto_trainer.larc_risk import strategy_v4_enabled, risk_value
    risk = None
    if strategy_v4_enabled(state, goal):
        # Charge/head preferences are conditional on this training too. Price
        # the whole positive action yield once, after all such parts exist.
        success_value = sum(max(0., value) for key, value in components.items()
            if key not in {'vitality', 'verified_cost_reduction', 'energy_continuity',
                           'failure_penalty', 'failure_expected_loss', 'risk_aversion_preference'})
        risk = risk_value(success_value, candidate.failure_rate)
        components['failure_expected_loss'] = round(-risk['expected_loss'], 4)
        components['failure_penalty'] = 0.
        components['risk_aversion_preference'] = round(-risk['preference_cost'], 4)
    result.update(score=round(sum(components.values()), 4), features=features,
                  policy_version=policy_version(goal),
                  planning={**planning_context(state), "energy_horizon": continuity, "training_vitality_points": energy_points,
                            **({'current_charge_overlap':charge_overlap} if charge_overlap is not None else {}),
                            **({'wisdom_reserve': wisdom_reserve} if wisdom_reserve is not None else {}),
                            **({'training_risk': risk} if risk is not None else {}),
                            'mei_initial_contact':progress.get('mei_initial_contact'),
                            **({'preferred_heads':preferred_heads} if preferred_heads['requested'] else {}),
                            **({"friend_startup_value": progress["friend_startup_value"]} if "friend_startup_value" in progress else {}),
                            "reward_cycle": cycle, "charming": charming_plan,
                            "mei_charge": progress["mei_charge"], "fan_route": fan,
                            **({'mei_phase_bonus': mei_phase_bonus} if mei_phase_bonus is not None else {}),
                            "ss_redemption": progress["redemption"], "reward_preparation_delay": progress["preparation_delay"],
                            "training_vitality_percent": round(energy_percent, 4),
                            "training_vitality_basis": "observed_bar_preview" if delta_percent is not None else
                                "observed" if delta is not None else "policy_estimate",
                            "ready_after_base_charge": min(5, ready + newly_full) if ready is not None else None,
                            "estimated_reward_rules": "visible recovery icon=20 points; delayed reward discounted"},
                  unknown_fields=sorted(set(unknown)), evidence={"training_source": source, "participants": detail,
                      "vitality_preview": preview if preview_bound else None},
                  score_basis="observed gains and progress; labelled policy estimates for energy and future usefulness")
    if preferences_configured(goal):result['decision_preferences']=user_preferences(goal)
    from core.auto_trainer.larc_strategy181_scoring import apply_training_increment
    return apply_training_increment(state, candidate, goal, result,
        delta_percent=energy_percent, gain_value=gain_value)
