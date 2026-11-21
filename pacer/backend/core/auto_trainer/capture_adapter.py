from __future__ import annotations

import hashlib
import json
import stat
import re
from statistics import median
from pathlib import Path
from typing import Any

from core.auto_trainer.pacer_storage import _io
from core.auto_trainer.state import TRAINING_TYPES, TrainingTurnState
from core.training_gain_reliability import revalidate_formal_gain_option


_TRAINING_PAGE_OBSERVATION_SCHEMA = "ug_parent_training_page_observation.v1"
_CURRENT_STAT_FIELDS = (
    "speed",
    "stamina",
    "power",
    "guts",
    "wisdom",
    "skill_pt",
)


def build_state_from_capture_artifacts(
    *,
    manifest_path: str | Path,
    execution_root: str | Path,
    actual_group_id: str,
    training_options_path: str | Path | None = None,
    training_page_observation_path: str | Path | None = None,
    state_context: dict[str, Any] | None = None,
    formal_capture: bool = True,
) -> TrainingTurnState:
    manifest_path = Path(manifest_path)
    execution_root = Path(execution_root)
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("capture_manifest_root_not_object")

    group = _find_group(manifest, actual_group_id)
    raw_by_tab = _raw_files_by_tab(group, execution_root, formal_capture=formal_capture)
    observation_stats = None
    page_observation = None
    if training_page_observation_path is not None:
        page_observation = _load_training_page_observation(
            path=Path(training_page_observation_path),
            manifest_path=manifest_path,
            group=group,
            raw_by_tab=raw_by_tab,
            actual_group_id=actual_group_id,
        )
        observation_stats = page_observation["current_stats"]
    sidecar_options, sidecar_warnings = _load_sidecar_options(
        training_options_path,
        formal_capture=formal_capture,
        manifest_path=manifest_path,
        execution_root=execution_root,
        actual_group_id=actual_group_id,
        raw_by_tab=raw_by_tab,
    )

    state_context = dict(state_context or {})
    context_turn = state_context.get("turn")
    group_turn = group.get("turn_index")
    if formal_capture:
        if type(context_turn) is not int:
            raise ValueError("capture_context_turn_invalid")
        if context_turn != group_turn:
            raise ValueError("capture_context_turn_mismatch")
    context_stats = state_context.get("current_stats", {})
    larc = state_context.get("scenario_state", {}).get("scenario") == "larc"
    warnings = [str(item) for item in state_context.get("warnings", [])]
    if page_observation is not None:
        warnings.extend(str(item) for item in page_observation.get("warnings", []))
    complete_context = all(type(context_stats.get(name)) is int for name in _CURRENT_STAT_FIELDS)
    if observation_stats is not None:
        if context_stats and context_stats != observation_stats:
            if not larc:
                raise ValueError("training_current_stats_conflict")
            if any(value is not None and observation_stats.get(name)!=value for name,value in context_stats.items()):
                warnings.append("training_current_stats_difference_not_reconciled")
            elif not complete_context:
                warnings.append("current_stats_completed_from_training_page")
        if not context_stats or (larc and not complete_context):
            state_context["current_stats"] = observation_stats
    warnings.extend(sidecar_warnings)

    training_options: list[dict[str, Any]] = []
    vitality_previews = {}
    for tab in TRAINING_TYPES:
        raw = raw_by_tab[tab]
        option = dict(sidecar_options.get(tab, {"training_type": tab, "confidence": 0.0}))
        if formal_capture and option:
            option = revalidate_formal_gain_option(option)
        option["training_type"] = tab
        option_raw = dict(option.get("raw_data", {})) if isinstance(option.get("raw_data"), dict) else {}
        option_raw["capture"] = raw
        option["raw_data"] = option_raw
        if larc and page_observation is not None:
            from core.auto_trainer.larc_vitality_preview import accepted_preview

            preview = page_observation["tabs"][tab].get("vitality_preview")
            if isinstance(preview, dict):
                if accepted_preview(preview) and preview.get("training_type") == tab:
                    vitality_previews[tab] = preview
                    preview = {**preview, "source": {
                        key: raw.get(key) for key in ("run_session_id", "turn_index", "sha256", "absolute_path")}}
                elif preview.get("status") != "abstained":
                    preview = {"status": "abstained", "reason": "vitality_preview_fields_invalid"}
                mechanics = dict(option.get("scenario_mechanics") or {})
                mechanics["larc"] = {**(mechanics.get("larc") or {}), "vitality_preview": preview}
                option["scenario_mechanics"] = mechanics
            growth = page_observation["tabs"][tab].get("growth_effect_model")
            if isinstance(growth, dict) and growth.get("status") == "accepted":
                mechanics = dict(option.get("scenario_mechanics") or {})
                mechanics["larc"] = {**(mechanics.get("larc") or {}), "growth_effect_model": growth}
                option["scenario_mechanics"] = mechanics
            adapt_gain = page_observation["tabs"][tab].get("adaptation_gain_preview")
            if isinstance(adapt_gain, dict) and adapt_gain.get("status") == "accepted":
                ap_val = adapt_gain.get("adaptation_pt")
                if type(ap_val) is int and ap_val >= 0:
                    mechanics = dict(option.get("scenario_mechanics") or {})
                    larc_m = dict(mechanics.get("larc") or {})
                    te = dict(larc_m.get("training_effects") or {})
                    te.update({
                        "status": "accepted",
                        "adaptation_pt": ap_val,
                        "source": {
                            key: raw.get(key)
                            for key in ("run_session_id", "turn_index", "sha256", "absolute_path")
                        },
                    })
                    larc_m["training_effects"] = te
                    mechanics["larc"] = larc_m
                    option["scenario_mechanics"] = mechanics
        training_options.append(option)

    scenario_state = dict(state_context.get("scenario_state", {}))
    if larc and type(state_context.get('remaining_turns')) is int:
        scenario_state['larc_remaining_turns']=state_context['remaining_turns']
    for context_key in ("period_text", "target_text"):
        if context_key in state_context and context_key not in scenario_state:
            scenario_state[context_key] = state_context[context_key]

    vitality = state_context.get("vitality")
    if larc and page_observation is not None:
        current = [row["current_percent"] for row in vitality_previews.values()]
        if len(current) >= 3 and max(current) - min(current) <= 2:
            # The main-menu reader's fixed crop can under-measure the full
            # width after capacity grows. Use agreeing training-bar readings.
            vitality = round(median(current))
            scenario_state["larc_vitality"] = {"status": "accepted", "unit": "percent_of_current_max",
                "current_percent": round(median(current), 2), "basis": "training_bar_consensus",
                "training_types": list(vitality_previews)}
        else:
            scenario_state["larc_vitality"] = {"status": "abstained", "reason": "vitality_preview_consensus_unavailable"}

    state_data = {
        "screen": "training_menu",
        "turn": context_turn if context_turn is not None else group_turn,
        "vitality": vitality,
        "current_stats": state_context.get("current_stats", {}),
        "scenario_state": scenario_state,
        "training_options": training_options,
        "warnings": warnings,
        "errors": [str(item) for item in state_context.get("errors", [])],
    }
    return TrainingTurnState.from_dict(state_data)


