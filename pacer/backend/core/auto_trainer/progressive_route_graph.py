from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.auto_trainer.screen_graph import DEFAULT_SCREEN_GRAPH_PATH


DEFAULT_PROGRESSIVE_ROUTE_PATH = Path("config/progressive_route_grand_masters.json")
_PHASES = frozenset({"bootstrap", "turn_loop", "classic_inheritance", "senior_inheritance", "skill_handoff", "career_end", "complete", "mismatch"})
_TURN_EFFECTS = frozenset({"none", "primary_pending", "primary_complete", "terminal"})


class ProgressiveRouteConfigError(ValueError):
    """Raised when a progressive route config cannot be validated."""


@dataclass(frozen=True)
class ActionTransitionSpec:
    action_id: str
    expected_next_node_ids: tuple[str, ...]


@dataclass(frozen=True)
class RouteNodeSpec:
    node_id: str
    phase: str
    detector_node_ids: tuple[str, ...]
    actions: tuple[ActionTransitionSpec, ...]
    neighbor_node_ids: tuple[str, ...]
    transient: bool
    resync_anchor: bool
    turn_effect: str
    default_action_id: str | None


@dataclass(frozen=True)
class ProgressiveRouteSettings:
    condition_audit_interval: int
    scenario_adapter: str
    goddess_auto_policy_enabled: bool


class ProgressiveRouteGraph:
    def __init__(
        self,
        nodes: tuple[RouteNodeSpec, ...],
        settings: ProgressiveRouteSettings | None = None,
    ) -> None:
        self._nodes = {node.node_id: node for node in nodes}
        self.settings = settings

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PROGRESSIVE_ROUTE_PATH) -> "ProgressiveRouteGraph":
        config_path = Path(path)
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProgressiveRouteConfigError(f"{config_path}: invalid JSON") from exc
        detector_ids = _screen_graph_detector_ids()
        raw_nodes = data.get("nodes") if isinstance(data, dict) else None
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise ProgressiveRouteConfigError("nodes must be a non-empty list")
        settings = _parse_settings(data.get("settings")) if "settings" in data else None
        nodes = tuple(_parse_node(item, index, detector_ids) for index, item in enumerate(raw_nodes))
        node_ids = tuple(node.node_id for node in nodes)
        if len(set(node_ids)) != len(node_ids):
            raise ProgressiveRouteConfigError("duplicate route node id")
        known = set(node_ids)
        for node in nodes:
            for target in (*node.neighbor_node_ids, *(target for action in node.actions for target in action.expected_next_node_ids)):
                if target not in known:
                    raise ProgressiveRouteConfigError(f"{node.node_id}: unknown target {target}")
        return cls(nodes, settings)

    def node(self, node_id: str) -> RouteNodeSpec:
        try:
            return self._nodes[node_id]
        except KeyError as exc:
            raise ProgressiveRouteConfigError(f"unknown route node {node_id}") from exc

    def expected_nodes(self, node_id: str, action_id: str) -> tuple[str, ...]:
        for action in self.node(node_id).actions:
            if action.action_id == action_id:
                return action.expected_next_node_ids
        raise ProgressiveRouteConfigError(f"{node_id}: action {action_id} is not allowlisted")

    def neighbor_nodes(self, node_id: str) -> tuple[str, ...]:
        return self.node(node_id).neighbor_node_ids

    def default_action(self, node_id: str) -> str | None:
        return self.node(node_id).default_action_id

    def detector_ids_for(self, node_ids: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(detector for node_id in node_ids for detector in self.node(node_id).detector_node_ids))

    def resync_nodes(self) -> tuple[str, ...]:
        return tuple(node.node_id for node in self._nodes.values() if node.resync_anchor)


