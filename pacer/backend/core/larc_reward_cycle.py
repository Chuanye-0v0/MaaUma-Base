"""Run-local three-reward cycles, separate from observed/earned rewards."""
from copy import deepcopy


ACTIVE_CYCLE_STATES = {"observed", "projected", "aligned"}
ONE_TIME_CONDITIONS = {"charming", "practice_good"}
REWARD_FAMILIES = {
    "numeric_gain": {"numeric_gain", "stat_gain", "skill_pt"},
    "positive_condition": {"positive_condition", "charming", "practice_good"},
}


def _condition_replacement(reward, source, observed=None):
    # Only these condition awards become stats after acquisition. Recovery
    # rewards, including max-vitality recovery, keep their normal cycle.
    result = (deepcopy(observed) if observed is not None else
              {"status": "accepted", "kind": "stat_gain", "amount": None, "earned": False})
    result["replacement"] = {
        "original_reward": deepcopy(reward), "source": deepcopy(source),
        "status": "observed" if observed is not None else "projected",
        "basis": "current_reward_observation" if observed is not None else
                 "acquired_condition_becomes_stat_gain",
    }
    return result


def reward_matches(left, right):
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    a, b = left.get("kind"), right.get("kind")
    return bool(a and b and (a == b or b in REWARD_FAMILIES.get(a, ())
                             or a in REWARD_FAMILIES.get(b, ())))


def observe_cycle(current, upcoming, source, previous=None):
    """A fresh information row rebases the queue, including changed ordering."""
    rewards = [current, *(upcoming if isinstance(upcoming, list) else [])]
    if len(rewards) != 3 or any(not isinstance(r, dict) or r.get("status") != "accepted"
                               or not r.get("kind") for r in rewards):
        return None
    rewards = [_condition_replacement(r, source)
               if r.get("kind") in ONE_TIME_CONDITIONS and r.get("earned") is True else r
               for r in rewards]
    return {"schema_version": "larc_reward_cycle.v1", "status": "observed",
            "sequence": deepcopy(rewards), "position": 0, "source": deepcopy(source),
            "ss_participations": (previous or {}).get("ss_participations", 0),
            "basis": "observed_three_rewards_user_cycle_rule"}


def cycle_window(cycle):
    if not isinstance(cycle, dict) or cycle.get("status") not in ACTIVE_CYCLE_STATES:
        return None
    sequence, position = cycle.get("sequence"), cycle.get("position")
    if (not isinstance(sequence, list) or len(sequence) != 3 or type(position) is not int
            or not 0 <= position < 3 or any(not isinstance(r, dict) or not r.get("kind")
                or r.get("status") != "accepted" for r in sequence)):
        return None
    return sequence[position:] + sequence[:position]


def advance_cycle(cycle, reward, source, *, guaranteed):
    result = deepcopy(cycle)
    window = cycle_window(result)
    if window is None or not guaranteed or not reward_matches(reward, window[0]):
        result.update(status="needs_alignment", last_execution_source=deepcopy(source))
        return result
    if window[0]["kind"] in ONE_TIME_CONDITIONS:
        result["sequence"][result["position"]] = _condition_replacement(window[0], source)
    result.update(position=(result["position"] + 1) % 3, status="projected",
                  ss_participations=result.get("ss_participations", 0) + 1,
                  last_execution_source=deepcopy(source))
    return result


def align_cycle(cycle, current, source):
    """Use normal subsequent observations; do not request an effects audit."""
    result = deepcopy(cycle)
    if not isinstance(current, dict) or current.get("status") != "accepted":
        return result
    last = result.get("last_execution_source", {}).get("turn_index", -1)
    if source.get("turn_index", -1) <= last:
        return result  # A repeated pre-action frame cannot undo a consumed SS.
    window = cycle_window(result)
    if window is not None:
        expected, kind = window[0], current.get("kind")
        if expected["kind"] in ONE_TIME_CONDITIONS and kind in {"stat_gain", "numeric_gain"}:
            # Also accept a condition acquired elsewhere or a pre-patch cache.
            # Replace this slot rather than jumping to the other stats slot.
            result["sequence"][result["position"]] = _condition_replacement(
                expected, source, current if kind == "stat_gain" else None)
            result.update(status="aligned", last_current_source=deepcopy(source))
            return result
        if (expected.get("replacement", {}).get("status") == "projected"
                and kind and kind not in REWARD_FAMILIES):
            # A specific current reward overrides the rule-derived substitute.
            observed = deepcopy(current)
            observed["replacement"] = {**deepcopy(expected["replacement"]),
                "status": "observed", "source": deepcopy(source), "basis": "current_reward_observation"}
            result["sequence"][result["position"]] = observed
            result.update(status="aligned", last_current_source=deepcopy(source))
            return result
    if window is not None and reward_matches(current, window[0]):
        result.update(status="aligned", last_current_source=deepcopy(source))
        return result
    matches = [i for i, reward in enumerate(result.get("sequence", []))
               if reward_matches(current, reward)]
    if len(matches) == 1:
        # Position is observed; do not invent missing participation counts.
        result.update(position=matches[0], status="aligned", last_current_source=deepcopy(source))
    else:
        result["status"] = "needs_alignment"
    return result


def participant_cycle(participant, source):
    """Return a linked current-run window; an unknown association is not a cycle."""
    packet = participant.get("ss_reward_cycle")
    if isinstance(packet, dict):
        rewards = cycle_window(packet)
    else:
        packet = participant.get("ss_information_preview", {})
        if (not isinstance(packet, dict) or packet.get("status") != "observed_information"
                or packet.get("freshness") not in {"same_turn", "historical"}):
            return None
        seeded = observe_cycle(packet.get("current"), packet.get("upcoming"), packet.get("source"))
        rewards = cycle_window(seeded)
    if rewards is None or not isinstance(source, dict):
        return None
    origin, association = packet.get("source"), packet.get("association")
    if (not isinstance(origin, dict) or not isinstance(association, dict)
            or association.get("status") != "accepted"
            or origin.get("run_session_id") != source.get("run_session_id")
            or type(origin.get("turn_index")) is not int
            or not 0 <= origin["turn_index"] <= source.get("turn_index", -1)):
        return None
    current = participant.get("ss_reward", participant.get("current_reward", {}))
    if isinstance(current, dict) and current.get("status") == "accepted":
        if current.get("earned") is True or not reward_matches(current, rewards[0]):
            return None
    return rewards
