from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


AUTHORITY_SCHEMA = "uma.career_supervisor_turn_authority.v1"
DECISION_SCHEMA = "uma.career_supervisor_turn_decision.v1"
_SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ACTION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,95}$")
_OPTIONAL_AUTHORITY_FIELDS = frozenset(
    {
        "condition_authority_sha256",
        "goal_ledger_sha256",
        "race_plan_sha256",
        "training_packet_sha256",
        "scenario_packet_sha256",
    }
)
_HAZARD_FLAGS = frozenset(
    {
        "resource.prompt",
        "paid.prompt",
        "shop",
        "gacha",
        "carrot.prompt",
        "clock.prompt",
        "unknown.confirmation",
    }
)
_ACTION_KINDS = frozenset(
    {
        "training",
        "race",
        "rest",
        "outing",
        "clinic",
        "event",
        "scenario_hold",
        "scenario_use",
        "stop",
    }
)
_SAFETY_PREREQUISITES = frozenset(
    {
        "fresh_frame",
        "same_run_session",
        "same_generation",
        "packet_sha_match",
        "safety_gate",
    }
)


class TurnPacketError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TurnAuthorityBundle:
    schema_version: str
    run_session_id: str
    generation: int
    global_action_turn: int
    game_period: str
    source_frame_sha256: str
    perception_packet_sha256: str
    condition_authority_sha256: str | None
    goal_ledger_sha256: str | None
    race_plan_sha256: str | None
    training_packet_sha256: str | None
    scenario_packet_sha256: str | None
    unknown_fields: tuple[str, ...]
    hazard_flags: tuple[str, ...]
    bundle_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != AUTHORITY_SCHEMA:
            raise TurnPacketError("authority_schema_invalid")
        _require_text(self.run_session_id, "run_session_id_invalid")
        _require_text(self.game_period, "game_period_invalid")
        if type(self.generation) is not int or self.generation <= 0:
            raise TurnPacketError("generation_invalid")
        if (
            type(self.global_action_turn) is not int
            or self.global_action_turn < 0
        ):
            raise TurnPacketError("global_action_turn_invalid")
        _require_sha(self.source_frame_sha256, "authority_sha_invalid")
        _require_sha(self.perception_packet_sha256, "authority_sha_invalid")
        authority_values = {
            field_name: getattr(self, field_name)
            for field_name in _OPTIONAL_AUTHORITY_FIELDS
        }
        for value in authority_values.values():
            if value is not None:
                _require_sha(value, "authority_sha_invalid")
        _validate_name_tuple(
            self.unknown_fields,
            allowed=_OPTIONAL_AUTHORITY_FIELDS,
            code="authority_unknown_fields_invalid",
        )
        for field_name, value in authority_values.items():
            if (value is None) != (field_name in self.unknown_fields):
                raise TurnPacketError("authority_unknown_contract_invalid")
        _validate_name_tuple(
            self.hazard_flags,
            allowed=_HAZARD_FLAGS,
            code="authority_hazard_flags_invalid",
        )
        _require_sha(self.bundle_sha256, "authority_bundle_sha_invalid")
        material = self.to_dict()
        material.pop("bundle_sha256")
        if _canonical_sha(material) != self.bundle_sha256:
            raise TurnPacketError("authority_bundle_sha_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_session_id": self.run_session_id,
            "generation": self.generation,
            "global_action_turn": self.global_action_turn,
            "game_period": self.game_period,
            "source_frame_sha256": self.source_frame_sha256,
            "perception_packet_sha256": self.perception_packet_sha256,
            "condition_authority_sha256": self.condition_authority_sha256,
            "goal_ledger_sha256": self.goal_ledger_sha256,
            "race_plan_sha256": self.race_plan_sha256,
            "training_packet_sha256": self.training_packet_sha256,
            "scenario_packet_sha256": self.scenario_packet_sha256,
            "unknown_fields": list(self.unknown_fields),
            "hazard_flags": list(self.hazard_flags),
            "bundle_sha256": self.bundle_sha256,
        }


