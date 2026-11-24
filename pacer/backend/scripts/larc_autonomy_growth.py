"""Cheap eligibility checks, then a current-grid growth-upgrade recommendation."""
import json
from pathlib import Path
from core.native_capture_adb import native_instance_id
import re
from time import sleep, monotonic

from core.auto_trainer.larc_adaptation import LarcAdaptationSession
from core.auto_trainer.larc_adaptation_strategy import recommend_growth_adaptation
from core.auto_trainer.safe_advance import ADVANCE_CONFIRM_TOKEN
from scripts.ug_parent_one_turn_pipeline import _atomic_write_json
from scripts.ug_parent_safe_advance import parse_args as advance_args, run as advance


def menu_adaptation_points(text, state=None):
    # This is only a navigation prefilter. The actual purchase uses the grid.
    if state is not None:
        observed = state.scenario_state.get("larc_growth_observation", {})
        points = observed.get("adaptation_points")
        if (observed.get("status") == "accepted" and observed.get("turn_index") == state.turn
                and type(points) is int and points >= 0):
            return points
    tail = str(text).split("海外适应性", 1)
    if len(tail) != 2:
        return None
    values = re.findall(r"(?m)^\s*([0-9][0-9,，]*)\s*$", tail[1])
    if len(values) != 1:
        return None
    return int(values[0].replace(",", "").replace("，", ""))


def completed_levels(run_root):
    levels = [0] * 10
    for path in (Path(run_root) / "session").glob("larc_adaptation_*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (value.get("run_session_id") != Path(run_root).name or value.get("inspect_only")
                or value.get("stage") != "complete" or value.get("returned") is not True):
            continue
        targets = (value.get("plan") or {}).get("targets")
        if (isinstance(targets, list) and len(targets) == 10
                and all(type(x) is int and 0 <= x <= 3 for x in targets)):
            levels = [max(a, b) for a, b in zip(levels, targets)]
    return levels


def growth_check_key(run_root, period, text, *, preferred_training=None, race_day=False, state=None, goal=None):
    if not isinstance(period, dict) or period.get("status") != "verified":
        return None
    value = period.get("value") or {}
    year, month, half = value.get("year_index"), value.get("month"), value.get("half")
    if (year not in (2, 3) or type(month) is not int or not 1 <= month <= 12
            or half not in ("first", "second") or year == 2 and month < 7):
        return None
    points = menu_adaptation_points(text, state)
    final = race_day and year == 3 and month == 10 and half == "first"
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    first_race_resource = (unified_larc_enabled(goal) and race_day and year == 2
                           and month in {9, 10} and half == "first")
    if points is None or points < (0 if first_race_resource else 150 if final else 200):
        return None
    levels = completed_levels(run_root)
    if final and levels[9] == 0:
        path = Path(run_root)/'session/larc_adaptation_final_arc.json'
        if path.is_file():
            try:
                final_session = LarcAdaptationSession.load(path, Path(run_root).name)
                receipt = final_session.receipt or {}
                known = receipt.get('levels') or []
                if (final_session.stage=='complete' and final_session.returned and len(known)==10
                        and all(type(value) is int for value in known) and min(known[:9])>=2
                        and known[9]==0 and receipt.get('pending_cost')==0):
                    return None  # The only optional final bonus is confirmed unavailable.
            except (OSError, ValueError, KeyError, TypeError):
                pass
    abroad = 7 <= month <= 9
    possible = (levels[9] < 2 if final else first_race_resource or not race_day and (
        (year == 2 or month < 9) and (levels[5] < 3 or levels[7] < 3)
        or year == 3 and month in (7, 8) and levels[6] < 3
        or abroad and any(level < 3 for level in levels[:5])))
    if not possible:
        return None
    timing = "first_race_resource" if first_race_resource else "final" if final else ("late_abroad" if month == 9 and half == "second"
        else "abroad" if abroad else "domestic")
    # Different layouts with the same point balance must not share a cached
    # no-purchase decision. This uses existing numbers, no extra capture/hash.
    layout = tuple((c.training_type, tuple(sorted(c.stats_gain.items())), c.failure_rate)
                   for c in state.training_options) if state is not None else ()
    key = (year, month, half, timing, points, tuple(levels), layout, preferred_training if abroad else None)
    from core.auto_trainer.larc_workflow_adaptation import cache_signature
    workflow = None if race_day else cache_signature(state, goal)
    return key if workflow is None else (*key, workflow)


def _unlock_window(value):
    """Refresh locked slots at scenario boundaries, not every overseas turn."""
    year, month, half = value.get("year_index"), value.get("month"), value.get("half")
    if month in (7, 8):
        window = "summer_expedition"
    elif month == 9:
        window = "overseas_race_" + str(half)
    else:
        window = "domestic_before" if isinstance(month, int) and month < 7 else "domestic_after"
    return year, window


