"""Conservative visual recognizer for Grand Masters training fragments.

The recognizer deliberately receives only pixels.  It locates the selected
training card from its visible ``Decide`` ribbon, then reads the badge above
that card.  It does not accept a training-tab argument or use a file name as a
classification feature.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np


CANONICAL_SIZE = (720, 1280)
_COLOR_ORDER = ("yellow", "red", "blue")
SUPPORTED_KIND_CLASSES = ("speed", "stamina", "guts", "wisdom")

# Gold-verified, color-independent icon-shape descriptors. Each vector is:
# white coverage, four row coverages, four column coverages, component count.
_KIND_TEMPLATES: dict[str, tuple[tuple[float, ...], ...]] = {
    "speed": (
        (0.33272, 0.22266, 0.20139, 0.58594, 0.33681, 0.20588, 0.55147, 0.5, 0.07353, 0.375),
    ),
    "stamina": (
        (0.47610, 0.23828, 0.80903, 0.62891, 0.21875, 0.33456, 0.61029, 0.61397, 0.34559, 0.5),
        (0.45588, 0.24609, 0.81597, 0.63672, 0.12153, 0.29779, 0.61029, 0.59926, 0.31618, 0.375),
    ),
    "guts": (
        (0.28, 0.16, 0.55, 0.37, 0.04, 0.11, 0.49, 0.47, 0.06, 0.125),
        (0.39522, 0.30469, 0.20139, 0.67188, 0.42361, 0.40074, 0.52941, 0.42647, 0.22426, 0.5),
    ),
    "wisdom": (
        (0.46, 0.32, 0.67, 0.68, 0.17, 0.34, 0.61, 0.53, 0.36, 0.625),
        (0.36, 0.64, 0.65, 0.12, 0.02, 0.24, 0.49, 0.38, 0.32, 0.750),
        (0.49908, 0.21094, 0.68056, 0.55859, 0.52083, 0.40441, 0.66544, 0.55882, 0.36765, 0.75),
    ),
}


def recognize_grand_masters_training_fragment(img: np.ndarray) -> dict[str, Any]:
    """Recognize the selected training card's built-in fragment badge.

    Every field is independently allowed to abstain.  ROI coordinates use the
    input image's pixel coordinate space and are returned as ``[x0, y0, x1,
    y1]`` so a caller can retain the evidence alongside the decision.
    """
    if img is None or img.size == 0 or len(img.shape) != 3 or img.shape[2] != 3:
        return _empty_result("invalid_image")

    normalized, layout_evidence = _normalize_to_canonical(img)
    if normalized is None:
        return _empty_result("unsupported_layout", evidence=layout_evidence)

    ribbon_candidates = _locate_selected_ribbon_candidates(normalized)
    if not ribbon_candidates:
        return _empty_result("selected_ribbon_not_found", evidence=layout_evidence)

    ribbon: tuple[int, int, int, int] | None = None
    structure_evidence: dict[str, Any] = {}
    for candidate in ribbon_candidates:
        accepted, candidate_evidence = _has_selected_card_structure(normalized, candidate)
        if accepted:
            ribbon = candidate
            structure_evidence = candidate_evidence
            break
    if ribbon is None:
        return _empty_result(
            "selected_fragment_structure_not_found",
            evidence={**layout_evidence, "ribbon_candidates": len(ribbon_candidates)},
        )

    badge_roi = _badge_roi_from_ribbon(ribbon, normalized.shape[:2])
    x0, y0, x1, y1 = badge_roi
    badge = normalized[y0:y1, x0:x1]
    if badge.size == 0:
        return _empty_result(
            "badge_crop_empty",
            badge_roi=badge_roi,
            evidence=layout_evidence,
        )

    color_value, color_confidence, color_evidence = classify_fragment_color(badge)
    kind_roi = _inner_roi(badge_roi, normalized.shape[:2], inset_x=6, inset_y=6)
    kx0, ky0, kx1, ky1 = kind_roi
    kind_value, kind_confidence, kind_evidence = _classify_fragment_kind(
        normalized[ky0:ky1, kx0:kx1]
    )
    count_roi = _count_roi_from_badge(badge_roi, normalized.shape[:2])
    cx0, cy0, cx1, cy1 = count_roi
    count_value, count_confidence, count_evidence = _classify_fragment_count(
        badge,
        normalized[cy0:cy1, cx0:cx1],
    )

    return {
        "color": {
            "value": color_value,
            "confidence": round(color_confidence, 3),
            "evidence": color_evidence,
        },
        "kind": {
            "value": kind_value,
            "confidence": round(kind_confidence, 3),
            "evidence": kind_evidence,
        },
        "count": {
            "value": count_value,
            "confidence": round(count_confidence, 3),
            "evidence": count_evidence,
        },
        "evidence": {
            "badge_roi": list(badge_roi),
            "color_roi": list(badge_roi),
            "kind_icon_roi": list(kind_roi),
            "count_roi": list(count_roi),
            "selected_ribbon_roi": list(ribbon),
            "selected_card_structure": structure_evidence,
            "coordinate_space": "canonical_720x1280_pixels",
            **layout_evidence,
        },
    }


def _normalize_to_canonical(
    img: np.ndarray,
) -> tuple[np.ndarray | None, dict[str, Any]]:
    height, width = img.shape[:2]
    evidence = {
        "source_size": [width, height],
        "canonical_size": list(CANONICAL_SIZE),
    }
    if width < 360 or height < 640 or width * 16 != height * 9:
        return None, evidence
    if (width, height) == CANONICAL_SIZE:
        evidence["normalized"] = False
        return img, evidence
    evidence["normalized"] = True
    return (
        cv2.resize(img, CANONICAL_SIZE, interpolation=cv2.INTER_AREA),
        evidence,
    )


def classify_fragment_color(
    color_roi: np.ndarray,
) -> tuple[str | None, float, dict[str, Any]]:
    """Classify badge-frame color with a best-versus-second margin gate."""
    if color_roi is None or color_roi.size == 0:
        return None, 0.0, {"reason": "empty_color_roi", "counts": {}, "margin": 0.0}

    hsv = cv2.cvtColor(color_roi, cv2.COLOR_BGR2HSV)
    hue, saturation, value = cv2.split(hsv)
    masks = {
        "yellow": (hue >= 18) & (hue <= 42) & (saturation > 80) & (value > 110),
        "red": ((hue <= 12) | (hue >= 170)) & (saturation > 80) & (value > 110),
        "blue": (hue >= 88) & (hue <= 132) & (saturation > 70) & (value > 100),
    }
    counts = {name: int(np.count_nonzero(mask)) for name, mask in masks.items()}
    ranked = sorted(_COLOR_ORDER, key=counts.__getitem__, reverse=True)
    best, second = ranked[:2]
    roi_pixels = int(color_roi.shape[0] * color_roi.shape[1])
    matched_pixels = sum(counts.values())
    matched_coverage = matched_pixels / roi_pixels if roi_pixels else 0.0
    best_coverage = counts[best] / roi_pixels if roi_pixels else 0.0
    second_coverage = counts[second] / roi_pixels if roi_pixels else 0.0
    margin = best_coverage - second_coverage
    sharpness = float(
        cv2.Laplacian(cv2.cvtColor(color_roi, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
    )
    raw_confidence = best_coverage * 0.65 + margin * 0.35
    sharpness_quality = min(1.0, sharpness / 500.0)
    confidence = max(0.0, min(1.0, raw_confidence * sharpness_quality))
    evidence = {
        "counts": counts,
        "best": best,
        "second": second,
        "roi_pixels": roi_pixels,
        "matched_pixels": matched_pixels,
        "unmatched_pixels": max(0, roi_pixels - matched_pixels),
        "matched_coverage": round(matched_coverage, 4),
        "best_coverage": round(best_coverage, 4),
        "second_coverage": round(second_coverage, 4),
        "margin": round(margin, 4),
        "sharpness": round(sharpness, 3),
    }
    if (
        matched_coverage < 0.50
        or best_coverage < 0.40
        or margin < 0.16
        or sharpness < 500.0
    ):
        evidence["reason"] = "low_color_quality_or_margin"
        return None, confidence, evidence
    return best, confidence, evidence


def _locate_selected_ribbon_candidates(
    img: np.ndarray,
) -> list[tuple[int, int, int, int]]:
    """Find geometrically plausible ribbon components in canonical pixels."""
    height, _ = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hue, saturation, value = cv2.split(hsv)
    yellow = ((hue >= 18) & (hue <= 42) & (saturation > 80) & (value > 110)).astype(
        np.uint8
    )
    _, _, stats, _ = cv2.connectedComponentsWithStats(yellow, connectivity=8)
    candidates: list[tuple[int, int, int, int, int]] = []
    for x, y, width, component_height, area in stats[1:]:
        if (
            y > height * 0.80
            and 65 <= width <= 80
            and 24 <= component_height <= 38
            and area >= 500
        ):
            candidates.append((int(x), int(y), int(width), int(component_height), int(area)))
    candidates.sort(key=lambda item: item[1])
    return [
        (x, y, x + width, y + component_height)
        for x, y, width, component_height, _ in candidates
    ]


def _has_selected_card_structure(
    img: np.ndarray,
    ribbon: tuple[int, int, int, int],
) -> tuple[bool, dict[str, Any]]:
    """Require the visible training-card structure above the Decide ribbon."""
    x0, y0, x1, _ = ribbon
    card_roi = _clamp_roi((x0 - 40, y0 - 90, x1 + 40, y0), img.shape[:2])
    rx0, ry0, rx1, ry1 = card_roi
    crop = img[ry0:ry1, rx0:rx1]
    if crop.size == 0:
        return False, {"card_roi": list(card_roi), "reason": "empty_card_roi"}
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hue, saturation, value = cv2.split(hsv)
    green_ratio = float(
        np.mean(
            (hue >= 38)
            & (hue <= 92)
            & (saturation > 70)
            & (value > 80)
        )
    )
    white_ratio = float(np.mean((saturation < 65) & (value > 170)))
    colored_ratio = float(np.mean((saturation > 70) & (value > 80)))
    evidence = {
        "card_roi": list(card_roi),
        "green_ratio": round(green_ratio, 4),
        "white_ratio": round(white_ratio, 4),
        "colored_ratio": round(colored_ratio, 4),
    }
    return white_ratio >= 0.12 and colored_ratio >= 0.10, evidence


def _badge_roi_from_ribbon(
    ribbon: tuple[int, int, int, int], image_shape: tuple[int, int]
) -> tuple[int, int, int, int]:
    x0, y0, _, _ = ribbon
    return _clamp_roi((x0 - 28, y0 - 131, x0 + 16, y0 - 85), image_shape)


def _count_roi_from_badge(
    badge_roi: tuple[int, int, int, int], image_shape: tuple[int, int]
) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = badge_roi
    return _clamp_roi((x1, y0, x1 + (x1 - x0), y1), image_shape)


def _inner_roi(
    roi: tuple[int, int, int, int],
    image_shape: tuple[int, int],
    *,
    inset_x: int,
    inset_y: int,
) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = roi
    return _clamp_roi((x0 + inset_x, y0 + inset_y, x1 - inset_x, y1 - inset_y), image_shape)


def _clamp_roi(
    roi: tuple[int, int, int, int], image_shape: tuple[int, int]
) -> tuple[int, int, int, int]:
    height, width = image_shape
    x0, y0, x1, y1 = roi
    x0, x1 = max(0, x0), min(width, x1)
    y0, y1 = max(0, y0), min(height, y1)
    return x0, y0, max(x0, x1), max(y0, y1)


def _classify_fragment_kind(
    icon_roi: np.ndarray,
) -> tuple[str | None, float, dict[str, Any]]:
    """Use icon silhouette/edge evidence, never the training-card identity.

    Only gold-verified templates are supported. Unfamiliar or low-margin
    glyphs abstain rather than borrowing meaning from the selected training
    tab or its filename.
    """
    if icon_roi is None or icon_roi.size == 0:
        return None, 0.0, {
            "reason": "empty_kind_icon_roi",
            "supported_classes": list(SUPPORTED_KIND_CLASSES),
            "template_scores": {},
        }

    hsv = cv2.cvtColor(icon_roi, cv2.COLOR_BGR2HSV)
    saturation, value = hsv[:, :, 1], hsv[:, :, 2]
    bright = ((saturation < 65) & (value > 170)).astype(np.uint8) * 255
    edge = cv2.Canny(cv2.cvtColor(icon_roi, cv2.COLOR_BGR2GRAY), 80, 160)
    bright_pixels = int(np.count_nonzero(bright))
    edge_pixels = int(np.count_nonzero(edge))
    height, width = bright.shape
    coverage = bright_pixels / max(1, bright.size)
    edge_coverage = edge_pixels / max(1, edge.size)
    sharpness = float(
        cv2.Laplacian(cv2.cvtColor(icon_roi, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
    )
    binary = (bright > 0).astype(np.uint8)
    row_profile = [
        float(np.mean(binary[index * height // 4 : (index + 1) * height // 4]))
        for index in range(4)
    ]
    column_profile = [
        float(np.mean(binary[:, index * width // 4 : (index + 1) * width // 4]))
        for index in range(4)
    ]
    component_count, _, stats, _ = cv2.connectedComponentsWithStats(binary)
    retained_components = sum(int(stat[4]) >= 5 for stat in stats[1:component_count])
    descriptor = (
        coverage,
        *row_profile,
        *column_profile,
        min(retained_components, 8) / 8.0,
    )
    scores = {
        kind: max(
            0.0,
            1.0
            - min(_descriptor_distance(descriptor, template) for template in templates)
            / 0.35,
        )
        for kind, templates in _KIND_TEMPLATES.items()
    }
    ranked = sorted(SUPPORTED_KIND_CLASSES, key=scores.__getitem__, reverse=True)
    best, second = ranked[:2]
    margin = scores[best] - scores[second]
    confidence = max(0.0, min(1.0, scores[best] * 0.75 + margin * 0.25))
    evidence = {
        "template_scores": {name: round(score, 4) for name, score in scores.items()},
        "supported_classes": list(SUPPORTED_KIND_CLASSES),
        "best": best,
        "second": second,
        "margin": round(margin, 4),
        "bright_pixels": bright_pixels,
        "edge_pixels": edge_pixels,
        "coverage": round(coverage, 4),
        "edge_coverage": round(edge_coverage, 4),
        "sharpness": round(sharpness, 3),
        "retained_components": retained_components,
    }
    if coverage < 0.25 or edge_coverage < 0.08 or sharpness < 500.0:
        evidence["reason"] = "low_kind_quality"
        return None, min(confidence, 0.25), evidence
    if scores[best] < 0.60 or margin < 0.14:
        evidence["reason"] = "low_kind_template_margin"
        return None, confidence, evidence
    return best, confidence, evidence


def _descriptor_distance(
    left: tuple[float, ...], right: tuple[float, ...]
) -> float:
    return float(np.sqrt(np.mean([(a - b) ** 2 for a, b in zip(left, right)])))


def _classify_fragment_count(
    badge: np.ndarray,
    count_roi: np.ndarray,
) -> tuple[int | None, float, dict[str, Any]]:
    """Recognize one explicit selected-card badge; otherwise abstain."""
    if badge is None or badge.size == 0 or count_roi is None or count_roi.size == 0:
        return None, 0.0, {"reason": "count_roi_empty", "x2_roi": None}
    hsv = cv2.cvtColor(badge, cv2.COLOR_BGR2HSV)
    colored_coverage = float(np.mean((hsv[:, :, 1] > 80) & (hsv[:, :, 2] > 110)))
    bright_coverage = float(np.mean((hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 170)))
    count_context_coverage = float(np.mean(cv2.cvtColor(count_roi, cv2.COLOR_BGR2HSV)[:, :, 2] > 60))
    evidence = {
        "reason": "one_visible_selected_training_badge",
        "single_badge_roi": "badge_roi",
        "x2_roi": None,
        "second_badge_roi": None,
        "colored_coverage": round(colored_coverage, 4),
        "bright_coverage": round(bright_coverage, 4),
        "count_context_coverage": round(count_context_coverage, 4),
    }
    if (
        colored_coverage < 0.45
        or bright_coverage < 0.12
        or count_context_coverage < 0.10
    ):
        evidence["reason"] = "single_badge_structure_unconfirmed"
        return None, 0.0, evidence
    return 1, min(1.0, 0.65 + colored_coverage * 0.2 + bright_coverage * 0.15), evidence


def _empty_result(
    reason: str,
    *,
    badge_roi: tuple[int, int, int, int] | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result_evidence: dict[str, Any] = {"reason": reason, **(evidence or {})}
    if badge_roi is not None:
        result_evidence["badge_roi"] = list(badge_roi)
    return {
        "color": {"value": None, "confidence": 0.0, "evidence": {"reason": reason}},
        "kind": {
            "value": None,
            "confidence": 0.0,
            "evidence": {
                "reason": reason,
                "supported_classes": list(SUPPORTED_KIND_CLASSES),
                "template_scores": {},
            },
        },
        "count": {"value": None, "confidence": 0.0, "evidence": {"reason": reason}},
        "evidence": result_evidence,
    }
