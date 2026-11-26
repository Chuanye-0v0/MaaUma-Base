from __future__ import annotations

import copy

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from time import sleep
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.auto_trainer.capture_adapter import load_capture_group_raws
from core.auto_trainer.pacer_storage import _io
from core.auto_trainer.config import load_goal_config
from core.auto_trainer.runtime_control import decision_scope
from core.auto_trainer.pipeline_timing import timed_call,profile_pipeline
from core.auto_trainer.larc_supporter_goal import deadline_larc_supporter_goal
from core.auto_trainer.parser_sidecar import build_training_options_sidecar
from core.auto_trainer.training_page_observation import (
    build_training_page_observation,
)
from core.auto_trainer.training_current_stats import read_training_current_stats
from core.auto_trainer.main_menu_state_provider import (
    SCHEMA_VERSION as MAIN_MENU_STATE_CONTEXT_SCHEMA_VERSION,
    provide_main_menu_state,
)
from core.auto_trainer.condition_context import (
    load_explicit_condition_context,
    merge_explicit_conditions,
)
from core.auto_trainer.dashboard_runner import ControlledStop, CooperativeRunnerDriver
from core.auto_trainer.grand_masters_scenario import (
    GRAND_MASTERS_ACTION_IDS,
    GrandMastersScenarioAdapter,
    GrandMastersScenarioRequest,
    GrandMastersTrainingPage,
)
from core.auto_trainer.progressive_route_controller import ProgressiveRouteController
from core.auto_trainer.progressive_route_graph import ProgressiveRouteGraph
from core.auto_trainer.progressive_route_policy import build_player_scenario_policy
from core.auto_trainer.progressive_route_session import ProgressiveRouteSession
from core.auto_trainer.career_supervisor_turn import (
    build_turn_authority_bundle,
)
from core.auto_trainer.perception_reliability import (
    load_trusted_gate_authorization_registry_from_workspace,
)
from core.auto_trainer.perception_reliability_builder import (
    build_perception_reliability_artifact,
    write_perception_reliability_artifact_exclusive,
)
from core.auto_trainer.state import TRAINING_TYPES
from scripts.capture_ug_parent_live_tabs import main as capture_live_tabs_main
from scripts.ug_parent_live_loop import main as live_loop_main


