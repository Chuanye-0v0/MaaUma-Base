"""Read the Derby fan shortfall from already observed main-menu text; no OCR."""
from __future__ import annotations

from pathlib import Path
import re


def _valid_source(source):
    return (isinstance(source, dict)
            and isinstance(source.get("path"), (str, Path)) and bool(str(source["path"]).strip())
            and isinstance(source.get("sha256"), str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", source["sha256"]) is not None
            and isinstance(source.get("run_session_id"), str) and bool(source["run_session_id"].strip())
            and type(source.get("turn_index")) is int and source["turn_index"] >= 0)


def read_larc_fan_goal(text, *, remaining_turns, source_path, source_sha256,
                       run_session_id, turn_index):
    source = {"path": source_path, "sha256": source_sha256,
              "run_session_id": run_session_id, "turn_index": turn_index}
    if (not isinstance(text, str) or not _valid_source(source)
            or type(remaining_turns) is not int or remaining_turns < 0):
        return None
    compact = "".join(text.split())
    if (compact.count("全国德比中进入前") != 1
            or re.search(r"全国德比\s*中进入前\s*3(?!\d)", text) is None
            or compact.count("参赛条件剩余") != 1):
        return None
    gap = re.search(r"参赛条件剩余([0-9]{1,3}(?:[,，][0-9]{3})+|[0-9]+)人", compact)
    if gap is None:
        gap = re.search(r"(?m)^\s*([0-9]{1,3}(?:[,，][0-9]{3})+|[0-9]+)人\s*\n\s*参赛条件剩余\s*$", text)
    if gap is None:
        return None
    remaining_fans = int(gap.group(1).replace(",", "").replace("，", ""))
    if remaining_fans <= 0:
        return None
    source["path"] = str(source_path)
    return {"schema_version": "larc_fan_goal.v1", "status": "accepted",
            "target_id": "larc_japan_derby_top3", "remaining_fans": remaining_fans,
            "remaining_turns": remaining_turns, "source": source}


def fan_goal_due(goal):
    return (isinstance(goal, dict) and goal.get("schema_version") == "larc_fan_goal.v1"
            and goal.get("status") == "accepted" and goal.get("target_id") == "larc_japan_derby_top3"
            and _valid_source(goal.get("source"))
            and type(goal.get("remaining_fans")) is int and goal["remaining_fans"] > 0
            and type(goal.get("remaining_turns")) is int and 1 <= goal["remaining_turns"] <= 6)


def plan_ss_before_fan_race(goal, text, period, *, growth_observation=None):
    """Conditional SS/expectation fan progress; no supplemental-race schedule."""
    if (not isinstance(goal, dict) or goal.get("status") != "accepted"
            or not _valid_source(goal.get("source")) or goal.get("remaining_turns", 0) < 1
            or period.get("status") != "verified"
            or period.get("source_frame_sha256") != goal["source"]["sha256"]):
        return None
    expectation=None
    growth=growth_observation if isinstance(growth_observation,dict) else {}
    if (growth.get('status')=='accepted' and growth.get('turn_index')==goal['source']['turn_index']
            and growth.get('source_frame_sha256')==goal['source']['sha256']
            and type(growth.get('expectation')) in {int,float} and 0<=growth['expectation']<=200):
        expectation=float(growth['expectation'])
    if expectation is None:
        values = re.findall(r"期待度([0-9]{1,3}(?:\.[0-9]+)?)%", "".join(str(text).split()))
        if len(values)==1 and 0<=float(values[0])<=200:expectation=float(values[0])
    date = period.get("value", {})
    month, half = date.get("month"), date.get("half")
    if (date.get("stage") != "classic" or type(month) is not int
            or not 1 <= month <= 12 or half not in {"first", "second"}):
        return None
    reward_status=('expectation_unknown' if expectation is None else 'threshold_already_visible_reward_unconfirmed'
        if expectation>=40 else 'conditional_event_not_received')
    return {"reason": "compare_ss_expectation_fan_progress", "expectation": expectation,
            "conditional_threshold": 40, "conditional_reward_fans": 2000,
            "predicted_remaining_fans": max(0, goal["remaining_fans"] - 2000) if reward_status=='conditional_event_not_received' else None,
            "remaining_turns": goal["remaining_turns"], "remaining_fans": goal["remaining_fans"],
            "source": goal["source"], "reward_status": reward_status,
            "fans_received":False,"supplemental_race_planned":False}


def fan_route_value(state, *, expectation_delta=0., earliest_actions=1, reference_value=0.):
    """Conditional fan-threshold progress; never add fans or schedule a race.

    The 40%/+2000 event has a recorded in-project instance. The event resolves after SS without a separate action. Delta and half
    credit are planning estimates, not a guaranteed SS expectation formula.
    """
    packet = state.scenario_state.get("larc_fan_plan")
    empty = {"score": 0., "reason": "fan_route_unknown"}
    if not isinstance(packet, dict) or packet.get("reward_status") != "conditional_event_not_received":
        return empty
    source = packet.get("source", {})
    if (not _valid_source(source) or source["turn_index"] != state.turn
            or not state.training_options or any(
                o.raw_data.get("capture", {}).get("run_session_id") != source["run_session_id"]
                or o.raw_data.get("capture", {}).get("turn_index") != state.turn
                for o in state.training_options)):
        return empty
    remaining, gap = packet.get("remaining_turns"), packet.get("remaining_fans")
    expectation = packet.get("expectation")
    if (type(remaining) is not int or type(gap) is not int or gap <= 0
            or type(expectation) not in {int, float} or not 0 <= expectation < 40
            or earliest_actions > remaining or expectation_delta <= 0):
        return dict(empty, reason="fan_reward_route_not_timely")
    progress = min(1., expectation_delta / (40 - expectation))
    coverage = float(packet.get("conditional_reward_fans", 0) >= gap)
    score = max(0., reference_value) * .5 * progress * coverage
    return {"score": round(score, 4), "reason": "conditional_fan_threshold_progress",
            "expectation_delta_estimate": expectation_delta, "threshold_progress_estimate": progress,
            "fan_gap_coverage": coverage, "earliest_actions_estimate": earliest_actions,
            "reward_event_buffer_actions": 0, "conditional_credit": .5, "reference_action_value": round(reference_value, 4),
            "fans_received": False,"supplemental_race_planned":False}


def fan_action_reference(state, goal):
    """Same observed numeric action value for SS and every training tab."""
    from core.auto_trainer.larc_training_value import gain_value, training_numeric_issues
    return max((gain_value(state, option.stats_gain, goal, project=False)["score"]
                * (1 - option.failure_rate / 100) for option in state.training_options
                if not training_numeric_issues(option)), default=0.)