def cached_growth_recommendation(ctx, period, text, **kwargs):
    """Reuse observed levels, not a stale balance or a previous training decision."""
    points = menu_adaptation_points(text, kwargs.get("state"))
    cached = getattr(ctx, "growth_grid", None)
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    if (unified_larc_enabled(kwargs.get('goal')) and cached
            and cached.get('grid', {}).get('pending_cost') != 0):
        # Re-inspect/resolve the original ledger; a cache is not authority to
        # turn an absent/unknown/pending payment into a zero-cost balance.
        return None
    completed = completed_levels(ctx.root)
    if cached:
        observed = cached.get('grid', {}).get('levels')
        if not isinstance(observed, list) or len(observed) != len(completed):
            return None
        levels = [max(a, b) if type(a) is int and 0 <= a <= 3
                  else b if a is None and b > 0 else None
                  for a, b in zip(observed, completed)]
        if any(type(level) is not int for level in levels):
            return None  # Refresh unknown slots; never turn them into zero.
    else:
        levels = completed
    if points is None or not levels or (0 in levels and (
            cached is None or _unlock_window(cached["period"]) != _unlock_window(period.get("value") or {}))):
        # Keep unrelated locked slots from forcing a visit every training turn.
        # Scenario/race boundaries can unlock them and still require a refresh.
        return None
    grid = {"levels": list(levels), "points": points, "pending_cost": 0, "errors": []}
    _record_forecast_context(ctx, grid)
    recommendation = recommend_growth_adaptation(grid, period,
        support_profile=getattr(ctx, "support_profile", None), **kwargs)
    result = {"ok": True, "grid": grid, "source": "known_levels_current_points",
              "recommendation": recommendation}
    if recommendation.get('procurement_trace'):
        result['procurement_trace'] = recommendation['procurement_trace']
    return result


def _record_forecast_context(ctx, grid):
    # Older read-only callers need not own a persistent run session.
    if getattr(ctx, "root", None) is None or not getattr(ctx, "run_id", None):
        return
    from core.auto_trainer.larc_expedition_forecast import record_expedition_context
    record_expedition_context(ctx.root / "session", ctx.run_id,
                             getattr(ctx, "progress", {}).get("next_turn"), grid)


