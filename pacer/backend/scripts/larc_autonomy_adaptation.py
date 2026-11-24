"""One bounded call to an explicitly selected existing L'Arc aptitude phase."""
import hashlib
import json
from pathlib import Path
from core.native_capture_adb import native_instance_id

from core.auto_trainer.route_handoff import is_route_handoff
from core.auto_trainer.career_goal_ledger import CareerGoalLedgerStore
from core.auto_trainer.config import load_goal_config
from core.auto_trainer.larc_adaptation import (
    LarcAdaptationSession, PHASE_SLOT_COUNTS, LANGUAGE_DEFERRED_PHASES, POWER_DEFERRED_PHASES,
    adaptation_target_levels_ready, adaptation_relevant_errors,
)
from core.auto_trainer.pacer_storage import _io
from core.auto_trainer.route_transition_session import RouteTransitionSession
from core.auto_trainer.safe_advance import ADVANCE_CONFIRM_TOKEN
from core.auto_trainer.skip_session import RunSkipSession
from scripts.ug_parent_one_turn_pipeline import _atomic_write_json
from scripts.ug_parent_safe_advance import parse_args as advance_args, run as advance


def select_larc_adaptation_phase(period, *, race_target_id=None, race_day=False, goal=None):
    """Dispatch fixed essentials; V3 prices optional pre-expedition upgrades."""
    if not isinstance(period, dict) or period.get("status") != "verified":
        raise ValueError("larc_adaptation_period_unverified")
    value = period.get("value", {})
    if not isinstance(value, dict):
        raise ValueError("larc_adaptation_period_invalid")
    if value.get("pre_debut") is True:
        return None
    year, stage = value.get("year_index"), value.get("stage")
    month, half = value.get("month"), value.get("half")
    if (type(year) is not int or year not in (1, 2, 3)
            or stage != {1: "junior", 2: "classic", 3: "senior"}[year]
            or type(month) is not int or not 1 <= month <= 12 or half not in {"first", "second"}):
        raise ValueError("larc_adaptation_period_invalid")
    if race_day:
        return {
            ("classic", "larc_niel_participation"): "first_overseas_race",
            ("classic", "larc_arc_participation"): "first_arc",
            ("senior", "larc_foy_participation"): "foy_prelude",
            ("senior", "larc_final_arc_top5"): "final_arc",
        }.get((stage, race_target_id))
    if (year, month, half) == (2, 6, "second") and goal is not None:
        from core.auto_trainer.larc_policy import overseas_purchase_v3_enabled
        from core.auto_trainer.larc_strategy_params import unified_larc_enabled
        if overseas_purchase_v3_enabled(goal) and unified_larc_enabled(goal):
            # Let the current-grid purchase planner compare spending on power
            # with Pt/friendship goals. Race-day essentials still run above.
            return None
    return {
        (1, 12, "first"): "first_exchange",
        (1, 12, "second"): "first_exchange",
        (2, 6, "second"): "first_expedition",
    }.get((year, month, half))


def _home_observed_after_freeze(session, child, return_index):
    rows = child.get("step_reports", [])
    home_reason = child.get("stop_reason")
    if (child.get("is_valid") is not True or home_reason not in {"ready_main_menu", "race_day_main_menu"}
            or len(rows) <= return_index + 1
            or any(row.get("result", {}).get("tap_performed") is True for row in rows[return_index + 1:])):
        return False
    row = rows[-1]
    result = row.get("result", {})
    decision, route = result.get("decision", {}), result.get("route_transition", {})
    if (result.get("reason") != home_reason or not is_route_handoff(decision, reason=home_reason)
            or decision.get("route_state") != "main_menu"
            or route.get("run_session_id") != session.run_session_id or route.get("status") != "tracking"
            or route.get("current_state") != "main_menu" or route.get("expected_next_states") != []
            or route.get("last_action_id") not in {"safe_advance.larc_adaptation_return",
                                                  "wait:larc_adaptation_return_loading"}
            or route.get("mismatch_state") is not None):
        return False
    frame = Path(row.get("screenshot_path", ""))
    try:
        # The CLI exports a top-level source only for the ordinary training home.
        source = child.get("source_screenshot_path")
        if (not _io(frame).is_file() or (source is None and home_reason == "ready_main_menu")
                or (source is not None and Path(source).resolve() != frame.resolve())):
            return False
        sha = hashlib.sha256(_io(frame).read_bytes()).hexdigest()
    except OSError:
        return False
    return (result.get("frame_safety", {}).get("source_image_sha256") == sha
            and route.get("observation_sha256") == sha
            and sha != rows[return_index].get("result", {}).get("frame_safety", {}).get("source_image_sha256"))


