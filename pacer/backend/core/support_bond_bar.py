"""Fail-closed horizontal support bond-bar measurement."""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np


SCHEMA_VERSION = "bond_bar_measurement.v1"


def measure_bond_bar(
    roi_image: np.ndarray,
    bbox: tuple[int, int, int, int],
    *,
    source: str,
) -> dict[str, Any]:
    if source != "detected":
        return _abstain(source, ["inferred_source"])
    patch = _strict_crop(roi_image, bbox)
    if patch is None:
        return _abstain(source, ["invalid_bbox"])
    return _measure_detected_patch(patch, bbox=bbox, source=source)


def _strict_crop(
    image: np.ndarray,
    bbox: tuple[int, int, int, int],
) -> np.ndarray | None:
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] != 3:
        return None
    if len(bbox) != 4 or any(type(value) is not int for value in bbox):
        return None
    x1, y1, x2, y2 = bbox
    height, width = image.shape[:2]
    if x1 < 0 or y1 < 0 or x2 > width or y2 > height or x1 >= x2 or y1 >= y2:
        return None
    return image[y1:y2, x1:x2]


def _measure_detected_patch(
    patch: np.ndarray,
    *,
    bbox: tuple[int, int, int, int],
    source: str,
) -> dict[str, Any]:
    colored_area_ratio = _colored_area_ratio(patch)
    height, width = patch.shape[:2]
    base_evidence: dict[str, Any] = {"input_bbox": list(bbox)}
    if height < 8 or width < 48 or not 4.0 <= (width / height) <= 12.0:
        return _abstain(
            source,
            ["geometry_invalid"],
            colored_area_ratio_shadow=colored_area_ratio,
            evidence=base_evidence,
        )

    track = _locate_track(patch)
    if track is None:
        return _abstain(
            source,
            ["track_not_found"],
            colored_area_ratio_shadow=colored_area_ratio,
            evidence=base_evidence,
        )
    track_bbox, inner_bbox = track
    ix1, iy1, ix2, iy2 = inner_bbox
    inner = patch[iy1:iy2, ix1:ix2]
    base_evidence.update(
        {
            "track_bbox": list(track_bbox),
            "inner_track_bbox": list(inner_bbox),
        }
    )
    if inner.size == 0 or inner.shape[0] < 2 or inner.shape[1] < 24:
        return _abstain(
            source,
            ["geometry_invalid"],
            colored_area_ratio_shadow=colored_area_ratio,
            evidence=base_evidence,
        )

    measurement = _measure_left_prefix(inner)
    base_evidence.update(measurement["evidence"])
    rejection_reasons = measurement["rejection_reasons"]
    if rejection_reasons:
        return _abstain(
            source,
            rejection_reasons,
            colored_area_ratio_shadow=colored_area_ratio,
            evidence=base_evidence,
        )

    ratio = round(float(measurement["ratio"]), 2)
    base_evidence["rejection_reasons"] = []
    base_evidence["candidate_fill_ratio"] = ratio
    if abs(ratio - 0.80) <= 0.03:
        return _abstain(
            source,
            ["friendship_threshold_uncertain"],
            colored_area_ratio_shadow=colored_area_ratio,
            evidence=base_evidence,
        )

    if ratio < 0.35:
        band = "low"
    elif ratio < 0.65:
        band = "medium"
    elif ratio < 0.80:
        band = "high"
    else:
        band = "friendship_ready"
    confidence = min(
        0.99,
        0.72
        + (0.18 * float(measurement["prefix_contiguity"]))
        + (0.09 * float(measurement["threshold_agreement"])),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "measurement_status": "accepted",
        "source": source,
        "fill_ratio_estimate": ratio,
        "bond_band": band,
        "friendship_ready": band == "friendship_ready",
        "decision_safe": False,
        "confidence": round(confidence, 3),
        "colored_area_ratio_shadow": colored_area_ratio,
        "evidence": base_evidence,
    }


