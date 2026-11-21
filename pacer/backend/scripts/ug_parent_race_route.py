from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass, field, replace
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.auto_trainer.race_route import (
    RaceRouteExecutor,
    RaceRoutePolicy,
    RaceRoutePlanner,
    RaceRouteSession,
    is_race_scene_wait_result,
)
from core.auto_trainer.career_goal_ledger import CareerGoalLedgerStore
from core.auto_trainer.main_menu_state_provider import provide_main_menu_state
from core.auto_trainer.race_calendar import RaceCalendar
from core.auto_trainer.route_transition_session import RouteTransitionSession, RouteTransitionStatus
from core.auto_trainer.route_handoff import is_route_handoff_result


MAX_CONSECUTIVE_IDENTICAL_TAPS = 8
MAX_REPEATED_ACTION_CYCLES = 3
RACE_LIST_EXIT_REASONS = frozenset(
    {
        "leave_selected_non_g1_race_list",
        "leave_unverified_g1_race_list",
        "leave_non_goal_race_list",
        "leave_fan_goal_recommendation_list",
    }
)


@dataclass
class CountRaceInvocation:
    """One in-memory race, retained while Safe owns its intervening dialogue."""

    run_session_id: str
    observed_runner_turn: int
    session: RaceRouteSession
    card_reader: Callable[[Path], dict[str, Any]]
    race_route_session_id: str = field(default_factory=lambda: uuid4().hex)
    steps: list[dict[str, Any]] = field(default_factory=list, init=False)
    result_persisted: bool = field(default=False, init=False)
    race_scene_waits: int = field(default=0, init=False)
    repeated_tap_count: int = field(default=0, init=False)
    previous_tap_signature: tuple | None = field(default=None, init=False)
    recent_tap_signatures: list[tuple] = field(default_factory=list, init=False)
    pre_tap: Callable[[dict[str, Any]], None] | None = field(default=None, init=False)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely route Uma race pages until training can resume."
    )
    parser.add_argument("--mode", choices=["dry-run", "live"], default="dry-run")
    parser.add_argument("--max-steps", type=int, default=1)
    parser.add_argument("--screenshot-dir", required=True)
    parser.add_argument("--input-image")
    parser.add_argument("--adb-port", type=int)
    parser.add_argument("--scenario", choices=["grand_masters", "larc"], default="grand_masters")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-token")
    parser.add_argument("--wbc-only", action="store_true")
    parser.add_argument("--inspection-only", action="store_true")
    parser.add_argument("--inspection-ledger-json")
    parser.add_argument("--inspection-calendar-json")
    parser.add_argument("--inspection-runner-turn", type=int)
    parser.add_argument("--inspection-resume-report")
    parser.add_argument("--inspection-resume-report-sha256")
    parser.add_argument("--transition-session-json")
    parser.add_argument("--transition-run-session-id")
    parser.add_argument("--allow-enter-race", action="store_true")
    parser.add_argument("--allow-enter-race-from-main-menu", action="store_true")
    parser.add_argument("--allow-fan-goal-recommended-race", action="store_true")
    parser.add_argument("--allow-select-visible-g1", action="store_true")
    parser.add_argument("--allow-scan-race-list-for-g1", action="store_true")
    parser.add_argument("--allow-leave-race-list", action="store_true")
    parser.add_argument(
        "--allow-leave-unverified-g1-race-list",
        action="store_true",
    )
    parser.add_argument("--allow-leave-race-detail", action="store_true")
    parser.add_argument("--allow-confirm-race", action="store_true")
    parser.add_argument("--allow-quick-result", action="store_true")
    parser.add_argument("--allow-skip-race-scene", action="store_true")
    parser.add_argument("--allow-result-continue", action="store_true")
    parser.add_argument("--allow-locked-race-entry", action="store_true")
    parser.add_argument(
        "--planned-race-id",
        action="append",
        default=[],
        help="Plan-bound race ID. Repeat for the current schedule allowlist.",
    )
    parser.add_argument("--schedule-packet-sha256")
    parser.add_argument(
        "--allow-grand-masters-final-race-confirm",
        action="store_true",
    )
    parser.add_argument(
        "--allow-cancel-unverified-grand-masters-final",
        action="store_true",
    )
    parser.add_argument(
        "--visible-text",
        help="Optional OCR/manual visible text used as a secondary screen-state hint.",
    )
    parser.add_argument("--wait-seconds", type=float, default=1.0)
    parser.add_argument("--settle-seconds", type=float, default=1.2)
    parser.add_argument(
        "--race-scene-wait-limit",
        type=int,
        default=25,
        help=(
            "Stop with a diagnostic reason after this many consecutive race-scene waits "
            "(race_scene_running or landscape_race_skip_enabled); 0 disables."
        ),
    )
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    indent = 2 if args.pretty else None
    try:
        report, code = run(args)
    except Exception as exc:
        return _print(
            {"is_valid": False, "status": "error", "error": f"{type(exc).__name__}: {exc}"},
            indent=indent,
            code=2,
        )
    return _print(report, indent=indent, code=code)


