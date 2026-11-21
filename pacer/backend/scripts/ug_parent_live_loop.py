from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.auto_trainer.ai_review import AITrainingReviewClient, CommandAIReviewClient
from core.auto_trainer.capture_adapter import build_state_from_capture_artifacts
from core.auto_trainer.condition_context import (
    load_explicit_condition_context,
    merge_explicit_conditions,
)
from core.auto_trainer.config import load_goal_config, validate_live_execution_gate
from core.auto_trainer.career_supervisor_turn import build_turn_authority_bundle
from core.auto_trainer.dashboard_runner import CooperativeRunnerDriver
from core.auto_trainer.decision import numeric_blind_terminal_state_errors
from core.auto_trainer.executor import TrainingActionExecutor
from core.auto_trainer.frame_safety import FrameSafetyAnalyzer
from core.auto_trainer.live_loop import UGParentTrainingLoop
from core.auto_trainer.perception_reliability import (
    build_perception_decision_view,
    load_perception_reliability_bundle,
    load_trusted_gate_authorization_registry_from_workspace,
)
from core.auto_trainer.pacer_storage import _io
from core.auto_trainer.parser_sidecar import build_training_options_sidecar
from core.auto_trainer.state import TrainingTurnState
from core.auto_trainer.structured_advisor_runtime import (
    CommandStructuredAdvisorClient,
    StructuredAdvisorRuntime,
)
from core.auto_trainer.structured_training_advisor import (
    TrainingAdvisorMetadata,
)