def _locate_track(
    patch: np.ndarray,
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]] | None:
    height, width = patch.shape[:2]
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 30, 100)
    row_coverage = (edges > 0).mean(axis=1)
    upper_limit = max(1, height // 3)
    lower_start = min(height - 1, (height * 2) // 3)
    top = int(np.argmax(row_coverage[:upper_limit]))
    bottom = lower_start + int(np.argmax(row_coverage[lower_start:]))
    if row_coverage[top] < 0.70 or row_coverage[bottom] < 0.70:
        return None
    if bottom - top < int(round(height * 0.55)):
        return None

    x_inset = max(3, int(round(width * 0.04)))
    y_inset = max(2, int(round(height * 0.21)))
    inner_bbox = (x_inset, y_inset, width - x_inset, height - y_inset)
    if inner_bbox[0] >= inner_bbox[2] or inner_bbox[1] >= inner_bbox[3]:
        return None
    return (0, top, width, bottom + 1), inner_bbox


def _measure_left_prefix(inner: np.ndarray) -> dict[str, Any]:
    lab = cv2.cvtColor(inner, cv2.COLOR_BGR2LAB).astype(np.float32)
    hsv = cv2.cvtColor(inner, cv2.COLOR_BGR2HSV)
    column_saturation = np.median(hsv[:, :, 1], axis=0)
    column_value = np.median(hsv[:, :, 2], axis=0)
    neutral_divider_cutoff = float(np.median(column_value)) - 15.0
    divider_columns = (
        (column_saturation <= 16)
        & (column_value <= neutral_divider_cutoff)
    )
    columns = np.median(lab, axis=0)
    width = columns.shape[0]
    tail_width = max(4, int(np.ceil(width * 0.20)))
    tail = columns[-tail_width:]
    right_anchor = columns[-1]
    tail_distance = np.linalg.norm(tail - right_anchor, axis=1)
    cluster_cutoff = max(4.0, float(np.percentile(tail_distance, 40)))
    reference_candidates = tail[tail_distance <= cluster_cutoff]
    reference = np.median(
        reference_candidates if len(reference_candidates) else tail,
        axis=0,
    )
    distances = np.linalg.norm(columns - reference, axis=1)
    low = float(np.percentile(distances, 5))
    high = float(np.percentile(distances, 95))
    separation = high - low

    if separation < 6.0:
        saturation = float(np.median(hsv[:, :, 1]))
        column_spread = float(np.max(np.linalg.norm(columns - np.median(columns, axis=0), axis=1)))
        if saturation > 12.0 and column_spread < 5.0:
            return {
                "ratio": 1.0,
                "prefix_contiguity": 1.0,
                "threshold_agreement": 1.0,
                "rejection_reasons": [],
                "evidence": {
                    "left_anchor_score": 1.0,
                    "prefix_contiguity": 1.0,
                    "right_tail_empty_score": None,
                    "threshold_estimates": [1.0, 1.0, 1.0],
                    "threshold_agreement": 1.0,
                    "reference_tail_fraction": 0.20,
                },
            }
        return {
            "ratio": None,
            "prefix_contiguity": 0.0,
            "threshold_agreement": 0.0,
            "rejection_reasons": ["track_not_found"],
            "evidence": {
                "left_anchor_score": 0.0,
                "prefix_contiguity": 0.0,
                "right_tail_empty_score": None,
                "threshold_estimates": [],
                "threshold_agreement": 0.0,
                "reference_tail_fraction": 0.20,
            },
        }

    thresholds = [low + (separation * factor) for factor in (0.35, 0.45, 0.55)]
    estimates: list[float] = []
    contiguities: list[float] = []
    left_scores: list[float] = []
    tail_scores: list[float] = []
    for threshold in thresholds:
        # The live bond bar is divided into five cells by dark neutral strokes.
        # Those strokes differ from the gray tail and would otherwise look like
        # isolated "filled" islands.  Keep them as gaps: the existing maximum
        # gap rule still rejects real discontinuities, while 1-2 px dividers no
        # longer extend the apparent fill into later empty cells.
        prefix = _analyze_prefix((distances > threshold) & ~divider_columns)
        if prefix["rejection_reason"]:
            return {
                "ratio": None,
                "prefix_contiguity": prefix["contiguity"],
                "threshold_agreement": 0.0,
                "rejection_reasons": [prefix["rejection_reason"]],
                "evidence": {
                    "left_anchor_score": prefix["left_anchor_score"],
                    "prefix_contiguity": prefix["contiguity"],
                    "right_tail_empty_score": prefix["tail_empty_score"],
                    "threshold_estimates": estimates,
                    "threshold_agreement": 0.0,
                    "reference_tail_fraction": 0.20,
                    "neutral_divider_column_count": int(
                        divider_columns.sum()
                    ),
                },
            }
        estimates.append(float(prefix["ratio"]))
        contiguities.append(float(prefix["contiguity"]))
        left_scores.append(float(prefix["left_anchor_score"]))
        tail_scores.append(float(prefix["tail_empty_score"]))

    spread = max(estimates) - min(estimates)
    if spread > 0.05:
        return {
            "ratio": None,
            "prefix_contiguity": min(contiguities),
            "threshold_agreement": 0.0,
            "rejection_reasons": ["threshold_disagreement"],
            "evidence": {
                "left_anchor_score": min(left_scores),
                "prefix_contiguity": min(contiguities),
                "right_tail_empty_score": min(tail_scores),
                "threshold_estimates": [round(value, 4) for value in estimates],
                "threshold_agreement": 0.0,
                "reference_tail_fraction": 0.20,
                "neutral_divider_column_count": int(divider_columns.sum()),
            },
        }

    agreement = max(0.0, 1.0 - (spread / 0.05))
    return {
        "ratio": float(np.median(estimates)),
        "prefix_contiguity": min(contiguities),
        "threshold_agreement": agreement,
        "rejection_reasons": [],
        "evidence": {
            "left_anchor_score": min(left_scores),
            "prefix_contiguity": round(min(contiguities), 4),
            "right_tail_empty_score": round(min(tail_scores), 4),
            "threshold_estimates": [round(value, 4) for value in estimates],
            "threshold_agreement": round(agreement, 4),
            "reference_tail_fraction": 0.20,
            "neutral_divider_column_count": int(divider_columns.sum()),
        },
    }


def _analyze_prefix(mask: np.ndarray) -> dict[str, Any]:
    width = int(mask.size)
    anchor_width = max(2, int(np.ceil(width * 0.04)))
    left_anchor_score = float(mask[:anchor_width].mean())
    filled = np.flatnonzero(mask)
    if filled.size == 0:
        return {
            "ratio": 0.0,
            "contiguity": 0.0,
            "left_anchor_score": left_anchor_score,
            "tail_empty_score": 1.0,
            "rejection_reason": "track_not_found",
        }
    if left_anchor_score < 0.60:
        return {
            "ratio": 0.0,
            "contiguity": 0.0,
            "left_anchor_score": left_anchor_score,
            "tail_empty_score": float((~mask).mean()),
            "rejection_reason": "non_prefix_fill",
        }

    last_filled = int(filled[-1])
    prefix = mask[: last_filled + 1]
    empty_runs = _true_run_lengths(~prefix)
    maximum_gap = max(empty_runs, default=0)
    if maximum_gap > max(1, int(np.floor(width * 0.08))):
        return {
            "ratio": (last_filled + 1) / width,
            "contiguity": float(prefix.mean()),
            "left_anchor_score": left_anchor_score,
            "tail_empty_score": float((~mask[last_filled + 1 :]).mean())
            if last_filled + 1 < width
            else 1.0,
            "rejection_reason": "non_prefix_fill",
        }
    tail = mask[last_filled + 1 :]
    return {
        "ratio": (last_filled + 1) / width,
        "contiguity": float(prefix.mean()),
        "left_anchor_score": left_anchor_score,
        "tail_empty_score": float((~tail).mean()) if tail.size else 1.0,
        "rejection_reason": None,
    }


def _true_run_lengths(mask: np.ndarray) -> list[int]:
    runs: list[int] = []
    current = 0
    for value in mask.tolist():
        if value:
            current += 1
        elif current:
            runs.append(current)
            current = 0
    if current:
        runs.append(current)
    return runs


def _colored_area_ratio(patch: np.ndarray) -> float:
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    colored = (hsv[:, :, 1] > 45) & (hsv[:, :, 2] > 70)
    return round(float(colored.mean()), 4)


def _abstain(
    source: str,
    rejection_reasons: list[str],
    *,
    colored_area_ratio_shadow: float | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence_payload = dict(evidence or {})
    evidence_payload["rejection_reasons"] = list(rejection_reasons)
    return {
        "schema_version": SCHEMA_VERSION,
        "measurement_status": "abstain",
        "source": source,
        "fill_ratio_estimate": None,
        "bond_band": "unknown",
        "friendship_ready": None,
        "decision_safe": False,
        "confidence": 0.0,
        "colored_area_ratio_shadow": colored_area_ratio_shadow,
        "evidence": evidence_payload,
    }
