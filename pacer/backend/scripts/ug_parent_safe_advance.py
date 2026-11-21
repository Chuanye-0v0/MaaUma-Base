from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.auto_trainer.route_handoff import is_route_handoff_result
from core.auto_trainer.safe_advance import (
    post_wait_navigation_reobserve,
    POST_WAIT_FREEZE_MAX_FRAME_DIFF,
    POST_WAIT_FREEZE_POLL_SECONDS,
    POST_WAIT_FREEZE_STABLE_FRAMES,
    POST_WAIT_FREEZE_TIMEOUT_SECONDS,
    SafeAdvanceExecutor,
    SpecialPageWaitBudget,
)
from core.auto_trainer.dashboard_runner import CooperativeRunnerDriver
from core.auto_trainer.career_goal_ledger import CareerGoalLedgerStore
from core.auto_trainer.larc_adaptation import LarcAdaptationSession
from core.auto_trainer.skip_session import RunSkipSession
from core.auto_trainer.route_transition_session import RouteTransitionSession
from core.auto_trainer.safe_advance_transition import is_global_route_interrupt


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely advance non-training Uma pages until a stop condition."
    )
    parser.add_argument("--mode", choices=["dry-run", "live"], default="dry-run")
    parser.add_argument("--max-steps", type=int, default=1)
    parser.add_argument(
        "--special-page-wait-limit",
        type=int,
        default=2,
        help="Hard cap for consecutive waits on recognized special-page transitions (1-3).",
    )
    parser.add_argument("--screenshot-dir", required=True)
    parser.add_argument("--input-image")
    parser.add_argument("--adb-port", type=int)
    parser.add_argument("--native-capture", action=argparse.BooleanOptionalAction, default=None,
                        help="Use MuMu device-2 renderer frames for this invocation only.")
    parser.add_argument("--scenario", choices=["grand_masters", "larc"], default="grand_masters")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-token")
    parser.add_argument(
        "--expected-action-id",
        help="Fail closed unless the fresh-frame tap uses this exact coordinate_name.",
    )
    parser.add_argument(
        "--allow-unbound-goal-summary-continue",
        action="store_true",
        help="One-shot, exact-action exception for an unbound goal summary page.",
    )
    parser.add_argument(
        "--run-session-id",
        help="Run identity bound to the persisted whole-run skip state.",
    )
    parser.add_argument(
        "--skip-session-json",
        help="Persisted whole-run skip state; requires --run-session-id.",
    )
    parser.add_argument(
        "--transition-session-json",
        help="Persisted expected-next-state route session; requires --run-session-id.",
    )
    parser.add_argument(
        "--transition-resync",
        action="store_true",
        help="Explicitly allow global reclassification for the first frame only.",
    )
    parser.add_argument("--loading-wait-resume-report")
    parser.add_argument("--loading-wait-resume-report-sha256")
    parser.add_argument("--capture-observation-report")
    parser.add_argument("--capture-observation-report-sha256")
    parser.add_argument("--capture-observation-invocation")
    parser.add_argument("--capture-observation-invocation-sha256")
    parser.add_argument(
        "--route-bootstrap-state",
        choices=[
            "training-menu",
            "main-menu",
            "dialogue",
            "event-choice",
            "loading",
            "modal",
            "race-list",
            "scenario-select",
            "career-opening",
            "live-career",
            "live-career-opening",
        ],
        help="Trusted actual screen for the first constrained route frame.",
    )
    parser.add_argument(
        "--career-goal-observed-turn",
        type=int,
        help="Current run turn used to bind structured career-goal perception.",
    )
    parser.add_argument(
        "--career-goal-ledger-json",
        help="Run-bound append-only career goal ledger.",
    )
    parser.add_argument("--allow-team-formation-confirm", action="store_true")
    parser.add_argument("--allow-return-main-menu-from-training", action="store_true")
    parser.add_argument("--allow-larc-ss", action="store_true")
    parser.add_argument("--allow-larc-first-arc-skill-inspection", action="store_true")
    parser.add_argument("--allow-larc-final-arc-skill-inspection", action="store_true")
    parser.add_argument("--larc-first-exchange-session-json",
                        help="Opt in to the two observed L'Arc race essentials, including menu entry and return.")
    parser.add_argument("--larc-first-expedition-session-json",
                        help="Opt in to the four CN-verified level-two essentials before the first expedition.")
    for training_kind in ("guts", "stamina", "power"):
        parser.add_argument(f"--larc-{training_kind}-expedition-session-json")
    parser.add_argument("--larc-speed-expedition-session-json",
                        help="Opt in to one nutrition-management level-two to level-three speed upgrade.")
    parser.add_argument("--larc-french-three-session-json",
                        help="Opt in to one French-ability level-two to level-three upgrade for 200 points.")
    parser.add_argument("--larc-expedition-skill-pt-session-json",
                        help="Opt in to one overseas-expedition level-one to level-three skill-point upgrade.")
    parser.add_argument("--larc-willpower-three-session-json",
                        help="Opt in to one willpower level-one to level-three upgrade.")
    parser.add_argument("--larc-courage-three-session-json",
                        help="Opt in to one courage level-two to level-three upgrade for 300 points.")
    parser.add_argument("--larc-first-overseas-race-session-json",
                        help="Opt in to the five verified level-two essentials for the first overseas race.")
    parser.add_argument("--larc-foy-prelude-session-json")
    parser.add_argument("--larc-first-arc-session-json",
                        help="Opt in to the eight observed level-two essentials for the first Arc race.")
    parser.add_argument("--larc-final-arc-session-json",
                        help="Opt in to the nine observed level-two essentials including the final Arc curse clearance.")
    parser.add_argument("--larc-champion-dream-two-session-json",
                        help="Opt in to the final Arc champion-dream level-one to level-two upgrade for 150 points.")
    parser.add_argument("--larc-bundle-session-json", help="Execute a saved multi-upgrade L'Arc plan in one visit.")
    parser.add_argument("--larc-inspect-adaptation-session-json",
                        help="Inspect unlocked aptitude details and return; never select an upgrade level.")
    parser.add_argument("--allow-enter-training-from-global-home", action="store_true")
    parser.add_argument("--allow-startup-promo-skip", action="store_true")
    parser.add_argument(
        "--allow-career-end-finish-after-skill-handoff",
        action="store_true",
    )
    parser.add_argument("--expected-career-end-skill-points", type=int)
    parser.add_argument("--expected-unused-skill-points", type=int)
    parser.add_argument("--allow-finish-training-with-unused-skill-points", action="store_true",
                        help="Finish after the skill owner returned; do not require a second exact Pt OCR.")
    parser.add_argument(
        "--visible-text",
        help="Optional OCR/manual visible text used as a secondary safety and screen-state hint.",
    )
    parser.add_argument("--wait-seconds", type=float, default=0.8)
    parser.add_argument("--settle-seconds", type=float, default=0.3)
    parser.add_argument(
        "--post-wait-freezes",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Wait for post-tap frames to stabilize before the next safe-advance step.",
    )
    parser.add_argument(
        "--post-wait-freeze-poll-seconds",
        type=float,
        default=POST_WAIT_FREEZE_POLL_SECONDS,
    )
    parser.add_argument(
        "--post-wait-freeze-timeout-seconds",
        type=float,
        default=POST_WAIT_FREEZE_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--post-wait-freeze-stable-frames",
        type=int,
        default=POST_WAIT_FREEZE_STABLE_FRAMES,
    )
    parser.add_argument(
        "--post-wait-freeze-max-frame-diff",
        type=float,
        default=POST_WAIT_FREEZE_MAX_FRAME_DIFF,
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


def run(
    args: argparse.Namespace,
    *,
    control_driver: CooperativeRunnerDriver | None = None,
    larc_member_roster: Any | None = None,
    larc_member_roster_path: str | Path | None = None,
    larc_observed_turn: int | None = None,
    larc_supporter_goal: dict[str, Any] | None = None,
    larc_ss_preview_only: bool = False,
    larc_ss_preview_decider: Any | None = None,
) -> tuple[dict[str, Any], int]:
    if args.max_steps <= 0:
        raise ValueError("max_steps_must_be_positive")
    if type(args.special_page_wait_limit) is not int or not 1 <= args.special_page_wait_limit <= 3:
        raise ValueError("special_page_wait_limit")
    if args.input_image and args.mode != "dry-run":
        raise ValueError("input_image_requires_dry_run")
    if args.input_image and args.max_steps != 1:
        raise ValueError("input_image_requires_max_steps_1")
    if args.allow_unbound_goal_summary_continue:
        if args.max_steps != 1:
            raise ValueError("unbound_goal_summary_requires_max_steps_1")
        if args.expected_action_id not in {
            "safe_advance.goal_achievement_continue",
            "safe_advance.goal_remaining_continue",
        }:
            raise ValueError("unbound_goal_summary_expected_action_required")
        if not args.post_wait_freezes:
            raise ValueError("unbound_goal_summary_post_wait_freeze_required")
        if (
            args.run_session_id
            or args.skip_session_json
            or args.transition_session_json
            or args.transition_resync
            or args.career_goal_observed_turn is not None
            or args.career_goal_ledger_json
        ):
            raise ValueError("unbound_goal_summary_state_forbidden")
    if args.expected_career_end_skill_points is not None and (
        args.expected_career_end_skill_points < 0
        or not args.allow_career_end_finish_after_skill_handoff
        or args.expected_action_id != "safe_advance.career_end_finish"
        or args.max_steps != 1
    ):
        raise ValueError("career_end_skill_points_requires_single_exact_finish")
    if args.expected_unused_skill_points is not None and (
        args.expected_unused_skill_points <= 0
        or args.expected_action_id != "safe_advance.finish_training"
        or args.max_steps != 1
        or not args.run_session_id
        or not args.transition_session_json
        or args.transition_resync
        or args.route_bootstrap_state is not None
    ):
        raise ValueError("unused_skill_points_requires_pending_single_exact_finish")
    state_files_supplied = bool(
        args.skip_session_json
        or args.transition_session_json
        or args.career_goal_ledger_json
        or getattr(args, "larc_first_exchange_session_json", None)
        or getattr(args, "larc_first_expedition_session_json", None)
        or getattr(args, "larc_speed_expedition_session_json", None)
        or any(getattr(args, f"larc_{kind}_expedition_session_json", None) for kind in ("guts", "stamina", "power"))
        or getattr(args, "larc_french_three_session_json", None)
        or getattr(args, "larc_expedition_skill_pt_session_json", None)
        or getattr(args, "larc_willpower_three_session_json", None)
        or getattr(args, "larc_courage_three_session_json", None)
        or getattr(args, "larc_first_overseas_race_session_json", None)
        or getattr(args, "larc_first_arc_session_json", None)
        or getattr(args, "larc_final_arc_session_json", None)
        or getattr(args, "larc_champion_dream_two_session_json", None)
        or getattr(args, "larc_bundle_session_json", None)
        or getattr(args, "larc_inspect_adaptation_session_json", None)
    )
    if bool(args.run_session_id) != state_files_supplied:
        raise ValueError("run_session_arguments_must_be_supplied_together")
    if args.allow_larc_first_arc_skill_inspection and (
        args.scenario != "larc" or args.max_steps != 1
        or args.expected_action_id != "safe_advance.larc_first_arc_skill_open"
        or not args.transition_session_json or args.transition_resync
    ):
        raise ValueError("larc_first_arc_skill_inspection_requires_single_exact_action")
    if args.allow_larc_final_arc_skill_inspection and (
        args.scenario != "larc" or args.max_steps != 1
        or args.expected_action_id != "safe_advance.larc_final_arc_skill_open"
        or not args.transition_session_json or args.transition_resync
    ):
        raise ValueError("larc_final_arc_skill_inspection_requires_single_exact_action")
    if bool(args.career_goal_observed_turn) != bool(
        args.career_goal_ledger_json
    ):
        raise ValueError("career_goal_arguments_must_be_supplied_together")
    if args.transition_resync and not args.transition_session_json:
        raise ValueError("transition_resync_requires_transition_session")
    loading_resume = bool(args.loading_wait_resume_report or args.loading_wait_resume_report_sha256)
    capture_values = (args.capture_observation_report, args.capture_observation_report_sha256,
                      args.capture_observation_invocation, args.capture_observation_invocation_sha256)
    capture_resume = any(capture_values)
    if capture_resume and (not all(capture_values) or args.mode != "live" or not args.execute
        or args.max_steps != 1 or args.input_image or args.transition_resync or args.route_bootstrap_state
        or loading_resume or not args.allow_return_main_menu_from_training
        or args.expected_action_id != "safe_advance.training_menu_back"
        or not args.transition_session_json or not Path(args.transition_session_json).is_file()
        or Path(args.screenshot_dir).exists()):
        raise ValueError("capture_observation_handoff_requires_new_single_exact_back_no_resync")
    if loading_resume and (
        not args.loading_wait_resume_report or not args.loading_wait_resume_report_sha256
        or args.mode != "live" or args.max_steps != 1 or args.input_image
        or args.transition_resync or args.route_bootstrap_state
        or not args.transition_session_json or not Path(args.transition_session_json).is_file()
    ):
        raise ValueError("loading_wait_resume_requires_single_existing_session_no_resync")
    SafeAdvanceExecutor.validate_post_wait_freeze_options(
        poll_seconds=args.post_wait_freeze_poll_seconds,
        timeout_seconds=args.post_wait_freeze_timeout_seconds,
        stable_frames=args.post_wait_freeze_stable_frames,
        max_frame_diff=args.post_wait_freeze_max_frame_diff,
    )

    screenshot_dir = Path(args.screenshot_dir)
    adaptation_path = getattr(args, "larc_first_exchange_session_json", None)
    expedition_path = getattr(args, "larc_first_expedition_session_json", None)
    speed_expedition_path = getattr(args, "larc_speed_expedition_session_json", None)
    additional_training = [(f"{kind}_expedition", getattr(args, f"larc_{kind}_expedition_session_json", None))
                           for kind in ("guts", "stamina", "power")]
    additional_paths = [path for phase_name, path in additional_training if path]
    french_path = getattr(args, "larc_french_three_session_json", None)
    expedition_skill_path = getattr(args, "larc_expedition_skill_pt_session_json", None)
    willpower_path = getattr(args, "larc_willpower_three_session_json", None)
    courage_path = getattr(args, "larc_courage_three_session_json", None)
    overseas_race_path = getattr(args, "larc_first_overseas_race_session_json", None)
    arc_path = getattr(args, "larc_first_arc_session_json", None)
    foy_path = getattr(args, "larc_foy_prelude_session_json", None)
    final_arc_path = getattr(args, "larc_final_arc_session_json", None)
    champion_dream_path = getattr(args, "larc_champion_dream_two_session_json", None)
    bundle_path = getattr(args, "larc_bundle_session_json", None)
    inspection_path = getattr(args, "larc_inspect_adaptation_session_json", None)
    if sum(bool(path) for path in (adaptation_path, expedition_path, speed_expedition_path, french_path, expedition_skill_path,
                                  willpower_path, courage_path, foy_path,
                                  overseas_race_path, arc_path, final_arc_path, champion_dream_path, bundle_path, inspection_path)) + len(additional_paths) > 1:
        raise ValueError("larc_adaptation_modes_exclusive")
    phase = "courage_three" if courage_path else ("willpower_three" if willpower_path else ("first_arc" if arc_path else ("first_overseas_race" if overseas_race_path else
        ("expedition_skill_pt" if expedition_skill_path else ("french_three" if french_path else ("speed_expedition" if speed_expedition_path else
        ("first_expedition" if expedition_path else "first_exchange")))))))
    if final_arc_path:
        phase = "final_arc"
    if foy_path:
        phase = "foy_prelude"
    if champion_dream_path:
        phase = "champion_dream_two"
    if additional_paths:
        phase = next(name for name, path in additional_training if path)
    if bundle_path:
        from core.auto_trainer.larc_adaptation import BUNDLE_PHASES, LANGUAGE_DEFERRED_PHASES, POWER_DEFERRED_PHASES
        phase = LarcAdaptationSession.load(Path(bundle_path), args.run_session_id).phase
        if phase not in BUNDLE_PHASES and phase not in LANGUAGE_DEFERRED_PHASES and phase not in POWER_DEFERRED_PHASES:
            raise ValueError("larc_adaptation_bundle_required")
    adaptation_path = (adaptation_path or expedition_path or speed_expedition_path or french_path or expedition_skill_path or
                       willpower_path or courage_path or foy_path or
                       overseas_race_path or arc_path or final_arc_path or champion_dream_path or bundle_path or inspection_path
                       or (additional_paths[0] if additional_paths else None))
    adaptation_session = None
    if adaptation_path:
        if (args.scenario != "larc" or not args.run_session_id or args.transition_resync
                or (args.mode == "live" and not args.transition_session_json)):
            raise ValueError("larc_adaptation_context_invalid")
        path = Path(adaptation_path)
        adaptation_session = (LarcAdaptationSession.load(path, args.run_session_id) if path.exists()
                              else LarcAdaptationSession(args.run_session_id, path=path,
                                                        inspect_only=bool(inspection_path), phase=phase))
        if adaptation_session.inspect_only != bool(inspection_path) or adaptation_session.phase != phase:
            raise ValueError("larc_adaptation_mode_mismatch")
    executor = SafeAdvanceExecutor(adb_factory=(lambda: args._capture_adb) if getattr(args, "_capture_adb", None) is not None else _adb_factory(
        args.adb_port, native_capture=args.native_capture),
        **({"safety_analyzer": args._safety_analyzer} if getattr(args, "_safety_analyzer", None) is not None else {}))
    special_page_wait_budget = SpecialPageWaitBudget(
        limit=args.special_page_wait_limit,
    )
    skip_session_path = Path(args.skip_session_json) if args.skip_session_json else None
    skip_session = None
    if skip_session_path is not None:
        skip_session = (
            RunSkipSession.load(
                skip_session_path,
                expected_run_session_id=args.run_session_id,
            )
            if skip_session_path.exists()
            else RunSkipSession.new(args.run_session_id)
        )
    transition_session_path = (
        Path(args.transition_session_json)
        if args.transition_session_json
        else None
    )
    transition_session = None
    if transition_session_path is not None:
        transition_session = (
            RouteTransitionSession.load(
                transition_session_path,
                expected_run_session_id=args.run_session_id,
            )
            if transition_session_path.exists()
            else RouteTransitionSession.new(args.run_session_id)
        )
    step_reports: list[dict[str, Any]] = []
    performed = 0
    stop_reason = "max_steps_reached"
    last_tap_signature: tuple[Any, ...] | None = None
    repeated_tap_count = 0
    adb_tap_reobserve_count = 0
    recent_reasons: list[str] = []
    # The immediately preceding observer may hand off one native home frame.
    # Consume it once; subsequent steps and post-tap captures must stay fresh.
    initial_frame = vars(args).pop("_initial_frame", None)

    for step in range(1, args.max_steps + 1):
        screenshot_path = (
            Path(args.input_image)
            if args.input_image
            else screenshot_dir / f"advance_step_{step:03d}.png"
        )
        visible_text = _visible_text_for_step(
            args.visible_text,
            mode=args.mode,
            input_image=bool(args.input_image),
        )
        result = executor.run_once(
            **({"initial_frame": initial_frame} if step == 1 and initial_frame is not None else {}),
            mode=args.mode,
            execute=args.execute,
            confirm_token=args.confirm_token,
            scenario=args.scenario,
            screenshot_path=screenshot_path,
            wait_seconds=args.wait_seconds,
            settle_seconds=args.settle_seconds,
            post_wait_freezes=args.post_wait_freezes,
            post_wait_freeze_poll_seconds=args.post_wait_freeze_poll_seconds,
            post_wait_freeze_timeout_seconds=args.post_wait_freeze_timeout_seconds,
            post_wait_freeze_stable_frames=args.post_wait_freeze_stable_frames,
            post_wait_freeze_max_frame_diff=args.post_wait_freeze_max_frame_diff,
            allow_team_formation_confirm=args.allow_team_formation_confirm,
            allow_return_main_menu_from_training=args.allow_return_main_menu_from_training,
            allow_larc_ss=args.allow_larc_ss,
            allow_larc_first_arc_skill_inspection=args.allow_larc_first_arc_skill_inspection,
            allow_larc_final_arc_skill_inspection=args.allow_larc_final_arc_skill_inspection,
            larc_ss_preview_only=larc_ss_preview_only,
            larc_ss_preview_decider=larc_ss_preview_decider,
            larc_supporter_goal=larc_supporter_goal,
            larc_adaptation_session=adaptation_session,
            larc_member_roster=larc_member_roster,
            larc_member_roster_path=larc_member_roster_path,
            larc_observed_turn=larc_observed_turn,
            allow_enter_training_from_global_home=(
                args.allow_enter_training_from_global_home
            ),
            allow_career_end_finish_after_skill_handoff=(
                args.allow_career_end_finish_after_skill_handoff
            ),
            expected_career_end_skill_points=(
                args.expected_career_end_skill_points
            ),
            expected_unused_skill_points=args.expected_unused_skill_points,
            allow_finish_training_with_unused_skill_points=args.allow_finish_training_with_unused_skill_points,
            allow_startup_promo_skip=args.allow_startup_promo_skip,
            visible_text=visible_text,
            expected_action_id=args.expected_action_id,
            skip_session=skip_session,
            transition_session=transition_session,
            transition_resync=bool(args.transition_resync and step == 1),
            loading_wait_resume_report=args.loading_wait_resume_report,
            loading_wait_resume_report_sha256=args.loading_wait_resume_report_sha256,
            loading_wait_resume_session_path=transition_session_path if loading_resume else None,
            capture_observation_report=args.capture_observation_report,
            capture_observation_report_sha256=args.capture_observation_report_sha256,
            capture_observation_invocation=args.capture_observation_invocation,
            capture_observation_invocation_sha256=args.capture_observation_invocation_sha256,
            capture_observation_session_path=transition_session_path if capture_resume else None,
            route_bootstrap_state=(
                args.route_bootstrap_state if step == 1 else None
            ),
            special_page_wait_budget=special_page_wait_budget,
            control_driver=control_driver,
            goal_completion_observe_only=args.scenario == "larc",
            career_goal_observed_turn=args.career_goal_observed_turn,
            career_goal_ledger_path=(
                Path(args.career_goal_ledger_json)
                if args.career_goal_ledger_json
                else None
            ),
            career_goal_run_session_id=(
                args.run_session_id if args.career_goal_ledger_json else None
            ),
            career_goal_ledger_store=(
                CareerGoalLedgerStore()
                if args.career_goal_ledger_json
                else None
            ),
            allow_unbound_goal_summary_continue=(
                args.allow_unbound_goal_summary_continue
            ),
        )
        if skip_session is not None:
            result["skip_session"] = skip_session.to_dict()
            if args.mode == "live" and not (loading_resume or capture_resume):
                skip_session.save(skip_session_path)
        if adaptation_session is not None:
            result["larc_adaptation_session"] = adaptation_session.to_dict()
        if transition_session is not None:
            if args.mode == "live" and not (loading_resume or capture_resume):
                result["route_transition"] = transition_session.to_dict()
                transition_session.save(transition_session_path)
            else:
                result.setdefault("route_transition", transition_session.to_dict())
        recovery_key = "capture_observation_handoff" if capture_resume else "contract_amendment"
        if (loading_resume or capture_resume) and isinstance(result.get(recovery_key), dict):
            try:
                transition_session.save(transition_session_path)
                persisted = RouteTransitionSession.load(transition_session_path, expected_run_session_id=args.run_session_id)
                if persisted.to_dict() != transition_session.to_dict():
                    raise ValueError("loading_wait_resume_readback_mismatch")
                if skip_session is not None:
                    skip_session.save(skip_session_path)
                result[recovery_key]["persisted"] = True
            except Exception as exc:
                result[recovery_key]["persisted"] = False
                result[recovery_key]["persistence_status"] = "failed_or_unverified_disk_may_be_updated"
                result["performed"] = False
                result["reason"] = f"{recovery_key}_persistence:{exc}" if capture_resume else f"loading_wait_resume_persistence:{exc}"
        step_reports.append(
            {"step": step, "screenshot_path": str(screenshot_path), "result": result}
        )
        if result.get("performed"):
            performed += 1
            if result.get("reason") in {"career_complete", "terminal_confirmation_unknown"}:
                stop_reason = str(result["reason"])
                break
            decision = result.get("decision")
            if isinstance(decision, dict) and is_global_route_interrupt(decision):
                stop_reason = "global_route_interrupt_performed"
                break
            reason = _result_reason(result)
            signature = _tap_signature(result)
            if signature is not None and signature == last_tap_signature:
                repeated_tap_count += 1
            else:
                last_tap_signature = signature
                repeated_tap_count = 1 if signature is not None else 0
            recent_reasons.append(reason)
            if _unexpected_game_home_after_setup(recent_reasons):
                stop_reason = "unexpected_game_home_after_setup"
                break
            if repeated_tap_count >= 3:
                stop_reason = "route_stalled"
                break
            continue
        if result.get("tap_performed"):
            performed += 1
        if post_wait_navigation_reobserve(result):
            # Navigation was submitted, but no page/result completion is claimed.
            # Re-enter the normal reader with a new frame; never replay a stored tap.
            result['read_only_recovery']='fresh_observation_after_navigation'
            continue
        if (
            result.get("reason") == "post_wait_freeze_insufficient_budget"
            and result.get("tap_performed")
        ):
            # The tap was already executed; only the post-tap freeze
            # verification budget was exhausted on a slow emulator.  Treat the
            # step as performed and let the next step re-capture/re-plan.
            continue
        if (
            result.get("reason") == "skip_session_regression_probe"
            and not result.get("tap_performed")
            and isinstance(result.get("decision"), dict)
            and result["decision"].get("action") == "wait"
        ):
            continue
        if (
            args.mode == "live"
            and not (loading_resume or capture_resume)
            and adb_tap_reobserve_count < 1
            and _safe_adb_tap_reobserve(result, adaptation_session)
        ):
            adb_tap_reobserve_count += 1
            result["read_only_recovery"] = "fresh_observation_after_adb_tap_failure"
            continue
        stop_reason = str(result.get("reason", "stopped"))
        break
    else:
        stop_reason = "max_steps_reached"

    report = {
            "is_valid": True,
            "status": ("completed" if stop_reason == "max_steps_reached" else
                       "handoff" if step_reports and is_route_handoff_result(step_reports[-1]["result"])
                       else "stopped"),
            "performed_steps": performed,
            "requested_steps": args.max_steps,
            "stop_reason": stop_reason,
            "step_reports": step_reports,
    }
    if skip_session is not None:
        report["skip_session"] = skip_session.to_dict()
        report["skip_session_path"] = str(skip_session_path)
    if transition_session is not None:
        preview = (
            step_reports[-1]["result"].get("route_transition")
            if args.mode == "dry-run" and step_reports
            else None
        )
        report["route_transition"] = preview or transition_session.to_dict()
        report["transition_session_path"] = str(transition_session_path)
    if stop_reason == "ready_main_menu" and step_reports:
        source_path = Path(step_reports[-1]["screenshot_path"])
        if source_path.is_file():
            report["source_screenshot_path"] = str(source_path)
        else:
            report["is_valid"] = False
            report["status"] = "stopped"
            report["stop_reason"] = "ready_main_menu_source_missing"
            return (report, 2)
    if stop_reason.startswith(("loading_wait_resume_persistence:", "capture_observation_handoff_persistence:")):
        report["is_valid"] = False
        return (report, 2)
    return (report, 0)


def _visible_text_for_step(
    visible_text: str | None,
    *,
    mode: str,
    input_image: bool,
) -> str | None:
    if mode == "dry-run" and input_image:
        return visible_text
    return None


def _adb_factory(adb_port: int | None, *, native_capture: bool | None = None):
    from core.native_capture_adb import native_instance_id
    cached = None
    def create_adb():
        nonlocal cached
        if native_capture is True or (native_capture is None and native_instance_id(adb_port) is not None):
            from core.native_capture_adb import NativeCaptureADB

            if cached is None:
                cached = NativeCaptureADB(port=adb_port)
            return cached
        from core.adb import ADB

        return ADB(port=adb_port) if adb_port is not None else ADB()

    return create_adb


SETUP_REASONS = {
    "scenario_select_continue",
    "trainee_select_continue",
    "inheritance_select_continue",
    "support_deck_select_friend",
    "support_deck_start_training",
    "friend_support_select_first",
}
REPEAT_STALL_EXEMPT_REASONS = {
    "advance_dialogue",
    "skip_bottom_dialogue",
    "enable_bottom_skip_toggle",
    "story_fast_forward",
    "skip_race_story_scene",
}
_NON_REOBSERVABLE_SPEND_OR_TERMINAL_TAPS = {
    "safe_advance.larc_adaptation_upgrade",
    "safe_advance.larc_ss_confirm",
    "safe_advance.finish_training",
    "safe_advance.career_end_finish",
}


def _safe_adb_tap_reobserve(
    result: dict[str, Any],
    adaptation_session: Any | None = None,
) -> bool:
    """Allow one fresh-screen re-observation after a non-spending ADB tap failure."""
    if (
        result.get("performed") is not False
        or result.get("tap_performed")
        or result.get("reason") != "adb_tap_failed"
    ):
        return False
    if adaptation_session is not None and getattr(adaptation_session, "stage", None) == "submission_pending":
        return False
    decision = result.get("decision")
    if not isinstance(decision, dict) or decision.get("action") != "tap":
        return False
    tap = decision.get("tap")
    if not isinstance(tap, dict):
        return False
    coord = str(tap.get("coordinate_name") or "")
    return bool(coord) and coord not in _NON_REOBSERVABLE_SPEND_OR_TERMINAL_TAPS


def _result_reason(result: dict[str, Any]) -> str:
    decision = result.get("decision")
    if isinstance(decision, dict) and decision.get("reason"):
        return str(decision["reason"])
    return str(result.get("reason", "unknown"))


def _tap_signature(result: dict[str, Any]) -> tuple[Any, ...] | None:
    decision = result.get("decision")
    if not isinstance(decision, dict) or decision.get("action") != "tap":
        return None
    if str(decision.get("reason", "")) in REPEAT_STALL_EXEMPT_REASONS:
        return None
    tap = decision.get("tap") if isinstance(decision.get("tap"), dict) else {}
    explanation_text = None
    if decision.get("reason") == "larc_explanation_continue":
        evidence = decision.get("evidence", {})
        if evidence.get("larc_explanation", {}).get("kind") == "scenario_explanation":
            # Different tutorial pages share one continue button; preserve the
            # same-page stall limit without treating real page turns as stalls.
            explanation_text = evidence.get("screen_text", {}).get("normalized_text")
    return (
        decision.get("reason"),
        tap.get("coordinate_name"),
        tap.get("x"),
        tap.get("y"),
        tap.get("scaled"),
        explanation_text,
    )


def _unexpected_game_home_after_setup(recent_reasons: list[str]) -> bool:
    if not recent_reasons or recent_reasons[-1] != "enter_training_from_home":
        return False
    return any(reason in SETUP_REASONS for reason in recent_reasons[:-1])


def _print(data: dict[str, Any], *, indent: int | None, code: int) -> int:
    print(json.dumps(data, ensure_ascii=True, indent=indent))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
