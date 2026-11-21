from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from core.auto_trainer.condition_tracker import (
    ConditionProbePlan,
    ConditionQuickSnapshot,
    ConditionTracker,
)
from core.auto_trainer.progressive_route_graph import ProgressiveRouteConfigError, ProgressiveRouteGraph
from core.auto_trainer.progressive_route_session import ProgressiveRouteError, ProgressiveRouteSession
from core.auto_trainer.scenario_adapter import (
    AdvisorChoice,
    ScenarioAdapter,
    ScenarioDecision,
    ScenarioPacket,
)


@dataclass(frozen=True)
class RouteFrameObservation:
    source_sha256: str
    stable: bool
    period_key: str
    calendar_ordinal: int
    perception: dict[str, Any]


@dataclass(frozen=True)
class RouteControllerDecision:
    kind: str
    reason: str
    node_id: str | None
    action_id: str | None
    delay_seconds: float | None
    candidate_node_ids: tuple[str, ...]


class ScopedRouteDetector(Protocol):
    def match_expected(
        self,
        frame: RouteFrameObservation,
        candidate_route_ids: tuple[str, ...],
    ) -> str | None: ...

    def match_recovery(self, frame: RouteFrameObservation) -> str | None: ...


RouteDetector = (
    Callable[[RouteFrameObservation, tuple[str, ...]], str | None]
    | ScopedRouteDetector
)