PARTIAL_LIVE_SIDECAR_MIN_CONFIDENCE = 0.35
_LEGACY_FORMAL_CAPTURE_ERRORS = (
    "capture_group_not_found:",
    "capture_group_missing_actual_group_id",
    "capture_group_missing_turn_index",
    "capture_raw_invalid_sha256:",
    "capture_raw_missing_file:",
    "capture_raw_missing_tabs:",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one UG parent training turn. Formal live execution currently uses "
            "only the deterministic local policy; static and command advisors are "
            "dry-run-only."
        )
    )
    parser.add_argument("--goal-config", required=True)
    parser.add_argument("--output-root")
    parser.add_argument("--capture-manifest")
    parser.add_argument("--execution-root")
    parser.add_argument("--actual-group-id", required=True)
    parser.add_argument("--run-session-id", required=True)
    parser.add_argument("--turn-index", type=int, required=True)
    parser.add_argument("--source-state-key", required=True)
    parser.add_argument("--operator-id", required=True)
    state_context = parser.add_mutually_exclusive_group(required=True)
    state_context.add_argument("--state-context-json")
    state_context.add_argument("--state-context-provider", choices=["main-menu-png"])
    parser.add_argument("--main-menu-screenshot-path")
    parser.add_argument(
        "--main-menu-consensus-screenshot-path",
        action="append",
        default=[],
    )
    parser.add_argument(
        "--start-screen",
        choices=["main-menu", "training-menu"],
        default="training-menu",
    )
    parser.add_argument("--settle-seconds", type=float, default=0.9)
    parser.add_argument("--adb-port", type=int)
    parser.add_argument("--transition-session-json")
    parser.add_argument("--skip-session-json")
    parser.add_argument("--capture-live", action="store_true")
    parser.add_argument("--capture-execute", action="store_true")
    parser.add_argument("--capture-confirm-token")
    parser.add_argument("--sidecar-output-json")
    parser.add_argument("--training-options-json")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--mode", choices=["dry-run", "live"], default="dry-run")
    parser.add_argument("--execute-training-confirm", action="store_true")
    parser.add_argument("--prefer-larc-ss", action="store_true")
    parser.add_argument("--joint-collect-only", action="store_true",
                        help="Experimental L'Arc observation only; never confirm a primary action.")
    parser.add_argument("--collect-larc-ss-information", action="store_true")
    parser.add_argument("--initialize-larc-reward-cycles", action="store_true",
                        help="Initialize reward sequences once, then use run-local cycles")
    parser.add_argument("--larc-member-roster-json")
    parser.add_argument("--training-confirm-token")
    parser.add_argument("--max-turns", type=int, required=True)
    parser.add_argument(
        "--ai-review-json",
        help="Static advisor JSON for dry-run only; live mode rejects it.",
    )
    parser.add_argument(
        "--ai-review-command",
        help=(
            "Dry-run-only protocol/replay advisor command; live mode rejects it until "
            "a sandboxed provider is available."
        ),
    )
    parser.add_argument("--ai-review-command-timeout-seconds", type=int, default=60)
    parser.add_argument(
        "--structured-advisor-mode",
        choices=["off", "observer", "advisor"],
        default="off",
    )
    parser.add_argument(
        "--structured-advisor-command-arg",
        action="append",
        default=[],
    )
    parser.add_argument("--structured-advisor-model-id")
    parser.add_argument(
        "--structured-advisor-invalid-policy",
        choices=["local_fallback", "stop"],
        default="local_fallback",
    )
    parser.add_argument(
        "--structured-advisor-timeout-seconds",
        type=int,
        default=60,
    )
    parser.add_argument(
        "--structured-advisor-ttl-ms",
        type=int,
        default=30_000,
    )
    parser.add_argument("--min-live-sidecar-confidence", type=float, default=0.65)
    parser.add_argument("--min-decision-confidence", type=float, default=0.0)
    parser.add_argument("--allow-partial-live-sidecar", action="store_true")
    parser.add_argument("--post-action-skip-taps", type=int, default=0)
    parser.add_argument(
        "--numeric-blind-fallback",
        action="store_true",
        help="Use the local numeric-blind fallback policy for this process run.",
    )
    parser.add_argument("--condition-report-json")
    parser.add_argument("--require-explicit-condition-probe", action="store_true")
    parser.add_argument("--progressive-route-session")
    parser.add_argument(
        "--scenario-policy-id",
        choices=["hold", "first_usable_goddess"],
        default="hold",
    )
    parser.add_argument("--scenario-audit-out")
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def _prepare_progressive_scenario(
    controller: ProgressiveRouteController,
    pages: tuple[GrandMastersTrainingPage, ...],
    policy_id: str,
    audit_out: Path,
):
    """Bind five structured pages and choose only through a finite local policy."""
    if not isinstance(controller, ProgressiveRouteController):
        raise TypeError("controller must be ProgressiveRouteController")
    if not isinstance(pages, tuple) or len(pages) != len(TRAINING_TYPES) or any(
        not isinstance(page, GrandMastersTrainingPage) for page in pages
    ):
        raise TypeError("pages must be structured GrandMastersTrainingPage records")
    if (
        {page.training_type for page in pages} != set(TRAINING_TYPES)
        or len({page.source_frame_sha256 for page in pages}) != len(TRAINING_TYPES)
    ):
        raise TypeError("pages must be structured GrandMastersTrainingPage records")
    adapter = GrandMastersScenarioAdapter(
        player_policy=build_player_scenario_policy(policy_id)
    )
    session = controller.session
    recollection = session.scenario_recollection_required
    source_frame_sha256 = (
        pages[-1].source_frame_sha256
        if recollection
        else session.last_observation_sha256
    )
    if source_frame_sha256 is None:
        raise ValueError("scenario source frame is unavailable")
    request = GrandMastersScenarioRequest(
        run_session_id=session.run_session_id,
        transition_version=session.transition_version,
        generation=session.scenario_generation + (1 if recollection else 0),
        source_frame_sha256=source_frame_sha256,
        current_node_id=session.current_node_id,
        training_pages=pages,
        usable_goddess_action_ids=GRAND_MASTERS_ACTION_IDS[1:],
        goddess_availability_known=True,
    )
    packet = (
        controller.build_recollected_scenario_packet(adapter=adapter, request=request)
        if recollection
        else controller.build_scenario_packet(adapter=adapter, request=request)
    )
    decision = controller.choose_and_commit_scenario_action(
        adapter=adapter,
        packet=packet,
        recollection=recollection,
    )
    audit = {
        "schema": "uma.progressive_scenario_preparation.v1",
        "run_session_id": session.run_session_id,
        "policy_id": policy_id,
        "packet": packet.to_dict(),
        "decision": {
            "action_id": decision.action_id,
            "packet_sha256": decision.packet_sha256,
            "source": decision.source,
            "confidence": decision.confidence,
            "invalidates_collection": decision.invalidates_collection,
        },
        "session_transition_version": session.transition_version,
        "scenario_recollection_required": session.scenario_recollection_required,
    }
    audit["audit_sha256"] = hashlib.sha256(
        json.dumps(
            audit,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    _atomic_write_json(audit_out, audit)
    return decision


def _load_progressive_training_pages(
    sidecar_path: Path,
    *,
    state_context: dict[str, Any],
    expected_frame_sha256: dict[str, str],
) -> tuple[GrandMastersTrainingPage, ...]:
    try:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("progressive scenario sidecar is invalid") from exc
    options = payload.get("training_options") if isinstance(payload, dict) else None
    if not isinstance(options, list) or len(options) != len(TRAINING_TYPES):
        raise ValueError("progressive scenario requires exactly five training pages")
    mood_value = state_context.get("mood", state_context.get("motivation"))
    mood = mood_value if type(mood_value) is str and mood_value else None
    pages: list[GrandMastersTrainingPage] = []
    for option in options:
        if not isinstance(option, dict):
            raise ValueError("progressive scenario training option is invalid")
        raw_data = option.get("raw_data")
        capture = raw_data.get("capture") if isinstance(raw_data, dict) else None
        source_sha256 = capture.get("sha256") if isinstance(capture, dict) else None
        if type(source_sha256) is not str:
            raise ValueError("progressive scenario page lacks source frame SHA")
        training_type = option.get("training_type")
        if (
            type(training_type) is not str
            or expected_frame_sha256.get(training_type) != source_sha256
        ):
            raise ValueError("progressive scenario page SHA does not match capture manifest")
        pages.append(
            GrandMastersTrainingPage.from_training_data(
                source_sha256,
                option,
                mood=mood,
            )
        )
    if {page.training_type for page in pages} != set(TRAINING_TYPES):
        raise ValueError("progressive scenario training page types are incomplete")
    if len({page.source_frame_sha256 for page in pages}) != len(TRAINING_TYPES):
        raise ValueError("progressive scenario training page frames must be distinct")
    return tuple(pages)


def main(
    argv: list[str] | None = None,
    *,
    formal_condition_binding_key: str | None = None,
    control_driver: CooperativeRunnerDriver | None = None,
    training_parser: Any | None = None,
) -> int:
    args = parse_args(argv)
    indent = 2 if args.pretty else None
    try:
        report, code = run_pipeline(
            args,
            formal_condition_binding_key=formal_condition_binding_key,
            control_driver=control_driver,
            training_parser=training_parser,
        )
    except Exception as exc:
        return _print(
            {"is_valid": False, "error": f"{type(exc).__name__}: {exc}"},
            indent=indent,
            code=2,
        )
    report["advisor"] = {"source": _advisor_source(args)}
    return _print(report, indent=indent, code=code)


@decision_scope
@profile_pipeline
def run_pipeline(
    args: argparse.Namespace,
    *,
    formal_condition_binding_key: str | None = None,
    control_driver: CooperativeRunnerDriver | None = None,
    training_parser: Any | None = None,
    larc_supporter_goal: dict[str, Any] | None = None,
    larc_friend_outing: dict[str, Any] | None = None,
    larc_fan_ss_only: bool = False,
    larc_fan_plan: dict[str, Any] | None = None,
    larc_pre_action: Callable[[Any], dict[str, Any] | None] | None = None,
    larc_ss_sieve_pre_action: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
) -> tuple[dict[str, Any], int]:
    _validate_ai_review_source(args)
    _validate_state_context_configuration(args)
    joint_collect = getattr(args, "joint_collect_only", False)
    _enable_ss_deadline_comparison(args, joint_collect=joint_collect)
    if joint_collect and (args.prefer_larc_ss or args.execute_training_confirm
            or args.structured_advisor_mode != "off" or args.mode != "live"
            or not args.capture_live or not args.capture_execute
            or args.capture_confirm_token != "CAPTURE_UG_PARENT_LIVE_TABS"
            or not args.transition_session_json or not args.skip_session_json or args.adb_port is None
            or load_goal_config(args.goal_config).scenario != "larc"):
        raise ValueError("joint_collect_context_or_execution_conflict")
    if larc_fan_ss_only and not getattr(args, "prefer_larc_ss", False):
        raise ValueError("larc_fan_ss_comparison_required")
    if args.larc_member_roster_json and training_parser is None:
        from core.training_parser import TrainingParser

        training_parser = TrainingParser(use_mimo=False, recognition_mode="lightweight",
                                         larc_roster_path=args.larc_member_roster_json)
    initialize_cycles = getattr(args, "initialize_larc_reward_cycles", False)
    if (args.collect_larc_ss_information or initialize_cycles) and (
        args.mode != "live" or not args.capture_live or not args.capture_execute
        or args.capture_confirm_token != "CAPTURE_UG_PARENT_LIVE_TABS"
        or args.capture_manifest or args.execution_root or args.adb_port is None
        or not args.transition_session_json or training_parser is None
        or training_parser.larc_roster_path is None
        or load_goal_config(args.goal_config).scenario != "larc"
    ):
        raise ValueError("larc_information_capture_context_required")
    if getattr(args, "prefer_larc_ss", False) and (
        args.mode != "live" or not args.execute_training_confirm
        or args.training_confirm_token != "UG_PARENT_AUTOTRAINER_SANDBOX"
        or not args.transition_session_json or not args.skip_session_json or args.adb_port is None
        or load_goal_config(args.goal_config).scenario != "larc"
    ):
        raise ValueError("larc_ss_execution_context_required")
    if bool(args.progressive_route_session) != bool(args.scenario_audit_out):
        raise ValueError(
            "progressive route session and scenario audit output must be provided together"
        )
    if args.progressive_route_session and args.mode != "dry-run":
        raise ValueError("progressive scenario preparation is dry-run-only")
    condition_context = _load_condition_context(
        args,
        required_turn_root=(
            Path(args.output_root).parent
            if args.output_root
            else Path(args.run_dir).parent
        ),
        formal_condition_binding_key=formal_condition_binding_key,
        expected_turn_index=args.turn_index,
    )
    if args.mode == "live":
        _validate_live_min_sidecar_confidence(args.min_live_sidecar_confidence)
    if args.state_context_provider and (args.capture_manifest or args.execution_root):
        return (
            {
                "is_valid": False,
                "status": "provider_requires_fresh_capture",
                "error": "provider_requires_fresh_capture",
            },
            2,
        )
    if control_driver is not None:
        try:
            control_driver.reach("before_capture")
        except ControlledStop as exc:
            return (
                {
                    "is_valid": True,
                    "status": "control_stopped",
                    "control_boundary": exc.boundary,
                },
                0,
            )
    provider_stage = timed_call('main_menu_state',_prepare_provider_state_context,args)
    if provider_stage is not None and not provider_stage["is_valid"]:
        return (
            {
                "is_valid": False,
                "status": "state_context_provider_invalid",
                "state_context_provider": provider_stage,
            },
            2,
        )
    if larc_supporter_goal is not None:
        source = larc_supporter_goal.get("source", {})
        if (not (args.prefer_larc_ss or joint_collect) or provider_stage is None
                or source.get("run_session_id") != args.run_session_id
                or source.get("turn_index") != args.turn_index
                or Path(source.get("path", "")).resolve() != Path(args.main_menu_screenshot_path).resolve()
                or not source.get("sha256")
                or source["sha256"] != provider_stage.get("source_screenshot_sha256")):
            raise ValueError("larc_supporter_goal_source_mismatch")
    if larc_fan_plan is not None:
        source = larc_fan_plan.get("source", {})
        if (provider_stage is None or source.get("run_session_id") != args.run_session_id
                or source.get("turn_index") != args.turn_index
                or source.get("sha256") != provider_stage.get("source_screenshot_sha256")
                or Path(source.get("path", "")).resolve() != Path(args.main_menu_screenshot_path).resolve()):
            raise ValueError("larc_fan_plan_source_mismatch")
    deadline_ss_only = deadline_larc_supporter_goal(larc_supporter_goal)
    capture_report = timed_call('capture',_run_or_reuse_capture,args)
    if capture_report.get("dry_run"):
        return (
            {
                "is_valid": True,
                "status": "capture_dry_run",
                "stages": {
                    "capture": capture_report,
                    **({"state_context_provider": provider_stage} if provider_stage else {}),
                },
            },
            0,
        )
    if not capture_report.get("is_valid", True):
        return _stage_error("capture", capture_report), 2

    if larc_ss_sieve_pre_action is not None:
        sieve = larc_ss_sieve_pre_action(capture_report)
        if sieve is not None:
            return {"is_valid": True, "status": "ss_sieve_return_home", "turn_consumed": False,
                    "ss_sieve_evidence": sieve, "stages": {"capture": capture_report}}, 0

    if args.collect_larc_ss_information or initialize_cycles:
        information = ({"status": "not_required", "reason": "independent_deadline_ss"}
                       if deadline_ss_only else timed_call('ss_reward_information',_collect_larc_ss_information,args, training_parser, capture_report))
        capture_report["larc_ss_information"] = information
        if (information.get("fresh_training_capture_required") is True
                and information.get("returned_to_training") is True
                and information.get("turn_consumed") is False
                and information.get("turn_consuming_action_executed") is False):
            # The probe closed a pre-existing information page. Its old
            # training collection cannot authorize an action after recovery.
            return {"is_valid": True, "status": "larc_ss_information_recollect",
                    "stages": {"capture": capture_report,
                               "larc_ss_information": information}}, 0
        if not deadline_ss_only and information["status"] not in {"completed", "cached", "optional_unavailable"}:
            return _stage_error("larc_ss_information", information, capture_report), 2

    ss_report = None

    manifest_path = Path(str(capture_report["manifest_path"]))
    execution_root = Path(str(capture_report["output_root"]))
    state_context_source_path = Path(
        provider_stage["state_context_source_path"]
        if provider_stage is not None
        else args.state_context_json
    )
    state_context_snapshot_path = _capture_context_path(capture_report, execution_root)
    state_context = _read_state_context_snapshot(state_context_snapshot_path)
    growth_source_bound = _bind_larc_strategy18_growth(args, state_context, provider_stage)
    purchase_marker_added = _inject_same_turn_purchase_marker(args, state_context)
    if larc_supporter_goal is not None:
        state_context["scenario_state"] = {
            **state_context.get("scenario_state", {}),
            "larc_supporter_goal": larc_supporter_goal,
        }
    if larc_fan_plan is not None:
        state_context.setdefault("scenario_state", {})["larc_fan_plan"] = larc_fan_plan
    if larc_friend_outing is not None:
        state_context["scenario_state"] = {
            **state_context.get("scenario_state", {}), "larc_friend_outing": larc_friend_outing,
        }
    attribute_session = None
    if (state_context.get("scenario_state", {}).get("scenario") == "larc"
            and args.transition_session_json and Path(args.transition_session_json).is_file()):
        from core.auto_trainer.larc_attribute_session import load_attribute_context
        from core.auto_trainer.larc_strategy_params import unified_larc_enabled
        attribute_session = Path(args.transition_session_json).parent
        _unified_goal = unified_larc_enabled(load_goal_config(args.goal_config))
        state_context["scenario_state"].update(
            load_attribute_context(attribute_session, args.run_session_id, args.turn_index,
                first_expedition_memory=_unified_goal,
                departure18_memory=_unified_goal))
        from core.auto_trainer.larc_capacity import capacity_context
        state_context["scenario_state"]["larc_capacity"] = capacity_context(
            attribute_session, args.run_session_id, args.turn_index,
            roster_path=(args.larc_member_roster_json
                         or getattr(training_parser, "larc_roster_path", None)))
        from core.auto_trainer.run_support_profile import load_profile
        try:
            support_profile = load_profile(attribute_session.parent, args.run_session_id)
        except (OSError, ValueError):
            support_profile = None
        if support_profile is not None:
            state_context["scenario_state"]["support_card_profile"] = support_profile
            if isinstance(support_profile.get("deck_inspect"), dict):
                state_context["scenario_state"]["larc_observed_deck"] = support_profile["deck_inspect"]
    # Match the SS comparator's run-local history, including parser-owned rosters.
    larc_roster = (training_parser.get_larc_roster(args.run_session_id)
                  if state_context.get("scenario_state", {}).get("scenario") == "larc"
                  and training_parser is not None and training_parser.larc_roster_path is not None else None)
    if larc_roster is not None:
        state_context["scenario_state"]["larc_ss_execution_count"] = len(larc_roster.to_dict()["ss_executions"])
        from core.auto_trainer.larc_strategy_params import unified_larc_enabled
        if unified_larc_enabled(load_goal_config(args.goal_config)):
            from core.auto_trainer.larc_action_history import ss_wins_context
            state_context['scenario_state']['larc_ss_wins'] = ss_wins_context(
                args.run_session_id, args.turn_index,
                current=state_context['scenario_state'].get('larc_ss_wins'), roster=larc_roster.to_dict())
    # Optional asset history is projected only from the original persisted
    # roster. A confirmation is not a result, and missing caps keep B on the
    # old scoring path without extra captures, writes, or game actions.
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    strategy181_context_enabled = (state_context.get('scenario_state', {}).get('scenario') == 'larc'
        and unified_larc_enabled(load_goal_config(args.goal_config)))
    if strategy181_context_enabled:
        from core.auto_trainer.larc_strategy181_observation import observation_context
        state_context['scenario_state']['larc_strategy181_observation'] = observation_context(
            args.run_session_id, args.turn_index,
            roster=larc_roster.to_dict() if larc_roster is not None else None)
    state_context = _merge_condition_context_into_state_context(
        state_context,
        condition_context,
        mode=args.mode,
    )
    if (condition_context is not None or larc_fan_plan is not None or larc_supporter_goal is not None
            or larc_friend_outing is not None or attribute_session is not None or larc_roster is not None
            or purchase_marker_added or growth_source_bound or strategy181_context_enabled):
        _atomic_write_json(state_context_snapshot_path, state_context)
    state_context_paths = {
        "source_path": str(state_context_source_path),
        "source_sha256": _sha256_file(state_context_source_path),
        "snapshot_path": str(state_context_snapshot_path),
        "snapshot_sha256": _sha256_file(state_context_snapshot_path),
    }
    if provider_stage is not None:
        state_context_paths.update(
            {
                "provider_path": provider_stage["provider_path"],
                "source_screenshot_path": provider_stage["source_screenshot_path"],
                "source_screenshot_sha256": provider_stage["source_screenshot_sha256"],
            }
        )
    capture_report["state_context_source_path"] = state_context_paths["source_path"]
    capture_report["state_context_snapshot_path"] = state_context_paths["snapshot_path"]
    _record_state_context_paths(manifest_path, state_context_paths)
    if condition_context is not None:
        _record_condition_context(manifest_path, condition_context["metadata"])
    # This goal is already bound to this call's fresh provider frame above.
    # Only SS can save this turn; unrelated training numbers cannot veto it.
    sidecar_path = training_page_observation_path = None
    sidecar_report = {"is_valid": True, "status": "not_required_for_deadline_ss"}
    training_page_observation_report = {"is_valid": True, "status": "not_required_for_deadline_ss"}
    quality_gate: dict[str, Any] | None = None
    if not deadline_ss_only or joint_collect:
        if args.training_options_json:
            sidecar_path = Path(args.training_options_json)
            sidecar_report = {
                "is_valid": True,
                "source": "reviewed_training_options",
                "output_path": str(sidecar_path),
                "warnings": [],
                "errors": [],
            }
        else:
            sidecar_path = (
                Path(args.sidecar_output_json)
                if args.sidecar_output_json
                else execution_root / "training_options.generated.json"
            )
            sidecar_path.parent.mkdir(parents=True, exist_ok=True)
            sidecar_report = timed_call('gains_and_participants',build_training_options_sidecar,
                manifest_path=manifest_path,
                execution_root=execution_root,
                actual_group_id=args.actual_group_id,
                output_path=sidecar_path,
                parser=training_parser,
                state_context=state_context,
                overwrite=True,
                pretty=args.pretty,
            )
        if not sidecar_report.get("is_valid"):
            return _stage_error("sidecar", sidecar_report, capture_report), 2

        if args.mode == "live":
            quality_gate = _evaluate_sidecar_quality(
                sidecar_path=sidecar_path,
                sidecar_report=sidecar_report,
                min_confidence=args.min_live_sidecar_confidence,
                allow_partial=args.allow_partial_live_sidecar,
            )
            if not quality_gate["accepted"]:
                return (
                    {
                        "is_valid": False,
                        "status": "sidecar_quality_failed",
                        "quality_gate": quality_gate,
                        "stages": {
                            "capture": capture_report,
                            "sidecar": sidecar_report,
                            **({"state_context_provider": provider_stage} if provider_stage else {}),
                        },
                    },
                    2,
                )

        training_page_observation_path = (
            execution_root / "training_page_observation.generated.json"
        )
        observation_reads = []

        def remember_stats(image):
            result = read_training_current_stats(image)
            observation_reads.append((image, result))
            return result

        from core.larc_growth_reader import read_training_growth
        growth_reader = (read_training_growth
            if state_context.get("scenario_state", {}).get("scenario") == "larc" else None)
        training_page_observation_report = timed_call('displayed_stats',build_training_page_observation,
            manifest_path=manifest_path,
            execution_root=execution_root,
            actual_group_id=args.actual_group_id,
            output_path=training_page_observation_path,
            reader=remember_stats,
            growth_reader=growth_reader,
            main_menu_context=state_context,
            overwrite=True,
            pretty=args.pretty,
        )
        if (
            training_page_observation_report.get("is_valid") is not True
            or training_page_observation_report.get("consensus_complete") is not True
            or not training_page_observation_path.is_file()
        ):
            return (
                {
                    "is_valid": False,
                    "status": "training_page_observation_failed",
                    "stages": {
                        "capture": capture_report,
                        "sidecar": sidecar_report,
                        "training_page_observation": training_page_observation_report,
                        **({"state_context_provider": provider_stage} if provider_stage else {}),
                    },
                },
                2,
            )

    attribute_state = None
    if attribute_session is not None and not deadline_ss_only:
        from core.auto_trainer.capture_adapter import build_state_from_capture_artifacts
        from core.auto_trainer.larc_attribute_session import record_attribute_sample
        from core.auto_trainer.larc_strategy_params import unified_larc_enabled
        attribute_state = build_state_from_capture_artifacts(
            manifest_path=manifest_path, execution_root=execution_root,
            actual_group_id=args.actual_group_id, training_options_path=sidecar_path,
            training_page_observation_path=training_page_observation_path, state_context=state_context)
        try:
            _unified_sample = unified_larc_enabled(load_goal_config(args.goal_config))
            record_attribute_sample(attribute_session, args.run_session_id, attribute_state,
                first_expedition_memory=_unified_sample,
                departure18_memory=_unified_sample)
        except (OSError, ValueError, TypeError) as exc:
            # Forecast history is optional. Keep the normal game route moving.
            capture_report["attribute_history_note"] = str(exc)

    if joint_collect:
        from dataclasses import replace
        from core.auto_trainer.capture_adapter import build_state_from_capture_artifacts
        state = attribute_state or build_state_from_capture_artifacts(
            manifest_path=manifest_path, execution_root=execution_root,
            actual_group_id=args.actual_group_id, training_options_path=sidecar_path,
            training_page_observation_path=training_page_observation_path, state_context=state_context)
        ss_report, ss_code = _run_larc_ss_comparison(
            args, training_parser, capture_report, state, collect_only=True)
        context = dict(state.scenario_state)
        for stage in ("entry", "preview"):
            observation = _latest_ss_observation(ss_report, stage, args)
            if observation is not None:
                context[f"larc_ss_{stage}"] = observation
        state = replace(state, scenario_state=context)
        safe_stop = ss_report.get("stop_reason") in {
            "ready_larc_ss_preview", "ready_training_menu", "goal_deadline_unresolved"}
        consumed = any(step.get("result", {}).get("tap_performed") is True
            and step.get("result", {}).get("decision", {}).get("reason") == "larc_ss_confirm"
            for step in ss_report.get("step_reports", []))
        accepted = ss_code == 0 and safe_stop and ss_report.get("is_valid") is True and not consumed
        report = {"is_valid": accepted, "status": "joint_await_choice" if accepted else "joint_collection_stopped",
            "turn_consumed": consumed, "execution_authorized": False, "state": state.to_dict(),
            "stages": {"capture": capture_report, "sidecar": sidecar_report,
                       "training_page_observation": training_page_observation_report,
                       "state_context_provider": provider_stage, "larc_ss": ss_report}}
        _atomic_write_json(execution_root / "joint_collection.json", report)
        return report, 0 if report["is_valid"] else 2

    if getattr(args, "prefer_larc_ss", False):
        from core.auto_trainer.capture_adapter import build_state_from_capture_artifacts
        state = attribute_state or build_state_from_capture_artifacts(
            manifest_path=manifest_path, execution_root=execution_root,
            actual_group_id=args.actual_group_id, training_options_path=sidecar_path,
            training_page_observation_path=training_page_observation_path, state_context=state_context,
        )
        changed = False
        if larc_pre_action is not None:
            preparation = timed_call('growth_adaptation',larc_pre_action,state)
            if preparation is not None and (not preparation.get("ok") or preparation.get("requires_recollection")):
                return {"is_valid": preparation.get("ok") is True,
                        "status": "larc_growth_recollect" if preparation.get("ok") else "larc_growth_stopped",
                        "stop_reason": preparation.get("reason") if not preparation.get("ok") else None,
                        "turn_consumed": False, "growth_adaptation": preparation,
                        "stages": {"capture": capture_report}}, 0 if preparation.get("ok") else 2
            if preparation is not None:
                from core.auto_trainer.larc_joint_career.binding import attach_preparation
                if attach_preparation(state, state_context, preparation, load_goal_config(args.goal_config)):
                    changed = True
            if preparation is not None and attribute_session is not None:
                # The existing grid check may have learned this turn's balance
                # after the state was built. Share it with SS and local actions.
                from core.auto_trainer.larc_expedition_forecast import load_expedition_context
                expedition = load_expedition_context(attribute_session, args.run_session_id, args.turn_index)
                if expedition and expedition != state_context["scenario_state"].get("larc_expedition_context"):
                    state.scenario_state["larc_expedition_context"] = expedition
                    state_context["scenario_state"]["larc_expedition_context"] = expedition
                    changed = True
        from core.auto_trainer.larc_candidate_quality import enabled as quality_enabled
        if not deadline_ss_only and _can_refresh_candidate_capture(args) and quality_enabled():
            from core.auto_trainer.larc_candidate_quality import refresh_requests, row_diagnostics
            from core.auto_trainer.larc_opening import LarcOpeningStrategy
            quality_goal = load_goal_config(args.goal_config)
            local = LarcOpeningStrategy(quality_goal).evaluate(state)
            purchase = state.scenario_state.get('larc_last_training_purchase') or {}
            purchased_tab = (purchase.get('training_type') if purchase.get('turn') == state.turn else None)
            requests = refresh_requests(state, quality_goal, local.scored_candidates,
                                        purchased_training=purchased_tab, limit=2)
            if requests:
                refresh = _refresh_candidate_capture(args, capture_report, requests)
                capture_report.update(refresh)
                if not refresh.get('is_valid'):
                    return {'is_valid': False, 'status': 'larc_candidate_refresh_failed',
                            'turn_consumed': False, 'stages': {'capture': capture_report}}, 2
                # The newly committed group owns a separate context snapshot.
                # Preserve this invocation's enhancements in that generation,
                # leaving the prior capture's complete recovery scene intact.
                state_context_snapshot_path = _capture_context_path(capture_report, execution_root)
                state_context_paths["snapshot_path"] = str(state_context_snapshot_path)
                capture_report["state_context_snapshot_path"] = str(state_context_snapshot_path)
                _restore_refreshed_context(manifest_path, state_context_snapshot_path,
                    state_context, state_context_paths, condition_context=condition_context)
                sidecar_report = timed_call('candidate_refresh_parse', build_training_options_sidecar,
                    manifest_path=manifest_path, execution_root=execution_root,
                    actual_group_id=args.actual_group_id, output_path=sidecar_path,
                    parser=training_parser, state_context=state_context, overwrite=True, pretty=args.pretty)
                observation_reads.clear()
                training_page_observation_report = timed_call('candidate_refresh_observation', build_training_page_observation,
                    manifest_path=manifest_path, execution_root=execution_root,
                    actual_group_id=args.actual_group_id, output_path=training_page_observation_path,
                    reader=remember_stats, growth_reader=growth_reader,
                    main_menu_context=state_context, overwrite=True, pretty=args.pretty)
                if (not sidecar_report.get('is_valid')
                        or not training_page_observation_report.get('is_valid')
                        or not training_page_observation_report.get('consensus_complete')):
                    return {'is_valid': False, 'status': 'larc_candidate_refresh_failed',
                            'turn_consumed': False, 'stages': {'capture': capture_report,
                                'sidecar': sidecar_report, 'training_page_observation': training_page_observation_report}}, 2
                state = build_state_from_capture_artifacts(
                    manifest_path=manifest_path, execution_root=execution_root,
                    actual_group_id=args.actual_group_id, training_options_path=sidecar_path,
                    training_page_observation_path=training_page_observation_path, state_context=state_context)
                diagnostic = capture_report['candidate_quality_refresh']
                diagnostic['requests'] = requests
                diagnostic['after'] = [row_diagnostics(candidate) for candidate in state.training_options
                                       if candidate.training_type in {r['training_type'] for r in requests}]
                refreshed_decision = LarcOpeningStrategy(quality_goal).evaluate(state)
                diagnostic['recomputed_selection'] = {'action': refreshed_decision.action,
                                                     'target': refreshed_decision.target}
                from core.auto_trainer.candidate_failure_archive import archive_pipeline_refresh_failures
                diagnostic['failure_evidence'] = archive_pipeline_refresh_failures(args, execution_root, diagnostic)
                _atomic_write_json(execution_root / 'candidate_quality_refresh.json', diagnostic)
                attribute_state = state
        ss_report, ss_code = timed_call('ss_compare_and_execute',_run_larc_ss_comparison,args, training_parser, capture_report, state)
        if ss_report.get('strategy18_plan') is not None:
            state_context['scenario_state']['larc_strategy18_plan'] = ss_report['strategy18_plan']
            changed = True
        if ss_report.get('current_nonfull_entry') is not None:
            # Retain the current count for the training-side completion quote.
            # This is entry evidence only, not a held preview or permission to tap.
            from core.auto_trainer.larc_year1_engine import NONFULL_ENTRY
            state_context['scenario_state'][NONFULL_ENTRY] = ss_report['current_nonfull_entry']
            changed = True
        ss_steps = [row["result"] for row in ss_report.get("step_reports", [])]
        consumed = any(row.get("tap_performed") is True and
                       row.get("decision", {}).get("reason") == "larc_ss_confirm" for row in ss_steps)
        stages = {"capture": capture_report, "larc_ss": ss_report, "sidecar": sidecar_report,
                  "training_page_observation": training_page_observation_report,
                  "state_context_provider": provider_stage}
        if any(row.get("larc_ss_execution_receipt", {}).get("status") == "persistence_failed" for row in ss_steps):
            return {"is_valid": False, "status": "larc_ss_receipt_persistence_failed",
                    "turn_consumed": consumed, "stages": stages}, 2
        if consumed:
            if training_parser is not None:
                _record_larc_ss_execution(args, training_parser, ss_steps)
            for step in ss_steps:
                stored=step.get('larc_ss_execution_receipt')or{}
                if stored.get('status')=='persisted':
                    _record_standalone_deadline_action(args,'ss',stored.get('receipt')or{})
            return {"is_valid": True, "status": "larc_ss_executed", "stages": stages}, 0
        if ss_report.get('stop_reason')=='full_ss_deadline_unresolved':
            return {'is_valid':True,'status':'full_ss_deadline_unresolved','turn_consumed':False,
                'ss_deadline':ss_report.get('ss_deadline'),'stages':stages},0
        if ss_report.get("stop_reason") == "goal_deadline_unresolved":
            return {"is_valid": True, "status": "goal_deadline_unresolved", "turn_consumed": False,
                    "goal_deadline": {"reason": "no_executable_ss", "supporter_goal": larc_supporter_goal},
                    "stages": stages}, 0
        ready = ss_report.get("stop_reason")
        declined_preview = ready == "ready_larc_ss_preview" and ss_report.get("comparison", {}).get("selected") is False
        optional_deferred = ss_report.get("ss_optional_deferred") is True
        navigation = any(row.get("tap_performed") is True for row in ss_steps)
        if (ss_code != 0 or ss_report.get("is_valid") is not True
                or not (optional_deferred or declined_preview or (ready == "ready_training_menu" and not navigation))):
            return {"is_valid": False, "status": "larc_ss_handoff", "stages": stages}, 2
        if deadline_ss_only:
            return {"is_valid": True, "status": "goal_deadline_unresolved", "turn_consumed": False,
                    "goal_deadline": {"reason": "no_executable_ss", "supporter_goal": larc_supporter_goal},
                    "stages": stages}, 0
        if larc_fan_ss_only:
            return {"is_valid": True, "status": "larc_fan_ss_not_selected", "turn_consumed": False,
                    "stages": stages}, 0
        # The ordinary executor already switches from the SS tab through a fresh
        # precheck/target check. No new back navigation or five-page capture.
        for stage in ("entry", "preview") if declined_preview else ("entry",):
            observation = _latest_ss_observation(ss_report, stage, args)
            if observation is not None:
                state_context["scenario_state"][f"larc_ss_{stage}"] = observation
                changed = True
        if changed:
            _atomic_write_json(state_context_snapshot_path, state_context)
            state_context_paths["snapshot_sha256"] = _sha256_file(state_context_snapshot_path)
            _record_state_context_paths(manifest_path, state_context_paths)
            if (isinstance(training_page_observation_report, dict)
                    and training_page_observation_report.get("is_valid") is True
                    and training_page_observation_report.get("consensus_complete") is True
                    and state_context.get("turn") == training_page_observation_report.get("turn_index")
                    and training_page_observation_path is not None):
                training_page_observation_report["source_manifest_sha256"] = _sha256_file(manifest_path)
                serialized_obs = json.dumps(
                    training_page_observation_report,
                    ensure_ascii=False,
                    indent=2 if args.pretty else None,
                    sort_keys=True,
                    allow_nan=False,
                )
                _io(training_page_observation_path).write_text(serialized_obs + "\n", encoding="utf-8")
            else:
                cached_reads = iter(observation_reads)

                def reuse_stats(image):
                    observed_image, result = next(cached_reads)
                    if observed_image.shape != image.shape:
                        raise ValueError("training_observation_source_changed")
                    return result

                prior_growth = {tab: data.get("growth_effect_model", {}) for tab, data in
                                training_page_observation_report.get("tabs", {}).items()}
                training_page_observation_report = timed_call('displayed_stats_rebind',build_training_page_observation,
                    manifest_path=manifest_path, execution_root=execution_root,
                    actual_group_id=args.actual_group_id,
                    output_path=training_page_observation_path, reader=reuse_stats,
                    growth_reader=(lambda image, tab: prior_growth.get(tab, {})) if prior_growth else None,
                    main_menu_context=state_context,
                    overwrite=True, pretty=args.pretty,
                )
            if (training_page_observation_report.get("is_valid") is not True
                    or training_page_observation_report.get("consensus_complete") is not True):
                return {"is_valid": False, "status": "training_page_observation_failed",
                        "stages": {**stages, "training_page_observation": training_page_observation_report}}, 2

    scenario_stage: dict[str, Any] | None = None
    if args.progressive_route_session:
        session_path = Path(args.progressive_route_session)
        audit_path = Path(args.scenario_audit_out)
        _validate_progressive_scenario_paths(
            session_path=session_path,
            audit_path=audit_path,
            protected_paths={
                "manifest": manifest_path,
                "sidecar": sidecar_path,
                "training_page_observation": training_page_observation_path,
                "state_context_snapshot": state_context_snapshot_path,
                "state_context_source": state_context_source_path,
            },
        )
        session = ProgressiveRouteSession.load(session_path)
        if session.run_session_id != args.run_session_id:
            raise ValueError("progressive route session run_session_id mismatch")
        pages = _load_progressive_training_pages(
            sidecar_path,
            state_context=state_context,
            expected_frame_sha256={
                training_type: raw["sha256"]
                for training_type, raw in load_capture_group_raws(
                    manifest_path=manifest_path,
                    execution_root=execution_root,
                    actual_group_id=args.actual_group_id,
                    formal_capture=True,
                ).items()
            },
        )
        controller = ProgressiveRouteController(
            ProgressiveRouteGraph.load(
                ROOT / "config" / "progressive_route_grand_masters.json"
            ),
            session,
        )
        decision = _prepare_progressive_scenario(
            controller,
            pages,
            args.scenario_policy_id,
            audit_path,
        )
        session.save(session_path)
        scenario_stage = {
            "is_valid": True,
            "policy_id": args.scenario_policy_id,
            "action_id": decision.action_id,
            "packet_sha256": decision.packet_sha256,
            "invalidates_collection": decision.invalidates_collection,
            "audit_path": str(audit_path),
            "audit_sha256": _sha256_file(audit_path),
            "performed": False,
        }
        if decision.action_id != "scenario.hold":
            return (
                {
                    "is_valid": True,
                    "status": "scenario_recollection_required",
                    "action_id": decision.action_id,
                    "stages": {
                        "capture": capture_report,
                        "sidecar": sidecar_report,
                        "training_page_observation": training_page_observation_report,
                        "scenario": scenario_stage,
                    },
                },
                0,
            )

    structured_metadata = _build_structured_metadata(
        args=args,
        state_context=state_context,
        state_context_paths=state_context_paths,
        sidecar_path=sidecar_path,
        condition_report_path=(
            Path(args.condition_report_json)
            if args.condition_report_json
            else None
        ),
        scenario_stage=scenario_stage,
    )
    condition_report_path = (
        Path(args.condition_report_json)
        if args.condition_report_json
        else None
    )
    perception_reliability_stage = (
        _prepare_perception_reliability_artifact(
            args=args,
            manifest_path=manifest_path,
            execution_root=execution_root,
            sidecar_path=sidecar_path,
            state_context=state_context,
            state_context_snapshot_path=state_context_snapshot_path,
            condition_report_path=condition_report_path,
            scenario_stage=scenario_stage,
        )
    )
    live_loop_report, live_loop_code = timed_call('training_decide_and_execute',_invoke_json_cli,
        live_loop_main,
        _live_loop_argv(
            args=args,
            manifest_path=manifest_path,
            execution_root=execution_root,
            sidecar_path=sidecar_path,
            training_page_observation_path=training_page_observation_path,
            state_context_snapshot_path=state_context_snapshot_path,
            condition_report_path=condition_report_path,
            require_explicit_condition_probe=args.require_explicit_condition_probe,
            structured_metadata=structured_metadata,
            perception_reliability_stage=perception_reliability_stage,
        ),
        formal_condition_binding_key=formal_condition_binding_key,
        control_driver=control_driver,
        frame_safety_analyzer=getattr(args, "_safety_analyzer", None),
    )
    _record_standalone_deadline_execution(args,live_loop_report.get('execution')or{})
    if live_loop_code != 0 or not live_loop_report.get("is_valid"):
        return (
            {
                "is_valid": False,
                "status": "live_loop_failed",
                "stages": {
                    "capture": capture_report,
                    "sidecar": sidecar_report,
                    "training_page_observation": training_page_observation_report,
                    "perception_reliability": perception_reliability_stage,
                    **({"scenario": scenario_stage} if scenario_stage else {}),
                    "live_loop": live_loop_report,
                    **({"state_context_provider": provider_stage} if provider_stage else {}),
                    **({"condition_context": condition_context["metadata"]} if condition_context else {}),
                },
            },
            2,
        )

    control_stopped = live_loop_report.get("status") == "control_stopped"
    deadline_stopped = "goal_deadline_unresolved" in live_loop_report.get("decision", {}).get("risk_flags", [])
    report = {
        "is_valid": True,
        "status": "control_stopped" if control_stopped else "goal_deadline_unresolved" if deadline_stopped else "completed",
        **({"turn_consumed": False, "goal_deadline": {
            "reason": "no_executable_ss" if ss_report is not None else "missing_ss_evidence",
            "supporter_goal": larc_supporter_goal,
        }} if deadline_stopped else {}),
        "stages": {
            "capture": capture_report,
            **({"larc_ss": ss_report} if ss_report is not None else {}),
            "sidecar": sidecar_report,
            "training_page_observation": training_page_observation_report,
            "perception_reliability": perception_reliability_stage,
            **({"scenario": scenario_stage} if scenario_stage else {}),
            "live_loop": live_loop_report,
            **({"state_context_provider": provider_stage} if provider_stage else {}),
            **({"condition_context": condition_context["metadata"]} if condition_context else {}),
        },
        "artifacts": {
            "manifest_path": str(manifest_path),
            "sidecar_path": str(sidecar_path),
            "training_page_observation_path": str(training_page_observation_path),
            "run_dir": str(Path(args.run_dir)),
            "state_context_source_path": state_context_paths["source_path"],
            "state_context_source_sha256": state_context_paths["source_sha256"],
            "state_context_snapshot_path": state_context_paths["snapshot_path"],
            "state_context_snapshot_sha256": state_context_paths["snapshot_sha256"],
            **(
                {
                    "perception_reliability_path": (
                        perception_reliability_stage["path"]
                    ),
                    "perception_reliability_sha256": (
                        perception_reliability_stage["sha256"]
                    ),
                    "perception_authority_bundle_sha256": (
                        perception_reliability_stage[
                            "authority_bundle_sha256"
                        ]
                    ),
                }
                if perception_reliability_stage["status"] == "available"
                else {}
            ),
            **(
                {
                    "condition_report_path": condition_context["metadata"]["source"]["report_path"],
                    "condition_report_sha256": condition_context["metadata"]["source"]["report_sha256"],
                    "condition_provenance": condition_context["metadata"]["provenance"],
                }
                if condition_context
                else {}
            ),
            **(
                {
                    "state_context_provider_path": state_context_paths["provider_path"],
                    "source_screenshot_path": state_context_paths["source_screenshot_path"],
                    "source_screenshot_sha256": state_context_paths["source_screenshot_sha256"],
                }
                if provider_stage
                else {}
            ),
        },
    }
    if quality_gate is not None:
        report["quality_gate"] = quality_gate
    if control_stopped:
        report["control_boundary"] = live_loop_report.get("control_boundary")
    return (report, 0)


def _run_or_reuse_capture(args: argparse.Namespace) -> dict[str, Any]:
    has_existing = bool(args.capture_manifest or args.execution_root)
    if has_existing:
        if not (args.capture_manifest and args.execution_root):
            raise ValueError("existing_capture_requires_manifest_and_execution_root")
        return {
            "is_valid": True,
            "source": "existing_capture",
            "manifest_path": str(Path(args.capture_manifest)),
            "output_root": str(Path(args.execution_root)),
            "actual_group_id": args.actual_group_id,
        }

    if not args.output_root:
        raise ValueError("output_root_required_without_existing_capture")
    capture_report, capture_code = _invoke_json_cli(
        capture_live_tabs_main,
        _capture_argv(args),
    )
    if capture_code != 0:
        capture_report.setdefault("is_valid", False)
    return capture_report


def _can_refresh_candidate_capture(args):
    return (args.mode == 'live' and args.capture_live and args.capture_execute
            and args.capture_confirm_token == 'CAPTURE_UG_PARENT_LIVE_TABS'
            and bool(args.transition_session_json) and not args.capture_manifest and not args.execution_root)


def _refresh_candidate_capture(args, capture_report, requests):
    """One bounded observation round; never invokes a consuming executor."""
    if not _can_refresh_candidate_capture(args):
        return {'is_valid': False, 'errors': ['candidate_refresh_requires_current_live_capture']}
    tabs = [row['training_type'] for row in requests]
    if not 1 <= len(tabs) <= 2 or len(set(tabs)) != len(tabs):
        return {'is_valid': False, 'errors': ['candidate_refresh_bound_invalid']}
    refresh_args = copy.copy(args)
    refresh_args.start_screen = 'training-menu'
    argv = _capture_argv(refresh_args) + [
        '--refresh-tabs', *tabs, '--prior-manifest', capture_report['manifest_path'],
        '--prior-execution-root', capture_report['output_root']]
    report, code = _invoke_json_cli(capture_live_tabs_main, argv)
    if code != 0:
        report['is_valid'] = False
    report.setdefault('candidate_quality_refresh', {}).update(
        requests=requests, max_tabs=2, rounds=1, turn_consumed=False,
        consuming_actions_repeated=False)
    return report


def _inject_same_turn_purchase_marker(args, state_context):
    career_marker_added = False
    if getattr(args, '_larc_career_joint_main', None) is not None:
        from core.auto_trainer.larc_joint_career.binding import inject_settled_marker
        career_marker_added = inject_settled_marker(args._larc_career_joint_main, state_context,
            run_id=args.run_session_id, turn=args.turn_index, goal=load_goal_config(args.goal_config))
    marker = getattr(args, '_larc_last_training_purchase', None)
    scenario = state_context.get('scenario_state') or {}
    if (scenario.get('scenario') != 'larc' or not isinstance(marker, dict)
            or type(marker.get('turn')) is not int or marker['turn'] != args.turn_index
            or state_context.get('turn') != args.turn_index
            or marker.get('training_type') not in TRAINING_TYPES):
        return career_marker_added
    state_context.setdefault('scenario_state', {})['larc_last_training_purchase'] = {
        'turn': marker['turn'], 'training_type': marker['training_type']}
    return True


def _capture_context_path(capture_report, execution_root):
    root = Path(execution_root).resolve()
    value = capture_report.get("state_context_path")
    if not value:
        manifest = json.loads(_io(Path(capture_report["manifest_path"])).read_text(encoding="utf-8-sig"))
        group = next((g for g in manifest.get("groups", [])
                      if g.get("actual_group_id") == capture_report.get("actual_group_id")), {})
        value = group.get("provenance", {}).get("capture_artifacts", {}).get("state_context")
    if isinstance(value, str) and value:
        path = Path(value)
        path = (path if path.is_absolute() else root / path).resolve()
        if path.is_relative_to(root):
            return path
    return root / "state_context.json"


def _restore_refreshed_context(manifest_path, snapshot_path, state_context, paths, *, condition_context=None):
    """Keep the enhanced context authoritative after the capture builder writes."""
    _atomic_write_json(snapshot_path, state_context)
    paths['snapshot_sha256'] = _sha256_file(snapshot_path)
    _record_state_context_paths(manifest_path, paths)
    if condition_context is not None:
        _record_condition_context(manifest_path, condition_context['metadata'])


def _capture_argv(args: argparse.Namespace) -> list[str]:
    argv = [
        "--output-root",
        str(Path(args.output_root)),
        "--actual-group-id",
        args.actual_group_id,
        "--run-session-id",
        args.run_session_id,
        "--turn-index",
        str(args.turn_index),
        "--source-state-key",
        args.source_state_key,
        "--operator-id",
        args.operator_id,
        "--state-context-json",
        str(
            Path(
                args._provider_state_context_source_path
                if args.state_context_provider
                else args.state_context_json
            )
        ),
        "--start-screen",
        args.start_screen,
        "--settle-seconds",
        str(args.settle_seconds),
    ]
    if args.adb_port is not None:
        argv.extend(["--adb-port", str(args.adb_port)])
    if args.transition_session_json:
        argv.extend(["--transition-session-json", args.transition_session_json])
    if args.capture_live:
        argv.append("--live")
    if args.capture_execute:
        argv.append("--execute")
    if args.capture_confirm_token:
        argv.extend(["--confirm-token", args.capture_confirm_token])
    if args.pretty:
        argv.append("--pretty")
    return argv


def _live_loop_argv(
    *,
    args: argparse.Namespace,
    manifest_path: Path,
    execution_root: Path,
    sidecar_path: Path,
    state_context_snapshot_path: Path,
    condition_report_path: Path | None,
    require_explicit_condition_probe: bool,
    training_page_observation_path: Path | None = None,
    structured_metadata: dict[str, Any] | None = None,
    perception_reliability_stage: dict[str, Any] | None = None,
) -> list[str]:
    argv = [
        "--goal-config",
        str(Path(args.goal_config)),
        "--capture-manifest",
        str(manifest_path),
        "--execution-root",
        str(execution_root),
        "--actual-group-id",
        args.actual_group_id,
        "--training-options-json",
        str(sidecar_path),
        "--state-context-json",
        str(state_context_snapshot_path),
        "--run-dir",
        str(Path(args.run_dir)),
        "--mode",
        args.mode,
        "--max-turns",
        str(args.max_turns),
        "--post-action-skip-taps",
        str(args.post_action_skip_taps),
        "--min-decision-confidence",
        str(args.min_decision_confidence),
    ]
    if training_page_observation_path is not None:
        argv.extend(
            [
                "--training-page-observation-json",
                str(training_page_observation_path),
            ]
        )
    if args.adb_port is not None:
        argv.extend(["--adb-port", str(args.adb_port)])
    if condition_report_path is not None:
        argv.extend(["--condition-report-json", str(condition_report_path)])
    if require_explicit_condition_probe:
        argv.append("--require-explicit-condition-probe")
    if (
        isinstance(perception_reliability_stage, dict)
        and perception_reliability_stage.get("status") == "available"
        and isinstance(perception_reliability_stage.get("path"), str)
        and isinstance(perception_reliability_stage.get("authority_path"), str)
        and isinstance(perception_reliability_stage.get("frames_path"), str)
    ):
        argv.extend(
            [
                "--perception-reliability-json",
                perception_reliability_stage["path"],
                "--perception-authority-json",
                perception_reliability_stage["authority_path"],
                "--perception-frames-json",
                perception_reliability_stage["frames_path"],
                "--perception-turn-id",
                args.actual_group_id,
            ]
        )
    if args.execute_training_confirm:
        argv.append("--execute-training-confirm")
    if args.training_confirm_token:
        argv.extend(["--confirm-token", args.training_confirm_token])
    if args.allow_partial_live_sidecar:
        argv.append("--require-selected-primary-gain-reliable")
    if getattr(args, "numeric_blind_fallback", False) is True:
        argv.append("--numeric-blind-fallback")
    if args.ai_review_json:
        argv.extend(["--ai-review-json", str(Path(args.ai_review_json))])
    if args.ai_review_command:
        argv.extend(["--ai-review-command", args.ai_review_command])
        argv.extend(
            [
                "--ai-review-command-timeout-seconds",
                str(args.ai_review_command_timeout_seconds),
            ]
        )
    structured_mode = getattr(
        args,
        "structured_advisor_mode",
        "off",
    )
    if structured_mode != "off":
        argv.extend(
            ["--structured-advisor-mode", structured_mode]
        )
        for command_arg in getattr(
            args,
            "structured_advisor_command_arg",
            [],
        ):
            argv.extend(
                ["--structured-advisor-command-arg", command_arg]
            )
        argv.extend(
            [
                "--structured-advisor-model-id",
                str(args.structured_advisor_model_id),
                "--structured-advisor-invalid-policy",
                str(args.structured_advisor_invalid_policy),
                "--structured-advisor-timeout-seconds",
                str(args.structured_advisor_timeout_seconds),
                "--structured-advisor-ttl-ms",
                str(args.structured_advisor_ttl_ms),
            ]
        )
        if not isinstance(structured_metadata, dict):
            raise ValueError("structured_advisor_metadata_required")
        metadata_flags = {
            "run_session_id": "--structured-run-session-id",
            "generation": "--structured-generation",
            "global_action_turn": "--structured-global-action-turn",
            "game_period": "--structured-game-period",
            "source_frame_path": "--structured-source-frame-path",
            "source_frame_sha256": "--structured-source-frame-sha256",
            "perception_packet_sha256": (
                "--structured-perception-packet-sha256"
            ),
            "training_packet_sha256": (
                "--structured-training-packet-sha256"
            ),
            "condition_authority_sha256": (
                "--structured-condition-authority-sha256"
            ),
        }
        for key, flag in metadata_flags.items():
            value = structured_metadata.get(key)
            if value is None:
                raise ValueError(
                    f"structured_advisor_metadata_missing:{key}"
                )
            argv.extend([flag, str(value)])
    if args.pretty:
        argv.append("--pretty")
    return argv


def _validate_ai_review_source(args: argparse.Namespace) -> None:
    from core.auto_trainer.larc_ai_runtime import current_larc_ai
    runtime=current_larc_ai()
    if runtime is not None and runtime.mode!='local' and (getattr(args,'structured_advisor_mode','off')!='off'
            or args.ai_review_json or args.ai_review_command):
        raise ValueError('larc_ai_and_structured_advisor_conflict')
    if args.mode == "live":
        if args.ai_review_json:
            raise ValueError("ai_review_static_json_forbidden_in_live")
        if args.ai_review_command:
            raise ValueError("ai_review_command_not_sandboxed_for_live")
    if args.ai_review_json and args.ai_review_command:
        raise ValueError("ai_review_source_conflict")
    structured_mode = getattr(
        args,
        "structured_advisor_mode",
        "off",
    )
    structured_args = tuple(
        getattr(args, "structured_advisor_command_arg", [])
    )
    if structured_mode == "off":
        if structured_args or getattr(
            args,
            "structured_advisor_model_id",
            None,
        ):
            raise ValueError("structured_advisor_disabled_with_config")
    else:
        if not structured_args:
            raise ValueError("structured_advisor_command_required")
        if not args.structured_advisor_model_id:
            raise ValueError("structured_advisor_model_id_required")
        if args.ai_review_json or args.ai_review_command:
            raise ValueError("legacy_and_structured_advisor_conflict")
        if args.state_context_provider != "main-menu-png":
            raise ValueError(
                "structured_advisor_requires_main_menu_provider"
            )


def _build_structured_metadata(
    *,
    args: argparse.Namespace,
    state_context: dict[str, Any],
    state_context_paths: dict[str, str],
    sidecar_path: Path,
    condition_report_path: Path | None,
    scenario_stage: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if getattr(args, "structured_advisor_mode", "off") == "off":
        return None
    source_path_value = state_context_paths.get(
        "source_screenshot_path"
    )
    source_sha = state_context_paths.get(
        "source_screenshot_sha256"
    )
    if not source_path_value or not source_sha:
        raise ValueError("structured_advisor_source_frame_missing")
    source_path = Path(source_path_value)
    if _sha256_file(source_path) != source_sha:
        raise ValueError("structured_advisor_source_frame_mismatch")
    training_sha = _sha256_file(sidecar_path)
    condition_sha = (
        _sha256_file(condition_report_path)
        if condition_report_path is not None
        else state_context_paths["snapshot_sha256"]
    )
    scenario_sha = (
        scenario_stage.get("packet_sha256")
        if isinstance(scenario_stage, dict)
        else None
    )
    perception_material = {
        "source_frame_sha256": source_sha,
        "state_context_snapshot_sha256": (
            state_context_paths["snapshot_sha256"]
        ),
        "training_packet_sha256": training_sha,
        "condition_authority_sha256": condition_sha,
        "scenario_packet_sha256": scenario_sha,
    }
    perception_sha = hashlib.sha256(
        json.dumps(
            perception_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    game_period = state_context.get("game_calendar")
    if not isinstance(game_period, str) or not game_period.strip():
        game_period = f"global_turn_{args.turn_index}"
    return {
        "run_session_id": args.run_session_id,
        "generation": 1,
        "global_action_turn": args.turn_index,
        "game_period": game_period,
        "source_frame_path": str(source_path),
        "source_frame_sha256": source_sha,
        "perception_packet_sha256": perception_sha,
        "training_packet_sha256": training_sha,
        "condition_authority_sha256": condition_sha,
    }


def _prepare_perception_reliability_artifact(
    *,
    args: argparse.Namespace,
    manifest_path: Path,
    execution_root: Path,
    sidecar_path: Path,
    state_context: dict[str, Any],
    state_context_snapshot_path: Path,
    condition_report_path: Path | None,
    scenario_stage: dict[str, Any] | None,
) -> dict[str, Any]:
    """Persist same-turn reliability evidence before any decision loop."""

    try:
        raw_by_tab = load_capture_group_raws(
            manifest_path=manifest_path,
            execution_root=execution_root,
            actual_group_id=args.actual_group_id,
            formal_capture=True,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        reason = str(exc)
        legacy_missing = any(
            reason.startswith(prefix)
            for prefix in _LEGACY_FORMAL_CAPTURE_ERRORS
        )
        if args.mode == "dry-run" and legacy_missing:
            return {
                "status": "unavailable",
                "reason": "legacy_formal_capture_provenance_missing",
            }
        raise ValueError(
            f"formal_five_tab_provenance_required:{reason}"
        ) from exc

    frames = {
        tab: str(raw_by_tab[tab]["sha256"]).lower()
        for tab in TRAINING_TYPES
    }
    try:
        sidecar_bytes = _io(sidecar_path).read_bytes()
        sidecar = json.loads(sidecar_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("perception_sidecar_invalid") from exc
    if not isinstance(sidecar, dict):
        raise ValueError("perception_sidecar_invalid")
    training_sha = hashlib.sha256(sidecar_bytes).hexdigest()
    snapshot_sha = _sha256_file(state_context_snapshot_path)
    condition_sha = (
        _sha256_file(condition_report_path)
        if condition_report_path is not None
        else snapshot_sha
    )
    scenario_sha = (
        scenario_stage.get("packet_sha256")
        if isinstance(scenario_stage, dict)
        else None
    )
    if scenario_sha is not None and (
        not isinstance(scenario_sha, str)
        or len(scenario_sha) != 64
        or any(char not in "0123456789abcdef" for char in scenario_sha)
    ):
        raise ValueError("scenario_packet_sha256_invalid")
    perception_material = {
        "raw_frame_sha256_by_tab": frames,
        "state_context_snapshot_sha256": snapshot_sha,
        "training_packet_sha256": training_sha,
        "condition_authority_sha256": condition_sha,
        "scenario_packet_sha256": scenario_sha,
    }
    perception_sha = hashlib.sha256(
        json.dumps(
            perception_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    game_period = state_context.get("game_calendar")
    if not isinstance(game_period, str) or not game_period.strip():
        game_period = f"global_turn_{args.turn_index}"
    unknown_fields = [
        "goal_ledger_sha256",
        "race_plan_sha256",
    ]
    if scenario_sha is None:
        unknown_fields.append("scenario_packet_sha256")
    authority = build_turn_authority_bundle(
        run_session_id=args.run_session_id,
        generation=1,
        global_action_turn=args.turn_index,
        game_period=game_period,
        source_frame_sha256=frames["speed"],
        perception_packet_sha256=perception_sha,
        condition_authority_sha256=condition_sha,
        goal_ledger_sha256=None,
        race_plan_sha256=None,
        training_packet_sha256=training_sha,
        scenario_packet_sha256=scenario_sha,
        unknown_fields=tuple(unknown_fields),
        hazard_flags=(),
    )
    gate_registry = (
        load_trusted_gate_authorization_registry_from_workspace(ROOT)
    )
    if not gate_registry.is_production_trusted:
        raise ValueError("trusted_perception_gate_registry_unavailable")
    artifact = build_perception_reliability_artifact(
        sidecar=sidecar,
        raw_frame_sha256_by_tab=frames,
        authority=authority,
        gate_registry=gate_registry,
        turn_id=args.actual_group_id,
    )
    output_path = execution_root / "perception_reliability.json"
    artifact_sha = write_perception_reliability_artifact_exclusive(
        output_path,
        artifact,
    )
    authority_path = execution_root / "perception_authority.json"
    _io(authority_path).write_text(
        json.dumps(authority.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    frames_path = execution_root / "perception_frames.json"
    _io(frames_path).write_text(
        json.dumps(frames, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "status": "available",
        "path": str(output_path),
        "sha256": artifact_sha,
        "authority_path": str(authority_path),
        "frames_path": str(frames_path),
        "authority_bundle_sha256": authority.bundle_sha256,
        "perception_packet_sha256": perception_sha,
        "turn_authority": authority.to_dict(),
    }


def _validate_state_context_configuration(args: argparse.Namespace) -> None:
    if args.mode == "live" and args.state_context_provider != "main-menu-png":
        raise ValueError("live_requires_main_menu_state_provider")


def _advisor_source(args: argparse.Namespace) -> str:
    if getattr(args, "structured_advisor_mode", "off") != "off":
        return f"structured_{args.structured_advisor_mode}"
    if args.ai_review_command:
        return "command"
    if args.ai_review_json:
        return "static_dry_run"
    return "none"


def _read_state_context_snapshot(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(_io(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ValueError(f"state_context_snapshot_missing:{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"state_context_snapshot_invalid:{path}:{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"state_context_snapshot_root_not_object:{path}")
    return data


def _load_condition_context(
    args: argparse.Namespace,
    *,
    required_turn_root: Path,
    formal_condition_binding_key: str | None,
    expected_turn_index: int,
) -> dict[str, Any] | None:
    if args.require_explicit_condition_probe and not args.condition_report_json:
        raise ValueError("explicit_condition_probe_report_required")
    if not args.condition_report_json:
        return None
    if args.require_explicit_condition_probe and formal_condition_binding_key is None:
        raise ValueError("condition_binding_key_missing")
    return load_explicit_condition_context(
        args.condition_report_json,
        required_turn_root=required_turn_root,
        formal_live=args.mode == "live" and args.require_explicit_condition_probe,
        formal_binding_key=formal_condition_binding_key,
        expected_turn_id=args.actual_group_id,
        expected_turn_index=expected_turn_index,
    )


def _merge_condition_context_into_state_context(
    state_context: dict[str, Any],
    condition_context: dict[str, Any] | None,
    *,
    mode: str,
) -> dict[str, Any]:
    scenario_state = state_context.get("scenario_state")
    has_preexisting_condition_context = isinstance(scenario_state, dict) and (
        "conditions" in scenario_state or "condition_context" in scenario_state
    )
    if mode == "live" and has_preexisting_condition_context:
        if condition_context is None:
            raise ValueError("untrusted_preexisting_condition_context_in_live")
        state_context = dict(state_context)
        sanitized_scenario_state = dict(scenario_state)
        sanitized_scenario_state.pop("conditions", None)
        sanitized_scenario_state.pop("condition_context", None)
        state_context["scenario_state"] = sanitized_scenario_state
    if condition_context is None:
        return state_context
    return merge_explicit_conditions(state_context, condition_context)


def _prepare_provider_state_context(args: argparse.Namespace) -> dict[str, Any] | None:
    if not args.state_context_provider:
        return None
    if args.state_context_provider != "main-menu-png":
        return {"is_valid": False, "error": "state_context_provider_unknown"}
    if args.start_screen != "main-menu":
        return {
            "is_valid": False,
            "error": "state_context_provider_requires_main_menu_start_screen",
        }
    if not args.main_menu_screenshot_path:
        return {"is_valid": False, "error": "main_menu_screenshot_path_required"}
    if not args.output_root:
        return {"is_valid": False, "error": "provider_output_root_required"}
    try:
        goal_scenario = load_goal_config(args.goal_config).scenario
    except Exception as exc:
        return {
            "is_valid": False,
            "error": f"goal_config_invalid:{type(exc).__name__}: {exc}",
        }

    screenshot_path = Path(args.main_menu_screenshot_path)
    if not screenshot_path.is_file():
        return {
            "is_valid": False,
            "error": f"main_menu_screenshot_missing:{screenshot_path}",
        }
    consensus_paths = [
        Path(path) for path in args.main_menu_consensus_screenshot_path
    ]
    for consensus_path in consensus_paths:
        if not consensus_path.is_file():
            return {
                "is_valid": False,
                "error": f"main_menu_consensus_screenshot_missing:{consensus_path}",
            }
        if _same_file_or_resolved_path(screenshot_path, consensus_path):
            return {
                "is_valid": False,
                "error": "main_menu_consensus_source_not_independent",
            }
    provider_root = Path(args.output_root)
    provider_path = provider_root / "state_context.provider.json"
    source_path = provider_root / "state_context.provider.source.json"
    snapshot_path = provider_root / "state_context.json"
    source_frame_path = provider_root / "state_context.source_frame.png"
    conflict = _provider_path_conflict(
        screenshot_path,
        {
            "provider_wrapper": provider_path,
            "context_source": source_path,
            "capture_snapshot": snapshot_path,
            "source_frame_archive": source_frame_path,
        },
    )
    if conflict is not None:
        return {"is_valid": False, "error": conflict}
    for consensus_path in consensus_paths:
        conflict = _provider_path_conflict(consensus_path, {
            "provider_wrapper": provider_path,
            "context_source": source_path,
            "capture_snapshot": snapshot_path,
            "source_frame_archive": source_frame_path,
        })
        if conflict is not None:
            return {"is_valid": False, "error": conflict}
    archive_error = _archive_source_screenshot(
        screenshot_path,
        source_frame_path,
    )
    if archive_error is not None:
        return {
            "is_valid": False,
            "error": archive_error,
            "source_screenshot_path": str(screenshot_path),
            "source_screenshot_archive_path": str(source_frame_path),
        }
    try:
        cached = getattr(args, "_main_menu_provider_report", None)
        if (isinstance(cached, dict) and not consensus_paths
                and cached.get("valid_for_training") is True
                and cached.get("state_context", {}).get("turn") == args.turn_index
                and cached.get("state_context", {}).get("scenario_state", {}).get("scenario") == goal_scenario
                and cached.get("source", {}).get("path") == str(screenshot_path)
                and cached.get("source", {}).get("sha256") == _sha256_file(source_frame_path)):
            provider_report = copy.deepcopy(cached)
            provider_report["source"]["path"] = str(source_frame_path)
            _atomic_write_json(provider_path, provider_report)
        else:
            provider_report = provide_main_menu_state(
                source_frame_path,
                provider_path,
                turn_index=args.turn_index,
                scenario=goal_scenario,
                consensus_png_paths=consensus_paths,
            )
    except Exception as exc:
        return {
            "is_valid": False,
            "error": f"{type(exc).__name__}: {exc}",
            "provider_path": str(provider_path),
            "source_screenshot_path": str(source_frame_path),
        }
    conflict = _provider_path_conflict(
        source_frame_path,
        {
            "provider_wrapper": provider_path,
            "context_source": source_path,
            "capture_snapshot": snapshot_path,
        },
    )
    if conflict is not None:
        return {"is_valid": False, "error": conflict}
    wrapper, wrapper_error = _read_and_validate_provider_wrapper(
        provider_path,
        source_frame_path,
        provider_report,
    )
    if wrapper_error is not None:
        return {
            "is_valid": False,
            "error": wrapper_error,
            "provider_path": str(provider_path),
            "source_screenshot_path": str(source_frame_path),
        }
    assert wrapper is not None
    state_context = wrapper["state_context"]
    _atomic_write_json(source_path, state_context)
    args._provider_state_context_source_path = str(source_path)
    verified_source = wrapper["source"]
    return {
        "is_valid": True,
        "provider": "main-menu-png",
        "provider_path": str(provider_path),
        "state_context_source_path": str(source_path),
        "source_screenshot_path": str(verified_source["path"]),
        "source_screenshot_sha256": str(verified_source["sha256"]),
        "report": wrapper,
    }


def _archive_source_screenshot(
    source_path: Path,
    archive_path: Path,
) -> str | None:
    source = Path(source_path)
    archive = Path(archive_path)
    if source.is_symlink():
        return "source_screenshot_symlink_rejected"
    try:
        source_resolved = source.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return "source_screenshot_archive_source_invalid"
    if not source_resolved.is_file():
        return "source_screenshot_archive_source_invalid"
    try:
        source_bytes = source_resolved.read_bytes()
    except OSError:
        return "source_screenshot_archive_source_invalid"
    if not source_bytes:
        return "source_screenshot_archive_source_empty"

    archive.parent.mkdir(parents=True, exist_ok=True)
    try:
        with archive.open("xb") as handle:
            handle.write(source_bytes)
    except FileExistsError:
        try:
            if archive.read_bytes() != source_bytes:
                return "source_screenshot_archive_conflict"
        except OSError:
            return "source_screenshot_archive_invalid"
    except OSError:
        return "source_screenshot_archive_write_failed"
    return None


def _provider_path_conflict(
    screenshot_path: Path,
    destinations: dict[str, Path],
) -> str | None:
    for label, destination in destinations.items():
        if _same_file_or_resolved_path(screenshot_path, destination):
            return f"state_context_provider_path_conflict:screenshot:{label}"
    return None


def _same_file_or_resolved_path(left: Path, right: Path) -> bool:
    if left.exists() and right.exists():
        try:
            if os.path.samefile(left, right):
                return True
        except OSError:
            pass
    return os.path.normcase(str(left.resolve(strict=False))) == os.path.normcase(
        str(right.resolve(strict=False))
    )


def _validate_progressive_scenario_paths(
    *,
    session_path: Path,
    audit_path: Path,
    protected_paths: dict[str, Path],
) -> None:
    if _same_file_or_resolved_path(session_path, audit_path):
        raise ValueError("progressive scenario session and audit paths conflict")
    for label, protected in protected_paths.items():
        if _same_file_or_resolved_path(session_path, protected):
            raise ValueError(f"progressive scenario session path conflicts with {label}")
        if _same_file_or_resolved_path(audit_path, protected):
            raise ValueError(f"progressive scenario audit path conflicts with {label}")


def _read_and_validate_provider_wrapper(
    provider_path: Path,
    screenshot_path: Path,
    provider_report: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        wrapper = json.loads(provider_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None, "provider_wrapper_missing"
    except (OSError, json.JSONDecodeError):
        return None, "provider_wrapper_invalid_json"
    if not isinstance(wrapper, dict):
        return None, "provider_wrapper_root_not_object"
    if not _canonical_json_equal(provider_report, wrapper):
        return None, "state_context_provider_persisted_mismatch"
    if wrapper.get("schema_version") != MAIN_MENU_STATE_CONTEXT_SCHEMA_VERSION:
        return None, "provider_wrapper_schema_invalid"
    if wrapper.get("valid_for_training") is not True:
        return None, "provider_wrapper_not_valid_for_training"
    if not isinstance(wrapper.get("state_context"), dict):
        return None, "provider_wrapper_state_context_invalid"
    source = wrapper.get("source")
    if not isinstance(source, dict):
        return None, "provider_wrapper_source_invalid"
    persisted_source_path = source.get("path")
    if not isinstance(persisted_source_path, str) or not persisted_source_path:
        return None, "provider_wrapper_source_path_mismatch"
    if os.path.normcase(str(Path(persisted_source_path).resolve(strict=False))) != os.path.normcase(
        str(screenshot_path.resolve(strict=False))
    ):
        return None, "provider_wrapper_source_path_mismatch"
    try:
        actual_sha256 = hashlib.sha256(screenshot_path.read_bytes()).hexdigest()
    except OSError:
        return None, "provider_source_screenshot_read_failed"
    if source.get("sha256") != actual_sha256:
        return None, "provider_wrapper_source_sha256_mismatch"
    return wrapper, None


def _canonical_json_equal(left: Any, right: Any) -> bool:
    try:
        left_json = json.dumps(
            left,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        right_json = json.dumps(
            right,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return False
    return left_json == right_json


def _json_file_canonically_matches(path: Path, expected: Any) -> bool:
    try:
        actual = json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    return _canonical_json_equal(expected, actual)


def _atomic_write_json(path: Path, data: dict[str, Any], *, buffered_diagnostic=False) -> None:
    path = _io(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    try:
        with temporary:
            if buffered_diagnostic:
                encoded=None
                try:
                    encoded=json.dumps(data,ensure_ascii=False)
                    temporary.write(encoded)
                except MemoryError:
                    # Large diagnostics can retain the original streaming path.
                    # A failed UTF-8 write may already have emitted a prefix.
                    encoded=None
                    temporary.seek(0);temporary.truncate(0)
                    json.dump(data,temporary,ensure_ascii=False)
            else:json.dump(data, temporary, ensure_ascii=False)
            temporary.flush()
        # Windows readers/virus scanners can briefly deny replacement even
        # after our own temporary handle is closed. Retry publication, never
        # the game action or the serialization, and retain the old complete file.
        for delay in (0.02, 0.05, 0.1, 0.2, 0.4, None):
            try:
                os.replace(temporary.name, path)
                break
            except OSError as exc:
                if getattr(exc, "winerror", None) not in {5, 32, 33} or delay is None:
                    raise
                sleep(delay)
    finally:
        Path(temporary.name).unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(_io(path).read_bytes()).hexdigest()


def _validate_live_min_sidecar_confidence(value: float) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(
            "min_live_sidecar_confidence_must_be_finite_between_0_and_1:"
            f"{value}"
        )


def _record_state_context_paths(manifest_path: Path, paths: dict[str, str]) -> None:
    try:
        manifest = json.loads(_io(manifest_path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ValueError(f"capture_manifest_missing:{manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"capture_manifest_invalid:{manifest_path}:{exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError(f"capture_manifest_root_not_object:{manifest_path}")
    manifest["state_context"] = dict(paths)
    _atomic_write_json(manifest_path, manifest)


def _record_condition_context(manifest_path: Path, metadata: dict[str, Any]) -> None:
    try:
        manifest = json.loads(_io(manifest_path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ValueError(f"capture_manifest_missing:{manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"capture_manifest_invalid:{manifest_path}:{exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError(f"capture_manifest_root_not_object:{manifest_path}")
    manifest["condition_context"] = metadata
    _atomic_write_json(manifest_path, manifest)


def _invoke_json_cli(
    fn: Callable[[list[str]], int],
    argv: list[str],
    *,
    formal_condition_binding_key: str | None = None,
    control_driver: CooperativeRunnerDriver | None = None,
    frame_safety_analyzer: Any | None = None,
) -> tuple[dict[str, Any], int]:
    stdout = StringIO()
    with redirect_stdout(stdout):
        kwargs: dict[str, Any] = {}
        if formal_condition_binding_key is not None:
            kwargs["formal_condition_binding_key"] = formal_condition_binding_key
        if control_driver is not None:
            kwargs["control_driver"] = control_driver
        if frame_safety_analyzer is not None:
            kwargs["frame_safety_analyzer"] = frame_safety_analyzer
        code = fn(argv, **kwargs) if kwargs else fn(argv)
    raw = stdout.getvalue().strip()
    if not raw:
        return {"is_valid": False, "error": "empty_stage_stdout"}, code
    data = _parse_stage_stdout_json(raw)
    if not isinstance(data, dict):
        raise ValueError("stage_stdout_root_not_object")
    return data, code


def _parse_stage_stdout_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(raw):
            if char != "{":
                continue
            try:
                data, end = decoder.raw_decode(raw[index:])
            except json.JSONDecodeError:
                continue
            if raw[index + end :].strip() == "":
                return data
        raise


def _evaluate_sidecar_quality(
    *,
    sidecar_path: Path,
    sidecar_report: dict[str, Any],
    min_confidence: float,
    allow_partial: bool = False,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if sidecar_report.get("warnings"):
        errors.append("sidecar_warnings_present")
        warnings.extend(str(item) for item in sidecar_report.get("warnings", []))
    data = json.loads(sidecar_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        return {
            "accepted": False,
            "min_confidence": min_confidence,
            "errors": ["sidecar_root_not_object"],
            "warnings": warnings,
        }
    if data.get("warnings"):
        errors.append("sidecar_file_warnings_present")
        warnings.extend(str(item) for item in data.get("warnings", []))
    options = data.get("training_options")
    if not isinstance(options, list):
        return {
            "accepted": False,
            "min_confidence": min_confidence,
            "errors": errors + ["training_options_missing"],
            "warnings": warnings,
        }

    seen: set[str] = set()
    critical_unknown = (
        {"parse_failed", "training_type"}
        if allow_partial
        else {"parse_failed", "training_type", "stats_gain", "failure_rate"}
    )
    for item in options:
        if not isinstance(item, dict):
            errors.append("training_option_not_object")
            continue
        tab = str(item.get("training_type", ""))
        if tab not in TRAINING_TYPES:
            errors.append(f"unknown_training_type:{tab}")
            continue
        if tab in seen:
            errors.append(f"duplicate_training_type:{tab}")
        seen.add(tab)
        confidence = item.get("confidence")
        if confidence is None:
            errors.append(f"confidence_missing:{tab}")
        elif not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            errors.append(f"confidence_invalid_type:{tab}:{type(confidence).__name__}")
        else:
            confidence_value = float(confidence)
            if not math.isfinite(confidence_value):
                errors.append(f"confidence_not_finite:{tab}:{confidence_value}")
            elif not 0.0 <= confidence_value <= 1.0:
                errors.append(f"confidence_out_of_range:{tab}:{confidence_value}")
            else:
                confidence_floor = (
                    PARTIAL_LIVE_SIDECAR_MIN_CONFIDENCE if allow_partial else min_confidence
                )
                if confidence_value < confidence_floor:
                    errors.append(f"low_confidence:{tab}:{round(confidence_value, 4)}")
                elif allow_partial and confidence_value < min_confidence:
                    warnings.append(
                        f"partial_low_confidence:{tab}:{round(confidence_value, 4)}"
                    )
        unknown_fields = item.get("unknown_fields", [])
        if isinstance(unknown_fields, list):
            partial_unknown = sorted(
                str(field)
                for field in unknown_fields
                if allow_partial and str(field) in {"stats_gain", "failure_rate"}
            )
            if partial_unknown:
                warnings.append(f"partial_unknown_fields:{tab}:{','.join(partial_unknown)}")
            bad_unknown = sorted(
                str(field) for field in unknown_fields if str(field) in critical_unknown
            )
            if bad_unknown:
                errors.append(f"critical_unknown_fields:{tab}:{','.join(bad_unknown)}")
        else:
            errors.append(f"unknown_fields_not_list:{tab}")

    missing = [tab for tab in TRAINING_TYPES if tab not in seen]
    if missing:
        errors.append(f"training_options_missing_tabs:{','.join(missing)}")
    return {
        "accepted": not errors,
        "min_confidence": min_confidence,
        "allow_partial": allow_partial,
        "partial_min_confidence": PARTIAL_LIVE_SIDECAR_MIN_CONFIDENCE,
        "errors": errors,
        "warnings": warnings,
    }


def _enable_ss_deadline_comparison(args, *, joint_collect=False):
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    authorized_main=(getattr(args,'mode',None)=='live' and getattr(args,'capture_live',False)
        and getattr(args,'capture_execute',False) and getattr(args,'execute_training_confirm',False))
    if not joint_collect and authorized_main and unified_larc_enabled(load_goal_config(args.goal_config)):
        args.prefer_larc_ss=True


def _record_standalone_deadline_action(args,kind,receipt):
    # Autonomy commits its original receipt itself. An independent one-turn
    # caller needs the same history producer, after actual consumption only.
    if getattr(args,'_larc_history_owner',None)=='autonomous':return
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    if not unified_larc_enabled(load_goal_config(args.goal_config)):return
    if not getattr(args,'transition_session_json',None):return
    from core.auto_trainer.larc_action_history import record_confirmed_action
    path=Path(args.output_root)/'ss_deadline_action_receipt.json'
    _atomic_write_json(path,receipt)
    record_confirmed_action(Path(args.transition_session_json).parent,
        args.run_session_id,args.turn_index,kind,path)


def _record_standalone_deadline_execution(args,execution):
    if execution.get('performed')is not True:return
    kind={'executed':'train','executed_rest_confirmed':'rest',
        'executed_normal_outing':'outing','executed_mei_outing':'outing'}.get(execution.get('reason'))
    if kind:_record_standalone_deadline_action(args,kind,execution)


def _blocked_ss_deadline_report(deadline,report=None):
    return {**(report or {}),'is_valid':True,'stop_reason':'full_ss_deadline_unresolved',
        'turn_consumed':False,'ss_optional_deferred':False,'ss_deadline':deadline,
        'comparison':{'selected':False,'reason':'user_full_group_ss_deadline_blocked',
            'local_action':'stop','local_target':None,'ss_deadline':deadline}}


def _scanned_ss_unavailable(capture_report, supporter_goal, *, run_id=None,turn=None,full_group_only=False):
    """Use this turn's last training frame; unknown evidence keeps the probe."""
    from core.auto_trainer.larc_ss_count import read_larc_ss_count
    from core.auto_trainer.larc_supporter_goal import deadline_larc_supporter_goal
    from core.training_menu_precheck import TrainingMenuPrechecker
    import cv2

    information=capture_report.get('larc_ss_information') or {}
    availability=information.get('ss_availability') or {}
    source=availability.get('source') or {}
    if (run_id is not None and turn is not None and information.get('status')=='completed'
            and information.get('reason')=='ss_not_unlocked' and information.get('returned_to_training') is True
            and information.get('turn_consumed') is False and availability.get('status')=='locked'
            and source.get('run_session_id')==run_id and source.get('turn_index')==turn):
        return {'source':'current_information_entry','availability':availability,'reason':'ss_not_unlocked'}

    raw_dir = capture_report.get("raw_720_dir")
    if not raw_dir or capture_report.get("is_valid") is not True:
        return None
    path = Path(raw_dir) / "wisdom.png"
    if not path.is_file():
        return None
    check = TrainingMenuPrechecker(scenario="larc").check(path)
    if not check.accepted or check.selected_tab != "wisdom":
        return None
    image = cv2.imread(str(path))
    count = read_larc_ss_count(image, stage="entry")
    # Balanced policies retain partial groups; the current count can avoid an
    # unnecessary preview. Historical and explicit trial policies keep theirs.
    minimum = 5 if full_group_only else 1 if deadline_larc_supporter_goal(supporter_goal) else 3
    if count["status"] == "accepted" and count["count"] < minimum:
        return {"source_path": str(path), "count": count, "preview_minimum": minimum}
    return None


def _run_larc_ss_comparison(args, training_parser, capture_report, state, *, collect_only=False):
    from contextlib import ExitStack
    with ExitStack() as resources:
        report, code = _run_larc_ss_comparison_owned(args,training_parser,capture_report,state,
            collect_only=collect_only,resources=resources)
    plan = state.scenario_state.get('larc_strategy18_plan')
    if plan is not None:
        report['strategy18_plan'] = plan
    return report, code


def _bind_larc_strategy18_growth(args, state_context, provider_stage):
    """Bind the existing current-main AP producer; its frame differs from tabs."""
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    if not unified_larc_enabled(load_goal_config(args.goal_config)):
        return False
    context = state_context.get('scenario_state') or {}
    growth = context.get('larc_growth_observation') or {}
    if (not isinstance(provider_stage, dict) or provider_stage.get('is_valid') is not True
            or state_context.get('turn') != args.turn_index
            or context.get('scenario') != 'larc' or growth.get('status') != 'accepted'
            or growth.get('turn_index') != args.turn_index
            or not growth.get('source_frame_sha256')
            or growth['source_frame_sha256'] != provider_stage.get('source_screenshot_sha256')):
        return False
    context['larc_growth_observation'] = {**growth, 'run_session_id': args.run_session_id,
        'source': {'run_session_id': args.run_session_id, 'turn_index': args.turn_index,
                   'sha256': growth['source_frame_sha256'],
                   'path': provider_stage.get('source_screenshot_path'),
                   'source_kind': 'accepted_main_menu_state_provider'}}
    return True


def _strategy18_preview_facts(preview):
    """Compare current offer contents independently of recapture image paths."""
    def participant_facts(row):
        result = {k: row.get(k) for k in ('slot', 'current_reward', 'win_guaranteed', 'member_ref')}
        identity = result['member_ref']
        if isinstance(identity, dict):
            # Re-observing the same member changes association diagnostics
            # (new/matched reason, confidence), not the offer's identity. Keep
            # the actual binding and acceptance fields in this projection.
            result['member_ref'] = {k: identity.get(k) for k in (
                'status', 'run_session_id', 'member_id', 'identity_scope')}
        return result
    def facts(value):
        if isinstance(value, dict):
            return {k: facts(v) for k, v in value.items()
                    if k not in {'source','comparison','planning','absolute_path','path','sha256','source_sha256'}}
        if isinstance(value, (list, tuple)):
            return [facts(v) for v in value]
        return value
    observation = preview.get('participants_observation') or {}
    return facts({k: preview.get(k) for k in ('status', 'stage', 'kind', 'ready_count', 'stats_gain')} | {
        'participants': [participant_facts(row) for row in observation.get('participants', [])],
        'win_rates': observation.get('win_rates'), 'win_rates_status': observation.get('win_rates_status')})


def _apply_strategy18_ss_comparison(state, goal, preview, comparison):
    """One joint choice at the accepted preview boundary, before any SS input."""
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    from core.auto_trainer.larc_strategy_versions import (
        policy_version_for_revision, strategy_revision)
    if not unified_larc_enabled(goal):
        return comparison
    from core.auto_trainer import larc_departure_planner as production
    from core.auto_trainer.larc_opening import LarcOpeningStrategy
    baseline = timed_call('ss_joint_baseline', LarcOpeningStrategy(goal).evaluate_baseline, state) if production.in_scope(state, goal) else None
    report = timed_call('ss_joint_planning', production.evaluate, state, goal, baseline=baseline, ss_preview=preview, ss_comparison=comparison)
    comparison = {**comparison, 'policy_version': policy_version_for_revision(strategy_revision(goal))}
    bound = production.bind_plan(state, report)
    if 'larc_strategy18_plan' in bound.scenario_state:
        state.scenario_state['larc_strategy18_plan'] = bound.scenario_state['larc_strategy18_plan']
    planning = {**(comparison.get('planning') or {}), 'departure18': report}
    if not report.get('active'):
        return {**comparison, 'planning': planning}
    advice = report.get('advice') or {}
    action_id = advice.get('action_id') if isinstance(advice, dict) else advice
    if action_id == 'ss.full':
        return {**comparison, 'selected': True, 'reason': 'strategy18_joint_route', 'planning': planning}
    if isinstance(action_id, str) and action_id.startswith('train:'):
        action, target = 'train', action_id.split(':', 1)[1]
    elif action_id == 'rest.normal':
        action, target = 'rest', None
    elif isinstance(action_id, str) and action_id.startswith('outing.satake_mei.'):
        action, target = 'outing', 'satake_mei:' + action_id.rsplit('.', 1)[1]
    else:
        return {**comparison, 'planning': planning}
    return {**comparison, 'selected': False, 'reason': 'strategy18_joint_route',
        'local_action': action, 'local_target': target, 'training_candidates': baseline.scored_candidates,
        'planning': planning}


def _run_larc_ss_comparison_owned(args, training_parser, capture_report, state, *, collect_only,resources):
    from dataclasses import replace
    from core.auto_trainer.larc_ss_choice import choose_larc_ss
    from scripts.ug_parent_safe_advance import parse_args as ss_args, run as run_ss

    goal = load_goal_config(args.goal_config)
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    from core.auto_trainer.larc_ss_deadline import deadline_status
    from core.auto_trainer.larc_ss_deadline_execution import execution_context,register_bound_preview,requires_ss_observation
    roster = (training_parser.get_larc_roster(args.run_session_id)
              if training_parser is not None and training_parser.larc_roster_path is not None else None)
    history = roster.to_dict()["ss_executions"] if roster is not None else None
    root = Path(capture_report["output_root"])
    strategy18 = unified_larc_enabled(goal)
    shared_context = state.scenario_state
    cached_comparison = cached_preview_facts = None
    def record_comparison(preview):
        nonlocal cached_comparison, cached_preview_facts
        preceding_deadline_history=state.scenario_state.get('larc_ss_deadline_history')or{}
        if strategy18:
            # The original safe-advance roster binder verified this exact image.
            # Normalize its existing source spelling; do not invent a hash.
            source=(preview.get('participants_observation')or{}).get('source')or{}
            if not preview.get('source') and source.get('run_session_id')==args.run_session_id and source.get('turn_index')==args.turn_index:
                preview={**preview,'source':{**source,'path':source.get('absolute_path'),
                    'sha256':source.get('source_sha256')}}
        if strategy18 and cached_comparison is not None:
            # The executor independently validates the fresh confirming frame.
            # Re-reading a preview cannot rerank or consume the held SS twice.
            register_bound_preview(preview)
            if _strategy18_preview_facts(preview) != cached_preview_facts:
                plan = shared_context.get('larc_strategy18_plan')
                if isinstance(plan, dict):
                    shared_context['larc_strategy18_plan'] = {
                        **plan, 'active': False, 'reason': 'confirming_ss_offer_changed_original_flow_kept'}
                return {**cached_comparison, 'selected': False,
                        'planning': {**(cached_comparison.get('planning') or {}),
                                     'departure18': shared_context.get('larc_strategy18_plan')},
                        'reason': 'confirming_ss_offer_changed_original_flow_kept'}
            return cached_comparison
        if strategy18 and getattr(args, 'transition_session_json', None):
            from core.auto_trainer.larc_action_history import record_ss_offer, ss_cycle_history,ss_deadline_history
            try:
                session = Path(args.transition_session_json).parent
                record_ss_offer(session, args.run_session_id, args.turn_index, state, preview)
                state.scenario_state['larc_ss_cycle_history'] = ss_cycle_history(
                    session, args.run_session_id, args.turn_index)
                if strategy18:
                    projected=ss_deadline_history(session,args.run_session_id,args.turn_index)
                    # Saving today's first offer is not proof that yesterday's
                    # absent main-action prefix suddenly became complete.
                    if preceding_deadline_history.get('status')!='accepted':
                        projected={**projected,'status':'unknown',
                            'reason':preceding_deadline_history.get('reason','preceding_main_history_unavailable')}
                    state.scenario_state['larc_ss_deadline_history']=projected
            except (OSError, ValueError, TypeError, KeyError) as error:
                state.scenario_state['larc_strategy_history_note'] = type(error).__name__
        register_bound_preview(preview)
        if strategy18:
            state.scenario_state['_larc_strategy18_ss_comparison_pending'] = True
        try:
            decision=timed_call('ss_local_comparison',choose_larc_ss,state,goal,preview,ss_executions=history)
        finally:
            state.scenario_state.pop('_larc_strategy18_ss_comparison_pending', None)
        if strategy18 and not decision.get('year1_b197'):
            if requires_ss_observation(decision.get('ss_deadline') or {}):
                # The ordinary SS owner below resolves this deadline from the
                # current preview. A training/outing quote cannot change it.
                state.scenario_state.pop('larc_strategy18_plan', None)
                shared_context.pop('larc_strategy18_plan', None)
            else:
                decision = _apply_strategy18_ss_comparison(state, goal, preview, decision)
        from core.auto_trainer.larc_ai_runtime import apply_ss_ai
        local_choice = (decision.get('selected'), decision.get('local_action'), decision.get('local_target'))
        decision=apply_ss_ai(state,goal,preview,decision,
            require_primary_gain=getattr(args,'require_selected_primary_gain_reliable',False))
        if strategy18:
            if local_choice != (decision.get('selected'), decision.get('local_action'), decision.get('local_target')):
                plan = state.scenario_state.get('larc_strategy18_plan')
                if isinstance(plan, dict):
                    state.scenario_state['larc_strategy18_plan'] = {
                        **plan, 'active': False, 'reason': 'explicit_ai_or_user_choice_priority'}
                    decision = {**decision, 'planning': {**(decision.get('planning') or {}),
                        'departure18': state.scenario_state['larc_strategy18_plan']}}
            if 'larc_strategy18_plan' in state.scenario_state:
                shared_context['larc_strategy18_plan'] = state.scenario_state['larc_strategy18_plan']
            cached_comparison = decision
            cached_preview_facts = _strategy18_preview_facts(preview)
        _atomic_write_json(root/'ss_comparison_snapshot.json',{
            'schema_version':'larc_ss_comparison_snapshot.v1','state':state.to_dict(),
            'preview':preview,'ss_executions':history,'goal':goal.to_dict(),
            'decision':decision,'tap_or_purchase_authorized':False})
        return decision
    kwargs = ({"larc_member_roster": roster, "larc_member_roster_path": training_parser.larc_roster_path,
               "larc_observed_turn": args.turn_index} if roster is not None else {})
    kwargs["larc_supporter_goal"] = state.scenario_state.get("larc_supporter_goal")
    kwargs["larc_ss_preview_decider"] = (
        (lambda preview: {"selected": False, "reason": "joint_waiting_for_choice"}) if collect_only
        else record_comparison)
    from core.auto_trainer.larc_policy import training_balance_v2_enabled
    full_group_only = not collect_only and training_balance_v2_enabled(goal)
    unavailable = _scanned_ss_unavailable(capture_report, kwargs["larc_supporter_goal"],
        run_id=args.run_session_id,turn=args.turn_index,full_group_only=full_group_only)
    if unavailable is not None:
        # This existing scan is a fresh explicit nonfull/locked observation,
        # not an unknown preview. It cannot authorize a small SS or invent five.
        count=unavailable.get('count')or{}
        scanned_count=count.get('count',0)
        if strategy18 and not collect_only:
            # A fresh explicit nonfull/locked scan owns a real no-SS pool.
            # An absent or failed preview never creates such an observation.
            accepted_source = (unavailable.get('availability') or {}).get('source') or {}
            source_path = unavailable.get('source_path') or accepted_source.get('path', accepted_source.get('absolute_path'))
            entry = {'status': 'accepted', 'stage': 'entry', 'ready_count': scanned_count,
                'source': {'run_session_id': args.run_session_id, 'turn_index': args.turn_index,
                           'path': source_path, 'sha256': accepted_source.get('sha256', accepted_source.get('source_sha256'))}}
            if source_path and Path(source_path).is_file():
                entry['source']['sha256'] = _sha256_file(Path(source_path))
            state.scenario_state['larc_ss_entry'] = entry
            _apply_strategy18_ss_comparison(state, goal, entry, {'selected': False})
        pending=deadline_status(state,goal,{'status':'accepted','stage':'entry','ready_count':scanned_count,
            'source':{'run_session_id':args.run_session_id,'turn_index':args.turn_index}})
        if not collect_only and requires_ss_observation(pending):
            return _blocked_ss_deadline_report(pending),0
        # No SS candidate exists: the ordinary decision stage below scores the
        # five training pages and recovery choices once. Do not run that same
        # expensive policy here just to discard it with an unavailable preview.
        report = {"is_valid": True, "stop_reason": "ready_training_menu", "step_reports": [],
                  "scan_reuse": unavailable, "baseline_deferred_to_decision": not collect_only}
        period=state.scenario_state.get('larc_period')or{}
        if (strategy18 and goal.larc_policy.get('revision') in ('1.9.8', '1.9.9', '1.9.10', '1.9.11', '1.9.12', '2.0', '2.1', '2.2') and not collect_only
                and period.get('status') in ('accepted','verified')
                and (period.get('value')or{}).get('year_index') == 1
                and count.get('status') == 'accepted' and type(count.get('count')) is int
                and 0 <= scanned_count < 5 and entry['source']['sha256']):
            report['current_nonfull_entry'] = entry
        if full_group_only:
            report["comparison"] = {"selected": False, "reason": "wait_for_full_ss_group"}
            if deadline_larc_supporter_goal(kwargs["larc_supporter_goal"]):
                report["comparison"]["deadline_resolution"] = {
                    "status": "unreachable", "reason": "no_full_group",
                    "ready_count": (unavailable.get("count") or {}).get("count", 0),
                    "points_if_all_win": None}
        _atomic_write_json(root / "larc_ss" / "report.json", report)
        return report, 0
    baseline = (kwargs["larc_ss_preview_decider"]({}) if collect_only else
                choose_larc_ss(state, goal, {}, ss_executions=history, preflight_only=True))
    if (not collect_only and baseline.get("status") != "eligible"
            and baseline.get("local_action") not in {"train", "rest", "outing"}
            and baseline.get("deadline_ss_only") is not True):
        report = {"is_valid": True, "stop_reason": "local_training_gate_stopped", "step_reports": [],
                  "comparison": baseline}
        _atomic_write_json(root / "larc_ss" / "report.json", report)
        return report, 0

    from core.execution_frames import ExecutionFrames
    from core.native_capture_adb import adb_for_capture
    supplied_adb=getattr(args,'_capture_adb',None)
    shared_frames=ExecutionFrames(supplied_adb or adb_for_capture(args.adb_port))
    if supplied_adb is None:resources.callback(shared_frames.close)

    def run_phase(name, *, preview_only):
        # First entry can include two tutorial pages before the preview.
        # The preview-only phase still cannot confirm/consume an SS action.
        argv = ["--mode", "live", "--scenario", "larc", "--max-steps", "6" if preview_only else "3",
                "--screenshot-dir", str(root / name), "--adb-port", str(args.adb_port),
                "--run-session-id", args.run_session_id,
                "--transition-session-json", args.transition_session_json,
                "--skip-session-json", args.skip_session_json,
                "--execute", "--confirm-token", "UG_PARENT_SAFE_ADVANCE_SANDBOX"]
        if not preview_only:
            argv.append("--allow-larc-ss")
        phase_args = ss_args(argv)
        phase_args._capture_adb=shared_frames
        if getattr(args, "_safety_analyzer", None) is not None:
            phase_args._safety_analyzer = args._safety_analyzer
        phase_kwargs = dict(kwargs)
        if preview_only and not collect_only:
            # The capture phase cannot confirm SS. Wait for its entry/roster
            # observations before running the expensive comparison once below.
            phase_kwargs["larc_ss_preview_decider"] = lambda preview: {
                "selected": False, "reason": "comparison_deferred_until_capture_complete"}
        with execution_context(state,goal):
            report, code = run_ss(phase_args, larc_ss_preview_only=preview_only, **phase_kwargs)
        _atomic_write_json(root / name / "report.json", report)
        return report, code

    report, code = run_phase("larc_ss_probe", preview_only=True)
    if collect_only:
        # Return immediately: never run the second (confirming) phase, regardless
        # of any existing automatic comparison or primary-action preference.
        return report, code
    if _optional_ss_observation_failure(report):
        pending=deadline_status(state,goal)
        if requires_ss_observation(pending):
            return _blocked_ss_deadline_report(pending,report),0
        # No SS turn was consumed. Drop only this optional candidate; the
        # ordinary executor still reads its current target/failure-risk UI.
        report = {**report, "is_valid": True, "ss_optional_deferred": True,
            "probe_stop_reason": report.get("stop_reason"),
            "stop_reason": "ss_observation_deferred",
            "comparison": {"selected": False, "reason": "ss_observation_unavailable"}}
        _atomic_write_json(root / "larc_ss" / "report.json", report)
        return report, 0
    if code == 0 and report.get("is_valid") is True and report.get("stop_reason") == "ready_larc_ss_preview":
        entry = _latest_ss_observation(report, "entry", args)
        preview = _latest_ss_observation(report, "preview", args)
        if entry is not None or preview is not None:
            context = dict(state.scenario_state)
            if entry is not None:
                context["larc_ss_entry"] = entry
            if preview is not None:
                context["larc_ss_preview"] = preview
            state = replace(state, scenario_state=context)
        # Score only after this capture has supplied all available evidence.
        # The confirming phase still reads and scores its own current preview.
        for step in reversed(report.get("step_reports", [])):
            ss = step.get("result", {}).get("decision", {}).get("evidence", {}).get("larc_ss", {})
            if ss.get("status") == "accepted" and ss.get("stage") == "preview":
                ss["comparison"] = kwargs["larc_ss_preview_decider"](preview or ss)
                break
    comparison = _last_ss_comparison(report)
    if (code == 0 and report.get("is_valid") is True
            and report.get("stop_reason") == "ready_larc_ss_preview"
            and comparison is not None and comparison.get("selected") is True):
        probe = report
        report, code = run_phase("larc_ss", preview_only=False)
        report = {**report, "probe": probe,
                  "step_reports": probe.get("step_reports", []) + report.get("step_reports", [])}
        comparison = _last_ss_comparison(report)
    if comparison is not None:
        report["comparison"] = comparison
        pending=comparison.get('ss_deadline')or{}
        if requires_ss_observation(pending)and comparison.get('selected')is not True:
            return _blocked_ss_deadline_report(pending,report),0
    report["training_baseline"] = baseline
    if _optional_ss_observation_failure(report):
        pending=deadline_status(state,goal)
        if requires_ss_observation(pending):
            return _blocked_ss_deadline_report(pending,report),0
        # The second, fresh preview may abstain too. The same no-consuming-
        # input rule applies here; do not strand the normal training branch.
        report = {**report, "is_valid": True, "ss_optional_deferred": True,
            "probe_stop_reason": report.get("stop_reason"),
            "stop_reason": "ss_observation_deferred",
            "comparison": {"selected": False, "reason": "ss_observation_unavailable"}}
        code = 0
    _atomic_write_json(root / "larc_ss" / "report.json", report)
    return report, code


def _optional_ss_observation_failure(report):
    """A failed optional read is not failure of the entire career."""
    reason = str(report.get("stop_reason", "")).lower()
    if reason in {"ready_larc_ss_preview", "ready_training_menu", "goal_deadline_unresolved"}:
        return False
    if not (reason.startswith(("larc_ss_", "career_goal_")) or reason in {
            "route_state_out_of_scope", "route_action_out_of_scope", "transition_contract_missing",
            "common_career_page_unrecognized", "bottom_skip_state_unconfirmed", "max_steps_reached"}):
        return False
    if any(word in reason for word in ("run_session", "run_mismatch", "device", "receipt", "source_sha")):
        return False
    for row in report.get("step_reports", []):
        result = row.get("result", {})
        decision = result.get("decision", {})
        tap = decision.get("tap") if isinstance(decision.get("tap"), dict) else {}
        if (result.get("input_may_have_occurred") or result.get("tap_outcome") in {"unknown", "failed", "pending"}
                or decision.get("reason") == "larc_ss_confirm"
                or tap.get("coordinate_name") == "safe_advance.larc_ss_confirm"
                or result.get("frame_safety", {}).get("dangerous")
                or decision.get("evidence", {}).get("screen_text", {}).get("dangerous")):
            return False
    return True


def _last_ss_comparison(report):
    for step in reversed(report.get("step_reports", [])):
        ss = step.get("result", {}).get("decision", {}).get("evidence", {}).get("larc_ss", {})
        if isinstance(ss.get("comparison"), dict):
            return ss["comparison"]
    return None


def _latest_ss_observation(report, stage, args):
    for step in reversed(report.get("step_reports", [])):
        result = step.get("result", {})
        observation = result.get("decision", {}).get("evidence", {}).get("larc_ss", {})
        if (observation.get("status") != "accepted" or observation.get("stage") != stage
                or type(observation.get("ready_count")) is not int
                or not 0 <= observation["ready_count"] <= 5):
            continue
        path = step.get("screenshot_path")
        sha = result.get("frame_safety", {}).get("source_image_sha256")
        if isinstance(path, str) and path and isinstance(sha, str) and sha:
            return {**observation, "source": {"run_session_id": args.run_session_id,
                    "turn_index": args.turn_index, "path": path, "sha256": sha}}
        break
    return None


def _is_larc_overseas_expedition_period(args) -> bool:
    provider = getattr(args, "_main_menu_provider_report", None)
    if not isinstance(provider, dict):
        return False
    lp = provider.get("larc_period")
    if not isinstance(lp, dict):
        lp = ((provider.get("state_context") or {}).get("scenario_state") or {}).get("larc_period")
    if not isinstance(lp, dict):
        return False
    if "status" in lp and lp.get("status") != "verified":
        return False
    val = lp.get("value") if isinstance(lp.get("value"), dict) else lp
    year_index = val.get("year_index")
    month = val.get("month")
    half = val.get("half")
    return (
        (year_index == 2 and ((month == 7 and half == "second") or month in (8, 9)))
        or (year_index == 3 and month in (7, 8, 9))
    )


def _collect_larc_ss_information(args, training_parser, capture_report):
    from core.native_capture_adb import adb_for_capture
    from core.auto_trainer.larc_ss_information_probe import LarcSSInformationProbe, PROBE_CONFIRM_TOKEN

    roster = training_parser.get_larc_roster(args.run_session_id)
    initialize_only = (getattr(args, "initialize_larc_reward_cycles", False)
                       and not args.collect_larc_ss_information)
    if initialize_only and roster.reward_cycles_initialized:
        return {"status": "cached", "reason": "reward_cycle_initialization_already_attempted"}
    if not roster.needs_ss_information:
        return {"status": "cached", "reason": "no_observed_ss_execution_since_scan"}
    if _is_larc_overseas_expedition_period(args):
        # Overseas expedition replaces the training-screen SS information button
        # with overseas adaptation points and disables SS matches. Defer cycle
        # initialization until back in Japan without failing mid-career takeover.
        return {
            "status": "optional_unavailable",
            "reason": "overseas_expedition_no_ss_information",
            "returned_to_training": True,
            "turn_consumed": False,
            "turn_consuming_action_executed": False,
        }
    result = LarcSSInformationProbe(adb=adb_for_capture(args.adb_port)).run(
        run_session_id=args.run_session_id, turn_index=args.turn_index,
        roster=roster, roster_path=training_parser.larc_roster_path,
        transition_session_path=args.transition_session_json,
        output_dir=Path(capture_report["output_root"]) / "ss_information",
        execute=True, confirm_token=PROBE_CONFIRM_TOKEN,
    )
    safely_returned = (result.get("returned_to_training") is True
                       and result.get("turn_consumed") is False
                       and result.get("turn_consuming_action_executed") is False)
    if (initialize_only and result.get("reason") != "ss_not_unlocked"
            and result.get("reason") != "information_open_deferred" and safely_returned):
        roster.mark_cycle_initialization_attempted()
        roster.save(training_parser.larc_roster_path)
    if safely_returned and result["status"] != "completed":
        # Reward-cycle information improves ranking but is not needed to keep
        # playing. Retain the original scan result and unknown/stale rewards;
        # the ordinary training/SS preview still supplies actionable state.
        # A verified same-turn return permits continuation, not a fake scan.
        return {**result, "probe_status": result["status"], "status": "optional_unavailable"}
    return result


def _record_larc_ss_execution(args, training_parser, steps):
    for step in steps:
        decision = step.get("decision", {})
        if step.get("tap_performed") is True and decision.get("reason") == "larc_ss_confirm":
            training_parser.record_larc_ss_execution({
                "run_session_id": args.run_session_id, "turn_index": args.turn_index,
                "action_id": decision.get("tap", {}).get("coordinate_name"),
                "tap_performed": True,
                "source_sha256": step.get("frame_safety", {}).get("source_image_sha256"),
            })


def _stage_error(
    stage: str,
    report: dict[str, Any],
    capture_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stages: dict[str, Any] = {}
    if capture_report is not None:
        stages["capture"] = capture_report
    stages[stage] = report
    return {
        "is_valid": False,
        "status": f"{stage}_failed",
        "stages": stages,
    }


def _print(data: dict[str, Any], *, indent: int | None, code: int) -> int:
    print(json.dumps(data, ensure_ascii=False, indent=indent))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
