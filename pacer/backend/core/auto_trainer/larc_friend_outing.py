"""Mei's five outings: bounded effects and local recovery opportunity cost.

The support bonus is not inferred from a yellow hat or a friend badge. Unknown
bonuses use sourced planning references/defaults. Scores are policy values,
never promised game rewards or receipt-validation thresholds.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

from core.auto_trainer.larc_training_value import energy_continuity, gain_value
from core.auto_trainer.larc_policy import policy_version
from core.auto_trainer.larc_recovery import planning_context, recovery_value, recovery_weight
from core.auto_trainer.run_support_profile import outing_inputs
from core.auto_trainer.mei_outing_effects import merge_verified_packet_effects
from core.auto_trainer.larc_capacity import capacity_bounds, planning_capacity
from core.auto_trainer.larc_recovery_routes import REST_SCENARIOS
from core.auto_trainer.larc_mood import MOODS
from core.auto_trainer.larc_mood_calendar import action_mood_value


CHARACTER = "satake_mei"
SCHEMA = "larc_friend_outing.v1"
TITLES = {
    1: "礼赞如雨", 2: "不远的将来就能克服？", 3: "缘分终将与梦想相连",
    4: "憧憬化为起点", 5: "许下星空的誓言",
}
# Unboosted values; stage 3 is the upper, recovery branch.
_BASE = {
    1: ((30, 30), {"guts": (5, 5)}, 1),
    2: ((25, 25), {"power": (5, 5), "guts": (5, 5)}, 1),
    3: ((35, 35), {"guts": (15, 15)}, 1),
    4: ((25, 25), {"guts": (20, 20)}, 0),
    5: ((35, 40), {"guts": (5, 10)}, 1),
}


def outing_effects(stage, *, recovery_bonus=None, event_bonus=None):
    if type(stage) is not int or stage not in _BASE:
        raise ValueError("mei_outing_stage_invalid")
    for value, maximum in ((recovery_bonus, 80), (event_bonus, 20)):
        if value is not None and (type(value) is not int or not 0 <= value <= maximum):
            raise ValueError("mei_outing_bonus_invalid")
    recovery, stats, mood = _BASE[stage]
    def scaled(bounds, bonus, maximum):
        lo, hi = (0, maximum) if bonus is None else (bonus, bonus)
        return [math.floor(bounds[0] * (100 + lo) / 100),
                math.floor(bounds[1] * (100 + hi) / 100)]
    return {
        "stage": stage, "title": TITLES[stage],
        "vitality_points": scaled(recovery, recovery_bonus, 80),
        "stats": {key: scaled(value, event_bonus, 20) for key, value in stats.items()},
        "mood_levels": mood, "friend_bond": 5 if stage < 5 else None,
        "choice": "top" if stage == 3 else None,
        "condition": "positive_thinking" if stage == 3 else None,
        "skill_hint": {"rarity_required": "SSR", "name": "永不言弃", "level_range": [1, 3]} if stage == 5 else None,
        "unknown_fields": (["support_recovery_bonus"] if recovery_bonus is None else [])
            + (["support_event_bonus"] if event_bonus is None else [])
            + (["final_event_outcome", "support_rarity"] if stage == 5 else []),
    }


def verified_source(source, *, run_id=None, turn=None):
    if (not isinstance(source, dict) or not isinstance(source.get("run_session_id"), str)
            or not source["run_session_id"] or type(source.get("turn_index")) is not int
            or source["turn_index"] < 1
            or run_id is not None and source["run_session_id"] != run_id
            or turn is not None and source["turn_index"] != turn):
        return False
    try:
        path = Path(source["path"])
        return path.is_absolute() and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == source["sha256"]
    except (KeyError, TypeError, OSError):
        return False


def outing_completion_value(state, goal, stage, bonus):
    """Incremental fifth-outing value, not the whole common ending reward.

    Reference: https://umamusumelabo.com/chara_support/ssr-satakemei
    Completed ending: 15 power/25 guts/30 Pt; incomplete: 15/18/20,
    before the card's event effectiveness. Skill hint value is a preference,
    not free skill Pt or an automatically learned skill.
    """
    empty = {"score": 0., "components": {}, "ending_increment": {}, "hint_status": "not_fifth_outing"}
    if stage != 5:
        return empty
    from core.auto_trainer.larc_attribute_plan import free_windows
    remaining, _ = free_windows(state, goal, {})
    event = bonus.get("event")
    multiplier = 1 + (event if type(event) is int else 0) / 100
    increment = {"guts": math.floor(25 * multiplier) - math.floor(18 * multiplier),
                 "skill_pt": math.floor(30 * multiplier) - math.floor(20 * multiplier)}
    ending_credit = .85 * (.98 ** max(0, remaining - 1))
    numeric = gain_value(state, increment, goal, project=False)
    ending = numeric["score"] * ending_credit
    packet = bound_outing_packet(state) or {}
    profile = bound_outing_inputs(packet)
    summary = state.scenario_state.get("larc_event_progress", {})
    run_id = packet.get("source", {}).get("run_session_id")
    if (summary.get("run_id") != run_id
            or type(summary.get("current_turn_index")) is int and summary["current_turn_index"] > state.turn):
        summary = {}
    def matches(row):
        return any(str(row.get(k, "")).removeprefix("cn:") == "永不言弃"
                   for k in ("skill_id", "name", "skill_name", "name_zh_cn"))
    learned = any(matches(row) for row in summary.get("learned_skills", []) if isinstance(row, dict))
    hints = [row for row in summary.get("skill_hints", []) if isinstance(row, dict) and matches(row)]
    level = max((row.get("hint_level", 0) for row in hints if type(row.get("hint_level")) is int), default=0)
    settings = state.scenario_state.get("larc_outing_value_preferences", {})
    usefulness = settings.get("never_give_up_usefulness", 1.)
    usefulness = min(1., max(0., usefulness)) if type(usefulness) in {int, float} else 1.
    skill_pt = state.current_stats.get("skill_pt", 0)
    affordable = min(1., max(0., skill_pt) / 153)
    hint = 0.
    status = "rarity_unknown"
    if learned:
        status = "already_learned"
    elif profile and profile["rarity"] == "SSR":
        # Only the guaranteed Lv1 branch is credited. Unknown inventory gives
        # a discounted option value, not a claim that the skill is absent.
        status = "existing_hint" if hints else "inventory_incomplete_option_value"
        hint = (0. if level >= 1 else 17. if hints else 26. * .5)
        hint *= numeric["weights"]["skill_pt"] * usefulness * affordable
    components = {"outing_completion_increment": round(ending, 4), "outing_skill_hint_option": round(hint, 4)}
    return {"score": round(sum(components.values()), 4), "components": components,
            "ending_increment": increment, "ending_credit": round(ending_credit, 4),
            "hint_status": status, "hint_usefulness": usefulness,
            "basis": "incremental_ending_over_incomplete_outing; discounted_usable_hint_option",
            "unknown_fields": ["completion_result"] + (["support_event_bonus"] if event is None else [])}


def domestic_period(period):
    """L'Arc's July-September expedition has no friend-outing action."""
    if not isinstance(period, dict) or period.get("status") != "verified":
        return False
    value = period.get("value", {})
    if value.get("year_index") == 1:
        return True
    return (value.get("year_index") in {2, 3} and type(value.get("month")) is int
            and value["month"] in {1, 2, 3, 4, 5, 6, 10, 11, 12}
            and value.get("half") in {"first", "second"})


def _bound_menu_source(state, packet):
    source = packet.get('source') or {}
    captures = [item.raw_data.get('capture') or {} for item in state.training_options]
    return (len(captures) == 5 and verified_source(source, turn=state.turn)
            and all(c.get('run_session_id') == source['run_session_id']
                    and c.get('turn_index') == state.turn for c in captures))


def bound_outing_progress(state):
    """Verify stable progress carried by a suppressed, non-actionable entry."""
    packet = state.scenario_state.get('larc_friend_outing')
    if (not isinstance(packet, dict) or packet.get('schema_version') != SCHEMA
            or packet.get('status') != 'unknown' or packet.get('character') != CHARACTER
            or packet.get('reason') != 'mei_outing_temporarily_excluded' or packet.get('pending') is not None
            or not domestic_period(state.scenario_state.get('larc_period'))
            or not _bound_menu_source(state, packet)):
        return None
    progress = packet.get('confirmed_progress')
    if (not isinstance(progress, dict) or progress.get('unlocked') is not True
            or type(progress.get('completed')) is not int or not 0 <= progress['completed'] <= 5):
        return None
    prior = progress.get('progress_source')
    source = packet['source']
    # The producer has already checked the historical receipt against the
    # run ledger. Old captures can be released; never treat them as this page.
    if (not isinstance(prior, dict) or prior.get('run_session_id') != source['run_session_id']
            or type(prior.get('turn_index')) is not int or not 1 <= prior['turn_index'] <= state.turn):
        return None
    return {**progress, 'run_session_id': source['run_session_id'],
            'support_card_profile': packet.get('support_card_profile')}


def bound_outing_packet(state):
    packet = state.scenario_state.get("larc_friend_outing")
    if (not isinstance(packet, dict) or packet.get("schema_version") != SCHEMA
            or packet.get("status") != "accepted" or packet.get("character") != CHARACTER
            or packet.get("unlocked") is not True or type(packet.get("completed")) is not int
            or not 0 <= packet["completed"] < 5 or packet.get("pending") is not None
            or not domestic_period(state.scenario_state.get("larc_period"))):
        return None
    return packet if _bound_menu_source(state, packet) else None



def bound_outing_inputs(packet):
    """Planning inputs for a packet already accepted by bound_outing_packet.

    Only callers with a current accepted Mei outing invoke this helper. The
    public two-argument outing_inputs(None, run) keeps its unbound behavior.
    Explicit current packet effects are merged only after the original
    accepted/same-run source check; profile explicit values remain first.
    """
    if (not isinstance(packet, dict) or packet.get("schema_version") != SCHEMA
            or packet.get("status") != "accepted" or packet.get("character") != CHARACTER
            or packet.get("unlocked") is not True or type(packet.get("completed")) is not int
            or not 0 <= packet["completed"] < 5 or packet.get("pending") is not None):
        return None
    run_id = (packet.get("source") or {}).get("run_session_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    result = outing_inputs(packet.get("support_card_profile"), run_id, confirmed_outing=True)
    legacy = packet.get("card_bonuses") or {}
    if (isinstance(legacy, dict) and legacy.get("status") == "accepted"
            and verified_source(legacy.get("source"), run_id=run_id)):
        result = merge_verified_packet_effects(result, legacy)
    return result

def recovery_candidates(state, goal, *, future_reference=None):
    packet = bound_outing_packet(state)
    stage = packet["completed"] + 1 if packet else None
    # Current and future routes share one already-bound effect resolution.
    profile = bound_outing_inputs(packet) if packet else None
    bonus, usable_bonus = profile or {}, profile is not None
    effects = outing_effects(stage,
        recovery_bonus=bonus.get("recovery") if usable_bonus else None,
        event_bonus=bonus.get("event") if usable_bonus else None) if packet else None
    if effects and profile and profile.get("resolution"):
        effects["support_effect_resolution"] = profile["resolution"]
    if effects and profile and profile["rarity"] is not None and stage == 5:
        effects["unknown_fields"].remove("support_rarity")
        if profile["rarity"] != "SSR":
            effects["skill_hint"] = None
    vitality = min(100., max(0., state.vitality))  # The provider reports bar percent.
    capacity_lo, capacity_hi = capacity_bounds(state)
    maximum_known = capacity_lo == capacity_hi
    _, urgency = recovery_weight(state)
    mood = state.scenario_state.get("mood", "unknown")
    mood_known = mood in MOODS
    unknown = (effects["unknown_fields"] if effects else []) + ([] if maximum_known else ["max_vitality"])
    if not mood_known:
        unknown.append("mood")
    result = []
    options = [("rest.normal", [30, 70], {}, 0, False)]
    if effects:
        options.insert(0, (f"outing.satake_mei.{stage}", effects["vitality_points"],
            {key: bounds[0] for key, bounds in effects["stats"].items()}, effects["mood_levels"], True))
    for action_id, recovery, gains, mood_up, limited in options:
        mood_value = action_mood_value(state, goal, mood_up,
            grants_mood_down_protection=bool(limited and effects.get('condition') == 'positive_thinking'))
        lower = min(100 - vitality, recovery[0] * 100 / capacity_hi)
        upper = min(100 - vitality, recovery[1] * 100 / capacity_lo)
        scenarios = [(recovery[0], 1.)] if limited else REST_SCENARIOS
        outcomes = [(min(100 - vitality, points * 100 / planning_capacity(state)), weight)
                    for points, weight in scenarios]
        gains["skill_pt"] = 0
        numeric = gain_value(state, gains, goal)
        plans = [(energy_continuity(state, goal, usable, consumed_outing=limited, consumed_gains=gains, future_reference=future_reference), weight)
                 for usable, weight in outcomes]
        # Representative route uses the central scenario; scalar values use
        # the same weighted branches as the score, including overflow clipping.
        continuity = dict(plans[len(plans) // 2][0])
        for key in ("score", "after_vitality_percent", "continuation_value_delta", "future_value_after",
                    "estimated_recovery_turns_after", "estimated_trainings_supported"):
            if all(type(plan.get(key)) in {int, float} for plan, _ in plans):
                continuity[key] = round(sum(plan[key] * weight for plan, weight in plans), 4)
        if not limited:
            continuity["rest_outcomes"] = [{"points": points, "planning_weight": weight,
                "after_vitality_percent": plan.get("after_vitality_percent"),
                "continuation_value_delta": plan.get("continuation_value_delta"),
                "future_route_after": plan.get("future_route_after")} for (points, weight), (plan, _) in zip(scenarios, plans)]
        chain = outing_chain_value(state, goal, stage, bonus if usable_bonus else {}) if limited else {"score": 0.}
        completion = outing_completion_value(state, goal, stage, bonus if usable_bonus else {}) if limited else {"components": {}}
        # Advancing the chain is valuable when using a recovery turn anyway.
        # Don't burn an almost-full outing just to collect future option value.
        chain["current_recovery_need_factor"] = min(1., max(0., (85 - vitality) / 35))
        chain["score"] = round(chain["score"] * chain["current_recovery_need_factor"], 4)
        # One mood level, not a reset to best mood. Limited outings keep their
        # unused recovery value for a later weak training turn.
        parts = {**numeric["components"], "recovery": sum(recovery_value(state, usable) * weight for usable, weight in outcomes),
                 "energy_continuity": continuity["score"],
                 "mood_recovery": mood_value["score"],
                 "outing_chain_progress": chain["score"],
                 "unused_outing_recovery": 0., **completion["components"]}
        no_need = parts["recovery"] <= 0 and mood_value["score"] <= 0
        result.append({"action_id": action_id, "action": "outing" if limited else "rest",
            "target": f"satake_mei:{stage}" if limited else None,
            "score": round(sum(parts.values()), 4), "components": parts,
            "policy_version": policy_version(goal), "phase": numeric["phase"],
            "planning": {**planning_context(state), "energy_horizon": continuity, "outing_chain": chain,
                         "outing_completion": completion, "mood_recovery": mood_value},
            "rejected": no_need, "source_confidence": .9,
            "risk_flags": ["no_effective_recovery_need"] if no_need else [],
            "effects": effects if limited else {"vitality_points": recovery, "random_outcome": True,
                "planning_scenarios": [{"points": points, "weight": weight} for points, weight in scenarios],
                "probability_status": "unknown; planning weights are not game probabilities"},
            "recovery_percent": [round(lower, 4), round(upper, 4)],
            "unknown_fields": unknown if limited else ["rest_outcome", "rest_condition_outcome"],
            "score_basis": "current yield plus shared future route value; bounded outing effects and weighted rest scenarios",
            "pre_expedition_weight": urgency, "source": packet["source"] if packet else None})
    from core.auto_trainer.larc_charge_unlock import charge_unlock_window
    from core.auto_trainer.larc_policy import training_balance_v2_enabled
    opening=charge_unlock_window(state,goal)
    if training_balance_v2_enabled(goal) and opening and packet is None:
        # Ordinary recreation is useful before charge unlock. Price one mood
        # level conservatively; random healing is not promised as a 50-point
        # rest or borrowed from Mei's limited outings.
        normal_mood=action_mood_value(state,goal,1)
        if normal_mood['score']>0:
            continuity=energy_continuity(state,goal,0.,consumed_gains={})
            parts={'recovery':0.,'energy_continuity':continuity['score'],
                   'mood_recovery':normal_mood['score']}
            result.append({'action_id':'outing.normal','action':'outing','target':'normal',
                'score':round(sum(parts.values()),4),'components':parts,
                'policy_version':policy_version(goal),'phase':numeric['phase'],
                'planning':{**planning_context(state),'energy_horizon':continuity,
                    'mood_recovery':normal_mood,'opening_charge_unlock':opening,
                    'recovery_basis':'zero_heal_lower_bound; ordinary_outing_result_varies'},
                'rejected':False,'source_confidence':.85,
                'unknown_fields':['normal_outing_recovery','normal_outing_extra_mood'],
                'risk_flags':[],'score_basis':'opening energy preservation and one mood level; no promised random healing',
                'participants_progress':[]})
    from core.auto_trainer.larc_recovery_policy import apply_recovery_rules
    apply_recovery_rules(state, goal, result)
    return result


def outing_chain_value(state, goal, stage, bonus):
    """Discounted option value relative to future rest turns, not free healing.

    Each later stage still occupies a recovery turn. Only its extra stat/mood
    value and healing above a 50-point rest estimate receive credit. The
    remaining calendar bounds how many such recovery turns can be useful.
    """
    from core.auto_trainer.larc_attribute_plan import free_windows
    remaining, _ = free_windows(state, goal, {})
    opportunities = min(5 - stage, max(0., (remaining - 1) / 5))
    score, rows = 0., []
    for distance, future_stage in enumerate(range(stage + 1, 6), 1):
        fraction = min(1., max(0., opportunities - (distance - 1)))
        if not fraction:
            break
        effects = outing_effects(future_stage, recovery_bonus=bonus.get("recovery"), event_bonus=bonus.get("event"))
        gains = {key: value[0] for key, value in effects["stats"].items()}
        gains["skill_pt"] = 0
        extra = gain_value(state, gains, goal)["score"] + 6 * effects["mood_levels"]
        extra += .5 * max(0., effects["vitality_points"][0] - 50)
        value = fraction * (.55 ** distance) * extra
        score += value
        rows.append({"stage": future_stage, "future_turns_required": distance,
                     "discounted_extra_value": round(value, 4)})
    return {"score": round(score, 4), "stages": rows, "remaining_free_actions": remaining,
            "basis": "future recovery alternatives; one action per outing, no immediate future heal or promised event"}
