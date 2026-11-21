from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Literal, Mapping, Protocol


SCENARIO_PACKET_SCHEMA = "uma.scenario_packet.v1"

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ACTION_ID_RE = re.compile(r"scenario\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\Z")
_FORBIDDEN_KEYS = frozenset(
    {
        "image",
        "image_path",
        "screenshot",
        "coordinate",
        "coordinates",
        "x",
        "y",
        "tap",
        "shell",
        "command",
        "screenshot_path",
        "image_bytes",
        "adb",
        "adb_command",
        "api_key",
        "token",
        "env",
        "environment",
        "environment_variables",
    }
)


class ScenarioContractError(ValueError):
    """Raised when a scenario packet or decision violates the pure contract."""


def _canonical_json_bytes(value: object) -> bytes:
    primitive = value.to_dict() if hasattr(value, "to_dict") else _plain_json(value)
    try:
        return json.dumps(
            primitive,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ScenarioContractError("payload must contain finite JSON values") from exc


def _sha256_payload(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _canonical_json_object(payload: object, label: str) -> Mapping[str, object]:
    if type(payload) is not bytes:
        raise ScenarioContractError(f"{label} must be canonical JSON bytes")
    try:
        decoded = payload.decode("utf-8")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScenarioContractError(f"{label} must be canonical JSON") from exc
    if not isinstance(value, Mapping):
        raise ScenarioContractError(f"{label} must be a canonical JSON object")
    if _canonical_json_bytes(value) != payload:
        raise ScenarioContractError(f"{label} must be canonical JSON")
    return value


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    return value


def _freeze_json(value: object, path: str = "mechanism_state") -> object:
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str or not key:
                raise ScenarioContractError(f"{path} keys must be non-empty strings")
            if key.casefold() in _FORBIDDEN_KEYS:
                raise ScenarioContractError(f"{path}.{key} is forbidden")
            frozen[key] = _freeze_json(item, f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item, f"{path}[]") for item in value)
    if value is None or type(value) in {bool, str, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ScenarioContractError(f"{path} must contain finite JSON values")


def _non_empty_string(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise ScenarioContractError(f"{label} must be a non-empty string")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ScenarioContractError(f"{label} must be a lowercase SHA-256")
    return value


def _integer(value: object, label: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ScenarioContractError(f"{label} must be an integer >= {minimum}")
    return value


def _strings(value: object, label: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise ScenarioContractError(f"{label} must be a string sequence")
    result = tuple(_non_empty_string(item, label) for item in value)
    if not allow_empty and not result:
        raise ScenarioContractError(f"{label} must not be empty")
    if len(set(result)) != len(result):
        raise ScenarioContractError(f"{label} contains duplicates")
    return result


def _action_ids(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    result = _strings(value, label, allow_empty=allow_empty)
    if any(_ACTION_ID_RE.fullmatch(action_id) is None for action_id in result):
        raise ScenarioContractError(f"{label} contains an unknown action id")
    return result


def _confidence(value: object, label: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ScenarioContractError(f"{label} must be finite")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ScenarioContractError(f"{label} must be between 0 and 1")
    return result


@dataclass(frozen=True, kw_only=True)
class ScenarioPacketRequest:
    adapter_id: str
    run_session_id: str
    transition_version: int
    generation: int
    source_frame_sha256: str
    current_node_id: str
    expected_next_node_ids: tuple[str, ...]
    mechanism_state: Mapping[str, object]
    allowed_action_ids: tuple[str, ...]
    critical_unknown_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapter_id", _non_empty_string(self.adapter_id, "adapter_id"))
        object.__setattr__(self, "run_session_id", _non_empty_string(self.run_session_id, "run_session_id"))
        object.__setattr__(
            self,
            "transition_version",
            _integer(self.transition_version, "transition_version", minimum=1),
        )
        object.__setattr__(self, "generation", _integer(self.generation, "generation", minimum=0))
        object.__setattr__(
            self,
            "source_frame_sha256",
            _sha256(self.source_frame_sha256, "source_frame_sha256"),
        )
        object.__setattr__(
            self,
            "current_node_id",
            _non_empty_string(self.current_node_id, "current_node_id"),
        )
        object.__setattr__(
            self,
            "expected_next_node_ids",
            _strings(self.expected_next_node_ids, "expected_next_node_ids"),
        )
        if not isinstance(self.mechanism_state, Mapping):
            raise ScenarioContractError("mechanism_state must be an object")
        object.__setattr__(self, "mechanism_state", _freeze_json(self.mechanism_state))
        object.__setattr__(
            self,
            "allowed_action_ids",
            _action_ids(self.allowed_action_ids, "allowed_action_ids"),
        )
        object.__setattr__(
            self,
            "critical_unknown_fields",
            _strings(self.critical_unknown_fields, "critical_unknown_fields"),
        )


@dataclass(frozen=True, kw_only=True)
class ScenarioPacket:
    adapter_id: str
    run_session_id: str
    transition_version: int
    generation: int
    source_frame_sha256: str
    current_node_id: str
    expected_next_node_ids: tuple[str, ...]
    mechanism_state: Mapping[str, object]
    allowed_action_ids: tuple[str, ...]
    critical_unknown_fields: tuple[str, ...]
    packet_sha256: str

    def __post_init__(self) -> None:
        request = ScenarioPacketRequest(
            adapter_id=self.adapter_id,
            run_session_id=self.run_session_id,
            transition_version=self.transition_version,
            generation=self.generation,
            source_frame_sha256=self.source_frame_sha256,
            current_node_id=self.current_node_id,
            expected_next_node_ids=self.expected_next_node_ids,
            mechanism_state=self.mechanism_state,
            allowed_action_ids=self.allowed_action_ids,
            critical_unknown_fields=self.critical_unknown_fields,
        )
        for field_name in (
            "adapter_id",
            "run_session_id",
            "transition_version",
            "generation",
            "source_frame_sha256",
            "current_node_id",
            "expected_next_node_ids",
            "mechanism_state",
            "allowed_action_ids",
            "critical_unknown_fields",
        ):
            object.__setattr__(self, field_name, getattr(request, field_name))
        object.__setattr__(self, "packet_sha256", _sha256(self.packet_sha256, "packet_sha256"))
        if self.packet_sha256 != _sha256_payload(self._payload_without_sha()):
            raise ScenarioContractError("packet_sha256 does not bind the packet")

    @classmethod
    def from_request(cls, request: ScenarioPacketRequest) -> "ScenarioPacket":
        if not isinstance(request, ScenarioPacketRequest):
            raise ScenarioContractError("request must be ScenarioPacketRequest")
        payload = _packet_payload(request)
        return cls(
            adapter_id=request.adapter_id,
            run_session_id=request.run_session_id,
            transition_version=request.transition_version,
            generation=request.generation,
            source_frame_sha256=request.source_frame_sha256,
            current_node_id=request.current_node_id,
            expected_next_node_ids=request.expected_next_node_ids,
            mechanism_state=request.mechanism_state,
            allowed_action_ids=request.allowed_action_ids,
            critical_unknown_fields=request.critical_unknown_fields,
            packet_sha256=_sha256_payload(payload),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return _packet_payload(self)

    def to_dict(self) -> dict[str, object]:
        return {**self._payload_without_sha(), "packet_sha256": self.packet_sha256}


def scenario_packet_to_canonical_json(packet: ScenarioPacket) -> bytes:
    if not isinstance(packet, ScenarioPacket):
        raise ScenarioContractError("packet must be ScenarioPacket")
    return _canonical_json_bytes(packet.to_dict())


def scenario_packet_from_canonical_json(payload: object) -> ScenarioPacket:
    value = _canonical_json_object(payload, "scenario packet")
    required_keys = {
        "schema_version",
        "adapter_id",
        "run_session_id",
        "transition_version",
        "generation",
        "source_frame_sha256",
        "current_node_id",
        "expected_next_node_ids",
        "mechanism_state",
        "allowed_action_ids",
        "critical_unknown_fields",
        "packet_sha256",
    }
    if set(value) != required_keys:
        raise ScenarioContractError("scenario packet fields are invalid")
    if value.get("schema_version") != SCENARIO_PACKET_SCHEMA:
        raise ScenarioContractError("scenario packet schema is invalid")
    return ScenarioPacket(
        adapter_id=value["adapter_id"],
        run_session_id=value["run_session_id"],
        transition_version=value["transition_version"],
        generation=value["generation"],
        source_frame_sha256=value["source_frame_sha256"],
        current_node_id=value["current_node_id"],
        expected_next_node_ids=value["expected_next_node_ids"],
        mechanism_state=value["mechanism_state"],
        allowed_action_ids=value["allowed_action_ids"],
        critical_unknown_fields=value["critical_unknown_fields"],
        packet_sha256=value["packet_sha256"],
    )


def _packet_payload(value: ScenarioPacketRequest | ScenarioPacket) -> dict[str, object]:
    return {
        "schema_version": SCENARIO_PACKET_SCHEMA,
        "adapter_id": value.adapter_id,
        "run_session_id": value.run_session_id,
        "transition_version": value.transition_version,
        "generation": value.generation,
        "source_frame_sha256": value.source_frame_sha256,
        "current_node_id": value.current_node_id,
        "expected_next_node_ids": list(value.expected_next_node_ids),
        "mechanism_state": _plain_json(value.mechanism_state),
        "allowed_action_ids": list(value.allowed_action_ids),
        "critical_unknown_fields": list(value.critical_unknown_fields),
    }


@dataclass(frozen=True, kw_only=True)
class ScenarioDecision:
    action_id: str
    packet_sha256: str
    source: Literal["local", "advisor"]
    confidence: float
    invalidates_collection: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _action_ids((self.action_id,), "action_id")[0])
        object.__setattr__(self, "packet_sha256", _sha256(self.packet_sha256, "packet_sha256"))
        if self.source not in {"local", "advisor"}:
            raise ScenarioContractError("source must be local or advisor")
        object.__setattr__(self, "confidence", _confidence(self.confidence, "confidence"))
        if type(self.invalidates_collection) is not bool:
            raise ScenarioContractError("invalidates_collection must be bool")


@dataclass(frozen=True, kw_only=True)
class AdvisorChoice:
    action_id: str
    packet_sha256: str
    confidence: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _action_ids((self.action_id,), "action_id")[0])
        object.__setattr__(self, "packet_sha256", _sha256(self.packet_sha256, "packet_sha256"))
        object.__setattr__(self, "confidence", _confidence(self.confidence, "confidence"))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "AdvisorChoice":
        if not isinstance(value, Mapping):
            raise ScenarioContractError("advisor choice must be an object")
        _freeze_json(value, "advisor_choice")
        if set(value) != {"action_id", "packet_sha256", "confidence"}:
            raise ScenarioContractError("advisor choice fields are invalid")
        return cls(
            action_id=value["action_id"],
            packet_sha256=value["packet_sha256"],
            confidence=value["confidence"],
        )


def advisor_choice_to_canonical_json(choice: AdvisorChoice) -> bytes:
    if not isinstance(choice, AdvisorChoice):
        raise ScenarioContractError("advisor choice must be AdvisorChoice")
    return _canonical_json_bytes(
        {
            "action_id": choice.action_id,
            "packet_sha256": choice.packet_sha256,
            "confidence": choice.confidence,
        }
    )


def advisor_choice_from_canonical_json(payload: object) -> AdvisorChoice:
    value = _canonical_json_object(payload, "advisor choice")
    return AdvisorChoice.from_mapping(value)


def validate_advisor_choice(
    packet: ScenarioPacket,
    choice: AdvisorChoice | Mapping[str, object] | None,
    *,
    minimum_confidence: float = 0.80,
) -> AdvisorChoice | None:
    if not isinstance(packet, ScenarioPacket):
        raise ScenarioContractError("packet must be ScenarioPacket")
    threshold = _confidence(minimum_confidence, "minimum_confidence")
    if choice is None:
        return None
    try:
        normalized = choice if isinstance(choice, AdvisorChoice) else AdvisorChoice.from_mapping(choice)
    except (ScenarioContractError, TypeError):
        return None
    if normalized.packet_sha256 != packet.packet_sha256:
        return None
    if normalized.action_id not in packet.allowed_action_ids:
        return None
    if normalized.confidence < threshold:
        return None
    return normalized


def choose_advisor_or_local(
    packet: ScenarioPacket,
    local_decision: ScenarioDecision,
    advisor_choice: AdvisorChoice | Mapping[str, object] | None,
    *,
    invalidating_action_ids: tuple[str, ...] = (),
    minimum_confidence: float = 0.80,
) -> ScenarioDecision:
    if not isinstance(local_decision, ScenarioDecision):
        raise ScenarioContractError("local_decision must be ScenarioDecision")
    if local_decision.source != "local":
        raise ScenarioContractError("local_decision must have local source")
    if local_decision.packet_sha256 != packet.packet_sha256:
        raise ScenarioContractError("local_decision is bound to another packet")
    if local_decision.action_id not in packet.allowed_action_ids:
        raise ScenarioContractError("local_decision action is not allowlisted")
    invalidating = _action_ids(invalidating_action_ids, "invalidating_action_ids", allow_empty=True)
    if any(action_id not in packet.allowed_action_ids for action_id in invalidating):
        raise ScenarioContractError("invalidating action is not allowlisted")
    normalized = validate_advisor_choice(
        packet,
        advisor_choice,
        minimum_confidence=minimum_confidence,
    )
    if normalized is None:
        return local_decision
    return ScenarioDecision(
        action_id=normalized.action_id,
        packet_sha256=packet.packet_sha256,
        source="advisor",
        confidence=normalized.confidence,
        invalidates_collection=normalized.action_id in invalidating,
    )


class ScenarioAdapter(Protocol):
    adapter_id: str

    def build_packet(self, request: ScenarioPacketRequest) -> ScenarioPacket: ...

    def local_decision(self, packet: ScenarioPacket) -> ScenarioDecision: ...

    def choose(
        self,
        packet: ScenarioPacket,
        advisor_choice: AdvisorChoice | None = None,
    ) -> ScenarioDecision: ...

    def expected_nodes_after(self, decision: ScenarioDecision) -> tuple[str, ...]: ...