@dataclass(frozen=True)
class CandidateAction:
    action_id: str
    action_kind: str
    consumes_primary_turn: bool
    allowed: bool
    priority: int
    source_packet_sha256s: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    requires_resource: bool
    expected_failure_rate: int | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.action_id, str)
            or _ACTION_ID_PATTERN.fullmatch(self.action_id) is None
        ):
            raise TurnPacketError("candidate_action_id_invalid")
        if self.action_kind not in _ACTION_KINDS:
            raise TurnPacketError("candidate_action_kind_invalid")
        if type(self.consumes_primary_turn) is not bool:
            raise TurnPacketError("candidate_primary_flag_invalid")
        if type(self.allowed) is not bool:
            raise TurnPacketError("candidate_allowed_invalid")
        if type(self.requires_resource) is not bool:
            raise TurnPacketError("candidate_resource_flag_invalid")
        if type(self.priority) is not int:
            raise TurnPacketError("candidate_priority_invalid")
        _validate_sha_tuple(
            self.source_packet_sha256s,
            "candidate_source_sha_invalid",
        )
        _validate_reason_tuple(
            self.rejection_reasons,
            "candidate_rejection_reasons_invalid",
        )
        if self.requires_resource and self.allowed:
            raise TurnPacketError("resource_candidate_cannot_be_allowed")
        if not self.allowed and not self.rejection_reasons:
            raise TurnPacketError("candidate_rejection_reason_required")
        if self.allowed and self.rejection_reasons:
            raise TurnPacketError("allowed_candidate_has_rejection_reason")
        if self.expected_failure_rate is not None and (
            type(self.expected_failure_rate) is not int
            or not 0 <= self.expected_failure_rate <= 100
        ):
            raise TurnPacketError("candidate_failure_rate_invalid")
        if (
            self.action_kind == "training"
            and self.allowed
            and self.expected_failure_rate is None
        ):
            raise TurnPacketError("candidate_failure_rate_required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_kind": self.action_kind,
            "consumes_primary_turn": self.consumes_primary_turn,
            "allowed": self.allowed,
            "priority": self.priority,
            "source_packet_sha256s": list(self.source_packet_sha256s),
            "rejection_reasons": list(self.rejection_reasons),
            "requires_resource": self.requires_resource,
            "expected_failure_rate": self.expected_failure_rate,
        }


@dataclass(frozen=True)
class TurnDecisionPacket:
    schema_version: str
    run_session_id: str
    generation: int
    global_action_turn: int
    authority_bundle_sha256: str
    candidates: tuple[CandidateAction, ...]
    local_preferred_action_id: str
    selected_action_id: str
    rejected_action_ids: tuple[str, ...]
    safety_prerequisites: tuple[str, ...]
    decision_packet_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != DECISION_SCHEMA:
            raise TurnPacketError("decision_schema_invalid")
        _require_text(self.run_session_id, "run_session_id_invalid")
        if type(self.generation) is not int or self.generation <= 0:
            raise TurnPacketError("generation_invalid")
        if (
            type(self.global_action_turn) is not int
            or self.global_action_turn < 0
        ):
            raise TurnPacketError("global_action_turn_invalid")
        _require_sha(
            self.authority_bundle_sha256,
            "decision_authority_sha_invalid",
        )
        if not isinstance(self.candidates, tuple) or not self.candidates:
            raise TurnPacketError("candidates_required")
        if any(
            not isinstance(candidate, CandidateAction)
            for candidate in self.candidates
        ):
            raise TurnPacketError("candidate_invalid")
        action_ids = tuple(candidate.action_id for candidate in self.candidates)
        if len(set(action_ids)) != len(action_ids):
            raise TurnPacketError("candidate_action_id_duplicate")
        if self.local_preferred_action_id not in action_ids:
            raise TurnPacketError("local_preferred_action_unknown")
        if self.selected_action_id != self.local_preferred_action_id:
            raise TurnPacketError("decision_selected_action_mismatch")
        preferred = self.candidates[
            action_ids.index(self.local_preferred_action_id)
        ]
        if not preferred.allowed:
            raise TurnPacketError("local_preferred_action_not_allowed")
        expected_rejected = tuple(
            candidate.action_id
            for candidate in self.candidates
            if not candidate.allowed
        )
        if self.rejected_action_ids != expected_rejected:
            raise TurnPacketError("decision_rejected_actions_mismatch")
        _validate_name_tuple(
            self.safety_prerequisites,
            allowed=_SAFETY_PREREQUISITES,
            code="safety_prerequisites_invalid",
        )
        if "safety_gate" not in self.safety_prerequisites:
            raise TurnPacketError("safety_gate_prerequisite_required")
        for candidate in self.candidates:
            if self.authority_bundle_sha256 not in (
                candidate.source_packet_sha256s
            ):
                raise TurnPacketError("candidate_authority_binding_missing")
        _require_sha(
            self.decision_packet_sha256,
            "decision_packet_sha_invalid",
        )
        material = self.to_dict()
        material.pop("decision_packet_sha256")
        if _canonical_sha(material) != self.decision_packet_sha256:
            raise TurnPacketError("decision_packet_sha_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_session_id": self.run_session_id,
            "generation": self.generation,
            "global_action_turn": self.global_action_turn,
            "authority_bundle_sha256": self.authority_bundle_sha256,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "local_preferred_action_id": self.local_preferred_action_id,
            "selected_action_id": self.selected_action_id,
            "rejected_action_ids": list(self.rejected_action_ids),
            "safety_prerequisites": list(self.safety_prerequisites),
            "decision_packet_sha256": self.decision_packet_sha256,
        }