class ProgressiveRouteController:
    def __init__(self, graph: ProgressiveRouteGraph, session: ProgressiveRouteSession, *, settle_delay_seconds: float = 1.0, max_settle_frames: int = 5) -> None:
        self.graph = graph
        self.session = session
        self.settle_delay_seconds = settle_delay_seconds
        self.max_settle_frames = max_settle_frames

    def build_scenario_packet(
        self,
        *,
        adapter: ScenarioAdapter,
        request: Any,
    ) -> ScenarioPacket:
        packet = adapter.build_packet(request)
        self._validate_scenario_packet_authority(adapter, packet)
        return packet

    def build_recollected_scenario_packet(
        self,
        *,
        adapter: ScenarioAdapter,
        request: Any,
    ) -> ScenarioPacket:
        if not self.session.scenario_recollection_required:
            raise ProgressiveRouteError("scenario recollection is not required")
        packet = adapter.build_packet(request)
        self._validate_scenario_packet_authority(
            adapter,
            packet,
            recollection=True,
        )
        return packet

    def plan_condition_probe(
        self,
        *,
        tracker: ConditionTracker,
        snapshot: ConditionQuickSnapshot,
        critical_race: bool = False,
    ) -> ConditionProbePlan:
        """Plan a later explicit probe from caller-supplied structured state."""
        if not isinstance(tracker, ConditionTracker):
            raise ProgressiveRouteError("tracker must be ConditionTracker")
        tracker.observe_main_menu(snapshot)
        return tracker.plan_probe(
            self.session.global_action_turn,
            critical_race=critical_race,
        )

    def choose_scenario_action(
        self,
        *,
        adapter: ScenarioAdapter,
        packet: ScenarioPacket,
        advisor_choice: AdvisorChoice | dict[str, object] | None = None,
        candidate_sha256: tuple[str, ...] = (),
    ) -> ScenarioDecision:
        return self.choose_and_commit_scenario_action(
            adapter=adapter,
            packet=packet,
            advisor_choice=advisor_choice,
            candidate_sha256=candidate_sha256,
            recollection=False,
        )

    def choose_and_commit_scenario_action(
        self,
        *,
        adapter: ScenarioAdapter,
        packet: ScenarioPacket,
        advisor_choice: AdvisorChoice | dict[str, object] | None = None,
        candidate_sha256: tuple[str, ...] = (),
        recollection: bool = False,
    ) -> ScenarioDecision:
        if type(recollection) is not bool:
            raise ProgressiveRouteError("recollection must be boolean")
        if recollection != self.session.scenario_recollection_required:
            raise ProgressiveRouteError("scenario recollection state mismatch")
        self._validate_scenario_packet_authority(
            adapter,
            packet,
            recollection=recollection,
        )
        decision, expected_nodes = self._choose_and_validate_scenario_action(
            adapter,
            packet,
            advisor_choice,
        )
        self._validate_scenario_decision_commit(packet, decision)
        if recollection:
            self.session.bind_recollected_scenario_packet(
                packet,
                candidate_sha256=candidate_sha256,
            )
        else:
            self.session.bind_scenario_packet(
                packet,
                candidate_sha256=candidate_sha256,
            )
        self._commit_scenario_decision(decision, expected_nodes)
        return decision

    def _choose_and_validate_scenario_action(
        self,
        adapter: ScenarioAdapter,
        packet: ScenarioPacket,
        advisor_choice: AdvisorChoice | dict[str, object] | None,
    ) -> tuple[ScenarioDecision, tuple[str, ...]]:
        decision = adapter.choose(packet, advisor_choice)
        if not isinstance(decision, ScenarioDecision):
            raise ProgressiveRouteError("scenario adapter returned an invalid decision")
        if decision.packet_sha256 != packet.packet_sha256:
            raise ProgressiveRouteError("scenario decision packet SHA mismatch")
        if decision.action_id not in packet.allowed_action_ids:
            raise ProgressiveRouteError("scenario decision action is not packet-allowlisted")
        expected_nodes = self._action_specific_expected_nodes(adapter, packet, decision)
        return decision, expected_nodes

    @staticmethod
    def _validate_scenario_decision_commit(
        packet: ScenarioPacket,
        decision: ScenarioDecision,
    ) -> None:
        is_hold = decision.action_id == "scenario.hold"
        if is_hold == decision.invalidates_collection:
            raise ProgressiveRouteError(
                "scenario invalidation semantics do not match the action"
            )
        if is_hold:
            return
        pages = packet.mechanism_state.get("training_pages")
        if not isinstance(pages, Mapping) or set(pages) != {
            "speed",
            "stamina",
            "power",
            "guts",
            "wisdom",
        }:
            raise ProgressiveRouteError(
                "scenario use requires five structured training pages"
            )

    def _commit_scenario_decision(
        self,
        decision: ScenarioDecision,
        expected_nodes: tuple[str, ...],
    ) -> None:
        self.session.commit_scenario_decision(decision)
        self.session.expected_next_node_ids = expected_nodes

    def handle_frame(
        self,
        frame: RouteFrameObservation,
        detector: RouteDetector,
        *,
        unknown_page_policy: str = "manual_on_unknown",
        resync: bool = False,
    ) -> RouteControllerDecision:
        if unknown_page_policy not in {"manual_on_unknown", "allowlisted_default"}:
            raise ValueError("unknown_page_policy is invalid")
        if not frame.stable:
            if not self.session.record_settle_frame(frame.source_sha256):
                return RouteControllerDecision(
                    "wait",
                    "frame_not_fresh",
                    None,
                    None,
                    self.settle_delay_seconds,
                    (),
                )
            if self.session.settle_count >= self.max_settle_frames:
                self.session.mark_mismatch("unstable_frame_limit")
                return RouteControllerDecision("stop", "unstable_frame_limit", None, None, None, ())
            return RouteControllerDecision("wait", "frame_unstable", None, None, self.settle_delay_seconds, ())
        self.session.clear_settle_frames()
        candidate_routes = self._candidate_routes(resync)
        candidate_detectors = self.graph.detector_ids_for(candidate_routes)
        matched = self._detect(
            frame,
            detector,
            candidate_routes,
            candidate_detectors,
            resync=resync,
        )
        if matched is None and self.session.expected_next_node_ids and not resync:
            candidate_routes = self.graph.neighbor_nodes(self.session.current_node_id)
            candidate_detectors = self.graph.detector_ids_for(candidate_routes)
            matched = self._detect(
                frame,
                detector,
                candidate_routes,
                candidate_detectors,
            )
        if matched is None:
            self.session.mark_mismatch("stable_screen_unknown")
            return RouteControllerDecision("stop", "stable_screen_unknown", None, None, None, candidate_detectors)
        if resync:
            self.session.resync(matched, frame.source_sha256)
        else:
            self.session.observe(matched, frame.source_sha256, frame.period_key, frame.calendar_ordinal)
        action_id = self.graph.default_action(matched)
        return RouteControllerDecision("action" if action_id else "track", "matched", matched, action_id, None, candidate_detectors)

    def _detect(
        self,
        frame: RouteFrameObservation,
        detector: RouteDetector,
        candidate_routes: tuple[str, ...],
        candidate_detectors: tuple[str, ...],
        *,
        resync: bool = False,
    ) -> str | None:
        match_expected = getattr(detector, "match_expected", None)
        if callable(match_expected):
            if resync:
                match_recovery = getattr(detector, "match_recovery", None)
                if not callable(match_recovery):
                    raise ProgressiveRouteError(
                        "scoped detector does not support recovery"
                    )
                matched = match_recovery(frame)
            else:
                matched = match_expected(frame, candidate_routes)
        elif callable(detector):
            matched = detector(frame, candidate_detectors)
        else:
            raise TypeError("detector must be callable or scoped")
        if matched is None:
            return None
        return self._resolve_match(matched, frame, candidate_routes)

    def _validate_scenario_packet_authority(
        self,
        adapter: ScenarioAdapter,
        packet: ScenarioPacket,
        *,
        recollection: bool = False,
    ) -> None:
        if not isinstance(packet, ScenarioPacket):
            raise ProgressiveRouteError("scenario adapter returned an invalid packet")
        if getattr(adapter, "adapter_id", None) != packet.adapter_id:
            raise ProgressiveRouteError("scenario packet adapter mismatch")
        if packet.run_session_id != self.session.run_session_id:
            raise ProgressiveRouteError("scenario packet run_session_id mismatch")
        if packet.transition_version != self.session.transition_version:
            raise ProgressiveRouteError("scenario packet transition version mismatch")
        if packet.current_node_id != self.session.current_node_id:
            raise ProgressiveRouteError("scenario packet current node mismatch")
        if recollection:
            if packet.source_frame_sha256 == self.session.last_observation_sha256:
                raise ProgressiveRouteError(
                    "recollected scenario packet requires a fresh source frame"
                )
        elif packet.source_frame_sha256 != self.session.last_observation_sha256:
            raise ProgressiveRouteError("scenario packet source frame mismatch")
        if packet.packet_sha256 in self.session.stale_scenario_packet_sha256:
            raise ProgressiveRouteError("scenario packet is stale")

        authorized_envelope: set[str] = set()
        for action_id in packet.allowed_action_ids:
            try:
                authorized_envelope.update(
                    self.graph.expected_nodes(packet.current_node_id, action_id)
                )
            except ProgressiveRouteConfigError as exc:
                raise ProgressiveRouteError(
                    "scenario packet contains an action outside graph authority"
                ) from exc
        if not set(packet.expected_next_node_ids).issubset(authorized_envelope):
            raise ProgressiveRouteError(
                "scenario packet expected nodes exceed graph authority"
            )

    def _validate_bound_scenario_packet_authority(
        self,
        adapter: ScenarioAdapter,
        packet: ScenarioPacket,
    ) -> None:
        if not isinstance(packet, ScenarioPacket):
            raise ProgressiveRouteError("scenario packet must be a ScenarioPacket")
        if getattr(adapter, "adapter_id", None) != packet.adapter_id:
            raise ProgressiveRouteError("scenario packet adapter mismatch")
        if packet.run_session_id != self.session.run_session_id:
            raise ProgressiveRouteError("scenario packet run_session_id mismatch")
        if packet.current_node_id != self.session.current_node_id:
            raise ProgressiveRouteError("scenario packet current node mismatch")
        if packet.packet_sha256 != self.session.scenario_packet_sha256:
            raise ProgressiveRouteError("scenario packet is not the current bound packet")
        if (
            packet.transition_version
            != self.session.scenario_packet_transition_version
        ):
            raise ProgressiveRouteError("scenario packet transition version mismatch")
        if packet.source_frame_sha256 != self.session.scenario_source_frame_sha256:
            raise ProgressiveRouteError("scenario packet source frame mismatch")
        if tuple(packet.allowed_action_ids) != self.session.scenario_allowed_action_ids:
            raise ProgressiveRouteError("scenario packet allowlist binding mismatch")
        if packet.packet_sha256 in self.session.stale_scenario_packet_sha256:
            raise ProgressiveRouteError("scenario packet is stale")

    def _action_specific_expected_nodes(
        self,
        adapter: ScenarioAdapter,
        packet: ScenarioPacket,
        decision: ScenarioDecision,
    ) -> tuple[str, ...]:
        expected = adapter.expected_nodes_after(decision)
        if not isinstance(expected, (tuple, list)) or any(
            type(node_id) is not str or not node_id for node_id in expected
        ):
            raise ProgressiveRouteError("scenario adapter expected nodes are invalid")
        expected_nodes = tuple(expected)
        if not expected_nodes or len(set(expected_nodes)) != len(expected_nodes):
            raise ProgressiveRouteError("scenario adapter expected nodes are invalid")
        if not set(expected_nodes).issubset(packet.expected_next_node_ids):
            raise ProgressiveRouteError(
                "scenario action expected nodes exceed packet authority"
            )
        try:
            graph_expected = self.graph.expected_nodes(
                packet.current_node_id,
                decision.action_id,
            )
        except ProgressiveRouteConfigError as exc:
            raise ProgressiveRouteError(
                "scenario action is outside graph authority"
            ) from exc
        if not set(expected_nodes).issubset(graph_expected):
            raise ProgressiveRouteError(
                "scenario action expected nodes exceed graph authority"
            )
        return expected_nodes

    def _candidate_routes(self, resync: bool) -> tuple[str, ...]:
        if resync:
            return self.graph.resync_nodes()
        if self.session.expected_next_node_ids:
            return self.session.expected_next_node_ids
        return self.graph.neighbor_nodes(self.session.current_node_id)

    def _resolve_match(self, matched: str, frame: RouteFrameObservation, candidates: tuple[str, ...]) -> str | None:
        if matched in candidates:
            return matched
        if matched == "inheritance_factor_result":
            if frame.period_key == "classic-03-late" and "inheritance.classic.result" in candidates:
                return "inheritance.classic.result"
            if frame.period_key == "senior-03-late" and "inheritance.senior.result" in candidates:
                return "inheritance.senior.result"
        return None
