from __future__ import annotations

from typing import Any, Protocol

from core.auto_trainer.progressive_route_graph import ProgressiveRouteGraph
from core.auto_trainer.progressive_route_session import ProgressiveRouteError
from core.auto_trainer.screen_graph import ScreenGraph


_INHERITANCE_DETECTOR_ID = "inheritance_factor_result"


class StructuredRouteFrame(Protocol):
    perception: dict[str, Any]


class ScopedRouteRecognizer:
    """Match only detectors authorized by the current route envelope."""

    def __init__(
        self,
        graph: ProgressiveRouteGraph,
        screen_graph: ScreenGraph,
    ) -> None:
        self.graph = graph
        self.screen_graph = screen_graph

    def match_expected(
        self,
        frame: StructuredRouteFrame,
        candidate_route_ids: tuple[str, ...],
    ) -> str | None:
        return self._match(frame, candidate_route_ids)

    def match_recovery(self, frame: StructuredRouteFrame) -> str | None:
        return self._match(frame, self.graph.resync_nodes())

    def _match(
        self,
        frame: StructuredRouteFrame,
        candidate_route_ids: tuple[str, ...],
    ) -> str | None:
        candidate_detector_ids = self.graph.detector_ids_for(candidate_route_ids)
        matched = self.screen_graph.match(
            dict(frame.perception),
            candidate_node_ids=candidate_detector_ids,
        )
        if matched is None:
            return None
        detector_id = matched.get("node_id")
        if type(detector_id) is not str or detector_id not in candidate_detector_ids:
            return None
        mapped_routes = tuple(
            route_id
            for route_id in candidate_route_ids
            if detector_id in self.graph.node(route_id).detector_node_ids
        )
        if detector_id == _INHERITANCE_DETECTOR_ID and mapped_routes:
            return detector_id
        if len(mapped_routes) > 1:
            raise ProgressiveRouteError("ambiguous_scoped_detector")
        return mapped_routes[0] if mapped_routes else None
