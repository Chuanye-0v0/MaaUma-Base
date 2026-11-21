from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.auto_trainer.route_handoff import is_route_handoff_result
from core.auto_trainer.event_choice_route import EventChoiceExecutor, EventChoicePolicy
from core.auto_trainer.dashboard_runner import CooperativeRunnerDriver
from core.auto_trainer.route_transition_session import (
    POST_ACTION_ROUTE_SUCCESSORS,
    RouteTransitionError,
    RouteTransitionSession,
    record_external_action_receipt,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely choose a recognized Uma event option."
    )
    parser.add_argument("--mode", choices=["dry-run", "live"], default="dry-run")
    parser.add_argument("--max-steps", type=int, default=1)
    parser.add_argument("--screenshot-dir", required=True)
    parser.add_argument("--input-image")
    parser.add_argument("--adb-port", type=int)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-token")
    parser.add_argument("--allow-choose-event", action="store_true")
    parser.add_argument("--choice", choices=["top", "second", "bottom"], default="top")
    parser.add_argument("--formal-run-session-id")
    parser.add_argument("--formal-generation", type=int)
    parser.add_argument("--formal-global-action-turn", type=int)
    parser.add_argument("--formal-game-period")
    parser.add_argument("--transition-session-json")
    parser.add_argument("--transition-run-session-id")
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
    expected_event: tuple[str, ...] | None = None,
) -> tuple[dict[str, Any], int]:
    if args.max_steps <= 0:
        raise ValueError("max_steps_must_be_positive")
    if expected_event is not None and args.max_steps != 1:
        raise ValueError("bound_event_requires_one_step")
    if args.input_image and args.mode != "dry-run":
        raise ValueError("input_image_requires_dry_run")
    if args.input_image and args.max_steps != 1:
        raise ValueError("input_image_requires_max_steps_1")
    transition_session_json = getattr(args, "transition_session_json", None)
    transition_run_session_id = getattr(args, "transition_run_session_id", None)
    if (transition_session_json is None) != (transition_run_session_id is None):
        raise ValueError("transition_session_context_incomplete")
    transition_context_enabled = (
        transition_session_json is not None
        and transition_run_session_id is not None
    )
    if args.mode == "live" and args.execute and not transition_context_enabled:
        return {
            "is_valid": False,
            "status": "stopped",
            "performed_steps": 0,
            "requested_steps": args.max_steps,
            "stop_reason": "event_route_transition_context_required",
            "step_reports": [],
        }, 2

    policy = EventChoicePolicy(
        allow_choose_event=bool(args.allow_choose_event),
        choice=str(args.choice),
        expected_event=expected_event,
    )
    screenshot_dir = Path(args.screenshot_dir)
    capture_adb = getattr(args, "_capture_adb", None)
    initial_frame = getattr(args, "_initial_frame", None)
    if hasattr(args, "_initial_frame"):
        del args._initial_frame
    executor = EventChoiceExecutor(adb_factory=(lambda: capture_adb) if capture_adb is not None else _adb_factory(args.adb_port),
        **({"safety_analyzer": args._safety_analyzer} if getattr(args, "_safety_analyzer", None) is not None else {}))
    step_reports: list[dict[str, Any]] = []
    performed = 0
    last_persisted_receipt: dict[str, Any] | None = None
    receipt_failure_reason: str | None = None
    stop_reason = "max_steps_reached"
    formal_capture: dict[str, object] | None = None

    def transition_preflight(source: dict[str, Any]) -> None:
        action_id = source.get("action_id")
        if action_id not in {"event_choice.top", "event_choice.second", "event_choice.third", "event_choice.fourth", "event_choice.bottom", "event_choice.dialogue_next"}:
            raise RouteTransitionError("event_route_receipt_action_invalid")
        if source.get("action_state") != "event_choice" or source.get("coordinate_name") != action_id:
            raise RouteTransitionError("event_route_receipt_source_invalid")
        persisted = RouteTransitionSession.load(
            transition_session_json, expected_run_session_id=transition_run_session_id)
        candidate = copy.deepcopy(persisted)
        candidate.observe("event_choice", source_sha256=source["source_sha256"])
        candidate.commit_action(action_id, expected_next_states=POST_ACTION_ROUTE_SUCCESSORS,
            source_sha256=source["source_sha256"])

    for step in range(1, args.max_steps + 1):
        screenshot_path = (
            Path(args.input_image)
            if args.input_image
            else screenshot_dir / f"event_choice_step_{step:03d}.png"
        )
        result = executor.run_once(
            mode=args.mode,
            execute=args.execute,
            confirm_token=args.confirm_token,
            policy=policy,
            screenshot_path=screenshot_path,
            control_driver=control_driver,
            transition_preflight=transition_preflight if transition_context_enabled else None,
            **({"choice_selector": args._choice_selector} if getattr(args, "_choice_selector", None) else {}),
            **({"prior_observation": args._prior_observation} if step == 1 and getattr(args, "_prior_observation", None) else {}),
            **({"initial_frame": initial_frame} if step == 1 and initial_frame is not None else {}),
        )
        step_reports.append({"step": step, "result": result})
        if step == 1:
            formal_capture = _formal_capture_from_first_result(
                args=args,
                result=result,
                artifact_path=screenshot_path,
            )
        if result.get("performed") is True:
            if transition_context_enabled:
                source = result.get("route_action_receipt_source")
                if not isinstance(source, dict):
                    receipt_failure_reason = stop_reason = (
                        "event_route_receipt_source_missing"
                    )
                    break
                action_id = source.get("action_id")
                if not isinstance(action_id, str) or action_id not in {
                    "event_choice.top",
                    "event_choice.second",
                    "event_choice.third",
                    "event_choice.fourth",
                    "event_choice.bottom",
                    "event_choice.dialogue_next",
                }:
                    receipt_failure_reason = stop_reason = (
                        "event_route_receipt_action_invalid"
                    )
                    break
                if (
                    source.get("action_state") != "event_choice"
                    or source.get("coordinate_name") != action_id
                ):
                    receipt_failure_reason = stop_reason = (
                        "event_route_receipt_source_invalid"
                    )
                    break
                source_path = source.get("source_path")
                source_sha256 = source.get("source_sha256")
                if (
                    not isinstance(source_path, (str, Path))
                    or not str(source_path)
                    or not isinstance(source_sha256, str)
                ):
                    receipt_failure_reason = stop_reason = (
                        "event_route_receipt_source_missing"
                    )
                    break
                try:
                    receipt = record_external_action_receipt(
                        transition_session_json,
                        expected_run_session_id=transition_run_session_id,
                        action_state="event_choice",
                        action_id=action_id,
                        source_path=source_path,
                        source_sha256=source_sha256,
                    )
                except (OSError, RouteTransitionError) as exc:
                    receipt_failure_reason = stop_reason = (
                        f"event_route_receipt:{exc}"
                    )
                    break
                result["route_transition"] = receipt.to_dict()
                last_persisted_receipt = result["route_transition"]
            performed += 1
            continue
        stop_reason = str(result.get("reason", "stopped"))
        if stop_reason.startswith("event_route_preflight:"):
            receipt_failure_reason = stop_reason
        break
    else:
        stop_reason = "max_steps_reached"

    report: dict[str, Any] = {
            "is_valid": True,
            "status": ("completed" if stop_reason == "max_steps_reached" else
                       "handoff" if step_reports and is_route_handoff_result(step_reports[-1]["result"])
                       else "stopped"),
            "performed_steps": performed,
            "requested_steps": args.max_steps,
            "stop_reason": stop_reason,
            "step_reports": step_reports,
    }
    if last_persisted_receipt is not None:
        report["route_transition"] = last_persisted_receipt
    if receipt_failure_reason is not None:
        report["is_valid"] = False
        report["status"] = "stopped"
    if stop_reason == "ready_main_menu" and step_reports:
        final_result = step_reports[-1].get("result")
        decision = (
            final_result.get("decision")
            if isinstance(final_result, dict)
            else None
        )
        evidence = decision.get("evidence") if isinstance(decision, dict) else None
        source_value = evidence.get("image") if isinstance(evidence, dict) else None
        source_path = Path(source_value) if isinstance(source_value, str) else None
        if source_path is not None and source_path.is_file():
            report["source_screenshot_path"] = str(source_path)
        else:
            report["is_valid"] = False
            report["status"] = "stopped"
            report["stop_reason"] = "ready_main_menu_source_missing"
            return report, 2
    if formal_capture is not None:
        report["formal_capture"] = formal_capture
    return report, 2 if receipt_failure_reason is not None else 0


