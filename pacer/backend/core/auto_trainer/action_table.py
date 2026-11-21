from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from core.coordinates import CoordinateScaler


DEFAULT_ACTION_TABLE_PATH = Path("config/auto_trainer_actions.json")


class ActionTable:
    """Load configured screen-state actions and render planner decisions."""

    def __init__(self, path: str | Path = DEFAULT_ACTION_TABLE_PATH):
        self.path = Path(path)
        self.data = self._load()

    def decision(
        self,
        section: str,
        state: str,
        img: np.ndarray,
        *,
        evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        entry = self.data.get(section, {}).get(state)
        if not isinstance(entry, dict):
            return None

        action = str(entry.get("action", ""))
        reason = str(entry.get("reason", state))
        decision: dict[str, Any] = {
            "action": action,
            "reason": reason,
            "state": state,
            "evidence": evidence,
        }
        route_state = entry.get("route_state")
        if route_state is not None:
            decision["route_state"] = str(route_state)

        if action == "tap":
            point = entry.get("point")
            if not isinstance(point, dict):
                return None
            x = int(point["x"])
            y = int(point["y"])
            height, width = img.shape[:2]
            tap_x, tap_y = CoordinateScaler(
                reference_size=self.reference_size,
                current_size=(width, height),
            ).scale_point(x, y)
            decision["tap"] = {
                "coordinate_name": str(entry.get("coordinate_name", state)),
                "x": tap_x,
                "y": tap_y,
                "scaled": False,
            }
        return decision

    @property
    def reference_size(self) -> tuple[int, int]:
        size = self.data.get("reference_size", {})
        return int(size.get("width", 720)), int(size.get("height", 1280))

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as f:
            return json.load(f)
