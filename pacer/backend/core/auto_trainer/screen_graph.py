from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from core.coordinates import CoordinateScaler
from core.auto_trainer.speed_toggle import SpeedTogglePlanner
from core.perception import Perception
from core.screen_text import analyze_screen_text


DEFAULT_SCREEN_GRAPH_PATH = Path("config/auto_trainer_screen_graph.json")


class ScreenGraphConfigError(ValueError):
    """Raised when a screen graph config cannot be loaded safely."""


class FrameFeatureContext:
    """Lazily computed image features scoped to one ScreenGraph.match call."""

    def __init__(self, img: np.ndarray | None) -> None:
        self.img = img
        self._hsv: np.ndarray | None = None

    def hsv(self) -> np.ndarray | None:
        if self.img is None or self.img.size == 0:
            return None
        if self._hsv is None:
            self._hsv = cv2.cvtColor(self.img, cv2.COLOR_BGR2HSV)
        return self._hsv


class ScreenGraph:
    """Small resource-defined screen recognizer inspired by MAA pipelines."""

    def __init__(
        self,
        nodes: list[dict[str, Any]],
        *,
        reference_size: tuple[int, int] = (720, 1280),
    ) -> None:
        self.nodes = sorted(
            nodes,
            key=lambda item: (-int(item.get("priority", 0)), int(item.get("_index", 0))),
        )
        self.reference_size = reference_size

    @classmethod
    def load(cls, path: str | Path = DEFAULT_SCREEN_GRAPH_PATH) -> "ScreenGraph":
        graph_path = Path(path)
        if not graph_path.exists():
            return cls([])
        with graph_path.open("r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f), source=str(graph_path))

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source: str = "<dict>") -> "ScreenGraph":
        if not isinstance(data, dict):
            raise ScreenGraphConfigError(f"{source}: root must be an object")
        raw_nodes = data.get("nodes")
        if not isinstance(raw_nodes, list):
            raise ScreenGraphConfigError(f"{source}: nodes must be a list")
        reference = data.get("reference_size", {})
        reference_size = (
            int(reference.get("width", 720)) if isinstance(reference, dict) else 720,
            int(reference.get("height", 1280)) if isinstance(reference, dict) else 1280,
        )
        nodes: list[dict[str, Any]] = []
        for index, node in enumerate(raw_nodes):
            if not isinstance(node, dict):
                raise ScreenGraphConfigError(f"{source}: nodes[{index}] must be an object")
            for field in ("id", "when", "action"):
                if field not in node:
                    raise ScreenGraphConfigError(f"{source}: nodes[{index}].{field} is required")
            if not isinstance(node["when"], dict):
                raise ScreenGraphConfigError(f"{source}: nodes[{index}].when must be an object")
            if not isinstance(node["action"], dict):
                raise ScreenGraphConfigError(f"{source}: nodes[{index}].action must be an object")
            action_type = node["action"].get("type")
            if action_type not in {"stop", "tap", "wait", "delegate"}:
                raise ScreenGraphConfigError(
                    f"{source}: nodes[{index}].action.type must be stop, tap, wait, or delegate"
                )
            normalized = dict(node)
            normalized["_index"] = index
            nodes.append(normalized)
        return cls(nodes, reference_size=reference_size)

    def match_file(
        self,
        image_path: str | Path,
        *,
        state: dict[str, Any] | None = None,
        candidate_node_ids: Any = None,
    ) -> dict[str, Any] | None:
        path = Path(image_path)
        img = cv2.imread(str(path))
        if img is None:
            raise ValueError(f"Cannot read image: {path}")
        perception = dict(state or Perception().analyze_image(img))
        perception.setdefault("image", str(path))
        landscape_kind = classify_landscape_race_screen(img)
        if landscape_kind is not None:
            perception.setdefault("landscape_race_kind", landscape_kind)
        return self.match(
            perception,
            img,
            image_path=str(path),
            candidate_node_ids=candidate_node_ids,
        )

    def match(
        self,
        state: dict[str, Any],
        img: np.ndarray | None = None,
        *,
        image_path: str | None = None,
        candidate_node_ids: Any = None,
    ) -> dict[str, Any] | None:
        features = FrameFeatureContext(img)
        candidates = _normalize_candidate_node_ids(candidate_node_ids)
        for node in self.nodes:
            if candidates is not None and node["id"] not in candidates:
                continue
            if node.get("enabled", True) is False:
                continue
            evidence: dict[str, Any] = {"matched": [], "failed": []}
            if not self._condition_matches(
                node["when"],
                state,
                img,
                evidence,
                features=features,
            ):
                continue
            decision = self._decision_for(node, state, img, evidence)
            graph_evidence = {
                "node_id": str(node["id"]),
                "priority": int(node.get("priority", 0)),
                "matched": evidence["matched"],
                "failed": evidence["failed"],
            }
            if image_path is not None:
                graph_evidence["image"] = image_path
            decision.setdefault("state", str(node["id"]))
            decision.setdefault("evidence", {})
            decision["evidence"]["screen_graph"] = graph_evidence
            return {
                "node_id": str(node["id"]),
                "priority": int(node.get("priority", 0)),
                "decision": decision,
                "evidence": graph_evidence,
            }
        return None

    def _condition_matches(
        self,
        condition: dict[str, Any],
        state: dict[str, Any],
        img: np.ndarray | None,
        evidence: dict[str, list[Any]],
        *,
        features: FrameFeatureContext | None = None,
    ) -> bool:
        features = features or FrameFeatureContext(img)
        if "all" in condition:
            items = condition["all"]
            if not isinstance(items, list):
                raise ScreenGraphConfigError("condition.all must be a list")
            ok = all(
                self._condition_matches(item, state, img, evidence, features=features)
                for item in items
            )
            evidence["matched" if ok else "failed"].append("all")
            return ok
        if "any" in condition:
            items = condition["any"]
            if not isinstance(items, list):
                raise ScreenGraphConfigError("condition.any must be a list")
            ok = any(
                self._condition_matches(item, state, img, evidence, features=features)
                for item in items
            )
            evidence["matched" if ok else "failed"].append("any")
            return ok
        if "not" in condition:
            ok = not self._condition_matches(
                condition["not"],
                state,
                img,
                {"matched": [], "failed": []},
                features=features,
            )
            evidence["matched" if ok else "failed"].append("not")
            return ok

        checks: list[bool] = []
        if "screen" in condition:
            expected = condition["screen"]
            screens = {str(item) for item in expected} if isinstance(expected, list) else {str(expected)}
            ok = str(state.get("screen", "unknown")) in screens
            evidence["matched" if ok else "failed"].append("screen")
            checks.append(ok)
        if "field" in condition:
            actual = _field_value(state, str(condition["field"]))
            expected = condition.get("equals")
            ok = actual == expected
            evidence["matched" if ok else "failed"].append(str(condition["field"]))
            checks.append(ok)
        if "text_terms" in condition:
            terms = condition["text_terms"]
            if isinstance(terms, str):
                terms = [terms]
            text = _flatten_text(state)
            upper = text.upper()
            resource_terms: set[str] | None = None
            matched: list[str] = []
            for term in terms:
                value = str(term)
                needle = value.upper()
                if needle in {"TP", "RP"}:
                    if resource_terms is None:
                        resource_terms = {
                            str(item).upper()
                            for item in analyze_screen_text(text).get("resource_terms", [])
                        }
                    if needle in resource_terms:
                        matched.append(value)
                    continue
                if needle in upper:
                    matched.append(value)
            ok = bool(matched)
            evidence["matched" if ok else "failed"].append({"text_terms": matched or list(terms)})
            checks.append(ok)
        if "bottom_skip_state" in condition:
            expected = condition["bottom_skip_state"]
            states = {str(item) for item in expected} if isinstance(expected, list) else {str(expected)}
            actual = SpeedTogglePlanner._bottom_skip_state(img) if img is not None else "absent"
            ok = actual in states
            evidence["matched" if ok else "failed"].append(
                {"bottom_skip_state": actual, "expected": sorted(states)}
            )
            checks.append(ok)
        if "hsv_ratio" in condition:
            ok = self._hsv_ratio_matches(
                condition["hsv_ratio"],
                img,
                evidence,
                features=features,
            )
            checks.append(ok)
        return bool(checks) and all(checks)

    def _hsv_ratio_matches(
        self,
        spec: dict[str, Any],
        img: np.ndarray | None,
        evidence: dict[str, list[Any]],
        *,
        features: FrameFeatureContext | None = None,
    ) -> bool:
        if img is None:
            evidence["failed"].append("hsv_ratio")
            return False
        roi = spec.get("roi", {})
        if not isinstance(roi, dict):
            raise ScreenGraphConfigError("hsv_ratio.roi must be an object")
        height, width = img.shape[:2]
        x0, x1 = _scaled_range(roi.get("x", [0.0, 1.0]), width)
        y0, y1 = _scaled_range(roi.get("y", [0.0, 1.0]), height)
        if img[y0:y1, x0:x1].size == 0:
            evidence["failed"].append("hsv_ratio")
            return False
        hsv_frame = (features or FrameFeatureContext(img)).hsv()
        if hsv_frame is None:
            evidence["failed"].append("hsv_ratio")
            return False
        hsv = hsv_frame[y0:y1, x0:x1]
        mask_spec = spec.get("mask", {})
        if not isinstance(mask_spec, dict):
            raise ScreenGraphConfigError("hsv_ratio.mask must be an object")
        mask = np.ones(hsv.shape[:2], dtype=bool)
        if "h_min" in mask_spec:
            mask &= hsv[:, :, 0] >= int(mask_spec["h_min"])
        if "h_max" in mask_spec:
            mask &= hsv[:, :, 0] <= int(mask_spec["h_max"])
        if "s_min" in mask_spec:
            mask &= hsv[:, :, 1] >= int(mask_spec["s_min"])
        if "s_max" in mask_spec:
            mask &= hsv[:, :, 1] <= int(mask_spec["s_max"])
        if "v_min" in mask_spec:
            mask &= hsv[:, :, 2] >= int(mask_spec["v_min"])
        if "v_max" in mask_spec:
            mask &= hsv[:, :, 2] <= int(mask_spec["v_max"])
        ratio = float(np.mean(mask))
        ok = True
        if "gt" in spec:
            ok = ok and ratio > float(spec["gt"])
        if "gte" in spec:
            ok = ok and ratio >= float(spec["gte"])
        if "lt" in spec:
            ok = ok and ratio < float(spec["lt"])
        if "lte" in spec:
            ok = ok and ratio <= float(spec["lte"])
        payload = {"hsv_ratio": round(ratio, 4), "roi": roi}
        bucket = evidence["matched" if ok else "failed"]
        bucket.append("hsv_ratio")
        bucket.append(payload)
        return bool(ok)

    def _decision_for(
        self,
        node: dict[str, Any],
        state: dict[str, Any],
        img: np.ndarray | None,
        evidence: dict[str, list[Any]],
    ) -> dict[str, Any]:
        action = node["action"]
        action_type = str(action["type"])
        reason = str(action.get("reason", node["id"]))
        decision: dict[str, Any] = {
            "action": action_type,
            "reason": reason,
            "state": str(node["id"]),
            "evidence": {},
        }
        route_state = action.get("route_state")
        if route_state is not None:
            decision["route_state"] = str(route_state)
        if action_type == "tap":
            if img is None:
                raise ScreenGraphConfigError(f"{node['id']}: tap action requires an image")
            decision["tap"] = self._tap_for(action, img, str(node["id"]))
        return decision

    def _tap_for(self, action: dict[str, Any], img: np.ndarray, node_id: str) -> dict[str, Any]:
        height, width = img.shape[:2]
        if "point" in action:
            point = action["point"]
            if not isinstance(point, dict):
                raise ScreenGraphConfigError(f"{node_id}: action.point must be an object")
            x, y = CoordinateScaler(
                reference_size=self.reference_size,
                current_size=(width, height),
            ).scale_point(int(point["x"]), int(point["y"]))
        elif "roi_center" in action:
            roi = action["roi_center"]
            if not isinstance(roi, dict):
                raise ScreenGraphConfigError(f"{node_id}: action.roi_center must be an object")
            x0, x1 = _scaled_range(roi.get("x", [0.0, 1.0]), width)
            y0, y1 = _scaled_range(roi.get("y", [0.0, 1.0]), height)
            x, y = (x0 + x1) // 2, (y0 + y1) // 2
        else:
            raise ScreenGraphConfigError(f"{node_id}: tap action needs point or roi_center")
        return {
            "coordinate_name": str(action.get("coordinate_name", node_id)),
            "x": int(x),
            "y": int(y),
            "scaled": False,
        }


