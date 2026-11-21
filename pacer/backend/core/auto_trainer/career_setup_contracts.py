from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Mapping


def _jsonable(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class CareerSetupPagePacket:
    page_state: str
    source_sha256: str
    page_family: str
    action_id: str | None = None
    canonical_target_bbox: tuple[int, int, int, int] | None = None
    identities: tuple[str, ...] = ()
    identity_matches: tuple[bool, ...] = ()
    matcher_versions: tuple[tuple[str, str], ...] = ()
    resource_policy: str | None = None
    settings: tuple[tuple[str, object], ...] = ()
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CareerSetupPlan:
    session_id: str
    source_sha256: str
    packet_digest: str
    action_id: str
    canonical_center: tuple[int, int]
    endpoint: str
    expected_next_states: tuple[str, ...]
    expires_monotonic_ns: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "CareerSetupPagePacket",
    "CareerSetupPlan",
    "canonical_sha256",
]
