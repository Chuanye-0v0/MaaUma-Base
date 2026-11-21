from __future__ import annotations

import hashlib
import json
import re
from dataclasses import InitVar, dataclass
from typing import Protocol

from core.auto_trainer.grand_masters_scenario import GRAND_MASTERS_ACTION_IDS
from core.auto_trainer.progressive_route_session import ProgressiveRouteError


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DIRECTIVE_SEAL = object()
_GODDESS_ACTION_IDS = frozenset(GRAND_MASTERS_ACTION_IDS[1:])

UNSUPPORTED_LIVE_ACTION_IDS = frozenset(
    ("primary.outing",) + GRAND_MASTERS_ACTION_IDS[1:]
)


@dataclass(frozen=True)
class PendingRouteDirective:
    kind: str
    reason: str
    action_id: str | None
    expected_node_ids: tuple[str, ...]
    delay_seconds: float | None
    scenario_allowed_action_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteDirective:
    kind: str
    reason: str
    action_id: str
    expected_node_ids: tuple[str, ...]
    delay_seconds: float | None
    run_id: str
    session_id: str
    run_session_id: str
    directive_sequence: int
    source_frame_sha256: str
    transition_packet_sha256: str
    directive_binding_sha256: str
    _seal: InitVar[object]

    def __post_init__(self, _seal: object) -> None:
        if _seal is not _DIRECTIVE_SEAL:
            raise ProgressiveRouteError("route directive must be sealed by bind_route_directive")


@dataclass(frozen=True)
class RouteExecutionResult:
    action_id: str
    status: str
    source_frame_sha256: str
    performed: bool
    run_id: str
    session_id: str
    run_session_id: str
    directive_sequence: int
    transition_packet_sha256: str
    directive_binding_sha256: str
    details: dict[str, object]


class ProgressiveActionPort(Protocol):
    def execute(self, directive: RouteDirective) -> RouteExecutionResult: ...


class DryRunProgressiveActionPort:
    def execute(self, directive: RouteDirective) -> RouteExecutionResult:
        if not isinstance(directive, RouteDirective):
            raise TypeError("directive must be RouteDirective")
        if not directive.action_id:
            raise ProgressiveRouteError("directive action_id must be non-empty")
        return RouteExecutionResult(
            action_id=directive.action_id,
            status="dry_run",
            source_frame_sha256=directive.source_frame_sha256,
            performed=False,
            run_id=directive.run_id,
            session_id=directive.session_id,
            run_session_id=directive.run_session_id,
            directive_sequence=directive.directive_sequence,
            transition_packet_sha256=directive.transition_packet_sha256,
            directive_binding_sha256=directive.directive_binding_sha256,
            details={"runner_mode": "dry_run"},
        )


def bind_route_directive(
    pending: PendingRouteDirective,
    *,
    run_id: str,
    session_id: str,
    run_session_id: str,
    directive_sequence: int,
    source_frame_sha256: str,
    transition_packet_sha256: str,
) -> RouteDirective:
    if not isinstance(pending, PendingRouteDirective):
        raise TypeError("pending must be PendingRouteDirective")
    if pending.kind != "execute":
        raise ProgressiveRouteError("pending kind must be execute")
    _non_empty_string(run_id, "run_id")
    _non_empty_string(session_id, "session_id")
    _non_empty_string(run_session_id, "run_session_id")
    if run_session_id != session_id:
        raise ProgressiveRouteError("run_session_id must equal session_id")
    if (
        isinstance(directive_sequence, bool)
        or not isinstance(directive_sequence, int)
        or directive_sequence <= 0
    ):
        raise ProgressiveRouteError("directive_sequence must be a positive integer")
    _sha256(source_frame_sha256, "source_frame_sha256")
    _sha256(transition_packet_sha256, "transition_packet_sha256")
    action_id = pending.action_id
    _non_empty_string(action_id, "action_id")
    _validate_action_ids(
        pending.scenario_allowed_action_ids,
        "scenario_allowed_action_ids",
    )
    if (
        action_id in _GODDESS_ACTION_IDS
        and action_id not in pending.scenario_allowed_action_ids
    ):
        raise ProgressiveRouteError("scenario action is not packet-allowlisted")
    binding_sha256 = canonical_directive_binding_sha256(
        run_id=run_id,
        session_id=session_id,
        run_session_id=run_session_id,
        directive_sequence=directive_sequence,
        kind=pending.kind,
        action_id=action_id,
        source_frame_sha256=source_frame_sha256,
        transition_packet_sha256=transition_packet_sha256,
    )
    return RouteDirective(
        kind=pending.kind,
        reason=pending.reason,
        action_id=action_id,
        expected_node_ids=pending.expected_node_ids,
        delay_seconds=pending.delay_seconds,
        run_id=run_id,
        session_id=session_id,
        run_session_id=run_session_id,
        directive_sequence=directive_sequence,
        source_frame_sha256=source_frame_sha256,
        transition_packet_sha256=transition_packet_sha256,
        directive_binding_sha256=binding_sha256,
        _seal=_DIRECTIVE_SEAL,
    )


def canonical_directive_binding_sha256(
    *,
    run_id: str,
    session_id: str,
    run_session_id: str,
    directive_sequence: int,
    kind: str,
    action_id: str,
    source_frame_sha256: str,
    transition_packet_sha256: str,
) -> str:
    payload = {
        "run_id": run_id,
        "session_id": session_id,
        "run_session_id": run_session_id,
        "directive_sequence": directive_sequence,
        "kind": kind,
        "action_id": action_id,
        "source_frame_sha256": source_frame_sha256,
        "transition_packet_sha256": transition_packet_sha256,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ProgressiveRouteError(f"{label} must be a lowercase SHA-256")
    return value


def _non_empty_string(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise ProgressiveRouteError(f"{label} must be a non-empty string")
    return value


def _validate_action_ids(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or any(
        type(action_id) is not str or not action_id for action_id in value
    ):
        raise ProgressiveRouteError(f"{label} must contain non-empty action ids")
    if len(set(value)) != len(value):
        raise ProgressiveRouteError(f"{label} must be distinct")
    return value
