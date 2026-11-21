from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.auto_trainer.career_supervisor_turn import (
    TurnAuthorityBundle,
    TurnDecisionPacket,
)
from core.auto_trainer.frame_safety import (
    FormalActionSafetyGate,
    FrameSafetyPacket,
)
from core.auto_trainer.structured_advisor_runtime import (
    StructuredAdvisorOutcome,
)


class StructuredActionGateError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


DEFAULT_ACTION_COORDINATE_NAMES = {
    "training.speed": "speed",
    "training.stamina": "stamina",
    "training.power": "power",
    "training.guts": "guts",
    "training.wisdom": "wisdom",
    "rest.normal": "rest",
    "outing.normal": "outing",
    "clinic.normal": "clinic",
}

EXECUTION_ACTION_COORDINATE_NAMES = {
    "train:speed": "training_menu.speed_decide",
    "train:stamina": "training_menu.stamina_decide",
    "train:power": "training_menu.power_decide",
    "train:guts": "training_menu.guts_decide",
    "train:wisdom": "training_menu.wisdom_decide",
    "training.speed": "training_menu.speed_decide",
    "training.stamina": "training_menu.stamina_decide",
    "training.power": "training_menu.power_decide",
    "training.guts": "training_menu.guts_decide",
    "training.wisdom": "training_menu.wisdom_decide",
    "rest.normal": "main_menu.rest",
    "outing.normal": "main_menu.outing",
    "clinic.normal": "main_menu.clinic",
}


@dataclass(frozen=True)
class StructuredActionPlan:
    schema_version: str
    run_session_id: str
    generation: int
    global_action_turn: int
    decision_packet_sha256: str
    advisor_request_sha256: str
    action_id: str
    local_decision_json: str
    plan_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != "uma.structured_action_plan.v1":
            raise StructuredActionGateError(
                "structured_plan_schema_invalid"
            )
        try:
            decoded = json.loads(self.local_decision_json)
        except json.JSONDecodeError:
            raise StructuredActionGateError(
                "structured_plan_decision_invalid"
            ) from None
        if not isinstance(decoded, dict):
            raise StructuredActionGateError(
                "structured_plan_decision_invalid"
            )
        material = self.to_dict()
        material.pop("plan_sha256")
        if _canonical_sha(material) != self.plan_sha256:
            raise StructuredActionGateError(
                "structured_plan_sha_mismatch"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_session_id": self.run_session_id,
            "generation": self.generation,
            "global_action_turn": self.global_action_turn,
            "decision_packet_sha256": self.decision_packet_sha256,
            "advisor_request_sha256": self.advisor_request_sha256,
            "action_id": self.action_id,
            "local_decision_json": self.local_decision_json,
            "plan_sha256": self.plan_sha256,
        }

    def local_decision(self) -> dict[str, Any]:
        decoded = json.loads(self.local_decision_json)
        if not isinstance(decoded, dict):
            raise StructuredActionGateError(
                "structured_plan_decision_invalid"
            )
        return decoded


@dataclass(frozen=True)
class StructuredActionGateVerdict:
    schema_version: str
    status: str
    execution_allowed: bool
    reason: str
    authority: str
    action_id: str | None
    decision_packet_sha256: str
    advisor_request_sha256: str
    action_plan_sha256: str
    source_frame_sha256: str
    underlying_gate_reason: str | None
    verdict_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != "uma.structured_action_gate_verdict.v1":
            raise StructuredActionGateError(
                "structured_verdict_schema_invalid"
            )
        if self.authority != "local_formal_action_safety_gate":
            raise StructuredActionGateError(
                "structured_verdict_authority_invalid"
            )
        payload = self.to_dict()
        payload.pop("verdict_sha256")
        if _canonical_sha(payload) != self.verdict_sha256:
            raise StructuredActionGateError(
                "structured_verdict_sha_mismatch"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "execution_allowed": self.execution_allowed,
            "reason": self.reason,
            "authority": self.authority,
            "action_id": self.action_id,
            "decision_packet_sha256": self.decision_packet_sha256,
            "advisor_request_sha256": self.advisor_request_sha256,
            "action_plan_sha256": self.action_plan_sha256,
            "source_frame_sha256": self.source_frame_sha256,
            "underlying_gate_reason": self.underlying_gate_reason,
            "verdict_sha256": self.verdict_sha256,
        }


