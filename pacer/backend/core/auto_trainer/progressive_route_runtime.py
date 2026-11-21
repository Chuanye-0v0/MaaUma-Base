from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from core.auto_trainer.condition_context import condition_allows_clinic
from core.auto_trainer.condition_tracker import (
    ConditionQuickSnapshot,
    ConditionStatus,
    ConditionTracker,
)
from core.auto_trainer.grand_masters_scenario import (
    GRAND_MASTERS_ACTION_IDS,
    GrandMastersScenarioAdapter,
    GrandMastersScenarioRequest,
    GrandMastersTrainingPage,
)
from core.auto_trainer.progressive_route_action_port import (
    PendingRouteDirective,
    RouteDirective,
    RouteExecutionResult,
    bind_route_directive,
    canonical_directive_binding_sha256,
)
from core.auto_trainer.progressive_route_controller import (
    ProgressiveRouteController,
    RouteControllerDecision,
    RouteFrameObservation,
)
from core.auto_trainer.progressive_route_graph import (
    ProgressiveRouteConfigError,
    ProgressiveRouteGraph,
)
from core.auto_trainer.progressive_route_policy import build_player_scenario_policy
from core.auto_trainer.progressive_route_recognizer import ScopedRouteRecognizer
from core.auto_trainer.progressive_route_session import (
    ProgressiveRouteError,
    ProgressiveRouteSession,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRAINING_TYPES = ("speed", "stamina", "power", "guts", "wisdom")
_PRIMARY_ACTION_IDS = frozenset(
    {
        "primary.training",
        "primary.race",
        "primary.outing",
        "primary.rest",
        "primary.clinic",
    }
)


@dataclass(frozen=True)
class CapturedRouteFrame:
    path: str
    source_sha256: str
    captured_at: float
    stable: bool
    period_key: str
    calendar_ordinal: int
    perception: dict[str, object]

    def __post_init__(self) -> None:
        if type(self.path) is not str or not self.path:
            raise ProgressiveRouteError("frame path must be non-empty")
        _sha256(self.source_sha256, "source_sha256")
        if (
            isinstance(self.captured_at, bool)
            or not isinstance(self.captured_at, (int, float))
            or not math.isfinite(float(self.captured_at))
            or self.captured_at <= 0
        ):
            raise ProgressiveRouteError("captured_at must be positive and finite")
        if not isinstance(self.stable, bool):
            raise ProgressiveRouteError("stable must be boolean")
        if type(self.period_key) is not str or not self.period_key:
            raise ProgressiveRouteError("period_key must be non-empty")
        if (
            isinstance(self.calendar_ordinal, bool)
            or not isinstance(self.calendar_ordinal, int)
            or self.calendar_ordinal < 0
        ):
            raise ProgressiveRouteError("calendar_ordinal must be non-negative")
        if not isinstance(self.perception, dict):
            raise ProgressiveRouteError("perception must be an object")


class ProgressiveRouteRuntime:
    def __init__(
        self,
        graph: ProgressiveRouteGraph,
        session: ProgressiveRouteSession,
        recognizer: ScopedRouteRecognizer,
        *,
        unknown_page_policy: str = "manual_on_unknown",
        scenario_policy_id: str = "hold",
        usable_goddess_action_ids: tuple[str, ...] = GRAND_MASTERS_ACTION_IDS[1:],
        goddess_availability_known: bool = True,
    ) -> None:
        if not isinstance(graph, ProgressiveRouteGraph):
            raise TypeError("graph must be ProgressiveRouteGraph")
        if not isinstance(session, ProgressiveRouteSession):
            raise TypeError("session must be ProgressiveRouteSession")
        if unknown_page_policy not in {"manual_on_unknown", "allowlisted_default"}:
            raise ValueError("unknown_page_policy is invalid")
        if not isinstance(usable_goddess_action_ids, tuple) or any(
            action_id not in GRAND_MASTERS_ACTION_IDS[1:]
            for action_id in usable_goddess_action_ids
        ):
            raise ProgressiveRouteError(
                "usable_goddess_action_ids must contain real Goddess actions"
            )
        if len(set(usable_goddess_action_ids)) != len(usable_goddess_action_ids):
            raise ProgressiveRouteError("usable_goddess_action_ids must be distinct")
        if type(goddess_availability_known) is not bool:
            raise ProgressiveRouteError("goddess_availability_known must be boolean")
        self.graph = graph
        self.session = session
        self.recognizer = recognizer
        self.unknown_page_policy = unknown_page_policy
        self.controller = ProgressiveRouteController(graph, session)
        audit_interval = (
            graph.settings.condition_audit_interval
            if graph.settings is not None
            else 6
        )
        self.condition_tracker = session.restore_condition_tracker(audit_interval)
        self.scenario_adapter = GrandMastersScenarioAdapter(
            player_policy=build_player_scenario_policy(scenario_policy_id)
        )
        self.usable_goddess_action_ids = usable_goddess_action_ids
        self.goddess_availability_known = goddess_availability_known
        self._last_condition_snapshot: ConditionQuickSnapshot | None = None
        self._current_condition_assessment: dict[str, object] | None = None
        self._pending_directive: PendingRouteDirective | None = None
        self._pending_source_frame_sha256: str | None = None
        self._sealed_directive: RouteDirective | None = None
        self._last_directive_sequence = 0
        self._terminal_reason: str | None = None

    def observe_condition_snapshot(
        self,
        snapshot: ConditionQuickSnapshot,
        *,
        critical_race: bool = False,
    ) -> PendingRouteDirective | None:
        self._require_idle()
        if not isinstance(snapshot, ConditionQuickSnapshot):
            raise TypeError("snapshot must be ConditionQuickSnapshot")
        if self._last_condition_snapshot is None:
            self.condition_tracker.observe_main_menu(snapshot)
        else:
            self.condition_tracker.observe_event_change(
                self._last_condition_snapshot,
                snapshot,
            )
        self._last_condition_snapshot = snapshot
        plan = self.condition_tracker.plan_probe(
            self.session.global_action_turn,
            critical_race=critical_race,
        )
        self.session.apply_condition_tracker(self.condition_tracker, plan)
        return self._condition_probe_directive(plan.reasons) if plan.required else None

    def accept_condition_report(
        self,
        assessment: Mapping[str, object],
    ) -> PendingRouteDirective:
        self._require_idle()
        normalized = self._validated_condition_assessment(assessment)
        self.condition_tracker.apply_assessment(
            normalized,
            self.session.global_action_turn,
        )
        plan = self.condition_tracker.plan_probe(self.session.global_action_turn)
        self.session.apply_condition_tracker(self.condition_tracker, plan)
        self._current_condition_assessment = copy.deepcopy(normalized)
        self._last_condition_snapshot = None
        if plan.required:
            return self._condition_probe_directive(plan.reasons)
        return self._collect_scenario_directive("condition_assessment_accepted")

    def bind_training_pages(
        self,
        pages: tuple[GrandMastersTrainingPage, ...],
        *,
        candidate_sha256: tuple[str, ...] = (),
    ) -> PendingRouteDirective:
        self._require_idle()
        plan = self.condition_tracker.plan_probe(self.session.global_action_turn)
        self.session.apply_condition_tracker(self.condition_tracker, plan)
        if plan.required:
            return self._condition_probe_directive(plan.reasons)
        if not self._five_fresh_training_pages(pages):
            return self._collect_scenario_directive("five_fresh_training_pages_required")
        source_frame_sha256 = self.session.last_observation_sha256
        generation = self.session.scenario_generation
        if self.session.scenario_recollection_required:
            generation += 1
            source_frame_sha256 = pages[-1].source_frame_sha256
        if source_frame_sha256 is None:
            raise ProgressiveRouteError("scenario source frame is unavailable")
        request = GrandMastersScenarioRequest(
            run_session_id=self.session.run_session_id,
            transition_version=self.session.transition_version,
            generation=generation,
            source_frame_sha256=source_frame_sha256,
            current_node_id=self.session.current_node_id,
            training_pages=pages,
            usable_goddess_action_ids=self.usable_goddess_action_ids,
            goddess_availability_known=self.goddess_availability_known,
        )
        if self.session.scenario_recollection_required:
            try:
                packet = self.controller.build_recollected_scenario_packet(
                    adapter=self.scenario_adapter,
                    request=request,
                )
            except ProgressiveRouteError:
                return self._collect_scenario_directive(
                    "five_fresh_training_pages_required"
                )
        else:
            packet = self.controller.build_scenario_packet(
                adapter=self.scenario_adapter,
                request=request,
            )
        try:
            decision = self.controller.choose_and_commit_scenario_action(
                adapter=self.scenario_adapter,
                packet=packet,
                candidate_sha256=candidate_sha256,
                recollection=self.session.scenario_recollection_required,
            )
        except ProgressiveRouteError:
            return self._collect_scenario_directive(
                "five_fresh_training_pages_required"
            )
        expected_nodes = self.session.expected_next_node_ids
        pending = PendingRouteDirective(
            kind="execute",
            reason="scenario_decision",
            action_id=decision.action_id,
            expected_node_ids=expected_nodes,
            delay_seconds=None,
            scenario_allowed_action_ids=tuple(packet.allowed_action_ids),
        )
        self._pending_directive = pending
        self._pending_source_frame_sha256 = source_frame_sha256
        return pending

    def next_directive(self) -> PendingRouteDirective:
        self._require_idle()
        expected = (
            self.session.expected_next_node_ids
            or self.graph.neighbor_nodes(self.session.current_node_id)
        )
        return PendingRouteDirective(
            kind="capture",
            reason="route_observation_required",
            action_id=None,
            expected_node_ids=expected,
            delay_seconds=None,
        )

    def accept_frame(
        self,
        frame: CapturedRouteFrame,
        *,
        recovery: bool = False,
    ) -> PendingRouteDirective:
        self._require_idle()
        if not isinstance(frame, CapturedRouteFrame):
            raise TypeError("frame must be CapturedRouteFrame")
        observation = RouteFrameObservation(
            source_sha256=frame.source_sha256,
            stable=frame.stable,
            period_key=frame.period_key,
            calendar_ordinal=frame.calendar_ordinal,
            perception=dict(frame.perception),
        )
        decision = self.controller.handle_frame(
            observation,
            self.recognizer,
            unknown_page_policy=self.unknown_page_policy,
            resync=recovery,
        )
        pending = self._from_controller_decision(decision)
        if pending.kind == "execute":
            self._pending_directive = pending
            self._pending_source_frame_sha256 = frame.source_sha256
        return pending

    def seal_pending_directive(
        self,
        pending: PendingRouteDirective,
        *,
        run_id: str,
        session_id: str,
        run_session_id: str,
        directive_sequence: int,
        source_frame_sha256: str,
        transition_packet_sha256: str,
    ) -> RouteDirective:
        self._require_not_terminal()
        if self._sealed_directive is not None:
            raise ProgressiveRouteError("runtime has sealed execution")
        if (
            session_id != self.session.run_session_id
            or run_session_id != self.session.run_session_id
        ):
            raise ProgressiveRouteError(
                "session_id and run_session_id must match runtime session"
            )
        if pending is not self._pending_directive:
            raise ProgressiveRouteError("directive is not the current pending directive")
        if source_frame_sha256 != self._pending_source_frame_sha256:
            raise ProgressiveRouteError("source_frame_sha256 does not match pending frame")
        if (
            isinstance(directive_sequence, bool)
            or not isinstance(directive_sequence, int)
            or directive_sequence <= self._last_directive_sequence
        ):
            raise ProgressiveRouteError("directive_sequence must be strictly monotonic")
        if pending.kind != "execute":
            raise ProgressiveRouteError("pending kind must be execute")
        current_node_id = self.session.current_node_id
        try:
            authorized_expected_nodes = self.graph.expected_nodes(
                current_node_id,
                pending.action_id,
            )
        except ProgressiveRouteConfigError as exc:
            raise ProgressiveRouteError(
                "pending action is outside current graph/controller authority"
            ) from exc
        if pending.expected_node_ids != authorized_expected_nodes:
            raise ProgressiveRouteError(
                "pending expected nodes exceed current graph/controller authority"
            )
        self._validate_primary_before_port(pending.action_id)
        directive = bind_route_directive(
            pending,
            run_id=run_id,
            session_id=session_id,
            run_session_id=run_session_id,
            directive_sequence=directive_sequence,
            source_frame_sha256=source_frame_sha256,
            transition_packet_sha256=transition_packet_sha256,
        )
        self._sealed_directive = directive
        self._last_directive_sequence = directive_sequence
        self._pending_directive = None
        self._pending_source_frame_sha256 = None
        return directive

    def accept_execution(
        self,
        result: RouteExecutionResult,
    ) -> PendingRouteDirective:
        self._require_not_terminal()
        expected = self._sealed_directive
        if expected is None or not isinstance(result, RouteExecutionResult):
            return self._terminal_stop("execution_binding_mismatch")
        checks = (
            (result.action_id, expected.action_id, "execution_action_mismatch"),
            (
                result.source_frame_sha256,
                expected.source_frame_sha256,
                "execution_source_mismatch",
            ),
            (result.run_id, expected.run_id, "execution_run_mismatch"),
            (result.session_id, expected.session_id, "execution_session_mismatch"),
            (
                result.run_session_id,
                expected.run_session_id,
                "execution_run_session_mismatch",
            ),
            (
                result.transition_packet_sha256,
                expected.transition_packet_sha256,
                "execution_packet_mismatch",
            ),
            (
                result.directive_sequence,
                expected.directive_sequence,
                "execution_sequence_mismatch",
            ),
        )
        for actual, wanted, reason in checks:
            if actual != wanted:
                return self._terminal_stop(reason)
        recomputed_binding = canonical_directive_binding_sha256(
            run_id=result.run_id,
            session_id=result.session_id,
            run_session_id=result.run_session_id,
            directive_sequence=result.directive_sequence,
            kind=expected.kind,
            action_id=result.action_id,
            source_frame_sha256=result.source_frame_sha256,
            transition_packet_sha256=result.transition_packet_sha256,
        )
        if (
            result.directive_binding_sha256 != expected.directive_binding_sha256
            or result.directive_binding_sha256 != recomputed_binding
        ):
            return self._terminal_stop("execution_binding_mismatch")
        expected_performed = {
            "dry_run": False,
            "performed": True,
            "rejected": False,
            "unsupported": False,
        }
        if type(result.status) is not str or result.status not in expected_performed:
            return self._terminal_stop("execution_status_invalid")
        if (
            type(result.performed) is not bool
            or result.performed is not expected_performed[result.status]
        ):
            return self._terminal_stop("execution_status_mismatch")
        if result.status == "unsupported":
            return self._terminal_stop("unsupported_live_action")
        if result.status == "rejected":
            return self._terminal_stop("execution_rejected")

        if expected.action_id in _PRIMARY_ACTION_IDS:
            self.session.begin_primary_action(
                expected.action_id,
                expected.source_frame_sha256,
                expected.expected_node_ids,
            )
        else:
            self.session.commit_action(
                expected.action_id,
                expected.source_frame_sha256,
                expected.expected_node_ids,
            )
        self._sealed_directive = None
        return self.next_directive()

    def _require_not_terminal(self) -> None:
        if self._terminal_reason is not None:
            raise ProgressiveRouteError(
                f"runtime is terminal: {self._terminal_reason}"
            )

    def _require_idle(self) -> None:
        self._require_not_terminal()
        if self._sealed_directive is not None:
            raise ProgressiveRouteError("runtime has sealed execution")
        if self._pending_directive is not None:
            raise ProgressiveRouteError("runtime has pending execution")

    def _terminal_stop(self, reason: str) -> PendingRouteDirective:
        self._terminal_reason = reason
        self._pending_directive = None
        self._pending_source_frame_sha256 = None
        self._sealed_directive = None
        return self._stop(reason)

    def _validate_primary_before_port(self, action_id: str) -> None:
        if action_id not in _PRIMARY_ACTION_IDS:
            return
        if self.session.turn_context.primary_status != "not_started":
            raise ProgressiveRouteError("a primary action is already active")
        if (
            self.session.condition_dirty
            or self.session.condition_probe_reasons
            or self._current_condition_assessment is None
        ):
            raise ProgressiveRouteError("primary action requires condition authority")
        if (
            self.session.scenario_recollection_required
            or len(self.session.scenario_training_page_sha256) != 5
            or self.session.scenario_decision_sha256 is None
        ):
            raise ProgressiveRouteError("primary action requires scenario preparation")
        if action_id == "primary.clinic" and not self._clinic_allowed():
            raise ProgressiveRouteError(
                "primary.clinic requires a formally allowed negative condition"
            )

    def _clinic_allowed(self) -> bool:
        return (
            self._current_condition_assessment is not None
            and not self.condition_tracker.dirty
            and condition_allows_clinic(self._current_condition_assessment)
        )

    def _five_fresh_training_pages(self, pages: object) -> bool:
        if not isinstance(pages, tuple) or len(pages) != len(_TRAINING_TYPES):
            return False
        if any(not isinstance(page, GrandMastersTrainingPage) for page in pages):
            return False
        if {page.training_type for page in pages} != set(_TRAINING_TYPES):
            return False
        page_sha256 = tuple(page.source_frame_sha256 for page in pages)
        if len(set(page_sha256)) != len(page_sha256):
            return False
        return not bool(
            set(page_sha256) & set(self.session.stale_scenario_training_page_sha256)
        )

    def _validated_condition_assessment(
        self,
        assessment: Mapping[str, object],
    ) -> dict[str, object]:
        required = {
            "status",
            "conditions",
            "source_sha256",
            "report_sha256",
            "artifact_sha256",
            "warnings",
        }
        if not isinstance(assessment, Mapping) or set(assessment) != required:
            raise ProgressiveRouteError("condition assessment fields are invalid")
        try:
            status = ConditionStatus(assessment["status"])
        except (TypeError, ValueError) as exc:
            raise ProgressiveRouteError("condition assessment status is invalid") from exc
        conditions = assessment["conditions"]
        warnings = assessment["warnings"]
        if not isinstance(conditions, (tuple, list)) or any(
            not isinstance(condition, Mapping) for condition in conditions
        ):
            raise ProgressiveRouteError("condition assessment conditions are invalid")
        if not isinstance(warnings, (tuple, list)) or any(
            type(warning) is not str for warning in warnings
        ):
            raise ProgressiveRouteError("condition assessment warnings are invalid")
        source_sha256 = _sha256(
            assessment["source_sha256"],
            "condition source_sha256",
        )
        report_sha256 = _sha256(
            assessment["report_sha256"],
            "condition report_sha256",
        )
        artifact_sha256 = _sha256(
            assessment["artifact_sha256"],
            "condition artifact_sha256",
        )
        if source_sha256 != artifact_sha256:
            raise ProgressiveRouteError(
                "condition source and artifact SHA mismatch"
            )
        if status in {ConditionStatus.UNKNOWN, ConditionStatus.HEALTHY}:
            if conditions:
                raise ProgressiveRouteError(
                    "condition status conflicts with conditions"
                )
        elif not conditions:
            raise ProgressiveRouteError("condition status requires conditions")
        expected_context = {
            "condition_source_sha256": source_sha256,
            "report_sha256": report_sha256,
            "artifact_sha256": artifact_sha256,
        }
        if status in {ConditionStatus.REGISTERED, ConditionStatus.NEGATIVE}:
            for condition in conditions:
                if condition.get("source_sha256") != source_sha256:
                    raise ProgressiveRouteError(
                        "condition source SHA binding mismatch"
                    )
                formal_context = condition.get("formal_context")
                if (
                    not isinstance(formal_context, Mapping)
                    or dict(formal_context) != expected_context
                ):
                    raise ProgressiveRouteError(
                        "condition formal_context binding mismatch"
                    )
        return {
            "status": status.value,
            "conditions": tuple(copy.deepcopy(tuple(conditions))),
            "source_sha256": source_sha256,
            "report_sha256": report_sha256,
            "artifact_sha256": artifact_sha256,
            "warnings": tuple(copy.deepcopy(tuple(warnings))),
        }

    @staticmethod
    def _condition_probe_directive(reasons: object) -> PendingRouteDirective:
        reason_values = tuple(
            reason.value if hasattr(reason, "value") else str(reason)
            for reason in reasons
        )
        return PendingRouteDirective(
            kind="explicit_probe",
            reason="condition_probe:" + ",".join(reason_values),
            action_id=None,
            expected_node_ids=(),
            delay_seconds=None,
        )

    @staticmethod
    def _collect_scenario_directive(reason: str) -> PendingRouteDirective:
        return PendingRouteDirective(
            kind="collect_scenario",
            reason=reason,
            action_id=None,
            expected_node_ids=(),
            delay_seconds=None,
        )

    def _from_controller_decision(
        self,
        decision: RouteControllerDecision,
    ) -> PendingRouteDirective:
        if decision.kind == "wait":
            return PendingRouteDirective(
                kind="wait",
                reason=decision.reason,
                action_id=None,
                expected_node_ids=self.session.expected_next_node_ids,
                delay_seconds=decision.delay_seconds,
            )
        if decision.kind == "stop":
            return self._terminal_stop(decision.reason)
        if decision.kind == "action":
            if decision.node_id is None or decision.action_id is None:
                raise ProgressiveRouteError("controller action decision is incomplete")
            expected = self.graph.expected_nodes(
                decision.node_id,
                decision.action_id,
            )
            return PendingRouteDirective(
                kind="execute",
                reason=decision.reason,
                action_id=decision.action_id,
                expected_node_ids=expected,
                delay_seconds=None,
                scenario_allowed_action_ids=self.session.scenario_allowed_action_ids,
            )
        if decision.kind == "track":
            return self.next_directive()
        raise ProgressiveRouteError("controller returned an unknown decision kind")

    @staticmethod
    def _stop(reason: str) -> PendingRouteDirective:
        return PendingRouteDirective(
            kind="stop",
            reason=reason,
            action_id=None,
            expected_node_ids=(),
            delay_seconds=None,
        )


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ProgressiveRouteError(f"{label} must be a lowercase SHA-256")
    return value
