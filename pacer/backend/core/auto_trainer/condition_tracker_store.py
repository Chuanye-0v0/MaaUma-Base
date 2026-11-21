"""Strict, run-bound persistence for :mod:`condition_tracker` state."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from core.auto_trainer.condition_tracker import (
    ConditionProbeReason,
    ConditionStatus,
    ConditionTracker,
)


STATE_SCHEMA_VERSION = "condition_tracker_state.v1"
STATE_KEYS = frozenset(
    {"schema_version", "run_id", "last_committed_turn", "probe_reasons", "tracker"}
)


@dataclass(frozen=True)
class ConditionTrackerLoadResult:
    tracker: ConditionTracker
    last_committed_turn: int
    probe_reasons: Sequence[ConditionProbeReason]
    recovery_reason: str | None


class ConditionTrackerStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(
        self, *, expected_run_id: str, current_turn: int, audit_interval: int
    ) -> ConditionTrackerLoadResult:
        return _load_condition_tracker_state(
            self.path,
            expected_run_id=expected_run_id,
            current_turn=current_turn,
            audit_interval=audit_interval,
        )

    def save(
        self,
        *,
        run_id: str,
        last_committed_turn: int,
        tracker: ConditionTracker,
        probe_reasons: Sequence[ConditionProbeReason],
    ) -> str:
        payload = _encode_condition_tracker_state(
            run_id=run_id,
            last_committed_turn=last_committed_turn,
            tracker=tracker,
            probe_reasons=probe_reasons,
        )
        _atomic_replace_verified(self.path, payload)
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


def _load_condition_tracker_state(
    path: Path,
    *,
    expected_run_id: str,
    current_turn: int,
    audit_interval: int,
) -> ConditionTrackerLoadResult:
    _require_run_id(expected_run_id)
    _require_turn(current_turn, "condition_tracker_current_turn_invalid")
    _require_audit_interval(audit_interval)
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        return _recovery(audit_interval, "condition_tracker_state_missing")
    except OSError:
        return _recovery(audit_interval, "condition_tracker_state_read_failed")
    try:
        payload = json.loads(
            raw_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_members,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _recovery(audit_interval, "condition_tracker_state_invalid_json")
    if not isinstance(payload, dict) or set(payload) != STATE_KEYS:
        return _recovery(audit_interval, "condition_tracker_state_keys_invalid")
    if payload["schema_version"] != STATE_SCHEMA_VERSION:
        return _recovery(audit_interval, "condition_tracker_state_schema_invalid")
    if payload["run_id"] != expected_run_id:
        return _recovery(audit_interval, "condition_tracker_run_mismatch")
    try:
        _require_run_id(payload["run_id"])
        _require_turn(payload["last_committed_turn"], "condition_tracker_last_turn_invalid")
    except ValueError:
        return _recovery(audit_interval, "condition_tracker_state_fields_invalid")
    if payload["last_committed_turn"] > current_turn:
        return _recovery(audit_interval, "condition_tracker_turn_regression")
    try:
        tracker = ConditionTracker.from_state_dict(payload["tracker"])
    except (TypeError, ValueError):
        return _recovery(audit_interval, "condition_tracker_state_tracker_invalid")
    if tracker.audit_interval != audit_interval:
        return _recovery(audit_interval, "condition_tracker_audit_interval_mismatch")
    if (
        tracker.last_explicit_probe_turn is not None
        and tracker.last_explicit_probe_turn > payload["last_committed_turn"]
    ):
        return _recovery(audit_interval, "condition_tracker_internal_turn_regression")
    try:
        reasons = _decode_reasons(payload["probe_reasons"])
    except ValueError:
        return _recovery(audit_interval, "condition_tracker_probe_reasons_invalid")
    return ConditionTrackerLoadResult(
        tracker=tracker,
        last_committed_turn=payload["last_committed_turn"],
        probe_reasons=reasons,
        recovery_reason=None,
    )


def _encode_condition_tracker_state(
    *,
    run_id: str,
    last_committed_turn: int,
    tracker: ConditionTracker,
    probe_reasons: Sequence[ConditionProbeReason],
) -> bytes:
    _require_run_id(run_id)
    _require_turn(last_committed_turn, "condition_tracker_last_turn_invalid")
    if not isinstance(tracker, ConditionTracker):
        raise ValueError("condition_tracker_required")
    reasons = _validate_reasons(probe_reasons)
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "run_id": run_id,
        "last_committed_turn": last_committed_turn,
        "probe_reasons": [reason.value for reason in reasons],
        "tracker": tracker.to_state_dict(),
    }
    return (
        json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _atomic_replace_verified(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="xb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_parent_directory(path.parent)
        if path.read_bytes() != payload:
            raise OSError("condition_tracker_state_atomic_verify_failed")
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _recovery(audit_interval: int, reason: str) -> ConditionTrackerLoadResult:
    return ConditionTrackerLoadResult(
        tracker=ConditionTracker(
            audit_interval=audit_interval, status=ConditionStatus.UNKNOWN, dirty=True
        ),
        last_committed_turn=0,
        probe_reasons=(),
        recovery_reason=reason,
    )


def _decode_reasons(value: object) -> tuple[ConditionProbeReason, ...]:
    if not isinstance(value, list):
        raise ValueError("condition_tracker_probe_reasons_invalid")
    try:
        reasons = tuple(ConditionProbeReason(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError("condition_tracker_probe_reasons_invalid") from exc
    return _validate_reasons(reasons)


def _validate_reasons(
    reasons: Sequence[ConditionProbeReason],
) -> tuple[ConditionProbeReason, ...]:
    if isinstance(reasons, (str, bytes)) or not isinstance(reasons, Sequence):
        raise ValueError("condition_tracker_probe_reasons_invalid")
    result = tuple(reasons)
    if any(not isinstance(reason, ConditionProbeReason) for reason in result):
        raise ValueError("condition_tracker_probe_reasons_invalid")
    if len(set(result)) != len(result) or tuple(
        sorted(result, key=lambda reason: list(ConditionProbeReason).index(reason))
    ) != result:
        raise ValueError("condition_tracker_probe_reasons_invalid")
    return result


def _require_run_id(value: object) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("condition_tracker_run_id_invalid")


def _require_turn(value: object, reason: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(reason)


def _require_audit_interval(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("condition_audit_interval_invalid")


def _fsync_parent_directory(parent: Path) -> None:
    try:
        descriptor = os.open(parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _reject_duplicate_json_members(pairs: list[tuple[object, object]]) -> dict[object, object]:
    result: dict[object, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("condition_tracker_state_duplicate_json_member")
        result[key] = value
    return result


def _reject_nonstandard_json_constant(value: str) -> object:
    raise ValueError(f"condition_tracker_state_nonstandard_json_constant:{value}")
