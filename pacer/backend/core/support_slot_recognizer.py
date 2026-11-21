from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from core.support_bond_bar import measure_bond_bar
from core.support_identity_matcher import SupportIdentityMatcher


REF_W = 1440
REF_H = 2560
ROI_X1 = 1120
ROI_Y1 = 250
ROI_X2 = 1440
ROI_Y2 = 1700


@dataclass
class _CircleCandidate:
    cx: int
    cy: int
    r: int
    band_score: float


@dataclass
class _BarCandidate:
    x1: int
    y1: int
    x2: int
    y2: int
    width: int
    area: int


class SupportSlotRecognizer:
    def __init__(self, identity_matcher: SupportIdentityMatcher | None = None):
        self._identity_matcher = identity_matcher or SupportIdentityMatcher()

    def recognize(self, image_path: str | Path) -> dict[str, Any]:
        path = Path(image_path)
        image = cv2.imread(str(path))
        if image is None:
            raise ValueError(f"unable_to_read_image:{path}")
        return self.recognize_array(image, image_path=path)

    def recognize_array(
        self,
        image: np.ndarray,
        *,
        image_path: str | Path | None = None,
        debug_output_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        if image is None or image.size == 0:
            raise ValueError("empty_image")

        height, width = image.shape[:2]
        if (width, height) == (REF_W, REF_H):
            canonical = cv2.resize(image, (REF_W // 2, REF_H // 2), interpolation=cv2.INTER_AREA)
            result = self.recognize_array(canonical, image_path=image_path)
            native_result = self._recognize_native_resolution(image, image_path=image_path)
            if self._prefer_native_single_slot_result(result, native_result):
                return native_result
            self._reconcile_exact_template_slots(canonical, result, image_path=image_path)
            for slot in result.get("support_slots", []):
                for key in ("bbox", "hint_badge_bbox"):
                    bbox = slot.get(key)
                    if isinstance(bbox, list) and len(bbox) == 4:
                        slot[key] = [int(value * 2) for value in bbox]
                evidence = slot.get("hint_badge_evidence")
                if isinstance(evidence, dict):
                    bbox = evidence.get("bbox")
                    if isinstance(bbox, list) and len(bbox) == 4:
                        evidence["bbox"] = [int(value * 2) for value in bbox]
            result["image"] = str(image_path) if image_path is not None else ""
            result["image_size"] = [width, height]
            result["roi"] = list(self.compute_roi(width, height))
            return result

        roi = self.compute_roi(width, height)
        x1, y1, x2, y2 = roi
        roi_image = image[y1:y2, x1:x2]
        circles = self._detect_avatar_candidates(roi_image)
        bars = self._detect_bond_bars(roi_image)
        slots = self._build_slots(roi_image, circles, bars, roi)

        hint_count = sum(1 for slot in slots if slot["hint_badge"])
        identity_candidates = self._collect_identity_candidates(slots)
        friendship = self._legacy_friendship_from_slots(slots)
        entity_counts = self._summarize_entity_counts(slots)
        result = {
            "image": str(image_path) if image_path is not None else "",
            "image_size": [width, height],
            "roi": list(roi),
            "source": "cv_heuristic",
            "support_slots": slots,
            "support_count": len(slots),
            **entity_counts,
            "hint_count": hint_count,
            "red_dot_count": hint_count,
            "identity_candidates": identity_candidates,
            "support_slot_summary": {
                "count": len(slots),
                **entity_counts,
                "red_dot_count": hint_count,
                "identity_candidates": identity_candidates,
            },
            "friendship": friendship,
        }
        if self._reconcile_scaled_template_geometry_slots(image, result):
            return result
        if debug_output_dir is not None:
            self._write_debug_artifacts(
                image=image,
                roi=roi,
                roi_image=roi_image,
                circles=circles,
                bars=bars,
                result=result,
                debug_output_dir=Path(debug_output_dir),
            )
        return result

    def _recognize_native_resolution(
        self,
        image: np.ndarray,
        *,
        image_path: str | Path | None,
    ) -> dict[str, Any]:
        height, width = image.shape[:2]
        roi = self.compute_roi(width, height)
        x1, y1, x2, y2 = roi
        roi_image = image[y1:y2, x1:x2]
        circles = self._detect_avatar_candidates(roi_image)
        bars = self._detect_bond_bars(roi_image)
        slots = self._build_slots(roi_image, circles, bars, roi)
        hint_count = sum(1 for slot in slots if slot["hint_badge"])
        entity_counts = self._summarize_entity_counts(slots)
        identity_candidates = self._collect_identity_candidates(slots)
        return {
            "image": str(image_path) if image_path is not None else "",
            "image_size": [width, height],
            "roi": list(roi),
            "source": "cv_heuristic",
            "support_slots": slots,
            "support_count": len(slots),
            **entity_counts,
            "hint_count": hint_count,
            "red_dot_count": hint_count,
            "identity_candidates": identity_candidates,
            "support_slot_summary": {
                "count": len(slots),
                **entity_counts,
                "red_dot_count": hint_count,
                "identity_candidates": identity_candidates,
            },
            "friendship": self._legacy_friendship_from_slots(slots),
        }

    @staticmethod
    def _prefer_native_single_slot_result(
        canonical: dict[str, Any],
        native: dict[str, Any],
    ) -> bool:
        # A canonical frame with verified template evidence has more precise
        # identity/portrait geometry than the native heuristic.  Do not let a
        # coarser native pass replace it just because it found an extra circle.
        if canonical.get("source") == "template_geometry_fusion":
            return False
        canonical_count = int(canonical.get("support_count", 0))
        native_count = int(native.get("support_count", 0))
        if canonical_count == 0 and native_count == 1:
            native_slots = native.get("support_slots")
            # Synthetic callers exercise the count-level contract only.  A
            # real native singleton without a hint must also sit in the upper
            # avatar band; this rejects lower-panel circular controls.
            if not isinstance(native_slots, list):
                return True
            if any(slot.get("hint_badge") for slot in native_slots if isinstance(slot, dict)):
                return True
            roi = native.get("roi")
            if not isinstance(roi, list) or len(roi) != 4 or not native_slots:
                return False
            bbox = native_slots[0].get("bbox")
            if not isinstance(bbox, list) or len(bbox) != 4:
                return False
            return (
                bbox[1] < roi[1] + ((roi[3] - roi[1]) * 0.25)
                and not SupportSlotRecognizer._is_unsupported_inferred_slot(native_slots[0])
            )
        if (
            canonical_count > 0
            and native_count > canonical_count
            and not any(
                SupportSlotRecognizer._is_unsupported_inferred_slot(slot)
                for slot in native.get("support_slots", [])
                if isinstance(slot, dict)
            )
            and all(
                slot.get("badge_kind") != "bond_heart"
                for slot in native.get("support_slots", [])
                if isinstance(slot, dict)
            )
        ):
            return True
        if canonical_count != 1 or native_count != 1:
            return False
        canonical_slot = canonical.get("support_slots", [{}])[0]
        native_slot = native.get("support_slots", [{}])[0]
        if canonical_slot.get("template_match_confidence"):
            return False
        return canonical_slot.get("badge_kind") != native_slot.get("badge_kind")

    @staticmethod
    def _is_unsupported_inferred_slot(slot: dict[str, Any]) -> bool:
        if slot.get("bond_bar_source") != "inferred":
            return False
        return not (
            slot.get("hint_badge") is True
            or slot.get("badge_kind") == "bond_heart"
            or bool(slot.get("matched_card_id"))
            or bool(slot.get("template_match_confidence"))
            or bool((slot.get("portrait_visual_evidence") or {}).get("accepted"))
        )

    def _reconcile_scaled_template_geometry_slots(
        self,
        image: np.ndarray,
        result: dict[str, Any],
    ) -> bool:
        """Associate scaled template evidence without replacing detector slots.

        Verified templates can name an already-detected portrait.  They may
        create slots only when the detector produced no slots at all and each
        added slot also overlaps independent circle geometry.  This keeps
        portrait counting separate from identity matching.
        """
        roi = self.compute_roi(image.shape[1], image.shape[0])
        find_matches = getattr(self._identity_matcher, "find_scaled_template_matches", None)
        if not callable(find_matches):
            return False
        matches = find_matches(
            image,
            # Template localization is an identity override, not a loose
            # candidate search.  Keep it above the 0.95 verified threshold;
            # lower scores remain dashboard candidates and never become
            # effective support identities.
            minimum_score=0.95,
            search_bbox=list(roi),
        )
        if not matches:
            return False

        roi_image = image[roi[1]:roi[3], roi[0]:roi[2]]
        geometry_candidates = self._detect_template_geometry_candidates(roi_image)
        existing_slots = result.get("support_slots")
        if isinstance(existing_slots, list) and existing_slots:
            self._apply_scaled_template_identity_overlays(
                roi_image,
                roi,
                existing_slots,
                matches,
                geometry_candidates,
            )
            self._refresh_result_slots(result, existing_slots)
            return False

        slots: list[dict[str, Any]] = []
        used_geometry: set[int] = set()
        used_entities: set[tuple[str, str]] = set()
        for match in sorted(matches, key=lambda item: item["template_match_confidence"], reverse=True):
            identity = match["identity"]
            entity_key = self._identity_entity_key(identity)
            if entity_key is None or entity_key in used_entities:
                continue
            geometry_index, geometry = self._find_overlapping_template_geometry(
                geometry_candidates,
                roi,
                match["bbox"],
                used_geometry,
            )
            if geometry is None:
                continue
            slot = self._make_slot(
                roi_image,
                geometry,
                self._infer_bond_bar(roi_image, geometry),
                roi,
                bond_bar_source="inferred",
            )
            slot["bbox"] = list(match["bbox"])
            slot["bbox_roi"] = [
                match["bbox"][0] - roi[0],
                match["bbox"][1] - roi[1],
                match["bbox"][2] - roi[0],
                match["bbox"][3] - roi[1],
            ]
            self._apply_identity_match(slot, identity)
            slot["source"] = "template_geometry_fusion"
            slot["template_match_confidence"] = match["template_match_confidence"]
            slot["template_match_path"] = match["template_path"]
            slot["template_scale"] = match["template_scale"]
            slot["identity_template_bbox"] = list(match["bbox"])
            slot["avatar_geometry"] = {
                "kind": "hough_circle",
                "center": [geometry.cx + roi[0], geometry.cy + roi[1]],
                "radius": geometry.r,
                "band_score": round(float(geometry.band_score), 3),
            }
            slots.append(slot)
            used_geometry.add(geometry_index)
            used_entities.add(entity_key)

        if not slots:
            return False
        self._apply_full_roi_hint_badges(roi_image, slots, roi)
        self._refresh_result_slots(result, slots)
        result["source"] = "template_geometry_fusion"
        return True

    def _apply_scaled_template_identity_overlays(
        self,
        roi_image: np.ndarray,
        roi: tuple[int, int, int, int],
        slots: list[dict[str, Any]],
        matches: list[dict[str, Any]],
        geometry_candidates: list[_CircleCandidate],
    ) -> None:
        """Write verified identities onto existing slots using one-to-one geometry."""
        used_slots: set[int] = set()
        used_entities: set[tuple[str, str]] = set()
        matched_slots: list[dict[str, Any]] = []
        for match in sorted(matches, key=lambda item: item["template_match_confidence"], reverse=True):
            identity = match["identity"]
            entity_key = self._identity_entity_key(identity)
            if entity_key is None or entity_key in used_entities:
                continue
            geometry_index, geometry = self._find_overlapping_template_geometry(
                geometry_candidates,
                roi,
                match["bbox"],
                set(),
            )
            if geometry_index < 0 or geometry is None:
                continue
            candidates = [
                (index, slot)
                for index, slot in enumerate(slots)
                if index not in used_slots
                and (
                    self._slot_overlaps_template_geometry(slot, match["bbox"])
                    or self._slot_is_near_circle_geometry(slot, geometry, roi)
                )
            ]
            if not candidates:
                continue
            slot_index, slot = max(
                candidates,
                key=lambda item: self._slot_template_overlap_score(item[1], match["bbox"]),
            )
            self._apply_identity_match(slot, identity)
            slot["template_match_confidence"] = match["template_match_confidence"]
            slot["template_match_path"] = match["template_path"]
            slot["template_scale"] = match["template_scale"]
            slot["identity_template_bbox"] = list(match["bbox"])
            slot["avatar_geometry"] = {
                "kind": "hough_circle",
                "center": [geometry.cx + roi[0], geometry.cy + roi[1]],
                "radius": geometry.r,
                "band_score": round(float(geometry.band_score), 3),
            }
            used_slots.add(slot_index)
            used_entities.add(entity_key)
            matched_slots.append(slot)

        if matched_slots:
            self._correct_verified_template_hint_badges(roi_image, matched_slots, roi)

    def _correct_verified_template_hint_badges(
        self,
        roi_image: np.ndarray,
        slots: list[dict[str, Any]],
        roi: tuple[int, int, int, int],
    ) -> None:
        """Confirm red exclamation evidence without discarding other hint icons."""
        badges = self._detect_full_roi_hint_badges(roi_image)
        for slot in slots:
            matched_badge = self._full_roi_badge_for_slot(slot, badges, roi)
            if matched_badge is not None:
                self._set_full_roi_hint_badge(slot, matched_badge, roi)
                continue
            if slot.get("badge_kind") == "hint_exclamation" and self._legacy_hint_bbox_is_red(roi_image, slot, roi):
                slot["hint_badge"] = False
                slot["event_or_skill_hint_possible"] = False
                slot["hint_badge_bbox"] = None
                slot["hint_badge_bbox_roi"] = None
                slot["hint_badge_evidence"] = None
                slot["badge_kind"] = "none"
                slot["badge_reason"] = "unverified_red_exclamation_geometry"

    @staticmethod
    def _legacy_hint_bbox_is_red(
        roi_image: np.ndarray,
        slot: dict[str, Any],
        roi: tuple[int, int, int, int],
    ) -> bool:
        bbox = slot.get("hint_badge_bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            return False
        x1 = max(0, int(bbox[0] - roi[0]))
        y1 = max(0, int(bbox[1] - roi[1]))
        x2 = min(roi_image.shape[1], int(bbox[2] - roi[0]))
        y2 = min(roi_image.shape[0], int(bbox[3] - roi[1]))
        if x2 <= x1 or y2 <= y1:
            return False
        hsv = cv2.cvtColor(roi_image[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
        saturated = (hsv[:, :, 1] >= 120) & (hsv[:, :, 2] >= 100)
        red_or_pink = (hsv[:, :, 0] <= 20) | (hsv[:, :, 0] >= 160)
        return bool(np.count_nonzero(saturated & red_or_pink) >= 80)

    def _detect_template_geometry_candidates(self, roi_image: np.ndarray) -> list[_CircleCandidate]:
        gray = cv2.medianBlur(cv2.cvtColor(roi_image, cv2.COLOR_BGR2GRAY), 5)
        scale = roi_image.shape[1] / 320.0
        circles = cv2.HoughCircles(
            gray,
            cv2.HOUGH_GRADIENT,
            dp=1.2,
            minDist=max(26, int(52 * scale)),
            param1=80,
            param2=24,
            minRadius=max(14, int(36 * scale)),
            maxRadius=max(24, int(76 * scale)),
        )
        if circles is None:
            return []
        max_y = int(roi_image.shape[0] * 0.65)
        candidates: list[_CircleCandidate] = []
        for raw in np.round(circles[0]).astype(int):
            cx, cy, radius = (int(raw[0]), int(raw[1]), int(raw[2]))
            if cx < int(roi_image.shape[1] * 0.2):
                continue
            if cy < int(max(36, 44 * scale)) or cy > max_y:
                continue
            candidates.append(
                _CircleCandidate(
                    cx=cx,
                    cy=cy,
                    r=radius,
                    band_score=self._band_score(roi_image, cx, cy, radius),
                )
            )
        return candidates

    @staticmethod
    def _find_overlapping_template_geometry(
        candidates: list[_CircleCandidate],
        roi: tuple[int, int, int, int],
        template_bbox: list[int],
        used_indices: set[int],
    ) -> tuple[int, _CircleCandidate | None]:
        best_index = -1
        best_candidate: _CircleCandidate | None = None
        best_score = 0.0
        template_area = max(1, (template_bbox[2] - template_bbox[0]) * (template_bbox[3] - template_bbox[1]))
        for index, circle in enumerate(candidates):
            if index in used_indices:
                continue
            circle_bbox = [
                roi[0] + circle.cx - circle.r,
                roi[1] + circle.cy - circle.r,
                roi[0] + circle.cx + circle.r,
                roi[1] + circle.cy + circle.r,
            ]
            intersection_width = max(0, min(circle_bbox[2], template_bbox[2]) - max(circle_bbox[0], template_bbox[0]))
            intersection_height = max(0, min(circle_bbox[3], template_bbox[3]) - max(circle_bbox[1], template_bbox[1]))
            overlap = (intersection_width * intersection_height) / template_area
            if overlap >= 0.35 and overlap > best_score:
                best_index = index
                best_candidate = circle
                best_score = overlap
        return best_index, best_candidate

    def _apply_full_roi_hint_badges(
        self,
        roi_image: np.ndarray,
        slots: list[dict[str, Any]],
        roi: tuple[int, int, int, int],
    ) -> None:
        badges = self._detect_full_roi_hint_badges(roi_image)
        for slot in slots:
            slot["hint_badge"] = False
            slot["event_or_skill_hint_possible"] = False
            slot["hint_badge_bbox"] = None
            slot["hint_badge_bbox_roi"] = None
            slot["hint_badge_evidence"] = None
            slot["badge_kind"] = "none"
            slot["badge_reason"] = "no_full_roi_hint_badge"
            badge = self._full_roi_badge_for_slot(slot, badges, roi)
            if badge is not None:
                self._set_full_roi_hint_badge(slot, badge, roi)

    def _confirm_legacy_hint_badges(
        self,
        roi_image: np.ndarray,
        slots: list[dict[str, Any]],
        roi: tuple[int, int, int, int],
    ) -> None:
        """Require full red-exclamation evidence before promoting legacy candidates."""
        badges = self._detect_full_roi_hint_badges(roi_image)
        for slot in slots:
            if not slot.get("hint_badge") or slot.get("badge_kind") not in {
                "hint_icon",
                "hint_exclamation",
            }:
                continue
            badge = self._full_roi_badge_for_slot(slot, badges, roi)
            if badge is not None:
                self._set_full_roi_hint_badge(slot, badge, roi)
                continue
            slot["hint_badge"] = False
            slot["event_or_skill_hint_possible"] = False
            slot["hint_badge_bbox"] = None
            slot["hint_badge_bbox_roi"] = None
            slot["hint_badge_evidence"] = None
            slot["badge_kind"] = "unverified_colored_icon"
            slot["badge_reason"] = "legacy_candidate_missing_red_exclamation_geometry"

    @staticmethod
    def _full_roi_badge_for_slot(
        slot: dict[str, Any],
        badges: list[dict[str, Any]],
        roi: tuple[int, int, int, int],
    ) -> dict[str, Any] | None:
        template_bbox = slot.get("identity_template_bbox")
        if isinstance(template_bbox, list) and len(template_bbox) == 4:
            slot_bbox = [
                template_bbox[0] - roi[0],
                template_bbox[1] - roi[1],
                template_bbox[2] - roi[0],
                template_bbox[3] - roi[1],
            ]
        else:
            slot_bbox = slot.get("bbox_roi")
        if not isinstance(slot_bbox, list) or len(slot_bbox) != 4:
            bbox = slot.get("bbox")
            if not isinstance(bbox, list) or len(bbox) != 4:
                return None
            slot_bbox = [
                bbox[0] - roi[0],
                bbox[1] - roi[1],
                bbox[2] - roi[0],
                bbox[3] - roi[1],
            ]
        center_x = (slot_bbox[0] + slot_bbox[2]) / 2.0
        width = slot_bbox[2] - slot_bbox[0]
        height = slot_bbox[3] - slot_bbox[1]
        for badge in badges:
            bbox = badge["bbox_roi"]
            badge_center_x = (bbox[0] + bbox[2]) / 2.0
            badge_center_y = (bbox[1] + bbox[3]) / 2.0
            if badge_center_x < center_x + (width * 0.25):
                continue
            if badge_center_y < slot_bbox[1] - (height * 0.35):
                continue
            if badge_center_y > slot_bbox[1] + (height * 0.6):
                continue
            return badge
        return None

    def _set_full_roi_hint_badge(
        self,
        slot: dict[str, Any],
        badge: dict[str, Any],
        roi: tuple[int, int, int, int],
    ) -> None:
        bbox = badge["bbox_roi"]
        absolute_bbox = self._map_bbox_to_original(bbox, roi)
        slot["hint_badge"] = True
        slot["event_or_skill_hint_possible"] = True
        slot["hint_badge_bbox"] = absolute_bbox
        slot["hint_badge_bbox_roi"] = bbox
        slot["hint_badge_evidence"] = {
            "bbox": absolute_bbox,
            "bbox_roi": bbox,
            "crop_path": None,
            "kind": "hint_exclamation",
            "reason": "full_roi_red_exclamation_geometry",
        }
        slot["badge_kind"] = "hint_exclamation"
        slot["badge_reason"] = "full_roi_red_exclamation_geometry"

    @staticmethod
    def _detect_full_roi_hint_badges(roi_image: np.ndarray) -> list[dict[str, Any]]:
        hsv = cv2.cvtColor(roi_image, cv2.COLOR_BGR2HSV)
        scale = roi_image.shape[1] / 320.0
        red_mask = (
            (((hsv[:, :, 0] <= 20) | (hsv[:, :, 0] >= 160))
            & (hsv[:, :, 1] >= 120)
            & (hsv[:, :, 2] >= 100))
            .astype(np.uint8)
            * 255
        )
        count, _, stats, _ = cv2.connectedComponentsWithStats(red_mask, 8)
        badges: list[dict[str, Any]] = []
        for index in range(1, count):
            x, y, width, height, area = [int(value) for value in stats[index]]
            if (
                area < int(500 * scale * scale)
                or width < int(30 * scale)
                or height < int(30 * scale)
            ):
                continue
            aspect = width / max(float(height), 1.0)
            if aspect < 0.8 or aspect > 1.2:
                continue
            patch_hsv = hsv[y:y + height, x:x + width]
            white_mask = (
                ((patch_hsv[:, :, 1] <= 65) & (patch_hsv[:, :, 2] >= 190)).astype(np.uint8) * 255
            )
            white_count, _, white_stats, _ = cv2.connectedComponentsWithStats(white_mask, 8)
            stems: list[tuple[int, int, int, int]] = []
            dots: list[tuple[int, int, int, int]] = []
            for white_index in range(1, white_count):
                white_x, white_y, white_width, white_height, white_area = [
                    int(value) for value in white_stats[white_index]
                ]
                if (
                    int(8 * scale) <= white_width <= int(23 * scale)
                    and white_height >= int(18 * scale)
                    and white_height >= int(white_width * 1.35)
                    and white_area >= int(120 * scale * scale)
                ):
                    stems.append((white_x, white_y, white_width, white_height))
                elif (
                    max(2, int(4 * scale)) <= white_width <= int(18 * scale)
                    and max(2, int(3 * scale)) <= white_height <= int(14 * scale)
                    and white_area >= max(6, int(15 * scale * scale))
                ):
                    dots.append((white_x, white_y, white_width, white_height))

            has_exclamation_topology = any(
                abs((dot_x + (dot_width / 2.0)) - (stem_x + (stem_width / 2.0)))
                <= max(5 * scale, stem_width * 0.8)
                and stem_y + stem_height - max(2, int(3 * scale))
                <= dot_y
                <= stem_y + stem_height + int(12 * scale)
                for stem_x, stem_y, stem_width, stem_height in stems
                for dot_x, dot_y, dot_width, dot_height in dots
            )
            if has_exclamation_topology:
                badges.append(
                    {
                        "bbox_roi": [x, y, x + width, y + height],
                        "confidence": round(min(0.99, 0.7 + (area / 10000.0)), 3),
                    }
                )
        return sorted(badges, key=lambda badge: (badge["bbox_roi"][1], badge["bbox_roi"][0]))

    @staticmethod
    def compute_roi(width: int, height: int) -> tuple[int, int, int, int]:
        sx = width / REF_W
        sy = height / REF_H
        return (
            int(ROI_X1 * sx),
            int(ROI_Y1 * sy),
            int(ROI_X2 * sx),
            int(ROI_Y2 * sy),
        )

    def _build_slots(
        self,
        roi_image: np.ndarray,
        circles: list[_CircleCandidate],
        bars: list[_BarCandidate],
        roi: tuple[int, int, int, int],
    ) -> list[dict[str, Any]]:
        if not circles:
            return []

        slots: list[dict[str, Any]] = []
        used_circles: set[int] = set()
        slot_scale = roi_image.shape[1] / 160.0
        min_dy = int(max(22, 22 * slot_scale))
        max_dy = int(max(88, 88 * slot_scale))

        for bar in sorted(bars, key=lambda item: item.y1):
            best_index = -1
            best_score = None
            bar_mid_y = (bar.y1 + bar.y2) // 2
            bar_mid_x = (bar.x1 + bar.x2) // 2
            for index, circle in enumerate(circles):
                if index in used_circles:
                    continue
                gap = bar_mid_y - circle.cy
                if gap < min_dy or gap > max_dy:
                    continue
                if (
                    roi_image.shape[1] <= 200
                    and abs(circle.cx - bar_mid_x) > max(20, int(circle.r * 1.2))
                ):
                    continue
                score = (circle.r * 3) - gap + (circle.band_score * 10.0)
                if best_score is None or score > best_score:
                    best_score = score
                    best_index = index
            if best_index >= 0:
                used_circles.add(best_index)
                slot = self._make_slot(
                    roi_image,
                    circles[best_index],
                    bar,
                    roi,
                    bond_bar_source="detected",
                )
                slot["bond_bar_source"] = "detected"
                slots.append(slot)

        # A missing bar can happen on normalized legacy captures, but lower
        # panel controls also form convincing circles. Restrict barless
        # inference to the upper support-avatar band.
        allow_inferred_slots = len(bars) >= 2 or (
            not bars
            and any(circle.cy < int(roi_image.shape[0] * 0.4) for circle in circles)
        )
        spacing_threshold = int(max(90, 90 * slot_scale))
        if allow_inferred_slots:
            for circle_index, circle in sorted(enumerate(circles), key=lambda item: item[1].cy):
                if circle_index in used_circles:
                    continue
                if circle.band_score < 0.42:
                    continue
                if any(abs(slot["bbox"][1] - (circle.cy + roi[1] - circle.r)) < spacing_threshold for slot in slots):
                    continue
                inferred_bar = self._infer_bond_bar(roi_image, circle)
                slot = self._make_slot(
                    roi_image,
                    circle,
                    inferred_bar,
                    roi,
                    bond_bar_source="inferred",
                )
                slot["bond_bar_source"] = "inferred"
                slots.append(slot)

        slots.sort(key=lambda slot: slot["bbox"][1])
        self._confirm_legacy_hint_badges(roi_image, slots, roi)
        normalized: list[dict[str, Any]] = []
        for slot_index, slot in enumerate(slots):
            slot["slot"] = slot_index
            identity = self._identity_matcher.match_slot(
                image=roi_image,
                bbox=slot["bbox_roi"],
                slot_index=slot_index,
                slot_payload=slot,
            ).to_dict()
            identity_payload = {
                "status": identity.get("status", "unknown"),
                "identity_state": identity.get("identity_state", "unknown_identity"),
                "matched_card_id": identity.get("matched_card_id"),
                "confidence": identity.get("confidence", 0.0),
                "identity_confidence": identity.get("identity_confidence", identity.get("confidence", 0.0)),
                "display_name": identity.get("display_name", ""),
                "card_name": identity.get("card_name", identity.get("display_name", "")),
                "type": identity.get("type", identity.get("card_type", "unknown")),
                "card_type": identity.get("card_type", identity.get("type", "unknown")),
                "character_name": identity.get("character_name", "unknown"),
                "source": identity.get("source"),
                "reason": identity.get("reason"),
                "unknown_reason": identity.get(
                    "unknown_reason",
                    "" if identity.get("status") == "known" else identity.get("reason"),
                ),
                "manual_status": identity.get("manual_status", "unknown"),
                "event_skill_notes": identity.get("event_skill_notes", ""),
                "needs_manual_label": bool(identity.get("needs_manual_label")),
                "evidence_sources": identity.get("evidence_sources", []),
                "data_links": identity.get("data_links", {"events": [], "skills": [], "hints": []}),
            }
            support_card = identity.get("support_card")
            if not isinstance(support_card, dict):
                support_card = {
                    "slot": identity.get("slot"),
                    "card_id": identity.get("card_id"),
                    "matched_card_id": identity.get("matched_card_id"),
                    "character_id": identity.get("character_id", "unknown"),
                    "character_name": identity_payload["character_name"],
                    "display_name": identity.get("display_name", ""),
                    "card_name": identity_payload["card_name"],
                    "type": identity_payload["type"],
                    "card_type": identity_payload["card_type"],
                    "confidence": identity_payload["confidence"],
                    "identity_confidence": identity_payload["identity_confidence"],
                    "status": identity_payload["status"],
                    "identity_state": identity_payload["identity_state"],
                    "reason": identity_payload["reason"],
                    "unknown_reason": identity_payload["unknown_reason"],
                    "source": identity_payload["source"],
                    "evidence_sources": identity_payload["evidence_sources"],
                    "matched_template_path": identity.get("matched_template_path"),
                    "template_path": identity.get("template_path"),
                    "avatar_template_path": identity.get("avatar_template_path"),
                    "support_bonus_summary": identity.get("support_bonus_summary", {}),
                    "training_bonus_summary": identity.get("training_bonus_summary", {}),
                    "stat_bonuses": identity.get("stat_bonuses", identity.get("support_bonus_summary", {})),
                    "event_hooks": identity.get("event_hooks", []),
                    "event_names": identity.get("event_names", identity.get("event_hooks", [])),
                    "skill_hint_tags": identity.get("skill_hint_tags", []),
                    "skill_hints": identity.get("skill_hints", identity.get("skill_hint_tags", [])),
                    "data_links": identity_payload["data_links"],
                    "event_skill_notes": identity_payload["event_skill_notes"],
                    "manual_status": identity_payload["manual_status"],
                    "needs_manual_label": identity_payload["needs_manual_label"],
                }
            slot["identity"] = identity_payload
            slot["identity_status"] = identity_payload["status"]
            slot["identity_state"] = identity_payload["identity_state"]
            slot["identity_confidence"] = identity_payload["identity_confidence"]
            slot["identity_unknown_reason"] = identity_payload["unknown_reason"]
            slot["matched_card_id"] = identity_payload["matched_card_id"]
            slot["identity_candidates"] = identity.get("identity_candidates", [])
            slot["character_id"] = identity["character_id"]
            slot["character_name"] = identity_payload["character_name"]
            slot["card_id"] = identity["card_id"]
            slot["display_name"] = identity.get("display_name", "")
            slot["card_name"] = identity_payload["card_name"]
            slot["identity_display_name"] = identity.get("display_name", "")
            slot["identity_card_type"] = identity_payload["card_type"]
            slot["identity_source"] = identity.get("source")
            slot["identity_reason"] = identity.get("reason")
            slot["support_card"] = support_card
            slot["matched_template_path"] = identity.get("matched_template_path")
            slot["support_bonus_summary"] = identity.get("support_bonus_summary", {})
            slot["training_bonus_summary"] = identity.get("training_bonus_summary", {})
            slot["event_hooks"] = identity.get("event_hooks", [])
            slot["skill_hint_tags"] = identity.get("skill_hint_tags", [])
            slot["template_path"] = identity.get("template_path")
            slot["avatar_template_path"] = identity.get("avatar_template_path")
            slot["manual_status"] = identity_payload["manual_status"]
            slot["event_skill_notes"] = identity_payload["event_skill_notes"]
            slot["needs_manual_label"] = identity_payload["needs_manual_label"]
            slot["evidence_sources"] = identity_payload["evidence_sources"]
            slot["data_links"] = identity_payload["data_links"]
            entity_kind = str(identity.get("entity_kind") or "")
            if not entity_kind:
                entity_kind = (
                    "support_card"
                    if identity_payload["status"] == "known" and identity_payload["matched_card_id"]
                    else "unknown_portrait"
                )
            slot["entity_kind"] = entity_kind
            slot["is_effective_support"] = bool(identity.get("is_effective_support")) and (
                identity_payload["status"] == "known"
                and bool(identity_payload["matched_card_id"])
            )
            slot["confidence"] = round(max(slot["confidence"], identity["confidence"]), 3)
            slot["source"] = "cv_heuristic"
            if self._is_unsupported_inferred_slot(slot):
                continue
            slot.pop("bbox_roi", None)
            normalized.append(slot)
        for slot_index, slot in enumerate(normalized):
            slot["slot"] = slot_index
            support_card = slot.get("support_card")
            if isinstance(support_card, dict):
                support_card["slot"] = slot_index
            for candidate in slot.get("identity_candidates", []):
                if isinstance(candidate, dict):
                    candidate["slot"] = slot_index
        return normalized

    @staticmethod
    def _summarize_entity_counts(slots: list[dict[str, Any]]) -> dict[str, int]:
        visible_portrait_count = len(slots)
        effective_support_count = sum(1 for slot in slots if slot.get("is_effective_support"))
        non_support_npc_count = sum(1 for slot in slots if slot.get("entity_kind") == "non_support_npc")
        return {
            "visible_portrait_count": visible_portrait_count,
            "effective_support_count": effective_support_count,
            "non_support_npc_count": non_support_npc_count,
            "unresolved_portrait_count": visible_portrait_count - effective_support_count - non_support_npc_count,
        }

    def _reconcile_exact_template_slots(
        self,
        image: np.ndarray,
        result: dict[str, Any],
        *,
        image_path: str | Path | None,
    ) -> None:
        roi = self.compute_roi(image.shape[1], image.shape[0])
        matches = self._identity_matcher.find_exact_template_matches(
            image,
            minimum_score=0.98,
            search_bbox=list(roi),
        )
        if not matches:
            return

        slots = result.get("support_slots", [])
        if not isinstance(slots, list):
            return
        roi_image = image[roi[1]:roi[3], roi[0]:roi[2]]
        circles = self._detect_avatar_candidates(roi_image)
        assignments: list[tuple[float, float, dict[str, Any], dict[str, Any], _CircleCandidate | None]] = []
        for match in matches:
            candidate_slots = [
                slot for slot in slots if self._slot_overlaps_template_geometry(slot, match["bbox"])
            ]
            geometry_circle = self._circle_overlapping_template(circles, roi, match["bbox"])
            if not candidate_slots and geometry_circle is not None:
                candidate_slots = [
                    slot
                    for slot in slots
                    if self._slot_is_near_circle_geometry(slot, geometry_circle, roi)
                ]
            if not candidate_slots and geometry_circle is None:
                continue
            slot = (
                max(
                    candidate_slots,
                    key=lambda item: self._slot_template_overlap_score(item, match["bbox"]),
                )
                if candidate_slots
                else None
            )
            geometry_score = (
                self._slot_template_overlap_score(slot, match["bbox"])
                if slot is not None
                else 0.35
            )
            assignments.append(
                (
                    float(match["template_match_confidence"]) + geometry_score,
                    geometry_score,
                    match,
                    slot,
                    geometry_circle,
                )
            )

        assigned_entity_keys: set[tuple[str, str]] = set()
        assigned_slot_ids: set[int] = set()
        for _, _, match, slot, geometry_circle in sorted(assignments, key=lambda item: (item[0], item[1]), reverse=True):
            identity = match["identity"]
            entity_key = self._identity_entity_key(identity)
            if entity_key is not None and entity_key in assigned_entity_keys:
                if slot is not None and id(slot) not in assigned_slot_ids:
                    self._mark_slot_unresolved_duplicate_entity(slot, match)
                continue
            if slot is not None and id(slot) in assigned_slot_ids:
                continue
            if slot is None:
                if geometry_circle is None:
                    continue
                slot = self._make_slot(
                    roi_image,
                    geometry_circle,
                    self._infer_bond_bar(roi_image, geometry_circle),
                    roi,
                    bond_bar_source="inferred",
                )
                slots.append(slot)
            self._apply_identity_match(slot, identity)
            slot["template_match_confidence"] = match["template_match_confidence"]
            slot["template_match_path"] = match["template_path"]
            slot["identity_template_bbox"] = list(match["bbox"])
            assigned_slot_ids.add(id(slot))
            if entity_key is not None:
                assigned_entity_keys.add(entity_key)

        self._refresh_result_slots(result, slots)

    def _refresh_result_slots(self, result: dict[str, Any], slots: list[dict[str, Any]]) -> None:
        slots.sort(key=lambda slot: slot["bbox"][1])
        for slot_index, slot in enumerate(slots):
            slot["slot"] = slot_index
        entity_counts = self._summarize_entity_counts(slots)
        hint_count = sum(1 for slot in slots if slot.get("hint_badge"))
        identity_candidates = self._collect_identity_candidates(slots)
        result["support_slots"] = slots
        result["support_count"] = len(slots)
        result.update(entity_counts)
        result["hint_count"] = hint_count
        result["red_dot_count"] = hint_count
        result["identity_candidates"] = identity_candidates
        result["support_slot_summary"] = {
            "count": len(slots),
            **entity_counts,
            "red_dot_count": hint_count,
            "identity_candidates": identity_candidates,
        }
        result["friendship"] = self._legacy_friendship_from_slots(slots)

    @staticmethod
    def _legacy_friendship_from_slots(slots: list[dict[str, Any]]) -> bool:
        for slot in slots:
            measured = slot.get("bond_bar")
            if (
                isinstance(measured, dict)
                and measured.get("schema_version") == "bond_bar_measurement.v1"
            ):
                if (
                    measured.get("measurement_status") == "accepted"
                    and measured.get("friendship_ready") is True
                    and measured.get("decision_safe") is True
                ):
                    return True
                # A v1 abstention must not be bypassed by the older broad
                # color-area heuristic stored beside it.
                continue
            legacy = slot.get("legacy_bond_bar_visual")
            if not isinstance(legacy, dict):
                candidate = measured
                if (
                    isinstance(candidate, dict)
                    and candidate.get("schema_version") != "bond_bar_measurement.v1"
                ):
                    legacy = candidate
            if isinstance(legacy, dict) and legacy.get("status") == "friendship":
                return True
        return False

    @staticmethod
    def _identity_entity_key(identity: Any) -> tuple[str, str] | None:
        entity_kind = str(getattr(identity, "entity_kind", ""))
        if entity_kind == "support_card":
            card_id = getattr(identity, "matched_card_id", None)
            return (entity_kind, str(card_id)) if card_id else None
        if entity_kind == "non_support_npc":
            entity_id = getattr(identity, "card_id", None) or getattr(identity, "character_id", None)
            return (entity_kind, str(entity_id)) if entity_id else None
        return None

    @staticmethod
    def _mark_slot_unresolved_duplicate_entity(slot: dict[str, Any], match: dict[str, Any]) -> None:
        identity = match["identity"]
        candidate = {
            "card_id": getattr(identity, "card_id", None),
            "matched_card_id": None,
            "character_id": getattr(identity, "character_id", "unknown"),
            "character_name": getattr(identity, "character_name", "unknown"),
            "display_name": getattr(identity, "display_name", ""),
            "confidence": match["template_match_confidence"],
            "identity_confidence": match["template_match_confidence"],
            "status": "ambiguous",
            "reason": "duplicate_entity_candidate",
            "source": "exact_template_spatial_candidate",
        }
        candidates = slot.get("identity_candidates")
        slot["identity_candidates"] = [*candidates, candidate] if isinstance(candidates, list) else [candidate]
        slot["identity"] = {
            "status": "ambiguous",
            "identity_state": "unknown_identity",
            "matched_card_id": None,
            "confidence": match["template_match_confidence"],
            "identity_confidence": match["template_match_confidence"],
            "unknown_reason": "duplicate_entity_candidate",
        }
        slot["identity_status"] = "ambiguous"
        slot["identity_state"] = "unknown_identity"
        slot["identity_confidence"] = match["template_match_confidence"]
        slot["identity_unknown_reason"] = "duplicate_entity_candidate"
        slot["matched_card_id"] = None
        slot["is_effective_support"] = False
        slot["entity_kind"] = "unknown_portrait"

    @staticmethod
    def _slot_template_overlap_score(slot: dict[str, Any], template_bbox: list[int]) -> float:
        slot_bbox = slot.get("bbox")
        if not (
            isinstance(slot_bbox, list)
            and len(slot_bbox) == 4
            and isinstance(template_bbox, list)
            and len(template_bbox) == 4
        ):
            return 0.0
        intersection_width = max(0, min(slot_bbox[2], template_bbox[2]) - max(slot_bbox[0], template_bbox[0]))
        intersection_height = max(0, min(slot_bbox[3], template_bbox[3]) - max(slot_bbox[1], template_bbox[1]))
        intersection_area = intersection_width * intersection_height
        slot_area = max(1, (slot_bbox[2] - slot_bbox[0]) * (slot_bbox[3] - slot_bbox[1]))
        template_area = max(1, (template_bbox[2] - template_bbox[0]) * (template_bbox[3] - template_bbox[1]))
        return intersection_area / min(slot_area, template_area)

    @classmethod
    def _slot_overlaps_template_geometry(cls, slot: dict[str, Any], template_bbox: list[int]) -> bool:
        return cls._slot_template_overlap_score(slot, template_bbox) >= 0.35

    @staticmethod
    def _circle_overlapping_template(
        circles: list[_CircleCandidate],
        roi: tuple[int, int, int, int],
        template_bbox: list[int],
    ) -> _CircleCandidate | None:
        roi_x1, roi_y1, _, _ = roi
        best_circle: _CircleCandidate | None = None
        best_score = 0.0
        for circle in circles:
            circle_bbox = [
                roi_x1 + circle.cx - circle.r,
                roi_y1 + circle.cy - circle.r,
                roi_x1 + circle.cx + circle.r,
                roi_y1 + circle.cy + circle.r,
            ]
            intersection_width = max(0, min(circle_bbox[2], template_bbox[2]) - max(circle_bbox[0], template_bbox[0]))
            intersection_height = max(0, min(circle_bbox[3], template_bbox[3]) - max(circle_bbox[1], template_bbox[1]))
            overlap = intersection_width * intersection_height
            template_area = max(1, (template_bbox[2] - template_bbox[0]) * (template_bbox[3] - template_bbox[1]))
            score = overlap / template_area
            if score >= 0.35 and score > best_score:
                best_circle = circle
                best_score = score
        return best_circle

    @staticmethod
    def _slot_is_near_circle_geometry(
        slot: dict[str, Any],
        circle: _CircleCandidate,
        roi: tuple[int, int, int, int],
    ) -> bool:
        bbox = slot.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            return False
        center_x = roi[0] + circle.cx
        center_y = roi[1] + circle.cy
        slot_center_x = (bbox[0] + bbox[2]) // 2
        slot_center_y = (bbox[1] + bbox[3]) // 2
        return (
            abs(slot_center_x - center_x) <= max(24, int(circle.r * 1.2))
            and abs(slot_center_y - center_y) <= max(48, int(circle.r * 1.8))
        )

    @staticmethod
    def _apply_identity_match(slot: dict[str, Any], match: Any) -> None:
        identity = match.to_dict()
        identity_payload = {
            "status": identity.get("status", "unknown"),
            "identity_state": identity.get("identity_state", "unknown_identity"),
            "matched_card_id": identity.get("matched_card_id"),
            "confidence": identity.get("confidence", 0.0),
            "identity_confidence": identity.get("identity_confidence", identity.get("confidence", 0.0)),
            "display_name": identity.get("display_name", ""),
            "card_name": identity.get("card_name", identity.get("display_name", "")),
            "type": identity.get("type", identity.get("card_type", "unknown")),
            "card_type": identity.get("card_type", identity.get("type", "unknown")),
            "character_name": identity.get("character_name", "unknown"),
            "source": identity.get("source"),
            "reason": identity.get("reason"),
            "unknown_reason": identity.get("unknown_reason", ""),
            "manual_status": identity.get("manual_status", "unknown"),
            "event_skill_notes": identity.get("event_skill_notes", ""),
            "needs_manual_label": bool(identity.get("needs_manual_label")),
            "evidence_sources": identity.get("evidence_sources", []),
            "data_links": identity.get("data_links", {"events": [], "skills": [], "hints": []}),
        }
        slot["identity"] = identity_payload
        slot["identity_status"] = identity_payload["status"]
        slot["identity_state"] = identity_payload["identity_state"]
        slot["identity_confidence"] = identity_payload["identity_confidence"]
        slot["identity_unknown_reason"] = identity_payload["unknown_reason"]
        slot["matched_card_id"] = identity_payload["matched_card_id"]
        slot["identity_candidates"] = identity.get("identity_candidates", [])
        slot["character_id"] = identity.get("character_id", "unknown")
        slot["character_name"] = identity_payload["character_name"]
        slot["card_id"] = identity.get("card_id")
        slot["display_name"] = identity.get("display_name", "")
        slot["card_name"] = identity_payload["card_name"]
        slot["identity_display_name"] = identity.get("display_name", "")
        slot["identity_card_type"] = identity_payload["card_type"]
        slot["identity_source"] = identity.get("source")
        slot["identity_reason"] = identity.get("reason")
        slot["support_card"] = identity.get("support_card", {})
        slot["matched_template_path"] = identity.get("matched_template_path")
        slot["support_bonus_summary"] = identity.get("support_bonus_summary", {})
        slot["training_bonus_summary"] = identity.get("training_bonus_summary", {})
        slot["event_hooks"] = identity.get("event_hooks", [])
        slot["skill_hint_tags"] = identity.get("skill_hint_tags", [])
        slot["template_path"] = identity.get("template_path")
        slot["avatar_template_path"] = identity.get("avatar_template_path")
        slot["manual_status"] = identity_payload["manual_status"]
        slot["event_skill_notes"] = identity_payload["event_skill_notes"]
        slot["needs_manual_label"] = identity_payload["needs_manual_label"]
        slot["evidence_sources"] = identity_payload["evidence_sources"]
        slot["data_links"] = identity_payload["data_links"]
        slot["entity_kind"] = identity.get("entity_kind", "unknown_portrait")
        slot["is_effective_support"] = bool(identity.get("is_effective_support")) and (
            identity_payload["status"] == "known" and bool(identity_payload["matched_card_id"])
        )
        slot["confidence"] = round(max(slot.get("confidence", 0.0), identity_payload["confidence"]), 3)

    def _write_debug_artifacts(
        self,
        *,
        image: np.ndarray,
        roi: tuple[int, int, int, int],
        roi_image: np.ndarray,
        circles: list[_CircleCandidate],
        bars: list[_BarCandidate],
        result: dict[str, Any],
        debug_output_dir: Path,
    ) -> None:
        debug_output_dir.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(debug_output_dir / "roi.png"), roi_image)

        overlay = roi_image.copy()
        for circle in circles:
            cv2.circle(overlay, (circle.cx, circle.cy), circle.r, (0, 180, 255), 2)
        for bar in bars:
            cv2.rectangle(overlay, (bar.x1, bar.y1), (bar.x2, bar.y2), (0, 255, 0), 2)
        cv2.imwrite(str(debug_output_dir / "roi_overlay.png"), overlay)

        for slot in result.get("support_slots", []):
            bbox = slot.get("bbox")
            if not isinstance(bbox, list) or len(bbox) != 4:
                continue
            x1, y1, x2, y2 = [int(v) for v in bbox]
            crop = image[max(0, y1):max(0, y2), max(0, x1):max(0, x2)]
            if crop.size == 0:
                continue
            cv2.imwrite(str(debug_output_dir / f"slot_{slot.get('slot', 0)}_avatar.png"), crop)

            hint_bbox = slot.get("hint_badge_bbox")
            if not slot.get("hint_badge") or not isinstance(hint_bbox, list) or len(hint_bbox) != 4:
                continue
            hx1, hy1, hx2, hy2 = [int(v) for v in hint_bbox]
            hint_crop = image[max(0, hy1):max(0, hy2), max(0, hx1):max(0, hx2)]
            if hint_crop.size == 0:
                continue
            hint_crop_path = (debug_output_dir / f"slot_{slot.get('slot', 0)}_hint_badge.png").resolve()
            cv2.imwrite(str(hint_crop_path), hint_crop)
            slot["hint_badge_crop_path"] = str(hint_crop_path)
            evidence = slot.get("hint_badge_evidence")
            if isinstance(evidence, dict):
                evidence["crop_path"] = str(hint_crop_path)

        manifest = {
            "image": result.get("image", ""),
            "roi": list(roi),
            "support_count": result.get("support_count", 0),
            "hint_count": result.get("hint_count", 0),
            "circles": [
                {
                    "cx": circle.cx,
                    "cy": circle.cy,
                    "r": circle.r,
                    "band_score": round(circle.band_score, 3),
                }
                for circle in circles
            ],
            "bars": [
                {
                    "x1": bar.x1,
                    "y1": bar.y1,
                    "x2": bar.x2,
                    "y2": bar.y2,
                    "width": bar.width,
                    "area": bar.area,
                }
                for bar in bars
            ],
            "support_slots": result.get("support_slots", []),
        }
        (debug_output_dir / "debug_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _make_slot(
        self,
        roi_image: np.ndarray,
        circle: _CircleCandidate,
        bar: _BarCandidate,
        roi: tuple[int, int, int, int],
        *,
        bond_bar_source: str,
    ) -> dict[str, Any]:
        avatar_bbox_roi = [
            max(0, circle.cx - circle.r),
            max(0, circle.cy - circle.r),
            min(roi_image.shape[1], circle.cx + circle.r),
            min(roi_image.shape[0], circle.cy + circle.r),
        ]
        badge = self._detect_badge(roi_image, circle)
        legacy_bond_bar_visual = self._summarize_bond_bar(roi_image, bar)
        bond_bar = measure_bond_bar(
            roi_image,
            (bar.x1, bar.y1, bar.x2, bar.y2),
            source=bond_bar_source,
        )
        confidence = 0.55
        if legacy_bond_bar_visual:
            confidence += 0.2
        if badge["badge_kind"] != "none":
            confidence += 0.1
        if circle.band_score > 0.55:
            confidence += 0.1
        avatar_bbox = self._map_bbox_to_original(avatar_bbox_roi, roi)
        hint_badge_bbox_roi = self._normalize_badge_bbox(
            badge.get("badge_bbox_roi"),
            roi_image.shape[1],
            roi_image.shape[0],
        )
        hint_badge_bbox = (
            self._map_bbox_to_original(hint_badge_bbox_roi, roi)
            if badge["hint_badge"] and hint_badge_bbox_roi is not None
            else None
        )
        hint_badge_evidence = (
            {
                "bbox": hint_badge_bbox,
                "bbox_roi": hint_badge_bbox_roi,
                "crop_path": None,
                "kind": badge["badge_kind"],
                "reason": badge["badge_reason"],
            }
            if badge["hint_badge"] and hint_badge_bbox_roi is not None
            else None
        )
        portrait_visual_evidence = self._portrait_visual_evidence(
            roi_image,
            avatar_bbox_roi,
        )
        return {
            "slot": 0,
            "bbox": avatar_bbox,
            "bbox_roi": avatar_bbox_roi,
            "character_id": "unknown",
            "character_name": "unknown",
            "card_id": None,
            "identity": {"status": "unknown", "matched_card_id": None, "confidence": 0.0},
            "identity_status": "unknown",
            "identity_state": "unknown_identity",
            "matched_card_id": None,
            "identity_candidates": [],
            "identity_display_name": "",
            "identity_card_type": "unknown",
            "evidence_sources": [],
            "data_links": {"events": [], "skills": [], "hints": []},
            "support_bonus_summary": {},
            "training_bonus_summary": {},
            "event_hooks": [],
            "skill_hint_tags": [],
            "template_path": None,
            "avatar_template_path": None,
            "confidence": round(min(confidence, 0.98), 3),
            "bond_bar": bond_bar,
            "bond_bar_source": bond_bar_source,
            "legacy_bond_bar_visual": legacy_bond_bar_visual,
            "hint_badge": badge["hint_badge"],
            "event_or_skill_hint_possible": badge["hint_badge"],
            "hint_badge_bbox": hint_badge_bbox,
            "hint_badge_bbox_roi": hint_badge_bbox_roi if badge["hint_badge"] else None,
            "hint_badge_crop_path": None,
            "hint_badge_evidence": hint_badge_evidence,
            "badge_kind": badge["badge_kind"],
            "badge_reason": badge["badge_reason"],
            "portrait_visual_evidence": portrait_visual_evidence,
            "source": "cv_heuristic",
        }

    @staticmethod
    def _portrait_visual_evidence(
        roi_image: np.ndarray,
        bbox: list[int],
    ) -> dict[str, Any]:
        x1, y1, x2, y2 = bbox
        crop = roi_image[y1:y2, x1:x2]
        if crop.size == 0:
            return {
                "accepted": False,
                "diameter_ratio": 0.0,
                "skin_fraction": 0.0,
                "luminance_std": 0.0,
            }
        height, width = crop.shape[:2]
        roi_width = roi_image.shape[1]
        diameter_ratio = min(height, width) / roi_width if roi_width > 0 else 0.0
        yy, xx = np.ogrid[:height, :width]
        radius = min(height, width) * 0.37
        mask = (
            (xx - ((width - 1) / 2.0)) ** 2
            + (yy - ((height - 1) / 2.0)) ** 2
            <= radius ** 2
        )
        if int(mask.sum()) < 16:
            return {
                "accepted": False,
                "diameter_ratio": 0.0,
                "skin_fraction": 0.0,
                "luminance_std": 0.0,
            }
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        skin = (
            (hsv[:, :, 0] <= 25)
            & (hsv[:, :, 1] >= 25)
            & (hsv[:, :, 1] <= 190)
            & (hsv[:, :, 2] >= 100)
            & mask
        )
        skin_fraction = float(skin.sum() / mask.sum())
        luminance_std = float(gray[mask].std())
        accepted = diameter_ratio >= 0.4 and (
            skin_fraction >= 0.18
            or (skin_fraction >= 0.09 and luminance_std >= 50.0)
        )
        return {
            "accepted": bool(accepted),
            "diameter_ratio": round(diameter_ratio, 3),
            "skin_fraction": round(skin_fraction, 3),
            "luminance_std": round(luminance_std, 3),
        }

    def _detect_avatar_candidates(self, roi_image: np.ndarray) -> list[_CircleCandidate]:
        gray = cv2.cvtColor(roi_image, cv2.COLOR_BGR2GRAY)
        gray = cv2.medianBlur(gray, 5)
        scale = roi_image.shape[1] / 320.0
        slot_scale = roi_image.shape[1] / 160.0
        circles = cv2.HoughCircles(
            gray,
            cv2.HOUGH_GRADIENT,
            dp=1.2,
            minDist=max(26, int(52 * scale)),
            param1=80,
            param2=24,
            minRadius=max(14, int(36 * scale)),
            maxRadius=max(24, int(76 * scale)),
        )
        if circles is None:
            return []

        max_y = int(roi_image.shape[0] * 0.86)
        candidates: list[_CircleCandidate] = []
        for raw in np.round(circles[0]).astype(int):
            cx, cy, r = (int(raw[0]), int(raw[1]), int(raw[2]))
            if cx < int(roi_image.shape[1] * 0.34):
                continue
            if cy < int(max(48, 70 * scale)) or cy > max_y:
                continue
            band_score = self._band_score(roi_image, cx, cy, r)
            candidates.append(_CircleCandidate(cx=cx, cy=cy, r=r, band_score=band_score))

        candidates.sort(key=lambda item: item.cy)
        upper_limit = int(roi_image.shape[0] * 0.53)
        upper_candidates = [
            candidate for candidate in candidates
            if candidate.cy <= upper_limit
        ]
        lower = [
            candidate for candidate in candidates
            if candidate.cy > upper_limit
        ]
        deduped: list[_CircleCandidate] = []
        y_gap = int(max(58, 58 * slot_scale))
        for candidate in upper_candidates:
            if deduped and abs(candidate.cy - deduped[-1].cy) < y_gap and abs(candidate.cx - deduped[-1].cx) < y_gap:
                candidate_score = (
                    candidate.r,
                    candidate.cx,
                    candidate.band_score,
                )
                current_score = (
                    deduped[-1].r,
                    deduped[-1].cx,
                    deduped[-1].band_score,
                )
                if candidate_score > current_score:
                    deduped[-1] = candidate
                continue
            deduped.append(candidate)
        upper = deduped
        if len(upper) < 3:
            return upper

        # Lower roster slots are admitted only as a continuation of an
        # already-observed three-slot vertical lattice.  This recovers real
        # fourth/fifth portraits without reopening the broad lower-panel
        # circle false positives that motivated the old hard cutoff.
        recent_centers = [candidate.cy for candidate in upper[-3:]]
        spacings = np.diff(np.asarray(recent_centers, dtype=np.float32))
        expected_spacing = float(np.median(spacings))
        if not (
            80.0 * slot_scale
            <= expected_spacing
            <= 145.0 * slot_scale
            and float(np.max(np.abs(spacings - expected_spacing)))
            <= 18.0 * slot_scale
        ):
            return upper
        roster_x = float(np.median([candidate.cx for candidate in upper[-3:]]))
        admitted = list(upper)
        next_y = admitted[-1].cy + expected_spacing
        remaining = list(lower)
        for _ in range(2):
            eligible = [
                candidate
                for candidate in remaining
                if candidate.band_score >= 0.42
                and abs(candidate.cx - roster_x) <= 40.0 * slot_scale
                and abs(candidate.cy - next_y) <= 32.0 * slot_scale
                and self._portrait_visual_evidence(
                    roi_image,
                    [
                        max(0, candidate.cx - candidate.r),
                        max(0, candidate.cy - candidate.r),
                        min(roi_image.shape[1], candidate.cx + candidate.r),
                        min(roi_image.shape[0], candidate.cy + candidate.r),
                    ],
                )["accepted"]
            ]
            if not eligible:
                break
            candidate = min(
                eligible,
                key=lambda item: (
                    abs(item.cy - next_y),
                    -item.band_score,
                    -item.r,
                ),
            )
            admitted.append(candidate)
            remaining = [
                item
                for item in remaining
                if abs(item.cy - candidate.cy) > 32.0 * slot_scale
            ]
            next_y = candidate.cy + expected_spacing
        return admitted

    def _detect_bond_bars(self, roi_image: np.ndarray) -> list[_BarCandidate]:
        search_h = int(min(roi_image.shape[0], roi_image.shape[0] * 0.58))
        x_start = int(roi_image.shape[1] * 0.16)
        x_end = int(roi_image.shape[1] * 0.94)
        region = roi_image[:search_h, x_start:x_end]
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
        mask = (((gray < 210) & (hsv[:, :, 1] > 20)) | (gray < 120)).astype(np.uint8) * 255
        kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (max(15, int(roi_image.shape[1] * 0.16)), max(3, int(roi_image.shape[1] * 0.02))),
        )
        opened = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        count, _, stats, _ = cv2.connectedComponentsWithStats(opened, 8)
        bars: list[_BarCandidate] = []
        for index in range(1, count):
            x, y, w, h, area = [int(v) for v in stats[index]]
            if w < int(roi_image.shape[1] * 0.22) or w > int(roi_image.shape[1] * 0.62):
                continue
            if h < max(6, int(roi_image.shape[1] * 0.035)) or h > max(22, int(roi_image.shape[1] * 0.16)):
                continue
            if y < int(roi_image.shape[0] * 0.17) or y > int(roi_image.shape[0] * 0.52):
                continue
            bars.append(
                _BarCandidate(
                    x1=x + x_start,
                    y1=y,
                    x2=x + x_start + w,
                    y2=y + h,
                    width=w,
                    area=area,
                )
            )

        bars.sort(key=lambda item: item.y1)
        deduped: list[_BarCandidate] = []
        slot_scale = roi_image.shape[1] / 160.0
        y_gap = int(max(44, 52 * slot_scale))
        for bar in bars:
            if deduped and abs(bar.y1 - deduped[-1].y1) < y_gap:
                if bar.area > deduped[-1].area:
                    deduped[-1] = bar
                continue
            deduped.append(bar)
        return deduped

    def _infer_bond_bar(self, roi_image: np.ndarray, circle: _CircleCandidate) -> _BarCandidate:
        y1 = min(roi_image.shape[0] - 1, circle.cy + circle.r + 2)
        y2 = min(roi_image.shape[0], y1 + max(8, int(circle.r * 0.42)))
        x1 = max(0, circle.cx - circle.r - 10)
        x2 = min(roi_image.shape[1], circle.cx + circle.r + 12)
        return _BarCandidate(x1=x1, y1=y1, x2=x2, y2=y2, width=x2 - x1, area=(x2 - x1) * max(1, y2 - y1))

    def _summarize_bond_bar(self, roi_image: np.ndarray, bar: _BarCandidate) -> dict[str, Any]:
        x1, y1, x2, y2 = bar.x1, bar.y1, bar.x2, bar.y2
        patch = roi_image[max(0, y1):min(roi_image.shape[0], y2), max(0, x1):min(roi_image.shape[1], x2)]
        if patch.size == 0:
            return {
                "bbox": [x1, y1, x2, y2],
                "status": "unknown",
                "ratio": 0.0,
                "color_class": "unknown",
                "confidence": 0.0,
            }
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        colored = ((hsv[:, :, 1] > 45) & (hsv[:, :, 2] > 70)).mean()
        bright = ((hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 185)).mean()
        if bright > 0.55 and colored > 0.35:
            status = "friendship"
            color_class = "bright_colored"
            confidence = min(0.99, 0.65 + (bright - 0.55) + (colored - 0.35))
        elif colored > 0.42:
            status = "high"
            color_class = "colored"
            confidence = min(0.9, 0.55 + (colored - 0.42))
        elif colored > 0.22:
            status = "medium"
            color_class = "colored"
            confidence = min(0.8, 0.45 + (colored - 0.22))
        elif (gray < 215).mean() > 0.18:
            status = "low"
            color_class = "dark_or_desaturated"
            confidence = 0.35
        else:
            status = "unknown"
            color_class = "desaturated"
            confidence = 0.0
        provisional_status = status
        if confidence < 0.6:
            status = "unknown"
        return {
            "bbox": [x1, y1, x2, y2],
            "status": status,
            "provisional_status": provisional_status,
            "ratio": round(float(colored), 3),
            "color_class": color_class,
            "confidence": round(float(confidence), 3),
        }

    def _detect_badge(self, roi_image: np.ndarray, circle: _CircleCandidate) -> dict[str, Any]:
        left_badge = self._detect_badge_region(roi_image, circle, side="left")
        right_badge = self._detect_badge_region(roi_image, circle, side="right")

        split_like_same_badge = (
            left_badge["badge_kind"] == "hint_icon"
            and right_badge["badge_kind"] == "hint_exclamation"
            and left_badge.get("_dominant_hue") is not None
            and right_badge.get("_dominant_hue") is not None
            and abs(int(left_badge["_dominant_hue"]) - int(right_badge["_dominant_hue"])) <= 6
            and left_badge.get("_component_top") is not None
            and right_badge.get("_component_top") is not None
            and abs(int(left_badge["_component_top"]) - int(right_badge["_component_top"])) <= max(10, int(circle.r * 0.18))
            and int(left_badge["_component_top"]) <= (circle.cy - circle.r - max(12, int(circle.r * 0.2)))
            and int(right_badge["_component_top"]) <= (circle.cy - circle.r - max(12, int(circle.r * 0.2)))
            and self._is_header_band_circle(roi_image, circle)
            and right_badge.get("_component_left") is not None
        )
        if split_like_same_badge:
            large_header_artifact = (circle.r / max(float(roi_image.shape[1]), 1.0)) >= 0.18
            if (
                large_header_artifact
                and int(right_badge["_component_left"]) <= circle.cx + max(8, int(circle.r * 0.25))
            ):
                return {
                    "hint_badge": False,
                    "badge_kind": "none",
                    "badge_reason": "split_header_badge_false_positive",
                }
            return left_badge

        if right_badge["badge_kind"] == "hint_exclamation":
            reason_parts = [right_badge["badge_reason"]]
            if left_badge["badge_kind"] != "none":
                reason_parts.append(left_badge["badge_reason"])
            return {
                "hint_badge": True,
                "badge_kind": "hint_exclamation",
                "badge_reason": ";".join(part for part in reason_parts if part),
                "badge_bbox_roi": right_badge.get("badge_bbox_roi"),
            }
        if left_badge["badge_kind"] != "none":
            if (
                left_badge["badge_kind"] == "hint_icon"
                and left_badge.get("_component_top") is not None
                and left_badge.get("_dominant_hue") is not None
                and 120 <= int(left_badge["_dominant_hue"]) <= 155
                and self._is_header_band_circle(roi_image, circle)
                and int(left_badge["_component_top"]) < circle.cy - circle.r - max(12, int(circle.r * 0.2))
            ):
                return {
                    "hint_badge": False,
                    "badge_kind": "none",
                    "badge_reason": "top_header_badge_false_positive",
                }
            return left_badge
        if right_badge["badge_kind"] != "none":
            return right_badge
        return {
            "hint_badge": False,
            "badge_kind": "none",
            "badge_reason": "no_badge_detected",
        }

    @staticmethod
    def _is_header_band_circle(roi_image: np.ndarray, circle: _CircleCandidate) -> bool:
        return circle.cy < int(roi_image.shape[0] * 0.22)

    def _detect_badge_region(
        self,
        roi_image: np.ndarray,
        circle: _CircleCandidate,
        *,
        side: str,
    ) -> dict[str, Any]:
        scale = roi_image.shape[1] / 320.0
        if side == "left":
            x1 = max(0, circle.cx - circle.r - int(24 * scale))
            x2 = min(roi_image.shape[1], circle.cx + circle.r // 3)
            y1 = max(0, circle.cy - circle.r - int(20 * scale))
            y2 = min(roi_image.shape[0], circle.cy + circle.r // 3)
        else:
            x1 = max(0, circle.cx - circle.r // 3)
            x2 = min(roi_image.shape[1], circle.cx + circle.r + int(28 * scale))
            y1 = max(0, circle.cy - circle.r - int(24 * scale))
            y2 = min(roi_image.shape[0], circle.cy + circle.r // 4)

        patch = roi_image[y1:y2, x1:x2]
        if patch.size == 0:
            return {
                "hint_badge": False,
                "badge_kind": "none",
                "badge_reason": f"{side}_empty_patch",
            }

        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        yy, xx = np.mgrid[y1:y2, x1:x2]
        dist = np.sqrt((xx - circle.cx) ** 2 + (yy - circle.cy) ** 2)
        outside_circle = dist >= (circle.r * 0.82)
        side_mask = (xx <= circle.cx) & (yy <= circle.cy) if side == "left" else (xx >= circle.cx) & (yy <= circle.cy)
        mask = (
            (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 110)
            & (gray < 245)
            & outside_circle
            & side_mask
        ).astype(np.uint8) * 255
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        largest = 0
        largest_index = -1
        for index in range(1, count):
            area = int(stats[index, cv2.CC_STAT_AREA])
            if area > largest:
                largest = area
                largest_index = index
        area_threshold = max(60, int(1200 * scale * scale))
        if largest < area_threshold or largest_index < 0:
            return {
                "hint_badge": False,
                "badge_kind": "none",
                "badge_reason": f"{side}_below_area_threshold:{largest}",
            }

        component = labels == largest_index
        hue_mask = component & (hsv[:, :, 1] > 120) & (hsv[:, :, 2] > 120)
        if not np.any(hue_mask):
            return {
                "hint_badge": False,
                "badge_kind": "none",
                "badge_reason": f"{side}_no_colored_component",
                "_dominant_hue": None,
                "_component_top": None,
                "_component_left": None,
            }

        dominant_hue = int(np.bincount(hsv[:, :, 0][hue_mask], minlength=180).argmax())
        component_top = int(y1 + stats[largest_index, cv2.CC_STAT_TOP])
        component_left = int(x1 + stats[largest_index, cv2.CC_STAT_LEFT])
        component_bbox_roi = [
            int(x1 + stats[largest_index, cv2.CC_STAT_LEFT]),
            int(y1 + stats[largest_index, cv2.CC_STAT_TOP]),
            int(x1 + stats[largest_index, cv2.CC_STAT_LEFT] + stats[largest_index, cv2.CC_STAT_WIDTH]),
            int(y1 + stats[largest_index, cv2.CC_STAT_TOP] + stats[largest_index, cv2.CC_STAT_HEIGHT]),
        ]
        if side == "left":
            if dominant_hue <= 6:
                return {
                    "hint_badge": False,
                    "badge_kind": "bond_heart",
                    "badge_reason": f"left_top_warm_heart_like:h{dominant_hue}",
                    "badge_bbox_roi": component_bbox_roi,
                    "_dominant_hue": dominant_hue,
                    "_component_top": component_top,
                    "_component_left": component_left,
                }
            return {
                "hint_badge": True,
                "badge_kind": "hint_icon",
                "badge_reason": f"left_top_colored_hint_like:h{dominant_hue}",
                "badge_bbox_roi": component_bbox_roi,
                "_dominant_hue": dominant_hue,
                "_component_top": component_top,
                "_component_left": component_left,
            }

        return {
            "hint_badge": True,
            "badge_kind": "hint_exclamation",
            "badge_reason": f"right_top_hint_like:h{dominant_hue}",
            "badge_bbox_roi": component_bbox_roi,
            "_dominant_hue": dominant_hue,
            "_component_top": component_top,
            "_component_left": component_left,
        }

    @staticmethod
    def _map_bbox_to_original(
        bbox_roi: list[int],
        roi: tuple[int, int, int, int],
    ) -> list[int]:
        return [
            int(bbox_roi[0] + roi[0]),
            int(bbox_roi[1] + roi[1]),
            int(bbox_roi[2] + roi[0]),
            int(bbox_roi[3] + roi[1]),
        ]

    @staticmethod
    def _normalize_badge_bbox(raw_bbox: Any, width: int, height: int) -> list[int] | None:
        if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
            return None
        try:
            x1, y1, x2, y2 = [int(value) for value in raw_bbox]
        except (TypeError, ValueError):
            return None
        x1 = max(0, min(width, x1))
        x2 = max(0, min(width, x2))
        y1 = max(0, min(height, y1))
        y2 = max(0, min(height, y2))
        if x2 <= x1 or y2 <= y1:
            return None
        return [x1, y1, x2, y2]

    @staticmethod
    def _collect_identity_candidates(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for slot_index, slot in enumerate(slots):
            slot_number = slot.get("slot", slot_index)
            raw_candidates = slot.get("identity_candidates")
            if isinstance(raw_candidates, list) and raw_candidates:
                for raw_candidate in raw_candidates:
                    if not isinstance(raw_candidate, dict):
                        continue
                    candidate = dict(raw_candidate)
                    candidate["slot"] = slot_number
                    if "identity_confidence" not in candidate:
                        candidate["identity_confidence"] = candidate.get("confidence", 0.0)
                    if "evidence_sources" not in candidate:
                        candidate["evidence_sources"] = slot.get("evidence_sources", [])
                    if "data_links" not in candidate:
                        candidate["data_links"] = slot.get("data_links", {"events": [], "skills": [], "hints": []})
                    if "character_name" not in candidate:
                        candidate["character_name"] = slot.get("character_name", "unknown")
                    candidates.append(candidate)
                continue

            identity = slot.get("identity")
            if not isinstance(identity, dict):
                identity = {}
            candidates.append(
                {
                    "slot": slot_number,
                    "card_id": slot.get("card_id"),
                    "matched_card_id": slot.get("matched_card_id"),
                    "character_id": slot.get("character_id", "unknown"),
                    "character_name": slot.get("character_name", "unknown"),
                    "display_name": slot.get("identity_display_name", ""),
                    "type": slot.get("identity_card_type", "unknown"),
                    "card_type": slot.get("identity_card_type", "unknown"),
                    "confidence": identity.get("confidence", slot.get("confidence", 0.0)),
                    "identity_confidence": identity.get("identity_confidence", slot.get("identity_confidence", 0.0)),
                    "status": identity.get("status", slot.get("identity_status", "unknown")),
                    "identity_state": identity.get("identity_state", slot.get("identity_state", "unknown_identity")),
                    "reason": identity.get("reason", slot.get("identity_reason")),
                    "unknown_reason": identity.get("unknown_reason", slot.get("identity_unknown_reason")),
                    "source": identity.get("source", slot.get("identity_source")),
                    "evidence_sources": identity.get("evidence_sources", slot.get("evidence_sources", [])),
                    "data_links": identity.get("data_links", slot.get("data_links", {"events": [], "skills": [], "hints": []})),
                    "manual_status": identity.get("manual_status", slot.get("manual_status", "unknown")),
                    "event_skill_notes": identity.get("event_skill_notes", slot.get("event_skill_notes", "")),
                    "needs_manual_label": bool(identity.get("needs_manual_label", slot.get("needs_manual_label", False))),
                }
            )
        return candidates

    @staticmethod
    def _band_score(roi_image: np.ndarray, cx: int, cy: int, r: int) -> float:
        y1 = min(roi_image.shape[0], cy + r + 2)
        y2 = min(roi_image.shape[0], y1 + max(12, int(r * 0.55)))
        x1 = max(0, cx - r - 10)
        x2 = min(roi_image.shape[1], cx + r + 24)
        band = roi_image[y1:y2, x1:x2]
        if band.size == 0:
            return 0.0
        hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
        mask = (((hsv[:, :, 1] > 30) & (gray < 230)) | (gray < 160)).astype(np.uint8)
        return float(mask.mean())
