from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any


TRAINING_TYPES = ("speed", "stamina", "power", "guts", "wisdom")
STAT_KEYS = ("speed", "stamina", "power", "guts", "wisdom", "skill_pt")


def _int_or_none(value: Any) -> int | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    ):
        return int(value)
    return None


def _bounded_float_or_none(
    value: Any,
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        return None
    return parsed


def _append_unknown_field(raw_data: dict[str, Any], field_name: str) -> None:
    unknown_fields = raw_data.get("unknown_fields")
    unknown_fields = list(unknown_fields) if isinstance(unknown_fields, list) else []
    if field_name not in unknown_fields:
        unknown_fields.append(field_name)
    raw_data["unknown_fields"] = unknown_fields


@dataclass(frozen=True)
class TrainingCandidate:
    training_type: str
    stats_gain: dict[str, int] = field(default_factory=dict)
    failure_rate: float | None = None
    support_count: int | None = None
    hint_count: int | None = None
    friendship: bool | None = None
    fragment_color: str | None = None
    fragment_stat: str | None = None
    fragment_available: bool | None = None
    fragment_state: str | None = None
    fragment_count: int | None = None
    fragments: list[dict[str, Any]] = field(default_factory=list)
    scenario_mechanics: dict[str, Any] = field(default_factory=dict)
    vitality_delta: int | None = None
    confidence: float = 0.0
    raw_data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.training_type not in TRAINING_TYPES:
            raise ValueError(f"training_type:{self.training_type}")

    @property
    def action_id(self) -> str:
        return f"train:{self.training_type}"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrainingCandidate":
        training_type = str(data.get("training_type", "unknown"))
        raw_data = dict(data)
        embedded_raw = raw_data.pop("raw_data", None)
        if isinstance(embedded_raw, dict):
            raw_data.update(embedded_raw)
        confidence_value = data.get("confidence")
        confidence = _bounded_float_or_none(
            confidence_value,
            minimum=0.0,
            maximum=1.0,
        )
        if confidence is None:
            confidence = 0.0
            if confidence_value is not None:
                _append_unknown_field(raw_data, "confidence_invalid")
        failure_rate_value = data.get("failure_rate")
        failure_rate = _bounded_float_or_none(
            failure_rate_value,
            minimum=0.0,
            maximum=100.0,
        )
        if failure_rate is None and failure_rate_value is not None:
            _append_unknown_field(raw_data, "failure_rate_invalid")
        gains = data.get("stats_gain", {})
        if not isinstance(gains, dict):
            gains = {}
        parsed_gains: dict[str, int] = {}
        for key, value in gains.items():
            if key not in STAT_KEYS:
                continue
            if type(value) is int:
                parsed_gains[key] = value
            else:
                _append_unknown_field(raw_data, f"stats_gain_invalid_type:{key}")
        friendship = None
        if "friendship" in data and data.get("friendship") is not None:
            friendship = bool(data["friendship"])
        return cls(
            training_type=training_type,
            stats_gain=parsed_gains,
            failure_rate=failure_rate,
            support_count=_int_or_none(data.get("support_count")),
            hint_count=_int_or_none(data.get("hint_count")),
            friendship=friendship,
            fragment_color=str(data.get("fragment_color"))
            if data.get("fragment_color")
            else None,
            fragment_stat=str(data.get("fragment_stat"))
            if data.get("fragment_stat")
            else None,
            fragment_available=data.get("fragment_available")
            if isinstance(data.get("fragment_available"), bool)
            else None,
            fragment_state=str(data.get("fragment_state"))
            if data.get("fragment_state")
            else None,
            fragment_count=_int_or_none(data.get("fragment_count")),
            fragments=[
                dict(item)
                for item in data.get("fragments", [])
                if isinstance(item, dict)
            ],
            scenario_mechanics=dict(data.get("scenario_mechanics", {}))
            if isinstance(data.get("scenario_mechanics"), dict)
            else {},
            vitality_delta=_int_or_none(data.get("vitality_delta")),
            confidence=confidence,
            raw_data=raw_data,
        )

    def total_gain(self) -> int:
        return sum(value for value in self.stats_gain.values() if value > 0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TrainingTurnState:
    screen: str
    turn: int | None = None
    vitality: int | None = None
    current_stats: dict[str, int] = field(default_factory=dict)
    scenario_state: dict[str, Any] = field(default_factory=dict)
    training_options: list[TrainingCandidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrainingTurnState":
        options = [
            TrainingCandidate.from_dict(item)
            for item in data.get("training_options", [])
            if isinstance(item, dict)
        ]
        seen: set[str] = set()
        for option in options:
            if option.training_type in seen:
                raise ValueError(f"duplicate_training_type:{option.training_type}")
            seen.add(option.training_type)
        current_stats = data.get("current_stats", {})
        if not isinstance(current_stats, dict):
            current_stats = {}
        parsed_stats = {
            key: int(value)
            for key, value in current_stats.items()
            if key in STAT_KEYS
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        }
        scenario_state = data.get("scenario_state", {})
        if not isinstance(scenario_state, dict):
            scenario_state = {}
        return cls(
            screen=str(data.get("screen", "unknown")),
            turn=_int_or_none(data.get("turn")),
            vitality=_int_or_none(data.get("vitality")),
            current_stats=parsed_stats,
            scenario_state=dict(scenario_state),
            training_options=options,
            warnings=[str(item) for item in data.get("warnings", [])],
            errors=[str(item) for item in data.get("errors", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "screen": self.screen,
            "turn": self.turn,
            "vitality": self.vitality,
            "current_stats": dict(self.current_stats),
            "scenario_state": dict(self.scenario_state),
            "training_options": [option.to_dict() for option in self.training_options],
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }
