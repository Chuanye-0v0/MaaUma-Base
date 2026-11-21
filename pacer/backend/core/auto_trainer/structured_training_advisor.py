from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

from core.auto_trainer.career_supervisor_turn import (
    CandidateAction,
    TurnAuthorityBundle,
    TurnDecisionPacket,
    build_turn_authority_bundle,
    build_turn_decision_packet,
)
from core.auto_trainer.state import TrainingTurnState
from core.auto_trainer.structured_action_gate import (
    EXECUTION_ACTION_COORDINATE_NAMES,
    StructuredActionPlan,
    build_structured_action_plan,
)
from core.auto_trainer.structured_advisor_runtime import (
    StructuredAdvisorOutcome,
    StructuredAdvisorRuntime,
)


_SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ACTION_TO_KIND = {
    "train": "training",
    "rest": "rest",
    "clinic": "clinic",
}
_ACTION_TO_ID = {
    "rest": "rest.normal",
    "clinic": "clinic.normal",
}


@dataclass(frozen=True)
class TrainingAdvisorMetadata:
    run_session_id: str
    generation: int
    global_action_turn: int
    game_period: str
    source_frame_sha256: str
    perception_packet_sha256: str
    training_packet_sha256: str | None
    condition_authority_sha256: str | None
    goal_ledger_sha256: str | None = None
    race_plan_sha256: str | None = None
    scenario_packet_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_session_id, str) or not self.run_session_id:
            raise ValueError("metadata_run_session_invalid")
        if type(self.generation) is not int or self.generation <= 0:
            raise ValueError("metadata_generation_invalid")
        if (
            type(self.global_action_turn) is not int
            or self.global_action_turn < 0
        ):
            raise ValueError("metadata_global_turn_invalid")
        if not isinstance(self.game_period, str) or not self.game_period:
            raise ValueError("metadata_game_period_invalid")
        for value in (
            self.source_frame_sha256,
            self.perception_packet_sha256,
        ):
            _require_sha(value)
        for value in (
            self.training_packet_sha256,
            self.condition_authority_sha256,
            self.goal_ledger_sha256,
            self.race_plan_sha256,
            self.scenario_packet_sha256,
        ):
            if value is not None:
                _require_sha(value)


@dataclass(frozen=True)
class StructuredTrainingAdvisorTurn:
    authority: TurnAuthorityBundle
    decision_packet: TurnDecisionPacket
    decision_context: dict[str, object]
    outcome: StructuredAdvisorOutcome
    plan: StructuredActionPlan | None
    final_decision: dict[str, Any]


def build_structured_training_advisor_input(
    *,
    state: TrainingTurnState,
    deterministic_decision: Mapping[str, object],
    metadata: TrainingAdvisorMetadata,
) -> tuple[
    TurnAuthorityBundle,
    TurnDecisionPacket,
    dict[str, object],
]:
    if not isinstance(state, TrainingTurnState):
        raise ValueError("training_state_required")
    if not isinstance(deterministic_decision, Mapping):
        raise ValueError("deterministic_decision_required")
    if not isinstance(metadata, TrainingAdvisorMetadata):
        raise ValueError("training_advisor_metadata_required")

    optional = {
        "condition_authority_sha256": (
            metadata.condition_authority_sha256
        ),
        "goal_ledger_sha256": metadata.goal_ledger_sha256,
        "race_plan_sha256": metadata.race_plan_sha256,
        "training_packet_sha256": metadata.training_packet_sha256,
        "scenario_packet_sha256": metadata.scenario_packet_sha256,
    }
    unknown_fields = tuple(
        sorted(key for key, value in optional.items() if value is None)
    )
    authority = build_turn_authority_bundle(
        run_session_id=metadata.run_session_id,
        generation=metadata.generation,
        global_action_turn=metadata.global_action_turn,
        game_period=metadata.game_period,
        source_frame_sha256=metadata.source_frame_sha256,
        perception_packet_sha256=metadata.perception_packet_sha256,
        condition_authority_sha256=metadata.condition_authority_sha256,
        goal_ledger_sha256=metadata.goal_ledger_sha256,
        race_plan_sha256=metadata.race_plan_sha256,
        training_packet_sha256=metadata.training_packet_sha256,
        scenario_packet_sha256=metadata.scenario_packet_sha256,
        unknown_fields=unknown_fields,
        hazard_flags=(),
    )
    candidates, local_action_id = _build_candidates(
        state=state,
        decision=deterministic_decision,
        authority=authority,
    )
    packet = build_turn_decision_packet(
        authority=authority,
        candidates=candidates,
        local_preferred_action_id=local_action_id,
        safety_prerequisites=(
            "fresh_frame",
            "packet_sha_match",
            "same_generation",
            "same_run_session",
            "safety_gate",
        ),
    )
    context = _build_decision_context(
        state=state,
        decision=deterministic_decision,
        metadata=metadata,
        packet=packet,
    )
    return authority, packet, context


def apply_structured_training_advisor(
    *,
    state: TrainingTurnState,
    deterministic_decision: Mapping[str, object],
    metadata: TrainingAdvisorMetadata,
    runtime: StructuredAdvisorRuntime,
) -> StructuredTrainingAdvisorTurn:
    if not isinstance(runtime, StructuredAdvisorRuntime):
        raise ValueError("structured_advisor_runtime_required")
    authority, packet, context = (
        build_structured_training_advisor_input(
            state=state,
            deterministic_decision=deterministic_decision,
            metadata=metadata,
        )
    )
    outcome = runtime.evaluate(
        packet,
        decision_context=context,
    )
    final_decision = dict(deterministic_decision)
    selected = outcome.selected_action_id
    plan: StructuredActionPlan | None = None
    if selected is None:
        final_decision.update(
            {
                "action": "stop",
                "target": None,
                "reason": "structured_advisor_safe_stop",
            }
        )
    else:
        _apply_action_id(final_decision, selected)
        local_decision = _build_local_execution_decision(selected)
        plan = build_structured_action_plan(
            decision_packet=packet,
            advisor_outcome=outcome,
            local_decision=local_decision,
        )
    final_decision["structured_advisor"] = {
        "mode": outcome.request.mode,
        "resolution_status": outcome.resolution.status,
        "resolution_reason": outcome.resolution.reason,
        "selected_action_id": selected,
        "local_preferred_action_id": (
            outcome.resolution.local_preferred_action_id
        ),
        "advisor_action_id": outcome.resolution.advisor_action_id,
        "advisor_selection_applied": (
            outcome.resolution.advisor_selection_applied
        ),
        "decision_packet_sha256": packet.decision_packet_sha256,
        "request_sha256": outcome.request.request_sha256,
        "decision_context_sha256": (
            outcome.request.decision_context_sha256
        ),
        "audit_sha256": outcome.audit.audit_sha256,
        "action_plan_sha256": plan.plan_sha256 if plan else None,
        "safety_gate_state": "pending",
        "execution_authorized": False,
    }
    return StructuredTrainingAdvisorTurn(
        authority=authority,
        decision_packet=packet,
        decision_context=context,
        outcome=outcome,
        plan=plan,
        final_decision=final_decision,
    )


def _build_candidates(
    *,
    state: TrainingTurnState,
    decision: Mapping[str, object],
    authority: TurnAuthorityBundle,
) -> tuple[tuple[CandidateAction, ...], str]:
    action = decision.get("action")
    if action == "train":
        local_target = decision.get("target")
        local_action_id = (
            f"train:{local_target}"
            if isinstance(local_target, str)
            else ""
        )
        scored_by_id = _scored_candidates(decision)
        candidates = []
        for priority, option in enumerate(state.training_options):
            reasons: list[str] = []
            scored = scored_by_id.get(option.action_id)
            if option.failure_rate is None:
                reasons.append("failure_rate_unknown")
            elif scored is None:
                reasons.append("candidate_not_scored")
            elif (
                scored.get("rejected") is not False
                or scored.get("hard_rejected") is True
                or scored.get("eligible") is False
            ):
                reasons.append("locally_rejected")
            source_hashes = _unique_hashes(
                authority.bundle_sha256,
                authority.training_packet_sha256,
            )
            candidates.append(
                CandidateAction(
                    action_id=option.action_id,
                    action_kind="training",
                    consumes_primary_turn=True,
                    allowed=not reasons,
                    priority=priority,
                    source_packet_sha256s=source_hashes,
                    rejection_reasons=tuple(reasons),
                    requires_resource=False,
                    expected_failure_rate=(
                        int(option.failure_rate)
                        if option.failure_rate is not None
                        else None
                    ),
                )
            )
        if not any(
            candidate.action_id == local_action_id
            and candidate.allowed
            for candidate in candidates
        ):
            raise ValueError("local_action_missing_authority")
        return tuple(candidates), local_action_id

    if action in _ACTION_TO_ID:
        action_id = _ACTION_TO_ID[str(action)]
        condition_sha = authority.condition_authority_sha256
        if condition_sha is None:
            raise ValueError("local_action_missing_authority")
        candidate = CandidateAction(
            action_id=action_id,
            action_kind=_ACTION_TO_KIND[str(action)],
            consumes_primary_turn=True,
            allowed=True,
            priority=0,
            source_packet_sha256s=_unique_hashes(
                authority.bundle_sha256,
                condition_sha,
            ),
            rejection_reasons=(),
            requires_resource=False,
            expected_failure_rate=None,
        )
        return (candidate,), action_id
    raise ValueError("local_action_not_supported")


