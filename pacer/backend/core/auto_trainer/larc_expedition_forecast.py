"""Bounded overseas alternatives for the short energy horizon, never purchases.

Only committed adaptations are certain. Fresh, affordable upgrades form separate
planning scenarios sharing one budget; scenario weights are not game probabilities.
"""
from __future__ import annotations

import json
from pathlib import Path

from core.auto_trainer.larc_recovery import period_value

CONTEXT_FILE = "larc_expedition_context.json"
TRAINING_SLOTS = {"guts": 0, "stamina": 1, "power": 2, "speed": 3, "wisdom": 4}
LEVEL_TWO_COSTS = (50, 50, 100, 100, 100, 100, 200, 200, 200, 150)
# Main-stat facility bases at levels 1 and 5. Intermediate values are a bounded
# approximation, used only when a facility level is explicitly supplied.
BASES = {"speed": (10, 16), "stamina": (9, 15), "power": (11, 17),
         "guts": (10, 16), "wisdom": (9, 14)}


def valid_levels(value):
    return (isinstance(value, list) and len(value) == 10
            and all(type(x) is int and 0 <= x <= 3 for x in value))


def load_expedition_context(session_dir, run_id, turn):
    root = Path(session_dir)
    levels, known, observation = [0] * 10, False, {}
    for path in root.glob("larc_adaptation_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        targets = (data.get("plan") or {}).get("targets")
        if (data.get("run_session_id") == run_id and data.get("stage") == "complete"
                and data.get("returned") is True and not data.get("inspect_only") and valid_levels(targets)):
            levels = [max(a, b) for a, b in zip(levels, targets)]
            known = True
    path = root / CONTEXT_FILE
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if (data.get("run_session_id") == run_id and type(data.get("through_turn")) is int
                    and data["through_turn"] <= turn and valid_levels(data.get("levels"))):
                observation = data
                levels = [max(a, b) for a, b in zip(levels, data["levels"])]
                known = True
        except (OSError, ValueError):
            pass
    if not known:
        return {}
    # A balance predating another purchase is not spendable forecast money.
    fresh = observation.get("through_turn") == turn and observation.get("levels") == levels
    return {"status": "accepted", "run_session_id": run_id, "through_turn": turn,
            "levels": levels, "points": observation.get("points") if fresh else None,
            "balance_current": fresh, "basis": "committed_sessions_and_latest_grid"}


def record_expedition_context(session_dir, run_id, turn, grid):
    if (type(turn) is not int or turn < 1 or not isinstance(grid, dict) or not valid_levels(grid.get("levels"))
            or grid.get("errors") or grid.get("pending_cost") != 0
            or type(grid.get("points")) is not int or grid["points"] < 0):
        return
    path = Path(session_dir) / CONTEXT_FILE
    data = {"run_session_id": run_id, "through_turn": turn,
            "levels": grid["levels"], "points": grid["points"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _context(state):
    packet = state.scenario_state.get("larc_expedition_context", {})
    if not isinstance(packet, dict):
        return {}
    runs = {c.raw_data.get("capture", {}).get("run_session_id") for c in state.training_options}
    if (packet.get("status") == "accepted" and packet.get("run_session_id")
            and runs == {packet["run_session_id"]} and valid_levels(packet.get("levels"))
            and type(packet.get("through_turn")) is int and packet["through_turn"] <= state.turn):
        return packet
    return {}


def forecast_metadata(candidate, state=None):
    larc = candidate.scenario_mechanics.get("larc") or {}
    model = larc.get("growth_effect_model") or {}
    result = {}
    if state is not None:
        from core.auto_trainer.larc_training_forecast import growth_snapshot
        result.update(growth_snapshot(state))
    result["friendship"] = candidate.friendship
    if candidate.friendship is None:
        from core.auto_trainer.larc_recovery_timing import _friendship_evidence
        if _friendship_evidence(candidate) == "eligible_partner_observed":
            result.update(friendship=True, friendship_basis="eligible_partner_estimate")
    if model.get("status") == "accepted":
        for key in ("training_level", "training_effect_percent"):
            value = model.get(key)
            if type(value) in {int, float} and 0 <= value <= (5 if key == "training_level" else 500):
                result[key] = value
        multiplier = model.get("skill_pt_multiplier")
        if type(multiplier) in {int, float} and 0 < multiplier < float("inf"):
            result["skill_pt_multiplier"] = multiplier
            result["skill_pt_multiplier_basis"] = "accepted_growth_effect_model"
    preview = larc.get("vitality_preview") or {}
    source, capture = preview.get("source") or {}, candidate.raw_data.get("capture") or {}
    if (preview.get("status") == "accepted" and capture.get("sha256")
            and all(source.get(k) == capture.get(k) for k in ("run_session_id", "turn_index", "sha256"))
            and type(preview.get("delta_percent")) in {int, float}):
        result["net_vitality_percent"] = preview["delta_percent"]
    # Typed observations are additive. Keep the legacy field and every old
    # consumer's units/absence unchanged for saved goals and data-only arms.
    from core.auto_trainer.larc_energy_observation import training_energy_observation
    result["energy_observation"] = training_energy_observation(candidate, state)
    return result


def expedition_scenarios(state, goal, reference, window, training_cost):
    """Transform future overseas layouts, preserving current domestic scoring."""
    if window["kind"] != "expedition_entry":
        return {"active": False, "scenarios": []}
    from core.auto_trainer.larc_adaptation_strategy import deck_training_coverage
    context = _context(state)
    levels = context.get("levels", [0] * 10)
    coverage = deck_training_coverage(state)
    year = period_value(state).get("year_index")
    cases = [{"name": "committed", "levels": levels, "cost": 0, "weight": 1.}]
    points = (context.get("points") if context.get("balance_current")
              and context.get("through_turn") == state.turn else None)
    reserve = sum(cost for level, cost in zip(levels, LEVEL_TWO_COSTS) if level == 1)
    available = []
    if type(points) is int:
        for tab, slot in TRAINING_SLOTS.items():
            if levels[slot] == 2 and coverage["counts"][tab] and points - reserve >= 200:
                available.append((coverage["counts"][tab], tab, slot, 200))
        available.sort(reverse=True)
        # One prospective training purchase, not every tab spending the same Pt.
        available = available[:1]
        if year == 3 and levels[6] == 2 and points - reserve >= 300:
            available.append((0, "courage_three", 6, 300))
    if available:
        cases[0]["weight"] = .5
        for _, name, slot, cost in available:
            upgraded = list(levels)
            upgraded[slot] = 3
            cases.append({"name": f"conditional:{name}", "levels": upgraded,
                          "cost": cost, "weight": .5 / len(available)})
    active = bool(context) or any(row.get("forecast_metadata", {}).get("training_level")
                                 for layout in reference["opportunity_layouts"] for row in layout)
    if not active:
        return {"active": False, "scenarios": []}
    from core.auto_trainer.larc_training_forecast import training_scenarios
    report = training_scenarios(state, goal, reference, window, training_cost, cases=cases)
    return {**report, "deck_coverage": coverage, "race_reserve": reserve}