def run(args: argparse.Namespace, *, count_invocation: CountRaceInvocation | None = None) -> tuple[dict[str, Any], int]:
    if args.max_steps <= 0:
        raise ValueError("max_steps_must_be_positive")
    if args.input_image and args.mode != "dry-run":
        raise ValueError("input_image_requires_dry_run")
    if args.input_image and args.max_steps != 1:
        raise ValueError("input_image_requires_max_steps_1")
    inspection = bool(args.inspection_only)
    if count_invocation is not None and (
        inspection or args.wbc_only or args.input_image or args.visible_text is not None
        or tuple(args.planned_race_id) != count_invocation.session.planned_race_ids
        or args.schedule_packet_sha256 != count_invocation.session.schedule_packet_sha256
    ):
        raise ValueError("count_race:invocation_binding_invalid")
    resume_report = None
    resume_pending = bool(args.inspection_resume_report or args.inspection_resume_report_sha256)
    if resume_pending and (not inspection or not args.inspection_resume_report or not args.inspection_resume_report_sha256):
        raise ValueError("race_inspection:resume_context_required")
    if inspection:
        if (args.wbc_only or args.planned_race_id or args.schedule_packet_sha256 is not None
            or any(value for name, value in vars(args).items() if name.startswith("allow_"))
            or args.input_image or args.visible_text is not None or args.max_steps > 6):
            raise ValueError("race_inspection:unrelated_capability_or_input")
        if (not args.inspection_ledger_json or not args.inspection_calendar_json
            or not args.transition_session_json or not args.transition_run_session_id
            or type(args.inspection_runner_turn) is not int or args.inspection_runner_turn < 1):
            raise ValueError("race_inspection:context_required")
        inspection_store = CareerGoalLedgerStore()
        inspection_ledger_path = Path(args.inspection_ledger_json)
        inspection_ledger_bytes = inspection_ledger_path.read_bytes()
        inspection_ledger = inspection_store.load(inspection_ledger_path,
            expected_run_session_id=args.transition_run_session_id)
        active = inspection_ledger.active_goals()
        if (len(active) != 1 or active[0].goal_kind != "graded_race_rank_count"
            or active[0].progress_basis != "verified_lower_bound" or active[0].deadline_turn is None):
            raise ValueError("race_inspection:bound_count_required")
        inspection_calendar = RaceCalendar.load(args.inspection_calendar_json)
        initial = RouteTransitionSession.load(args.transition_session_json,
            expected_run_session_id=args.transition_run_session_id)
        if resume_pending:
            original_bytes = Path(args.inspection_resume_report).read_bytes()
            if hashlib.sha256(original_bytes).hexdigest() != args.inspection_resume_report_sha256:
                raise ValueError("race_inspection:resume_report_sha_mismatch")
            original = json.loads(original_bytes)
            resume_report = original.get("inspection", {})
            rows = resume_report.get("step_reports", [])
            if (original.get("run_session_id") != args.transition_run_session_id
                or resume_report.get("inspection_only") is not True
                or resume_report.get("goal_instance_id") != active[0].goal_instance_id
                or type(resume_report.get("ledger_revision")) is not int
                or resume_report["ledger_revision"] != inspection_ledger.revision
                or type(resume_report.get("calendar_turn")) is not int
                or resume_report["calendar_turn"] < 1
                or resume_report["calendar_turn"] > active[0].deadline_turn
                or type(resume_report.get("tap_count")) is not int or resume_report["tap_count"] != 1
                or type(resume_report.get("performed_steps")) is not int or resume_report["performed_steps"] != 1
                or len(rows) != 2 or rows[1].get("result", {}).get("performed") is not False
                or rows[1]["result"].get("tap_performed") is True
                or resume_report.get("stop_reason") != "race_inspection:action_out_of_scope"):
                raise ValueError("race_inspection:resume_report_invalid")
            opened = rows[0]["result"]
            decision, packet, verdict = opened["decision"], opened["frame_safety"], opened["safety_verdict"]
            receipt = opened.get("route_action_receipt_source", {})
            source_sha = hashlib.sha256(Path(receipt["source_path"]).read_bytes()).hexdigest()
            if (opened.get("performed") is not True or opened.get("tap_performed") is not True
                or (decision.get("action"), decision.get("reason"), decision.get("route_state")) !=
                    ("tap", "enter_race_from_main_menu", "ready_main_menu")
                or decision.get("tap") != {"coordinate_name": "main_menu.race", "scaled": False, "x": 604, "y": 1109}
                or receipt.get("action_id") != "main_menu.race" or receipt.get("coordinate_name") != "main_menu.race"
                or receipt.get("action_state") != "main_menu" or receipt.get("source_sha256") != source_sha
                or hashlib.sha256(Path(decision["evidence"]["image"]).read_bytes()).hexdigest() != source_sha
                or packet.get("source_image_sha256") != source_sha
                or verdict.get("allowed") is not True or verdict.get("action_id") != "main_menu.race"
                or verdict.get("source_image_sha256") != source_sha
                or initial.to_dict() != opened.get("route_transition")
                or initial.status != RouteTransitionStatus.TRACKING or initial.current_state != "main_menu"
                or initial.last_action_id != "main_menu.race" or initial.mismatch_state is not None
                or initial.expected_next_states != ("loading", "race_list")
                or initial.observation_sha256 != source_sha or initial.action_source_sha256 != source_sha):
                raise ValueError("race_inspection:resume_open_receipt_invalid")
        elif (initial.status != RouteTransitionStatus.TRACKING or initial.current_state != "main_menu"
            or initial.expected_next_states or initial.action_source_sha256 is not None or initial.mismatch_state is not None):
            raise ValueError("race_inspection:idle_main_session_required")
    if args.wbc_only and (
        args.planned_race_id or args.schedule_packet_sha256 is not None
        or any(getattr(args, flag) for flag in (
            "allow_enter_race_from_main_menu", "allow_fan_goal_recommended_race",
            "allow_select_visible_g1", "allow_scan_race_list_for_g1", "allow_leave_race_list",
            "allow_leave_unverified_g1_race_list", "allow_leave_race_detail",
            "allow_grand_masters_final_race_confirm", "allow_cancel_unverified_grand_masters_final",
        ))
    ):
        raise ValueError("wbc_only:unrelated_capability_or_binding")

    base_policy = RaceRoutePolicy(
        scenario=getattr(args, "scenario", "grand_masters"),
        count_reserved_only=count_invocation is not None,
        count_result_persisted=count_invocation.result_persisted if count_invocation else False,
        inspection_only=inspection,
        wbc_only=bool(args.wbc_only),
        allow_enter_race=bool(args.allow_enter_race),
        allow_enter_race_from_main_menu=bool(args.allow_enter_race_from_main_menu),
        allow_fan_goal_recommended_race=bool(
            args.allow_fan_goal_recommended_race
        ),
        allow_select_visible_g1=bool(args.allow_select_visible_g1),
        allow_scan_race_list_for_g1=bool(
            args.allow_scan_race_list_for_g1
        ),
        allow_leave_race_list=bool(args.allow_leave_race_list),
        allow_leave_unverified_g1_race_list=bool(
            args.allow_leave_unverified_g1_race_list
        ),
        allow_leave_race_detail=bool(args.allow_leave_race_detail),
        allow_confirm_race=bool(args.allow_confirm_race),
        allow_quick_result=bool(args.allow_quick_result),
        allow_skip_race_scene=bool(args.allow_skip_race_scene),
        allow_result_continue=bool(args.allow_result_continue),
        allow_locked_race_entry=bool(args.allow_locked_race_entry),
        allow_grand_masters_final_race_confirm=bool(
            args.allow_grand_masters_final_race_confirm
        ),
        allow_cancel_unverified_grand_masters_final=bool(
            args.allow_cancel_unverified_grand_masters_final
        ),
        visible_text=args.visible_text,
    )
    screenshot_dir = Path(args.screenshot_dir)
    executor = RaceRouteExecutor(adb_factory=_adb_factory(args.adb_port),
        **({"planner": RaceRoutePlanner(count_card_reader=count_invocation.card_reader)} if count_invocation else {}))
    step_reports: list[dict[str, Any]] = count_invocation.steps if count_invocation else []
    performed = sum(row["result"].get("performed") is True for row in step_reports)
    stop_reason = "max_steps_reached"
    race_scene_waits = count_invocation.race_scene_waits if count_invocation else 0
    repeated_tap_count = count_invocation.repeated_tap_count if count_invocation else 0
    previous_tap_signature = count_invocation.previous_tap_signature if count_invocation else None
    recent_tap_signatures = count_invocation.recent_tap_signatures if count_invocation else []
    session = count_invocation.session if count_invocation else RaceRouteSession(
        planned_race_ids=tuple(args.planned_race_id),
        schedule_packet_sha256=args.schedule_packet_sha256,
    )
    if resume_pending:
        session.inspection_phase = "opened"
        session.inspection_source_sha256 = initial.action_source_sha256
    inspection_error = None
    inspection_main_turn = resume_report["calendar_turn"] if resume_report is not None else None

    def validate_inspection_main(source_path: str, source_sha: str) -> None:
        nonlocal inspection_main_turn
        observed = provide_main_menu_state(source_path,
            screenshot_dir / f"{Path(source_path).stem}.provider.json",
            turn_index=args.inspection_runner_turn, race_calendar=inspection_calendar)
        anchor = (observed.get("state_context") or {}).get("scenario_state", {}).get("career_calendar_anchor")
        if observed.get("valid_for_training") is not True:
            raise ValueError("race_inspection:main_unverified")
        binding = inspection_store.bind_count_goal_deadline(inspection_ledger, anchor=anchor,
            observed_turn=args.inspection_runner_turn, source_frame_sha256=source_sha,
            expected_run_session_id=args.transition_run_session_id)
        if binding.status != "no_change":
            raise ValueError("race_inspection:bound_anchor_required")
        current_turn = anchor["career_time"]["global_turn"]
        if inspection_main_turn is not None and inspection_main_turn != current_turn:
            raise ValueError("race_inspection:calendar_turn_changed")
        inspection_main_turn = current_turn

    def inspection_preflight(source: dict[str, Any]) -> None:
        nonlocal staged_inspection, staged_amendment
        if inspection_ledger_path.read_bytes() != inspection_ledger_bytes:
            raise ValueError("race_inspection:ledger_changed")
        action = source.get("action_id")
        recommendation = action == "safe_advance.race_recommendation_top_confirm" and phase_before == "opened"
        state = "main_menu" if phase_before == "new" else "modal" if recommendation else "race_list"
        expected_action = "main_menu.race" if phase_before == "new" else "safe_advance.race_recommendation_top_confirm" if recommendation else "race_list.back"
        if resume_pending and not recommendation:
            raise ValueError("race_inspection:resume_recommendation_required")
        if (action != expected_action or source.get("coordinate_name") != action or source.get("action_state") != state
            or hashlib.sha256(Path(source["source_path"]).read_bytes()).hexdigest() != source.get("source_sha256")):
            raise ValueError("race_inspection:receipt_source_invalid")
        if phase_before == "new":
            validate_inspection_main(source["source_path"], source["source_sha256"])
        latest = RouteTransitionSession.load(args.transition_session_json,
            expected_run_session_id=args.transition_run_session_id)
        if latest.to_dict() != before_step.to_dict():
            raise ValueError("race_inspection:session_changed")
        staged_inspection = copy.deepcopy(latest)
        if resume_pending:
            if latest.to_dict() != initial.to_dict():
                raise ValueError("race_inspection:resume_session_changed")
            staged_inspection.commit_action("main_menu.race", source_sha256=latest.action_source_sha256,
                expected_next_states=("loading", "modal", "race_list"))
            staged_amendment = {"kind": "contract_amendment", "new_tap": False,
                "action_id": "main_menu.race", "source_sha256": latest.action_source_sha256,
                "source_report_sha256": args.inspection_resume_report_sha256,
                "old_version": latest.transition_version, "new_version": staged_inspection.transition_version,
                "old_expected_next_states": list(latest.expected_next_states),
                "new_expected_next_states": list(staged_inspection.expected_next_states)}
        staged_inspection.observe(state, source_sha256=source["source_sha256"])
        staged_inspection.commit_action(action, source_sha256=source["source_sha256"],
            expected_next_states=("loading", "modal", "race_list") if phase_before == "new" else
                                 ("loading", "race_list") if recommendation else ("loading", "main_menu"))

    first_step = len(step_reports) + 1
    for step in range(first_step, first_step + args.max_steps):
        if inspection:
            before_step = RouteTransitionSession.load(args.transition_session_json,
                expected_run_session_id=args.transition_run_session_id)
            phase_before = session.inspection_phase
            staged_inspection = None
            staged_amendment = None
        policy = _policy_for_step(
            base_policy,
            mode=args.mode,
            input_image=bool(args.input_image),
        )
        result = executor.run_once(
            mode=args.mode,
            execute=args.execute,
            confirm_token=args.confirm_token,
            policy=policy,
            session=session,
            screenshot_path=(
                Path(args.input_image)
                if args.input_image
                else screenshot_dir / f"race_route_step_{step:03d}.png"
            ),
            wait_seconds=args.wait_seconds,
            settle_seconds=args.settle_seconds,
            **({"transition_preflight": inspection_preflight} if inspection else {}),
            **({"transition_preflight": count_invocation.pre_tap}
               if count_invocation is not None and count_invocation.pre_tap is not None else {}),
        )
        step_reports.append({"step": step, "result": result})
        if (count_invocation is not None and session.count_resume_watch_pending
            and result.get("safety_verdict", {}).get("allowed") is True
            and ((result.get("performed") is True and result.get("tap_performed") is True
                  and result.get("decision", {}).get("action") == "tap")
                 or result.get("reason") == "race_result:continue_not_allowed")):
            session.count_resume_watch_pending = False
            session.count_resume_restart_title = None
        if inspection and args.mode == "live" and args.execute and (
            result.get("performed") is True or result.get("reason") == "race_inspection_complete"
        ):
            try:
                packet, verdict = result["frame_safety"], result["safety_verdict"]
                source_path = result["decision"]["evidence"]["image"]
                sha = hashlib.sha256(Path(source_path).read_bytes()).hexdigest()
                if (packet["source_image_sha256"] != sha or verdict.get("source_image_sha256") != sha
                    or verdict.get("allowed") is not True or inspection_ledger_path.read_bytes() != inspection_ledger_bytes):
                    raise ValueError("source_or_ledger_changed")
                latest = RouteTransitionSession.load(args.transition_session_json,
                    expected_run_session_id=args.transition_run_session_id)
                if latest.to_dict() != before_step.to_dict():
                    raise ValueError("session_changed")
                candidate = staged_inspection
                if result["decision"]["action"] == "tap":
                    receipt = result.get("route_action_receipt_source")
                    if (result.get("tap_performed") is not True or candidate is None
                        or not isinstance(receipt, dict) or receipt.get("source_sha256") != sha
                        or verdict.get("action_id") != receipt.get("action_id")):
                        raise ValueError("tap_receipt_invalid")
                else:
                    if resume_pending:
                        raise ValueError("resume_recommendation_required")
                    if result["reason"] == "race_inspection_complete":
                        validate_inspection_main(source_path, sha)
                    candidate = copy.deepcopy(latest)
                    candidate.observe("main_menu" if result["reason"] == "race_inspection_complete" else "loading",
                                      source_sha256=sha)
                    if result["decision"]["action"] == "wait":
                        candidate.commit_action("wait:race_inspection_loading", source_sha256=sha,
                            expected_next_states=("loading", "modal", "race_list") if phase_before == "opened" else
                                                 ("loading", "race_list") if phase_before == "confirmed" else ("loading", "main_menu"))
                candidate.save(args.transition_session_json)
                persisted = RouteTransitionSession.load(args.transition_session_json,
                    expected_run_session_id=args.transition_run_session_id)
                if persisted.to_dict() != candidate.to_dict():
                    raise ValueError("readback_mismatch")
                result["route_transition"] = persisted.to_dict()
                if staged_amendment is not None:
                    result["contract_amendment"] = staged_amendment
                    resume_pending = False
            except Exception as exc:
                inspection_error = f"race_inspection:persistence:{exc}"
                result["inspection_error"] = inspection_error
                performed += int(result.get("performed") is True)
                stop_reason = inspection_error
                break
        if is_race_scene_wait_result(result):
            race_scene_waits += 1
        else:
            race_scene_waits = 0
        if result.get("performed"):
            performed += 1
            tap_signature = _tap_signature(result)
            if tap_signature is None:
                previous_tap_signature = None
                repeated_tap_count = 0
                recent_tap_signatures.clear()
            elif tap_signature == previous_tap_signature:
                repeated_tap_count += 1
                recent_tap_signatures.append(tap_signature)
            else:
                previous_tap_signature = tap_signature
                repeated_tap_count = 1
                recent_tap_signatures.append(tap_signature)
            if len(recent_tap_signatures) > 18:
                del recent_tap_signatures[:-18]
            if repeated_tap_count >= MAX_CONSECUTIVE_IDENTICAL_TAPS:
                stop_reason = f"repeated_action_guard:{tap_signature[2]}"
                break
            cycle = _repeated_action_cycle(recent_tap_signatures)
            if cycle is not None:
                stop_reason = "repeated_action_cycle_guard:" + "->".join(
                    signature[2] for signature in cycle
                )
                break
            if result.get("reason") in RACE_LIST_EXIT_REASONS:
                stop_reason = str(result["reason"])
                break
            if result.get("reason") in {"career_complete", "terminal_confirmation_unknown"}:
                stop_reason = str(result["reason"])
                break
            if args.race_scene_wait_limit > 0 and race_scene_waits >= args.race_scene_wait_limit:
                stop_reason = "race_scene:wait_limit_reached"
                break
            continue
        stop_reason = str(result.get("reason", "stopped"))
        break
    else:
        stop_reason = "max_steps_reached"

    if inspection and stop_reason != "race_inspection_complete":
        inspection_error = inspection_error or stop_reason
    if count_invocation is not None:
        count_invocation.race_scene_waits = race_scene_waits
        count_invocation.repeated_tap_count = repeated_tap_count
        count_invocation.previous_tap_signature = previous_tap_signature
    last = step_reports[-1]["result"] if step_reports else {}
    normal_handoff = (inspection_error is None and last.get("reason") == stop_reason
                      and is_route_handoff_result(last))
    status = "completed" if stop_reason == "max_steps_reached" and not args.wbc_only else "stopped"
    return (
        {
            "is_valid": inspection_error is None,
            "status": "handoff" if normal_handoff else status,
            **({"handoff_target": last["decision"]["target"]} if normal_handoff else {}),
            "performed_steps": performed,
            "requested_steps": args.max_steps,
            "stop_reason": stop_reason,
            "step_reports": step_reports,
            **({"run_session_id": count_invocation.run_session_id,
                "observed_runner_turn": count_invocation.observed_runner_turn,
                "race_route_session_id": count_invocation.race_route_session_id,
                "planned_race_ids": list(session.planned_race_ids),
                "schedule_packet_sha256": session.schedule_packet_sha256} if count_invocation else {}),
            **({"inspection_only": True, "eligibility_status": "not_assessed",
                "tap_count": sum(item["result"].get("tap_performed") is True for item in step_reports),
                "goal_instance_id": active[0].goal_instance_id,
                "calendar_turn": inspection_main_turn,
                "ledger_revision": inspection_ledger.revision,
                "ledger_unchanged": inspection_ledger_path.read_bytes() == inspection_ledger_bytes} if inspection else {}),
        },
        0 if inspection_error is None else 2,
    )


