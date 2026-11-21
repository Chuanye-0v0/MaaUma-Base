"""Generic sparse BGR ROI scorer producing action-incapable evidence."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .evidence import ReferenceEvidence


_HEX_COLOR_PATTERN = re.compile(r"[0-9A-Fa-f]{6}\Z")
_SHA256_PATTERN = re.compile(r"[0-9A-Fa-f]{64}\Z")
_MAX_BGR_DISTANCE = math.sqrt(3.0) * 255.0


class SparseFingerprintConfigError(ValueError):
    """Sparse fingerprint configuration violates its frozen contract."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SparseColorPoint:
    x: int
    y: int
    color_hex: str

    def __post_init__(self) -> None:
        if type(self.x) is not int or type(self.y) is not int:
            _config_fail("invalid_point_coordinate")
        if (
            not isinstance(self.color_hex, str)
            or _HEX_COLOR_PATTERN.fullmatch(self.color_hex) is None
        ):
            _config_fail("invalid_point_color")


@dataclass(frozen=True)
class SparseFingerprint:
    candidate_id: str
    points: tuple[SparseColorPoint, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            _config_fail("invalid_candidate_id")
        points = _snapshot_tuple(self.points, "invalid_fingerprint_points")
        if not points:
            _config_fail("empty_fingerprint_points")
        if any(not isinstance(point, SparseColorPoint) for point in points):
            _config_fail("invalid_fingerprint_points")
        object.__setattr__(self, "points", points)


@dataclass(frozen=True)
class RoiBinding:
    anchor_id: str
    roi_bbox: tuple[int, int, int, int]
    canonical_size: tuple[int, int]
    transform_id: str
    source_frame_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.anchor_id, str) or not self.anchor_id.strip():
            _config_fail("invalid_anchor_id")
        if not isinstance(self.transform_id, str) or not self.transform_id.strip():
            _config_fail("invalid_transform_id")
        if not isinstance(self.source_frame_sha256, str):
            _config_fail("invalid_source_frame_sha256")

        roi_bbox = _snapshot_tuple(self.roi_bbox, "invalid_roi_bbox")
        canonical_size = _snapshot_tuple(
            self.canonical_size, "invalid_canonical_size"
        )
        if (
            len(roi_bbox) != 4
            or any(type(value) is not int for value in roi_bbox)
            or any(value < 0 for value in roi_bbox)
            or roi_bbox[2] <= roi_bbox[0]
            or roi_bbox[3] <= roi_bbox[1]
        ):
            _config_fail("invalid_roi_bbox")
        if (
            len(canonical_size) != 2
            or any(type(value) is not int for value in canonical_size)
            or any(value <= 0 for value in canonical_size)
        ):
            _config_fail("invalid_canonical_size")
        object.__setattr__(self, "roi_bbox", roi_bbox)
        object.__setattr__(self, "canonical_size", canonical_size)


