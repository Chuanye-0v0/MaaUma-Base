from __future__ import annotations

from dataclasses import dataclass, field
import re
from types import MappingProxyType
from typing import Callable, Mapping

from core.auto_trainer.scenario_adapter import (
    AdvisorChoice,
    ScenarioContractError,
    ScenarioDecision,
    ScenarioPacket,
    ScenarioPacketRequest,
    choose_advisor_or_local,
)


GRAND_MASTERS_ACTION_IDS = (
    "scenario.hold",
    "scenario.goddess.red",
    "scenario.goddess.blue",
    "scenario.goddess.yellow",
)

_HOLD_ACTION_ID = GRAND_MASTERS_ACTION_IDS[0]
_GODDESS_ACTION_IDS = GRAND_MASTERS_ACTION_IDS[1:]
_TRAINING_TYPES = ("speed", "stamina", "power", "guts", "wisdom")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
PlayerScenarioPolicy = Callable[[dict[str, object]], str]


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ScenarioContractError(f"{label} must be a lowercase SHA-256")
    return value


def _optional_count(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def _optional_percent(value: object) -> float | None:
    if type(value) in {int, float} and 0 <= value <= 100:
        return float(value)
    return None


def _optional_string(value: object) -> str | None:
    return value if type(value) is str and value else None


def _optional_bool(value: object) -> bool | None:
    return value if type(value) is bool else None


@dataclass(frozen=True, kw_only=True)
class GrandMastersTrainingPage:
    training_type: str
    source_frame_sha256: str
    support_count: int | None
    visible_portrait_count: int | None
    unresolved_portrait_count: int | None
    hint_count: int | None
    friendship: bool | None
    vitality_bar_percent: float | None
    mood: str | None
    fragment_color: str | None
    fragment_stat: str | None
    fragment_available: bool | None
    unknown_fields: tuple[str, ...]
    field_reliability: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        if self.training_type not in (*_TRAINING_TYPES, "unknown"):
            raise ScenarioContractError("training_type is not a Grand Masters training page")
        object.__setattr__(
            self,
            "source_frame_sha256",
            _sha256(self.source_frame_sha256, "source_frame_sha256"),
        )
        object.__setattr__(self, "unknown_fields", tuple(dict.fromkeys(self.unknown_fields)))
        object.__setattr__(self, "field_reliability", MappingProxyType(dict(self.field_reliability)))

    @classmethod
    def from_training_data(
        cls,
        source_frame_sha256: str,
        payload: Mapping[str, object],
        *,
        mood: str | None,
    ) -> "GrandMastersTrainingPage":
        if not isinstance(payload, Mapping):
            raise ScenarioContractError("training payload must be an object")
        raw_unknowns = payload.get("unknown_fields", ("unknown_fields",))
        if not isinstance(raw_unknowns, (tuple, list)) or any(
            type(item) is not str or not item for item in raw_unknowns
        ):
            raise ScenarioContractError("unknown_fields must be a string sequence")
        reliability = payload.get("field_reliability", {})
        if not isinstance(reliability, Mapping):
            reliability = {}
        return cls(
            training_type=_optional_string(payload.get("training_type")) or "unknown",
            source_frame_sha256=source_frame_sha256,
            support_count=_optional_count(payload.get("support_count")),
            visible_portrait_count=_optional_count(payload.get("visible_portrait_count")),
            unresolved_portrait_count=_optional_count(payload.get("unresolved_portrait_count")),
            hint_count=_optional_count(payload.get("hint_count")),
            friendship=_optional_bool(payload.get("friendship")),
            vitality_bar_percent=_optional_percent(payload.get("vitality_bar_percent")),
            mood=_optional_string(mood),
            fragment_color=_optional_string(payload.get("fragment_color")),
            fragment_stat=_optional_string(payload.get("fragment_stat")),
            fragment_available=_optional_bool(payload.get("fragment_available")),
            unknown_fields=tuple(raw_unknowns),
            field_reliability=reliability,
        )

    def critical_unknown_fields(self) -> tuple[str, ...]:
        unknowns: list[str] = []
        for name in (
            "support_count",
            "visible_portrait_count",
            "unresolved_portrait_count",
            "hint_count",
            "friendship",
            "vitality_bar_percent",
            "mood",
            "fragment_available",
        ):
            if getattr(self, name) is None:
                unknowns.append(name)
        if self.unresolved_portrait_count is not None and self.unresolved_portrait_count > 0:
            unknowns.append("unresolved_portrait_count")
        if self.fragment_available is True:
            if self.fragment_color is None:
                unknowns.append("fragment_color")
            if self.fragment_stat is None:
                unknowns.append("fragment_stat")
        if self.field_reliability.get("hint_count") != "reliable":
            unknowns.append("hint_count")
        if self.fragment_available is True and self.field_reliability.get("fragment") != "reliable":
            unknowns.append("fragment_available")
        for name in self.unknown_fields:
            if name == "hint_detection_unreliable":
                unknowns.append("hint_count")
            else:
                unknowns.append(name)
        return tuple(dict.fromkeys(unknowns))

    def to_dict(self) -> dict[str, object]:
        return {
            "training_type": self.training_type,
            "source_frame_sha256": self.source_frame_sha256,
            "support_count": self.support_count,
            "visible_portrait_count": self.visible_portrait_count,
            "unresolved_portrait_count": self.unresolved_portrait_count,
            "hint_count": self.hint_count,
            "friendship": self.friendship,
            "vitality_bar_percent": self.vitality_bar_percent,
            "mood": self.mood,
            "fragment_color": self.fragment_color,
            "fragment_stat": self.fragment_stat,
            "fragment_available": self.fragment_available,
            "unknown_fields": list(self.unknown_fields),
        }


@dataclass(frozen=True, kw_only=True)
class GrandMastersScenarioRequest:
    run_session_id: str
    transition_version: int
    generation: int
    source_frame_sha256: str
    current_node_id: str
    training_pages: tuple[GrandMastersTrainingPage, ...]
    usable_goddess_action_ids: tuple[str, ...] | None
    goddess_availability_known: bool

    def __post_init__(self) -> None:
        if type(self.goddess_availability_known) is not bool:
            raise ScenarioContractError("goddess_availability_known must be bool")
        if not isinstance(self.training_pages, (tuple, list)) or any(
            not isinstance(page, GrandMastersTrainingPage) for page in self.training_pages
        ):
            raise ScenarioContractError("training_pages must contain structured pages")
        object.__setattr__(self, "training_pages", tuple(self.training_pages))
        if self.usable_goddess_action_ids is not None:
            if not isinstance(self.usable_goddess_action_ids, (tuple, list)):
                raise ScenarioContractError("usable_goddess_action_ids must be a sequence")
            object.__setattr__(
                self,
                "usable_goddess_action_ids",
                tuple(self.usable_goddess_action_ids),
            )


class GrandMastersScenarioAdapter:
    adapter_id = "grand_masters"

    def __init__(self, *, player_policy: PlayerScenarioPolicy | None = None) -> None:
        if player_policy is not None and not callable(player_policy):
            raise ScenarioContractError("player_policy must be callable")
        self._player_policy = player_policy

    def build_packet(self, request: GrandMastersScenarioRequest) -> ScenarioPacket:
        if not isinstance(request, GrandMastersScenarioRequest):
            raise ScenarioContractError("request must be GrandMastersScenarioRequest")
        pages_by_type: dict[str, GrandMastersTrainingPage] = {}
        type_counts = {training_type: 0 for training_type in _TRAINING_TYPES}
        critical: list[str] = []
        for page in request.training_pages:
            if page.training_type not in _TRAINING_TYPES:
                critical.append("training_pages.unknown.training_type")
                continue
            type_counts[page.training_type] += 1
            pages_by_type.setdefault(page.training_type, page)

        for training_type in _TRAINING_TYPES:
            if type_counts[training_type] == 0:
                critical.append(f"training_pages.missing.{training_type}")
            elif type_counts[training_type] > 1:
                critical.append(f"training_pages.duplicate.{training_type}")
        page_shas = tuple(page.source_frame_sha256 for page in request.training_pages)
        if len(set(page_shas)) != len(page_shas):
            critical.append("training_pages.source_frame_sha256.duplicate")
        for training_type in _TRAINING_TYPES:
            page = pages_by_type.get(training_type)
            if page is None:
                continue
            critical.extend(
                f"training_pages.{training_type}.{field_name}"
                for field_name in page.critical_unknown_fields()
            )

        usable = self._normalize_usable_actions(request, critical)
        if not request.goddess_availability_known:
            critical.append("goddess_availability")
        critical_fields = tuple(dict.fromkeys(critical))
        allowed = (_HOLD_ACTION_ID,) if critical_fields else (_HOLD_ACTION_ID, *usable)
        expected_nodes = ("turn.pre_action.capture",)
        if len(allowed) > 1:
            expected_nodes += ("scenario.goddess.result",)
        mechanism_state = {
            "training_pages": {
                training_type: pages_by_type[training_type].to_dict()
                for training_type in _TRAINING_TYPES
                if training_type in pages_by_type
            },
            "usable_goddess_actions": list(usable),
            "goddess_availability_known": request.goddess_availability_known,
        }
        return ScenarioPacket.from_request(
            ScenarioPacketRequest(
                adapter_id=self.adapter_id,
                run_session_id=request.run_session_id,
                transition_version=request.transition_version,
                generation=request.generation,
                source_frame_sha256=request.source_frame_sha256,
                current_node_id=request.current_node_id,
                expected_next_node_ids=expected_nodes,
                mechanism_state=mechanism_state,
                allowed_action_ids=allowed,
                critical_unknown_fields=critical_fields,
            )
        )

    @staticmethod
    def _normalize_usable_actions(
        request: GrandMastersScenarioRequest,
        critical: list[str],
    ) -> tuple[str, ...]:
        raw = request.usable_goddess_action_ids
        if raw is None:
            critical.append("goddess_availability")
            return ()
        if len(set(raw)) != len(raw):
            critical.append("goddess_actions.duplicate")
            return ()
        invalid = tuple(action_id for action_id in raw if action_id not in _GODDESS_ACTION_IDS)
        if invalid:
            critical.extend(f"goddess_actions.invalid.{action_id}" for action_id in invalid)
            return ()
        return tuple(action_id for action_id in _GODDESS_ACTION_IDS if action_id in raw)

    def local_decision(self, packet: ScenarioPacket) -> ScenarioDecision:
        if packet.adapter_id != self.adapter_id:
            raise ScenarioContractError("packet belongs to another scenario adapter")
        action_id = _HOLD_ACTION_ID
        if self._player_policy is not None:
            try:
                requested_action = self._player_policy(packet.to_dict())
            except Exception:
                requested_action = _HOLD_ACTION_ID
            if requested_action in packet.allowed_action_ids:
                action_id = requested_action
        return ScenarioDecision(
            action_id=action_id,
            packet_sha256=packet.packet_sha256,
            source="local",
            confidence=1.0,
            invalidates_collection=action_id in _GODDESS_ACTION_IDS,
        )

    def choose(
        self,
        packet: ScenarioPacket,
        advisor_choice: AdvisorChoice | Mapping[str, object] | None = None,
    ) -> ScenarioDecision:
        local = self.local_decision(packet)
        return choose_advisor_or_local(
            packet,
            local,
            advisor_choice,
            invalidating_action_ids=tuple(
                action_id for action_id in _GODDESS_ACTION_IDS if action_id in packet.allowed_action_ids
            ),
        )

    def expected_nodes_after(self, decision: ScenarioDecision) -> tuple[str, ...]:
        if decision.action_id == _HOLD_ACTION_ID:
            return ("turn.pre_action.capture",)
        if decision.action_id in _GODDESS_ACTION_IDS:
            return ("scenario.goddess.result",)
        raise ScenarioContractError("decision action is not a Grand Masters action")
