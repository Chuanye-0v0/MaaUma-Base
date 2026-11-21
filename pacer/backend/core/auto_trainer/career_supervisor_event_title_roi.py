"""Offline-only localization for observed event-title banners.

This module deliberately does not identify an event, inspect options, or invoke
an executor.  It only converts a verified 720x1280 source frame into the
caller-required normalized title ROI, or abstains.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import cv2
import numpy as np

from core.apk_reference import RoiBinding


_FRAME_SIZE = (1280, 720)
_CANONICAL_SIZE = (295, 32)
_NATIVE_CROP_X = 6
_NATIVE_CROP_Y = 8
_PINK_ANCHOR_ID = "event_title_banner.pink"
_PINK_TRANSFORM_ID = (
    "uma.event_title_roi.pink_banner_720x1280_native_x6_y8_w295_h32.v2"
)
_COLORED_TEXT_ANCHOR_ID = "event_title_banner.colored_text"
_COLORED_TEXT_TRANSFORM_ID = (
    "uma.event_title_roi.verified_colored_banner_720x1280_"
    "native_x115_y244_w295_h32.v1"
)
_TITLE_STRIP_X0 = 109
_TITLE_STRIP_Y0 = 236
_TITLE_STRIP_HEIGHT = 50
_TITLE_STRIP_WIDTH = 456
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class EventTitleRoiLocalization:
    """A non-actionable, in-memory result of offline title-ROI localization."""

    status: str
    abstain_reason: str | None
    binding: RoiBinding | None
    source_frame_bbox: tuple[int, int, int, int] | None
    roi_bgr: np.ndarray | None
    crop_sha256: str | None

    @property
    def action_eligible(self) -> bool:
        return False

    @property
    def production_ready(self) -> bool:
        return False

    @property
    def usable_for_action(self) -> bool:
        return False

    def to_dict(self) -> dict[str, object]:
        """Expose provenance only; never serialize the image crop itself."""

        result: dict[str, object] = {
            "schema_version": "uma.event_title_roi.v1",
            "status": self.status,
            "action_eligible": False,
            "production_ready": False,
            "usable_for_action": False,
        }
        if self.abstain_reason is not None:
            result["abstain_reason"] = self.abstain_reason
        if self.binding is not None:
            result["anchor_id"] = self.binding.anchor_id
            result["source_frame_bbox"] = self.source_frame_bbox
            result["canonical_size"] = self.binding.canonical_size
            result["transform_id"] = self.binding.transform_id
            result["source_frame_sha256"] = self.binding.source_frame_sha256
        if self.crop_sha256 is not None:
            result["crop_sha256"] = self.crop_sha256
        return result


def localize_offline_event_title_roi(
    frame_bgr: np.ndarray,
    *,
    source_frame_sha256: str,
) -> EventTitleRoiLocalization:
    """Locate one complete supported title banner or fail closed.

    Existing pink fixtures retain their connected-component anchor.  Blue
    support-event and green main-story banners use a title-glyph sequence plus
    a concentrated colored-background check.  The frozen native 295x32
    fingerprint window is always copied without resize or interpolation.
    """

    if not _is_valid_frame(frame_bgr):
        return _abstain("unsupported_frame_dimensions")
    if not isinstance(source_frame_sha256, str) or not _SHA256_PATTERN.fullmatch(
        source_frame_sha256
    ):
        return _abstain("invalid_source_frame_sha256")

    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    pink_mask = cv2.inRange(hsv, (155, 90, 100), (179, 255, 255))
    candidates, coarse_seen = _title_banner_candidates(pink_mask)
    if len(candidates) > 1:
        return _abstain("candidate_ambiguous")
    if candidates:
        anchor_id = _PINK_ANCHOR_ID
        transform_id = _PINK_TRANSFORM_ID
    else:
        extended_pink_candidates = _extended_pink_title_banner_candidates(pink_mask)
        if len(extended_pink_candidates) > 1:
            return _abstain("candidate_ambiguous")
        colored_candidate = (
            extended_pink_candidates[0]
            if extended_pink_candidates
            else _colored_title_banner_candidate(hsv)
        )
        if colored_candidate is None:
            return _abstain(
                "candidate_geometry_rejected"
                if coarse_seen
                else "title_banner_not_found"
            )
        candidates = [colored_candidate]
        anchor_id = _COLORED_TEXT_ANCHOR_ID
        transform_id = _COLORED_TEXT_TRANSFORM_ID

    x0, y0, x1, y1 = candidates[0]
    frame_height, frame_width = frame_bgr.shape[:2]
    if (
        x0 < 0
        or y0 < 0
        or x1 <= x0
        or y1 <= y0
        or x1 > frame_width
        or y1 > frame_height
    ):
        return _abstain("source_banner_out_of_bounds")
    source_banner = frame_bgr[y0:y1, x0:x1]
    if source_banner.shape[0] != 50 or not 440 <= source_banner.shape[1] <= 470:
        return _abstain("source_banner_dimensions_unsupported")
    crop_x1 = _NATIVE_CROP_X + _CANONICAL_SIZE[0]
    crop_y1 = _NATIVE_CROP_Y + _CANONICAL_SIZE[1]
    if crop_x1 > source_banner.shape[1] or crop_y1 > source_banner.shape[0]:
        return _abstain("native_crop_out_of_bounds")
    roi_bgr = source_banner[
        _NATIVE_CROP_Y:crop_y1,
        _NATIVE_CROP_X:crop_x1,
    ].copy()
    if roi_bgr.shape != (32, 295, 3):
        return _abstain("crop_closure_failed")

    binding = RoiBinding(
        anchor_id=anchor_id,
        # Sparse fingerprints address the normalized 295x32 crop, while the
        # original source-frame bbox remains explicit provenance on the result.
        roi_bbox=(0, 0, *_CANONICAL_SIZE),
        canonical_size=_CANONICAL_SIZE,
        transform_id=transform_id,
        source_frame_sha256=source_frame_sha256,
    )
    return EventTitleRoiLocalization(
        status="located",
        abstain_reason=None,
        binding=binding,
        source_frame_bbox=(x0, y0, x1, y1),
        roi_bgr=roi_bgr,
        crop_sha256=hashlib.sha256(roi_bgr.tobytes()).hexdigest(),
    )


def _title_banner_candidates(mask: np.ndarray) -> tuple[list[tuple[int, int, int, int]], bool]:
    """Return complete banner candidates and whether a malformed one was seen."""

    labels, _component_map, stats, _centroids = cv2.connectedComponentsWithStats(mask)
    del labels
    candidates: list[tuple[int, int, int, int]] = []
    coarse_seen = False
    for x, y, width, height, area in stats[1:]:
        x, y, width, height, area = map(int, (x, y, width, height, area))
        # The source banner was sampled as a bounded, wide pink component.
        # This excludes the pink HUD widgets and slanted reward transitions.
        if not (90 <= x <= 130 and 90 <= y <= 215 and 70 <= height <= 95):
            continue
        if width >= 250 and area >= 5_000:
            coarse_seen = True
        if not (440 <= width <= 470 and 20_000 <= area <= 28_000):
            continue
        x0, y0, x1, y1 = x, y + height - 50, x + width, y + height
        if x0 < 0 or y0 < 0 or x1 > 720 or y1 > 1280:
            continue
        candidates.append((x0, y0, x1, y1))
    return candidates, coarse_seen


def _extended_pink_title_banner_candidates(
    mask: np.ndarray,
) -> list[tuple[int, int, int, int]]:
    """Bind decorated pink banners to the verified native database window."""

    labels, _component_map, stats, _centroids = cv2.connectedComponentsWithStats(mask)
    del labels
    candidates: list[tuple[int, int, int, int]] = []
    for x, y, width, height, area in stats[1:]:
        x, y, width, height, area = map(int, (x, y, width, height, area))
        if not (90 <= x <= 130 and 90 <= y <= 215 and 70 <= height <= 95):
            continue
        if not (471 <= width <= 520 and 20_000 <= area <= 30_000):
            continue
        candidates.append(
            (
                _TITLE_STRIP_X0,
                _TITLE_STRIP_Y0,
                _TITLE_STRIP_X0 + _TITLE_STRIP_WIDTH,
                _TITLE_STRIP_Y0 + _TITLE_STRIP_HEIGHT,
            )
        )
    return candidates


def _colored_title_banner_candidate(
    hsv: np.ndarray,
) -> tuple[int, int, int, int] | None:
    """Find one blue/green title strip from a crisp white glyph sequence."""

    text_band = hsv[246:275, 110:430]
    white_ink = (
        (text_band[:, :, 2] >= 170) & (text_band[:, :, 1] <= 170)
    )
    occupied_columns = white_ink.sum(axis=0) >= 3
    runs = _true_runs(occupied_columns, x_offset=110)
    glyph_runs = [
        (start, end)
        for start, end in runs
        if 5 <= end - start + 1 <= 45
    ]
    if len(glyph_runs) < 4:
        return None
    first_four = glyph_runs[:4]
    if first_four[-1][1] - first_four[0][0] > 125:
        return None
    if not 116 <= first_four[0][0] <= 121:
        return None

    x0 = _TITLE_STRIP_X0
    x1 = x0 + _TITLE_STRIP_WIDTH
    y0 = _TITLE_STRIP_Y0
    y1 = y0 + _TITLE_STRIP_HEIGHT
    source_band = hsv[y0:y1, x0:x1]
    colored = (source_band[:, :, 1] >= 60) & (source_band[:, :, 2] >= 70)
    if float(colored.mean()) < 0.75:
        return None
    hues = source_band[:, :, 0][colored]
    if hues.size == 0:
        return None
    hue_low, hue_mid, hue_high = np.percentile(hues, (10, 50, 90))
    if float(hue_high - hue_low) > 20.0:
        return None
    if not (30.0 <= float(hue_mid) <= 135.0):
        return None
    return x0, y0, x1, y1


def _true_runs(
    values: np.ndarray,
    *,
    x_offset: int,
) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(values.tolist()):
        if bool(value) and start is None:
            start = index
        elif not bool(value) and start is not None:
            runs.append((start + x_offset, index - 1 + x_offset))
            start = None
    if start is not None:
        runs.append((start + x_offset, len(values) - 1 + x_offset))
    return runs


def _is_valid_frame(frame_bgr: object) -> bool:
    return (
        isinstance(frame_bgr, np.ndarray)
        and frame_bgr.dtype == np.uint8
        and frame_bgr.ndim == 3
        and frame_bgr.shape == (*_FRAME_SIZE, 3)
    )


def _abstain(reason: str) -> EventTitleRoiLocalization:
    return EventTitleRoiLocalization(
        status="abstain",
        abstain_reason=reason,
        binding=None,
        source_frame_bbox=None,
        roi_bgr=None,
        crop_sha256=None,
    )