def _tap_signature(
    result: dict[str, Any],
) -> tuple[str, str, str, int, int] | None:
    decision = result.get("decision")
    if not isinstance(decision, dict) or decision.get("action") != "tap":
        return None
    tap = decision.get("tap")
    if not isinstance(tap, dict):
        return None
    coordinate_name = tap.get("coordinate_name")
    x = tap.get("x")
    y = tap.get("y")
    if (
        not isinstance(coordinate_name, str)
        or not coordinate_name
        or not isinstance(x, int)
        or isinstance(x, bool)
        or not isinstance(y, int)
        or isinstance(y, bool)
    ):
        return None
    return (
        str(decision.get("reason", "")),
        str(decision.get("route_state", "")),
        coordinate_name,
        x,
        y,
    )


def _repeated_action_cycle(
    signatures: list[tuple[str, str, str, int, int]],
) -> tuple[tuple[str, str, str, int, int], ...] | None:
    for cycle_length in (2, 3):
        repeated_length = cycle_length * MAX_REPEATED_ACTION_CYCLES
        if len(signatures) < repeated_length:
            continue
        tail = signatures[-repeated_length:]
        cycle = tuple(tail[:cycle_length])
        if len(set(cycle)) < 2:
            continue
        if all(
            tuple(tail[offset : offset + cycle_length]) == cycle
            for offset in range(0, repeated_length, cycle_length)
        ):
            return cycle
    return None


def _policy_for_step(
    policy: RaceRoutePolicy,
    *,
    mode: str,
    input_image: bool,
) -> RaceRoutePolicy:
    if mode == "dry-run" and input_image:
        return policy
    return replace(policy, visible_text=None)


def _adb_factory(adb_port: int | None):
    def create_adb():
        from core.adb import ADB

        return ADB(port=adb_port) if adb_port is not None else ADB()

    return create_adb


def _print(data: dict[str, Any], *, indent: int | None, code: int) -> int:
    payload = json.dumps(data, ensure_ascii=False, indent=indent)
    output_encoding = getattr(sys.stdout, "encoding", None)
    if output_encoding:
        try:
            payload.encode(output_encoding)
        except (LookupError, UnicodeEncodeError):
            payload = json.dumps(data, ensure_ascii=True, indent=indent)
    print(payload)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