def build_turn_authority_bundle(
    *,
    run_session_id: str,
    generation: int,
    global_action_turn: int,
    game_period: str,
    source_frame_sha256: str,
    perception_packet_sha256: str,
    condition_authority_sha256: str | None,
    goal_ledger_sha256: str | None,
    race_plan_sha256: str | None,
    training_packet_sha256: str | None,
    scenario_packet_sha256: str | None,
    unknown_fields: tuple[str, ...],
    hazard_flags: tuple[str, ...],
) -> TurnAuthorityBundle:
    _require_text(run_session_id, "run_session_id_invalid")
    _require_text(game_period, "game_period_invalid")
    if type(generation) is not int or generation <= 0:
        raise TurnPacketError("generation_invalid")
    if type(global_action_turn) is not int or global_action_turn < 0:
        raise TurnPacketError("global_action_turn_invalid")
    _require_sha(source_frame_sha256, "authority_sha_invalid")
    _require_sha(perception_packet_sha256, "authority_sha_invalid")
    authority_values = {
        "condition_authority_sha256": condition_authority_sha256,
        "goal_ledger_sha256": goal_ledger_sha256,
        "race_plan_sha256": race_plan_sha256,
        "training_packet_sha256": training_packet_sha256,
        "scenario_packet_sha256": scenario_packet_sha256,
    }
    for value in authority_values.values():
        if value is not None:
            _require_sha(value, "authority_sha_invalid")
    _validate_name_tuple(
        unknown_fields,
        allowed=_OPTIONAL_AUTHORITY_FIELDS,
        code="authority_unknown_fields_invalid",
    )
    unknown = set(unknown_fields)
    for field_name, value in authority_values.items():
        if (value is None) != (field_name in unknown):
            raise TurnPacketError("authority_unknown_contract_invalid")
    _validate_name_tuple(
        hazard_flags,
        allowed=_HAZARD_FLAGS,
        code="authority_hazard_flags_invalid",
    )
    payload: dict[str, Any] = {
        "schema_version": AUTHORITY_SCHEMA,
        "run_session_id": run_session_id,
        "generation": generation,
        "global_action_turn": global_action_turn,
        "game_period": game_period,
        "source_frame_sha256": source_frame_sha256,
        "perception_packet_sha256": perception_packet_sha256,
        **authority_values,
        "unknown_fields": list(unknown_fields),
        "hazard_flags": list(hazard_flags),
    }
    payload["bundle_sha256"] = _canonical_sha(payload)
    return TurnAuthorityBundle(
        schema_version=AUTHORITY_SCHEMA,
        run_session_id=run_session_id,
        generation=generation,
        global_action_turn=global_action_turn,
        game_period=game_period,
        source_frame_sha256=source_frame_sha256,
        perception_packet_sha256=perception_packet_sha256,
        condition_authority_sha256=condition_authority_sha256,
        goal_ledger_sha256=goal_ledger_sha256,
        race_plan_sha256=race_plan_sha256,
        training_packet_sha256=training_packet_sha256,
        scenario_packet_sha256=scenario_packet_sha256,
        unknown_fields=unknown_fields,
        hazard_flags=hazard_flags,
        bundle_sha256=payload["bundle_sha256"],
    )


