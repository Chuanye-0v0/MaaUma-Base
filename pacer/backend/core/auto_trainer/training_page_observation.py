"""Build bound current stats plus optional training vitality previews."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from .capture_adapter import load_capture_group_raws
from .training_current_stats import FIELD_ORDER, read_training_current_stats
from .larc_vitality_preview import read_overseas_adaptation_gain, read_training_vitality_preview


_SCHEMA_VERSION = "ug_parent_training_page_observation.v1"
_TRAINING_TYPES = ("speed", "stamina", "power", "guts", "wisdom")
_REPORT_KEYS = {"status", "reason", "values", "fields"}


def _manifest_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _base_report(
    *,
    manifest_path: Path,
    actual_group_id: str,
    manifest_sha256: str | None,
    turn_index: int | None = None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "actual_group_id": actual_group_id,
        "turn_index": turn_index,
        "source_manifest": str(manifest_path),
        "source_manifest_sha256": manifest_sha256,
        "tabs": {},
        "current_stats": {},
        "consensus_complete": False,
        "is_valid": False,
        "errors": list(errors or []),
    }


def _invalid(
    *,
    manifest_path: Path,
    actual_group_id: str,
    manifest_sha256: str | None,
    reason: str,
    turn_index: int | None = None,
) -> dict[str, Any]:
    return _base_report(
        manifest_path=manifest_path,
        actual_group_id=actual_group_id,
        manifest_sha256=manifest_sha256,
        turn_index=turn_index,
        errors=[reason],
    )


def _canonical_image(path: Path) -> np.ndarray | None:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        return None
    if image.shape == (1280, 720, 3):
        return image
    if image.shape == (2560, 1440, 3):
        return cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
    return None


def _valid_reader_report(report: Any) -> bool:
    if not isinstance(report, dict) or set(report) != _REPORT_KEYS:
        return False
    if report.get("status") != "ok":
        return False
    values = report.get("values")
    fields = report.get("fields")
    if not isinstance(values, dict) or set(values) != set(FIELD_ORDER):
        return False
    if not isinstance(fields, dict) or set(fields) != set(FIELD_ORDER):
        return False
    for field in FIELD_ORDER:
        value = values[field]
        if type(value) is not int or value < 0:
            return False
        field_report = fields[field]
        if not isinstance(field_report, dict):
            return False
        if field_report.get("status") != "accepted":
            return False
        if field_report.get("value") != value:
            return False
    return True


def build_training_page_observation(
    manifest_path: str | Path,
    execution_root: str | Path,
    actual_group_id: str,
    output_path: str | Path,
    *,
    reader: Callable[[np.ndarray], dict[str, Any]] = read_training_current_stats,
    growth_reader: Callable | None = None,
    main_menu_context: dict[str, Any] | None = None,
    overwrite: bool = False,
    pretty: bool = False,
) -> dict[str, Any]:
    """Keep page observations; normal L'Arc uses one current-stat source."""
    manifest_path = Path(manifest_path)
    output_path = Path(output_path)
    manifest_sha256 = _manifest_sha256(manifest_path)
    if manifest_sha256 is None:
        return _invalid(
            manifest_path=manifest_path,
            actual_group_id=actual_group_id,
            manifest_sha256=None,
            reason="capture_manifest_unreadable",
        )
    if output_path.exists() and not overwrite:
        return _invalid(
            manifest_path=manifest_path,
            actual_group_id=actual_group_id,
            manifest_sha256=manifest_sha256,
            reason="training_page_observation_output_exists",
        )

    try:
        raw_by_tab = load_capture_group_raws(
            manifest_path=manifest_path,
            execution_root=execution_root,
            actual_group_id=actual_group_id,
            formal_capture=True,
        )
    except (OSError, ValueError) as exc:
        return _invalid(
            manifest_path=manifest_path,
            actual_group_id=actual_group_id,
            manifest_sha256=manifest_sha256,
            reason=str(exc),
        )

    turn_indexes = {raw.get("turn_index") for raw in raw_by_tab.values()}
    turn_index = next(iter(turn_indexes)) if len(turn_indexes) == 1 else None
    if len(turn_indexes) != 1 or type(turn_index) is not int:
        return _invalid(
            manifest_path=manifest_path,
            actual_group_id=actual_group_id,
            manifest_sha256=manifest_sha256,
            reason="capture_group_turn_index_mismatch",
            turn_index=turn_index,
        )

    tabs: dict[str, Any] = {}
    errors: list[str] = []
    warnings: list[str] = []
    lightweight = (isinstance(main_menu_context, dict)
        and main_menu_context.get("scenario_state", {}).get("scenario") == "larc")
    if lightweight and main_menu_context.get("turn") != turn_index:
        return _invalid(manifest_path=manifest_path, actual_group_id=actual_group_id,
            manifest_sha256=manifest_sha256, turn_index=turn_index,
            reason="capture_context_turn_mismatch")
    consensus: dict[str, int] | None = None
    basis = "five_page_consensus"
    primary_tab = None
    current = main_menu_context.get("current_stats", {}) if lightweight else {}
    if lightweight and all(type(current.get(name)) is int and current[name] >= 0 for name in FIELD_ORDER):
        consensus = {name: current[name] for name in FIELD_ORDER}
        basis = "main_menu_snapshot"
    for tab in _TRAINING_TYPES:
        raw = raw_by_tab[tab]
        image = _canonical_image(Path(str(raw["absolute_path"])))
        if image is None:
            errors.append(f"training_current_stats_image_invalid:{tab}")
            continue
        if lightweight and consensus is not None:
            # These attributes are unchanged by switching training tabs. Do not
            # OCR them five more times or claim they were reread from this page.
            report = {"status": "not_rechecked", "reason": "current_stats_already_available",
                      "values": {}, "fields": {}}
        else:
            try:
                report = reader(image)
            except Exception:
                report = None
        tabs[tab] = {
            "sha256": raw["sha256"], "manifest_sha256": raw["sha256"],
            "relative_path": raw["relative_path"],
            "status": report.get("status") if isinstance(report, dict) else "unreadable",
            "reason": report.get("reason") if isinstance(report, dict) else "reader_unavailable",
            "values": report.get("values") if isinstance(report, dict) else {},
            "fields": report.get("fields") if isinstance(report, dict) else {},
        }
        try:
            tabs[tab]["vitality_preview"] = read_training_vitality_preview(image, tab)
        except Exception as exc:
            tabs[tab]["vitality_preview"] = {"status": "abstained", "reason": "preview_reader_unavailable"}
            warnings.append(f"vitality_preview_unavailable:{tab}:{type(exc).__name__}")
        try:
            tabs[tab]["adaptation_gain_preview"] = read_overseas_adaptation_gain(image)
        except Exception:
            tabs[tab]["adaptation_gain_preview"] = {"status": "abstained", "reason": "adaptation_gain_reader_unavailable"}
        if growth_reader is not None:
            try:
                tabs[tab]["growth_effect_model"] = growth_reader(image, tab)
            except Exception as exc:
                tabs[tab]["growth_effect_model"] = {"status": "abstained", "reason": "growth_reader_unavailable"}
                warnings.append(f"growth_model_unavailable:{tab}:{type(exc).__name__}")
        if lightweight and isinstance(report, dict) and report.get("status") == "not_rechecked":
            continue
        if not _valid_reader_report(report):
            reason = (
                f"training_current_stats_incomplete:{tab}"
                if isinstance(report, dict) and report.get("status") != "ok"
                else f"training_current_stats_fields_invalid:{tab}"
            )
            (warnings if lightweight else errors).append(reason)
            continue
        values = dict(report["values"])
        if consensus is None:
            consensus = values
            if lightweight:
                basis, primary_tab = "training_page_primary", tab
        elif values != consensus:
            errors.append(f"training_current_stats_conflict:{tab}")
        tabs[tab] = {
            "sha256": raw["sha256"],
            "manifest_sha256": raw["sha256"],
            "relative_path": raw["relative_path"],
            "status": report["status"],
            "reason": report["reason"],
            "values": values,
            "fields": report["fields"],
            "vitality_preview": tabs[tab]["vitality_preview"],
            **({"adaptation_gain_preview": tabs[tab]["adaptation_gain_preview"]} if "adaptation_gain_preview" in tabs[tab] else {}),
            **({"growth_effect_model": tabs[tab]["growth_effect_model"]} if "growth_effect_model" in tabs[tab] else {}),
        }

    if errors or consensus is None or len(tabs) != len(_TRAINING_TYPES):
        result = _base_report(
            manifest_path=manifest_path,
            actual_group_id=actual_group_id,
            manifest_sha256=manifest_sha256,
            turn_index=turn_index,
            errors=errors or ["training_current_stats_incomplete"],
        )
        result["tabs"] = tabs
        return result

    result = _base_report(
        manifest_path=manifest_path,
        actual_group_id=actual_group_id,
        manifest_sha256=manifest_sha256,
        turn_index=turn_index,
    )
    result.update(
        {
            "tabs": tabs,
            "current_stats": consensus,
            "consensus_complete": True,
            "is_valid": True,
            "errors": [],
            "warnings": warnings,
            "current_stats_basis": basis,
            "current_stats_primary_tab": primary_tab,
            "current_stats_context_turn": turn_index if lightweight else None,
        }
    )
    try:
        serialized = json.dumps(
            result,
            ensure_ascii=False,
            indent=2 if pretty else None,
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return _invalid(
            manifest_path=manifest_path,
            actual_group_id=actual_group_id,
            manifest_sha256=manifest_sha256,
            reason="training_page_observation_not_serializable",
            turn_index=turn_index,
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(serialized + "\n", encoding="utf-8")
    return result


__all__ = ["build_training_page_observation"]
