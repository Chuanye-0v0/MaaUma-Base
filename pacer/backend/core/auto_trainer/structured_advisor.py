from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from core.auto_trainer.career_supervisor_turn import TurnDecisionPacket


ADVISOR_REQUEST_SCHEMA = "uma.structured_advisor_request.v1"
ADVISOR_RESPONSE_SCHEMA = "uma.structured_advisor_response.v1"
_SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_MODES = frozenset({"observer", "advisor"})
_INVALID_POLICIES = frozenset({"local_fallback", "stop"})
_MAX_REQUEST_TTL_MS = 60_000
_MAX_CONTEXT_DEPTH = 6
_MAX_CONTEXT_NODES = 512
_MAX_CONTEXT_STRING_CHARS = 1_024
_MAX_CONTEXT_BYTES = 65_536
_CONTEXT_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$")
_FORBIDDEN_CONTEXT_KEY_PARTS = (
    "screenshot",
    "image",
    "capture",
    "coordinate",
    "bbox",
    "roi",
    "tap",
    "adb",
    "path",
    "raw_data",
    "pixels",
)
_RESPONSE_FIELDS = frozenset(
    {
        "schema_version",
        "request_sha256",
        "decision_packet_sha256",
        "run_session_id",
        "generation",
        "global_action_turn",
        "action_id",
        "model_id",
    }
)


class StructuredAdvisorError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AdvisorCandidateView:
    action_id: str
    action_kind: str
    priority: int
    consumes_primary_turn: bool
    expected_failure_rate: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_kind": self.action_kind,
            "priority": self.priority,
            "consumes_primary_turn": self.consumes_primary_turn,
            "expected_failure_rate": self.expected_failure_rate,
        }