def build_structured_action_plan(
    *,
    decision_packet: TurnDecisionPacket,
    advisor_outcome: StructuredAdvisorOutcome,
    local_decision: Mapping[str, object],
) -> StructuredActionPlan:
    if not isinstance(decision_packet, TurnDecisionPacket):
        raise StructuredActionGateError(
            "structured_decision_packet_required"
        )
    if not isinstance(advisor_outcome, StructuredAdvisorOutcome):
        raise StructuredActionGateError(
            "structured_advisor_outcome_required"
        )
    selected_action_id = advisor_outcome.selected_action_id
    if selected_action_id is None:
        raise StructuredActionGateError(
            "structured_selected_action_required"
        )
    if (
        advisor_outcome.request.decision_packet_sha256
        != decision_packet.decision_packet_sha256
        or advisor_outcome.audit.decision_packet_sha256
        != decision_packet.decision_packet_sha256
    ):
        raise StructuredActionGateError(
            "structured_advisor_decision_mismatch"
        )
    candidate = _candidate_by_id(decision_packet, selected_action_id)
    if candidate is None or not candidate.allowed or candidate.requires_resource:
        raise StructuredActionGateError(
            "structured_selected_action_not_allowed"
        )
    if not isinstance(local_decision, Mapping):
        raise StructuredActionGateError(
            "structured_plan_decision_invalid"
        )
    decision_copy = dict(local_decision)
    if decision_copy.get("structured_action_id") != selected_action_id:
        raise StructuredActionGateError(
            "structured_plan_action_mismatch"
        )
    if decision_copy.get("action") not in {"tap", "wait"}:
        raise StructuredActionGateError(
            "structured_plan_decision_invalid"
        )
    _validate_local_decision_shape(
        decision_copy,
        selected_action_id=selected_action_id,
    )
    try:
        local_decision_json = json.dumps(
            decision_copy,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise StructuredActionGateError(
            "structured_plan_decision_invalid"
        ) from None
    payload = {
        "schema_version": "uma.structured_action_plan.v1",
        "run_session_id": decision_packet.run_session_id,
        "generation": decision_packet.generation,
        "global_action_turn": decision_packet.global_action_turn,
        "decision_packet_sha256": decision_packet.decision_packet_sha256,
        "advisor_request_sha256": advisor_outcome.request.request_sha256,
        "action_id": selected_action_id,
        "local_decision_json": local_decision_json,
    }
    payload["plan_sha256"] = _canonical_sha(payload)
    return StructuredActionPlan(**payload)


class StructuredActionSafetyGate:
    def __init__(
        self,
        formal_gate: FormalActionSafetyGate | None = None,
    ) -> None:
        self.formal_gate = formal_gate or FormalActionSafetyGate()

    def evaluate(
        self,
        *,
        authority: TurnAuthorityBundle,
        decision_packet: TurnDecisionPacket,
        advisor_outcome: StructuredAdvisorOutcome,
        frame_packet: FrameSafetyPacket,
        plan: StructuredActionPlan,
    ) -> StructuredActionGateVerdict:
        common_error = self._binding_error(
            authority=authority,
            decision_packet=decision_packet,
            advisor_outcome=advisor_outcome,
            frame_packet=frame_packet,
            plan=plan,
        )
        if common_error is not None:
            return _verdict(
                status="blocked",
                execution_allowed=False,
                reason=common_error,
                action_id=plan.action_id,
                decision_packet=decision_packet,
                advisor_outcome=advisor_outcome,
                plan=plan,
                frame_packet=frame_packet,
                underlying_gate_reason=None,
            )
        local_decision = plan.local_decision()
        underlying = self.formal_gate.evaluate(
            frame_packet,
            local_decision,
        )
        if not underlying.allowed:
            return _verdict(
                status="blocked",
                execution_allowed=False,
                reason=underlying.reason,
                action_id=plan.action_id,
                decision_packet=decision_packet,
                advisor_outcome=advisor_outcome,
                plan=plan,
                frame_packet=frame_packet,
                underlying_gate_reason=underlying.reason,
            )
        candidate = _candidate_by_id(decision_packet, plan.action_id)
        if (
            local_decision.get("action") != "tap"
            or candidate is None
            or candidate.action_kind == "stop"
        ):
            return _verdict(
                status="non_executing",
                execution_allowed=False,
                reason="non_executing_local_action",
                action_id=plan.action_id,
                decision_packet=decision_packet,
                advisor_outcome=advisor_outcome,
                plan=plan,
                frame_packet=frame_packet,
                underlying_gate_reason=underlying.reason,
            )
        return _verdict(
            status="authorized",
            execution_allowed=True,
            reason="local_safety_gate_pass",
            action_id=plan.action_id,
            decision_packet=decision_packet,
            advisor_outcome=advisor_outcome,
            plan=plan,
            frame_packet=frame_packet,
            underlying_gate_reason=underlying.reason,
        )

    @staticmethod
    def _binding_error(
        *,
        authority: TurnAuthorityBundle,
        decision_packet: TurnDecisionPacket,
        advisor_outcome: StructuredAdvisorOutcome,
        frame_packet: FrameSafetyPacket,
        plan: StructuredActionPlan,
    ) -> str | None:
        if not isinstance(authority, TurnAuthorityBundle):
            return "authority_required"
        if not isinstance(decision_packet, TurnDecisionPacket):
            return "decision_packet_required"
        if not isinstance(advisor_outcome, StructuredAdvisorOutcome):
            return "advisor_outcome_required"
        if not isinstance(frame_packet, FrameSafetyPacket):
            return "frame_safety_packet_required"
        if not isinstance(plan, StructuredActionPlan):
            return "structured_action_plan_required"
        if (
            decision_packet.authority_bundle_sha256
            != authority.bundle_sha256
        ):
            return "authority_bundle_sha_mismatch"
        if frame_packet.source_image_sha256 != authority.source_frame_sha256:
            return "source_frame_sha_mismatch"
        if (
            plan.run_session_id != authority.run_session_id
            or plan.generation != authority.generation
            or plan.global_action_turn != authority.global_action_turn
        ):
            return "structured_plan_turn_mismatch"
        if (
            plan.decision_packet_sha256
            != decision_packet.decision_packet_sha256
            or advisor_outcome.request.decision_packet_sha256
            != decision_packet.decision_packet_sha256
            or advisor_outcome.audit.decision_packet_sha256
            != decision_packet.decision_packet_sha256
        ):
            return "structured_decision_sha_mismatch"
        if (
            plan.advisor_request_sha256
            != advisor_outcome.request.request_sha256
            or advisor_outcome.audit.request_sha256
            != advisor_outcome.request.request_sha256
        ):
            return "structured_advisor_request_sha_mismatch"
        if plan.action_id != advisor_outcome.selected_action_id:
            return "structured_selected_action_mismatch"
        candidate = _candidate_by_id(decision_packet, plan.action_id)
        if candidate is None or not candidate.allowed:
            return "structured_selected_action_not_allowed"
        if candidate.requires_resource:
            return "structured_resource_action_blocked"
        local_decision = plan.local_decision()
        try:
            _validate_local_decision_shape(
                local_decision,
                selected_action_id=plan.action_id,
            )
        except StructuredActionGateError as exc:
            return exc.code
        if local_decision.get("structured_action_id") != plan.action_id:
            return "structured_plan_action_mismatch"
        return None


class StructuredExecutionGuard:
    """Bind advisor selection to perception evidence and a fresh pre-tap frame."""

    def __init__(
        self,
        *,
        authority: TurnAuthorityBundle,
        decision_packet: TurnDecisionPacket,
        advisor_outcome: StructuredAdvisorOutcome,
        plan: StructuredActionPlan,
        perception_frame_packet: FrameSafetyPacket,
        frame_safety_analyzer: Any,
        structured_gate: StructuredActionSafetyGate | None = None,
        formal_gate: FormalActionSafetyGate | None = None,
    ) -> None:
        self.authority = authority
        self.decision_packet = decision_packet
        self.advisor_outcome = advisor_outcome
        self.plan = plan
        self.perception_frame_packet = perception_frame_packet
        self.frame_safety_analyzer = frame_safety_analyzer
        self.structured_gate = (
            structured_gate or StructuredActionSafetyGate()
        )
        self.formal_gate = formal_gate or FormalActionSafetyGate()

    def authorize(
        self,
        *,
        frame_path: str | Path,
        coordinate_name: str | None,
    ) -> dict[str, Any]:
        expected_coordinate = (
            self.plan.local_decision().get("tap", {}).get(
                "coordinate_name"
            )
        )
        if (
            not isinstance(coordinate_name, str)
            or coordinate_name != expected_coordinate
        ):
            return self._blocked(
                "structured_execution_coordinate_mismatch"
            )
        perception_verdict = self.structured_gate.evaluate(
            authority=self.authority,
            decision_packet=self.decision_packet,
            advisor_outcome=self.advisor_outcome,
            frame_packet=self.perception_frame_packet,
            plan=self.plan,
        )
        if not perception_verdict.execution_allowed:
            return self._blocked(
                perception_verdict.reason,
                perception_verdict=perception_verdict,
            )
        path = Path(frame_path)
        try:
            actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            execution_packet = self.frame_safety_analyzer.analyze(path)
        except Exception:
            return self._blocked(
                "structured_execution_frame_analysis_failed",
                perception_verdict=perception_verdict,
            )
        if (
            not isinstance(execution_packet, FrameSafetyPacket)
            or execution_packet.source_image_sha256 != actual_sha
        ):
            return self._blocked(
                "structured_execution_frame_sha_mismatch",
                perception_verdict=perception_verdict,
            )
        current_verdict = self.formal_gate.evaluate(
            execution_packet,
            self.plan.local_decision(),
        )
        if not current_verdict.allowed:
            return self._blocked(
                current_verdict.reason,
                perception_verdict=perception_verdict,
                execution_frame_sha256=actual_sha,
                underlying_gate_reason=current_verdict.reason,
            )
        payload = {
            "allowed": True,
            "reason": "structured_execution_authorized",
            "action_id": self.plan.action_id,
            "coordinate_name": coordinate_name,
            "decision_packet_sha256": (
                self.decision_packet.decision_packet_sha256
            ),
            "advisor_request_sha256": (
                self.advisor_outcome.request.request_sha256
            ),
            "action_plan_sha256": self.plan.plan_sha256,
            "perception_verdict_sha256": (
                perception_verdict.verdict_sha256
            ),
            "perception_frame_sha256": (
                self.perception_frame_packet.source_image_sha256
            ),
            "execution_frame_sha256": actual_sha,
            "underlying_gate_reason": current_verdict.reason,
        }
        payload["authorization_sha256"] = _canonical_sha(payload)
        return payload

    def _blocked(
        self,
        reason: str,
        *,
        perception_verdict: StructuredActionGateVerdict | None = None,
        execution_frame_sha256: str | None = None,
        underlying_gate_reason: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "allowed": False,
            "reason": reason,
            "action_id": self.plan.action_id,
            "decision_packet_sha256": (
                self.decision_packet.decision_packet_sha256
            ),
            "advisor_request_sha256": (
                self.advisor_outcome.request.request_sha256
            ),
            "action_plan_sha256": self.plan.plan_sha256,
            "perception_verdict_sha256": (
                perception_verdict.verdict_sha256
                if perception_verdict is not None
                else None
            ),
            "perception_frame_sha256": (
                self.perception_frame_packet.source_image_sha256
            ),
            "execution_frame_sha256": execution_frame_sha256,
            "underlying_gate_reason": underlying_gate_reason,
        }
        payload["authorization_sha256"] = _canonical_sha(payload)
        return payload


def _candidate_by_id(
    packet: TurnDecisionPacket,
    action_id: str,
):
    for candidate in packet.candidates:
        if candidate.action_id == action_id:
            return candidate
    return None


def _validate_local_decision_shape(
    decision: dict[str, Any],
    *,
    selected_action_id: str,
) -> None:
    action = decision.get("action")
    if action == "tap":
        if set(decision) != {
            "action",
            "reason",
            "structured_action_id",
            "tap",
        }:
            raise StructuredActionGateError(
                "structured_plan_decision_invalid"
            )
        tap = decision.get("tap")
        if not isinstance(tap, dict) or set(tap) != {"coordinate_name"}:
            raise StructuredActionGateError(
                "structured_plan_decision_invalid"
            )
        allowed_targets = {
            value
            for value in (
                DEFAULT_ACTION_COORDINATE_NAMES.get(selected_action_id),
                EXECUTION_ACTION_COORDINATE_NAMES.get(
                    selected_action_id
                ),
            )
            if value is not None
        }
        if not allowed_targets:
            raise StructuredActionGateError(
                "structured_action_target_unregistered"
            )
        if tap.get("coordinate_name") not in allowed_targets:
            raise StructuredActionGateError(
                "structured_action_target_mismatch"
            )
    elif action == "wait":
        if set(decision) != {
            "action",
            "reason",
            "structured_action_id",
        }:
            raise StructuredActionGateError(
                "structured_plan_decision_invalid"
            )
    else:
        raise StructuredActionGateError(
            "structured_plan_decision_invalid"
        )
    if (
        decision.get("structured_action_id") != selected_action_id
        or not isinstance(decision.get("reason"), str)
        or not decision["reason"].strip()
    ):
        raise StructuredActionGateError(
            "structured_plan_action_mismatch"
        )


def _verdict(
    *,
    status: str,
    execution_allowed: bool,
    reason: str,
    action_id: str | None,
    decision_packet: TurnDecisionPacket,
    advisor_outcome: StructuredAdvisorOutcome,
    plan: StructuredActionPlan,
    frame_packet: FrameSafetyPacket,
    underlying_gate_reason: str | None,
) -> StructuredActionGateVerdict:
    payload = {
        "schema_version": "uma.structured_action_gate_verdict.v1",
        "status": status,
        "execution_allowed": execution_allowed,
        "reason": reason,
        "authority": "local_formal_action_safety_gate",
        "action_id": action_id,
        "decision_packet_sha256": decision_packet.decision_packet_sha256,
        "advisor_request_sha256": advisor_outcome.request.request_sha256,
        "action_plan_sha256": plan.plan_sha256,
        "source_frame_sha256": frame_packet.source_image_sha256,
        "underlying_gate_reason": underlying_gate_reason,
    }
    payload["verdict_sha256"] = _canonical_sha(payload)
    return StructuredActionGateVerdict(**payload)


def _canonical_sha(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
