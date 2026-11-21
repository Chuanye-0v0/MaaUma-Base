"""Fail-closed reader for the six current values on a training page."""

from __future__ import annotations

from typing import Any
import copy
import hashlib
import math

import numpy as np

from core import numeric_ocr_crosscheck as numeric_crosscheck
from core.main_menu_state import (
    _analyze_numeric_field,
    _crop,
    _load_assets,
    numeric_field_accepted,
    supplement_numeric_field,
)


FIELD_ORDER = ("speed", "stamina", "power", "guts", "wisdom", "skill_pt")
_STAT_FIELDS = ("speed", "stamina", "power", "guts", "wisdom")
_SKILL_BACKGROUND_BBOX = (586, 862, 602, 892)


def _supplement_capped_training_stat(
    field: str,
    image: np.ndarray,
    primary: dict[str, Any],
    *,
    numeric_reader: Any = None,
    source_frame_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Recover a capped training-page stat displaying '最大' over '/<cap>'."""
    if field not in _STAT_FIELDS or not numeric_crosscheck.crosscheck_enabled():
        return None
    config, _ = _load_assets()
    base_roi = list(config["rois"][field])
    val_roi = [max(0, base_roi[0] - 8), base_roi[1], base_roi[2], base_roi[3]]
    cap_roi = [max(0, base_roi[0] - 8), 880, min(720, base_roi[2] + 4), 906]
    try:
        reader = numeric_reader
        if reader is None:
            from core.auto_trainer.frame_safety import LocalRapidOCRScreenTextReader

            reader = LocalRapidOCRScreenTextReader()
        val_crop = _crop(image, val_roi)
        raw_label, label_conf = (
            reader.read_line(val_crop)
            if hasattr(reader, "read_line")
            else reader(val_crop)
        )
        if isinstance(label_conf, bool):
            return None
        label_conf = float(label_conf)
    except Exception:
        return None
    threshold = numeric_crosscheck.second_opinion_min_score()
    if (
        str(raw_label).strip() != "最大"
        or not math.isfinite(label_conf)
        or not max(0.85, threshold) <= label_conf <= 1.0
    ):
        return None
    second = numeric_crosscheck.read_number(
        _crop(image, cap_roi),
        reader=reader,
        minimum=1200,
        maximum=config["numeric_fields"][field]["max"],
        prefixes=("/", ""),
        suffixes=("",),
    )
    if not isinstance(second, dict) or second.get("status") != "accepted":
        return None
    evidence = (
        copy.deepcopy(primary.get("evidence"))
        if isinstance(primary.get("evidence"), dict)
        else {}
    )
    primary_summary = {
        key: copy.deepcopy(primary.get(key))
        for key in ("value", "confidence", "abstained")
    }
    proof = {
        "schema_version": "uma.numeric_ocr_secondary.v1",
        "decision": "secondary_accepted",
        "field": field,
        "primary": primary_summary,
        "second": copy.deepcopy(second),
        "roi": cap_roi,
        "minimum": 1200,
        "maximum": config["numeric_fields"][field]["max"],
        "min_score": threshold,
        "source_frame_sha256": source_frame_sha256,
    }
    candidate = {
        "value": second.get("value"),
        "confidence": second.get("confidence"),
        "abstained": False,
        "source": "numeric_ocr_secondary",
        "evidence": {
            **evidence,
            "capped_max_label": {
                "text": "最大",
                "confidence": round(label_conf, 4),
                "roi": val_roi,
            },
            "numeric_crosscheck": proof,
        },
    }
    return candidate if numeric_field_accepted(candidate) else None


def _empty_field(reason: str, *, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "value": None,
        "confidence": 0.0,
        "abstained": True,
        "status": "unavailable",
        "reason": reason,
        "evidence": evidence or {},
    }


def _unavailable(reason: str) -> dict[str, Any]:
    fields = {}
    for field in FIELD_ORDER:
        evidence = {}
        if field == "skill_pt":
            evidence["training_background_suppressed"] = list(
                _SKILL_BACKGROUND_BBOX
            )
        fields[field] = _empty_field(reason, evidence=evidence)
    return {
        "status": "unavailable",
        "reason": reason,
        "values": {field: None for field in FIELD_ORDER},
        "fields": fields,
    }


def _field_evidence(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _empty_field("numeric_field_mapping_invalid")

    value = raw.get("value")
    confidence = raw.get("confidence")
    evidence = raw.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    try:
        confidence = float(confidence)
    except (TypeError, ValueError, OverflowError):
        confidence = 0.0

    # This reader already accepted finite non-abstained primary values (including
    # the existing .70 four-digit template path). Keep that primary contract;
    # lower-score secondary values require the shared explicit provenance check.
    accepted = numeric_field_accepted(raw, primary_threshold=0.0)
    if accepted:
        return {
            "value": value,
            "confidence": confidence,
            "abstained": False,
            "status": "accepted",
            "reason": "secondary_accepted" if raw.get("source") == "numeric_ocr_secondary" else "template_accepted",
            **({"source": "numeric_ocr_secondary"} if raw.get("source") == "numeric_ocr_secondary" else {}),
            "evidence": evidence,
        }

    return {
        "value": None,
        "confidence": confidence if np.isfinite(confidence) else 0.0,
        "abstained": True,
        "status": "unavailable",
        "reason": evidence.get("rejection_reason", "template_abstained"),
        "evidence": evidence,
    }


def read_training_current_stats(image: Any, *, numeric_reader: Any = None) -> dict[str, Any]:
    """Read bottom-panel current values without invoking main-menu state."""

    try:
        array = np.asarray(image)
    except Exception:
        return _unavailable("numeric_image_size_invalid")
    if array.shape != (1280, 720, 3):
        return _unavailable("numeric_image_size_invalid")

    fields: dict[str, dict[str, Any]] = {}
    frame_sha256 = hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()
    for field in FIELD_ORDER:
        field_image = array
        if field == "skill_pt":
            field_image = array.copy()
            x1, y1, x2, y2 = _SKILL_BACKGROUND_BBOX
            field_image[y1:y2, x1:x2] = 255
        primary = _analyze_numeric_field(field, field_image)
        supplemented = supplement_numeric_field(field, field_image, primary,
            numeric_reader=numeric_reader, site="training_current_stats", primary_threshold=0.0,
            source_frame_sha256=frame_sha256)
        field_result = _field_evidence(supplemented)
        if field_result["status"] != "accepted" and field in _STAT_FIELDS:
            capped = _supplement_capped_training_stat(
                field,
                field_image,
                primary,
                numeric_reader=numeric_reader,
                source_frame_sha256=frame_sha256,
            )
            if capped is not None:
                field_result = _field_evidence(capped)
        if field == "skill_pt":
            field_result["evidence"]["training_background_suppressed"] = list(
                _SKILL_BACKGROUND_BBOX
            )
        fields[field] = field_result

    values = {
        field: fields[field]["value"]
        if fields[field]["status"] == "accepted" else None
        for field in FIELD_ORDER
    }
    unavailable = next(
        (field for field in FIELD_ORDER if fields[field]["status"] != "accepted"),
        None,
    )
    if unavailable is not None:
        return {
            "status": "unavailable",
            "reason": f"field_unavailable:{unavailable}",
            "values": values,
            "fields": fields,
        }
    return {"status": "ok", "reason": None, "values": values, "fields": fields}


__all__ = ["FIELD_ORDER", "read_training_current_stats"]
