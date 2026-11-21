"""Shadow-only support portrait counts from the fixed training-page right lane."""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np


SCHEMA_VERSION = "support_right_lane_count_shadow.v1"
CANONICAL_WIDTH = 720
CANONICAL_HEIGHT = 1280
RIGHT_LANE_BBOX = (590, 170, 720, 720)


@dataclass(frozen=True)
class _CircleCandidate:
    cx: int
    cy: int
    radius: int


class SupportRightLaneRecognizer:
    """Read count-level support evidence without using support identity."""

    def recognize(self, image_path: str | Path) -> dict[str, Any]:
        path = Path(image_path)
        image = cv2.imread(str(path))
        if image is None:
            return self._abstain("unable_to_read_image")
        return self.recognize_array(image)

    def recognize_array(self, image: np.ndarray) -> dict[str, Any]:
        normalized = self._normalize_frame(image)
        if normalized is None:
            return self._abstain("unsupported_frame_size")
        candidates = self._detect_circle_candidates(normalized)
        slots: list[dict[str, Any]] = []
        for candidate in candidates:
            evidence = self._occupancy_evidence(normalized, candidate)
            hint = self._detect_slot_hint(normalized, candidate)
            has_support_anchor = any(
                evidence[key]
                for key in ("training_type_icon", "bond_bar_or_max", "strict_hint")
            )
            if not (
                evidence["circle_edge"]
                and evidence["interior_texture"]
                and has_support_anchor
            ):
                continue
            slots.append(
                {
                    "slot": len(slots),
                    "occupancy_status": "accepted",
                    "occupancy_reason": "fixed_lane_independent_visual_evidence",
                    "circle": [candidate.cx, candidate.cy, candidate.radius],
                    "evidence": evidence,
                    "hint": hint,
                    "bond_bar": None,
                }
            )
        hint_slots = [
            slot["slot"]
            for slot in slots
            if slot["hint"]["status"] == "accepted"
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "accepted",
            "reason": "fixed_right_lane_evaluated",
            "visible_portrait_count": len(slots),
            "verified_red_exclamation_count": len(hint_slots),
            "hint_slots": hint_slots,
            "slots": slots,
            "production_ready": False,
        }

    @staticmethod
    def _detect_circle_candidates(image: np.ndarray) -> list[_CircleCandidate]:
        x1, y1, x2, y2 = RIGHT_LANE_BBOX
        lane = image[y1:y2, x1:x2]
        gray = cv2.medianBlur(cv2.cvtColor(lane, cv2.COLOR_BGR2GRAY), 5)
        detected = cv2.HoughCircles(
            gray,
            cv2.HOUGH_GRADIENT,
            dp=1.2,
            minDist=80,
            param1=80,
            param2=20,
            minRadius=24,
            maxRadius=42,
        )
        if detected is None:
            return []
        raw_candidates: list[_CircleCandidate] = []
        for cx, cy, radius in np.round(detected[0]).astype(int):
            if cy < 20 or cy > 500:
                continue
            source_cx = int(cx + x1)
            if source_cx + int(radius) > x2 - 15:
                continue
            raw_candidates.append(
                _CircleCandidate(
                    cx=source_cx,
                    cy=int(cy + y1),
                    radius=int(radius),
                )
            )
        kept: list[_CircleCandidate] = []
        for candidate in sorted(raw_candidates, key=lambda item: (item.cy, -item.radius)):
            if any(abs(candidate.cy - previous.cy) < 80 for previous in kept):
                continue
            kept.append(candidate)
        return kept

    @staticmethod
    def _occupancy_evidence(
        image: np.ndarray,
        candidate: _CircleCandidate,
    ) -> dict[str, bool]:
        radius = candidate.radius
        x1 = max(0, candidate.cx - radius - 4)
        y1 = max(0, candidate.cy - radius - 4)
        x2 = min(image.shape[1], candidate.cx + radius + 5)
        y2 = min(image.shape[0], candidate.cy + radius + 5)
        patch = image[y1:y2, x1:x2]
        if patch.size == 0:
            return {
                "circle_edge": False,
                "interior_texture": False,
                "training_type_icon": False,
                "bond_bar_or_max": False,
                "strict_hint": False,
            }
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        yy, xx = np.ogrid[: gray.shape[0], : gray.shape[1]]
        local_cx = candidate.cx - x1
        local_cy = candidate.cy - y1
        distance = np.sqrt((xx - local_cx) ** 2 + (yy - local_cy) ** 2)
        annulus = (distance >= radius * 0.76) & (distance <= radius * 1.12)
        interior = distance <= radius * 0.70
        edges = cv2.Canny(gray, 60, 140) > 0
        annulus_size = max(1, int(np.count_nonzero(annulus)))
        edge_density = float(np.count_nonzero(edges & annulus)) / annulus_size
        texture = float(np.std(gray[interior])) if np.count_nonzero(interior) else 0.0
        training_type_icon = SupportRightLaneRecognizer._detect_training_type_icon(
            image,
            candidate,
        )
        bond_bar_or_max = SupportRightLaneRecognizer._detect_bond_bar_or_max(
            image,
            candidate,
        )
        hint = SupportRightLaneRecognizer._detect_slot_hint(image, candidate)
        return {
            "circle_edge": 0.025 <= edge_density <= 0.35,
            "interior_texture": texture >= 18.0,
            "training_type_icon": training_type_icon,
            "bond_bar_or_max": bond_bar_or_max,
            "strict_hint": hint["status"] == "accepted",
        }

    @staticmethod
    def _detect_slot_hint(
        image: np.ndarray,
        candidate: _CircleCandidate,
    ) -> dict[str, Any]:
        radius = candidate.radius
        x1 = max(0, candidate.cx + int(0.20 * radius))
        x2 = min(image.shape[1], candidate.cx + int(1.75 * radius))
        y1 = max(0, candidate.cy - int(1.95 * radius))
        y2 = min(image.shape[0], candidate.cy - int(0.25 * radius))
        if x2 <= x1 or y2 <= y1:
            return {"status": "rejected", "reason": "slot_hint_window_empty"}
        patch = image[y1:y2, x1:x2]
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        white = (
            ((hsv[:, :, 1] <= 70) & (hsv[:, :, 2] >= 185)).astype(np.uint8)
            * 255
        )
        count, _, stats, _ = cv2.connectedComponentsWithStats(white, 8)
        stems: list[tuple[int, int, int, int]] = []
        dots: list[tuple[int, int, int, int]] = []
        for index in range(1, count):
            component_x, component_y, width, height, area = [
                int(value) for value in stats[index]
            ]
            if (
                6 <= width <= 14
                and 13 <= height <= 24
                and height >= width * 1.30
                and area >= 50
            ):
                stems.append((component_x, component_y, width, height))
            elif (
                4 <= width <= 10
                and 4 <= height <= 10
                and area >= 15
            ):
                dots.append((component_x, component_y, width, height))

        for stem_x, stem_y, stem_width, stem_height in stems:
            for dot_x, dot_y, dot_width, dot_height in dots:
                stem_center = stem_x + (stem_width / 2.0)
                dot_center = dot_x + (dot_width / 2.0)
                if abs(stem_center - dot_center) > 5:
                    continue
                if not (
                    stem_y + stem_height - 2
                    <= dot_y
                    <= stem_y + stem_height + 8
                ):
                    continue
                envelope_x1 = max(0, min(stem_x, dot_x) - 8)
                envelope_y1 = max(0, stem_y - 8)
                envelope_x2 = min(
                    patch.shape[1],
                    max(stem_x + stem_width, dot_x + dot_width) + 8,
                )
                envelope_y2 = min(patch.shape[0], dot_y + dot_height + 8)
                envelope = hsv[
                    envelope_y1:envelope_y2,
                    envelope_x1:envelope_x2,
                ]
                red = (
                    ((envelope[:, :, 0] <= 25) | (envelope[:, :, 0] >= 145))
                    & (envelope[:, :, 1] >= 90)
                    & (envelope[:, :, 2] >= 90)
                )
                red_ratio = float(np.count_nonzero(red)) / max(1, red.size)
                white_left = min(stem_x, dot_x) - envelope_x1
                white_right = max(
                    stem_x + stem_width,
                    dot_x + dot_width,
                ) - envelope_x1
                left_red = int(np.count_nonzero(red[:, :max(1, white_left)]))
                right_red = int(
                    np.count_nonzero(red[:, min(red.shape[1] - 1, white_right):])
                )
                if red_ratio < 0.40 or left_red < 10 or right_red < 10:
                    continue
                return {
                    "status": "accepted",
                    "reason": "slot_local_red_shell_white_stem_dot",
                    "bbox": [
                        x1 + envelope_x1,
                        y1 + envelope_y1,
                        x1 + envelope_x2,
                        y1 + envelope_y2,
                    ],
                    "red_surround_ratio": round(red_ratio, 4),
                }
        return {"status": "rejected", "reason": "slot_hint_topology_missing"}

    @staticmethod
    def _detect_training_type_icon(
        image: np.ndarray,
        candidate: _CircleCandidate,
    ) -> bool:
        radius = candidate.radius
        x1 = max(0, candidate.cx - int(1.65 * radius))
        x2 = min(image.shape[1], candidate.cx - int(0.15 * radius))
        y1 = max(0, candidate.cy - int(1.55 * radius))
        y2 = min(image.shape[0], candidate.cy - int(0.15 * radius))
        if x2 <= x1 or y2 <= y1:
            return False
        patch = image[y1:y2, x1:x2]
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        white = (
            ((hsv[:, :, 1] <= 70) & (hsv[:, :, 2] >= 180)).astype(np.uint8)
            * 255
        )
        count, _, stats, _ = cv2.connectedComponentsWithStats(white, 8)
        for index in range(1, count):
            component_x, component_y, width, height, area = [
                int(value) for value in stats[index]
            ]
            if x1 + component_x < RIGHT_LANE_BBOX[0]:
                continue
            standard_glyph = (
                3 <= width <= 22
                and 10 <= height <= 25
                and area >= 80
                and 0.15 <= width / max(float(height), 1.0) <= 2.20
            )
            small_glyph = (
                4 <= width <= 12
                and 5 <= height <= 12
                and area >= 10
                and 0.25 <= width / max(float(height), 1.0) <= 2.40
            )
            if not (standard_glyph or small_glyph):
                continue
            pad = 5
            sx1 = max(0, component_x - pad)
            sy1 = max(0, component_y - pad)
            sx2 = min(patch.shape[1], component_x + width + pad)
            sy2 = min(patch.shape[0], component_y + height + pad)
            surround = hsv[sy1:sy2, sx1:sx2]
            saturated_surround = (
                (surround[:, :, 1] >= 90) & (surround[:, :, 2] >= 80)
            )
            surround_ratio = float(np.count_nonzero(saturated_surround)) / max(
                1,
                surround.shape[0] * surround.shape[1],
            )
            minimum_surround_ratio = 0.80 if small_glyph else 0.25
            if surround_ratio >= minimum_surround_ratio:
                return True
        return False

    @staticmethod
    def _detect_bond_bar_or_max(
        image: np.ndarray,
        candidate: _CircleCandidate,
    ) -> bool:
        radius = candidate.radius
        x1 = max(0, candidate.cx - int(1.20 * radius))
        x2 = min(image.shape[1], candidate.cx + int(1.20 * radius))
        y1 = max(0, candidate.cy + int(0.75 * radius))
        y2 = min(image.shape[0], candidate.cy + int(1.70 * radius))
        if x2 <= x1 or y2 <= y1:
            return False
        patch = image[y1:y2, x1:x2]
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        ui_ink = ((gray < 180).astype(np.uint8) * 255)
        kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (max(9, int(radius * 0.35)), 3),
        )
        connected = cv2.morphologyEx(ui_ink, cv2.MORPH_OPEN, kernel)
        count, _, stats, _ = cv2.connectedComponentsWithStats(connected, 8)
        for index in range(1, count):
            _, _, width, height, area = [int(value) for value in stats[index]]
            if width < int(radius * 1.0) or width > int(radius * 2.6):
                continue
            maximum_height = max(22, int(round(radius * 0.85)))
            if (
                height < 5
                or height > maximum_height
                or area < int(radius * radius * 0.16)
            ):
                continue
            if width / max(float(height), 1.0) >= 2.4:
                return True
        return False

    @staticmethod
    def _normalize_frame(image: np.ndarray) -> np.ndarray | None:
        if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] != 3:
            return None
        height, width = image.shape[:2]
        if (width, height) == (CANONICAL_WIDTH, CANONICAL_HEIGHT):
            return image
        if (width, height) == (CANONICAL_WIDTH * 2, CANONICAL_HEIGHT * 2):
            return cv2.resize(
                image,
                (CANONICAL_WIDTH, CANONICAL_HEIGHT),
                interpolation=cv2.INTER_AREA,
            )
        return None

    @staticmethod
    def _right_lane_bbox() -> tuple[int, int, int, int]:
        return RIGHT_LANE_BBOX

    @staticmethod
    def _abstain(reason: str) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "abstained",
            "reason": reason,
            "visible_portrait_count": None,
            "verified_red_exclamation_count": None,
            "hint_slots": [],
            "slots": [],
            "production_ready": False,
        }
