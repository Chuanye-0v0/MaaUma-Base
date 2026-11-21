from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping

from core.auto_trainer.condition_tracker import (
    ConditionProbePlan,
    ConditionProbeReason,
    ConditionStatus,
    ConditionTracker,
)
from core.auto_trainer.scenario_adapter import ScenarioDecision, ScenarioPacket


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRIMARY_ACTIONS = frozenset({"primary.training", "primary.race", "primary.outing", "primary.rest", "primary.clinic"})
_TRAINING_TYPES = ("speed", "stamina", "power", "guts", "wisdom")


class ProgressiveRouteError(ValueError):
    pass


class ProgressiveRouteStatus(str, Enum):
    BOOTSTRAP = "bootstrap"
    TRACKING = "tracking"
    SETTLING = "settling"
    MISMATCH = "mismatch"
    COMPLETE = "complete"


@dataclass
class TurnContext:
    global_action_turn: int
    period_key: str
    calendar_ordinal: int
    primary_action_id: str | None = None
    primary_status: str = "not_started"
    pre_event_sequence: list[dict[str, str]] = field(default_factory=list)
    post_event_sequence: list[dict[str, str]] = field(default_factory=list)
    scenario_action_sequence: list[dict[str, str]] = field(default_factory=list)
    condition_status: str = "unknown"
    condition_dirty: bool = False