def _screen_graph_detector_ids() -> frozenset[str]:
    try:
        data = json.loads(DEFAULT_SCREEN_GRAPH_PATH.read_text(encoding="utf-8"))
        return frozenset(str(node["id"]) for node in data["nodes"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ProgressiveRouteConfigError("screen graph detector ids are unavailable") from exc


def _parse_settings(value: Any) -> ProgressiveRouteSettings:
    required = {
        "condition_audit_interval",
        "scenario_adapter",
        "goddess_auto_policy_enabled",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ProgressiveRouteConfigError("settings must contain only the safe route settings")
    interval = value["condition_audit_interval"]
    if type(interval) is not int or interval < 0:
        raise ProgressiveRouteConfigError("condition_audit_interval must be a non-negative integer")
    adapter = value["scenario_adapter"]
    if adapter != "grand_masters":
        raise ProgressiveRouteConfigError("scenario_adapter must be grand_masters")
    enabled = value["goddess_auto_policy_enabled"]
    if type(enabled) is not bool:
        raise ProgressiveRouteConfigError("goddess_auto_policy_enabled must be bool")
    return ProgressiveRouteSettings(interval, adapter, enabled)


def _parse_node(value: Any, index: int, detector_ids: frozenset[str]) -> RouteNodeSpec:
    if not isinstance(value, dict):
        raise ProgressiveRouteConfigError(f"nodes[{index}] must be an object")
    required = ("node_id", "phase", "detector_node_ids", "actions", "neighbor_node_ids", "transient", "resync_anchor", "turn_effect", "default_action_id")
    if any(field not in value for field in required):
        raise ProgressiveRouteConfigError(f"nodes[{index}] missing required field")
    node_id = _string(value["node_id"], f"nodes[{index}].node_id")
    phase = _string(value["phase"], f"{node_id}.phase")
    if phase not in _PHASES:
        raise ProgressiveRouteConfigError(f"{node_id}: invalid phase")
    detector_node_ids = _strings(value["detector_node_ids"], f"{node_id}.detector_node_ids")
    if any(detector not in detector_ids for detector in detector_node_ids):
        raise ProgressiveRouteConfigError(f"{node_id}: unknown screen graph detector")
    neighbor_node_ids = _strings(value["neighbor_node_ids"], f"{node_id}.neighbor_node_ids")
    if type(value["transient"]) is not bool or type(value["resync_anchor"]) is not bool:
        raise ProgressiveRouteConfigError(f"{node_id}: transient and resync_anchor must be bool")
    turn_effect = _string(value["turn_effect"], f"{node_id}.turn_effect")
    if turn_effect not in _TURN_EFFECTS:
        raise ProgressiveRouteConfigError(f"{node_id}: invalid turn_effect")
    raw_actions = value["actions"]
    if not isinstance(raw_actions, list):
        raise ProgressiveRouteConfigError(f"{node_id}.actions must be a list")
    actions = tuple(_parse_action(item, node_id) for item in raw_actions)
    if len({action.action_id for action in actions}) != len(actions):
        raise ProgressiveRouteConfigError(f"{node_id}: duplicate action id")
    default_action_id = value["default_action_id"]
    if default_action_id is not None:
        default_action_id = _string(default_action_id, f"{node_id}.default_action_id")
        if default_action_id not in {action.action_id for action in actions}:
            raise ProgressiveRouteConfigError(f"{node_id}: default action is not allowlisted")
    return RouteNodeSpec(node_id, phase, detector_node_ids, actions, neighbor_node_ids, value["transient"], value["resync_anchor"], turn_effect, default_action_id)


def _parse_action(value: Any, node_id: str) -> ActionTransitionSpec:
    if not isinstance(value, dict) or set(value) != {"action_id", "expected_next_node_ids"}:
        raise ProgressiveRouteConfigError(f"{node_id}: invalid action transition")
    return ActionTransitionSpec(_string(value["action_id"], f"{node_id}.action_id"), _strings(value["expected_next_node_ids"], f"{node_id}.expected_next_node_ids"))


def _string(value: Any, label: str) -> str:
    if type(value) is not str or not value:
        raise ProgressiveRouteConfigError(f"{label} must be a non-empty string")
    return value


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str or not item for item in value):
        raise ProgressiveRouteConfigError(f"{label} must be a list of non-empty strings")
    return tuple(value)
