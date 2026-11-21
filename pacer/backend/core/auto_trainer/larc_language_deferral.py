"""The one optional first-expedition hazard; no general unprepared-race bypass."""
import json
from pathlib import Path

from core.auto_trainer.larc_adaptation import LANGUAGE_DEFERRED_PHASES


def record_purchase_plan(run_root, turn, recommendation, goal, *, grid=None):
    """Persist the current choice, including false, so stale truth cannot win."""
    from core.auto_trainer.larc_policy import overseas_purchase_v3_enabled
    if not overseas_purchase_v3_enabled(goal):
        return
    from scripts.ug_parent_one_turn_pipeline import _atomic_write_json
    run_root = Path(run_root)
    selected = recommendation.get("language_debuff_deferred") is True
    record = {"schema_version": "larc_purchase_plan.v3", "run_session_id": run_root.name,
              "through_turn": turn, "language_debuff_deferred": selected,
              "selection_basis": recommendation.get("language_deferral_basis"),
              "reason": recommendation.get("reason")}
    if isinstance(grid, dict):
        record.update({key: grid[key] for key in ("levels", "points") if key in grid})
    from core.auto_trainer.larc_strategy_params import params_for_goal, unified_larc_enabled
    basis = recommendation.get("language_deferral_basis") or {}
    ordinal = basis.get("selected_ordinal") if isinstance(basis, dict) else None
    if (selected and unified_larc_enabled(goal) and params_for_goal(goal).arc1_language_deferral_enabled
            and type(ordinal) is int and 36 <= ordinal <= 42
            and basis.get("exclusive_end_ordinal") == 43
            and basis.get("scope") == "first_expedition_language_only"):
        from core.auto_trainer.larc_calendar import free_action_days
        record.update(scope="first_expedition_language_only", selected_ordinal=ordinal,
                      valid_through_turn=turn + len(free_action_days(ordinal, 43)))
    _atomic_write_json(run_root / "session/larc_purchase_plan_v3.json", record)


def _selected_language_deferred_phase(run_root, phase, turn, goal, *, levels=None, points=None, state=None):
    """Use only this run/turn's explicit v3 purchase choice; old goals stay old."""
    if phase not in LANGUAGE_DEFERRED_PHASES.values():
        return phase
    from core.auto_trainer.larc_policy import overseas_purchase_v3_enabled
    if not overseas_purchase_v3_enabled(goal):
        return phase
    run_root = Path(run_root)
    try:
        plan = json.loads((run_root / "session/larc_purchase_plan_v3.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        plan = None
    from core.auto_trainer.larc_strategy_extension import active_provider
    from core.auto_trainer.larc_strategy_params import params_for_goal, unified_larc_enabled
    params = params_for_goal(goal)
    race_recheck = active_provider().race_language_recheck_enabled(params)
    recheck_ordinal = {"first_overseas_race": 40, "first_arc": 42}.get(phase)
    if (race_recheck and recheck_ordinal is not None
            and isinstance(levels, list) and len(levels) == 10 and type(points) is int and points >= 0):
        from core.auto_trainer.larc_purchase_v3 import first_arc_language_reserve_decision
        deferred, _, _ = first_arc_language_reserve_decision(
            state, goal, levels, points, recheck_ordinal, balance_resources=True)
        if not deferred:
            return phase
        return next(name for name, base in LANGUAGE_DEFERRED_PHASES.items() if base == phase)
    if (not isinstance(plan, dict) or plan.get("schema_version") != "larc_purchase_plan.v3"
            or plan.get("run_session_id") != run_root.name
            or type(turn) is not int or type(plan.get("through_turn")) is not int
            or plan.get("language_debuff_deferred") is not True):
        return phase
    if unified_larc_enabled(goal) and params.arc1_language_deferral_enabled:
        from core.auto_trainer.larc_calendar import free_action_days
        ordinal, valid = plan.get("selected_ordinal"), plan.get("valid_through_turn")
        plan_levels = plan.get("levels")
        if (plan.get("scope") != "first_expedition_language_only"
                or type(ordinal) is not int or not 36 <= ordinal <= 42
                or type(valid) is not int
                or valid != plan["through_turn"] + len(free_action_days(ordinal, 43))
                or not plan["through_turn"] <= turn <= valid
                or not isinstance(plan_levels, list) or len(plan_levels) != 10 or plan_levels[4] != 1):
            return phase
        if race_recheck and recheck_ordinal is not None:
            fresh_levels, fresh_points = plan_levels, plan.get("points")
            try:
                ctx_raw = json.loads((run_root / "session/larc_expedition_context.json").read_text(encoding="utf-8"))
                if (isinstance(ctx_raw, dict) and ctx_raw.get("run_session_id") == run_root.name
                        and ctx_raw.get("through_turn") == turn and ctx_raw.get("balance_current") is True
                        and isinstance(ctx_raw.get("levels"), list) and len(ctx_raw["levels"]) == 10
                        and type(ctx_raw.get("points")) is int):
                    fresh_levels, fresh_points = ctx_raw["levels"], ctx_raw["points"]
            except (OSError, ValueError, TypeError):
                pass
            if type(fresh_points) is int and fresh_points >= 0:
                from core.auto_trainer.larc_purchase_v3 import first_arc_language_reserve_decision
                deferred, _, _ = first_arc_language_reserve_decision(
                    state, goal, fresh_levels, fresh_points, recheck_ordinal, balance_resources=True)
                if not deferred:
                    return phase
    elif plan["through_turn"] != turn:
        return phase
    return next(name for name, base in LANGUAGE_DEFERRED_PHASES.items() if base == phase)


def selected_language_deferred_phase(run_root, phase, turn, goal, *, levels=None, points=None, state=None):
    """Select Niel power deferral via LarcStrategyParams; preserve language API."""
    selected = _selected_language_deferred_phase(run_root, phase, turn, goal, levels=levels, points=points, state=state)
    from core.auto_trainer.larc_strategy_params import params_for_goal, unified_larc_enabled
    if phase == "first_overseas_race" and unified_larc_enabled(goal) and params_for_goal(goal).arc1_defer_niel_power:
        return ("first_overseas_race_defer_power_language"
                if selected == "first_overseas_race_defer_language"
                else "first_overseas_race_defer_power")
    return selected


def unresolved_hazards_within(text, allowed):
    """Bind status to hazard by rows or an unambiguous uniform token order."""
    import re
    hazards = ("不习惯的草地", "未知赛道", "时差疲劳", "海外饮食", "长途旅行", "语言不通")
    statuses = {"未克服", "已克服"}
    compact = "".join(str(text).split())
    if not compact.count("未克服"):
        return False
    pattern = "|".join((*hazards, *sorted(statuses)))
    rows = []
    for line in str(text).splitlines():
        tokens = re.findall(pattern, "".join(line.split()))
        if not tokens:
            continue
        labels = [t for t in tokens if t in hazards]
        marks = [t for t in tokens if t in statuses]
        if len(labels) == len(marks) == 1:
            rows.append((labels[0], marks[0]))
        elif marks:
            rows = []
            break
    if len(rows) != compact.count("已克服") + compact.count("未克服"):
        tokens = re.findall(pattern, compact)
        if len(tokens) % 2:
            return False
        pairs = list(zip(tokens[::2], tokens[1::2]))
        if all(a in hazards and b in statuses for a,b in pairs):
            rows = pairs
        elif all(a in statuses and b in hazards for a,b in pairs):
            rows = [(b,a) for a,b in pairs]
        else:
            return False
    labels = [a for a,_ in rows]
    unresolved = [a for a,b in rows if b == "未克服"]
    return (len(labels) == len(set(labels)) and bool(unresolved)
            and len(unresolved) == compact.count("未克服") and set(unresolved) <= set(allowed))


def language_only_unresolved(text):
    """Require the single uncleared label to be language, not a nearby hazard.

    OCR commonly places status before or after its row label. A separate line
    is accepted only when its adjacent row is unambiguous; merged tokens must
    put the label and status in one segment. Unknown/ambiguous rows stay closed.
    """
    import re
    compact = "".join(str(text).split())
    if compact.count("未克服") != 1 or compact.count("语言不通") != 1:
        return False
    lines = ["".join(line.split()) for line in str(text).splitlines() if line.strip()]
    hazards = ("不习惯的草地", "未知赛道", "时差疲劳", "海外饮食", "长途旅行", "语言不通")
    # An explicit same-row pair provides the strongest, position-neutral link.
    for line in lines:
        if "未克服" in line and "语言不通" in line:
            if sum(label in line for label in hazards) == 1:
                return True
    # For status-on-its-own OCR lines, ensure only one adjacent hazard exists.
    for i, line in enumerate(lines):
        if line != "未克服":
            continue
        adjacent = [lines[j] for j in (i - 1, i + 1) if 0 <= j < len(lines)
                    and lines[j] in hazards]
        if adjacent == ["语言不通"]:
            return True
    # Single-line compact reports are usable only if the status sits at an
    # outer edge of the language row, not between two distinct hazard labels.
    tokens = re.findall("|".join((*hazards, "未克服", "已克服")), compact)
    i = tokens.index("未克服")
    left = tokens[i - 1] if i else None
    right = tokens[i + 1] if i + 1 < len(tokens) else None
    return ((left == "语言不通" and right not in hazards)
            or (right == "语言不通" and left not in hazards))