def _formal_capture_from_first_result(
    *,
    args: argparse.Namespace,
    result: dict[str, Any],
    artifact_path: Path,
) -> dict[str, object] | None:
    values = (
        args.formal_run_session_id,
        args.formal_generation,
        args.formal_global_action_turn,
        args.formal_game_period,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("formal_capture_context_incomplete")
    decision = result.get("decision")
    page_state = (
        decision.get("route_state") if isinstance(decision, dict) else None
    )
    if page_state != "event_choice":
        return None
    # A bounded pre-input reread may select a later frame. Formal metadata must
    # identify that same decision frame, not the original unreadable attempt.
    source=(result.get('route_action_receipt_source') or {}).get('source_path')
    source=source or (decision.get('evidence') or {}).get('image')
    if source is not None:
        artifact_path=Path(source)
        expected=(result.get('frame_safety') or {}).get('source_image_sha256')
        if expected and hashlib.sha256(artifact_path.read_bytes()).hexdigest()!=expected:
            raise ValueError('formal_capture_frame_binding_mismatch')
    return _formal_capture_metadata(
        artifact_path=artifact_path,
        run_session_id=args.formal_run_session_id,
        generation=args.formal_generation,
        global_action_turn=args.formal_global_action_turn,
        game_period=args.formal_game_period,
        page_state=page_state,
    )


def _formal_capture_metadata(
    *,
    artifact_path: Path,
    run_session_id: str,
    generation: int,
    global_action_turn: int,
    game_period: str,
    page_state: str,
) -> dict[str, object]:
    if (
        not isinstance(artifact_path, Path)
        or not isinstance(run_session_id, str)
        or not run_session_id
        or type(generation) is not int
        or generation <= 0
        or type(global_action_turn) is not int
        or global_action_turn < 0
        or not isinstance(game_period, str)
        or not game_period
        or page_state != "event_choice"
    ):
        raise ValueError("formal_capture_metadata_invalid")
    if game_period != "unknown":
        raise ValueError("formal_capture_game_period_must_be_unknown")
    source_path = artifact_path.resolve(strict=True)
    source_frame_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    material: dict[str, object] = {
        "schema_version": "uma.event_choice_formal_capture.v1",
        "run_session_id": run_session_id,
        "generation": generation,
        "global_action_turn": global_action_turn,
        "game_period": game_period,
        "page_state": page_state,
        "source_artifact_path": str(source_path),
        "source_frame_sha256": source_frame_sha256,
        "capture_ttl_turns": 0,
        "expires_after_global_action_turn": global_action_turn,
    }
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        **material,
        "capture_metadata_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _adb_factory(adb_port: int | None):
    def create_adb():
        from core.native_capture_adb import adb_for_capture

        return adb_for_capture(adb_port)

    return create_adb


def _print(data: dict[str, Any], *, indent: int | None, code: int) -> int:
    print(json.dumps(data, ensure_ascii=False, indent=indent))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
