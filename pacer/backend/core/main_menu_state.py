from __future__ import annotations

import hashlib
import json
import copy
import math
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from core import numeric_ocr_crosscheck as numeric_crosscheck


CANONICAL_SIZE = (720, 1280)
SUPPORTED_SIZES = {CANONICAL_SIZE, (1440, 2560)}
HIGH_CONFIDENCE = 0.8
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
G_RANK_TEMPLATE_SIZE = (16, 16)
G_RANK_TEMPLATE_HEX = (
    "00000000000007181ffc307660434367"
    "8ffe8dfc98fcbbf8fe18fff8fff8fff8"
)
G_RANK_TEMPLATE_SOURCE_SHA256 = (
    "40bda623f0c1907ae32dade81ae99f6a5ad0d0ac008caecaedc851ba020a44b9"
)

FIELD_NAMES = (
    "screen",
    "remaining_turns",
    "target_text",
    "speed",
    "stamina",
    "power",
    "guts",
    "wisdom",
    "skill_pt",
    "vitality",
    "mood",
    "fragments",
)

NUMERIC_FIELDS = (
    "remaining_turns",
    "speed",
    "stamina",
    "power",
    "guts",
    "wisdom",
    "skill_pt",
)


def _score(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return score if math.isfinite(score) and 0.0 <= score <= 1.0 else None


def secondary_number_accepted(second: Any, *, minimum: int = 0, maximum: int | None = None) -> bool:
    if not numeric_crosscheck.crosscheck_enabled() or not isinstance(second, dict):
        return False
    confidence, threshold = _score(second.get("confidence")), _score(second.get("min_score"))
    value = second.get("value")
    return (second.get("status") == "accepted" and second.get("engine") == "local_rapidocr_numeric"
            and type(value) is int and value >= minimum and (maximum is None or value <= maximum)
            and confidence is not None and threshold is not None
            and confidence >= max(threshold, numeric_crosscheck.second_opinion_min_score()))


def field_confidence_accepted(field: Any, *, primary_threshold: float = HIGH_CONFIDENCE) -> bool:
    """Keep primary thresholds; validate the explicit secondary numeric evidence.

    A source label or an accepted boolean alone cannot lower a threshold. The
    independent result, actual score, bounds and rejected primary must agree.
    """
    if not isinstance(field, dict) or field.get("abstained") is not False:
        return False
    confidence = _score(field.get("confidence"))
    if confidence is None:
        return False
    evidence = field.get("evidence")
    proof = evidence.get("numeric_crosscheck") if isinstance(evidence, dict) else None
    secondary = field.get("source") == "numeric_ocr_secondary" or (
        isinstance(proof, dict) and proof.get("decision") == "secondary_accepted")
    if not secondary:
        return confidence >= primary_threshold
    if not numeric_crosscheck.crosscheck_enabled() or not isinstance(proof, dict):
        return False
    second, primary = proof.get("second"), proof.get("primary")
    minimum, maximum = proof.get("minimum"), proof.get("maximum")
    threshold = _score(proof.get("min_score"))
    value = field.get("value")
    if (proof.get("schema_version") != "uma.numeric_ocr_secondary.v1"
            or proof.get("field") not in NUMERIC_FIELDS
            or type(value) is not int or type(minimum) is not int
            or type(maximum) is not int or not minimum <= value <= maximum
            or threshold is None or not isinstance(primary, dict)
            or not secondary_number_accepted(second, minimum=minimum, maximum=maximum)
            or second["value"] != value
            or _score(second.get("confidence")) != confidence
            or _score(second.get("min_score")) != threshold
            or confidence < max(threshold, numeric_crosscheck.second_opinion_min_score())):
        return False
    primary_score = _score(primary.get("confidence"))
    return (primary.get("abstained") is True or type(primary.get("value")) is not int
            or primary_score is None or primary_score < HIGH_CONFIDENCE)


def numeric_field_accepted(field: Any, *, primary_threshold: float = HIGH_CONFIDENCE) -> bool:
    return (isinstance(field, dict) and type(field.get("value")) is int
            and field["value"] >= 0
            and field_confidence_accepted(field, primary_threshold=primary_threshold))


def _numeric_archive(site: str, decision: str, **payload: Any) -> None:
    try:
        numeric_crosscheck.archive(site, decision=decision, **payload)
    except Exception:
        pass


def supplement_numeric_field(
    field: str, image: np.ndarray, primary: dict[str, Any], *,
    numeric_reader: Any = None, site: str = "main_menu_state",
    source_frame_sha256: str | None = None, primary_threshold: float = HIGH_CONFIDENCE,
) -> dict[str, Any]:
    """Read only this confirmed page's fixed numeric slot, after primary abstention."""
    config, _ = _load_assets()
    bounds = config["numeric_fields"][field]
    evidence = primary.get("evidence") if isinstance(primary.get("evidence"), dict) else {}
    roi = list(evidence.get("roi") or config["rois"][field])
    if field in {"speed", "stamina", "power", "guts", "wisdom"}:
        # Same fixed right-aligned stat box: include its existing four-digit
        # left extension, never the adjacent grade or a different field.
        roi[0] = min(roi[0], config["rois"][field][0] - 8)
    primary_summary = {key: copy.deepcopy(primary.get(key)) for key in ("value", "confidence", "abstained")}
    threshold = numeric_crosscheck.second_opinion_min_score()
    metadata = {"field": field, "primary": primary_summary, "second": None,
        "roi": roi, "minimum": bounds["min"], "maximum": bounds["max"],
        "min_score": threshold, "source_frame_sha256": source_frame_sha256}
    if numeric_field_accepted(primary, primary_threshold=primary_threshold):
        if os.environ.get("UMA_NUMERIC_OCR_ARCHIVE_VERBOSE") == "1":
            _numeric_archive(site, "primary_accepted", reason="primary_accepted", **metadata)
        return primary
    if not numeric_crosscheck.crosscheck_enabled():
        _numeric_archive(site, "primary_retained", reason="crosscheck_disabled", **metadata)
        return primary
    try:
        second = numeric_crosscheck.read_number(_crop(image, roi), reader=numeric_reader,
            minimum=bounds["min"], maximum=bounds["max"], prefixes=("",), suffixes=("",))
    except Exception:
        second = {"status": "abstained", "value": None, "confidence": None,
            "engine": "local_rapidocr_numeric", "reason": "secondary_reader_unavailable"}
    metadata["second"] = copy.deepcopy(second)
    result = primary
    decision = "secondary_abstained"
    if isinstance(second, dict) and second.get("status") == "accepted":
        proof = {"schema_version": "uma.numeric_ocr_secondary.v1", "decision": "secondary_accepted", **metadata}
        candidate = {"value": second.get("value"), "confidence": second.get("confidence"),
            "abstained": False, "source": "numeric_ocr_secondary",
            "evidence": {**copy.deepcopy(evidence), "numeric_crosscheck": proof}}
        if numeric_field_accepted(candidate):
            result, decision = candidate, "secondary_accepted"
    reason = second.get("reason") if isinstance(second, dict) else "secondary_invalid"
    if isinstance(second, dict) and second.get("status") == "accepted" and decision != "secondary_accepted":
        reason = "secondary_evidence_invalid"
    _numeric_archive(site, decision, reason=reason, **metadata)
    return result

REQUIRED_FOR_DECISION = ("screen", "remaining_turns", "vitality")
OPTIONAL_DECISION_FIELDS = ("speed", "stamina", "power", "guts", "wisdom", "skill_pt")
REQUIRED_FOR_NUMERIC_STATE = REQUIRED_FOR_DECISION + OPTIONAL_DECISION_FIELDS

VITALITY_ROI = (229, 149, 505, 174)


@dataclass(frozen=True)
class ImageTemplate:
    label: Any
    path: str
    image: np.ndarray
    min_prior_improvement: float = 0.0


def analyze_main_menu_navigation(png_bytes: bytes, *, numeric_reader: Any = None) -> dict[str, Any]:
    """Current L'Arc entry fields; never a full strategy state/context.

    Keep every original required numeric field and numeric-complete shortcut.
    Target text and mood are not consulted by the entry-navigation check; their
    full strategy observations still come from the ordinary outer provider.
    """
    source_sha = hashlib.sha256(png_bytes).hexdigest()
    result = _new_result(source_sha)
    result['fields'].pop('fragments')
    image = _decode_png(png_bytes)
    if image is None:
        result['errors'].append('decode_failed')
        return result
    height, width = image.shape[:2]
    result['source']['size'] = [width, height]
    canonical = _normalize_main_menu_image(image)
    if canonical is None:
        result['normalization']['mode'] = 'unsupported'
        result['errors'].append('unsupported_layout')
        return result
    from core.auto_trainer.transition_wait import connecting_overlay_visible
    if connecting_overlay_visible(canonical):
        result['warnings'].append('connecting_overlay')
        return result
    result['canonical_size'] = list(CANONICAL_SIZE)
    result['normalization'] = _normalization_evidence(width, height)
    result['fields']['remaining_turns'] = _analyze_numeric_field('remaining_turns', canonical, scenario='larc')
    result['fields']['screen'] = _detect_main_menu(canonical, scenario='larc')
    if result['fields']['screen']['abstained']:
        result['warnings'].append('main_menu_anchor_missing')
        return result
    for name in NUMERIC_FIELDS:
        if name != 'remaining_turns':
            result['fields'][name] = _analyze_numeric_field(name, canonical, scenario='larc')
    for name in NUMERIC_FIELDS:
        result['fields'][name] = supplement_numeric_field(name, canonical,
            result['fields'][name], numeric_reader=numeric_reader, source_frame_sha256=source_sha)
    result['fields']['vitality'] = _analyze_vitality(canonical)
    _set_training_readiness(result, allow_partial=True)
    return result


def analyze_main_menu_state(png_bytes: bytes, *, scenario: str | None = "grand_masters", numeric_reader: Any = None) -> dict[str, Any]:
    source_sha = hashlib.sha256(png_bytes).hexdigest()
    result = _new_result(source_sha)
    if scenario != "grand_masters":
        result["fields"].pop("fragments")
    image = _decode_png(png_bytes)
    if image is None:
        result["errors"].append("decode_failed")
        return result

    height, width = image.shape[:2]
    result["source"]["size"] = [width, height]
    canonical = _normalize_main_menu_image(image)
    if canonical is None:
        result["normalization"]["mode"] = "unsupported"
        result["errors"].append("unsupported_layout")
        return result
    if scenario == "larc":
        from core.auto_trainer.transition_wait import connecting_overlay_visible
        if connecting_overlay_visible(canonical):
            result["warnings"].append("connecting_overlay")
            return result
    result["canonical_size"] = list(CANONICAL_SIZE)
    result["normalization"] = _normalization_evidence(width, height)

    result["fields"]["target_text"] = _analyze_target_text(canonical)
    result["fields"]["remaining_turns"] = _analyze_numeric_field(
        "remaining_turns", canonical, scenario=scenario
    )
    screen = _detect_main_menu(canonical, scenario=scenario)
    result["fields"]["screen"] = screen
    if screen["abstained"]:
        result["warnings"].append("main_menu_anchor_missing")
        _numeric_archive("main_menu_state", "page_unconfirmed", reason="main_menu_anchor_missing", source_frame_sha256=source_sha)
        return result

    for field in NUMERIC_FIELDS:
        if field == "remaining_turns":
            continue
        result["fields"][field] = _analyze_numeric_field(field, canonical, scenario=scenario)
    for field in NUMERIC_FIELDS:
        result["fields"][field] = supplement_numeric_field(field, canonical,
            result["fields"][field], numeric_reader=numeric_reader, source_frame_sha256=source_sha)
    result["fields"]["vitality"] = _analyze_vitality(canonical)
    result["fields"]["mood"] = _classify_local_template("mood", canonical)
    if scenario == "grand_masters":
        result["fields"]["fragments"] = _analyze_fragments(canonical)
    _set_training_readiness(result, allow_partial=scenario == "larc")
    return result


class MainMenuTurnTracker:
    def __init__(self, confidence_threshold: float = HIGH_CONFIDENCE) -> None:
        self.confidence_threshold = confidence_threshold
        self._last_sha: str | None = None
        self._last_remaining: int | None = None
        self._last_remaining_confident = False

    def observe(self, observation: dict[str, Any]) -> dict[str, Any]:
        source_sha = observation.get("source", {}).get("sha256")
        remaining = observation.get("fields", {}).get("remaining_turns", {})
        value = remaining.get("value")
        confidence = float(remaining.get("confidence") or 0.0)
        abstained = bool(remaining.get("abstained", True))
        current_confident = (
            not abstained
            and isinstance(value, int)
            and numeric_field_accepted(remaining, primary_threshold=self.confidence_threshold)
        )

        if self._last_sha is None:
            status = "unknown"
        elif source_sha == self._last_sha:
            status = "duplicate"
        elif not current_confident or not self._last_remaining_confident:
            status = "unknown"
        elif value == self._last_remaining:
            status = "same_turn"
        else:
            status = "new_turn_candidate"

        if source_sha:
            self._last_sha = source_sha
        if current_confident:
            self._last_remaining = value
            self._last_remaining_confident = True

        return {
            "status": status,
            "source_sha256": source_sha,
            "remaining_turns": value if current_confident else None,
            "remaining_confidence": confidence,
        }


def _new_result(source_sha: str) -> dict[str, Any]:
    return {
        "version": 2,
        "source": {"sha256": source_sha, "size": None},
        "canonical_size": None,
        "normalization": {"native": False, "mode": "unavailable", "scale": None},
        "fields": {name: _abstained_field() for name in FIELD_NAMES},
        "valid_for_training": False,
        "numeric_state_complete": False,
        "warnings": [],
        "errors": [],
    }


def _abstained_field(evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "value": None,
        "confidence": 0.0,
        "abstained": True,
        "evidence": evidence or {},
    }


def _field(value: Any, confidence: float, evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "value": value,
        "confidence": round(float(confidence), 4),
        "abstained": False,
        "evidence": evidence,
    }


def _decode_png(png_bytes: bytes) -> np.ndarray | None:
    if not png_bytes.startswith(PNG_SIGNATURE):
        return None
    data = np.frombuffer(png_bytes, dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def _to_canonical(image: np.ndarray, width: int, height: int) -> np.ndarray:
    if (width, height) == CANONICAL_SIZE:
        return image
    return cv2.resize(image, CANONICAL_SIZE, interpolation=cv2.INTER_AREA)


def _normalize_main_menu_image(image: np.ndarray | None) -> np.ndarray | None:
    if image is None:
        return None
    height, width = image.shape[:2]
    if width * CANONICAL_SIZE[1] != height * CANONICAL_SIZE[0]:
        return None
    return _to_canonical(image, width, height)


def _normalization_evidence(width: int, height: int) -> dict[str, Any]:
    scale = [round(width / CANONICAL_SIZE[0], 4), round(height / CANONICAL_SIZE[1], 4)]
    if (width, height) == CANONICAL_SIZE:
        return {"native": True, "mode": "native", "scale": scale}
    return {
        "native": False,
        "mode": "controlled_downscale" if (width, height) in SUPPORTED_SIZES else "aspect_ratio_fallback",
        "scale": scale,
        "interpolation": "INTER_AREA",
    }


def _detect_main_menu(
    image: np.ndarray, *, scenario: str | None = "grand_masters",
) -> dict[str, Any]:
    remaining_color = _blue_ratio if scenario == "larc" else _red_ratio
    anchors = [
        ("remaining_panel", (15, 42, 149, 126), remaining_color, 0.22),
        ("objective_panel", (158, 75, 711, 136), _white_ratio, 0.45),
        ("stats_panel", (25, 825, 591, 906), _blue_ratio, 0.20),
        ("bottom_actions", (28, 939, 686, 1155), _saturated_ratio, 0.18),
        ("training_action", (220, 930, 500, 1045), _blue_ratio, 0.25),
        ("rest_action", (25, 930, 210, 1045), _green_ratio, 0.20),
        ("skill_action", (515, 930, 695, 1045), _blue_ratio, 0.20),
    ]
    scores = []
    for name, roi, scorer, threshold in anchors:
        score = scorer(_crop(image, roi))
        scores.append(
            {
                "anchor": name,
                "score": round(score, 4),
                "threshold": threshold,
                "passed": score >= threshold,
            }
        )
    passed = sum(1 for score in scores if score["passed"])
    action_names = {"training_action", "rest_action", "skill_action"}
    actions_pass = all(
        score["passed"] for score in scores if score["anchor"] in action_names
    )
    confidence = passed / len(anchors)
    evidence = {
        "roi": [list(anchor[1]) for anchor in anchors],
        "score": round(confidence, 4),
        "margin": round((passed - 5.5) / len(anchors), 4),
        "anchors": scores,
    }
    if passed < 6 or not actions_pass:
        return _abstained_field(evidence)
    return _field("main_menu", confidence, evidence)


def _analyze_numeric_field(
    field: str, image: np.ndarray, *, scenario: str | None = "grand_masters",
) -> dict[str, Any]:
    manifest, assets = _load_assets()
    config = manifest["numeric_fields"][field]
    style = config["style"]
    roi = tuple(manifest["rois"][field])
    crop = _crop(image, roi)
    mask_style = "remaining_blue" if field == "remaining_turns" and scenario == "larc" else style
    mask = _digit_mask(crop, mask_style)
    if field == "skill_pt" and scenario == "larc":
        # The legacy PT ROI includes the scenery to the left of the white box.
        # Match the fixed exclusion already used by training-current-stats.
        mask[:, :16] = 0
    components = _segment_glyphs(mask, style)
    # Gold stat gradients can lie above the brown value ceiling. Keep that
    # mask separate and require the existing complete four-digit geometry.
    if field in {"speed", "stamina", "power", "guts", "wisdom"} and not components:
        gold_mask = _digit_mask(crop, "small_gold")
        gold_components = _segment_glyphs(gold_mask, style, split_touching_gold=True)
        if len(gold_components) in {3, 4}:
            mask_style, mask, components = "small_gold", gold_mask, gold_components
    four_digit_left_extension = False
    # A clipped leading "1" can leave a three-pixel component at the old edge.
    clipped_prefix = (
        len(components) == 4
        and components[0]["bbox"][0] == 0 and components[0]["bbox"][2] <= 3
    )
    if field in {"speed", "stamina", "power", "guts", "wisdom"} and (len(components) == 3 or clipped_prefix):
        extension = 8
        extended_roi = (roi[0] - extension, roi[1], roi[2], roi[3])
        extended_crop = _crop(image, extended_roi)
        extended_mask = _digit_mask(extended_crop, mask_style)
        extended_components = _segment_glyphs(
            extended_mask, style, split_touching_gold=mask_style == "small_gold"
        )
        if len(extended_components) == 4:
            first_x, _, first_width, _ = extended_components[0]["bbox"]
            shifted_tail = [
                (x - extension, y, width, height)
                for x, y, width, height in (
                    component["bbox"] for component in extended_components[1:]
                )
            ]
            base_boxes = [component["bbox"] for component in components[-3:]]
            # Four-digit stat values are right aligned and start eight pixels
            # left of the legacy three-digit ROI.  Recover only when extending
            # the ROI reveals exactly one leading glyph that crosses the old
            # boundary, while the other three components remain pixel-exact.
            if (
                first_x <= 2
                and first_x + first_width > extension
                and first_x + first_width <= extension + 3
                and shifted_tail == base_boxes
            ):
                roi = extended_roi
                crop = extended_crop
                mask = extended_mask
                components = extended_components
                four_digit_left_extension = True
    discarded_left_panel_border: tuple[int, int, int, int] | None = None
    if field == "remaining_turns" and len(components) == 2:
        first_x, first_y, first_width, first_height = components[0]["bbox"]
        if (
            first_x <= 2
            and first_y == 0
            and first_width <= 8
            and first_height == mask.shape[0]
        ):
            discarded_left_panel_border = components[0]["bbox"]
            components = components[1:]
    evidence: dict[str, Any] = {
        "roi": list(roi),
        "score": 0.0,
        "margin": 0.0,
        "coverage": 0.0,
        "accepted_path": "glyphs",
        "segmentation": "binary_connected_components",
        "template_coverage": manifest["glyph_coverage"][style],
        "glyphs": [],
    }
    if mask_style == "remaining_blue":
        evidence["glyph_color_style"] = mask_style
    if mask_style == "small_gold":
        evidence["glyph_color_style"] = "small_gold"
        if any("split_from" in component for component in components):
            evidence["segmentation"] = "binary_connected_components_touching_split"
        if not four_digit_left_extension:
            evidence["rejection_reason"] = "gold_stat_incomplete_four_digit_geometry"
            return _abstained_field(evidence)
    if not config["min_glyphs"] <= len(components) <= config["max_glyphs"]:
        evidence["component_count"] = len(components)
        return _abstained_field(evidence)

    if len(components) not in config["supported_glyph_counts"]:
        evidence["component_count"] = len(components)
        evidence["rejection_reason"] = "unsupported_glyph_count"
        return _abstained_field(evidence)

    geometry_config = config.get("geometry_by_glyph_count", {}).get(
        str(len(components)), config
    )
    alignment_center = geometry_config.get(
        "alignment_center", config["alignment_center"]
    )
    slot_pitch = geometry_config.get("slot_pitch", config["slot_pitch"])
    allowed_slot_offset = geometry_config.get(
        "max_slot_offset", config["max_slot_offset"]
    )
    allowed_gap = geometry_config.get("max_gap", config["max_gap"])
    component_boxes = [component["bbox"] for component in components]
    left_edge_ratio = min(x for x, _, _, _ in component_boxes) / mask.shape[1]
    right_edge_ratio = max(x + width for x, _, width, _ in component_boxes) / mask.shape[1]
    centers = [x + width / 2.0 for x, _, width, _ in component_boxes]
    geometry_variant = "centered"
    rank_evidence: dict[str, Any] | None = None
    if four_digit_left_extension:
        alignment_center += 3.0
        slot_pitch = 17.0
        geometry_variant = "four_digit_left_extension"
    elif style == "small" and len(components) == 2 and field in {
        "speed",
        "stamina",
        "power",
        "guts",
        "wisdom",
    }:
        rank_evidence = _two_digit_g_rank_evidence(image, roi)
        if rank_evidence["accepted"]:
            # Main-menu stat values are right aligned.  A genuine two-digit
            # value occupies the final two slots; accepting that geometry is
            # safe only when the adjacent rank is independently verified as
            # gray G.  Otherwise a damaged leading digit of a three-digit
            # value must continue to abstain.
            alignment_center += slot_pitch / 2.0
            geometry_variant = "two_digit_g_rank_right_aligned"
    center_index = (len(centers) - 1) / 2.0
    expected_centers = [
        alignment_center + (index - center_index) * slot_pitch
        for index in range(len(centers))
    ]
    slot_offsets = [
        abs(actual - expected)
        for actual, expected in zip(centers, expected_centers)
    ]
    max_slot_offset = max(slot_offsets)
    gaps = [
        current[0] - (previous[0] + previous[2])
        for previous, current in zip(component_boxes, component_boxes[1:])
    ]
    max_gap = max(gaps, default=0)
    evidence["component_count"] = len(components)
    evidence["segmentation_geometry"] = {
        "variant": geometry_variant,
        "left_edge_ratio": round(left_edge_ratio, 4),
        "right_edge_ratio": round(right_edge_ratio, 4),
        "centers": [round(value, 4) for value in centers],
        "expected_centers": [round(value, 4) for value in expected_centers],
        "slot_offsets": [round(value, 4) for value in slot_offsets],
        "max_slot_offset": round(max_slot_offset, 4),
        "max_gap": max_gap,
    }
    if discarded_left_panel_border is not None:
        evidence["segmentation_geometry"]["discarded_left_panel_border"] = list(
            discarded_left_panel_border
        )
    if rank_evidence is not None:
        evidence["segmentation_geometry"]["rank_evidence"] = rank_evidence
    if (
        max_slot_offset > allowed_slot_offset
        or max_gap > allowed_gap
    ):
        evidence["rejection_reason"] = "incomplete_slot_geometry"
        return _abstained_field(evidence)

    thresholds = manifest["glyph_thresholds"][style]
    size = tuple(manifest["glyph_sizes"][style])
    digits: list[str] = []
    confidences: list[float] = []
    reliable = True
    for component_index, component in enumerate(components):
        normalized = _normalize_binary_glyph(component["mask"], size)
        ranked = _rank_glyph_digits(normalized, assets["glyphs"][style])
        rank_border_trim_pixels = 0
        if (
            four_digit_left_extension and component_index == 0
            and mask_style == "small" and config["max"] < 2000
            and component["bbox"][0] == 0 and component["bbox"][2] == 11
        ):
            # SS+ can touch the extended ROI's first column. Remove only that
            # boundary column, and still require an independently clear "1".
            trimmed = _normalize_binary_glyph(component["mask"][:, 1:], size)
            trial = _rank_glyph_digits(trimmed, assets["glyphs"][style])
            if (
                trial[0]["digit"] == 1
                and trial[0]["score"] <= thresholds["max_best_score"]
                and trial[1]["score"] - trial[0]["score"] >= 0.20
                and trial[0]["coverage"] >= 0.85
            ):
                ranked = trial
                rank_border_trim_pixels = 1
        best = ranked[0]
        second = ranked[1]
        margin = second["score"] - best["score"]
        confidence = _recognition_confidence(
            best["score"], margin, best["coverage"], thresholds["min_margin"]
        )
        glyph_reliable = (
            best["score"] <= thresholds["max_best_score"]
            and margin >= thresholds["min_margin"]
            and best["coverage"] >= thresholds["min_coverage"]
            and confidence >= HIGH_CONFIDENCE
            and best["digit"] in manifest["glyph_coverage"][style]["supported_digits"]
        )
        range_bounded_four_digit_prefix = (
            four_digit_left_extension
            and component_index == 0
            and config["min"] <= 1000
            and config["max"] < 2000
            and best["digit"] == 1
            and best["score"] <= 0.26
            and margin >= 0.20
            and best["coverage"] >= 0.85
        )
        if range_bounded_four_digit_prefix:
            # The S/S+ rank immediately left of a four-digit stat can connect
            # to the narrow leading "1".  The exact digit is nevertheless
            # bounded by the field range (<2000), and is accepted only when
            # the visual ranking independently prefers 1 by a wide margin.
            glyph_reliable = True
            confidence = max(confidence, 0.82)
        x, y, width, height = component["bbox"]
        glyph_evidence = {
            "digit": best["digit"] if glyph_reliable else None,
            "bbox": [roi[0] + x, roi[1] + y, width, height],
            "best_score": round(best["score"], 4),
            "second_score": round(second["score"], 4),
            "margin": round(margin, 4),
            "coverage": round(best["coverage"], 4),
            "confidence": round(confidence, 4),
            "template": best["template"],
            "acceptance_reason": (
                "range_bounded_four_digit_prefix"
                if range_bounded_four_digit_prefix
                else "template"
            ),
            "digit_supported": best["digit"]
            in manifest["glyph_coverage"][style]["supported_digits"],
            "abstained": not glyph_reliable,
        }
        if rank_border_trim_pixels:
            glyph_evidence["rank_border_trim_pixels"] = rank_border_trim_pixels
        if "split_from" in component:
            glyph_evidence["touching_split"] = {
                "source_bbox": [
                    roi[0] + component["split_from"][0],
                    roi[1] + component["split_from"][1],
                    *component["split_from"][2:],
                ],
                "cut_x": roi[0] + component["split_x"],
            }
        evidence["glyphs"].append(glyph_evidence)
        reliable = reliable and glyph_reliable
        if glyph_reliable:
            digits.append(str(best["digit"]))
            confidences.append(confidence)

    best_scores = [glyph["best_score"] for glyph in evidence["glyphs"]]
    margins = [glyph["margin"] for glyph in evidence["glyphs"]]
    coverages = [glyph["coverage"] for glyph in evidence["glyphs"]]
    evidence["score"] = round(1.0 - max(best_scores), 4)
    evidence["margin"] = round(min(margins), 4)
    evidence["coverage"] = round(min(coverages), 4)
    if not reliable or len(digits) != len(components):
        if four_digit_left_extension:
            low_margin = _four_digit_low_margin_value(
                evidence["glyphs"],
                config=config,
                thresholds=thresholds,
            )
            if low_margin is not None:
                value, confidence = low_margin
                evidence["accepted_path"] = "glyphs_four_digit_low_margin"
                evidence["range"] = [config["min"], config["max"]]
                evidence["range_passed"] = config["min"] <= value <= config["max"]
                if evidence["range_passed"]:
                    return _field(value, confidence, evidence)
        return _abstained_field(evidence)

    value = int("".join(digits))
    evidence["range"] = [config["min"], config["max"]]
    evidence["range_passed"] = config["min"] <= value <= config["max"]
    if not evidence["range_passed"]:
        return _abstained_field(evidence)
    return _field(value, min(confidences), evidence)


def _four_digit_low_margin_value(
    glyphs: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    thresholds: dict[str, Any],
) -> tuple[int, float] | None:
    """Best-effort four-digit value when trailing glyphs have real shapes but
    weak template margins.

    This is deliberately narrower than ordinary glyph acceptance:
    - only four-digit stats recovered through the left-extension path;
    - the two leading glyphs must already be reliable (usually 1 and 0);
    - both trailing glyphs must have a recognisable shape (low best score,
      adequate coverage) so occlusion/damage still abstains;
    - the field range must bound the result to a four-digit interval.
    """
    if len(glyphs) != 4:
        return None
    if config.get("min", 0) < 1000 or config.get("max", 0) >= 2000:
        return None
    prefix_digits: list[str] = []
    for glyph in glyphs[:2]:
        digit = glyph.get("digit")
        if glyph.get("abstained") is not False or not isinstance(digit, str):
            return None
        prefix_digits.append(digit)
    if prefix_digits[0] != "1":
        return None
    for glyph in glyphs[2:]:
        if (
            glyph.get("abstained") is not True
            or not isinstance(glyph.get("best_score"), (int, float))
            or not isinstance(glyph.get("coverage"), (int, float))
            or glyph["best_score"] > 0.35
            or glyph["coverage"] < thresholds["min_coverage"]
        ):
            return None
        best = glyph.get("best_score")
        second = glyph.get("second_score")
        margin = (
            float(second) - float(best)
            if isinstance(best, (int, float)) and isinstance(second, (int, float))
            else 0.0
        )
        if margin < 0.0:
            return None
    tail_digits = [str(glyph.get("digit")) for glyph in glyphs[2:]]
    if any(digit not in "0123456789" for digit in tail_digits):
        return None
    value = int("".join(prefix_digits + tail_digits))
    if not (config["min"] <= value <= config["max"]):
        return None
    coverage = min(float(glyph["coverage"]) for glyph in glyphs[2:])
    confidence = round(min(0.70, 0.35 + coverage * 0.35), 4)
    return value, confidence


def _two_digit_g_rank_evidence(
    image: np.ndarray,
    numeric_roi: tuple[int, int, int, int],
) -> dict[str, Any]:
    x1, y1, _x2, y2 = numeric_roi
    rank_roi = (x1 - 42, y1, x1 - 4, y2)
    rank = _crop(image, rank_roi)
    if rank.size == 0:
        return {"accepted": False, "roi": list(rank_roi), "reason": "empty_rank_roi"}
    hsv = cv2.cvtColor(rank, cv2.COLOR_BGR2HSV)
    low_saturation = float(np.mean(hsv[:, :, 1] < 55))
    dark = float(np.mean(hsv[:, :, 2] < 120))
    gray_mid = float(
        np.mean(
            (hsv[:, :, 1] < 55)
            & (hsv[:, :, 2] > 60)
            & (hsv[:, :, 2] < 210)
        )
    )
    gray_mask = (
        (hsv[:, :, 1] < 55)
        & (hsv[:, :, 2] > 60)
        & (hsv[:, :, 2] < 210)
    ).astype(np.uint8)
    normalized_mask = cv2.resize(
        gray_mask,
        G_RANK_TEMPLATE_SIZE,
        interpolation=cv2.INTER_AREA,
    ) >= 0.5
    template_bits = np.unpackbits(
        np.frombuffer(bytes.fromhex(G_RANK_TEMPLATE_HEX), dtype=np.uint8)
    )[: G_RANK_TEMPLATE_SIZE[0] * G_RANK_TEMPLATE_SIZE[1]]
    template_mask = template_bits.reshape(G_RANK_TEMPLATE_SIZE[1], G_RANK_TEMPLATE_SIZE[0]).astype(bool)
    intersection = int(np.sum(normalized_mask & template_mask))
    union = int(np.sum(normalized_mask | template_mask))
    template_iou = intersection / union if union else 0.0
    template_agreement = float(np.mean(normalized_mask == template_mask))
    accepted = (
        low_saturation > 0.90
        and dark > 0.10
        and gray_mid > 0.35
        and template_iou >= 0.85
        and template_agreement >= 0.90
    )
    return {
        "accepted": bool(accepted),
        "roi": list(rank_roi),
        "low_saturation": round(low_saturation, 4),
        "dark": round(dark, 4),
        "gray_mid": round(gray_mid, 4),
        "template_iou": round(template_iou, 4),
        "template_agreement": round(template_agreement, 4),
        "template_source_sha256": G_RANK_TEMPLATE_SOURCE_SHA256,
    }


def _digit_mask(crop: np.ndarray, style: str) -> np.ndarray:
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    if style == "remaining_blue":
        selected = (
            (hsv[:, :, 0] > 90)
            & (hsv[:, :, 0] < 125)
            & (hsv[:, :, 1] > 100)
            & (hsv[:, :, 2] > 120)
        )
    elif style == "remaining":
        selected = (
            ((hsv[:, :, 0] < 10) | (hsv[:, :, 0] > 170))
            & (hsv[:, :, 1] > 100)
            & (hsv[:, :, 2] > 120)
        )
    else:
        selected = (
            (hsv[:, :, 0] >= 5)
            & (hsv[:, :, 0] <= 30)
            & (hsv[:, :, 1] > 80)
            & (hsv[:, :, 2] < (240 if style == "small_gold" else 210))
        )
    return selected.astype(np.uint8) * 255


def _segment_glyphs(
    mask: np.ndarray, style: str, *, split_touching_gold: bool = False,
) -> list[dict[str, Any]]:
    count, _, stats, _ = cv2.connectedComponentsWithStats(
        (mask > 0).astype(np.uint8), 8
    )
    max_width = 45 if style == "remaining" else 23
    components = []
    for index in range(1, count):
        x, y, width, height, area = [int(value) for value in stats[index]]
        if height < 15 or area < 45:
            continue
        components.append(
            {
                "bbox": (x, y, width, height),
                "mask": mask[y : y + height, x : x + width],
            }
        )
    if split_touching_gold and style == "small":
        components = _split_touching_gold_stat(components)
    return sorted(
        (component for component in components if component["bbox"][2] <= max_width),
        key=lambda component: component["bbox"][0],
    )


def _split_touching_gold_stat(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Gold antialiasing can join two neighboring digits by a one-pixel bridge.
    # Only propose a split for the complete four-slot shape (two single glyphs
    # plus one double-width glyph). The caller still verifies the extended
    # four-digit geometry and every original template/confidence threshold.
    wide = [component for component in components if component["bbox"][2] > 23]
    if len(components) != 3 or len(wide) != 1:
        return components
    joined = wide[0]
    x, y, width, height = joined["bbox"]
    if not 24 <= width <= 40 or any(
        abs(component["bbox"][1] - y) > 2
        or abs(component["bbox"][3] - height) > 2
        for component in components
    ):
        return components
    columns = np.count_nonzero(joined["mask"], axis=0)
    cuts = list(range(width // 2 - 2, width // 2 + 3))
    minimum = min(int(columns[cut]) for cut in cuts)
    best_cuts = [cut for cut in cuts if columns[cut] == minimum]
    if len(best_cuts) != 1 or minimum > max(1, height // 10):
        return components
    cut = best_cuts[0]
    pieces = []
    for left, right in ((0, cut), (cut, width)):
        piece = joined["mask"][:, left:right]
        ys, xs = np.where(piece > 0)
        if not xs.size:
            return components
        x1, y1, x2, y2 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
        glyph = piece[y1:y2, x1:x2]
        if y2 - y1 < 15 or np.count_nonzero(glyph) < 45 or x2 - x1 > 23:
            return components
        pieces.append({
            "bbox": (x + left + x1, y + y1, x2 - x1, y2 - y1),
            "mask": glyph,
            "split_from": joined["bbox"],
            "split_x": x + cut,
        })
    return [component for component in components if component is not joined] + pieces


def _normalize_binary_glyph(mask: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    width, height = size
    foreground_y, foreground_x = np.where(mask > 0)
    if foreground_x.size == 0:
        return np.zeros((height, width), dtype=np.uint8)
    cropped = mask[
        foreground_y.min() : foreground_y.max() + 1,
        foreground_x.min() : foreground_x.max() + 1,
    ]
    scale = min((width - 4) / cropped.shape[1], (height - 4) / cropped.shape[0])
    new_width = max(1, round(cropped.shape[1] * scale))
    new_height = max(1, round(cropped.shape[0] * scale))
    resized = cv2.resize(
        cropped, (new_width, new_height), interpolation=cv2.INTER_NEAREST
    )
    normalized = np.zeros((height, width), dtype=np.uint8)
    left = (width - new_width) // 2
    top = (height - new_height) // 2
    normalized[top : top + new_height, left : left + new_width] = resized
    return normalized


def _rank_glyph_digits(
    query: np.ndarray, templates: list[ImageTemplate]
) -> list[dict[str, Any]]:
    best_by_digit: dict[int, dict[str, Any]] = {}
    for template in templates:
        score, coverage = _binary_metrics(query, template.image)
        candidate = {
            "digit": int(template.label),
            "score": score,
            "coverage": coverage,
            "template": template.path,
        }
        previous = best_by_digit.get(int(template.label))
        improves_enough = (
            previous is None
            or previous["score"] - score >= template.min_prior_improvement
        )
        if (previous is None or score < previous["score"]) and improves_enough:
            best_by_digit[int(template.label)] = candidate
    return sorted(best_by_digit.values(), key=lambda candidate: candidate["score"])


def _recognition_confidence(
    best_score: float, margin: float, coverage: float, minimum_margin: float
) -> float:
    margin_confidence = min(0.99, 0.8 + max(0.0, margin - minimum_margin) * 1.5)
    return max(0.0, min(0.99, 1.0 - best_score, coverage, margin_confidence))


def _analyze_target_text(image: np.ndarray) -> dict[str, Any]:
    manifest, assets = _load_assets()
    roi = tuple(manifest["rois"]["target_text"])
    query = _target_text_mask(_crop(image, roi))
    target_config = manifest["target_templates"][0]
    template = assets["targets"][0]
    thresholds = manifest["target_thresholds"]
    characters = []
    for index, block in enumerate(target_config["character_blocks"]):
        relative_roi = tuple(block["roi"])
        score, coverage = _binary_metrics(
            _crop(query, relative_roi), _crop(template.image, relative_roi)
        )
        second_score = 1.0
        margin = second_score - score
        confidence = max(0.0, min(0.99, 1.0 - score, coverage))
        reliable = (
            score <= thresholds["max_character_score"]
            and coverage >= thresholds["min_character_coverage"]
            and confidence >= HIGH_CONFIDENCE
        )
        x1, y1, x2, y2 = relative_roi
        characters.append(
            {
                "index": index,
                "character": block["character"],
                "roi": [roi[0] + x1, roi[1] + y1, roi[0] + x2, roi[1] + y2],
                "best_score": round(score, 4),
                "second_score": second_score,
                "margin": round(margin, 4),
                "coverage": round(coverage, 4),
                "confidence": round(confidence if reliable else 0.0, 4),
                "abstained": not reliable,
                "template": f"{template.path}#character-{index}",
            }
        )

    worst_score = max(item["best_score"] for item in characters)
    minimum_margin = min(item["margin"] for item in characters)
    minimum_coverage = min(item["coverage"] for item in characters)
    evidence = {
        "roi": list(roi),
        "score": round(1.0 - worst_score, 4),
        "margin": round(minimum_margin, 4),
        "best_score": round(worst_score, 4),
        "coverage": round(minimum_coverage, 4),
        "template": template.path,
        "accepted_path": "verified_character_blocks",
        "recognizer_scope": "verified_strings_only_not_ocr",
        "characters": characters,
    }
    if any(item["abstained"] for item in characters):
        return _abstained_field(evidence)
    confidence = min(item["confidence"] for item in characters)
    return _field(template.label, confidence, evidence)


def _target_text_mask(crop: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    selected = (
        (hsv[:, :, 0] >= 5)
        & (hsv[:, :, 0] <= 35)
        & (hsv[:, :, 1] > 55)
        & (hsv[:, :, 2] < 210)
    )
    return selected.astype(np.uint8) * 255


def _analyze_fragments(image: np.ndarray) -> dict[str, Any]:
    manifest, assets = _load_assets()
    slots = [tuple(roi) for roi in manifest["fragment_slots"]]
    size = tuple(manifest["fragment_size"])
    thresholds = manifest["fragment_thresholds"]
    slot_results = []
    for index, roi in enumerate(slots):
        query = cv2.resize(_crop(image, roi), size, interpolation=cv2.INTER_AREA)
        ranked_states = []
        for state, templates in assets["fragments"].items():
            scored = [(_normalized_absdiff(query, item.image), item) for item in templates]
            score, template = min(scored, key=lambda candidate: candidate[0])
            ranked_states.append((score, state, template))
        ranked_states.sort(key=lambda candidate: candidate[0])
        best_score, state, best = ranked_states[0]
        second_score = ranked_states[1][0]
        margin = second_score - best_score
        coverage = 1.0 - best_score
        confidence = _recognition_confidence(
            best_score, margin, coverage, thresholds["min_margin"]
        )
        reliable = (
            best_score <= thresholds["max_best_score"]
            and margin >= thresholds["min_margin"]
            and coverage >= thresholds["min_coverage"]
            and confidence >= HIGH_CONFIDENCE
        )
        slot_results.append(
            {
                "index": index,
                "state": state if reliable else "unknown",
                "confidence": round(confidence if reliable else 0.0, 4),
                "abstained": not reliable,
                "evidence": {
                    "roi": list(roi),
                    "best_score": round(best_score, 4),
                    "second_score": round(second_score, 4),
                    "margin": round(margin, 4),
                    "coverage": round(coverage, 4),
                    "template": best.path,
                },
            }
        )

    evidence = {
        "roi": [list(roi) for roi in slots],
        "score": round(
            min(1.0 - slot["evidence"]["best_score"] for slot in slot_results), 4
        ),
        "margin": round(
            min(slot["evidence"]["margin"] for slot in slot_results), 4
        ),
        "accepted_path": "slot_occupancy",
        "grid": [2, 4],
        "slots": slot_results,
    }
    if any(slot["abstained"] for slot in slot_results):
        return _abstained_field(evidence)
    value = sum(1 for slot in slot_results if slot["state"] == "filled")
    confidence = min(slot["confidence"] for slot in slot_results)
    return _field(value, confidence, evidence)


def _classify_local_template(field: str, image: np.ndarray) -> dict[str, Any]:
    manifest, assets = _load_assets()
    roi = tuple(manifest["rois"][field])
    crop = _crop(image, roi)
    scores = [
        (_normalized_absdiff(crop, template.image), template)
        for template in assets["local"][field]
    ]
    scores.sort(key=lambda item: item[0])
    best_score, best = scores[0]
    second_score = scores[1][0] if len(scores) > 1 else 1.0
    margin = second_score - best_score
    confidence = max(0.0, min(0.99, 1.0 - best_score * 8.0))
    evidence = {
        "roi": list(roi),
        "score": round(1.0 - best_score, 4),
        "margin": round(margin, 4),
        "template": best.path,
    }
    if (
        best_score > manifest["template_thresholds"][field]
        or confidence < HIGH_CONFIDENCE
    ):
        if field == "mood":
            from core.mood_reader import read_mood_label
            return read_mood_label(image)
        return _abstained_field(evidence)
    return _field(best.label, confidence, evidence)


def _analyze_vitality(image: np.ndarray) -> dict[str, Any]:
    crop = _crop(image, VITALITY_ROI)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    mask = (hsv[:, :, 1] > 80) & (hsv[:, :, 2] > 120)
    columns = np.where(mask.mean(axis=0) > 0.2)[0]
    evidence = {"roi": list(VITALITY_ROI), "score": 0.0, "margin": 0.0}
    if columns.size == 0:
        neutral_mid = (
            (hsv[:, :, 1] < 40)
            & (hsv[:, :, 2] > 50)
            & (hsv[:, :, 2] < 180)
        )
        gray_mean = float(gray.mean())
        gray_std = float(gray.std())
        column_span = float(np.ptp(gray.mean(axis=0)))
        evidence.update(
            {
                "neutral_mid_ratio": round(float(neutral_mid.mean()), 4),
                "gray_mean": round(gray_mean, 4),
                "gray_std": round(gray_std, 4),
                "column_span": round(column_span, 4),
            }
        )
        empty_rail = bool(
            neutral_mid.mean() >= 0.90
            and 95.0 <= gray_mean <= 145.0
            and 6.0 <= gray_std <= 25.0
            and column_span >= 35.0
        )
        if empty_rail:
            evidence["accepted_path"] = "empty_rail"
            evidence["score"] = round(float(neutral_mid.mean()), 4)
            return _field(
                {"ratio": 0.0, "percent": 0.0},
                0.9,
                evidence,
            )
        return _abstained_field(evidence)
    ratio = float((columns[-1] - columns[0] + 1) / crop.shape[1])
    ratio = max(0.0, min(1.0, ratio))
    evidence["score"] = round(float(mask.mean()), 4)
    evidence["margin"] = round(min(ratio, 1.0 - ratio), 4)
    return _field(
        {"ratio": round(ratio, 4), "percent": round(ratio * 100.0, 2)},
        0.9,
        evidence,
    )


def _is_valid_for_training(fields: dict[str, dict[str, Any]]) -> bool:
    if not all(field_confidence_accepted(fields.get(name)) for name in REQUIRED_FOR_DECISION):
        return False
    remaining = fields["remaining_turns"].get("value")
    vitality = fields["vitality"].get("value")
    percent = vitality.get("percent") if isinstance(vitality, dict) else None
    return (fields["screen"].get("value") == "main_menu"
            and type(remaining) is int and remaining >= 0
            and type(percent) in (int, float) and 0 <= percent <= 100)


def _numeric_fields_complete(fields):
    return _is_valid_for_training(fields) and all(
        field_confidence_accepted(fields.get(name))
        and type(fields[name].get("value")) is int and fields[name]["value"] >= 0
        for name in OPTIONAL_DECISION_FIELDS)


def _set_training_readiness(result, *, allow_partial):
    fields = result["fields"]
    numeric_complete = _numeric_fields_complete(fields)
    result["numeric_state_complete"] = numeric_complete
    result["valid_for_training"] = _is_valid_for_training(fields) if allow_partial else numeric_complete


def _crop(image: np.ndarray, roi: tuple[int, int, int, int] | list[int]) -> np.ndarray:
    x1, y1, x2, y2 = roi
    return image[y1:y2, x1:x2]


def _red_ratio(crop: np.ndarray) -> float:
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    red = (
        ((hsv[:, :, 0] < 8) | (hsv[:, :, 0] > 170))
        & (hsv[:, :, 1] > 80)
        & (hsv[:, :, 2] > 130)
    )
    return float(red.mean())


def _white_ratio(crop: np.ndarray) -> float:
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    white = (hsv[:, :, 1] < 45) & (hsv[:, :, 2] > 180)
    return float(white.mean())


def _blue_ratio(crop: np.ndarray) -> float:
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    blue = (
        (hsv[:, :, 0] > 90)
        & (hsv[:, :, 0] < 125)
        & (hsv[:, :, 1] > 50)
        & (hsv[:, :, 2] > 100)
    )
    return float(blue.mean())


def _green_ratio(crop: np.ndarray) -> float:
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    green = (
        (hsv[:, :, 0] > 35)
        & (hsv[:, :, 0] < 90)
        & (hsv[:, :, 1] > 60)
        & (hsv[:, :, 2] > 100)
    )
    return float(green.mean())


def _saturated_ratio(crop: np.ndarray) -> float:
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    saturated = (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 110)
    return float(saturated.mean())


def _binary_metrics(left: np.ndarray, right: np.ndarray) -> tuple[float, float]:
    if left.shape != right.shape:
        right = cv2.resize(
            right, (left.shape[1], left.shape[0]), interpolation=cv2.INTER_NEAREST
        )
    left_foreground = left > 0
    right_foreground = right > 0
    intersection = int((left_foreground & right_foreground).sum())
    union = int((left_foreground | right_foreground).sum())
    query_area = int(left_foreground.sum())
    score = 1.0 - intersection / max(union, 1)
    coverage = intersection / max(query_area, 1)
    return float(score), float(coverage)


def _normalized_absdiff(left: np.ndarray, right: np.ndarray) -> float:
    if left.shape != right.shape:
        right = cv2.resize(
            right, (left.shape[1], left.shape[0]), interpolation=cv2.INTER_AREA
        )
    diff = np.abs(left.astype(np.int16) - right.astype(np.int16))
    return float(diff.mean() / 255.0)


@lru_cache(maxsize=1)
def _load_assets() -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(__file__).resolve().parents[1] / "assets" / "main_menu_state_templates"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assets: dict[str, Any] = {
        "glyphs": {},
        "targets": [],
        "fragments": {},
        "local": {},
    }
    for style, entries in manifest["glyph_templates"].items():
        assets["glyphs"][style] = [
            _read_template(
                root,
                entry["path"],
                entry["digit"],
                cv2.IMREAD_GRAYSCALE,
                min_prior_improvement=float(entry.get("min_prior_improvement", 0.0)),
            )
            for entry in entries
        ]
    assets["targets"] = [
        _read_template(root, entry["path"], entry["value"], cv2.IMREAD_GRAYSCALE)
        for entry in manifest["target_templates"]
    ]
    for state, entries in manifest["fragment_templates"].items():
        assets["fragments"][state] = [
            _read_template(root, entry["path"], state, cv2.IMREAD_COLOR)
            for entry in entries
        ]
    for field, entries in manifest["templates"].items():
        assets["local"][field] = [
            _read_template(root, entry["path"], entry["value"], cv2.IMREAD_COLOR)
            for entry in entries
        ]
    return manifest, assets


def _read_template(
    root: Path,
    relative_path: str,
    label: Any,
    read_mode: int,
    *,
    min_prior_improvement: float = 0.0,
) -> ImageTemplate:
    image = cv2.imdecode(np.frombuffer((root / relative_path).read_bytes(), dtype=np.uint8), read_mode)
    if image is None:
        raise FileNotFoundError(root / relative_path)
    return ImageTemplate(
        label=label,
        path=relative_path,
        image=image,
        min_prior_improvement=min_prior_improvement,
    )
