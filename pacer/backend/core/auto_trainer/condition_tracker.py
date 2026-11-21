"""Pure condition state and explicit-probe trigger planning."""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TRACKER_STATE_KEYS = frozenset(
    {
        "audit_interval",
        "status",
        "dirty",
        "last_explicit_probe_turn",
        "conditions",
        "report_sha256",
        "source_frame_sha256",
        "latest_snapshot",
    }
)


class ConditionStatus(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    REGISTERED = "registered"
    NEGATIVE = "negative"


class ConditionProbeReason(str, Enum):
    DIRTY = "dirty"
    SUSPECTED_NEGATIVE = "suspected_negative"
    LAST_UNKNOWN = "last_unknown"
    CRITICAL_RACE = "critical_race"
    PERIODIC_AUDIT = "periodic_audit"


@dataclass(frozen=True)
class ConditionQuickSnapshot:
    source_frame_sha256: str
    vitality: float | None
    mood: str | None
    visible_condition_signature: str | None
    suspected_negative: bool

    def __post_init__(self) -> None:
        _require_sha256(self.source_frame_sha256, "condition_snapshot_source_sha256_invalid")
        if self.vitality is not None and (
            isinstance(self.vitality, bool)
            or not isinstance(self.vitality, (int, float))
            or not math.isfinite(float(self.vitality))
        ):
            raise ValueError("condition_snapshot_vitality_invalid")
        if self.mood is not None and (
            not isinstance(self.mood, str) or not self.mood
        ):
            raise ValueError("condition_snapshot_mood_invalid")
        if self.visible_condition_signature is not None and (
            not isinstance(self.visible_condition_signature, str)
            or not self.visible_condition_signature
        ):
            raise ValueError("condition_snapshot_signature_invalid")
        if not isinstance(self.suspected_negative, bool):
            raise ValueError("condition_snapshot_suspected_negative_invalid")


@dataclass(frozen=True)
class ConditionProbePlan:
    required: bool
    reasons: tuple[ConditionProbeReason, ...]


@dataclass
class ConditionTracker:
    audit_interval: int | None = 6
    status: ConditionStatus = ConditionStatus.UNKNOWN
    dirty: bool = False
    last_explicit_probe_turn: int | None = None
    conditions: tuple[Mapping[str, object], ...] = ()
    report_sha256: str | None = None
    source_frame_sha256: str | None = None
    _latest_snapshot: ConditionQuickSnapshot | None = field(
        default=None, init=False, repr=False
    )
    _last_seen_primary_turn: int | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.audit_interval is not None and (
            not isinstance(self.audit_interval, int)
            or isinstance(self.audit_interval, bool)
            or self.audit_interval < 0
        ):
            raise ValueError("condition_audit_interval_invalid")
        if not isinstance(self.status, ConditionStatus):
            raise ValueError("condition_status_invalid")
        if not isinstance(self.dirty, bool):
            raise ValueError("condition_dirty_invalid")
        if self.last_explicit_probe_turn is not None:
            _require_primary_turn(self.last_explicit_probe_turn)
            self._last_seen_primary_turn = self.last_explicit_probe_turn
        if not isinstance(self.conditions, tuple) or any(
            not isinstance(item, Mapping) for item in self.conditions
        ):
            raise ValueError("condition_tracker_conditions_invalid")
        if self.report_sha256 is not None:
            _require_sha256(self.report_sha256, "condition_report_sha256_invalid")
        if self.source_frame_sha256 is not None:
            _require_sha256(
                self.source_frame_sha256,
                "condition_source_frame_sha256_invalid",
            )

    def observe_main_menu(self, snapshot: ConditionQuickSnapshot) -> None:
        """Store structured quick state without changing formal condition status."""
        self._require_snapshot(snapshot)
        self._latest_snapshot = snapshot

    def observe_event_change(
        self,
        before: ConditionQuickSnapshot,
        after: ConditionQuickSnapshot,
    ) -> None:
        """Mark evidence dirty when the condition-region signature is unreliable or changed."""
        self._require_snapshot(before)
        self._require_snapshot(after)
        before_signature = before.visible_condition_signature
        after_signature = after.visible_condition_signature
        if (
            before_signature is None
            or after_signature is None
            or before_signature != after_signature
        ):
            self.dirty = True
        self._latest_snapshot = after

    def mark_dirty(self) -> None:
        """Invalidate cached formal authority without changing its status."""
        self.dirty = True

    def to_state_dict(self) -> dict[str, object]:
        """Export the strict, stable tracker state used for run-bound persistence."""
        latest = self._latest_snapshot
        return {
            "audit_interval": self.audit_interval,
            "status": self.status.value,
            "dirty": self.dirty,
            "last_explicit_probe_turn": self.last_explicit_probe_turn,
            "conditions": [copy.deepcopy(dict(item)) for item in self.conditions],
            "report_sha256": self.report_sha256,
            "source_frame_sha256": self.source_frame_sha256,
            "latest_snapshot": None
            if latest is None
            else {
                "source_frame_sha256": latest.source_frame_sha256,
                "vitality": latest.vitality,
                "mood": latest.mood,
                "visible_condition_signature": latest.visible_condition_signature,
                "suspected_negative": latest.suspected_negative,
            },
        }

    @classmethod
    def from_state_dict(cls, state: Mapping[str, object]) -> "ConditionTracker":
        """Restore only an exact state export; never guess or coerce evidence."""
        if not isinstance(state, Mapping) or set(state) != TRACKER_STATE_KEYS:
            raise ValueError("condition_tracker_state_keys_invalid")
        raw_status = state["status"]
        if not isinstance(raw_status, str):
            raise ValueError("condition_tracker_state_status_invalid")
        try:
            status = ConditionStatus(raw_status)
        except ValueError as exc:
            raise ValueError("condition_tracker_state_status_invalid") from exc
        raw_conditions = state["conditions"]
        if not isinstance(raw_conditions, list) or any(
            not isinstance(item, Mapping) for item in raw_conditions
        ):
            raise ValueError("condition_tracker_state_conditions_invalid")
        tracker = cls(
            audit_interval=state["audit_interval"],
            status=status,
            dirty=state["dirty"],
            last_explicit_probe_turn=state["last_explicit_probe_turn"],
            conditions=tuple(copy.deepcopy(dict(item)) for item in raw_conditions),
            report_sha256=state["report_sha256"],
            source_frame_sha256=state["source_frame_sha256"],
        )
        _validate_restored_authority(tracker)
        raw_latest = state["latest_snapshot"]
        if raw_latest is None:
            return tracker
        latest_keys = {
            "source_frame_sha256",
            "vitality",
            "mood",
            "visible_condition_signature",
            "suspected_negative",
        }
        if not isinstance(raw_latest, Mapping) or set(raw_latest) != latest_keys:
            raise ValueError("condition_tracker_state_snapshot_invalid")
        tracker._latest_snapshot = ConditionQuickSnapshot(
            source_frame_sha256=raw_latest["source_frame_sha256"],
            vitality=raw_latest["vitality"],
            mood=raw_latest["mood"],
            visible_condition_signature=raw_latest["visible_condition_signature"],
            suspected_negative=raw_latest["suspected_negative"],
        )
        return tracker

    def plan_probe(
        self,
        global_action_turn: int,
        critical_race: bool = False,
    ) -> ConditionProbePlan:
        """Return all active explicit-probe triggers in stable enum order."""
        self._record_primary_turn(global_action_turn)
        if not isinstance(critical_race, bool):
            raise ValueError("condition_critical_race_invalid")
        flags = {
            ConditionProbeReason.DIRTY: self.dirty,
            ConditionProbeReason.SUSPECTED_NEGATIVE: (
                self._latest_snapshot is not None
                and self._latest_snapshot.suspected_negative
            ),
            ConditionProbeReason.LAST_UNKNOWN: self.status is ConditionStatus.UNKNOWN,
            ConditionProbeReason.CRITICAL_RACE: critical_race,
            ConditionProbeReason.PERIODIC_AUDIT: self._periodic_due(global_action_turn),
        }
        reasons = tuple(reason for reason in ConditionProbeReason if flags[reason])
        return ConditionProbePlan(required=bool(reasons), reasons=reasons)

    def apply_assessment(
        self,
        assessment: Mapping[str, object],
        global_action_turn: int,
    ) -> None:
        """Consume one explicit assessment without advancing the primary turn."""
        self._record_primary_turn(global_action_turn)
        if not isinstance(assessment, Mapping):
            raise ValueError("condition_assessment_invalid")
        try:
            status = ConditionStatus(assessment.get("status"))
        except (TypeError, ValueError) as exc:
            raise ValueError("condition_assessment_status_invalid") from exc
        raw_conditions = assessment.get("conditions")
        if not isinstance(raw_conditions, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in raw_conditions
        ):
            raise ValueError("condition_assessment_conditions_invalid")
        if status in (ConditionStatus.UNKNOWN, ConditionStatus.HEALTHY) and raw_conditions:
            raise ValueError("condition_assessment_status_conditions_conflict")
        if status in (ConditionStatus.REGISTERED, ConditionStatus.NEGATIVE) and not raw_conditions:
            raise ValueError("condition_assessment_status_conditions_conflict")
        source_sha256 = assessment.get("source_sha256")
        report_sha256 = assessment.get("report_sha256")
        artifact_sha256 = assessment.get("artifact_sha256")
        _require_sha256(source_sha256, "condition_assessment_source_sha256_invalid")
        _require_sha256(report_sha256, "condition_assessment_report_sha256_invalid")
        _require_sha256(artifact_sha256, "condition_assessment_artifact_sha256_invalid")
        if source_sha256 != artifact_sha256:
            raise ValueError("condition_assessment_artifact_sha256_mismatch")

        self.status = status
        self.conditions = tuple(copy.deepcopy(raw_conditions))
        self.report_sha256 = report_sha256
        self.source_frame_sha256 = source_sha256
        self.last_explicit_probe_turn = global_action_turn
        self.dirty = False
        self._latest_snapshot = None

    @staticmethod
    def _require_snapshot(snapshot: ConditionQuickSnapshot) -> None:
        if not isinstance(snapshot, ConditionQuickSnapshot):
            raise TypeError("condition_snapshot_required")

    def _record_primary_turn(self, global_action_turn: int) -> None:
        _require_primary_turn(global_action_turn)
        if (
            self._last_seen_primary_turn is not None
            and global_action_turn < self._last_seen_primary_turn
        ):
            raise ValueError("condition_primary_turn_not_monotonic")
        self._last_seen_primary_turn = global_action_turn

    def _periodic_due(self, global_action_turn: int) -> bool:
        if self.audit_interval in (None, 0):
            return False
        baseline = self.last_explicit_probe_turn or 0
        return global_action_turn - baseline >= self.audit_interval


def _require_sha256(value: object, reason: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(reason)


def _require_primary_turn(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("condition_primary_turn_invalid")


def _validate_restored_authority(tracker: ConditionTracker) -> None:
    """Reject persisted states that could suppress a required baseline probe."""
    conclusive = tracker.status in {
        ConditionStatus.HEALTHY,
        ConditionStatus.REGISTERED,
        ConditionStatus.NEGATIVE,
    }
    authority_fields = (
        tracker.last_explicit_probe_turn,
        tracker.report_sha256,
        tracker.source_frame_sha256,
    )
    if conclusive and any(value is None for value in authority_fields):
        raise ValueError("condition_tracker_state_authority_invalid")
    if tracker.status is ConditionStatus.HEALTHY and tracker.conditions:
        raise ValueError("condition_tracker_state_authority_invalid")
    if tracker.status in {ConditionStatus.REGISTERED, ConditionStatus.NEGATIVE}:
        if not tracker.conditions:
            raise ValueError("condition_tracker_state_authority_invalid")
        if any(
            item.get("source_sha256") != tracker.source_frame_sha256
            for item in tracker.conditions
        ):
            raise ValueError("condition_tracker_state_authority_invalid")
    if tracker.status is ConditionStatus.UNKNOWN:
        if tracker.conditions or (
            any(value is None for value in authority_fields)
            and not all(value is None for value in authority_fields)
        ):
            raise ValueError("condition_tracker_state_authority_invalid")