def load_capture_group_raws(
    *,
    manifest_path: str | Path,
    execution_root: str | Path,
    actual_group_id: str,
    formal_capture: bool = True,
) -> dict[str, dict[str, Any]]:
    manifest = _read_json(Path(manifest_path))
    if not isinstance(manifest, dict):
        raise ValueError("capture_manifest_root_not_object")
    group = _find_group(manifest, actual_group_id)
    return _raw_files_by_tab(group, Path(execution_root), formal_capture=formal_capture)


def _read_json(path: Path) -> Any:
    return json.loads(_io(path).read_text(encoding="utf-8-sig"))


def _find_group(manifest: dict[str, Any], actual_group_id: str) -> dict[str, Any]:
    for group in manifest.get("groups", []):
        if isinstance(group, dict) and group.get("actual_group_id") == actual_group_id:
            return group
    raise ValueError(f"capture_group_not_found:{actual_group_id}")


def _load_training_page_observation(
    *,
    path: Path,
    manifest_path: Path,
    group: dict[str, Any],
    raw_by_tab: dict[str, dict[str, Any]],
    actual_group_id: str,
) -> dict[str, Any]:
    if not _io(path).is_file():
        raise ValueError("training_page_observation_missing")
    try:
        data = json.loads(_io(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("training_page_observation_invalid_json") from exc
    if not isinstance(data, dict):
        raise ValueError("training_page_observation_root_invalid")
    if data.get("schema_version") != _TRAINING_PAGE_OBSERVATION_SCHEMA:
        raise ValueError("training_page_observation_schema_mismatch")
    if data.get("actual_group_id") != actual_group_id:
        raise ValueError("training_page_observation_group_mismatch")
    if type(data.get("turn_index")) is not int or data["turn_index"] != group.get("turn_index"):
        raise ValueError("training_page_observation_turn_mismatch")
    try:
        manifest_sha = hashlib.sha256(_io(manifest_path).read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError("training_page_observation_manifest_unreadable") from exc
    if str(data.get("source_manifest_sha256", "")).lower() != manifest_sha:
        raise ValueError("training_page_observation_manifest_sha256_mismatch")
    if data.get("is_valid") is not True or data.get("consensus_complete") is not True:
        raise ValueError("training_page_observation_incomplete")
    if data.get("errors") != []:
        raise ValueError("training_page_observation_incomplete")
    lightweight = data.get("current_stats_basis") in {"main_menu_snapshot", "training_page_primary"}
    if lightweight and data.get("current_stats_context_turn") != group.get("turn_index"):
        raise ValueError("training_page_observation_turn_mismatch")

    tabs = data.get("tabs")
    if not isinstance(tabs, dict):
        raise ValueError("training_page_observation_fields_invalid")
    for tab in TRAINING_TYPES:
        entry = tabs.get(tab)
        if not isinstance(entry, dict):
            raise ValueError(f"training_page_observation_tab_sha256_mismatch:{tab}")
        expected_sha = str(raw_by_tab[tab].get("sha256", "")).lower()
        if (
            str(entry.get("sha256", "")).lower() != expected_sha
            or str(entry.get("manifest_sha256", "")).lower() != expected_sha
        ):
            raise ValueError(f"training_page_observation_tab_sha256_mismatch:{tab}")
        if lightweight and entry.get("status") != "ok":
            # Optional per-page stat OCR is not an execution prerequisite. Raw
            # page/run binding and the one complete primary reading still are.
            continue
        if entry.get("status") != "ok":
            raise ValueError("training_page_observation_fields_invalid")
        values = entry.get("values")
        if not isinstance(values, dict) or set(values) != set(_CURRENT_STAT_FIELDS):
            raise ValueError("training_page_observation_fields_invalid")
        for field in _CURRENT_STAT_FIELDS:
            if type(values[field]) is not int or values[field] < 0:
                raise ValueError("training_page_observation_fields_invalid")
        fields = entry.get("fields")
        if not isinstance(fields, dict) or set(fields) != set(_CURRENT_STAT_FIELDS):
            raise ValueError("training_page_observation_fields_invalid")
        for field in _CURRENT_STAT_FIELDS:
            field_report = fields[field]
            if (
                not isinstance(field_report, dict)
                or field_report.get("status") != "accepted"
                or field_report.get("value") != values[field]
            ):
                raise ValueError("training_page_observation_fields_invalid")

    current_stats = data.get("current_stats")
    if not isinstance(current_stats, dict) or set(current_stats) != set(_CURRENT_STAT_FIELDS):
        raise ValueError("training_page_observation_fields_invalid")
    for field in _CURRENT_STAT_FIELDS:
        if type(current_stats[field]) is not int or current_stats[field] < 0:
            raise ValueError("training_page_observation_fields_invalid")
    if lightweight and data.get("current_stats_basis") == "training_page_primary":
        primary = tabs.get(data.get("current_stats_primary_tab"), {})
        if primary.get("status") != "ok" or primary.get("values") != current_stats:
            raise ValueError("training_page_observation_fields_invalid")
    if not lightweight:
        for tab in TRAINING_TYPES:
            if tabs[tab]["values"] != current_stats:
                raise ValueError("training_page_observation_fields_invalid")
    return data


def _raw_files_by_tab(
    group: dict[str, Any],
    execution_root: Path,
    *,
    formal_capture: bool,
) -> dict[str, dict[str, Any]]:
    actual_group_id = group.get("actual_group_id")
    if formal_capture and (not isinstance(actual_group_id, str) or not actual_group_id):
        raise ValueError("capture_group_missing_actual_group_id")
    if formal_capture and type(group.get("turn_index")) is not int:
        raise ValueError("capture_group_missing_turn_index")
    raw_by_tab: dict[str, dict[str, Any]] = {}
    seen_sha256: set[str] = set()
    for raw in group.get("raw_files", []):
        if not isinstance(raw, dict):
            continue
        source_screen = str(raw.get("source_screen", ""))
        if source_screen.endswith("_tab"):
            tab = source_screen.removesuffix("_tab")
        else:
            tab = source_screen
        if tab not in TRAINING_TYPES:
            continue
        if tab in raw_by_tab:
            raise ValueError(f"capture_raw_duplicate_tab:{tab}")
        relative_path = str(raw.get("relative_path", "")).replace("\\", "/")
        if not relative_path:
            raise ValueError(f"capture_raw_missing_relative_path:{tab}")
        absolute_path = execution_root / Path(relative_path)
        expected_sha = raw.get("sha256")
        if formal_capture:
            if not isinstance(expected_sha, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha) is None:
                raise ValueError(f"capture_raw_invalid_sha256:{tab}")
            if _formal_path_has_symlink_or_reparse_component(execution_root, Path(relative_path)):
                raise ValueError(f"capture_raw_symlink_provenance:{tab}")
            if not _io(absolute_path).is_file():
                raise ValueError(f"capture_raw_missing_file:{tab}")
            try:
                absolute_path.resolve().relative_to(execution_root.resolve())
            except ValueError as exc:
                raise ValueError(f"capture_raw_outside_execution_root:{tab}") from exc
            parts = Path(relative_path).parts
            if actual_group_id not in parts:
                raise ValueError(f"capture_raw_group_binding_mismatch:{tab}")
            group_index = parts.index(actual_group_id)
            group_root = execution_root.joinpath(*parts[: group_index + 1]).resolve()
            try:
                absolute_path.resolve().relative_to(group_root)
            except ValueError as exc:
                raise ValueError(f"capture_raw_group_binding_mismatch:{tab}") from exc
            actual_sha = hashlib.sha256(_io(absolute_path).read_bytes()).hexdigest()
            if actual_sha.lower() != expected_sha.lower():
                raise ValueError(f"capture_raw_sha256_mismatch:{tab}")
            normalized_sha = expected_sha.lower()
            if normalized_sha in seen_sha256:
                raise ValueError(f"capture_raw_duplicate_sha256:{tab}")
            seen_sha256.add(normalized_sha)
        raw_by_tab[tab] = {
            "tab": tab,
            "relative_path": relative_path,
            "absolute_path": str(absolute_path),
            "source_screen": source_screen,
            "sequence": raw.get("sequence"),
            "sha256": expected_sha,
            "exists": _io(absolute_path).is_file(),
            "actual_group_id": actual_group_id,
            "turn_index": group.get("turn_index"),
            "run_session_id": group.get("run_session_id"),
        }

    missing = [tab for tab in TRAINING_TYPES if tab not in raw_by_tab]
    if missing:
        raise ValueError(f"capture_raw_missing_tabs:{','.join(missing)}")
    return raw_by_tab


def _formal_path_has_symlink_or_reparse_component(
    execution_root: Path,
    relative_path: Path,
) -> bool:
    """Reject indirection before formal provenance reads or SHA validation."""
    if relative_path.is_absolute():
        return True
    current = execution_root
    if _path_is_symlink_or_reparse(current):
        return True
    for part in relative_path.parts:
        if part in {"", ".", ".."}:
            return True
        current = current / part
        if _path_is_symlink_or_reparse(current):
            return True
    return False


def _path_is_symlink_or_reparse(path: Path) -> bool:
    try:
        info = _io(path).lstat()
        attributes = getattr(info, "st_file_attributes", 0)
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(attributes & 0x0400)


def _load_sidecar_options(
    training_options_path: str | Path | None,
    *,
    formal_capture: bool,
    manifest_path: Path,
    execution_root: Path,
    actual_group_id: str,
    raw_by_tab: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    if training_options_path is None:
        return {}, ["training_options_sidecar_missing"]
    data = _read_json(Path(training_options_path))
    if formal_capture:
        _validate_formal_sidecar_wrapper(
            data,
            manifest_path=manifest_path,
            execution_root=execution_root,
            actual_group_id=actual_group_id,
        )
        options = data["training_options"]
    elif isinstance(data, dict):
        options = data.get("training_options", [])
    else:
        options = data
    if not isinstance(options, list):
        raise ValueError("training_options_sidecar_not_list")

    parsed: dict[str, dict[str, Any]] = {}
    for item in options:
        if not isinstance(item, dict):
            continue
        tab = str(item.get("training_type", ""))
        if tab not in TRAINING_TYPES:
            raise ValueError(f"training_options_sidecar_unknown_tab:{tab}")
        if tab in parsed:
            raise ValueError(f"training_options_sidecar_duplicate_tab:{tab}")
        parsed_item = dict(item)
        if formal_capture:
            _validate_formal_sidecar_capture_binding(
                parsed_item,
                raw_by_tab[tab],
            )
        parsed[tab] = parsed_item
    missing = [tab for tab in TRAINING_TYPES if tab not in parsed]
    if missing:
        raise ValueError(f"training_options_sidecar_missing_tabs:{','.join(missing)}")
    return parsed, []


def _validate_formal_sidecar_wrapper(
    data: Any,
    *,
    manifest_path: Path,
    execution_root: Path,
    actual_group_id: str,
) -> None:
    if not isinstance(data, dict):
        raise ValueError("training_options_sidecar_formal_wrapper_required")
    if data.get("schema_version") != "ug_parent_training_options_sidecar.v1":
        raise ValueError("training_options_sidecar_schema_invalid")
    if data.get("is_valid") is not True:
        raise ValueError("training_options_sidecar_not_valid")
    for field in ("errors", "warnings"):
        if not isinstance(data.get(field), list) or data[field]:
            raise ValueError(f"training_options_sidecar_{field}_not_empty")
    if data.get("actual_group_id") != actual_group_id:
        raise ValueError("training_options_sidecar_group_binding_mismatch")
    if not _equivalent_path(data.get("source_manifest"), manifest_path):
        raise ValueError("training_options_sidecar_manifest_binding_mismatch")
    if not _equivalent_path(data.get("execution_root"), execution_root):
        raise ValueError("training_options_sidecar_root_binding_mismatch")
    if not isinstance(data.get("training_options"), list):
        raise ValueError("training_options_sidecar_not_list")


def _equivalent_path(value: Any, expected: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return Path(value).resolve() == expected.resolve()
    except OSError:
        return False


def _validate_formal_sidecar_capture_binding(
    option: dict[str, Any],
    raw: dict[str, Any],
) -> None:
    raw_data = option.get("raw_data")
    capture = raw_data.get("capture") if isinstance(raw_data, dict) else None
    if not isinstance(capture, dict):
        raise ValueError("training_options_sidecar_capture_binding_missing")
    expected_types = {
        "tab": str,
        "actual_group_id": str,
        "turn_index": int,
        "sha256": str,
    }
    for field, expected_type in expected_types.items():
        captured = capture.get(field)
        authoritative = raw.get(field)
        if (
            type(captured) is not expected_type
            or type(authoritative) is not expected_type
            or captured != authoritative
        ):
            raise ValueError(f"training_options_sidecar_capture_binding_{field}")
    if re.fullmatch(r"[0-9a-fA-F]{64}", capture["sha256"]) is None:
        raise ValueError("training_options_sidecar_capture_binding_sha256")
