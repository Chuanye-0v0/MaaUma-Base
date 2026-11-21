"""Strict SHA-bound audit packets for progressive-route transitions."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Mapping

from core.auto_trainer.condition_tracker import ConditionProbeReason, ConditionStatus
from core.auto_trainer.progressive_route_session import ProgressiveRouteSession


PROGRESSIVE_ROUTE_PACKET_SCHEMA = "uma.progressive_route.v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PHASES = frozenset({"career", "complete", "mismatch"})
_STATUSES = frozenset({"running", "complete", "mismatch"})
_PRIMARY_ACTIONS = frozenset(
    {
        "primary.training",
        "primary.race",
        "primary.outing",
        "primary.rest",
        "primary.clinic",
    }
)
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "run_session_id",
        "transition_version",
        "phase",
        "turn",
        "current_node_id",
        "expected_next_node_ids",
        "last_action_id",
        "source_frame_sha256",
        "packet_sha256",
        "condition_dirty",
        "primary_action",
        "event_sequence_count",
        "scenario_action_count",
        "status",
        "scenario",
        "condition",
    }
)
_TURN_FIELDS = frozenset({"global_action_turn", "period_text"})
_SCENARIO_FIELDS = frozenset(
    {
        "generation",
        "packet_sha256",
        "decision_sha256",
        "recollection_required",
        "training_page_sha256",
    }
)
_CONDITION_FIELDS = frozenset({"status", "report_sha256", "probe_reasons"})


class ProgressiveRoutePacketError(ValueError):
    """Raised when a route audit packet violates its pure contract."""


def canonical_packet_sha256(packet: Mapping[str, object]) -> str:
    """Hash canonical JSON while omitting only the packet's own SHA field."""
    if not isinstance(packet, Mapping):
        raise ProgressiveRoutePacketError("packet must be an object")
    payload = copy.deepcopy(dict(packet))
    payload.pop("packet_sha256", None)
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProgressiveRoutePacketError(
            "packet must contain finite JSON values"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def build_transition_packet(
    session: ProgressiveRouteSession,
    *,
    phase: str,
    period_text: str,
    status: str,
) -> dict[str, object]:
    """Snapshot one session boundary into a strict audit packet."""
    if not isinstance(session, ProgressiveRouteSession):
        raise ProgressiveRoutePacketError("session must be ProgressiveRouteSession")
    event_count = len(session.turn_context.pre_event_sequence) + len(
        session.turn_context.post_event_sequence
    )
    packet: dict[str, object] = {
        "schema_version": PROGRESSIVE_ROUTE_PACKET_SCHEMA,
        "run_session_id": session.run_session_id,
        "transition_version": session.transition_version,
        "phase": phase,
        "turn": {
            "global_action_turn": session.global_action_turn,
            "period_text": period_text,
        },
        "current_node_id": session.current_node_id,
        "expected_next_node_ids": list(session.expected_next_node_ids),
        "last_action_id": session.last_action_id,
        "source_frame_sha256": session.last_observation_sha256,
        "condition_dirty": session.condition_dirty,
        "primary_action": session.turn_context.primary_action_id,
        "event_sequence_count": event_count,
        "scenario_action_count": len(
            session.turn_context.scenario_action_sequence
        ),
        "status": status,
        "scenario": {
            "generation": session.scenario_generation,
            "packet_sha256": session.scenario_packet_sha256,
            "decision_sha256": session.scenario_decision_sha256,
            "recollection_required": session.scenario_recollection_required,
            "training_page_sha256": list(
                session.scenario_training_page_sha256
            ),
        },
        "condition": {
            "status": session.condition_status,
            "report_sha256": session.condition_report_sha256,
            "probe_reasons": list(session.condition_probe_reasons),
        },
    }
    packet["packet_sha256"] = canonical_packet_sha256(packet)
    return validate_transition_packet(packet, session=session)


def validate_transition_packet(
    packet: Mapping[str, object],
    *,
    session: ProgressiveRouteSession | None = None,
    expected_run_session_id: str | None = None,
) -> dict[str, object]:
    """Validate structure, semantics, SHA binding, and optional run binding."""
    if not isinstance(packet, Mapping):
        raise ProgressiveRoutePacketError("packet must be an object")
    if set(packet) != _TOP_LEVEL_FIELDS:
        raise ProgressiveRoutePacketError("packet top-level fields are invalid")
    value = copy.deepcopy(dict(packet))
    if value["schema_version"] != PROGRESSIVE_ROUTE_PACKET_SCHEMA:
        raise ProgressiveRoutePacketError("packet schema_version is invalid")
    _non_empty_string(value["run_session_id"], "run_session_id")
    if expected_run_session_id is not None:
        _expected_run_session_id(expected_run_session_id)
        if value["run_session_id"] != expected_run_session_id:
            raise ProgressiveRoutePacketError("packet run_session_id mismatch")
        if session is not None:
            if not isinstance(session, ProgressiveRouteSession):
                raise ProgressiveRoutePacketError(
                    "session must be ProgressiveRouteSession"
                )
            if session.run_session_id != expected_run_session_id:
                raise ProgressiveRoutePacketError(
                    "expected_run_session_id does not match session"
                )
    _integer(value["transition_version"], "transition_version")
    if value["phase"] not in _PHASES:
        raise ProgressiveRoutePacketError("phase is invalid")
    turn = _object(value["turn"], _TURN_FIELDS, "turn")
    _integer(turn["global_action_turn"], "turn.global_action_turn")
    _non_empty_string(turn["period_text"], "turn.period_text")
    _non_empty_string(value["current_node_id"], "current_node_id")
    _string_sequence(
        value["expected_next_node_ids"],
        "expected_next_node_ids",
    )
    if value["last_action_id"] is not None:
        _non_empty_string(value["last_action_id"], "last_action_id")
    _sha256(value["source_frame_sha256"], "source_frame_sha256")
    _sha256(value["packet_sha256"], "packet_sha256")
    if type(value["condition_dirty"]) is not bool:
        raise ProgressiveRoutePacketError("condition_dirty must be bool")
    primary_action = value["primary_action"]
    if primary_action is not None and primary_action not in _PRIMARY_ACTIONS:
        raise ProgressiveRoutePacketError("primary_action is invalid")
    if primary_action is not None and value["last_action_id"] != primary_action:
        raise ProgressiveRoutePacketError(
            "primary_action must match the committed last_action_id"
        )
    _integer(value["event_sequence_count"], "event_sequence_count")
    _integer(value["scenario_action_count"], "scenario_action_count")
    if value["status"] not in _STATUSES:
        raise ProgressiveRoutePacketError("status is invalid")

    scenario = _object(value["scenario"], _SCENARIO_FIELDS, "scenario")
    _integer(scenario["generation"], "scenario.generation")
    scenario_packet_sha = _optional_sha256(
        scenario["packet_sha256"], "scenario.packet_sha256"
    )
    scenario_decision_sha = _optional_sha256(
        scenario["decision_sha256"], "scenario.decision_sha256"
    )
    if type(scenario["recollection_required"]) is not bool:
        raise ProgressiveRoutePacketError(
            "scenario.recollection_required must be bool"
        )
    page_shas = _sha_sequence(
        scenario["training_page_sha256"],
        "scenario.training_page_sha256",
    )
    if scenario_decision_sha is not None and scenario_packet_sha is None:
        raise ProgressiveRoutePacketError(
            "scenario decision requires a current packet"
        )
    if scenario["recollection_required"] and (
        scenario_packet_sha is not None
        or scenario_decision_sha is not None
        or page_shas
    ):
        raise ProgressiveRoutePacketError(
            "scenario recollection cannot retain current bindings"
        )
    if not scenario["recollection_required"]:
        if scenario_packet_sha is None and page_shas:
            raise ProgressiveRoutePacketError(
                "scenario pages require a current packet"
            )
        if scenario_packet_sha is not None and len(page_shas) != 5:
            raise ProgressiveRoutePacketError(
                "current scenario packet requires exactly five pages"
            )

    condition = _object(value["condition"], _CONDITION_FIELDS, "condition")
    try:
        condition_status = ConditionStatus(condition["status"])
    except (TypeError, ValueError) as exc:
        raise ProgressiveRoutePacketError("condition.status is invalid") from exc
    condition_report_sha = _optional_sha256(
        condition["report_sha256"], "condition.report_sha256"
    )
    reasons = _condition_reasons(condition["probe_reasons"])
    if condition_status is ConditionStatus.UNKNOWN:
        if ConditionProbeReason.LAST_UNKNOWN not in reasons:
            raise ProgressiveRoutePacketError(
                "unknown condition must remain probe-triggerable"
            )
    elif ConditionProbeReason.LAST_UNKNOWN in reasons:
        raise ProgressiveRoutePacketError(
            "conclusive condition cannot claim last_unknown"
        )
    elif condition_report_sha is None:
        raise ProgressiveRoutePacketError(
            "conclusive condition requires a report SHA"
        )

    if value["packet_sha256"] != canonical_packet_sha256(value):
        raise ProgressiveRoutePacketError("packet_sha256 does not bind the packet")
    if session is not None:
        _validate_session_binding(value, session)
    return value


def write_transition_packet(
    path: str | Path,
    packet: Mapping[str, object],
    *,
    session: ProgressiveRouteSession | None = None,
) -> None:
    """Atomically replace a validated transition packet."""
    validated = validate_transition_packet(packet, session=session)
    destination = Path(path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProgressiveRoutePacketError("packet directory is unavailable") from exc
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(
                validated,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    except (OSError, TypeError, ValueError) as exc:
        raise ProgressiveRoutePacketError("packet atomic write failed") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def load_transition_packet(
    path: str | Path,
    *,
    session: ProgressiveRouteSession | None = None,
    expected_run_session_id: str | None = None,
) -> dict[str, object]:
    """Strictly load one packet; corrupt state is never replaced or reset."""
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProgressiveRoutePacketError("packet file is invalid") from exc
    if not isinstance(raw, dict):
        raise ProgressiveRoutePacketError("packet file root must be an object")
    return validate_transition_packet(
        raw,
        session=session,
        expected_run_session_id=expected_run_session_id,
    )


def _validate_session_binding(
    packet: Mapping[str, object],
    session: ProgressiveRouteSession,
) -> None:
    if not isinstance(session, ProgressiveRouteSession):
        raise ProgressiveRoutePacketError("session must be ProgressiveRouteSession")
    turn = packet["turn"]
    scenario = packet["scenario"]
    condition = packet["condition"]
    expected = {
        "run_session_id": session.run_session_id,
        "transition_version": session.transition_version,
        "current_node_id": session.current_node_id,
        "expected_next_node_ids": list(session.expected_next_node_ids),
        "last_action_id": session.last_action_id,
        "source_frame_sha256": session.last_observation_sha256,
        "condition_dirty": session.condition_dirty,
        "primary_action": session.turn_context.primary_action_id,
        "event_sequence_count": len(session.turn_context.pre_event_sequence)
        + len(session.turn_context.post_event_sequence),
        "scenario_action_count": len(
            session.turn_context.scenario_action_sequence
        ),
    }
    for key, expected_value in expected.items():
        if packet[key] != expected_value:
            raise ProgressiveRoutePacketError(f"packet {key} does not match session")
    if turn["global_action_turn"] != session.global_action_turn:
        raise ProgressiveRoutePacketError("packet turn does not match session")
    expected_scenario = {
        "generation": session.scenario_generation,
        "packet_sha256": session.scenario_packet_sha256,
        "decision_sha256": session.scenario_decision_sha256,
        "recollection_required": session.scenario_recollection_required,
        "training_page_sha256": list(session.scenario_training_page_sha256),
    }
    if scenario != expected_scenario:
        raise ProgressiveRoutePacketError("packet scenario does not match session")
    expected_condition = {
        "status": session.condition_status,
        "report_sha256": session.condition_report_sha256,
        "probe_reasons": list(session.condition_probe_reasons),
    }
    if condition != expected_condition:
        raise ProgressiveRoutePacketError("packet condition does not match session")


def _object(value: object, fields: frozenset[str], label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ProgressiveRoutePacketError(f"{label} fields are invalid")
    return dict(value)


def _non_empty_string(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise ProgressiveRoutePacketError(f"{label} must be a non-empty string")
    return value


def _expected_run_session_id(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise ProgressiveRoutePacketError(
            "expected_run_session_id must be a non-empty string"
        )
    return value


def _integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ProgressiveRoutePacketError(f"{label} must be an integer >= 0")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ProgressiveRoutePacketError(f"{label} must be a lowercase SHA-256")
    return value


def _optional_sha256(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, label)


def _string_sequence(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProgressiveRoutePacketError(f"{label} must be a list")
    result = tuple(_non_empty_string(item, label) for item in value)
    if len(result) != len(set(result)):
        raise ProgressiveRoutePacketError(f"{label} must be unique")
    return result


def _sha_sequence(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProgressiveRoutePacketError(f"{label} must be a list")
    result = tuple(_sha256(item, label) for item in value)
    if len(result) != len(set(result)):
        raise ProgressiveRoutePacketError(f"{label} must be unique")
    return result


def _condition_reasons(value: object) -> tuple[ConditionProbeReason, ...]:
    if not isinstance(value, list):
        raise ProgressiveRoutePacketError("condition.probe_reasons must be a list")
    try:
        reasons = tuple(ConditionProbeReason(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ProgressiveRoutePacketError(
            "condition.probe_reasons contains an invalid reason"
        ) from exc
    ordered = tuple(reason for reason in ConditionProbeReason if reason in reasons)
    if reasons != ordered:
        raise ProgressiveRoutePacketError(
            "condition.probe_reasons must be ordered and unique"
        )
    return reasons
