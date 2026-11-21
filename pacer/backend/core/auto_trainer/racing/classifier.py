"""Visual and layout classifiers for RaceRoute screens, cards, and overlays."""
from __future__ import annotations

import hashlib
import re
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from core.frame_io import read_image_with_retry
from core.auto_trainer.action_table import ActionTable
from core.auto_trainer.career_goal_perception import is_goal_achievement_title
from core.auto_trainer.larc_pages import (
    LARC_SYSTEM_GOAL_IDS,
    annotate_larc_race_page,
    larc_race_transition_kind,
    plan_larc_explanation,
)
from core.auto_trainer.frame_safety import FormalActionSafetyGate
from core.auto_trainer.runtime_control import reobserving_step, controlled_sleep
from core.auto_trainer.race_clock_retry import _ordinary_result_blocked, retry_prompt_visible
from core.auto_trainer.route_handoff import handoff, is_route_handoff
from core.auto_trainer.transition_wait import blue_white_transition_visible, orange_wipe_visible
from ._compat import (
    FrameSafetyAnalyzer,
    LocalRapidOCRScreenTextReader,
    Perception,
    RaceRouteExecutor,
    RaceRoutePlanner,
    SafeAdvancePlanner,
    TrainingMenuPrechecker,
    _current_clock_retry,
    analyze_screen_text,
    classify_landscape_race_screen,
    fuse_perception_with_text,
    match_main_menu_named_goal,
    rr_attr,
)
from .models import (
    RACE_ENTRY_POST_OBSERVATION_RETRIES,
    RACE_LIST_GRADE_REOBSERVATION_RETRIES,
    RACE_READ_ONLY_REOBSERVATION_DELAY_SECONDS,
    RACE_ROUTE_CONFIRM_TOKEN,
    RACE_SCENE_WAIT_REASONS,
    RESULT_TRANSITION_RECAPTURE_DELAY_SECONDS,
    RESULT_TRANSITION_RECAPTURE_LIMIT,
    SUPPORTED_RACE_ROUTE_SCREEN_SIZES,
    RaceRoutePolicy,
    RaceRouteSession,
    _RACE_ACCOUNT_CONFLICT_TEXT,
    is_race_scene_wait_result,
    larc_exchange_entry_released,
)