@dataclass
class ProgressiveRouteSession:
    run_session_id: str
    turn_context: TurnContext
    status: ProgressiveRouteStatus = ProgressiveRouteStatus.BOOTSTRAP
    current_node_id: str = "bootstrap.start"
    last_action_id: str | None = None
    expected_next_node_ids: tuple[str, ...] = ()
    last_observation_sha256: str | None = None
    last_action_sha256: str | None = None
    phase: str = "bootstrap"
    transition_version: int = 0
    settle_count: int = 0
    last_settle_sha256: str | None = None
    mismatch_reason: str | None = None
    last_committed_turn: dict[str, Any] | None = None
    scenario_generation: int = 0
    scenario_packet_sha256: str | None = None
    scenario_packet_transition_version: int | None = None
    scenario_source_frame_sha256: str | None = None
    scenario_decision_sha256: str | None = None
    scenario_action_id: str | None = None
    scenario_training_page_sha256: tuple[str, ...] = ()
    scenario_candidate_sha256: tuple[str, ...] = ()
    scenario_allowed_action_ids: tuple[str, ...] = ()
    scenario_recollection_required: bool = False
    scenario_invalidated_transition_version: int | None = None
    stale_scenario_packet_sha256: tuple[str, ...] = ()
    stale_scenario_decision_sha256: tuple[str, ...] = ()
    stale_scenario_training_page_sha256: tuple[str, ...] = ()
    stale_scenario_candidate_sha256: tuple[str, ...] = ()
    stale_scenario_action_ids: tuple[str, ...] = ()
    condition_status: str = ConditionStatus.UNKNOWN.value
    condition_dirty: bool = False
    condition_report_sha256: str | None = None
    condition_probe_reasons: tuple[str, ...] = (
        ConditionProbeReason.LAST_UNKNOWN.value,
    )
    condition_tracker_state: dict[str, object] | None = None

    @classmethod
    def new(cls, run_session_id: str, initial_calendar_key: str, initial_calendar_ordinal: int) -> "ProgressiveRouteSession":
        if type(run_session_id) is not str or not run_session_id:
            raise ProgressiveRouteError("run_session_id must be a non-empty string")
        _calendar(initial_calendar_key, initial_calendar_ordinal)
        return cls(run_session_id, TurnContext(0, initial_calendar_key, initial_calendar_ordinal))

    @property
    def global_action_turn(self) -> int:
        return self.turn_context.global_action_turn

    def apply_condition_tracker(
        self,
        tracker: ConditionTracker,
        plan: ConditionProbePlan | None = None,
    ) -> None:
        """Persist pure tracker state without advancing any route counter."""
        if not isinstance(tracker, ConditionTracker):
            raise ProgressiveRouteError("tracker must be ConditionTracker")
        if plan is not None and not isinstance(plan, ConditionProbePlan):
            raise ProgressiveRouteError("condition plan must be ConditionProbePlan")
        report_sha256 = tracker.report_sha256
        if report_sha256 is not None:
            _sha(report_sha256)
        plan_reasons = (
            (ConditionProbeReason.LAST_UNKNOWN,)
            if plan is None and tracker.status is ConditionStatus.UNKNOWN
            else (() if plan is None else plan.reasons)
        )
        if any(not isinstance(reason, ConditionProbeReason) for reason in plan_reasons):
            raise ProgressiveRouteError("condition plan contains an invalid reason")
        ordered_reasons = tuple(
            reason for reason in ConditionProbeReason if reason in plan_reasons
        )
        if tuple(plan_reasons) != ordered_reasons:
            raise ProgressiveRouteError("condition plan reasons are not ordered and distinct")
        if tracker.status is ConditionStatus.UNKNOWN:
            if ConditionProbeReason.LAST_UNKNOWN not in ordered_reasons:
                raise ProgressiveRouteError(
                    "unknown condition must retain last_unknown"
                )
        elif ConditionProbeReason.LAST_UNKNOWN in ordered_reasons:
            raise ProgressiveRouteError(
                "conclusive condition cannot retain last_unknown"
            )
        reasons = tuple(reason.value for reason in ordered_reasons)
        self.condition_status = tracker.status.value
        self.condition_dirty = tracker.dirty
        self.condition_report_sha256 = report_sha256
        self.condition_probe_reasons = reasons
        self.condition_tracker_state = tracker.to_state_dict()
        self.turn_context.condition_status = self.condition_status
        self.turn_context.condition_dirty = self.condition_dirty

    def restore_condition_tracker(
        self,
        audit_interval: int | None,
    ) -> ConditionTracker:
        """Restore the exact persisted tracker or fail closed for legacy sessions."""
        if self.condition_tracker_state is None:
            tracker = ConditionTracker(audit_interval=audit_interval, dirty=True)
            self.apply_condition_tracker(
                tracker,
                tracker.plan_probe(self.global_action_turn),
            )
            return tracker
        try:
            tracker = ConditionTracker.from_state_dict(self.condition_tracker_state)
        except (TypeError, ValueError) as exc:
            raise ProgressiveRouteError("invalid condition tracker state") from exc
        if tracker.audit_interval != audit_interval:
            raise ProgressiveRouteError("condition tracker audit interval mismatch")
        if (
            self.condition_status != tracker.status.value
            or self.condition_dirty is not tracker.dirty
            or self.condition_report_sha256 != tracker.report_sha256
        ):
            raise ProgressiveRouteError("condition tracker summary mismatch")
        self._validate_restored_condition_probe_reasons(tracker)
        return tracker

    def _validate_restored_condition_probe_reasons(
        self,
        tracker: ConditionTracker,
    ) -> None:
        try:
            reasons = tuple(
                ConditionProbeReason(value) for value in self.condition_probe_reasons
            )
        except (TypeError, ValueError) as exc:
            raise ProgressiveRouteError("invalid condition probe reasons") from exc
        ordered = tuple(reason for reason in ConditionProbeReason if reason in reasons)
        if reasons != ordered:
            raise ProgressiveRouteError("invalid condition probe reasons")
        if ConditionProbeReason.CRITICAL_RACE in reasons:
            raise ProgressiveRouteError(
                "critical race condition probe cannot be restored"
            )
        if ConditionProbeReason.DIRTY in reasons and not tracker.dirty:
            raise ProgressiveRouteError("condition probe reasons conflict with tracker")
        if (
            (ConditionProbeReason.LAST_UNKNOWN in reasons)
            != (tracker.status is ConditionStatus.UNKNOWN)
        ):
            raise ProgressiveRouteError("condition probe reasons conflict with tracker")
        if (
            ConditionProbeReason.SUSPECTED_NEGATIVE in reasons
            and (
                tracker._latest_snapshot is None
                or not tracker._latest_snapshot.suspected_negative
            )
        ):
            raise ProgressiveRouteError("condition probe reasons conflict with tracker")
        if ConditionProbeReason.PERIODIC_AUDIT in reasons and not _tracker_audit_due(
            tracker,
            self.global_action_turn,
        ):
            raise ProgressiveRouteError("condition probe reasons conflict with tracker")

    def bind_scenario_packet(
        self,
        packet: ScenarioPacket,
        *,
        candidate_sha256: tuple[str, ...] = (),
    ) -> None:
        if self.scenario_recollection_required:
            raise ProgressiveRouteError("scenario recollection must use the recollection binding")
        if self.scenario_packet_sha256 is not None:
            raise ProgressiveRouteError("a scenario packet is already bound")
        self._validate_scenario_packet_identity(packet, require_current_version=True)
        if packet.packet_sha256 in self.stale_scenario_packet_sha256:
            raise ProgressiveRouteError("scenario packet is stale")
        pages = _scenario_page_sha256(packet, require_five=False)
        candidates = _sha_sequence(candidate_sha256, "candidate_sha256")
        self._bind_current_scenario(packet, pages, candidates)

    def commit_scenario_decision(self, decision: ScenarioDecision) -> None:
        if not isinstance(decision, ScenarioDecision):
            raise ProgressiveRouteError("decision must be ScenarioDecision")
        if self.scenario_packet_sha256 is None:
            raise ProgressiveRouteError("no current scenario packet is bound")
        if self.scenario_decision_sha256 is not None:
            raise ProgressiveRouteError("the bound scenario packet already has a decision")
        if decision.packet_sha256 != self.scenario_packet_sha256:
            raise ProgressiveRouteError("scenario decision packet SHA mismatch")
        if decision.packet_sha256 in self.stale_scenario_packet_sha256:
            raise ProgressiveRouteError("scenario decision uses a stale packet")
        if decision.action_id not in self.scenario_allowed_action_ids:
            raise ProgressiveRouteError("scenario action is not packet-allowlisted")
        is_hold = decision.action_id == "scenario.hold"
        if is_hold == decision.invalidates_collection:
            raise ProgressiveRouteError("scenario invalidation semantics do not match the action")
        if not is_hold and len(self.scenario_training_page_sha256) != len(_TRAINING_TYPES):
            raise ProgressiveRouteError("scenario use requires five structured training pages")

        decision_sha256 = _scenario_decision_sha256(decision)
        self.record_scenario_action(
            decision.action_id,
            self.scenario_source_frame_sha256,
            packet_sha256=decision.packet_sha256,
            decision_sha256=decision_sha256,
            _bound_to_current_observation=True,
        )
        self.scenario_decision_sha256 = decision_sha256
        self.scenario_action_id = decision.action_id
        if is_hold:
            return

        self.stale_scenario_packet_sha256 = _merged(
            self.stale_scenario_packet_sha256,
            (self.scenario_packet_sha256,),
        )
        self.stale_scenario_decision_sha256 = _merged(
            self.stale_scenario_decision_sha256,
            (decision_sha256,),
        )
        self.stale_scenario_training_page_sha256 = _merged(
            self.stale_scenario_training_page_sha256,
            self.scenario_training_page_sha256,
        )
        self.stale_scenario_candidate_sha256 = _merged(
            self.stale_scenario_candidate_sha256,
            self.scenario_candidate_sha256,
        )
        self.stale_scenario_action_ids = tuple(
            dict.fromkeys((*self.stale_scenario_action_ids, decision.action_id))
        )
        self.scenario_packet_sha256 = None
        self.scenario_packet_transition_version = None
        self.scenario_source_frame_sha256 = None
        self.scenario_decision_sha256 = None
        self.scenario_action_id = None
        self.scenario_training_page_sha256 = ()
        self.scenario_candidate_sha256 = ()
        self.scenario_allowed_action_ids = ()
        self.scenario_recollection_required = True
        self.scenario_invalidated_transition_version = self.transition_version

    def bind_recollected_scenario_packet(
        self,
        packet: ScenarioPacket,
        *,
        candidate_sha256: tuple[str, ...] = (),
    ) -> None:
        if not self.scenario_recollection_required:
            raise ProgressiveRouteError("scenario recollection is not required")
        self._validate_scenario_packet_identity(packet, require_current_version=False)
        if packet.generation != self.scenario_generation + 1:
            raise ProgressiveRouteError("recollected scenario generation must advance by one")
        invalidated_version = self.scenario_invalidated_transition_version
        if invalidated_version is None or packet.transition_version <= invalidated_version:
            raise ProgressiveRouteError("recollected scenario transition version must advance")
        if packet.packet_sha256 in self.stale_scenario_packet_sha256:
            raise ProgressiveRouteError("recollected scenario packet is stale")
        if packet.critical_unknown_fields:
            raise ProgressiveRouteError("recollected scenario packet has critical unknowns")
        if packet.source_frame_sha256 == self.last_observation_sha256:
            raise ProgressiveRouteError("recollected scenario packet requires a fresh source frame")
        pages = _scenario_page_sha256(packet, require_five=True)
        if set(pages) & set(self.stale_scenario_training_page_sha256):
            raise ProgressiveRouteError("recollected scenario pages must all be fresh")
        candidates = _sha_sequence(candidate_sha256, "candidate_sha256")
        if set(candidates) & set(self.stale_scenario_candidate_sha256):
            raise ProgressiveRouteError("recollected scenario candidates must all be fresh")
        self._bind_current_scenario(packet, pages, candidates)

    def _validate_scenario_packet_identity(
        self,
        packet: ScenarioPacket,
        *,
        require_current_version: bool,
    ) -> None:
        if not isinstance(packet, ScenarioPacket):
            raise ProgressiveRouteError("packet must be ScenarioPacket")
        if packet.run_session_id != self.run_session_id:
            raise ProgressiveRouteError("scenario packet run_session_id mismatch")
        if packet.current_node_id != self.current_node_id:
            raise ProgressiveRouteError("scenario packet current node mismatch")
        if require_current_version and packet.transition_version != self.transition_version:
            raise ProgressiveRouteError("scenario packet transition version mismatch")
        if require_current_version and packet.source_frame_sha256 != self.last_observation_sha256:
            raise ProgressiveRouteError("scenario packet source frame mismatch")

    def _bind_current_scenario(
        self,
        packet: ScenarioPacket,
        pages: tuple[str, ...],
        candidates: tuple[str, ...],
    ) -> None:
        self.scenario_generation = packet.generation
        self.scenario_packet_sha256 = packet.packet_sha256
        self.scenario_packet_transition_version = packet.transition_version
        self.scenario_source_frame_sha256 = packet.source_frame_sha256
        self.scenario_decision_sha256 = None
        self.scenario_action_id = None
        self.scenario_training_page_sha256 = pages
        self.scenario_candidate_sha256 = candidates
        self.scenario_allowed_action_ids = tuple(packet.allowed_action_ids)
        self.scenario_recollection_required = False
        self.last_observation_sha256 = packet.source_frame_sha256
        self.transition_version += 1

    def observe(self, node_id: str, source_sha256: str, period_key: str, calendar_ordinal: int) -> None:
        _sha(source_sha256)
        _calendar(period_key, calendar_ordinal)
        if self.last_action_sha256 == source_sha256:
            raise ProgressiveRouteError("post-action observation must use a fresh frame")
        if node_id == "inheritance.classic.result":
            self._observe_inheritance("classic_inheritance", "classic-03-late", node_id, period_key, calendar_ordinal)
        elif node_id == "inheritance.senior.result":
            self._observe_inheritance("senior_inheritance", "senior-03-late", node_id, period_key, calendar_ordinal)
        else:
            if self.status is ProgressiveRouteStatus.BOOTSTRAP:
                self.status = ProgressiveRouteStatus.TRACKING
            self.current_node_id = node_id
            if node_id == "turn.main_menu.stable" and self.phase in {
                "classic_inheritance",
                "senior_inheritance",
            }:
                self.phase = "turn_loop"
            if node_id == "complete.main_menu":
                self.status = ProgressiveRouteStatus.COMPLETE
                self.phase = "complete"
        self.expected_next_node_ids = ()
        self.last_observation_sha256 = source_sha256
        self.transition_version += 1

    def commit_action(self, action_id: str, source_sha256: str, expected_next_node_ids: tuple[str, ...] = ()) -> None:
        if action_id in _PRIMARY_ACTIONS and self.scenario_recollection_required:
            raise ProgressiveRouteError("primary action is blocked until scenario recollection completes")
        _sha(source_sha256)
        if self.last_observation_sha256 != source_sha256:
            raise ProgressiveRouteError("action must bind to the current observation")
        self.last_action_id = action_id
        self.last_action_sha256 = source_sha256
        self.expected_next_node_ids = tuple(expected_next_node_ids)
        self.status = ProgressiveRouteStatus.SETTLING
        self.transition_version += 1

    def begin_primary_action(
        self,
        action_id: str,
        source_sha256: str,
        expected_next_node_ids: tuple[str, ...] = (),
    ) -> None:
        if self.scenario_recollection_required:
            raise ProgressiveRouteError("primary action is blocked until scenario recollection completes")
        if action_id not in _PRIMARY_ACTIONS:
            raise ProgressiveRouteError("action is not a primary action")
        if self.turn_context.primary_status != "not_started":
            raise ProgressiveRouteError("a primary action is already active")
        self.commit_action(action_id, source_sha256, expected_next_node_ids)
        self.turn_context.primary_action_id = action_id
        self.turn_context.primary_status = "pending"

    def mark_primary_complete(self, source_sha256: str) -> None:
        _sha(source_sha256)
        if self.turn_context.primary_status != "pending":
            raise ProgressiveRouteError("no pending primary action")
        if source_sha256 in {self.last_action_sha256, self.last_observation_sha256}:
            raise ProgressiveRouteError("primary completion requires a fresh frame")
        self.last_observation_sha256 = source_sha256
        self.turn_context.primary_status = "complete"
        self.status = ProgressiveRouteStatus.TRACKING
        self.transition_version += 1

    def record_event(self, event_id: str, source_sha256: str) -> None:
        record = self._record(event_id, source_sha256)
        destination = self.turn_context.pre_event_sequence if self.turn_context.primary_status == "not_started" else self.turn_context.post_event_sequence
        destination.append(record)

    def record_scenario_action(
        self,
        action_id: str,
        source_sha256: str,
        *,
        packet_sha256: str | None = None,
        decision_sha256: str | None = None,
        _bound_to_current_observation: bool = False,
    ) -> None:
        if not _bound_to_current_observation:
            self.turn_context.scenario_action_sequence.append(self._record(action_id, source_sha256))
            return
        if type(action_id) is not str or not action_id:
            raise ProgressiveRouteError("event/action id must be a non-empty string")
        _sha(source_sha256)
        if source_sha256 != self.last_observation_sha256:
            raise ProgressiveRouteError("bound scenario action requires the current observation")
        _sha(packet_sha256)
        _sha(decision_sha256)
        self.turn_context.scenario_action_sequence.append(
            {
                "id": action_id,
                "source_sha256": source_sha256,
                "packet_sha256": packet_sha256,
                "decision_sha256": decision_sha256,
            }
        )
        self.transition_version += 1

    def commit_turn(self, period_key: str, calendar_ordinal: int, source_sha256: str) -> None:
        _sha(source_sha256)
        _calendar(period_key, calendar_ordinal)
        if self.turn_context.primary_status != "complete":
            raise ProgressiveRouteError("turn requires exactly one completed primary action")
        if source_sha256 in {self.last_action_sha256, self.last_observation_sha256}:
            raise ProgressiveRouteError("turn commit requires a fresh frame")
        if calendar_ordinal != self.turn_context.calendar_ordinal + 1:
            raise ProgressiveRouteError("calendar ordinal must advance by exactly one")
        self.last_committed_turn = asdict(self.turn_context)
        next_turn = self.turn_context.global_action_turn + 1
        self.turn_context = TurnContext(
            next_turn,
            period_key,
            calendar_ordinal,
            condition_status=self.condition_status,
            condition_dirty=self.condition_dirty,
        )
        self.last_observation_sha256 = source_sha256
        self.last_action_sha256 = None
        self.last_action_id = None
        self.expected_next_node_ids = ()
        self.status = ProgressiveRouteStatus.TRACKING
        self.phase = "turn_loop"
        self.transition_version += 1

    def resync(self, node_id: str, source_sha256: str) -> None:
        _sha(source_sha256)
        self.current_node_id = node_id
        self.last_observation_sha256 = source_sha256
        self.expected_next_node_ids = ()
        self.status = ProgressiveRouteStatus.TRACKING
        self.mismatch_reason = None
        self.transition_version += 1

    def mark_mismatch(self, reason: str) -> None:
        self.status = ProgressiveRouteStatus.MISMATCH
        self.phase = "mismatch"
        self.mismatch_reason = reason
        self.transition_version += 1

    def record_settle_frame(self, source_sha256: str) -> bool:
        _sha(source_sha256)
        if source_sha256 in {
            self.last_action_sha256,
            self.last_observation_sha256,
            self.last_settle_sha256,
        }:
            return False
        self.last_settle_sha256 = source_sha256
        self.settle_count += 1
        self.transition_version += 1
        return True

    def clear_settle_frames(self) -> None:
        self.settle_count = 0
        self.last_settle_sha256 = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_session_id": self.run_session_id,
            "turn_context": asdict(self.turn_context),
            "status": self.status.value,
            "current_node_id": self.current_node_id,
            "last_action_id": self.last_action_id,
            "expected_next_node_ids": list(self.expected_next_node_ids),
            "last_observation_sha256": self.last_observation_sha256,
            "last_action_sha256": self.last_action_sha256,
            "phase": self.phase,
            "transition_version": self.transition_version,
            "settle_count": self.settle_count,
            "last_settle_sha256": self.last_settle_sha256,
            "mismatch_reason": self.mismatch_reason,
            "last_committed_turn": self.last_committed_turn,
            "scenario_generation": self.scenario_generation,
            "scenario_packet_sha256": self.scenario_packet_sha256,
            "scenario_packet_transition_version": self.scenario_packet_transition_version,
            "scenario_source_frame_sha256": self.scenario_source_frame_sha256,
            "scenario_decision_sha256": self.scenario_decision_sha256,
            "scenario_action_id": self.scenario_action_id,
            "scenario_training_page_sha256": list(self.scenario_training_page_sha256),
            "scenario_candidate_sha256": list(self.scenario_candidate_sha256),
            "scenario_allowed_action_ids": list(self.scenario_allowed_action_ids),
            "scenario_recollection_required": self.scenario_recollection_required,
            "scenario_invalidated_transition_version": self.scenario_invalidated_transition_version,
            "stale_scenario_packet_sha256": list(self.stale_scenario_packet_sha256),
            "stale_scenario_decision_sha256": list(self.stale_scenario_decision_sha256),
            "stale_scenario_training_page_sha256": list(
                self.stale_scenario_training_page_sha256
            ),
            "stale_scenario_candidate_sha256": list(self.stale_scenario_candidate_sha256),
            "stale_scenario_action_ids": list(self.stale_scenario_action_ids),
            "condition_status": self.condition_status,
            "condition_dirty": self.condition_dirty,
            "condition_report_sha256": self.condition_report_sha256,
            "condition_probe_reasons": list(self.condition_probe_reasons),
            "condition_tracker_state": (
                None
                if self.condition_tracker_state is None
                else copy.deepcopy(self.condition_tracker_state)
            ),
        }

    def save(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent, delete=False) as handle:
            json.dump(self.to_dict(), handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, destination)

    @classmethod
    def load(cls, path: str | Path) -> "ProgressiveRouteSession":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            context = TurnContext(**data["turn_context"])
            condition_status = data.get(
                "condition_status",
                context.condition_status,
            )
            if "condition_probe_reasons" in data:
                condition_probe_reasons = tuple(data["condition_probe_reasons"])
            elif condition_status == ConditionStatus.UNKNOWN.value:
                condition_probe_reasons = (
                    ConditionProbeReason.LAST_UNKNOWN.value,
                )
            else:
                condition_probe_reasons = ()
            session = cls(
                run_session_id=data["run_session_id"],
                turn_context=context,
                status=ProgressiveRouteStatus(data["status"]),
                current_node_id=data["current_node_id"],
                last_action_id=data["last_action_id"],
                expected_next_node_ids=tuple(data["expected_next_node_ids"]),
                last_observation_sha256=data["last_observation_sha256"],
                last_action_sha256=data["last_action_sha256"],
                phase=data["phase"],
                transition_version=data["transition_version"],
                settle_count=data["settle_count"],
                last_settle_sha256=data.get("last_settle_sha256"),
                mismatch_reason=data["mismatch_reason"],
                last_committed_turn=data["last_committed_turn"],
                scenario_generation=data.get("scenario_generation", 0),
                scenario_packet_sha256=data.get("scenario_packet_sha256"),
                scenario_packet_transition_version=data.get(
                    "scenario_packet_transition_version"
                ),
                scenario_source_frame_sha256=data.get("scenario_source_frame_sha256"),
                scenario_decision_sha256=data.get("scenario_decision_sha256"),
                scenario_action_id=data.get("scenario_action_id"),
                scenario_training_page_sha256=tuple(
                    data.get("scenario_training_page_sha256", ())
                ),
                scenario_candidate_sha256=tuple(data.get("scenario_candidate_sha256", ())),
                scenario_allowed_action_ids=tuple(data.get("scenario_allowed_action_ids", ())),
                scenario_recollection_required=data.get(
                    "scenario_recollection_required",
                    False,
                ),
                scenario_invalidated_transition_version=data.get(
                    "scenario_invalidated_transition_version"
                ),
                stale_scenario_packet_sha256=tuple(
                    data.get("stale_scenario_packet_sha256", ())
                ),
                stale_scenario_decision_sha256=tuple(
                    data.get("stale_scenario_decision_sha256", ())
                ),
                stale_scenario_training_page_sha256=tuple(
                    data.get("stale_scenario_training_page_sha256", ())
                ),
                stale_scenario_candidate_sha256=tuple(
                    data.get("stale_scenario_candidate_sha256", ())
                ),
                stale_scenario_action_ids=tuple(data.get("stale_scenario_action_ids", ())),
                condition_status=condition_status,
                condition_dirty=data.get("condition_dirty", context.condition_dirty),
                condition_report_sha256=data.get("condition_report_sha256"),
                condition_probe_reasons=condition_probe_reasons,
                condition_tracker_state=data.get("condition_tracker_state"),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProgressiveRouteError("invalid progressive route session") from exc
        cls.new(session.run_session_id, context.period_key, context.calendar_ordinal)
        for value in (session.last_observation_sha256, session.last_action_sha256, session.last_settle_sha256):
            if value is not None:
                _sha(value)
        session._validate_loaded_scenario_state()
        session._validate_loaded_condition_state()
        return session

    def _validate_loaded_condition_state(self) -> None:
        try:
            status = ConditionStatus(self.condition_status)
        except (TypeError, ValueError) as exc:
            raise ProgressiveRouteError("invalid condition status") from exc
        if type(self.condition_dirty) is not bool:
            raise ProgressiveRouteError("invalid condition dirty flag")
        if self.condition_report_sha256 is not None:
            _sha(self.condition_report_sha256)
        try:
            reasons = tuple(ConditionProbeReason(value) for value in self.condition_probe_reasons)
        except (TypeError, ValueError) as exc:
            raise ProgressiveRouteError("invalid condition probe reason") from exc
        ordered = tuple(reason for reason in ConditionProbeReason if reason in reasons)
        if reasons != ordered:
            raise ProgressiveRouteError("condition probe reasons are not ordered and distinct")
        if status is ConditionStatus.UNKNOWN:
            if ConditionProbeReason.LAST_UNKNOWN not in reasons:
                raise ProgressiveRouteError(
                    "unknown condition must retain last_unknown"
                )
        elif ConditionProbeReason.LAST_UNKNOWN in reasons:
            raise ProgressiveRouteError(
                "conclusive condition cannot retain last_unknown"
            )
        if self.turn_context.condition_status != status.value:
            raise ProgressiveRouteError("turn condition status mismatch")
        if self.turn_context.condition_dirty is not self.condition_dirty:
            raise ProgressiveRouteError("turn condition dirty mismatch")

    def _validate_loaded_scenario_state(self) -> None:
        if type(self.scenario_generation) is not int or self.scenario_generation < 0:
            raise ProgressiveRouteError("invalid scenario generation")
        if type(self.scenario_recollection_required) is not bool:
            raise ProgressiveRouteError("invalid scenario recollection flag")
        for value in (
            self.scenario_packet_sha256,
            self.scenario_source_frame_sha256,
            self.scenario_decision_sha256,
        ):
            if value is not None:
                _sha(value)
        for value in (
            self.scenario_packet_transition_version,
            self.scenario_invalidated_transition_version,
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ProgressiveRouteError("invalid scenario transition version")
            if value is not None and value > self.transition_version:
                raise ProgressiveRouteError("scenario transition version exceeds session")
        for values, label in (
            (self.scenario_training_page_sha256, "scenario_training_page_sha256"),
            (self.scenario_candidate_sha256, "scenario_candidate_sha256"),
            (self.stale_scenario_packet_sha256, "stale_scenario_packet_sha256"),
            (self.stale_scenario_decision_sha256, "stale_scenario_decision_sha256"),
            (
                self.stale_scenario_training_page_sha256,
                "stale_scenario_training_page_sha256",
            ),
            (self.stale_scenario_candidate_sha256, "stale_scenario_candidate_sha256"),
        ):
            if values != _sha_sequence(values, label):
                raise ProgressiveRouteError(f"{label} must be sorted and distinct")
        if len(self.scenario_training_page_sha256) > len(_TRAINING_TYPES):
            raise ProgressiveRouteError("too many scenario training page hashes")
        if any(type(value) is not str or not value for value in self.scenario_allowed_action_ids):
            raise ProgressiveRouteError("invalid scenario allowed action id")
        if len(set(self.scenario_allowed_action_ids)) != len(self.scenario_allowed_action_ids):
            raise ProgressiveRouteError("duplicate scenario allowed action id")
        if any(type(value) is not str or not value for value in self.stale_scenario_action_ids):
            raise ProgressiveRouteError("invalid stale scenario action id")
        if len(set(self.stale_scenario_action_ids)) != len(self.stale_scenario_action_ids):
            raise ProgressiveRouteError("duplicate stale scenario action id")

        current_values = (
            self.scenario_packet_transition_version,
            self.scenario_source_frame_sha256,
            self.scenario_decision_sha256,
            self.scenario_action_id,
        )
        if self.scenario_packet_sha256 is None:
            if any(value is not None for value in current_values):
                raise ProgressiveRouteError("unbound scenario state contains current hashes")
            if self.scenario_training_page_sha256 or self.scenario_candidate_sha256:
                raise ProgressiveRouteError("unbound scenario state contains current collection")
            if self.scenario_allowed_action_ids:
                raise ProgressiveRouteError("unbound scenario state contains allowed actions")
        else:
            if self.scenario_packet_sha256 in self.stale_scenario_packet_sha256:
                raise ProgressiveRouteError("current scenario packet is stale")
            if self.scenario_packet_transition_version is None or self.scenario_source_frame_sha256 is None:
                raise ProgressiveRouteError("bound scenario packet metadata is incomplete")
            if not self.scenario_allowed_action_ids:
                raise ProgressiveRouteError("bound scenario packet has no allowed action")
            if set(self.scenario_training_page_sha256) & set(
                self.stale_scenario_training_page_sha256
            ):
                raise ProgressiveRouteError("current scenario page is stale")
            if set(self.scenario_candidate_sha256) & set(
                self.stale_scenario_candidate_sha256
            ):
                raise ProgressiveRouteError("current scenario candidate is stale")
            if (self.scenario_decision_sha256 is None) != (self.scenario_action_id is None):
                raise ProgressiveRouteError("scenario decision binding is incomplete")
            if self.scenario_action_id is not None and self.scenario_action_id not in self.scenario_allowed_action_ids:
                raise ProgressiveRouteError("scenario decision action is not allowed")
        if self.scenario_recollection_required:
            if self.scenario_packet_sha256 is not None:
                raise ProgressiveRouteError("recollection state still has a current packet")
            if self.scenario_invalidated_transition_version is None:
                raise ProgressiveRouteError("recollection state lacks invalidation version")

    def _record(self, identifier: str, source_sha256: str) -> dict[str, str]:
        if type(identifier) is not str or not identifier:
            raise ProgressiveRouteError("event/action id must be a non-empty string")
        _sha(source_sha256)
        if source_sha256 in {self.last_action_sha256, self.last_observation_sha256}:
            raise ProgressiveRouteError("event/action requires a fresh frame")
        self.last_observation_sha256 = source_sha256
        self.transition_version += 1
        return {"id": identifier, "source_sha256": source_sha256}

    def _observe_inheritance(self, phase: str, expected_key: str, node_id: str, period_key: str, ordinal: int) -> None:
        if period_key != expected_key:
            raise ProgressiveRouteError("inheritance calendar key mismatch")
        if self.phase not in {"bootstrap", "turn_loop", phase}:
            raise ProgressiveRouteError("inheritance phase crossing is forbidden")
        self.phase = phase
        self.current_node_id = node_id
        self.status = ProgressiveRouteStatus.TRACKING


def _scenario_page_sha256(
    packet: ScenarioPacket,
    *,
    require_five: bool,
) -> tuple[str, ...]:
    pages = packet.mechanism_state.get("training_pages")
    if not isinstance(pages, Mapping):
        raise ProgressiveRouteError("scenario packet training_pages must be an object")
    if any(training_type not in _TRAINING_TYPES for training_type in pages):
        raise ProgressiveRouteError("scenario packet contains an unknown training page")
    if require_five and set(pages) != set(_TRAINING_TYPES):
        raise ProgressiveRouteError("scenario recollection requires all five training pages")
    values: list[str] = []
    for training_type in _TRAINING_TYPES:
        if training_type not in pages:
            continue
        page = pages[training_type]
        if not isinstance(page, Mapping):
            raise ProgressiveRouteError("scenario training page must be an object")
        source_sha256 = page.get("source_frame_sha256")
        _sha(source_sha256)
        values.append(source_sha256)
    return _sha_sequence(tuple(values), "scenario training page SHA")


def _scenario_decision_sha256(decision: ScenarioDecision) -> str:
    payload = {
        "action_id": decision.action_id,
        "packet_sha256": decision.packet_sha256,
        "source": decision.source,
        "confidence": decision.confidence,
        "invalidates_collection": decision.invalidates_collection,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha_sequence(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise ProgressiveRouteError(f"{label} must be a SHA sequence")
    result = tuple(value)
    for item in result:
        _sha(item)
    if len(set(result)) != len(result):
        raise ProgressiveRouteError(f"{label} must contain distinct SHA values")
    return tuple(sorted(result))


def _merged(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(first) | set(second)))


def _sha(value: str) -> None:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise ProgressiveRouteError("source_sha256 must be lower-case 64-hex")


def _tracker_audit_due(tracker: ConditionTracker, global_action_turn: int) -> bool:
    if tracker.audit_interval in (None, 0):
        return False
    baseline = tracker.last_explicit_probe_turn or 0
    return global_action_turn - baseline >= tracker.audit_interval


def _calendar(key: str, ordinal: int) -> None:
    if type(key) is not str or not key or type(ordinal) is not int:
        raise ProgressiveRouteError("invalid calendar")