def _verified_ready_reason(session, child, *, allow_partial=False):
    plan = session.plan
    if not session.returned or plan is None or (plan["deferred_slots"] and not allow_partial):
        return None
    for index, row in enumerate(child.get("step_reports", [])):
        result = row.get("result", {})
        decision = result.get("decision", {})
        detail = decision.get("evidence", {}).get("larc_adaptation", {})
        balance_diagnostic_only = (session.receipt or {}).get("completion_basis") == "current_target_levels_pending_zero"
        actionable_errors = adaptation_relevant_errors(detail.get("errors", []), plan)
        returned_after_freeze = (result.get("performed") is False
            and result.get("tap_performed") is True
            and result.get("reason") in {"post_wait_freeze_insufficient_budget", "post_wait_freeze_timeout"}
            and result.get("post_wait_freeze", {}).get("status") in {"insufficient_budget", "timeout"}
            and _home_observed_after_freeze(session, child, index))
        if ((result.get("performed") is not True and not returned_after_freeze) or result.get("tap_performed") is not True
                or decision.get("reason") != "larc_adaptation_return"
                or decision.get("tap", {}).get("coordinate_name") != "safe_advance.larc_adaptation_return"
                or detail.get("plan") != plan or not adaptation_target_levels_ready(detail.get("levels"),plan)
                or detail.get("pending_cost") != 0 or actionable_errors):
            continue
        frame = Path(row.get("screenshot_path", ""))
        if not _io(frame).is_file():
            continue
        sha = hashlib.sha256(_io(frame).read_bytes()).hexdigest()
        if result.get("frame_safety", {}).get("source_image_sha256") != sha:
            continue
        from core.auto_trainer.larc_adaptation import phase_required_levels
        required = phase_required_levels(session.phase)
        if (plan["cost"] == 0 and detail.get("completion_reason") == "larc_adaptation_already_ready"
                and all(detail["levels"][slot] >= target for slot, target in required.items())):
            return "larc_adaptation_already_ready"
        receipt = session.receipt
        if (session.stage == "complete" and plan["cost"] > 0 and isinstance(receipt, dict)
                and detail.get("completion_reason") == "larc_adaptation_verified"
                and detail.get("receipt") == receipt
                and receipt.get("run_session_id") == session.run_session_id
                and receipt.get("phase") == session.phase
                and (receipt.get("phase_targets_ready") is True or (
                    allow_partial and plan["deferred_slots"] and receipt.get("phase_targets_ready") is False))
                and receipt.get("levels") == detail.get("levels") and receipt.get("pending_cost") == 0
                and receipt.get("spent_points") == plan["cost"]
                and receipt.get("before_points") == plan["baseline_points"]
                and (balance_diagnostic_only or receipt.get("after_points") == plan["baseline_points"] - plan["cost"])
                and detail.get("points") == receipt["after_points"]
                and receipt.get("submitted_source_sha256") == session.submitted_source_sha256
                and receipt.get("result_source_sha256") == sha):
            return "larc_adaptation_verified"
    return None


