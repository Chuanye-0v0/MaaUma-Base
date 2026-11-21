from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from config.settings import ADB_ENDPOINT


SOURCE_SCHEMA = "uma.career_start_perception.v1"
DESTINATION_SCHEMA = "uma.career_start_destination.v1"
PLAN_SCHEMA = "uma.career_start_plan.v1"
DRY_RUN_SCHEMA = "uma.career_start_dry_run.v1"
ACTION_ID = "career_start.global_home_enter_career"
ACTION_REASON = "enter_new_career_from_global_home"
SOURCE_STATE = "global_home_ready"
DESTINATION_STATE = "scenario_select_ready"
ALLOWED_SOURCE_SIZES = frozenset({(720, 1280), (1440, 2560)})
SOURCE_MAX_AGE_NS = 10_000_000_000
POST_MAX_CAPTURES = 8
POST_MAX_SECONDS = 15.0
POST_POLL_SECONDS = 1.5

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def canonical_bytes(value: object) -> bytes:
    primitive = value.to_dict() if hasattr(value, "to_dict") else value
    return json.dumps(
        primitive,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _strict_fields(value: Mapping[str, Any], data_type: type[object]) -> None:
    expected = {field.name for field in fields(data_type)}
    actual = set(value)
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unknown:
        raise ValueError(f"unknown_fields:{','.join(unknown)}")
    if missing:
        raise ValueError(f"missing_fields:{','.join(missing)}")


def _validate_sha256(name: str, value: object) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(name)
    return value


def _validate_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(name)
    return value


def _int_tuple(name: str, value: object, length: int) -> tuple[int, ...]:
    if not isinstance(value, (tuple, list)) or len(value) != length:
        raise ValueError(name)
    result = tuple(_validate_int(name, item) for item in value)
    if any(item < 0 for item in result):
        raise ValueError(name)
    return result


@dataclass(frozen=True, kw_only=True)
class CareerButtonAnchor:
    action_id: str
    canonical_bbox: tuple[int, int, int, int]
    canonical_center: tuple[int, int]
    device_bbox: tuple[int, int, int, int]
    device_center: tuple[int, int]

    def __post_init__(self) -> None:
        if self.action_id != ACTION_ID:
            raise ValueError("action_id")
        for name, value, length in (
            ("canonical_bbox", self.canonical_bbox, 4),
            ("canonical_center", self.canonical_center, 2),
            ("device_bbox", self.device_bbox, 4),
            ("device_center", self.device_center, 2),
        ):
            object.__setattr__(self, name, _int_tuple(name, value, length))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CareerButtonAnchor":
        if not isinstance(value, Mapping):
            raise ValueError("career_button")
        _strict_fields(value, cls)
        return cls(
            action_id=value["action_id"],
            canonical_bbox=_int_tuple("canonical_bbox", value["canonical_bbox"], 4),
            canonical_center=_int_tuple("canonical_center", value["canonical_center"], 2),
            device_bbox=_int_tuple("device_bbox", value["device_bbox"], 4),
            device_center=_int_tuple("device_center", value["device_center"], 2),
        )


@dataclass(frozen=True, kw_only=True)
class CareerStartSourcePacket:
    schema_version: str
    source_image_sha256: str
    captured_monotonic_ns: int
    expires_monotonic_ns: int
    source_image_size: tuple[int, int]
    canonical_size: tuple[int, int]
    orientation: str
    page_state: str
    page_confidence: float
    top_status_bar: bool
    global_home_nav: bool
    home_tab_selected: bool
    career_button_count: int
    career_button: CareerButtonAnchor | None
    active_forbidden_page: str | None
    blocking_modal: bool
    scoped_safety_digest: str
    errors: Sequence[str]

    def __post_init__(self) -> None:
        if self.schema_version != SOURCE_SCHEMA:
            raise ValueError("schema_version")
        _validate_sha256("source_image_sha256", self.source_image_sha256)
        captured = _validate_int("captured_monotonic_ns", self.captured_monotonic_ns)
        expires = _validate_int("expires_monotonic_ns", self.expires_monotonic_ns)
        if captured < 0 or expires <= captured:
            raise ValueError("expires_monotonic_ns")
        source_size = _int_tuple("source_image_size", self.source_image_size, 2)
        if self.page_state == SOURCE_STATE and source_size not in ALLOWED_SOURCE_SIZES:
            raise ValueError("source_image_size")
        canonical_size = _int_tuple("canonical_size", self.canonical_size, 2)
        if canonical_size != (720, 1280):
            raise ValueError("canonical_size")
        if self.orientation != "portrait":
            raise ValueError("orientation")
        if isinstance(self.page_confidence, bool) or not isinstance(
            self.page_confidence, (int, float)
        ):
            raise ValueError("page_confidence")
        if not math.isfinite(float(self.page_confidence)) or not 0 <= float(
            self.page_confidence
        ) <= 1:
            raise ValueError("page_confidence")
        for name in (
            "top_status_bar",
            "global_home_nav",
            "home_tab_selected",
            "blocking_modal",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(name)
        count = _validate_int("career_button_count", self.career_button_count)
        if count < 0:
            raise ValueError("career_button_count")
        if self.career_button is not None and not isinstance(
            self.career_button, CareerButtonAnchor
        ):
            raise ValueError("career_button")
        if self.active_forbidden_page is not None and not isinstance(
            self.active_forbidden_page, str
        ):
            raise ValueError("active_forbidden_page")
        _validate_sha256("scoped_safety_digest", self.scoped_safety_digest)
        if not isinstance(self.errors, (tuple, list)) or not all(
            isinstance(item, str) for item in self.errors
        ):
            raise ValueError("errors")
        object.__setattr__(self, "source_image_size", source_size)
        object.__setattr__(self, "canonical_size", canonical_size)
        object.__setattr__(self, "errors", tuple(self.errors))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CareerStartSourcePacket":
        if not isinstance(value, Mapping):
            raise ValueError("source_packet")
        _strict_fields(value, cls)
        button = value["career_button"]
        return cls(
            schema_version=value["schema_version"],
            source_image_sha256=_validate_sha256(
                "source_image_sha256", value["source_image_sha256"]
            ),
            captured_monotonic_ns=_validate_int(
                "captured_monotonic_ns", value["captured_monotonic_ns"]
            ),
            expires_monotonic_ns=_validate_int(
                "expires_monotonic_ns", value["expires_monotonic_ns"]
            ),
            source_image_size=_int_tuple(
                "source_image_size", value["source_image_size"], 2
            ),
            canonical_size=_int_tuple("canonical_size", value["canonical_size"], 2),
            orientation=value["orientation"],
            page_state=value["page_state"],
            page_confidence=value["page_confidence"],
            top_status_bar=value["top_status_bar"],
            global_home_nav=value["global_home_nav"],
            home_tab_selected=value["home_tab_selected"],
            career_button_count=_validate_int(
                "career_button_count", value["career_button_count"]
            ),
            career_button=(
                CareerButtonAnchor.from_dict(button) if button is not None else None
            ),
            active_forbidden_page=value["active_forbidden_page"],
            blocking_modal=value["blocking_modal"],
            scoped_safety_digest=_validate_sha256(
                "scoped_safety_digest", value["scoped_safety_digest"]
            ),
            errors=tuple(value["errors"]),
        )


@dataclass(frozen=True, kw_only=True)
class CareerStartDestinationPacket:
    schema_version: str
    source_image_sha256: str
    page_state: str
    scenario_id: str | None
    selected: bool
    anchors: Mapping[str, bool | int]
    active_forbidden_page: str | None
    blocking_modal: bool
    raw_safety_digest: str
    scoped_safety_digest: str
    errors: Sequence[str]

    def __post_init__(self) -> None:
        if self.schema_version != DESTINATION_SCHEMA:
            raise ValueError("schema_version")
        _validate_sha256("source_image_sha256", self.source_image_sha256)
        if self.scenario_id is not None and not isinstance(self.scenario_id, str):
            raise ValueError("scenario_id")
        if not isinstance(self.selected, bool):
            raise ValueError("selected")
        if not isinstance(self.anchors, Mapping) or not all(
            isinstance(key, str)
            and (
                isinstance(value, bool)
                or (type(value) is int and value >= 0)
            )
            for key, value in self.anchors.items()
        ):
            raise ValueError("anchors")
        if self.active_forbidden_page is not None and not isinstance(
            self.active_forbidden_page, str
        ):
            raise ValueError("active_forbidden_page")
        if not isinstance(self.blocking_modal, bool):
            raise ValueError("blocking_modal")
        _validate_sha256("raw_safety_digest", self.raw_safety_digest)
        _validate_sha256("scoped_safety_digest", self.scoped_safety_digest)
        if not isinstance(self.errors, (tuple, list)) or not all(
            isinstance(item, str) for item in self.errors
        ):
            raise ValueError("errors")
        object.__setattr__(self, "anchors", dict(self.anchors))
        object.__setattr__(self, "errors", tuple(self.errors))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, kw_only=True)
class CareerStartPlan:
    schema_version: str = PLAN_SCHEMA
    action: str = "stop"
    reason: str = "unknown"
    action_id: str | None = None
    endpoint: str = ADB_ENDPOINT
    source_image_sha256: str = "0" * 64
    packet_digest: str = "0" * 64
    canonical_bbox: tuple[int, int, int, int] | None = None
    canonical_center: tuple[int, int] | None = None
    device_bbox: tuple[int, int, int, int] | None = None
    device_center: tuple[int, int] | None = None
    source_state: str | None = None
    destination_state: str | None = None
    errors: Sequence[str] = ()

    def __post_init__(self) -> None:
        if self.schema_version != PLAN_SCHEMA:
            raise ValueError("schema_version")
        _validate_sha256("source_image_sha256", self.source_image_sha256)
        _validate_sha256("packet_digest", self.packet_digest)
        object.__setattr__(self, "errors", tuple(self.errors))

    @classmethod
    def stop(cls, reason: str, *, source_image_sha256: str) -> "CareerStartPlan":
        return cls(reason=reason, source_image_sha256=source_image_sha256)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CareerStartDryRunPlan:
    plan: CareerStartPlan
    execution_allowed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.plan, CareerStartPlan):
            raise ValueError("plan")
        if self.execution_allowed is not False:
            raise ValueError("execution_allowed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DRY_RUN_SCHEMA,
            "plan": self.plan.to_dict(),
            "execution_allowed": False,
        }


__all__ = [
    "ACTION_ID",
    "ACTION_REASON",
    "ALLOWED_SOURCE_SIZES",
    "CareerButtonAnchor",
    "CareerStartDryRunPlan",
    "CareerStartDestinationPacket",
    "CareerStartPlan",
    "CareerStartSourcePacket",
    "DESTINATION_SCHEMA",
    "DESTINATION_STATE",
    "DRY_RUN_SCHEMA",
    "PLAN_SCHEMA",
    "POST_MAX_CAPTURES",
    "POST_MAX_SECONDS",
    "POST_POLL_SECONDS",
    "SOURCE_MAX_AGE_NS",
    "SOURCE_SCHEMA",
    "SOURCE_STATE",
    "canonical_bytes",
    "canonical_sha256",
]