def inspect_growth_adaptation(ctx, output, period, *, preferred_training=None, race_day=False, state=None, goal=None,
                              stay_on_grid=False):
    """Read one grid; optionally keep it open for purchase in this same visit."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    session_path = output / "inspection_session.json"
    LarcAdaptationSession(ctx.run_id, path=session_path, inspect_only=True,
                          inspect_grid_only=True, stay_on_grid=stay_on_grid).save()
    argv = ["--mode", "live", "--scenario", "larc", "--max-steps", "8",
        "--screenshot-dir", str(output / "capture"), "--adb-port", str(ctx.port),
        "--run-session-id", ctx.run_id, "--transition-session-json", str(ctx.route),
        "--skip-session-json", str(ctx.skip), "--career-goal-ledger-json", str(ctx.ledger),
        "--career-goal-observed-turn", str(ctx.progress["next_turn"]),
        "--larc-inspect-adaptation-session-json", str(session_path),
        "--execute", "--confirm-token", ADVANCE_CONFIRM_TOKEN]
    if native_instance_id(ctx.port) is not None:
        argv += ["--native-capture", "--settle-seconds", "0.15"]
    args = advance_args(argv)
    if getattr(ctx, "screen_reader", None) is not None:
        args._safety_analyzer = ctx.screen_reader
    child, code = advance(args)
    inspection_steps = list(child.get("step_reports", []))
    session = LarcAdaptationSession.load(session_path, ctx.run_id)
    last = (child.get("step_reports") or [{}])[-1].get("result", {})
    grid_ready = stay_on_grid and child.get("stop_reason") == "larc_adaptation_inspection_complete"
    if (not session.returned and not grid_ready and child.get("stop_reason") in {
            "route_state_out_of_scope", "larc_adaptation_inspection_complete", "larc_adaptation_unreadable"}
            and "海外适应性" in str(last.get("frame_safety", {}).get("visible_text", ""))):
        # Opening can expose the grid before its lower panel or return button.
        # This session is read-only: one bounded retry can only inspect/return.
        _atomic_write_json(output / "initial_report.json", child)
        sleep(.25)
        retry = list(argv)
        retry[retry.index("--screenshot-dir") + 1] = str(output / "capture_retry")
        child, code = advance(advance_args(retry))
        inspection_steps.extend(child.get("step_reports", []))
        grid_ready = stay_on_grid and child.get("stop_reason") == "larc_adaptation_inspection_complete"
    _atomic_write_json(output / "report.json", child)
    session = LarcAdaptationSession.load(session_path, ctx.run_id)
    observations = [row.get("result", {}).get("decision", {}).get("evidence", {}).get("larc_adaptation", {})
                    for row in inspection_steps]
    grid = next((x for x in reversed(observations) if isinstance(x.get("levels"), list)), None)
    if code or not (session.returned or grid_ready) or grid is None:
        return {"ok": False, "reason": child.get("stop_reason", "growth_grid_unavailable")}
    _record_forecast_context(ctx, grid)
    recommendation = recommend_growth_adaptation(grid, period,
        preferred_training=preferred_training, race_day=race_day, state=state, goal=goal,
        support_profile=getattr(ctx, "support_profile", None))
    result = {"ok": True, "recommendation": recommendation,
              "grid": {k: grid.get(k) for k in ("levels", "points", "pending_cost", "errors")}}
    if recommendation.get('procurement_trace'):
        result['procurement_trace'] = recommendation['procurement_trace']
    if stay_on_grid:
        result["inspection_session_path"] = str(session_path)
    _atomic_write_json(output / "recommendation.json", result)
    return result


def return_from_growth_inspection(ctx, session_path, output):
    """Close a no-purchase visit, without starting a new inspection."""
    session = LarcAdaptationSession.load(Path(session_path), ctx.run_id)
    session.stay_on_grid = False
    session.save()
    argv = ["--mode", "live", "--scenario", "larc", "--max-steps", "3",
        "--screenshot-dir", str(output), "--adb-port", str(ctx.port),
        "--run-session-id", ctx.run_id, "--transition-session-json", str(ctx.route),
        "--skip-session-json", str(ctx.skip),
        "--larc-inspect-adaptation-session-json", str(session_path),
        "--execute", "--confirm-token", ADVANCE_CONFIRM_TOKEN]
    if native_instance_id(ctx.port) is not None:
        argv += ["--native-capture", "--settle-seconds", "0.15"]
    child, code = advance(advance_args(argv))
    last = (child.get("step_reports") or [{}])[-1].get("result", {}).get("decision", {})
    if (native_instance_id(ctx.port) is not None and child.get("stop_reason") == "route_state_out_of_scope"
            and last.get("reason") in {"common_career_page_unrecognized", "larc_adaptation_return"}
            and LarcAdaptationSession.load(Path(session_path), ctx.run_id).returned):
        # The return tap already happened. Wait for home in memory, then hand
        # that frame to the reader; never send the return tap a second time.
        from core.native_capture_adb import adb_for_capture
        from core.execution_frames import wait_for_frame
        from core.auto_trainer.larc_control_wait import outing_controls
        adb = adb_for_capture(ctx.port)
        try:
            if adb.connect():
                ready = wait_for_frame(adb, lambda im: outing_controls(im, home=True), timeout=3.0)
                if ready.value:
                    retry = list(argv)
                    retry.remove("--execute")
                    retry[retry.index("--screenshot-dir") + 1] = str(Path(output) / "settled")
                    args = advance_args(retry)
                    args._initial_frame = ready.frame
                    child, code = advance(args)
        finally:
            adb.close()
    _atomic_write_json(Path(output) / "report.json", child)
    return {"ok": code == 0 and child.get("stop_reason") in {"ready_main_menu", "race_day_main_menu"},
            "reason": child.get("stop_reason"), "menu_source": child.get("source_screenshot_path")}


def restore_training_menu(ctx, source, output):
    """One navigation tap and a page check; never scan gains or five tabs."""
    from core.native_capture_adb import adb_for_capture
    from core.coordinates import CoordinateConfig
    from core.auto_trainer.route_transition_session import RouteTransitionSession
    from core.training_menu_precheck import TrainingMenuPrechecker
    from scripts.capture_ug_parent_live_tabs import (
        _transition_preflight, _transition_persist_pending, _transition_postcheck)

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if not source or not Path(source).is_file():
        return {"ok": False, "reason": "growth_return_home_source_missing"}
    route = RouteTransitionSession.load(ctx.route, expected_run_session_id=ctx.run_id)
    adb = adb_for_capture(ctx.port)
    if not adb.connect():
        return {"ok": False, "reason": "adb_connect_failed"}
    events = []
    action = "main_menu.training"
    sha = _transition_preflight(route, source_path=Path(source), observed_state="main_menu",
        action_id=action, coordinate_name=action, events=events)
    point = CoordinateConfig().point("main_menu", "training")
    if adb.tap_no_wait(*point, scaled=True) is not True:
        return {"ok": False, "reason": "growth_training_entry_tap_failed"}
    _transition_persist_pending(route, session_path=ctx.route, action_id=action,
                                coordinate_name=action, events=events)
    path = output / "training_menu.png"
    checker = TrainingMenuPrechecker(scenario="larc")
    deadline = monotonic() + 4.0
    while monotonic() < deadline:
        image = adb.screenshot(str(path))
        if image is not None and checker.check(path).accepted:
            _transition_postcheck(route, session_path=ctx.route, source_path=path,
                action_id=action, coordinate_name=action, source_sha=sha, events=events)
            _atomic_write_json(output / "report.json", {"ok": True, "events": events})
            return {"ok": True, "reason": "training_menu_restored"}
        sleep(.05)
    return {"ok": False, "reason": "growth_training_entry_not_observed"}