def _revalidate_returned_session(run_root, session, current_report):
    """Re-read only this phase's original failed completion report; never execute."""
    for path in sorted(run_root.glob(f"autonomous_*/**/adaptation_{session.phase}/report.json")):
        try:
            if not path.resolve().is_relative_to(run_root):
                continue
            previous = json.loads(_io(path).read_text(encoding="utf-8"))
            if (previous.get("stop_reason") not in {"larc_adaptation_completion_unverified", "larc_adaptation_phase_incomplete"}
                    or previous.get("is_valid") is not False or previous.get("phase_complete") is not False
                    or previous.get("status") != "stopped" or previous.get("session") != session.to_dict()
                    or any(previous.get(key) != current_report[key] for key in
                           ("run_session_id", "device", "turn_index", "phase", "phase_session_path"))
                    or previous.get("safe_advance_exit_code") != 0):
                continue
            child_path = path.parent / "safe_advance_report.json"
            if (Path(previous.get("safe_advance_report_path", "")).resolve() != child_path.resolve()
                    or not child_path.resolve().is_relative_to(path.parent.resolve())):
                continue
            child = json.loads(_io(child_path).read_text(encoding="utf-8"))
            if (child.get("is_valid") is not True or any(
                    not Path(row.get("screenshot_path", "")).resolve().is_relative_to(path.parent.resolve())
                    for row in child.get("step_reports", []))):
                continue
            if previous["stop_reason"] == "larc_adaptation_phase_incomplete":
                if session.plan["deferred_slots"] and _verified_ready_reason(session, child, allow_partial=True):
                    return {"is_valid": False, "phase_complete": False, "status": "stopped",
                            "stop_reason": "larc_adaptation_phase_incomplete",
                            "safe_advance_report_path": str(child_path), "safe_advance_exit_code": 0,
                            "revalidated_from_report": str(path)}
                continue
            if not any(row.get("result", {}).get("performed") is False
                       and row.get("result", {}).get("reason") in {"post_wait_freeze_insufficient_budget", "post_wait_freeze_timeout"}
                       and row.get("result", {}).get("decision", {}).get("reason") == "larc_adaptation_return"
                       for row in child.get("step_reports", [])):
                continue
            reason = _verified_ready_reason(session, child)
            if reason:
                return {"is_valid": True, "phase_complete": True, "status": "ready", "stop_reason": reason,
                        "safe_advance_report_path": str(child_path), "safe_advance_exit_code": 0,
                        "revalidated_from_report": str(path)}
        except (OSError, ValueError, TypeError):
            continue
    return None