class StaticAIReviewClient(AITrainingReviewClient):
    def __init__(self, response: str) -> None:
        self.response = response

    def review_training(self, prompt: dict[str, Any]) -> str:
        # Legacy static responses are offline-only, so bind them to this turn here.
        try:
            response = json.loads(self.response)
        except json.JSONDecodeError:
            return self.response
        if isinstance(response, dict) and "decision_packet_sha256" not in response:
            response["decision_packet_sha256"] = prompt["decision_packet_sha256"]
            return json.dumps(response, ensure_ascii=False)
        return self.response


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generic UG parent training loop. Formal live execution currently uses "
            "only the deterministic local policy; static and command advisors are "
            "dry-run-only."
        )
    )
    parser.add_argument("--goal-config", required=True)
    parser.add_argument("--input-state")
    parser.add_argument("--capture-manifest")
    parser.add_argument("--execution-root")
    parser.add_argument("--actual-group-id")
    parser.add_argument("--training-options-json")
    parser.add_argument("--training-page-observation-json")
    parser.add_argument("--auto-build-sidecar", action="store_true")
    parser.add_argument("--sidecar-output-json")
    parser.add_argument("--state-context-json")
    parser.add_argument("--perception-reliability-json")
    parser.add_argument("--perception-authority-json")
    parser.add_argument("--perception-frames-json")
    parser.add_argument("--perception-turn-id")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--mode", choices=["dry-run", "live"], default="dry-run")
    parser.add_argument("--execute-training-confirm", action="store_true")
    parser.add_argument("--confirm-token")
    parser.add_argument("--max-turns", type=int, required=True)
    parser.add_argument("--adb-port", type=int)
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
    parser.add_argument("--structured-run-session-id")
    parser.add_argument("--structured-generation", type=int)
    parser.add_argument("--structured-global-action-turn", type=int)
    parser.add_argument("--structured-game-period")
    parser.add_argument("--structured-source-frame-path")
    parser.add_argument("--structured-source-frame-sha256")
    parser.add_argument("--structured-perception-packet-sha256")
    parser.add_argument("--structured-training-packet-sha256")
    parser.add_argument("--structured-condition-authority-sha256")
    parser.add_argument("--post-action-skip-taps", type=int, default=0)
    parser.add_argument("--min-decision-confidence", type=float, default=0.0)
    parser.add_argument("--require-selected-primary-gain-reliable", action="store_true")
    parser.add_argument(
        "--numeric-blind-fallback",
        action="store_true",
        help="Use the local numeric-blind fallback policy for this process run.",
    )
    parser.add_argument("--condition-report-json")
    parser.add_argument("--require-explicit-condition-probe", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def _build_perception_decision_kwargs(
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Rebuild the decision-safe perception view from same-turn artifacts.

    Fails open: if any binding cannot be reconstructed, the decision keeps
    its existing numeric-blind behavior instead of crashing the turn.
    """

    if not args.perception_reliability_json:
        return {}
    try:
        authority_dict = json.loads(
            Path(args.perception_authority_json).read_text(encoding="utf-8")
        )
        frames = json.loads(
            Path(args.perception_frames_json).read_text(encoding="utf-8")
        )
        bundle_value = json.loads(
            Path(args.perception_reliability_json).read_text(encoding="utf-8")
        )
        authority_kwargs = {
            key: value
            for key, value in authority_dict.items()
            if key not in {"schema_version", "bundle_sha256"}
        }
        authority_kwargs["unknown_fields"] = tuple(
            authority_kwargs["unknown_fields"]
        )
        authority_kwargs["hazard_flags"] = tuple(
            authority_kwargs["hazard_flags"]
        )
        authority = build_turn_authority_bundle(**authority_kwargs)
        gate_registry = (
            load_trusted_gate_authorization_registry_from_workspace(ROOT)
        )
        bundle = load_perception_reliability_bundle(
            bundle_value,
            authority=authority,
            expected_turn_id=args.perception_turn_id,
            expected_frame_sha256_by_tab=frames,
            gate_registry=gate_registry,
        )
        view = build_perception_decision_view(bundle)
        return {
            "perception_decision_view": view,
            "perception_authority": authority,
            "perception_turn_id": args.perception_turn_id,
            "perception_source_frame_sha256_by_tab": frames,
        }
    except Exception as exc:  # noqa: BLE001 - fail open to existing behavior
        print(
            json.dumps(
                {
                    "perception_decision_unavailable": (
                        f"{type(exc).__name__}: {exc}"
                    )
                }
            ),
            file=sys.stderr,
        )
        return {}


def main(
    argv: list[str] | None = None,
    *,
    formal_condition_binding_key: str | None = None,
    control_driver: CooperativeRunnerDriver | None = None,
    frame_safety_analyzer: FrameSafetyAnalyzer | None = None,
) -> int:
    args = parse_args(argv)
    indent = 2 if args.pretty else None
    try:
        if args.mode == "live" and args.input_state:
            raise ValueError("input_state_forbidden_in_live")
        _validate_ai_review_source(args)
        if args.min_decision_confidence < 0.0 or args.min_decision_confidence > 1.0:
            raise ValueError("min_decision_confidence_out_of_range")
        try:
            state = _load_state(args)
        except FileNotFoundError as exc:
            kind = _missing_current_capture_input(args, exc)
            if kind is None:
                raise
            return _print(
                {
                    "is_valid": False,
                    "status": "pre_action_capture_artifact_missing",
                    "reason": f"pre_action_capture_artifact_missing:{kind}",
                    "error_type": "FileNotFoundError",
                    "error": f"FileNotFoundError: {exc}",
                },
                indent=indent,
                code=2,
            )
        observation_reads = 1
        if (args.numeric_blind_fallback and state.screen == "training_menu" and state.errors
                and not numeric_blind_terminal_state_errors(state.errors)):
            observation_reads += 1
            state = _reread_numeric_blind_state(args, state)
        condition_turn_root = Path(args.run_dir).parent
        condition_context = _load_condition_context(
            args,
            formal_condition_binding_key=formal_condition_binding_key,
            expected_turn_index=state.turn,
        )
        goal = load_goal_config(args.goal_config)
        ok, reason = validate_live_execution_gate(
            goal=goal,
            mode=args.mode,
            execute_training_confirm=args.execute_training_confirm,
            confirm_token=args.confirm_token,
            max_turns=args.max_turns,
        )
        if not ok:
            return _print(
                {"is_valid": False, "error": reason},
                indent=indent,
                code=2,
            )

        state = _merge_condition_context_into_state(
            state,
            condition_context,
            mode=args.mode,
        )
        structured_components = _load_structured_advisor(args)
        loop = UGParentTrainingLoop(
            goal=goal,
            run_dir=Path(args.run_dir),
            executor=TrainingActionExecutor(scenario=goal.scenario,
                adb_factory=_adb_factory(args.adb_port, native=goal.scenario == "larc"),
                outing_adb_factory=_adb_factory(args.adb_port, native=True),
                **({"frame_safety_analyzer": frame_safety_analyzer} if frame_safety_analyzer is not None else {})),
            ai_client=_load_ai_client(args),
            formal_condition_binding_key=formal_condition_binding_key,
            condition_turn_root=condition_turn_root,
            expected_condition_turn_id=args.actual_group_id,
            **structured_components,
        )
        run_turn_kwargs = {
            "mode": args.mode,
            "execute_training_confirm": args.execute_training_confirm,
            "confirm_token": args.confirm_token,
            "max_turns": args.max_turns,
            "post_action_skip_taps": args.post_action_skip_taps,
            "min_decision_confidence": args.min_decision_confidence,
            "require_selected_primary_gain_reliable": args.require_selected_primary_gain_reliable,
            "numeric_blind_fallback": args.numeric_blind_fallback,
        }
        run_turn_kwargs.update(_build_perception_decision_kwargs(args))
        if control_driver is not None:
            run_turn_kwargs["control_driver"] = control_driver
        turn_result = loop.run_turn(state, **run_turn_kwargs)
        if turn_result.get("status") == "control_stopped":
            return _print(
                {
                    "is_valid": True,
                    "status": "control_stopped",
                    "control_boundary": turn_result.get("control_boundary"),
                    "run_dir": str(Path(args.run_dir)),
                    "turns_completed": 0,
                    "turns_requested": args.max_turns,
                    "advisor": {"source": _advisor_source(args)},
                },
                indent=indent,
                code=0,
            )
        return _print(
            {
                "is_valid": True,
                "run_dir": str(Path(args.run_dir)),
                "turns_completed": 0 if turn_result.get("status") == "numeric_blind_state_refresh_required" else 1,
                "turns_requested": args.max_turns,
                "advisor": {"source": _advisor_source(args)},
                "decision": turn_result["decision"],
                "execution": turn_result["execution"],
                **({"numeric_blind_observation_reads": observation_reads} if args.numeric_blind_fallback else {}),
            },
            indent=indent,
            code=0,
        )
    except Exception as exc:
        return _print(
            {"is_valid": False, "error": f"{type(exc).__name__}: {exc}"},
            indent=indent,
            code=2,
        )


def _print(data: dict[str, Any], *, indent: int | None, code: int) -> int:
    print(json.dumps(data, ensure_ascii=False, indent=indent))
    return code


def _adb_factory(adb_port: int | None, *, native=False):
    def create_adb():
        if native:
            from core.native_capture_adb import adb_for_capture
            return adb_for_capture(adb_port)
        from core.adb import ADB

        return ADB(port=adb_port) if adb_port is not None else ADB()

    return create_adb


def _load_ai_client(args: argparse.Namespace) -> AITrainingReviewClient | None:
    if args.ai_review_json:
        return StaticAIReviewClient(
            Path(args.ai_review_json).read_text(encoding="utf-8-sig")
        )
    if args.ai_review_command:
        return CommandAIReviewClient(
            args.ai_review_command,
            timeout_seconds=args.ai_review_command_timeout_seconds,
            cwd=ROOT,
        )
    return None


def _validate_ai_review_source(args: argparse.Namespace) -> None:
    if args.mode == "live":
        if args.ai_review_json:
            raise ValueError("ai_review_static_json_forbidden_in_live")
        if args.ai_review_command:
            raise ValueError("ai_review_command_not_sandboxed_for_live")
    if args.ai_review_json and args.ai_review_command:
        raise ValueError("ai_review_source_conflict")
    structured_mode = args.structured_advisor_mode
    structured_args = tuple(args.structured_advisor_command_arg)
    if structured_mode == "off":
        if structured_args or args.structured_advisor_model_id:
            raise ValueError("structured_advisor_disabled_with_config")
    else:
        if not structured_args:
            raise ValueError("structured_advisor_command_required")
        if not args.structured_advisor_model_id:
            raise ValueError("structured_advisor_model_id_required")
        if args.ai_review_json or args.ai_review_command:
            raise ValueError("legacy_and_structured_advisor_conflict")


def _load_structured_advisor(
    args: argparse.Namespace,
) -> dict[str, Any]:
    if args.structured_advisor_mode == "off":
        return {}
    required = {
        "structured_run_session_id": args.structured_run_session_id,
        "structured_generation": args.structured_generation,
        "structured_global_action_turn": (
            args.structured_global_action_turn
        ),
        "structured_game_period": args.structured_game_period,
        "structured_source_frame_path": (
            args.structured_source_frame_path
        ),
        "structured_source_frame_sha256": (
            args.structured_source_frame_sha256
        ),
        "structured_perception_packet_sha256": (
            args.structured_perception_packet_sha256
        ),
        "structured_training_packet_sha256": (
            args.structured_training_packet_sha256
        ),
        "structured_condition_authority_sha256": (
            args.structured_condition_authority_sha256
        ),
    }
    missing = sorted(
        key for key, value in required.items() if value is None
    )
    if missing:
        raise ValueError(
            "structured_advisor_metadata_missing:"
            + ",".join(missing)
        )
    source_path = Path(args.structured_source_frame_path)
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if source_sha != args.structured_source_frame_sha256:
        raise ValueError("structured_source_frame_sha_mismatch")
    training_path = Path(args.training_options_json)
    training_sha = hashlib.sha256(training_path.read_bytes()).hexdigest()
    if training_sha != args.structured_training_packet_sha256:
        raise ValueError("structured_training_packet_sha_mismatch")
    analyzer = FrameSafetyAnalyzer()
    perception_frame_packet = analyzer.analyze(source_path)
    if perception_frame_packet.source_image_sha256 != source_sha:
        raise ValueError("structured_perception_frame_sha_mismatch")
    structured_command = tuple(args.structured_advisor_command_arg)
    structured_runtime_cwd = (
        Path(structured_command[1]).parent.parent
        if len(structured_command) == 2
        else ROOT
    )
    runtime = StructuredAdvisorRuntime(
        mode=args.structured_advisor_mode,
        expected_model_id=args.structured_advisor_model_id,
        client=CommandStructuredAdvisorClient(
            structured_command,
            expected_model_id=args.structured_advisor_model_id,
            timeout_seconds=args.structured_advisor_timeout_seconds,
            cwd=structured_runtime_cwd,
            base_url="https://api.deepseek.com",
            provider_timeout_ms=60_000,
        ),
        clock_unix_ms=lambda: int(time.time() * 1000),
        ttl_ms=args.structured_advisor_ttl_ms,
        invalid_response_policy=(
            args.structured_advisor_invalid_policy
        ),
    )
    metadata = TrainingAdvisorMetadata(
        run_session_id=args.structured_run_session_id,
        generation=args.structured_generation,
        global_action_turn=args.structured_global_action_turn,
        game_period=args.structured_game_period,
        source_frame_sha256=source_sha,
        perception_packet_sha256=(
            args.structured_perception_packet_sha256
        ),
        training_packet_sha256=training_sha,
        condition_authority_sha256=(
            args.structured_condition_authority_sha256
        ),
    )
    return {
        "structured_advisor_runtime": runtime,
        "structured_advisor_metadata": metadata,
        "structured_perception_frame_packet": (
            perception_frame_packet
        ),
        "structured_frame_safety_analyzer": analyzer,
    }


def _advisor_source(args: argparse.Namespace) -> str:
    if args.structured_advisor_mode != "off":
        return f"structured_{args.structured_advisor_mode}"
    if args.ai_review_command:
        return "command"
    if args.ai_review_json:
        return "static_dry_run"
    return "none"


def _resolved_capture_input(path: str) -> Path:
    # resolve() may preserve the Windows extended prefix for a missing file.
    return _io(_io(path).resolve())


def _missing_current_capture_input(args: argparse.Namespace, error: FileNotFoundError) -> str | None:
    """Name only disposable, current-turn inputs read before any execution."""
    if args.mode != "live" or not (error.filename and args.execution_root and args.actual_group_id):
        return None
    try:
        root = _resolved_capture_input(args.execution_root)
        missing = _resolved_capture_input(error.filename)
        if not root.is_relative_to(_resolved_capture_input(args.run_dir).parent) or not missing.is_relative_to(root):
            return None
        for filename, kind in (
            (args.capture_manifest, "manifest"),
            (args.state_context_json, "state_context"),
            (args.training_options_json, "training_options"),
            (args.training_page_observation_json, "training_observation"),
        ):
            if filename and missing == _resolved_capture_input(filename):
                return kind
        if missing.suffix.lower() == ".png" and args.actual_group_id in missing.relative_to(root).parts:
            return "training_frame"
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def _load_state(args: argparse.Namespace) -> TrainingTurnState:
    direct_state = bool(args.input_state)
    capture_state = bool(args.capture_manifest or args.execution_root or args.actual_group_id)
    if direct_state == capture_state:
        raise ValueError("exactly_one_state_source_required")
    if direct_state:
        data = json.loads(Path(args.input_state).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("input_state_root_not_object")
        return TrainingTurnState.from_dict(data)

    missing = [
        name
        for name, value in (
            ("capture_manifest", args.capture_manifest),
            ("execution_root", args.execution_root),
            ("actual_group_id", args.actual_group_id),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"capture_state_missing_args:{','.join(missing)}")
    training_options_path = _resolve_training_options_path(args)
    context = None
    if args.state_context_json:
        context = json.loads(
            _io(args.state_context_json).read_text(encoding="utf-8-sig")
        )
        if not isinstance(context, dict):
            raise ValueError("state_context_root_not_object")
    return build_state_from_capture_artifacts(
        manifest_path=Path(args.capture_manifest),
        execution_root=Path(args.execution_root),
        actual_group_id=args.actual_group_id,
        training_options_path=training_options_path,
        training_page_observation_path=(
            Path(args.training_page_observation_json)
            if args.training_page_observation_json
            else None
        ),
        state_context=context,
    )


def _reread_numeric_blind_state(args: argparse.Namespace, prior: TrainingTurnState) -> TrainingTurnState:
    """Reopen the current source once; do not adopt a different capture group."""
    try:
        current = _load_state(args)
    except (OSError, ValueError):
        return prior
    if current.screen != prior.screen or current.turn != prior.turn:
        return prior
    def capture_binding(state):
        return [(item.training_type, *(item.raw_data.get("capture", {}).get(key)
                    for key in ("run_session_id", "turn_index", "actual_group_id", "sha256")))
                for item in state.training_options]
    return current if capture_binding(current) == capture_binding(prior) else prior


def _load_condition_context(
    args: argparse.Namespace,
    *,
    formal_condition_binding_key: str | None,
    expected_turn_index: int | None,
) -> dict[str, Any] | None:
    if args.require_explicit_condition_probe and not args.condition_report_json:
        raise ValueError("explicit_condition_probe_report_required")
    if not args.condition_report_json:
        return None
    if args.require_explicit_condition_probe and formal_condition_binding_key is None:
        raise ValueError("condition_binding_key_missing")
    return load_explicit_condition_context(
        args.condition_report_json,
        required_turn_root=Path(args.run_dir).parent,
        formal_live=args.mode == "live" and args.require_explicit_condition_probe,
        formal_binding_key=formal_condition_binding_key,
        expected_turn_id=args.actual_group_id,
        expected_turn_index=expected_turn_index,
    )


def _merge_condition_context_into_state(
    state: TrainingTurnState,
    condition_context: dict[str, Any] | None,
    *,
    mode: str,
) -> TrainingTurnState:
    state_data = state.to_dict()
    scenario_state = state_data.get("scenario_state")
    has_preexisting_condition_context = isinstance(scenario_state, dict) and (
        "conditions" in scenario_state or "condition_context" in scenario_state
    )
    if mode == "live" and has_preexisting_condition_context:
        if condition_context is None:
            raise ValueError("untrusted_preexisting_condition_context_in_live")
        sanitized_scenario_state = dict(scenario_state)
        sanitized_scenario_state.pop("conditions", None)
        sanitized_scenario_state.pop("condition_context", None)
        state_data["scenario_state"] = sanitized_scenario_state
    if condition_context is None:
        return TrainingTurnState.from_dict(state_data)
    return TrainingTurnState.from_dict(
        merge_explicit_conditions(state_data, condition_context)
    )


def _resolve_training_options_path(args: argparse.Namespace) -> Path | None:
    if args.training_options_json:
        return Path(args.training_options_json)
    if not args.auto_build_sidecar:
        return None
    output_path = (
        Path(args.sidecar_output_json)
        if args.sidecar_output_json
        else Path(args.execution_root) / "training_options.generated.json"
    )
    state_context = None
    if args.state_context_json:
        state_context = json.loads(
            _io(args.state_context_json).read_text(encoding="utf-8-sig")
        )
        if not isinstance(state_context, dict):
            raise ValueError("state_context_root_not_object")
    report = build_training_options_sidecar(
        manifest_path=Path(args.capture_manifest),
        execution_root=Path(args.execution_root),
        actual_group_id=args.actual_group_id,
        output_path=output_path,
        state_context=state_context,
        overwrite=True,
        pretty=True,
    )
    if not report.get("is_valid"):
        errors = ",".join(str(item) for item in report.get("errors", []))
        raise ValueError(f"auto_sidecar_build_failed:{errors}")
    return output_path


if __name__ == "__main__":
    raise SystemExit(main())