def _scored_candidates(
    decision: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    raw = decision.get("scored_candidates")
    if not isinstance(raw, list):
        return {}
    result: dict[str, Mapping[str, object]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        action_id = item.get("action_id")
        if isinstance(action_id, str):
            result[action_id] = item
    return result


def _build_decision_context(
    *,
    state: TrainingTurnState,
    decision: Mapping[str, object],
    metadata: TrainingAdvisorMetadata,
    packet: TurnDecisionPacket,
) -> dict[str, object]:
    scores = _scored_candidates(decision)
    options: list[dict[str, object]] = []
    for option in state.training_options:
        score = scores.get(option.action_id, {}).get("score")
        options.append(
            {
                "action_id": option.action_id,
                "training_type": option.training_type,
                "stats_gain": dict(option.stats_gain),
                "failure_rate": option.failure_rate,
                "support_count": option.support_count,
                "hint_count": option.hint_count,
                "friendship": option.friendship,
                "fragment_color": option.fragment_color,
                "fragment_stat": option.fragment_stat,
                "fragment_available": option.fragment_available,
                "vitality_delta": option.vitality_delta,
                "confidence": option.confidence,
                "local_score": _finite_or_none(score),
            }
        )
    return {
        "game_period": metadata.game_period,
        "turn": state.turn,
        "vitality": state.vitality,
        "current_stats": dict(state.current_stats),
        "warnings": list(state.warnings),
        "errors": list(state.errors),
        "local_action": {
            "action": decision.get("action"),
            "target": decision.get("target"),
            "confidence": _finite_or_none(
                decision.get("confidence")
            ),
        },
        "allowed_action_ids": [
            candidate.action_id
            for candidate in packet.candidates
            if candidate.allowed
        ],
        "training_options": options,
    }


def _apply_action_id(
    decision: dict[str, Any],
    action_id: str,
) -> None:
    if action_id.startswith("train:"):
        decision["action"] = "train"
        decision["target"] = action_id.split(":", 1)[1]
        decision["reason"] = "structured advisor selected allowed training"
    elif action_id == "rest.normal":
        decision["action"] = "rest"
        decision["target"] = None
    elif action_id == "clinic.normal":
        decision["action"] = "clinic"
        decision["target"] = None
    else:
        raise ValueError("structured_action_not_supported")


def _build_local_execution_decision(
    action_id: str,
) -> dict[str, object]:
    coordinate_name = EXECUTION_ACTION_COORDINATE_NAMES.get(action_id)
    if coordinate_name is None:
        return {
            "action": "wait",
            "reason": "structured action has no local coordinate",
            "structured_action_id": action_id,
        }
    return {
        "action": "tap",
        "reason": "structured action pending local safety gate",
        "structured_action_id": action_id,
        "tap": {"coordinate_name": coordinate_name},
    }


def _unique_hashes(*values: str | None) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value is not None and value not in result:
            result.append(value)
    return tuple(result)


def _finite_or_none(value: object) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return value


def _require_sha(value: object) -> None:
    if not isinstance(value, str) or _SHA_PATTERN.fullmatch(value) is None:
        raise ValueError("metadata_sha_invalid")


def canonical_sha(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "StructuredTrainingAdvisorTurn",
    "TrainingAdvisorMetadata",
    "apply_structured_training_advisor",
    "build_structured_training_advisor_input",
    "canonical_sha",
]