def run_larc_adaptation(*, run_root: Path, output_dir: Path, adb_port: int,
                        phase: str, max_steps: int = 48, safety_analyzer=None):
    run_root, output_dir = Path(run_root).resolve(), Path(output_dir).resolve()
    if (phase not in PHASE_SLOT_COUNTS or type(max_steps) is not int or not 1 <= max_steps <= 48
            or not output_dir.is_relative_to(run_root) or output_dir == run_root):
        raise ValueError("larc_adaptation_phase_output_or_budget_invalid")
    progress = json.loads(_io(run_root / "progress.json").read_text(encoding="utf-8"))
    run_id = progress.get("run_session_id")
    if (run_id != run_root.name or type(adb_port) is not int or not 1 <= adb_port <= 65535
            or progress.get("device") != f"127.0.0.1:{adb_port}"
            or type(progress.get("next_turn")) is not int or progress["next_turn"] < 1):
        raise ValueError("larc_adaptation_run_or_device_mismatch")
    goal = load_goal_config(run_root / "goal.json")
    if goal.scenario != "larc":
        raise ValueError("larc_adaptation_scenario_mismatch")
    if phase in LANGUAGE_DEFERRED_PHASES or phase in POWER_DEFERRED_PHASES:
        from core.auto_trainer.larc_language_deferral import selected_language_deferred_phase
        if selected_language_deferred_phase(run_root, (LANGUAGE_DEFERRED_PHASES | POWER_DEFERRED_PHASES)[phase],
                progress["next_turn"], goal) != phase:
            raise ValueError("larc_language_deferral_not_selected")
    route_path = run_root / "session/transition_session.json"
    skip_path = run_root / "session/skip_session.json"
    ledger_path = run_root / "session/career_goal_ledger.json"
    RouteTransitionSession.load(route_path, expected_run_session_id=run_id)
    RunSkipSession.load(skip_path, expected_run_session_id=run_id)
    CareerGoalLedgerStore().load(ledger_path, expected_run_session_id=run_id)
    phase_path = run_root / "session" / f"larc_adaptation_{phase}.json"
    session = LarcAdaptationSession.load(phase_path, run_id) if _io(phase_path).exists() else None
    if session is not None and (session.phase != phase or session.inspect_only):
        raise ValueError("larc_adaptation_mode_mismatch")
    _io(output_dir).mkdir(parents=True, exist_ok=False)
    report = {"is_valid": False, "status": "stopped", "phase_complete": False,
              "run_session_id": run_id, "device": progress["device"],
              "turn_index": progress["next_turn"], "phase": phase,
              "phase_session_path": str(phase_path), "requested_steps": max_steps}

    def finish(reason, code=2):
        report["stop_reason"] = reason
        if session is not None:
            report["session"] = session.to_dict()
            report["deferred_slots"] = session.plan["deferred_slots"] if session.plan else []
        _atomic_write_json(_io(output_dir / "report.json"), report)
        return report, code

    # Observe a pending submission before deciding its outcome. A real result
    # notice may already be visible after an interrupted tap; the planner may
    # close it and read the grid, while an unresolved purchase still cannot tap.
    if session is not None and session.returned:
        revalidated = _revalidate_returned_session(run_root, session, report)
        if revalidated is not None:
            report.update(revalidated)
            return finish(revalidated["stop_reason"], 0 if revalidated["phase_complete"] else 2)
        return finish("larc_adaptation_returned_requires_prior_report")
    from core.auto_trainer.larc_adaptation import BUNDLE_PHASES
    saved_phase = phase in BUNDLE_PHASES or phase in LANGUAGE_DEFERRED_PHASES or phase in POWER_DEFERRED_PHASES
    if saved_phase and session is None:
        session = LarcAdaptationSession(run_id, path=phase_path, phase=phase)
        session.save()
    phase_option = "bundle" if saved_phase else phase.replace('_', '-')
    args = advance_args([
        "--mode", "live", "--scenario", "larc", "--max-steps", str(max_steps),
        "--screenshot-dir", str(output_dir / "advance"), "--adb-port", str(adb_port),
        *(["--native-capture", "--settle-seconds", "0.2"] if native_instance_id(adb_port) is not None else []),
        "--run-session-id", run_id, "--transition-session-json", str(route_path),
        "--skip-session-json", str(skip_path), "--career-goal-ledger-json", str(ledger_path),
        "--career-goal-observed-turn", str(progress["next_turn"]),
        f"--larc-{phase_option}-session-json", str(phase_path),
        "--execute", "--confirm-token", ADVANCE_CONFIRM_TOKEN,
    ])
    if safety_analyzer is not None:
        args._safety_analyzer = safety_analyzer
    try:
        child, code = advance(args)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}:{exc}"
        return finish("larc_adaptation_executor_error")
    child_path = output_dir / "safe_advance_report.json"
    _atomic_write_json(child_path, child)
    report.update(safe_advance_report_path=str(child_path), safe_advance_exit_code=code)
    if not _io(phase_path).exists():
        child_reason = child.get("stop_reason")
        if (
            isinstance(child_reason, str)
            and child_reason.strip()
            and not child.get("performed_steps")
        ):
            return finish(child_reason.strip())
        return finish("larc_adaptation_session_missing")
    session = LarcAdaptationSession.load(phase_path, run_id)
    if session.phase != phase or session.inspect_only:
        return finish("larc_adaptation_mode_mismatch")
    if session.stage == "submission_pending":
        return finish("larc_adaptation_submission_outcome_unknown")
    if code != 0 or child.get("is_valid") is not True:
        return finish(child.get("stop_reason", "larc_adaptation_child_unverified"))
    ready_reason = _verified_ready_reason(session, child)
    if ready_reason:
        report.update(is_valid=True, phase_complete=True, status="ready")
        return finish(ready_reason, 0)
    if session.returned and session.plan and session.plan["deferred_slots"]:
        return finish("larc_adaptation_phase_incomplete")
    return finish("larc_adaptation_completion_unverified" if session.returned
                  else child.get("stop_reason", "larc_adaptation_not_returned"))