def _scaled_range(value: Any, total: int) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ScreenGraphConfigError("roi ranges must be two-item lists")
    start, end = float(value[0]), float(value[1])
    if 0.0 <= start <= 1.0 and 0.0 <= end <= 1.0:
        return int(total * start), int(total * end)
    return int(start), int(end)


def _normalize_candidate_node_ids(value: Any) -> frozenset[str] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
        raise ScreenGraphConfigError("candidate_node_ids must be a collection of node ids")
    normalized = frozenset(value)
    if any(type(item) is not str or not item for item in normalized):
        raise ScreenGraphConfigError("candidate_node_ids contains an invalid node id")
    return normalized


def _field_value(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            if key in {"image", "buttons"}:
                continue
            parts.append(_flatten_text(item))
        return "\n".join(part for part in parts if part)
    if isinstance(value, (list, tuple, set)):
        return "\n".join(_flatten_text(item) for item in value)
    return str(value)


def classify_landscape_race_screen(img: np.ndarray) -> str | None:
    """Classify narrow landscape race/loading states used by guarded routes."""

    height, width = img.shape[:2]
    if width <= height:
        return None
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    def ratio(
        x0: float,
        x1: float,
        y0: float,
        y1: float,
        mask: np.ndarray,
    ) -> float:
        xs, xe = int(width * x0), int(width * x1)
        ys, ye = int(height * y0), int(height * y1)
        crop = mask[ys:ye, xs:xe]
        if crop.size == 0:
            return 0.0
        return float(np.mean(crop))

    white = (hsv[:, :, 1] < 50) & (hsv[:, :, 2] > 190)
    green = (
        (hsv[:, :, 0] >= 38)
        & (hsv[:, :, 0] <= 92)
        & (hsv[:, :, 1] > 70)
        & (hsv[:, :, 2] > 80)
    )
    orange = (
        (hsv[:, :, 0] >= 5)
        & (hsv[:, :, 0] <= 30)
        & (hsv[:, :, 1] > 50)
        & (hsv[:, :, 2] > 80)
    )

    full_white = float(np.mean(white))
    center_white = ratio(0.20, 0.80, 0.05, 0.98, white)
    center_green = ratio(0.20, 0.80, 0.05, 0.98, green)
    title_mid_white = ratio(0.25, 0.75, 0.25, 0.72, white)
    title_mid_orange = ratio(0.25, 0.75, 0.25, 0.72, orange)
    bottom_green = ratio(0.25, 0.75, 0.82, 0.98, green)
    card_white = ratio(0.00, 1.00, 0.08, 0.78, white)
    top_white = ratio(0.00, 1.00, 0.00, 0.15, white)
    header_green = ratio(0.22, 0.78, 0.02, 0.12, green)
    scene_green = float(np.mean(green))

    lineup_column_profile = np.mean(
        white[int(height * 0.10) : int(height * 0.62), :],
        axis=0,
    )
    lineup_white_columns = lineup_column_profile > 0.55
    lineup_card_runs = 0
    run_start: int | None = None
    for index, is_white_column in enumerate(lineup_white_columns):
        if is_white_column and run_start is None:
            run_start = index
        if not is_white_column and run_start is not None:
            run_width = index - run_start
            if width * 0.05 <= run_width <= width * 0.15:
                lineup_card_runs += 1
            run_start = None
    if run_start is not None:
        run_width = width - run_start
        if width * 0.05 <= run_width <= width * 0.15:
            lineup_card_runs += 1

    if center_white > 0.75 and header_green > 0.55:
        return "landscape_race_menu"
    if full_white > 0.90 and center_green > 0.02:
        return "landscape_loading"
    if lineup_card_runs >= 6 and bottom_green > 0.30:
        return "landscape_race_pre_start"
    if bottom_green > 0.45 and card_white > 0.45 and title_mid_white > 0.35:
        return "landscape_race_list"
    if top_white > 0.35 and title_mid_orange > 0.10:
        return "landscape_race_title"
    if scene_green > 0.25 and center_white < 0.25:
        return "landscape_race_running"
    return None