class RaceClassifierMixin:
    """Stateless visual/layout classifiers for RaceRoutePlanner."""

    @staticmethod
    def _is_landscape(perception: dict[str, Any]) -> bool:
        return str(perception.get("orientation")) == "landscape"

    @staticmethod
    def _bottom_center_green_button(
        perception: dict[str, Any],
        img: np.ndarray,
        *,
        min_y_ratio: float,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        candidates: list[tuple[float, int, int]] = []
        for button in perception.get("buttons", []):
            cx, cy = button.get("center", [0, 0])
            x, _y, w, h = button.get("bbox", [0, 0, 0, 0])
            if cy / max(height, 1) < min_y_ratio:
                continue
            if not (width * 0.22 <= cx <= width * 0.78):
                continue
            if w / max(width, 1) < 0.18:
                continue
            if w / max(width, 1) > 0.82 and h / max(height, 1) > 0.08:
                continue
            center_bias = abs(cx - width / 2) / max(width, 1)
            score = float(button.get("area", 0)) - center_bias * width * 40
            candidates.append((score, int(cx), int(cy)))
        if not candidates:
            return None
        _, x, y = max(candidates, key=lambda item: item[0])
        return x, y

    @staticmethod
    def _race_result_continue_button(
        perception: dict[str, Any],
        img: np.ndarray,
        screen_text: dict[str, Any] | None,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        text = ""
        if isinstance(screen_text, dict):
            text = "\n".join(
                str(screen_text.get(key, ""))
                for key in ("visible_text", "normalized_text")
                if screen_text.get(key)
            )
        has_result_continue_text = any(
            term in text
            for term in (
                "下一步",
                "继续",
                "结果",
                "缁х画",
                "缁撴灉",
                "绲愭灉",
            )
        )
        min_y_ratio = 0.80 if has_result_continue_text else 0.86
        candidates: list[tuple[float, int, int]] = []
        for button in perception.get("buttons", []):
            if str(button.get("kind", "")) != "primary_green":
                continue
            cx, cy = button.get("center", [0, 0])
            _x, _y, w, h = button.get("bbox", [0, 0, 0, 0])
            if cy / max(height, 1) < min_y_ratio:
                continue
            if not (width * 0.22 <= cx <= width * 0.88):
                continue
            width_ratio = w / max(width, 1)
            height_ratio = h / max(height, 1)
            if not (0.18 <= width_ratio <= 0.65):
                continue
            if not (0.02 <= height_ratio <= 0.09):
                continue
            center_bias = abs(cx - width / 2) / max(width, 1)
            score = float(button.get("area", 0)) - center_bias * width * 20 + cy
            candidates.append((score, int(cx), int(cy)))
        if not candidates:
            return None
        _, x, y = max(candidates, key=lambda item: item[0])
        return x, y

    @staticmethod
    def _playback_cancel_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        return int(width * 0.279), int(height * 0.652)

    @staticmethod
    def _goal_achieved_continue_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        if RaceRoutePlanner._looks_like_target_race_list(img):
            return None
        if RaceRoutePlanner._race_preview_action_points(img) is not None:
            return None

        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        top_band = hsv[0 : int(height * 0.12), :]
        center = hsv[int(height * 0.18) : int(height * 0.80), int(width * 0.02) : int(width * 0.98)]
        coach_bubble = hsv[int(height * 0.10) : int(height * 0.24), int(width * 0.12) : int(width * 0.98)]
        bottom_band = hsv[int(height * 0.76) : int(height * 0.94), :]
        button = hsv[int(height * 0.80) : int(height * 0.92), int(width * 0.20) : int(width * 0.80)]
        if top_band.size == 0 or center.size == 0 or coach_bubble.size == 0 or bottom_band.size == 0 or button.size == 0:
            return None

        top_green = np.mean(
            (top_band[:, :, 0] >= 38)
            & (top_band[:, :, 0] <= 92)
            & (top_band[:, :, 1] > 70)
            & (top_band[:, :, 2] > 80)
        )
        center_white = np.mean((center[:, :, 1] < 70) & (center[:, :, 2] > 165))
        center_gray = np.mean((center[:, :, 1] < 55) & (center[:, :, 2] > 95) & (center[:, :, 2] < 195))
        coach_white = np.mean((coach_bubble[:, :, 1] < 70) & (coach_bubble[:, :, 2] > 165))
        bottom_green = np.mean(
            (bottom_band[:, :, 0] >= 38)
            & (bottom_band[:, :, 0] <= 92)
            & (bottom_band[:, :, 1] > 70)
            & (bottom_band[:, :, 2] > 80)
        )
        button_green = np.mean(
            (button[:, :, 0] >= 38)
            & (button[:, :, 0] <= 92)
            & (button[:, :, 1] > 70)
            & (button[:, :, 2] > 80)
        )
        progress_layout = (
            top_green > 0.16
            and coach_white > 0.45
            and center_white > 0.42
            and center_gray > 0.22
            and bottom_green > 0.18
            and button_green > 0.30
        )
        achieved_layout = (
            top_green > 0.14
            and center_white > 0.62
            and coach_white > 0.40
            and bottom_green > 0.16
            and button_green > 0.24
            and center_gray < 0.08
        )
        if not (progress_layout or achieved_layout):
            return None
        return int(width * 0.50), int(height * 0.868)

    @staticmethod
    def _race_reward_continue_button(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        if RaceRoutePlanner._looks_like_target_race_list(img):
            return None
        if RaceRoutePlanner._race_preview_action_points(img) is not None:
            return None
        button = RaceRoutePlanner._bottom_center_green_button(
            perception,
            img,
            min_y_ratio=0.80,
        )
        if button is None or button[0] < width * 0.50:
            return None

        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        panel = hsv[int(height * 0.48) : int(height * 0.88), int(width * 0.02) : int(width * 0.98)]
        if panel.size == 0:
            return None
        panel_white = np.mean((panel[:, :, 1] < 60) & (panel[:, :, 2] > 175))
        panel_yellow = np.mean(
            (panel[:, :, 0] >= 15)
            & (panel[:, :, 0] <= 45)
            & (panel[:, :, 1] > 70)
            & (panel[:, :, 2] > 110)
        )
        panel_orange = np.mean(
            (panel[:, :, 0] >= 5)
            & (panel[:, :, 0] <= 30)
            & (panel[:, :, 1] > 70)
            & (panel[:, :, 2] > 100)
        )
        if panel_white > 0.60 and (panel_yellow > 0.18 or panel_orange > 0.18):
            return button
        return None

    @staticmethod
    def _primary_confirm_button(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        candidates: list[tuple[float, int, int]] = []
        for button in perception.get("buttons", []):
            cx, cy = button.get("center", [0, 0])
            _x, _y, w, _h = button.get("bbox", [0, 0, 0, 0])
            if not (width * 0.45 <= cx <= width * 0.93):
                continue
            if not (height * 0.58 <= cy <= height * 0.96):
                continue
            score = float(button.get("area", 0)) + w
            candidates.append((score, int(cx), int(cy)))
        if not candidates:
            return None
        _, x, y = max(candidates, key=lambda item: item[0])
        return x, y

    @staticmethod
    def _landscape_race_skip_toggle_state(img: np.ndarray) -> str | None:
        height, width = img.shape[:2]
        if width <= height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        control = hsv[
            int(height * 0.84) : int(height * 0.99),
            int(width * 0.015) : int(width * 0.16),
        ]
        if control.size == 0:
            return None
        white = np.mean((control[:, :, 1] < 70) & (control[:, :, 2] > 170))
        green = np.mean(
            (control[:, :, 0] >= 38)
            & (control[:, :, 0] <= 92)
            & (control[:, :, 1] > 70)
            & (control[:, :, 2] > 80)
        )
        orange = np.mean(
            (control[:, :, 0] >= 5)
            & (control[:, :, 0] <= 30)
            & (control[:, :, 1] > 50)
            & (control[:, :, 2] > 80)
        )
        dark = np.mean(control[:, :, 2] < 80)
        green_mask = cv2.inRange(
            control,
            np.array([38, 70, 80], dtype=np.uint8),
            np.array([92, 255, 255], dtype=np.uint8),
        )
        contours, _ = cv2.findContours(
            green_mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        control_height, control_width = control.shape[:2]
        inset_green_button = any(
            x > 2
            and y > 2
            and control_width * 0.60 <= w <= control_width * 0.95
            and control_height * 0.25 <= h <= control_height * 0.85
            and cv2.contourArea(contour) >= control_width * control_height * 0.15
            for contour in contours
            for x, y, w, h in [cv2.boundingRect(contour)]
        )
        if inset_green_button and green > 0.15 and white < 0.12:
            return "on"
        if dark > 0.30 and white > 0.18 and orange > 0.012 and green < 0.05:
            return "off"
        return None

    @staticmethod
    def _count_portrait_watch_controls(img: np.ndarray, visible_text: str | None,
                                      *, require_camera_label: bool = True) -> dict[str, Any] | None:
        if img.shape[:2] != (1280, 720) or (require_camera_label and
                "拍摄" not in {line.strip() for line in (visible_text or "").splitlines()}):
            return None
        height, width = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        # Same circle and arrow detectors as the existing landscape skip helper.
        radius_min, radius_max = int(width * 0.035), int(width * 0.065)
        circles = cv2.HoughCircles(gray, cv2.HOUGH_GRADIENT, dp=1.2, minDist=radius_min * 2,
            param1=100, param2=30, minRadius=radius_min, maxRadius=radius_max)
        if circles is None:
            return None
        controls = sorted((tuple(int(round(v)) for v in circle) for circle in circles[0]
            if circle[0] >= width * 0.60 and height * 0.93 <= circle[1] <= height * 0.97), key=lambda item: item[0])
        if len(controls) not in {2, 3}:
            return None
        left, right = controls[0], controls[-1]
        if not (width * 0.62 <= left[0] <= width * 0.70 and width * 0.87 <= right[0] <= width * 0.94):
            return None
        if any(abs(circle[1] - left[1]) > left[2] * 0.35 or abs(circle[2] - left[2]) > left[2] * 0.20 for circle in controls):
            return None
        arrows = [circle for circle in controls if RaceRoutePlanner._has_double_arrow(gray, circle)]
        if len(controls) == 3:
            middle = controls[1]
            if ((arrows and arrows != [middle]) or not left[2] + middle[2] < middle[0] - left[0] <= middle[2] * 3.5
                or not middle[2] + right[2] < right[0] - middle[0] <= middle[2] * 3.5):
                return None
        elif arrows:
            return None
        return {"source": "count_watch.portrait_camera_controls", "circles": [list(circle) for circle in controls],
                "skip_point": list(arrows[0][:2]) if arrows else None}

    @staticmethod
    def _right_bottom_race_skip_to_end_point(
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width <= height:
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        radius_min = max(16, int(min(width, height) * 0.035))
        radius_max = max(radius_min + 1, int(min(width, height) * 0.065))
        circles = cv2.HoughCircles(
            gray,
            cv2.HOUGH_GRADIENT,
            dp=1.2,
            minDist=radius_min * 2,
            param1=100,
            param2=30,
            minRadius=radius_min,
            maxRadius=radius_max,
        )
        if circles is None:
            return None
        candidates = sorted(
            (
                (int(round(x)), int(round(y)), int(round(radius)))
                for x, y, radius in circles[0]
                if x >= width * 0.65 and y >= height * 0.55
            ),
            key=lambda item: item[0],
        )
        for left_index, left in enumerate(candidates):
            left_x, left_y, left_radius = left
            for right_x, right_y, right_radius in candidates[left_index + 1 :]:
                if abs(left_radius - right_radius) > max(left_radius, right_radius) * 0.20:
                    continue
                distance_x = right_x - left_x
                if not (
                    left_radius + right_radius < distance_x
                    <= max(left_radius, right_radius) * 3.5
                ):
                    continue
                if abs(left_y - right_y) > max(left_radius, right_radius) * 0.35:
                    continue
                if RaceRoutePlanner._has_double_arrow(gray, left):
                    return left_x, left_y
        return None

    @staticmethod
    def _has_double_arrow(gray: np.ndarray, circle: tuple[int, int, int]) -> bool:
        center_x, center_y, radius = circle
        inset = max(8, int(radius * 0.60))
        y0, y1 = center_y - inset, center_y + inset + 1
        x0, x1 = center_x - inset, center_x + inset + 1
        if y0 < 0 or x0 < 0 or y1 > gray.shape[0] or x1 > gray.shape[1]:
            return False
        crop = gray[y0:y1, x0:x1]
        axis = np.arange(-inset, inset + 1)
        grid_x, grid_y = np.meshgrid(axis, axis)
        inner = (grid_x * grid_x + grid_y * grid_y) <= inset * inset
        mask = ((crop < 100) & inner).astype(np.uint8) * 255
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        def is_right_pointing_triangle(contour: np.ndarray) -> tuple[float, float] | None:
            x, y, width, height = cv2.boundingRect(contour)
            area = float(cv2.contourArea(contour))
            if not (
                area >= radius * radius * 0.04
                and radius * 0.35 <= width <= radius * 1.10
                and radius * 0.35 <= height <= radius * 1.10
                and 0.35 <= width / height <= 1.50
                and area / (width * height) <= 0.70
            ):
                return None
            perimeter = cv2.arcLength(contour, True)
            vertices = cv2.approxPolyDP(contour, 0.08 * perimeter, True)
            if len(vertices) != 3:
                return None
            moments = cv2.moments(contour)
            if moments["m00"] == 0:
                return None
            centroid_x = moments["m10"] / moments["m00"]
            if centroid_x > x + width * 0.43:
                return None
            points = vertices.reshape(-1, 2)
            rightmost = points[np.argmax(points[:, 0])]
            if not (
                rightmost[1] >= y + height * 0.25
                and rightmost[1] <= y + height * 0.75
            ):
                return None
            return x + width / 2, y + height / 2

        def is_fused_double_arrow(contour: np.ndarray) -> tuple[int, int, int, int] | None:
            x, y, width, height = cv2.boundingRect(contour)
            area = float(cv2.contourArea(contour))
            if not (
                area >= radius * radius * 0.08
                and radius * 0.50 <= width <= radius * 0.85
                and radius * 0.25 <= height <= radius * 0.55
                and 1.50 <= width / height <= 2.25
                and 0.25 <= area / (width * height) <= 0.65
            ):
                return None
            perimeter = cv2.arcLength(contour, True)
            vertices = cv2.approxPolyDP(contour, 0.05 * perimeter, True)
            if len(vertices) != 5:
                return None
            moments = cv2.moments(contour)
            if moments["m00"] == 0:
                return None
            centroid_x = moments["m10"] / moments["m00"]
            if not (x + width * 0.25 <= centroid_x <= x + width * 0.48):
                return None
            return x, y, width, height

        def is_terminal_bar(contour: np.ndarray) -> tuple[int, int, int, int] | None:
            x, y, width, height = cv2.boundingRect(contour)
            area = float(cv2.contourArea(contour))
            if not (
                area >= radius * radius * 0.04
                and width <= radius * 0.30
                and height >= radius * 0.25
                and area / (width * height) >= 0.65
            ):
                return None
            return x, y, width, height

        fused_double_arrows = [
            candidate
            for contour in contours
            for candidate in [is_fused_double_arrow(contour)]
            if candidate is not None
        ]
        terminal_bars = [
            candidate
            for contour in contours
            for candidate in [is_terminal_bar(contour)]
            if candidate is not None
        ]
        for arrow_x, arrow_y, arrow_width, arrow_height in fused_double_arrows:
            for bar_x, bar_y, _bar_width, bar_height in terminal_bars:
                if (
                    0 <= bar_x - (arrow_x + arrow_width) <= radius * 0.25
                    and abs((bar_y + bar_height / 2) - (arrow_y + arrow_height / 2))
                    <= radius * 0.35
                ):
                    return True

        components = sorted(
            (
                candidate
                for contour in contours
                for candidate in [is_right_pointing_triangle(contour)]
                if candidate is not None
            ),
            key=lambda item: item[0],
        )
        return any(
            0 < right_x - left_x <= radius * 0.90
            and abs(right_y - left_y) <= radius * 0.35
            for (left_x, left_y), (right_x, right_y) in zip(components, components[1:])
        )

    @staticmethod
    def _landscape_bottom_center_green_action_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width <= height:
            return None
        x0, x1 = int(width * 0.18), int(width * 0.82)
        y0, y1 = int(height * 0.82), int(height * 0.98)
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            return None
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(
            hsv,
            np.array([38, 90, 110], dtype=np.uint8),
            np.array([92, 255, 255], dtype=np.uint8),
        )
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates: list[tuple[float, int, int]] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = float(cv2.contourArea(contour))
            if area < width * height * 0.002:
                continue
            cx = x0 + x + w // 2
            cy = y0 + y + h // 2
            if not (width * 0.24 <= cx <= width * 0.76):
                continue
            center_bias = abs(cx - width / 2) / max(width, 1)
            candidates.append((area - center_bias * width * 10, int(cx), int(cy)))
        if not candidates:
            return None
        _score, cx, cy = max(candidates, key=lambda item: item[0])
        return cx, cy

    @staticmethod
    def _view_result_button(
        img: np.ndarray,
        go_to_race_button: tuple[int, int],
    ) -> tuple[int, int]:
        height, width = img.shape[:2]
        _go_x, go_y = go_to_race_button
        return int(width * 0.347), min(max(int(go_y), int(height * 0.86)), int(height * 0.96))

    @staticmethod
    def _looks_like_race_modal(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        modal = hsv[int(height * 0.25) : int(height * 0.88), int(width * 0.02) : int(width * 0.98)]
        if modal.size == 0:
            return False
        header = hsv[int(height * 0.22) : int(height * 0.30), int(width * 0.02) : int(width * 0.98)]
        confirm_zone = hsv[int(height * 0.66) : int(height * 0.78), int(width * 0.50) : int(width * 0.95)]
        cancel_zone = hsv[int(height * 0.66) : int(height * 0.78), int(width * 0.05) : int(width * 0.50)]
        green = np.mean(
            (modal[:, :, 0] >= 38)
            & (modal[:, :, 0] <= 92)
            & (modal[:, :, 1] > 70)
            & (modal[:, :, 2] > 80)
        )
        white = np.mean((modal[:, :, 1] < 60) & (modal[:, :, 2] > 175))
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        confirm_green = np.mean(
            (confirm_zone[:, :, 0] >= 38)
            & (confirm_zone[:, :, 0] <= 92)
            & (confirm_zone[:, :, 1] > 70)
            & (confirm_zone[:, :, 2] > 80)
        )
        cancel_white = np.mean((cancel_zone[:, :, 1] < 60) & (cancel_zone[:, :, 2] > 175))
        old_layout = (
            green > 0.06
            and white > 0.22
            and header_green > 0.35
            and confirm_green > 0.25
            and cancel_white > 0.65
        )
        if old_layout:
            return True

        top_header = hsv[int(height * 0.02) : int(height * 0.08), int(width * 0.02) : int(width * 0.98)]
        large_panel = hsv[int(height * 0.08) : int(height * 0.98), int(width * 0.02) : int(width * 0.98)]
        bottom_confirm = hsv[int(height * 0.88) : int(height * 0.96), int(width * 0.50) : int(width * 0.95)]
        bottom_cancel = hsv[int(height * 0.88) : int(height * 0.96), int(width * 0.05) : int(width * 0.50)]
        if (
            top_header.size == 0
            or large_panel.size == 0
            or bottom_confirm.size == 0
            or bottom_cancel.size == 0
        ):
            return False
        top_header_green = np.mean(
            (top_header[:, :, 0] >= 38)
            & (top_header[:, :, 0] <= 92)
            & (top_header[:, :, 1] > 70)
            & (top_header[:, :, 2] > 80)
        )
        panel_white = np.mean((large_panel[:, :, 1] < 60) & (large_panel[:, :, 2] > 175))
        bottom_confirm_green = np.mean(
            (bottom_confirm[:, :, 0] >= 38)
            & (bottom_confirm[:, :, 0] <= 92)
            & (bottom_confirm[:, :, 1] > 70)
            & (bottom_confirm[:, :, 2] > 80)
        )
        bottom_cancel_white = np.mean((bottom_cancel[:, :, 1] < 60) & (bottom_cancel[:, :, 2] > 175))
        return bool(
            top_header_green > 0.55
            and panel_white > 0.50
            and bottom_confirm_green > 0.35
            and bottom_cancel_white > 0.70
        )

    @staticmethod
    def _looks_like_race_playback_modal(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.295) : int(height * 0.345), int(width * 0.01) : int(width * 0.99)]
        song_section = hsv[int(height * 0.36) : int(height * 0.51), int(width * 0.02) : int(width * 0.98)]
        settings = hsv[int(height * 0.51) : int(height * 0.65), int(width * 0.02) : int(width * 0.98)]
        confirm_zone = hsv[int(height * 0.62) : int(height * 0.70), int(width * 0.50) : int(width * 0.95)]
        cancel_zone = hsv[int(height * 0.62) : int(height * 0.70), int(width * 0.05) : int(width * 0.50)]
        if (
            header.size == 0
            or song_section.size == 0
            or settings.size == 0
            or confirm_zone.size == 0
            or cancel_zone.size == 0
        ):
            return False
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        song_white = np.mean((song_section[:, :, 1] < 60) & (song_section[:, :, 2] > 175))
        settings_white = np.mean((settings[:, :, 1] < 60) & (settings[:, :, 2] > 175))
        confirm_green = np.mean(
            (confirm_zone[:, :, 0] >= 38)
            & (confirm_zone[:, :, 0] <= 92)
            & (confirm_zone[:, :, 1] > 70)
            & (confirm_zone[:, :, 2] > 80)
        )
        cancel_white = np.mean((cancel_zone[:, :, 1] < 60) & (cancel_zone[:, :, 2] > 175))
        return bool(
            header_green > 0.70
            and song_white > 0.65
            and settings_white > 0.55
            and confirm_green > 0.45
            and cancel_white > 0.75
        )

    @staticmethod
    def _looks_like_result_continue(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        if RaceRoutePlanner._looks_like_target_race_list(img):
            return False
        return RaceRoutePlanner._bottom_center_green_button(
            perception,
            img,
            min_y_ratio=0.72,
        ) is not None

    @staticmethod
    def _screen_text_has_race_list_title(evidence: dict[str, Any]) -> bool:
        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict):
            return False
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        # The persistent goal banner can remain above an event's +stat reward.
        # Neither proves a list title; title-missing lists keep their separate
        # visual-list and target-card paths in the caller.
        return "赛事一览" in normalized

    @staticmethod
    def _screen_text_has_fan_count_result(evidence: dict[str, Any]) -> bool:
        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict):
            return False
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        return "\u7c89\u4e1d\u6570" in normalized

    @staticmethod
    def _has_fan_goal_shortfall_warning_text(evidence: dict[str, Any]) -> bool:
        """Accept the distinct fan-shortfall warning, never a race confirmation."""

        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict):
            return False
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        return (
            (
                "粉丝数未达到目标赛事要求" in normalized
                or "目标粉丝数不足" in normalized
            )
            and (
                "距离目标赛事剩余" in normalized
                or (
                    "距离目标期限还剩" in normalized
                    and "回合" in normalized
                )
            )
            and "通过参赛获得粉丝吧" in normalized
            and "前往赛事" in normalized
            and "赛事详情" not in normalized
            and "要参加比赛吗" not in normalized
        )

    @staticmethod
    def _looks_like_target_race_list(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        title_top = hsv[0 : int(height * 0.08), 0 : int(width * 0.35)]
        goal_card = hsv[int(height * 0.54) : int(height * 0.80), int(width * 0.02) : int(width * 0.98)]
        selected_row = hsv[int(height * 0.54) : int(height * 0.67), int(width * 0.02) : int(width * 0.98)]
        disabled_row = hsv[int(height * 0.67) : int(height * 0.82), int(width * 0.02) : int(width * 0.98)]
        bottom_actions = hsv[int(height * 0.82) : int(height * 0.94), :]
        goal_header = hsv[int(height * 0.05) : int(height * 0.13), int(width * 0.20) : int(width * 0.96)]
        grade_badge = hsv[int(height * 0.54) : int(height * 0.61), int(width * 0.35) : int(width * 0.48)]
        target_label = hsv[int(height * 0.545) : int(height * 0.59), int(width * 0.02) : int(width * 0.36)]
        if (
            title_top.size == 0
            or goal_card.size == 0
            or selected_row.size == 0
            or disabled_row.size == 0
            or bottom_actions.size == 0
            or goal_header.size == 0
            or grade_badge.size == 0
            or target_label.size == 0
        ):
            return False
        title_red = np.mean(
            ((title_top[:, :, 0] <= 8) | (title_top[:, :, 0] >= 170))
            & (title_top[:, :, 1] > 70)
            & (title_top[:, :, 2] > 100)
        )
        goal_blue = np.mean(
            (goal_card[:, :, 0] >= 85)
            & (goal_card[:, :, 0] <= 125)
            & (goal_card[:, :, 1] > 60)
            & (goal_card[:, :, 2] > 90)
        )
        selected_green = np.mean(
            (selected_row[:, :, 0] >= 38)
            & (selected_row[:, :, 0] <= 92)
            & (selected_row[:, :, 1] > 70)
            & (selected_row[:, :, 2] > 80)
        )
        disabled_blue = np.mean(
            (disabled_row[:, :, 0] >= 85)
            & (disabled_row[:, :, 0] <= 125)
            & (disabled_row[:, :, 1] > 60)
            & (disabled_row[:, :, 2] > 90)
        )
        bottom_green = np.mean(
            (bottom_actions[:, :, 0] >= 38)
            & (bottom_actions[:, :, 0] <= 92)
            & (bottom_actions[:, :, 1] > 70)
            & (bottom_actions[:, :, 2] > 80)
        )
        selected_white = np.mean((selected_row[:, :, 1] < 60) & (selected_row[:, :, 2] > 175))
        selected_orange = np.mean(
            (selected_row[:, :, 0] >= 5)
            & (selected_row[:, :, 0] <= 35)
            & (selected_row[:, :, 1] > 60)
            & (selected_row[:, :, 2] > 100)
        )
        header_white = np.mean((goal_header[:, :, 1] < 60) & (goal_header[:, :, 2] > 170))
        header_orange = np.mean(
            (goal_header[:, :, 0] >= 5)
            & (goal_header[:, :, 0] <= 35)
            & (goal_header[:, :, 1] > 60)
            & (goal_header[:, :, 2] > 100)
        )
        header_blue = np.mean(
            (goal_header[:, :, 0] >= 85)
            & (goal_header[:, :, 0] <= 125)
            & (goal_header[:, :, 1] > 60)
            & (goal_header[:, :, 2] > 90)
        )
        badge_blue = np.mean(
            (grade_badge[:, :, 0] >= 85)
            & (grade_badge[:, :, 0] <= 125)
            & (grade_badge[:, :, 1] > 60)
            & (grade_badge[:, :, 2] > 90)
        )
        badge_orange = np.mean(
            (grade_badge[:, :, 0] >= 5)
            & (grade_badge[:, :, 0] <= 35)
            & (grade_badge[:, :, 1] > 60)
            & (grade_badge[:, :, 2] > 100)
        )
        target_label_red = np.mean(
            ((target_label[:, :, 0] <= 8) | (target_label[:, :, 0] >= 170))
            & (target_label[:, :, 1] > 70)
            & (target_label[:, :, 2] > 100)
        )
        legacy_goal_list = (
            title_red > 0.10
            and goal_blue > 0.08
            and selected_green > 0.005
            and disabled_blue > 0.12
            and bottom_green > 0.12
        )
        selected_g1_goal_list = (
            title_red > 0.10
            and header_white > 0.45
            and (header_orange > 0.06 or header_blue > 0.14)
            and selected_white > 0.50
            and selected_orange > 0.09
            and badge_blue > 0.08
            and bottom_green > 0.12
        )
        current_target_list = (
            title_red > 0.10
            and header_white > 0.45
            and target_label_red > 0.25
            and selected_white > 0.50
            and selected_green > 0.05
            and bottom_green > 0.12
        )
        return bool(legacy_goal_list or selected_g1_goal_list or current_target_list)

    @staticmethod
    def _looks_like_non_goal_graded_selected_race(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        top_goal = hsv[int(height * 0.055) : int(height * 0.125), int(width * 0.30) : int(width * 0.72)]
        remaining_goal = hsv[int(height * 0.085) : int(height * 0.125), int(width * 0.56) : int(width * 0.84)]
        row_label = hsv[int(height * 0.535) : int(height * 0.665), int(width * 0.02) : int(width * 0.38)]
        grade_badge = hsv[int(height * 0.545) : int(height * 0.605), int(width * 0.35) : int(width * 0.48)]
        if top_goal.size == 0 or remaining_goal.size == 0 or row_label.size == 0 or grade_badge.size == 0:
            return False
        top_white = np.mean((top_goal[:, :, 1] < 60) & (top_goal[:, :, 2] > 170))
        top_orange = np.mean(
            (top_goal[:, :, 0] >= 5)
            & (top_goal[:, :, 0] <= 35)
            & (top_goal[:, :, 1] > 60)
            & (top_goal[:, :, 2] > 100)
        )
        remaining_orange = np.mean(
            (remaining_goal[:, :, 0] >= 5)
            & (remaining_goal[:, :, 0] <= 35)
            & (remaining_goal[:, :, 1] > 60)
            & (remaining_goal[:, :, 2] > 100)
        )
        row_blue = np.mean(
            (row_label[:, :, 0] >= 85)
            & (row_label[:, :, 0] <= 125)
            & (row_label[:, :, 1] > 40)
            & (row_label[:, :, 2] > 80)
        )
        row_target_green = np.mean(
            (row_label[:, :, 0] >= 38)
            & (row_label[:, :, 0] <= 92)
            & (row_label[:, :, 1] > 70)
            & (row_label[:, :, 2] > 80)
        )
        row_target_red = np.mean(
            ((row_label[:, :, 0] <= 8) | (row_label[:, :, 0] >= 170))
            & (row_label[:, :, 1] > 70)
            & (row_label[:, :, 2] > 120)
        )
        badge_pink = np.mean(
            ((grade_badge[:, :, 0] <= 8) | (grade_badge[:, :, 0] >= 165))
            & (grade_badge[:, :, 1] > 55)
            & (grade_badge[:, :, 2] > 130)
        )
        badge_orange = np.mean(
            (grade_badge[:, :, 0] >= 5)
            & (grade_badge[:, :, 0] <= 35)
            & (grade_badge[:, :, 1] > 60)
            & (grade_badge[:, :, 2] > 100)
        )
        return bool(
            top_white > 0.45
            and top_orange > 0.14
            and remaining_orange > 0.20
            and row_blue > 0.25
            and row_target_green < 0.08
            and row_target_red < 0.08
            and badge_pink > 0.10
            and badge_orange > 0.10
        )

    @staticmethod
    def _looks_like_rank_overlay_result(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        if str(perception.get("screen")) != "portrait_ui":
            return False
        if RaceRoutePlanner._bottom_center_green_button(
            perception,
            img,
            min_y_ratio=0.72,
        ) is not None:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        center = hsv[int(height * 0.45) : int(height * 0.72), int(width * 0.20) : int(width * 0.80)]
        if center.size == 0:
            return False
        white = np.mean((center[:, :, 1] < 60) & (center[:, :, 2] > 175))
        brown = np.mean(
            (center[:, :, 0] >= 5)
            & (center[:, :, 0] <= 25)
            & (center[:, :, 1] > 50)
            & (center[:, :, 2] > 80)
        )
        dark = np.mean(hsv[:, :, 2] < 95)
        return bool(dark > 0.35 and white > 0.18 and brown > 0.10)

    @staticmethod
    def _looks_like_large_rank_result_animation(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        if str(perception.get("screen")) != "portrait_ui":
            return False
        if RaceRoutePlanner._bottom_center_green_button(
            perception,
            img,
            min_y_ratio=0.72,
        ) is not None:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        center = hsv[int(height * 0.45) : int(height * 0.72), int(width * 0.20) : int(width * 0.80)]
        rank_text = hsv[int(height * 0.52) : int(height * 0.72), int(width * 0.18) : int(width * 0.82)]
        if center.size == 0 or rank_text.size == 0:
            return False
        dark = np.mean(hsv[:, :, 2] < 95)
        center_gray = np.mean((center[:, :, 1] < 55) & (center[:, :, 2] > 70) & (center[:, :, 2] < 180))
        full_gray = np.mean((hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 60) & (hsv[:, :, 2] < 190))
        rank_text_white = np.mean((rank_text[:, :, 1] < 70) & (rank_text[:, :, 2] > 165))
        rank_text_bluegray = np.mean(
            (rank_text[:, :, 0] >= 95)
            & (rank_text[:, :, 0] <= 130)
            & (rank_text[:, :, 1] > 20)
            & (rank_text[:, :, 1] < 120)
            & (rank_text[:, :, 2] > 80)
            & (rank_text[:, :, 2] < 220)
        )
        giant_gray_digit = center_gray > 0.45 and full_gray > 0.38
        silver_rank_text = rank_text_white > 0.50 and rank_text_bluegray > 0.20
        center_gold = np.mean(
            (center[:, :, 0] <= 40)
            & (center[:, :, 1] > 80)
            & (center[:, :, 2] > 120)
        )
        full_gold = np.mean(
            (hsv[:, :, 0] <= 40)
            & (hsv[:, :, 1] > 80)
            & (hsv[:, :, 2] > 120)
        )
        giant_gold_rank = (
            (center_gold > 0.35 and full_gold > 0.20)
            or (dark > 0.30 and center_gold > 0.25 and full_gold > 0.045)
        )
        return bool(
            (dark > 0.35 and giant_gray_digit)
            or (dark > 0.20 and silver_rank_text)
            or (dark > 0.20 and giant_gold_rank)
        )

    @staticmethod
    def _looks_like_event_choice_layout(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        crop = img[
            int(height * 0.48) : int(height * 0.72),
            int(width * 0.02) : int(width * 0.98),
        ]
        if crop.size == 0:
            return False
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask = ((hsv[:, :, 1] < 80) & (hsv[:, :, 2] > 170)).astype("uint8") * 255
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 7))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes: list[tuple[int, int, int, int]] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w > width * 0.65 and h > height * 0.02:
                boxes.append((x, y, w, h))
        boxes.sort(key=lambda item: item[1])
        if len(boxes) < 2:
            return False
        for first, second in zip(boxes, boxes[1:]):
            gap = second[1] - (first[1] + first[3])
            if gap > height * 0.015:
                return True
        return False

    @staticmethod
    def _race_preview_action_points(img: np.ndarray) -> dict[str, Any] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        left = hsv[int(height * 0.875) : int(height * 0.955), int(width * 0.18) : int(width * 0.50)]
        right = hsv[int(height * 0.875) : int(height * 0.955), int(width * 0.50) : int(width * 0.82)]
        gap = hsv[int(height * 0.875) : int(height * 0.955), int(width * 0.47) : int(width * 0.53)]
        if left.size == 0 or right.size == 0:
            return None
        left_green = np.mean(
            (left[:, :, 0] >= 38)
            & (left[:, :, 0] <= 92)
            & (left[:, :, 1] > 70)
            & (left[:, :, 2] > 80)
        )
        left_white = np.mean((left[:, :, 1] < 70) & (left[:, :, 2] > 170))
        left_brown = np.mean(
            (left[:, :, 0] >= 5)
            & (left[:, :, 0] <= 30)
            & (left[:, :, 1] > 50)
            & (left[:, :, 2] > 70)
        )
        right_green = np.mean(
            (right[:, :, 0] >= 38)
            & (right[:, :, 0] <= 92)
            & (right[:, :, 1] > 70)
            & (right[:, :, 2] > 80)
        )
        right_bright_green = np.mean(
            (right[:, :, 0] >= 38)
            & (right[:, :, 0] <= 92)
            & (right[:, :, 1] > 120)
            & (right[:, :, 2] > 170)
        )
        right_white = np.mean((right[:, :, 1] < 70) & (right[:, :, 2] > 170))
        lock_icon = hsv[int(height * 0.83) : int(height * 0.90), int(width * 0.18) : int(width * 0.30)]
        lock_gold = 0.0
        if lock_icon.size:
            lock_gold = np.mean(
                (lock_icon[:, :, 0] >= 15)
                & (lock_icon[:, :, 0] <= 35)
                & (lock_icon[:, :, 1] > 60)
                & (lock_icon[:, :, 2] > 120)
            )
        gap_green = 0.0
        if gap.size:
            gap_green = np.mean(
                (gap[:, :, 0] >= 38)
                & (gap[:, :, 0] <= 92)
                & (gap[:, :, 1] > 70)
                & (gap[:, :, 2] > 80)
            )
        if not (left_white > 0.42 and left_brown > 0.04 and right_green > 0.35 and right_white < 0.20):
            left_gray = np.mean((left[:, :, 1] < 60) & (left[:, :, 2] > 80) & (left[:, :, 2] < 210))
            left_locked = (
                (
                    left_gray > 0.45
                    and left_white < 0.10
                    and left_green < 0.10
                    and right_green > 0.35
                    and right_bright_green > 0.25
                    and gap_green < 0.35
                )
                or (
                    lock_gold > 0.04
                    and left_gray > 0.35
                    and left_green < 0.20
                    and right_green > 0.35
                    and right_bright_green > 0.25
                )
            )
            if not left_locked:
                return None
        else:
            left_gray = np.mean((left[:, :, 1] < 60) & (left[:, :, 2] > 80) & (left[:, :, 2] < 210))
            left_locked = False

        left_locked = bool(
            left_locked
            or (
                lock_gold > 0.04
                and left_gray > 0.35
                and left_green < 0.20
                and right_green > 0.35
                and right_bright_green > 0.25
                and gap_green < 0.35
            )
        )
        return {
            "view_result": (int(width * 0.351), int(height * 0.919)),
            "go_to_race": RaceRoutePlanner._bottom_center_green_region(img, min_y_ratio=0.86)
            or (int(width * 0.65), int(height * 0.919)),
            "view_result_locked": left_locked,
        }

    @staticmethod
    def _looks_like_unknown_race_preview_action_layout(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        left = hsv[int(height * 0.875) : int(height * 0.955), int(width * 0.18) : int(width * 0.50)]
        right = hsv[int(height * 0.875) : int(height * 0.955), int(width * 0.50) : int(width * 0.82)]
        if left.size == 0 or right.size == 0:
            return False
        left_button_color = np.mean((left[:, :, 1] > 80) & (left[:, :, 2] > 80))
        right_green = np.mean(
            (right[:, :, 0] >= 38) & (right[:, :, 0] <= 92) & (right[:, :, 1] > 70) & (right[:, :, 2] > 80)
        )
        return bool(left_button_color > 0.10 and right_green > 0.35)

    @staticmethod
    def _looks_like_wbc_race_hub(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        def ratio(mask: np.ndarray, x0: float, x1: float, y0: float, y1: float) -> float:
            crop = mask[int(height * y0) : int(height * y1), int(width * x0) : int(width * x1)]
            if crop.size == 0:
                return 0.0
            return float(np.mean(crop))

        blue = (
            (hsv[:, :, 0] >= 85)
            & (hsv[:, :, 0] <= 130)
            & (hsv[:, :, 1] > 40)
            & (hsv[:, :, 2] > 80)
        )
        gold = (
            (hsv[:, :, 0] >= 15)
            & (hsv[:, :, 0] <= 35)
            & (hsv[:, :, 1] > 60)
            & (hsv[:, :, 2] > 120)
        )
        green = (
            (hsv[:, :, 0] >= 38)
            & (hsv[:, :, 0] <= 92)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 80)
        )
        pink = (
            (hsv[:, :, 0] >= 140)
            & (hsv[:, :, 0] <= 175)
            & (hsv[:, :, 1] > 60)
            & (hsv[:, :, 2] > 100)
        )
        white = (hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 175)

        title_blue = ratio(blue, 0.15, 0.90, 0.08, 0.30)
        title_gold = ratio(gold, 0.15, 0.90, 0.08, 0.30)
        title_white = ratio(white, 0.15, 0.90, 0.08, 0.30)
        left_white = ratio(white, 0.04, 0.48, 0.78, 0.90)
        left_green = ratio(green, 0.04, 0.48, 0.78, 0.90)
        right_pink = ratio(pink, 0.48, 0.96, 0.76, 0.91)
        right_gold = ratio(gold, 0.48, 0.96, 0.76, 0.91)
        legacy_hub = bool(
            title_blue > 0.45
            and title_gold > 0.04
            and title_white < 0.25
            and (
                (left_white > 0.35 and left_green > 0.08)
                or (right_pink + right_gold > 0.30)
            )
        )
        return legacy_hub or SafeAdvancePlanner._looks_like_wbc_entry_splash(img)

    @staticmethod
    def _has_pre_race_commentary_menu_icon(img: np.ndarray) -> bool:
        """Detect the bottom-right 三 menu icon paired with the race-detail loading bar."""
        height, width = img.shape[:2]
        if width >= height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        def crop(x0: float, x1: float, y0: float, y1: float) -> np.ndarray:
            return hsv[int(height * y0) : int(height * y1), int(width * x0) : int(width * x1)]

        menu = crop(0.85, 0.97, 0.89, 0.95)
        loading = crop(0.67, 0.88, 0.91, 0.99)
        if menu.size == 0 or loading.size == 0:
            return False
        menu_white = float(np.mean((menu[:, :, 1] < 70) & (menu[:, :, 2] > 170)))
        menu_brown = float(
            np.mean(
                (menu[:, :, 0] >= 5)
                & (menu[:, :, 0] <= 30)
                & (menu[:, :, 1] > 50)
                & (menu[:, :, 2] > 70)
            )
        )
        loading_bg = float(
            np.mean(
                (loading[:, :, 0] >= 45)
                & (loading[:, :, 0] <= 90)
                & (loading[:, :, 1] > 60)
                & (loading[:, :, 2] > 60)
                & (loading[:, :, 2] < 180)
            )
        )
        return bool(menu_white > 0.35 and menu_brown > 0.08 and loading_bg > 0.65)

    @staticmethod
    def _swbc_special_entry_go_to_race_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        if (
            RaceClassifierMixin._has_pre_race_commentary_menu_icon(img)
            or RaceClassifierMixin._looks_like_race_detail_loading(img)
        ):
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        yellow = (
            (hsv[:, :, 0] >= 15)
            & (hsv[:, :, 0] <= 40)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 120)
        )
        white = (hsv[:, :, 1] < 70) & (hsv[:, :, 2] > 170)
        brown = (
            (hsv[:, :, 0] >= 5)
            & (hsv[:, :, 0] <= 30)
            & (hsv[:, :, 1] > 50)
            & (hsv[:, :, 2] > 70)
        )
        green = (
            (hsv[:, :, 0] >= 38)
            & (hsv[:, :, 0] <= 92)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 80)
        )

        def ratio(mask: np.ndarray, x0: float, x1: float, y0: float, y1: float) -> float:
            crop = mask[int(height * y0) : int(height * y1), int(width * x0) : int(width * x1)]
            if crop.size == 0:
                return 0.0
            return float(np.mean(crop))

        if not (
            ratio(yellow, 0.04, 0.16, 0.02, 0.08) > 0.10
            and ratio(white, 0.20, 0.82, 0.02, 0.08) > 0.35
            and ratio(green, 0.45, 0.84, 0.86, 0.96) > 0.70
            and ratio(brown, 0.16, 0.50, 0.86, 0.96) > 0.05
        ):
            return None

        x0, x1 = int(width * 0.45), int(width * 0.84)
        y0, y1 = int(height * 0.86), int(height * 0.96)
        roi = green[y0:y1, x0:x1].astype(np.uint8) * 255
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
        roi = cv2.morphologyEx(roi, cv2.MORPH_CLOSE, kernel, iterations=2)
        contours, _ = cv2.findContours(roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates: list[tuple[float, int, int]] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = float(cv2.contourArea(contour))
            if w < width * 0.20 or h < height * 0.04:
                continue
            cx = x0 + x + w // 2
            cy = y0 + y + h // 2
            candidates.append((area, int(cx), int(cy)))
        if not candidates:
            return None
        _, x, y = max(candidates, key=lambda item: item[0])
        return x, y

    @staticmethod
    def _looks_like_rank_points_result(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        panel = hsv[int(height * 0.46) : int(height * 0.90), :]
        if panel.size == 0:
            return False
        white = np.mean((panel[:, :, 1] < 60) & (panel[:, :, 2] > 175))
        green = np.mean(
            (panel[:, :, 0] >= 38)
            & (panel[:, :, 0] <= 92)
            & (panel[:, :, 1] > 70)
            & (panel[:, :, 2] > 80)
        )
        gray = np.mean((panel[:, :, 1] < 45) & (panel[:, :, 2] > 80) & (panel[:, :, 2] < 190))
        return bool(white > 0.70 and 0.015 < green < 0.08 and gray < 0.05)

    @staticmethod
    def _looks_like_fan_rank_result(img: np.ndarray) -> bool:
        if not RaceRoutePlanner._looks_like_rank_points_result(img):
            return False
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        race_summary = hsv[
            int(height * 0.39) : int(height * 0.48),
            :,
        ]
        grade_badge = hsv[
            int(height * 0.39) : int(height * 0.45),
            int(width * 0.03) : int(width * 0.22),
        ]
        rank_header = hsv[
            int(height * 0.48) : int(height * 0.54),
            int(width * 0.01) : int(width * 0.99),
        ]
        if race_summary.size == 0 or grade_badge.size == 0 or rank_header.size == 0:
            return False
        summary_white = np.mean(
            (race_summary[:, :, 1] < 60) & (race_summary[:, :, 2] > 175)
        )
        badge_saturated = np.mean(
            (grade_badge[:, :, 1] > 90) & (grade_badge[:, :, 2] > 100)
        )
        header_green = np.mean(
            (rank_header[:, :, 0] >= 38)
            & (rank_header[:, :, 0] <= 92)
            & (rank_header[:, :, 1] > 70)
            & (rank_header[:, :, 2] > 80)
        )
        return bool(
            summary_white > 0.65
            and badge_saturated > 0.20
            and header_green > 0.20
        )

    @staticmethod
    def _looks_like_ranking_list_result(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        panel = hsv[int(height * 0.45) : int(height * 0.88), int(width * 0.02) : int(width * 0.98)]
        bottom_right = hsv[int(height * 0.86) : int(height * 0.96), int(width * 0.50) : int(width * 0.95)]
        if panel.size == 0 or bottom_right.size == 0:
            return False
        panel_white = np.mean((panel[:, :, 1] < 60) & (panel[:, :, 2] > 175))
        panel_brown = np.mean(
            (panel[:, :, 0] >= 5)
            & (panel[:, :, 0] <= 25)
            & (panel[:, :, 1] > 50)
            & (panel[:, :, 2] > 80)
        )
        button_green = np.mean(
            (bottom_right[:, :, 0] >= 38)
            & (bottom_right[:, :, 0] <= 92)
            & (bottom_right[:, :, 1] > 70)
            & (bottom_right[:, :, 2] > 80)
        )
        return bool(panel_white > 0.80 and panel_brown > 0.14 and button_green > 0.20)

    @staticmethod
    def _screen_continue_point(img: np.ndarray) -> tuple[int, int]:
        height, width = img.shape[:2]
        return int(width * 0.50), int(height * 0.88)

    @staticmethod
    def _rank_points_next_button(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        candidates: list[tuple[float, int, int]] = []
        for button in perception.get("buttons", []):
            cx, cy = button.get("center", [0, 0])
            _x, _y, w, _h = button.get("bbox", [0, 0, 0, 0])
            if cx < width * 0.45:
                continue
            if cy < height * 0.82:
                continue
            candidates.append((float(w), int(cx), int(cy)))
        if not candidates:
            return None
        _, x, y = max(candidates, key=lambda item: item[0])
        return x, y

    @staticmethod
    def _looks_like_race_detail_go_to_race(
        perception: dict[str, Any],
        img: np.ndarray,
        screen: str,
    ) -> bool:
        if screen != "bottom_action":
            return False
        button = RaceRoutePlanner._bottom_center_green_button(
            perception,
            img,
            min_y_ratio=0.80,
        )
        if button is None:
            return False
        _height, width = img.shape[:2]
        return bool(button[0] >= width * 0.58 or RaceRoutePlanner._looks_like_center_race_detail_action_layout(img))

    @staticmethod
    def _race_detail_back_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        roi = hsv[
            int(height * 0.91) : int(height * 0.995),
            int(width * 0.01) : int(width * 0.22),
        ]
        if roi.size == 0:
            return None
        white = float(np.mean((roi[:, :, 1] < 70) & (roi[:, :, 2] > 165)))
        if white < 0.35:
            return None
        return int(width * 0.115), int(height * 0.963)

    @staticmethod
    def _looks_like_race_reward_next_page(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        if str(perception.get("screen")) != "bottom_action":
            return False
        button = RaceRoutePlanner._bottom_center_green_button(
            perception,
            img,
            min_y_ratio=0.80,
        )
        if button is None or button[0] < width * 0.50:
            return False
        if RaceRoutePlanner._race_preview_action_points(img) is not None:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        panel = hsv[int(height * 0.48) : int(height * 0.88), int(width * 0.02) : int(width * 0.98)]
        bottom_left = hsv[int(height * 0.88) : int(height * 0.98), int(width * 0.02) : int(width * 0.48)]
        bottom_right = hsv[int(height * 0.88) : int(height * 0.98), int(width * 0.50) : int(width * 0.98)]
        if panel.size == 0 or bottom_left.size == 0 or bottom_right.size == 0:
            return False
        panel_white = np.mean((panel[:, :, 1] < 60) & (panel[:, :, 2] > 175))
        panel_green = np.mean(
            (panel[:, :, 0] >= 38)
            & (panel[:, :, 0] <= 92)
            & (panel[:, :, 1] > 70)
            & (panel[:, :, 2] > 80)
        )
        bottom_left_pink = np.mean(
            (
                (bottom_left[:, :, 0] >= 150)
                & (bottom_left[:, :, 0] <= 175)
                & (bottom_left[:, :, 1] > 70)
                & (bottom_left[:, :, 2] > 100)
            )
            | (
                (bottom_left[:, :, 0] <= 5)
                & (bottom_left[:, :, 1] > 70)
                & (bottom_left[:, :, 2] > 100)
            )
        )
        bottom_right_green = np.mean(
            (bottom_right[:, :, 0] >= 38)
            & (bottom_right[:, :, 0] <= 92)
            & (bottom_right[:, :, 1] > 70)
            & (bottom_right[:, :, 2] > 80)
        )
        return bool(
            panel_white > 0.62
            and panel_green > 0.06
            and bottom_left_pink > 0.10
            and bottom_right_green > 0.16
        )

    @staticmethod
    def _race_result_next_button_from_image(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        if RaceRoutePlanner._looks_like_target_race_list(img):
            return None
        if RaceRoutePlanner._race_preview_action_points(img) is not None:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        panel = hsv[int(height * 0.48) : int(height * 0.88), int(width * 0.02) : int(width * 0.98)]
        bottom_left = hsv[int(height * 0.88) : int(height * 0.98), int(width * 0.02) : int(width * 0.48)]
        bottom_right = hsv[int(height * 0.88) : int(height * 0.98), int(width * 0.50) : int(width * 0.98)]
        top_bubble = hsv[int(height * 0.02) : int(height * 0.12), int(width * 0.15) : int(width * 0.96)]
        rank_fx = hsv[int(height * 0.16) : int(height * 0.45), 0 : int(width * 0.55)]
        if (
            panel.size == 0
            or bottom_left.size == 0
            or bottom_right.size == 0
            or top_bubble.size == 0
            or rank_fx.size == 0
        ):
            return None
        panel_white = np.mean((panel[:, :, 1] < 60) & (panel[:, :, 2] > 175))
        bottom_left_pink = np.mean(
            (
                (bottom_left[:, :, 0] >= 150)
                & (bottom_left[:, :, 0] <= 175)
                & (bottom_left[:, :, 1] > 70)
                & (bottom_left[:, :, 2] > 100)
            )
            | (
                (bottom_left[:, :, 0] <= 5)
                & (bottom_left[:, :, 1] > 70)
                & (bottom_left[:, :, 2] > 100)
            )
        )
        bottom_right_green = np.mean(
            (bottom_right[:, :, 0] >= 38)
            & (bottom_right[:, :, 0] <= 92)
            & (bottom_right[:, :, 1] > 70)
            & (bottom_right[:, :, 2] > 80)
        )
        top_bubble_white = np.mean((top_bubble[:, :, 1] < 60) & (top_bubble[:, :, 2] > 175))
        rank_gold = np.mean(
            (rank_fx[:, :, 0] >= 15)
            & (rank_fx[:, :, 0] <= 40)
            & (rank_fx[:, :, 1] > 60)
            & (rank_fx[:, :, 2] > 120)
        )
        if not (
            panel_white > 0.58
            and bottom_left_pink > 0.16
            and bottom_right_green > 0.16
            and (top_bubble_white > 0.45 or rank_gold > 0.08)
        ):
            return None

        mask = cv2.inRange(
            bottom_right,
            np.array([38, 70, 80], dtype=np.uint8),
            np.array([92, 255, 255], dtype=np.uint8),
        )
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates: list[tuple[float, int, int]] = []
        x_offset = int(width * 0.50)
        y_offset = int(height * 0.88)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = float(cv2.contourArea(contour))
            if area < width * height * 0.004:
                continue
            candidates.append((area, x_offset + x + w // 2, y_offset + y + h // 2))
        if not candidates:
            return int(width * 0.72), int(height * 0.925)
        _area, cx, cy = max(candidates, key=lambda item: item[0])
        return int(cx), int(cy)

    @staticmethod
    def _portrait_race_result_click_point(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        """Recognize the portrait race-result ``点击`` overlay.

        This is intentionally narrower than the generic portrait-race
        detector: a large gold placement overlay and a dark foreground click
        strip must coexist.  Without both visual proofs we abstain, since a
        character/story page can also be portrait-oriented.
        """

        if str(perception.get("screen")) != "portrait_ui":
            return None
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        rank = hsv[
            int(height * 0.38) : int(height * 0.74),
            int(width * 0.08) : int(width * 0.92),
        ]
        click_strip = hsv[
            int(height * 0.74) : int(height * 0.90),
            int(width * 0.04) : int(width * 0.96),
        ]
        if rank.size == 0 or click_strip.size == 0:
            return None
        gold_ratio = float(
            np.mean(
                (rank[:, :, 0] <= 40)
                & (rank[:, :, 1] > 80)
                & (rank[:, :, 2] > 120)
            )
        )
        dark_ratio = float(np.mean(click_strip[:, :, 2] < 100))
        bright_ratio = float(np.mean(click_strip[:, :, 2] > 180))
        if gold_ratio < 0.28 or dark_ratio < 0.50 or bright_ratio > 0.25:
            # The compact award uses a smaller foreground badge. Its narrow
            # center-rank and lower click-strip geometry is sufficient even
            # when the lightweight perception pass has no OCR text attached.
            if RaceRoutePlanner._compact_gold_result_layout(perception, img):
                return int(width * 0.50), int(height * 0.83)
            # WBC uses a second result layout: the large gold placement is
            # higher on the page and the lower half is a bright, white-backed
            # ranking list rather than the dark translucent advance strip.
            # Keep this proof deliberately narrow so ordinary portrait/story
            # pages do not become implicit taps.
            upper = hsv[
                int(height * 0.12) : int(height * 0.52),
                int(width * 0.08) : int(width * 0.92),
            ]
            result_list = hsv[
                int(height * 0.56) : int(height * 0.92),
                int(width * 0.04) : int(width * 0.96),
            ]
            if upper.size == 0 or result_list.size == 0:
                return None
            upper_gold_ratio = float(
                np.mean(
                    (upper[:, :, 0] <= 40)
                    & (upper[:, :, 1] > 80)
                    & (upper[:, :, 2] > 120)
                )
            )
            result_list_white_ratio = float(
                np.mean(
                    (result_list[:, :, 1] < 70)
                    & (result_list[:, :, 2] > 180)
                )
            )
            if upper_gold_ratio < 0.08 or result_list_white_ratio < 0.72:
                return None
        return int(width * 0.50), int(height * 0.83)

    @staticmethod
    def _compact_brown_result_overlay(perception: dict[str, Any], img: np.ndarray,
                                      *, result_owned: bool = False) -> dict[str, Any] | None:
        """Overlay navigation proof only; never a race-result ledger receipt."""
        if img.shape[:2] != (1280, 720) or perception.get("screen") != "portrait_ui":
            return None
        text = "".join(str(perception.get("screen_text", {}).get("normalized_text", "")).split())
        caption = re.fullmatch(r"(?:第([2-9]|[1-9][0-9]+)名|([2-9]|[1-9][0-9]+)第名)点击", text)
        if caption is None and not result_owned:
            return None
        if RaceRoutePlanner._bottom_center_green_button(perception, img, min_y_ratio=.72) is not None:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        strip = hsv[int(1280*.80):int(1280*.855), int(720*.04):int(720*.96)]
        dark, bright = float(np.mean(strip[:, :, 2] < 100)), float(np.mean(strip[:, :, 2] > 180))
        if dark < .50 or bright > .25:
            return None
        # Same local ROI and side-anchor bounds as the existing compact rank reader.
        rank = hsv[720:905, 205:505]
        brown = ((rank[:, :, 0] >= 5) & (rank[:, :, 0] <= 30) & (rank[:, :, 1] > 80)
                 & (rank[:, :, 2] >= 80) & (rank[:, :, 2] <= 210)).astype(np.uint8)
        white = (rank[:, :, 1] < 50) & (rank[:, :, 2] > 215)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(brown, 8)
        left, right = [], []
        for i in range(1, count):
            x, y, width, height, area = map(int, stats[i])
            if not (50 <= height <= 110 and area >= .25 * width * height):
                continue
            if x + width <= 120: left.append(i)
            if x >= 180 and x + width <= 300: right.append(i)
        if len(left) != 1 or len(right) != 1:
            return None
        lx, ly, lw, lh, _ = map(int, stats[left[0]])
        rx, ry, rw, rh, _ = map(int, stats[right[0]])
        if min(ly+lh, ry+rh) - max(ly, ry) < .70 * min(lh, rh) or not 30 <= rx-(lx+lw) <= 160:
            return None
        digits = []
        for i in range(1, count):
            x, y, width, height, area = map(int, stats[i])
            if (x >= lx+lw and x+width <= rx and 90 <= height <= 160
                and y <= min(ly, ry) - .25*height
                and abs(y+height - max(ly+lh, ry+rh)) <= .20*min(lh, rh)
                and area >= .25*width*height):
                digits.append(i)
        if not digits or (not result_owned and len(digits) != len(caption.group(1) or caption.group(2))):
            return None
        outlines = []
        for i in left + digits + right:
            component = (labels == i).astype(np.uint8)
            ring = (cv2.dilate(component, np.ones((3, 3), np.uint8)) > 0) & (component == 0)
            ratio = float(np.mean(white[ring]))
            if ratio < .45:
                return None
            outlines.append(ratio)
        return {"source": "compact_brown_overlay.local_components_and_strip", "caption": text,
                "region_roi": [205, 720, 505, 905],
                "component_bboxes": [list(map(int, stats[i][:4])) for i in left + digits + right],
                "white_outline_ratios": outlines, "strip_dark": dark, "strip_bright": bright}

    @staticmethod
    def _pending_rank_overlay_point(img: np.ndarray, text: str,
                                    *, result_owned: bool = False) -> tuple[int, int] | None:
        """Result-owned rank badge; caption order varies with the animated digit.

        This only advances an already requested result, not a rank ledger read.
        Do not require the blinking click strip or the background page label.
        """
        if img.shape[0] <= img.shape[1]:
            return None
        caption = re.sub(r"[\s!！]", "", text).replace("点击", "").strip(".,，。")
        if ((not result_owned and not any(anchor in caption for anchor in ("第", "名")))
                or re.sub(r"第|名|\d", "", caption)):
            return None
        height, width = img.shape[:2]
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        badge = hsv[int(height*.52):int(height*.72), int(width*.28):int(width*.72)]
        gold = (badge[:,:,0] <= 40) & (badge[:,:,1] > 80) & (badge[:,:,2] > 120)
        if float(gold.mean()) < .28:
            return None
        return int(width*.5), int(height*.83)

    @staticmethod
    def _compact_gold_result_layout(perception: dict[str, Any], img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if str(perception.get("screen")) != "portrait_ui" or width > height:
            return False
        if RaceRoutePlanner._bottom_center_green_button(perception, img, min_y_ratio=0.72) is not None:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        rank = hsv[int(height * 0.52):int(height * 0.72), int(width * 0.28):int(width * 0.72)]
        strip = hsv[int(height * 0.80):int(height * 0.855), int(width * 0.04):int(width * 0.96)]
        if not rank.size or not strip.size:
            return False
        gold = float(np.mean((rank[:, :, 0] <= 40) & (rank[:, :, 1] > 80) & (rank[:, :, 2] > 120)))
        dark = float(np.mean(strip[:, :, 2] < 100))
        bright = float(np.mean(strip[:, :, 2] > 180))
        return gold >= 0.28 and dark >= 0.50 and bright <= 0.27

    def _read_compact_result_rank(self, path: Path, img: np.ndarray) -> dict[str, Any] | None:
        # This is an overlay continuation proof, not a race-result ledger receipt.
        # Reuse the existing region reader; construct rec-only PP lazily once.
        if img.shape[:2] != (1280, 720):
            return None
        from core.auto_trainer.race_result_rank_reader import PpOcrV5NumericReader, _parse_regions, _recognize

        try:
            encoded = path.read_bytes()
            source_sha = hashlib.sha256(encoded).hexdigest()
            if not np.array_equal(img, cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)):
                return None
            crop = cv2.resize(img[720:905, 205:505], None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            raw = self._fan_shortfall_regions_reader.read_regions(crop)
            regions = _parse_regions(raw)
            if len(regions) != 2 or {r.text.strip() for r in regions} != {"第", "名"}:
                return None
            left = next(r for r in regions if r.text.strip() == "第")
            right = next(r for r in regions if r.text.strip() == "名")
            lx1, ly1, lx2, ly2 = left.bounds
            rx1, ry1, rx2, ry2 = right.bounds
            if not (
                min(left.confidence, right.confidence) >= 0.90
                and 0 <= lx1 < lx2 <= 240 and 360 <= rx1 < rx2 <= 600
                and 0 <= ly1 < ly2 <= 370 and 0 <= ry1 < ry2 <= 370
                and 100 <= ly2 - ly1 <= 220 and 100 <= ry2 - ry1 <= 220
                and min(ly2, ry2) - max(ly1, ry1) >= 0.70 * min(ly2 - ly1, ry2 - ry1)
            ):
                return None
            # Full space between both real anchors, not a one-digit crop.
            x1 = int(np.floor(lx2 / 2 + 205)) - 8
            x2 = int(np.ceil(rx1 / 2 + 205)) + 8
            if not 30 <= x2 - x1 <= 160:
                return None
            if getattr(self, "_compact_rank_engine", None) is None:
                self._compact_rank_engine = PpOcrV5NumericReader._build_gain_engine()
            text, score = _recognize(self._compact_rank_engine, img[720:905, x1:x2].copy())
            if score < 0.90 or re.fullmatch(r"[1-9][0-9]*", text.strip()) is None:
                return None
            if hashlib.sha256(path.read_bytes()).hexdigest() != source_sha:
                return None
            return {"source": "compact_gold_overlay.local_rank", "source_image_sha256": source_sha,
                    "rank": int(text.strip()), "rank_text": text, "rank_confidence": score,
                    "confidence": min(left.confidence, right.confidence, score),
                    "region_roi": [205, 720, 505, 905], "region_scale": 2, "raw_regions": raw,
                    "rank_roi": [x1, 720, x2, 905]}
        except Exception:
            return None

    @staticmethod
    def _looks_like_center_race_detail_action_layout(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        left = hsv[int(height * 0.81) : int(height * 0.89), int(width * 0.02) : int(width * 0.30)]
        center = hsv[int(height * 0.81) : int(height * 0.89), int(width * 0.30) : int(width * 0.70)]
        right = hsv[int(height * 0.81) : int(height * 0.89), int(width * 0.70) : int(width * 0.98)]
        if left.size == 0 or center.size == 0 or right.size == 0:
            return False
        left_green = np.mean(
            (left[:, :, 0] >= 38)
            & (left[:, :, 0] <= 92)
            & (left[:, :, 1] > 70)
            & (left[:, :, 2] > 80)
        )
        left_white = np.mean((left[:, :, 1] < 70) & (left[:, :, 2] > 170))
        center_green = np.mean(
            (center[:, :, 0] >= 38)
            & (center[:, :, 0] <= 92)
            & (center[:, :, 1] > 70)
            & (center[:, :, 2] > 80)
        )
        right_gray = np.mean((right[:, :, 1] < 60) & (right[:, :, 2] > 80) & (right[:, :, 2] < 220))
        return bool(left_white > 0.55 and left_green < 0.10 and center_green > 0.45 and right_gray > 0.30)

    @staticmethod
    def _looks_like_portrait_race_title(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        if str(perception.get("screen")) != "unknown":
            return False
        if perception.get("buttons"):
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        controls = hsv[int(height * 0.90) : height, int(width * 0.52) : width]
        if controls.size == 0:
            return False
        title = hsv[
            int(height * 0.32) : int(height * 0.62),
            int(width * 0.15) : int(width * 0.85),
        ]
        white = np.mean((controls[:, :, 1] < 60) & (controls[:, :, 2] > 175))
        orange = np.mean(
            (controls[:, :, 0] >= 5)
            & (controls[:, :, 0] <= 30)
            & (controls[:, :, 1] > 60)
            & (controls[:, :, 2] > 90)
        )
        blue = np.mean(
            (title[:, :, 0] >= 85)
            & (title[:, :, 0] <= 125)
            & (title[:, :, 1] > 45)
            & (title[:, :, 2] > 80)
        )
        return bool(white > 0.08 and orange > 0.01 and blue > 0.12)

    @staticmethod
    def _portrait_race_title_skip_button(img: np.ndarray) -> tuple[int, int]:
        height, width = img.shape[:2]
        return int(width * 0.775), int(height * 0.96)

    @staticmethod
    def _looks_like_portrait_race_intro_controls(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        if str(perception.get("screen")) != "portrait_ui":
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        lower = hsv[int(height * 0.80) : height, :]
        controls = hsv[int(height * 0.88) : int(height * 0.99), int(width * 0.55) : int(width * 0.99)]
        if lower.size == 0 or controls.size == 0:
            return False
        lower_green = np.mean(
            (lower[:, :, 0] >= 38)
            & (lower[:, :, 0] <= 92)
            & (lower[:, :, 1] > 70)
            & (lower[:, :, 2] > 80)
        )
        controls_white = np.mean((controls[:, :, 1] < 65) & (controls[:, :, 2] > 170))
        controls_orange = np.mean(
            (controls[:, :, 0] >= 5)
            & (controls[:, :, 0] <= 30)
            & (controls[:, :, 1] > 60)
            & (controls[:, :, 2] > 90)
        )
        return bool(lower_green > 0.45 and controls_white > 0.18 and controls_orange > 0.04)

    @staticmethod
    def _looks_like_portrait_race_scene_controls(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> bool:
        if RaceRoutePlanner._looks_like_portrait_race_scene_bottom_controls(perception, img):
            return True
        height, width = img.shape[:2]
        if width > height:
            return False
        if str(perception.get("screen")) != "portrait_ui":
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        top = hsv[0 : int(height * 0.20), :]
        controls = hsv[int(height * 0.90) : int(height * 0.995), int(width * 0.55) : int(width * 0.99)]
        if top.size == 0 or controls.size == 0:
            return False
        top_saturated = np.mean((top[:, :, 1] > 80) & (top[:, :, 2] > 100))
        controls_white = np.mean((controls[:, :, 1] < 65) & (controls[:, :, 2] > 170))
        controls_orange = np.mean(
            (controls[:, :, 0] >= 5)
            & (controls[:, :, 0] <= 30)
            & (controls[:, :, 1] > 60)
            & (controls[:, :, 2] > 90)
        )
        controls_green = np.mean(
            (controls[:, :, 0] >= 38)
            & (controls[:, :, 0] <= 92)
            & (controls[:, :, 1] > 70)
            & (controls[:, :, 2] > 80)
        )
        return bool(
            top_saturated < 0.08
            and 0.18 < controls_white < 0.45
            and controls_orange > 0.04
            and controls_green < 0.12
        )

    @staticmethod
    def _looks_like_portrait_race_scene_bottom_controls(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        if str(perception.get("screen")) not in {"portrait_ui", "modal_confirm", "unknown", "bottom_action"}:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        top = hsv[0 : int(height * 0.20), :]
        controls = hsv[int(height * 0.90) : int(height * 0.995), int(width * 0.55) : int(width * 0.99)]
        if top.size == 0 or controls.size == 0:
            return False
        top_saturated = np.mean((top[:, :, 1] > 80) & (top[:, :, 2] > 100))
        top_track_green = np.mean(
            (top[:, :, 0] >= 35)
            & (top[:, :, 0] <= 95)
            & (top[:, :, 1] > 35)
            & (top[:, :, 2] > 40)
        )
        controls_white = np.mean((controls[:, :, 1] < 65) & (controls[:, :, 2] > 170))
        controls_orange = np.mean(
            (controls[:, :, 0] >= 5)
            & (controls[:, :, 0] <= 30)
            & (controls[:, :, 1] > 60)
            & (controls[:, :, 2] > 90)
        )
        controls_green = np.mean(
            (controls[:, :, 0] >= 38)
            & (controls[:, :, 0] <= 92)
            & (controls[:, :, 1] > 70)
            & (controls[:, :, 2] > 80)
        )
        bottom_action_race_controls = (
            str(perception.get("screen")) == "bottom_action"
            and controls_white > 0.14
            and controls_orange > 0.10
            and controls_green < 0.03
        )
        race_scene_top = top_saturated < 0.08 or top_track_green > 0.45
        return bool(
            (race_scene_top and 0.18 < controls_white < 0.45 and controls_orange > 0.04)
            or (controls_white > 0.22 and controls_orange > 0.18 and controls_green < 0.10)
            or bottom_action_race_controls
        )

    @staticmethod
    def _looks_like_gold_race_transition_controls(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        if str(perception.get("screen")) not in {"event_choice", "unknown", "portrait_ui", "bottom_action"}:
            return False
        if perception.get("buttons"):
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        top_left = hsv[0 : int(height * 0.18), 0 : int(width * 0.25)]
        scan = hsv[int(height * 0.20) : int(height * 0.88), :]
        controls = hsv[int(height * 0.88) : int(height * 0.995), int(width * 0.55) : int(width * 0.99)]
        if top_left.size == 0 or scan.size == 0 or controls.size == 0:
            return False
        top_left_red = np.mean(
            ((top_left[:, :, 0] <= 8) | (top_left[:, :, 0] >= 170))
            & (top_left[:, :, 1] > 70)
            & (top_left[:, :, 2] > 100)
        )
        scan_orange = np.mean(
            (scan[:, :, 0] >= 5)
            & (scan[:, :, 0] <= 30)
            & (scan[:, :, 1] > 30)
            & (scan[:, :, 2] > 80)
        )
        scan_white = np.mean((scan[:, :, 1] < 60) & (scan[:, :, 2] > 175))
        scan_green = np.mean(
            (scan[:, :, 0] >= 38)
            & (scan[:, :, 0] <= 92)
            & (scan[:, :, 1] > 70)
            & (scan[:, :, 2] > 80)
        )
        controls_white = np.mean((controls[:, :, 1] < 65) & (controls[:, :, 2] > 170))
        controls_orange = np.mean(
            (controls[:, :, 0] >= 5)
            & (controls[:, :, 0] <= 30)
            & (controls[:, :, 1] > 60)
            & (controls[:, :, 2] > 90)
        )
        controls_green = np.mean(
            (controls[:, :, 0] >= 38)
            & (controls[:, :, 0] <= 92)
            & (controls[:, :, 1] > 70)
            & (controls[:, :, 2] > 80)
        )
        return bool(
            top_left_red < 0.04
            and scan_orange > 0.82
            and scan_white < 0.04
            and scan_green < 0.03
            and controls_white > 0.16
            and controls_orange > 0.18
            and controls_green < 0.08
        )

    @staticmethod
    def _looks_like_post_race_victory_controls(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        if str(perception.get("screen")) not in {"portrait_ui", "bottom_action", "unknown"}:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        controls = hsv[int(height * 0.90) : int(height * 0.995), int(width * 0.55) : int(width * 0.99)]
        rank_fx = hsv[int(height * 0.45) : int(height * 0.85), 0 : int(width * 0.58)]
        if controls.size == 0 or rank_fx.size == 0:
            return False
        controls_white = np.mean((controls[:, :, 1] < 65) & (controls[:, :, 2] > 170))
        controls_orange = np.mean(
            (controls[:, :, 0] >= 5)
            & (controls[:, :, 0] <= 30)
            & (controls[:, :, 1] > 60)
            & (controls[:, :, 2] > 90)
        )
        controls_green = np.mean(
            (controls[:, :, 0] >= 38)
            & (controls[:, :, 0] <= 92)
            & (controls[:, :, 1] > 70)
            & (controls[:, :, 2] > 80)
        )
        gold_rank = np.mean(
            (rank_fx[:, :, 0] >= 12)
            & (rank_fx[:, :, 0] <= 38)
            & (rank_fx[:, :, 1] > 60)
            & (rank_fx[:, :, 2] > 120)
        )
        return bool(
            controls_white > 0.20
            and controls_orange > 0.10
            and controls_green < 0.14
            and gold_rank > 0.12
        )

    @staticmethod
    def _looks_like_training_story_dialogue(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        dialogue = hsv[
            int(height * 0.70) : int(height * 0.90),
            int(width * 0.02) : int(width * 0.98),
        ]
        bottom = hsv[int(height * 0.88) : height, :]
        top_left = hsv[0 : int(height * 0.20), 0 : int(width * 0.30)]
        if dialogue.size == 0 or bottom.size == 0 or top_left.size == 0:
            return False
        dialogue_white = np.mean(
            (dialogue[:, :, 1] < 65) & (dialogue[:, :, 2] > 170)
        )
        bottom_white = np.mean((bottom[:, :, 1] < 65) & (bottom[:, :, 2] > 170))
        hud_red = np.mean(
            ((top_left[:, :, 0] <= 8) | (top_left[:, :, 0] >= 170))
            & (top_left[:, :, 1] > 70)
            & (top_left[:, :, 2] > 120)
        )
        return bool(dialogue_white > 0.48 and bottom_white < 0.35 and hud_red > 0.08)

    @staticmethod
    def _portrait_race_intro_skip_button(img: np.ndarray) -> tuple[int, int]:
        height, width = img.shape[:2]
        return int(width * 0.77), int(height * 0.94)

    @staticmethod
    def _looks_like_portrait_race_scene(
        perception: dict[str, Any],
        img: np.ndarray,
        policy: RaceRoutePolicy,
    ) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        # This detector only returns a wait. Keep the prior authorized
        # live observation without enabling any scene tap or confirmation.
        if not (policy.allow_skip_race_scene or policy._confirmed_live_ordinary_route):
            return False
        screen = str(perception.get("screen"))
        if screen == "unknown" and not perception.get("buttons"):
            return True
        if screen not in {"portrait_ui", "bottom_action", "unknown"}:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        middle = hsv[int(height * 0.18) : int(height * 0.85), :]
        lower = hsv[int(height * 0.65) : int(height * 0.95), :]
        if middle.size == 0 or lower.size == 0:
            return False
        middle_green = np.mean(
            (middle[:, :, 0] >= 35)
            & (middle[:, :, 0] <= 95)
            & (middle[:, :, 1] > 35)
            & (middle[:, :, 2] > 40)
        )
        middle_white = np.mean((middle[:, :, 1] < 65) & (middle[:, :, 2] > 170))
        lower_green = np.mean(
            (lower[:, :, 0] >= 35)
            & (lower[:, :, 0] <= 95)
            & (lower[:, :, 1] > 35)
            & (lower[:, :, 2] > 40)
        )
        lower_white = np.mean((lower[:, :, 1] < 65) & (lower[:, :, 2] > 170))
        return bool(
            middle_green > 0.45
            and middle_white < 0.12
            and lower_green > 0.55
            and lower_white < 0.12
        )

    @staticmethod
    def _looks_like_portrait_race_start(
        perception: dict[str, Any],
        img: np.ndarray,
        policy: RaceRoutePolicy,
    ) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        if str(perception.get("screen")) != "portrait_ui":
            return False
        if not policy.allow_skip_race_scene:
            return False
        if RaceRoutePlanner._bottom_center_green_region(img, min_y_ratio=0.86) is None:
            return False
        crop = img[int(height * 0.86) : int(height * 0.98), int(width * 0.18) : int(width * 0.82)]
        if crop.size == 0:
            return False
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        green = np.mean(
            (hsv[:, :, 0] >= 38)
            & (hsv[:, :, 0] <= 92)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 80)
        )
        bright = np.mean(hsv[:, :, 2] > 170)
        dark = np.mean(hsv[:, :, 2] < 80)
        return bool(green > 0.25 and bright > 0.15 and dark < 0.03)

    @staticmethod
    def _looks_like_training_home_ready_for_race_entry(
        perception: dict[str, Any],
        img: np.ndarray,
        safe_planner: SafeAdvancePlanner,
    ) -> bool:
        story_dialogue = safe_planner._looks_like_training_story_dialogue(img)
        if story_dialogue and img.shape[:2] == (1280, 720):
            from core.main_menu_state import _detect_main_menu

            # Idle speech can hit the broad white-panel test; require every
            # existing home anchor, while retaining all foreground vetoes below.
            anchors = _detect_main_menu(img)["evidence"]["anchors"]
            story_dialogue = not all(anchor["passed"] for anchor in anchors)
        return bool(
            safe_planner._looks_like_training_home_controls(img)
            and safe_planner._go_to_race_modal_button(perception, img) is None
            and safe_planner._safe_green_confirm_button(perception, img) is None
            and not safe_planner._looks_like_confirm_modal_panel(img)
            and not story_dialogue
            and not safe_planner._looks_like_blocking_bottom_dialogue(img)
        )

    @staticmethod
    def _bottom_center_green_region(
        img: np.ndarray,
        *,
        min_y_ratio: float,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        x0, x1 = int(width * 0.18), int(width * 0.82)
        y0, y1 = int(height * min_y_ratio), int(height * 0.98)
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            return None
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(
            hsv,
            np.array([38, 70, 80], dtype=np.uint8),
            np.array([92, 255, 255], dtype=np.uint8),
        )
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates: list[tuple[int, int, int]] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = int(cv2.contourArea(contour))
            if area < width * height * 0.002:
                continue
            cx = x0 + x + w // 2
            cy = y0 + y + h // 2
            if not (width * 0.24 <= cx <= width * 0.76):
                continue
            candidates.append((area, int(cx), int(cy)))
        if not candidates:
            return None
        _, cx, cy = max(candidates, key=lambda item: item[0])
        return cx, cy

    @staticmethod
    def _looks_like_blue_white_transition(img: np.ndarray) -> bool:
        return bool(blue_white_transition_visible(img))

    @staticmethod
    def _looks_like_loading(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        white = np.mean((hsv[:, :, 1] < 45) & (hsv[:, :, 2] > 210))
        lower = hsv[int(height * 0.70) : height, :]
        saturated_lower = np.mean((lower[:, :, 1] > 80) & (lower[:, :, 2] > 100))
        return bool(white > 0.62 and saturated_lower < 0.08)

    @staticmethod
    def _looks_like_race_detail_loading(img: np.ndarray) -> bool:
        """Recognize the transient race-detail overlay before its result buttons render."""

        height, width = img.shape[:2]
        if (width, height) not in {(720, 1280), (1440, 2560)}:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        def crop(x0: float, x1: float, y0: float, y1: float) -> np.ndarray:
            return hsv[int(height * y0) : int(height * y1), int(width * x0) : int(width * x1)]

        title = crop(0.32, 0.68, 0.02, 0.07)
        tabs = crop(0.50, 0.98, 0.10, 0.15)
        attributes = crop(0.52, 0.98, 0.16, 0.42)
        loading = crop(0.67, 0.88, 0.91, 0.99)
        if any(region.size == 0 for region in (title, tabs, attributes, loading)):
            return False

        def ratio(region: np.ndarray, mask: np.ndarray) -> float:
            return float(np.mean(mask))

        title_white = ratio(title, (title[:, :, 1] < 55) & (title[:, :, 2] > 175))
        title_pink = ratio(
            title,
            (title[:, :, 0] >= 155) & (title[:, :, 0] <= 179) & (title[:, :, 1] > 35) & (title[:, :, 2] > 85),
        )
        tab_gold = ratio(
            tabs,
            (tabs[:, :, 0] >= 15) & (tabs[:, :, 0] <= 45) & (tabs[:, :, 1] > 65) & (tabs[:, :, 2] > 80),
        )
        tab_pink = ratio(
            tabs,
            (tabs[:, :, 0] >= 155) & (tabs[:, :, 0] <= 179) & (tabs[:, :, 1] > 35) & (tabs[:, :, 2] > 85),
        )
        tab_green = ratio(
            tabs,
            (tabs[:, :, 0] >= 35) & (tabs[:, :, 0] <= 90) & (tabs[:, :, 1] > 60) & (tabs[:, :, 2] > 90),
        )
        tab_white = ratio(tabs, (tabs[:, :, 1] < 55) & (tabs[:, :, 2] > 175))
        attributes_dark = ratio(attributes, attributes[:, :, 2] < 105)
        attributes_dark_135 = ratio(attributes, attributes[:, :, 2] < 135)
        attributes_pink = ratio(
            attributes,
            (attributes[:, :, 0] >= 155)
            & (attributes[:, :, 0] <= 179)
            & (attributes[:, :, 1] > 35)
            & (attributes[:, :, 2] > 85),
        )
        loading_background = ratio(
            loading,
            (loading[:, :, 0] >= 45)
            & (loading[:, :, 0] <= 90)
            & (loading[:, :, 1] > 60)
            & (loading[:, :, 2] > 60)
            & (loading[:, :, 2] < 180),
        )
        loading_glyphs = ratio(loading, (loading[:, :, 1] < 55) & (loading[:, :, 2] > 175))
        g1_loading = (
            title_white > 0.45
            and 0.20 < title_pink < 0.45
            and tab_gold > 0.20
            and tab_pink > 0.45
            and attributes_dark > 0.45
            and attributes_pink > 0.45
            and loading_background > 0.65
            and loading_glyphs > 0.06
        )
        debut_loading = (
            tab_green > 0.18
            and tab_white > 0.18
            and attributes_dark_135 > 0.55
            and loading_background > 0.65
            and loading_glyphs > 0.04
            and RaceClassifierMixin._has_pre_race_commentary_menu_icon(img)
        )
        return bool(g1_loading or debut_loading)

    @staticmethod
    def _looks_like_transition_wipe(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> bool:
        if perception.get('buttons'):
            return False
        return bool(orange_wipe_visible(img))