class SparseFingerprintMatcher:
    def __init__(
        self,
        *,
        candidates: Iterable[SparseFingerprint],
        acceptance_threshold: float | int,
        margin_threshold: float | int,
        matcher_version: str,
        source_database_sha256: str,
        allowed_transform_ids: Iterable[str],
    ):
        candidate_snapshot = _snapshot_iterable(
            candidates, "invalid_fingerprint_candidates"
        )
        if not candidate_snapshot:
            _config_fail("no_fingerprint_candidates")
        if any(
            not isinstance(candidate, SparseFingerprint)
            for candidate in candidate_snapshot
        ):
            _config_fail("invalid_fingerprint_candidates")
        candidate_ids = [candidate.candidate_id for candidate in candidate_snapshot]
        if len(set(candidate_ids)) != len(candidate_ids):
            _config_fail("duplicate_candidate_id")

        if not isinstance(matcher_version, str) or not matcher_version.strip():
            _config_fail("invalid_matcher_version")
        if (
            not isinstance(source_database_sha256, str)
            or _SHA256_PATTERN.fullmatch(source_database_sha256) is None
        ):
            _config_fail("invalid_source_database_sha256")

        transform_snapshot = _snapshot_iterable(
            allowed_transform_ids, "invalid_allowed_transform_ids"
        )
        if (
            not transform_snapshot
            or any(
                not isinstance(transform_id, str) or not transform_id.strip()
                for transform_id in transform_snapshot
            )
        ):
            _config_fail("invalid_allowed_transform_ids")

        self._candidates = candidate_snapshot
        self._acceptance_threshold = _validated_threshold(
            acceptance_threshold, "invalid_acceptance_threshold"
        )
        self._margin_threshold = _validated_threshold(
            margin_threshold, "invalid_margin_threshold"
        )
        self._matcher_version = matcher_version
        self._source_database_sha256 = source_database_sha256
        self._allowed_transform_ids = frozenset(transform_snapshot)

    def match(self, roi_bgr: np.ndarray, binding: RoiBinding) -> ReferenceEvidence:
        if not isinstance(binding, RoiBinding):
            return self._minimal_abstain("invalid_roi_binding")
        if (
            binding.roi_bbox[2] - binding.roi_bbox[0]
            != binding.canonical_size[0]
            or binding.roi_bbox[3] - binding.roi_bbox[1]
            != binding.canonical_size[1]
        ):
            return self._minimal_abstain(
                "roi_bbox_canonical_size_mismatch"
            )
        if _SHA256_PATTERN.fullmatch(binding.source_frame_sha256) is None:
            return self._minimal_abstain("invalid_source_frame_sha256")
        if binding.transform_id not in self._allowed_transform_ids:
            return self._minimal_abstain("unknown_transform_id")

        if not isinstance(roi_bgr, np.ndarray):
            return self._bound_abstain("invalid_roi_array", binding)
        if roi_bgr.dtype != np.uint8:
            return self._bound_abstain("invalid_roi_dtype", binding)
        if roi_bgr.ndim != 3 or roi_bgr.shape[2] != 3:
            return self._bound_abstain("invalid_roi_shape", binding)
        height, width = roi_bgr.shape[:2]
        if binding.canonical_size != (width, height):
            return self._bound_abstain(
                "roi_canonical_size_mismatch", binding
            )

        if any(
            point.x < 0
            or point.y < 0
            or point.x >= width
            or point.y >= height
            for candidate in self._candidates
            for point in candidate.points
        ):
            return self._bound_abstain(
                "fingerprint_point_out_of_bounds", binding
            )

        scores = [
            (self._score_candidate(roi_bgr, candidate), candidate.candidate_id)
            for candidate in self._candidates
        ]
        scores.sort(key=lambda item: (-item[0], item[1]))
        top_score, top_candidate_id = scores[0]
        runner_up_margin = (
            1.0
            if len(scores) == 1
            else float(np.clip(top_score - scores[1][0], 0.0, 1.0))
        )
        evidence_fields = self._evidence_fields(binding)

        if top_score < self._acceptance_threshold:
            return ReferenceEvidence.abstain(
                "fingerprint_score_below_threshold",
                candidate_id=top_candidate_id,
                score=top_score,
                runner_up_margin=runner_up_margin,
                **evidence_fields,
            )
        if runner_up_margin < self._margin_threshold:
            return ReferenceEvidence.ambiguous(
                candidate_id=top_candidate_id,
                score=top_score,
                runner_up_margin=runner_up_margin,
                reasons=("fingerprint_margin_below_threshold",),
                **evidence_fields,
            )
        return ReferenceEvidence.matched(
            candidate_id=top_candidate_id,
            score=top_score,
            runner_up_margin=runner_up_margin,
            **evidence_fields,
        )

    @staticmethod
    def _score_candidate(
        roi_bgr: np.ndarray, candidate: SparseFingerprint
    ) -> float:
        x_coordinates = np.fromiter(
            (point.x for point in candidate.points), dtype=np.intp
        )
        y_coordinates = np.fromiter(
            (point.y for point in candidate.points), dtype=np.intp
        )
        sampled_bgr = roi_bgr[y_coordinates, x_coordinates].astype(
            np.float64, copy=False
        )
        expected_bgr = np.asarray(
            [_rgb_hex_to_bgr(point.color_hex) for point in candidate.points],
            dtype=np.float64,
        )
        distances = np.linalg.norm(sampled_bgr - expected_bgr, axis=1)
        similarity = 1.0 - float(np.mean(distances / _MAX_BGR_DISTANCE))
        return float(np.clip(similarity, 0.0, 1.0))

    def _minimal_abstain(self, reason: str) -> ReferenceEvidence:
        return ReferenceEvidence.abstain(
            reason,
            matcher_version=self._matcher_version,
        )

    def _bound_abstain(
        self,
        reason: str,
        binding: RoiBinding,
        *,
        candidate_id: str | None = None,
        score: float | None = None,
        runner_up_margin: float | None = None,
    ) -> ReferenceEvidence:
        return ReferenceEvidence.abstain(
            reason,
            candidate_id=candidate_id,
            score=score,
            runner_up_margin=runner_up_margin,
            **self._evidence_fields(binding),
        )

    def _evidence_fields(self, binding: RoiBinding) -> dict[str, Any]:
        return {
            "anchor_id": binding.anchor_id,
            "roi_bbox": binding.roi_bbox,
            "canonical_size": binding.canonical_size,
            "transform_id": binding.transform_id,
            "source_database_sha256": self._source_database_sha256,
            "source_frame_sha256": binding.source_frame_sha256,
            "matcher_version": self._matcher_version,
        }


def _rgb_hex_to_bgr(color_hex: str) -> tuple[int, int, int]:
    red = int(color_hex[0:2], 16)
    green = int(color_hex[2:4], 16)
    blue = int(color_hex[4:6], 16)
    return blue, green, red


def _validated_threshold(value: Any, reason: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 1
        or not math.isfinite(value)
    ):
        _config_fail(reason)
    return float(value)


def _snapshot_tuple(value: Any, reason: str) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        _config_fail(reason)
    try:
        return tuple(value)
    except Exception:
        _config_fail(reason)


def _snapshot_iterable(value: Any, reason: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        _config_fail(reason)
    try:
        return tuple(value)
    except Exception:
        _config_fail(reason)


def _config_fail(reason: str) -> None:
    raise SparseFingerprintConfigError(reason) from None