def build_turn_decision_packet(
    *,
    authority: TurnAuthorityBundle,
    candidates: tuple[CandidateAction, ...],
    local_preferred_action_id: str,
    safety_prerequisites: tuple[str, ...],
) -> TurnDecisionPacket:
    if not isinstance(authority, TurnAuthorityBundle):
        raise TurnPacketError("turn_authority_required")
    if not isinstance(candidates, tuple) or not candidates:
        raise TurnPacketError("candidates_required")
    if any(not isinstance(item, CandidateAction) for item in candidates):
        raise TurnPacketError("candidate_invalid")
    action_ids = tuple(item.action_id for item in candidates)
    if len(set(action_ids)) != len(action_ids):
        raise TurnPacketError("candidate_action_id_duplicate")
    for candidate in candidates:
        if authority.bundle_sha256 not in candidate.source_packet_sha256s:
            raise TurnPacketError("candidate_authority_binding_missing")
        _validate_candidate_authorities(authority, candidate)
    if local_preferred_action_id not in action_ids:
        raise TurnPacketError("local_preferred_action_unknown")
    preferred = candidates[action_ids.index(local_preferred_action_id)]
    if not preferred.allowed:
        raise TurnPacketError("local_preferred_action_not_allowed")
    if not isinstance(safety_prerequisites, tuple):
        raise TurnPacketError("safety_prerequisites_invalid")
    normalized_safety = tuple(sorted(safety_prerequisites))
    _validate_name_tuple(
        normalized_safety,
        allowed=_SAFETY_PREREQUISITES,
        code="safety_prerequisites_invalid",
    )
    if "safety_gate" not in normalized_safety:
        raise TurnPacketError("safety_gate_prerequisite_required")
    if authority.hazard_flags and (
        len(candidates) != 1
        or preferred.action_kind != "stop"
        or preferred.consumes_primary_turn
    ):
        raise TurnPacketError("hazard_requires_stop_only")
    rejected_action_ids = tuple(
        item.action_id for item in candidates if not item.allowed
    )
    payload: dict[str, Any] = {
        "schema_version": DECISION_SCHEMA,
        "run_session_id": authority.run_session_id,
        "generation": authority.generation,
        "global_action_turn": authority.global_action_turn,
        "authority_bundle_sha256": authority.bundle_sha256,
        "candidates": [item.to_dict() for item in candidates],
        "local_preferred_action_id": local_preferred_action_id,
        "selected_action_id": local_preferred_action_id,
        "rejected_action_ids": list(rejected_action_ids),
        "safety_prerequisites": list(normalized_safety),
    }
    payload["decision_packet_sha256"] = _canonical_sha(payload)
    return TurnDecisionPacket(
        schema_version=DECISION_SCHEMA,
        run_session_id=authority.run_session_id,
        generation=authority.generation,
        global_action_turn=authority.global_action_turn,
        authority_bundle_sha256=authority.bundle_sha256,
        candidates=candidates,
        local_preferred_action_id=local_preferred_action_id,
        selected_action_id=local_preferred_action_id,
        rejected_action_ids=rejected_action_ids,
        safety_prerequisites=normalized_safety,
        decision_packet_sha256=payload["decision_packet_sha256"],
    )


def _validate_candidate_authorities(
    authority: TurnAuthorityBundle,
    candidate: CandidateAction,
) -> None:
    required_fields: tuple[str, ...] = ()
    if candidate.action_kind == "training":
        required_fields = ("training_packet_sha256",)
    elif candidate.action_kind == "race":
        required_fields = ("goal_ledger_sha256", "race_plan_sha256")
    elif candidate.action_kind in {"rest", "outing", "clinic"}:
        required_fields = ("condition_authority_sha256",)
    elif candidate.action_kind == "scenario_use":
        required_fields = ("scenario_packet_sha256", "training_packet_sha256")
    for field_name in required_fields:
        if getattr(authority, field_name) is None and candidate.allowed:
            raise TurnPacketError("candidate_missing_authority")


def _validate_sha_tuple(value: object, code: str) -> None:
    if (
        not isinstance(value, tuple)
        or not value
        or len(set(value)) != len(value)
    ):
        raise TurnPacketError(code)
    for item in value:
        _require_sha(item, code)


def _validate_reason_tuple(value: object, code: str) -> None:
    if (
        not isinstance(value, tuple)
        or len(set(value)) != len(value)
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise TurnPacketError(code)


def _validate_name_tuple(
    value: object,
    *,
    allowed: frozenset[str],
    code: str,
) -> None:
    if (
        not isinstance(value, tuple)
        or len(set(value)) != len(value)
        or tuple(sorted(value)) != value
        or any(item not in allowed for item in value)
    ):
        raise TurnPacketError(code)


def _require_text(value: object, code: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise TurnPacketError(code)


def _require_sha(value: object, code: str) -> None:
    if not isinstance(value, str) or _SHA_PATTERN.fullmatch(value) is None:
        raise TurnPacketError(code)


def _canonical_sha(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
