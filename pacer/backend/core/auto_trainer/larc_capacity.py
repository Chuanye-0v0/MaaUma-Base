"""Run-local vitality capacity from existing receipts, never from reference events."""
from __future__ import annotations

import json
from pathlib import Path


def _run(state):
    ids = {c.raw_data.get("capture", {}).get("run_session_id") for c in state.training_options}
    ids.discard(None)
    return next(iter(ids)) if len(ids) == 1 else None


def capacity_bounds(state):
    packet = state.scenario_state.get("larc_capacity", {})
    bounds = packet.get("bounds", [])
    if (packet.get("run_session_id") == _run(state) and _run(state)
            and type(packet.get("through_turn")) is int and packet["through_turn"] <= state.turn
            and len(bounds) == 2 and all(type(v) in {int, float} for v in bounds)
            and 100 <= bounds[0] <= bounds[1] <= 120):
        return tuple(bounds)
    outing = state.scenario_state.get("larc_friend_outing", {})
    source = outing.get("source", {})
    maximum = outing.get("max_vitality")
    if (outing.get("status") == "accepted" and source.get("run_session_id") == _run(state)
            and _run(state) and source.get("turn_index") == state.turn
            and type(maximum) is int and 100 <= maximum <= 120):
        return maximum, maximum
    return 100, 120


def planning_capacity(state):
    low, high = capacity_bounds(state)
    return (low + high) / 2


def recover_points(current, maximum, points, maximum_increase=0):
    """Apply a heal at its use-time energy; increasing capacity is not a heal."""
    new = min(120., maximum + maximum_increase)
    return min(new, max(0., min(maximum, max(0., current)) + points)), new


def recovery_delta(state, points, maximum_increase=0, *, maximum=None):
    """Convert old bar -> points -> increased cap -> new bar (increase isn't heal)."""
    old = planning_capacity(state) if maximum is None else maximum
    current = min(100., max(0., state.vitality))
    after_points, new = recover_points(current * old / 100, old, points, maximum_increase)
    return after_points * 100 / new - current


def capacity_context(session_dir, run_id, turn, *, roster_path=None):
    """Rebuild cheaply from persisted facts; duplicate/restart receipts count once."""
    root = Path(session_dir)
    notes = []
    def read(path):
        if path is None or not Path(path).is_file():
            return {}
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            if data.get("run_session_id", data.get("run_id")) != run_id:
                raise ValueError("run_mismatch")
            return data
        except (OSError, ValueError, TypeError) as exc:
            notes.append(str(exc))
            return {}
    profile = read(root / "support_card_profile.json")
    maximum = profile.get("max_vitality")
    baseline_turn = (profile.get("max_vitality_source") or {}).get("turn_index", 0)
    if type(baseline_turn) is not int or baseline_turn > turn:
        maximum, baseline_turn = None, 0
    low, high = (maximum, maximum) if type(maximum) is int and 100 <= maximum <= 120 else (100, 120)
    increments = {}
    roster = read(roster_path)
    seen = set()
    count = 0
    for entry in roster.get("ss_execution_participants", []):
        receipt = entry.get("receipt", {})
        at = receipt.get("turn_index")
        identity = entry.get("source_sha256")
        if (not identity or identity in seen or type(at) is not int or at >= turn
                or receipt.get("tap_performed") is not True
                or receipt.get("action_id") != "safe_advance.larc_ss_confirm"):
            continue
        seen.add(identity)
        count += 1
        if at < baseline_turn:
            continue
        for row in entry.get("participants_observation", {}).get("participants", []):
            reward = row.get("current_reward", {})
            if (row.get("win_guaranteed") is True and reward.get("status") == "accepted"
                    and reward.get("kind") == "max_vitality_and_recovery"):
                increments[at] = increments.get(at, 0) + 4
    events = read(root / "event_progress_summary.json")
    actual = {}
    seen_events = set()
    for index, event in enumerate(events.get("confirmed_occurrences", [])):
        at = event.get("turn_index")
        identity = event.get("occurrence_id", event.get("id", str(index)))
        if type(at) is not int or not baseline_turn <= at < turn or identity in seen_events:
            continue
        seen_events.add(identity)
        for effect in event.get("observed_effects", []):
            amount = effect.get("value")
            if (effect.get("kind") == "max_energy" and effect.get("op") == "add"
                    and type(amount) in {int, float} and 0 < amount <= 20):
                actual[at] = actual.get(at, 0) + amount
    # A directly observed effect on the same SS turn supersedes its icon rule.
    increments.update(actual)
    total = sum(increments.values())
    last_observed = roster.get("last_observed_turn")
    legacy_count = len(roster.get("ss_executions", [])) if type(last_observed) is int and last_observed < turn else 0
    return {"schema_version": "larc_capacity.v1", "run_session_id": run_id, "through_turn": turn,
            "bounds": [min(120, low + total), min(120, high + total)],
            "baseline_turn": baseline_turn, "ss_execution_count": max(count, legacy_count),
            "applied_increases_by_turn": increments,
            "basis": "explicit capacity plus completed guaranteed SS +4 rule and observed event deltas; missing base stays a range",
            "notes": notes}
