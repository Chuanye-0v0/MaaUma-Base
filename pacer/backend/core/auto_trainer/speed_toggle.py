from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from core.frame_io import read_image_with_retry


SPEED_TOGGLE_CONFIRM_TOKEN = "UG_PARENT_SPEED_TOGGLE_SANDBOX"
SUPPORTED_SPEED_TOGGLE_SCREEN_SIZES = {(720, 1280), (1440, 2560)}


class SpeedTogglePlanner:
    """Optional speed control: uncertain arrows never authorize a toggle.

    The career owner continues ordinary observation when this returns absent;
    an animated arrow glyph is not a mandatory page identity check.
    """

    def plan(self, image_path: str | Path) -> dict[str, Any]:
        path = Path(image_path)
        img = read_image_with_retry(path)
        if img is None:
            return self._stop("image_read", image_path=str(path))

        evidence = {"image": str(path)}
        level = self._bottom_skip_level(img)
        state = self._bottom_skip_state(img)
        evidence["skip_state"] = state
        evidence["skip_level"] = level
        if level == "double":
            return self._stop("speed_already_enabled", evidence=evidence)
        if level not in {"off", "single"}:
            return self._stop("speed_control_absent", evidence=evidence)

        height, width = img.shape[:2]
        return {
            "action": "tap",
            "reason": "enable_speed_toggle",
            "tap": {
                "coordinate_name": "speed_toggle.skip",
                "x": int(width * 0.358),
                "y": int(height * 0.975),
                "scaled": False,
            },
            "evidence": evidence,
        }

    @staticmethod
    def _bottom_skip_state(img: np.ndarray) -> str:
        level = SpeedTogglePlanner._bottom_skip_level(img)
        if level == "double":
            return "on"
        if level == "single":
            return "off"
        return level

    @staticmethod
    def _bottom_skip_level(img: np.ndarray) -> str:
        height, width = img.shape[:2]
        if width > height:
            return "absent"
        normalized = cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(normalized, cv2.COLOR_BGR2HSV)
        region = hsv[1203:1274, 173:338]
        if region.size == 0:
            return "absent"
        green = np.mean(
            (region[:, :, 0] >= 38)
            & (region[:, :, 0] <= 92)
            & (region[:, :, 1] > 70)
            & (region[:, :, 2] > 80)
        )
        white = np.mean((region[:, :, 1] < 60) & (region[:, :, 2] > 175))
        brown = np.mean(
            (region[:, :, 0] >= 5)
            & (region[:, :, 0] <= 30)
            & (region[:, :, 1] > 50)
            & (region[:, :, 2] > 80)
            & (region[:, :, 2] <= 210)
        )
        # The broad ROI contains grass above the pill. Only the pill's own
        # fill and glyphs can distinguish off from a green two-arrow control.
        pill = hsv[1238:1259, 184:329]
        pill_green = np.mean(
            (pill[:, :, 0] >= 38)
            & (pill[:, :, 0] <= 92)
            & (pill[:, :, 1] > 70)
            & (pill[:, :, 2] > 80)
        )
        pill_white = np.mean((pill[:, :, 1] < 60) & (pill[:, :, 2] > 175))
        pill_brown = np.mean(
            (pill[:, :, 0] >= 5)
            & (pill[:, :, 0] <= 30)
            & (pill[:, :, 1] > 50)
            & (pill[:, :, 2] > 80)
            & (pill[:, :, 2] <= 210)
        )
        if pill_green < 0.05 and pill_white > 0.65 and pill_brown > 0.025:
            return "off"
        pill_active_green = np.mean(
            (pill[:, :, 0] >= 38)
            & (pill[:, :, 0] <= 92)
            & (pill[:, :, 1] > 110)
            & (pill[:, :, 2] > 160)
        )
        if green > 0.25 and pill_green > 0.25:
            if pill_active_green <= 0.15:
                return "absent"
            first_arrow_white = SpeedTogglePlanner._arrow_slot_is_white_arrow(
                hsv,
                (260, 1234, 288, 1263),
                expected_edge="right",
            )
            second_arrow_white = SpeedTogglePlanner._arrow_slot_is_white_arrow(
                hsv,
                (298, 1234, 324, 1263),
                expected_edge="left",
            )
            if not first_arrow_white:
                return "absent"
            return "double" if second_arrow_white else "single"
        if white > 0.65 and brown > 0.025:
            return "off"
        if green < 0.05 and white > 0.40 and brown > 0.05:
            return "off"
        if white > 0.35 and brown > 0.12:
            return "off"
        return "absent"

    @staticmethod
    def _arrow_slot_is_white_arrow(
        hsv: np.ndarray,
        box: tuple[int, int, int, int],
        *,
        expected_edge: str,
    ) -> bool:
        """Distinguish a filled arrow triangle from title-page white text."""

        x1, y1, x2, y2 = box
        slot = hsv[y1:y2, x1:x2]
        if slot.size == 0 or expected_edge not in {"left", "right"}:
            return False
        white_mask = (
            (slot[:, :, 1] < 65) & (slot[:, :, 2] > 200)
        ).astype(np.uint8)
        white_ratio = float(np.mean(white_mask))
        if white_ratio >= 0.65:
            return True
        count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
            white_mask,
            connectivity=8,
        )
        for x, _y, width, height, area in stats[1:count]:
            edge_aligned = x <= 3 if expected_edge == "left" else x >= 15
            if (
                edge_aligned
                and 8 <= width <= 15
                and 15 <= height <= 23
                and 80 <= area <= 180
            ):
                return True
        return False

    @staticmethod
    def _arrow_slot_white_ratio(
        hsv: np.ndarray,
        box: tuple[int, int, int, int],
    ) -> float:
        x1, y1, x2, y2 = box
        slot = hsv[y1:y2, x1:x2]
        if slot.size == 0:
            return 0.0
        return float(np.mean((slot[:, :, 1] < 65) & (slot[:, :, 2] > 170)))

    @staticmethod
    def _stop(
        reason: str,
        *,
        evidence: dict[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        data = {"action": "stop", "reason": reason, "evidence": evidence or {}}
        data.update(extra)
        return data


class SpeedToggleExecutor:
    def __init__(
        self,
        *,
        adb_factory: Callable[[], Any] | None = None,
        planner: SpeedTogglePlanner | None = None,
    ) -> None:
        self.adb_factory = adb_factory or self._default_adb_factory
        self.planner = planner or SpeedTogglePlanner()

    def run_once(
        self,
        *,
        mode: str,
        execute: bool,
        confirm_token: str | None,
        screenshot_path: str | Path | None = None,
    ) -> dict[str, Any]:
        screenshot_path = Path(screenshot_path) if screenshot_path is not None else (
            Path(tempfile.gettempdir()) / "uma_speed_toggle" / "screen.png"
        )

        adb = self.adb_factory()
        if mode == "live":
            if not adb.connect():
                return {"performed": False, "reason": "adb_connect_failed", "decision": {}}
            size = tuple(adb.get_screen_size())
            if size not in SUPPORTED_SPEED_TOGGLE_SCREEN_SIZES:
                return {"performed": False, "reason": f"unsupported_screen_size:{size}", "decision": {}}
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            adb.screenshot(str(screenshot_path))
        elif mode != "dry-run":
            return {"performed": False, "reason": "mode", "decision": {}}

        decision = self.planner.plan(screenshot_path)
        if decision.get("action") != "tap":
            return {
                "performed": False,
                "reason": str(decision.get("reason", "stop")),
                "decision": decision,
            }
        if mode == "dry-run":
            return {"performed": False, "reason": "dry_run", "decision": decision}
        if not execute:
            return {"performed": False, "reason": "execute", "decision": decision}
        if confirm_token != SPEED_TOGGLE_CONFIRM_TOKEN:
            return {"performed": False, "reason": "confirm_token", "decision": decision}

        tap = decision["tap"]
        adb.tap(tap["x"], tap["y"], scaled=bool(tap.get("scaled", False)))
        return {"performed": True, "reason": str(decision["reason"]), "decision": decision}

    @staticmethod
    def _default_adb_factory() -> Any:
        from core.adb import ADB

        return ADB()