@dataclass(frozen=True)
class StructuredAdvisorRequest:
    schema_version: str
    mode: Literal["observer", "advisor"]
    run_session_id: str
    generation: int
    global_action_turn: int
    decision_packet_sha256: str
    expected_model_id: str
    decision_context_json: str
    decision_context_sha256: str
    local_preferred_action_id: str
    allowed_candidates: tuple[AdvisorCandidateView, ...]
    issued_at_unix_ms: int
    expires_at_unix_ms: int
    request_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != ADVISOR_REQUEST_SCHEMA:
            raise StructuredAdvisorError("advisor_request_schema_invalid")
        if self.mode not in _MODES:
            raise StructuredAdvisorError("advisor_mode_invalid")
        _require_text(self.run_session_id, "advisor_run_session_invalid")
        if type(self.generation) is not int or self.generation <= 0:
            raise StructuredAdvisorError("advisor_generation_invalid")
        if (
            type(self.global_action_turn) is not int
            or self.global_action_turn < 0
        ):
            raise StructuredAdvisorError("advisor_global_turn_invalid")
        _require_sha(
            self.decision_packet_sha256,
            "advisor_decision_sha_invalid",
        )
        if (
            not isinstance(self.expected_model_id, str)
            or _MODEL_ID_PATTERN.fullmatch(self.expected_model_id) is None
        ):
            raise StructuredAdvisorError("advisor_model_id_invalid")
        try:
            decoded_context = json.loads(self.decision_context_json)
            projected_context = _project_context(decoded_context)
        except (json.JSONDecodeError, StructuredAdvisorError):
            raise StructuredAdvisorError("advisor_context_invalid") from None
        canonical_context_json = json.dumps(
            projected_context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if (
            canonical_context_json != self.decision_context_json
            or len(canonical_context_json.encode("utf-8"))
            > _MAX_CONTEXT_BYTES
        ):
            raise StructuredAdvisorError("advisor_context_invalid")
        _require_sha(
            self.decision_context_sha256,
            "advisor_context_sha_invalid",
        )
        if (
            _canonical_sha(projected_context)
            != self.decision_context_sha256
        ):
            raise StructuredAdvisorError("advisor_context_sha_mismatch")
        _require_text(
            self.local_preferred_action_id,
            "advisor_local_action_invalid",
        )
        if (
            not isinstance(self.allowed_candidates, tuple)
            or not self.allowed_candidates
            or any(
                not isinstance(item, AdvisorCandidateView)
                for item in self.allowed_candidates
            )
        ):
            raise StructuredAdvisorError("advisor_candidates_invalid")
        action_ids = tuple(item.action_id for item in self.allowed_candidates)
        if len(set(action_ids)) != len(action_ids):
            raise StructuredAdvisorError("advisor_candidate_duplicate")
        if self.local_preferred_action_id not in action_ids:
            raise StructuredAdvisorError("advisor_local_action_not_allowed")
        if (
            type(self.issued_at_unix_ms) is not int
            or self.issued_at_unix_ms < 0
            or type(self.expires_at_unix_ms) is not int
            or self.expires_at_unix_ms <= self.issued_at_unix_ms
            or (
                self.expires_at_unix_ms - self.issued_at_unix_ms
                > _MAX_REQUEST_TTL_MS
            )
        ):
            raise StructuredAdvisorError("advisor_request_expiry_invalid")
        _require_sha(self.request_sha256, "advisor_request_sha_invalid")
        material = self.to_dict()
        material.pop("request_sha256")
        if _canonical_sha(material) != self.request_sha256:
            raise StructuredAdvisorError("advisor_request_sha_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "run_session_id": self.run_session_id,
            "generation": self.generation,
            "global_action_turn": self.global_action_turn,
            "decision_packet_sha256": self.decision_packet_sha256,
            "expected_model_id": self.expected_model_id,
            "decision_context": json.loads(self.decision_context_json),
            "decision_context_sha256": self.decision_context_sha256,
            "local_preferred_action_id": self.local_preferred_action_id,
            "allowed_candidates": [
                item.to_dict() for item in self.allowed_candidates
            ],
            "issued_at_unix_ms": self.issued_at_unix_ms,
            "expires_at_unix_ms": self.expires_at_unix_ms,
            "request_sha256": self.request_sha256,
        }


@dataclass(frozen=True)
class StructuredAdvisorResolution:
    status: Literal[
        "observed",
        "advisor_selected",
        "local_fallback",
        "safe_stop",
    ]
    reason: str
    selected_action_id: str | None
    local_preferred_action_id: str
    advisor_action_id: str | None
    advisor_selection_applied: bool
    decision_packet_sha256: str
    request_sha256: str
    safety_gate_required: bool = True
    execution_authorized: bool = False

    def __post_init__(self) -> None:
        if not self.safety_gate_required:
            raise StructuredAdvisorError("advisor_safety_gate_required")
        if self.execution_authorized:
            raise StructuredAdvisorError(
                "advisor_cannot_authorize_execution"
            )


def build_structured_advisor_request(
    *,
    decision_packet: TurnDecisionPacket,
    mode: Literal["observer", "advisor"],
    expected_model_id: str,
    decision_context: Mapping[str, object] | None = None,
    issued_at_unix_ms: int,
    expires_at_unix_ms: int,
) -> StructuredAdvisorRequest:
    if not isinstance(decision_packet, TurnDecisionPacket):
        raise StructuredAdvisorError("advisor_decision_packet_required")
    if mode not in _MODES:
        raise StructuredAdvisorError("advisor_mode_invalid")
    if (
        not isinstance(expected_model_id, str)
        or _MODEL_ID_PATTERN.fullmatch(expected_model_id) is None
    ):
        raise StructuredAdvisorError("advisor_model_id_invalid")
    if (
        type(issued_at_unix_ms) is not int
        or issued_at_unix_ms < 0
        or type(expires_at_unix_ms) is not int
        or expires_at_unix_ms <= issued_at_unix_ms
        or expires_at_unix_ms - issued_at_unix_ms > _MAX_REQUEST_TTL_MS
    ):
        raise StructuredAdvisorError("advisor_request_expiry_invalid")
    projected_context = _project_context(
        {} if decision_context is None else decision_context
    )
    decision_context_json = json.dumps(
        projected_context,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(decision_context_json.encode("utf-8")) > _MAX_CONTEXT_BYTES:
        raise StructuredAdvisorError("advisor_context_invalid")
    decision_context_sha256 = _canonical_sha(projected_context)
    allowed_candidates = tuple(
        AdvisorCandidateView(
            action_id=item.action_id,
            action_kind=item.action_kind,
            priority=item.priority,
            consumes_primary_turn=item.consumes_primary_turn,
            expected_failure_rate=item.expected_failure_rate,
        )
        for item in decision_packet.candidates
        if item.allowed and not item.requires_resource
    )
    if not allowed_candidates:
        raise StructuredAdvisorError("advisor_candidates_required")
    payload: dict[str, Any] = {
        "schema_version": ADVISOR_REQUEST_SCHEMA,
        "mode": mode,
        "run_session_id": decision_packet.run_session_id,
        "generation": decision_packet.generation,
        "global_action_turn": decision_packet.global_action_turn,
        "decision_packet_sha256": (
            decision_packet.decision_packet_sha256
        ),
        "expected_model_id": expected_model_id,
        "decision_context": projected_context,
        "decision_context_sha256": decision_context_sha256,
        "local_preferred_action_id": (
            decision_packet.local_preferred_action_id
        ),
        "allowed_candidates": [
            item.to_dict() for item in allowed_candidates
        ],
        "issued_at_unix_ms": issued_at_unix_ms,
        "expires_at_unix_ms": expires_at_unix_ms,
    }
    payload["request_sha256"] = _canonical_sha(payload)
    return StructuredAdvisorRequest(
        schema_version=ADVISOR_REQUEST_SCHEMA,
        mode=mode,
        run_session_id=decision_packet.run_session_id,
        generation=decision_packet.generation,
        global_action_turn=decision_packet.global_action_turn,
        decision_packet_sha256=decision_packet.decision_packet_sha256,
        expected_model_id=expected_model_id,
        decision_context_json=decision_context_json,
        decision_context_sha256=decision_context_sha256,
        local_preferred_action_id=(
            decision_packet.local_preferred_action_id
        ),
        allowed_candidates=allowed_candidates,
        issued_at_unix_ms=issued_at_unix_ms,
        expires_at_unix_ms=expires_at_unix_ms,
        request_sha256=payload["request_sha256"],
    )


def resolve_structured_advisor_response(
    *,
    request: StructuredAdvisorRequest,
    raw_response: Mapping[str, object] | None,
    now_unix_ms: int,
    invalid_response_policy: Literal["local_fallback", "stop"] = (
        "local_fallback"
    ),
) -> StructuredAdvisorResolution:
    if not isinstance(request, StructuredAdvisorRequest):
        raise StructuredAdvisorError("advisor_request_required")
    if type(now_unix_ms) is not int or now_unix_ms < 0:
        raise StructuredAdvisorError("advisor_now_invalid")
    if invalid_response_policy not in _INVALID_POLICIES:
        raise StructuredAdvisorError("advisor_invalid_policy")
    if (
        len(request.allowed_candidates) == 1
        and request.allowed_candidates[0].action_kind == "stop"
    ):
        advisor_action_id = None
        if isinstance(raw_response, Mapping):
            raw_action = raw_response.get("action_id")
            if isinstance(raw_action, str):
                advisor_action_id = raw_action
        return _invalid_resolution(
            request,
            reason="stop_only_local_authority",
            policy="local_fallback",
            advisor_action_id=advisor_action_id,
        )
    if raw_response is None:
        return _invalid_resolution(
            request,
            reason="advisor_unavailable",
            policy=invalid_response_policy,
        )
    parsed, error = _parse_response(raw_response)
    if error is not None:
        return _invalid_resolution(
            request,
            reason=error,
            policy=invalid_response_policy,
        )
    assert parsed is not None
    checks = (
        (
            parsed["request_sha256"] == request.request_sha256,
            "advisor_request_sha_mismatch",
        ),
        (
            parsed["decision_packet_sha256"]
            == request.decision_packet_sha256,
            "advisor_decision_sha_mismatch",
        ),
        (
            parsed["run_session_id"] == request.run_session_id,
            "advisor_run_session_mismatch",
        ),
        (
            parsed["generation"] == request.generation,
            "advisor_generation_mismatch",
        ),
        (
            parsed["global_action_turn"] == request.global_action_turn,
            "advisor_global_turn_mismatch",
        ),
        (
            parsed["model_id"] == request.expected_model_id,
            "advisor_model_mismatch",
        ),
        (
            now_unix_ms >= request.issued_at_unix_ms,
            "advisor_response_before_issue",
        ),
        (
            now_unix_ms <= request.expires_at_unix_ms,
            "advisor_response_expired",
        ),
    )
    for accepted, reason in checks:
        if not accepted:
            return _invalid_resolution(
                request,
                reason=reason,
                policy=invalid_response_policy,
                advisor_action_id=str(parsed["action_id"]),
            )
    action_id = str(parsed["action_id"])
    allowed_ids = tuple(
        item.action_id for item in request.allowed_candidates
    )
    if action_id not in allowed_ids:
        return _invalid_resolution(
            request,
            reason="advisor_action_not_allowed",
            policy=invalid_response_policy,
            advisor_action_id=action_id,
        )
    if request.mode == "observer":
        return StructuredAdvisorResolution(
            status="observed",
            reason="observer_selection_not_applied",
            selected_action_id=request.local_preferred_action_id,
            local_preferred_action_id=request.local_preferred_action_id,
            advisor_action_id=action_id,
            advisor_selection_applied=False,
            decision_packet_sha256=request.decision_packet_sha256,
            request_sha256=request.request_sha256,
        )
    return StructuredAdvisorResolution(
        status="advisor_selected",
        reason="advisor_allowed_action_selected",
        selected_action_id=action_id,
        local_preferred_action_id=request.local_preferred_action_id,
        advisor_action_id=action_id,
        advisor_selection_applied=True,
        decision_packet_sha256=request.decision_packet_sha256,
        request_sha256=request.request_sha256,
    )


def _parse_response(
    value: object,
) -> tuple[dict[str, object] | None, str | None]:
    if not isinstance(value, Mapping) or set(value) != _RESPONSE_FIELDS:
        return None, "advisor_response_schema_invalid"
    parsed = dict(value)
    if parsed["schema_version"] != ADVISOR_RESPONSE_SCHEMA:
        return None, "advisor_response_schema_invalid"
    try:
        _require_sha(
            parsed["request_sha256"],
            "advisor_response_schema_invalid",
        )
        _require_sha(
            parsed["decision_packet_sha256"],
            "advisor_response_schema_invalid",
        )
        _require_text(
            parsed["run_session_id"],
            "advisor_response_schema_invalid",
        )
        _require_text(
            parsed["action_id"],
            "advisor_response_schema_invalid",
        )
    except StructuredAdvisorError:
        return None, "advisor_response_schema_invalid"
    if (
        type(parsed["generation"]) is not int
        or parsed["generation"] <= 0
        or type(parsed["global_action_turn"]) is not int
        or parsed["global_action_turn"] < 0
        or not isinstance(parsed["model_id"], str)
        or _MODEL_ID_PATTERN.fullmatch(parsed["model_id"]) is None
    ):
        return None, "advisor_response_schema_invalid"
    return parsed, None


def _invalid_resolution(
    request: StructuredAdvisorRequest,
    *,
    reason: str,
    policy: str,
    advisor_action_id: str | None = None,
) -> StructuredAdvisorResolution:
    if policy == "stop":
        return StructuredAdvisorResolution(
            status="safe_stop",
            reason=reason,
            selected_action_id=None,
            local_preferred_action_id=request.local_preferred_action_id,
            advisor_action_id=advisor_action_id,
            advisor_selection_applied=False,
            decision_packet_sha256=request.decision_packet_sha256,
            request_sha256=request.request_sha256,
        )
    return StructuredAdvisorResolution(
        status="local_fallback",
        reason=reason,
        selected_action_id=request.local_preferred_action_id,
        local_preferred_action_id=request.local_preferred_action_id,
        advisor_action_id=advisor_action_id,
        advisor_selection_applied=False,
        decision_packet_sha256=request.decision_packet_sha256,
        request_sha256=request.request_sha256,
    )


def _canonical_sha(payload: Mapping[str, object]) -> str:
    material = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _project_context(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise StructuredAdvisorError("advisor_context_invalid")
    counter = [0]
    projected = _project_context_value(value, depth=0, counter=counter)
    if not isinstance(projected, dict):
        raise StructuredAdvisorError("advisor_context_invalid")
    return projected


def _project_context_value(
    value: object,
    *,
    depth: int,
    counter: list[int],
) -> object:
    counter[0] += 1
    if counter[0] > _MAX_CONTEXT_NODES or depth > _MAX_CONTEXT_DEPTH:
        raise StructuredAdvisorError("advisor_context_invalid")
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise StructuredAdvisorError("advisor_context_invalid")
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if (
            len(value) > _MAX_CONTEXT_STRING_CHARS
            or re.match(r"^[a-z]:[\\/]", lowered) is not None
            or value.startswith(("/", "\\"))
            or "http://" in lowered
            or "https://" in lowered
            or lowered.endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".bmp")
            )
        ):
            raise StructuredAdvisorError("advisor_context_invalid")
        return value
    if isinstance(value, Mapping):
        if len(value) > 64:
            raise StructuredAdvisorError("advisor_context_invalid")
        result: dict[str, object] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or _CONTEXT_KEY_PATTERN.fullmatch(key) is None
                or key.lower() in {"x", "y"}
                or key.lower() == "raw"
                or key.lower().startswith("raw_")
                or any(
                    part in key.lower()
                    for part in _FORBIDDEN_CONTEXT_KEY_PARTS
                )
            ):
                raise StructuredAdvisorError("advisor_context_invalid")
            result[key] = _project_context_value(
                item,
                depth=depth + 1,
                counter=counter,
            )
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 64:
            raise StructuredAdvisorError("advisor_context_invalid")
        return [
            _project_context_value(
                item,
                depth=depth + 1,
                counter=counter,
            )
            for item in value
        ]
    raise StructuredAdvisorError("advisor_context_invalid")


def _require_sha(value: object, code: str) -> None:
    if not isinstance(value, str) or _SHA_PATTERN.fullmatch(value) is None:
        raise StructuredAdvisorError(code)


def _require_text(value: object, code: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise StructuredAdvisorError(code)
