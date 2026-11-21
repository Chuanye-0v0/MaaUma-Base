"""Pure schema validation for verified APK reference payloads."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


_COLOR_PATTERN = re.compile(r"[0-9A-Fa-f]{6}\Z")
_DECIMAL_PATTERN = re.compile(r"[0-9]+\Z")
_HEX_PATTERN = re.compile(r"[0-9A-Fa-f]+\Z")


@dataclass(frozen=True)
class SourceValidationReport:
    source_key: str
    status: str
    record_count: int
    details: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "details",
            MappingProxyType(dict(self.details)),
        )


class ReferenceSchemaError(ValueError):
    """A verified source payload does not satisfy its frozen schema."""

    def __init__(
        self,
        reason: str,
        source_key: str,
        record_id: str | int | None = None,
    ):
        self.reason = reason
        self.source_key = source_key
        self.record_id = record_id
        super().__init__(f"{reason}: {source_key}")


def validate_reference_source(
    source_key: str, payload: Any
) -> SourceValidationReport:
    """Validate one verified source payload without modifying it."""
    if source_key == "event":
        return _validate_event(payload)
    if source_key == "factor":
        return _validate_factor(payload)
    if source_key == "resource":
        return _validate_resource(payload)
    if source_key == "race":
        return _validate_race(payload)
    if source_key == "skill":
        return _validate_skill(payload)
    if source_key == "training":
        return _validate_training(payload)
    raise ReferenceSchemaError("unknown_source_schema", source_key)


def _validate_event(payload: Any) -> SourceValidationReport:
    source_key = "event"
    _require_exact_top_level(payload, {"meta", "events"}, source_key)
    meta = payload["meta"]
    events = payload["events"]
    if not isinstance(meta, dict) or not isinstance(events, dict):
        _fail("invalid_source_shape", source_key)
    if len(events) != 1440:
        _fail("record_count_mismatch", source_key)
    if meta.get("total_options") != 1440:
        _fail("metadata_count_mismatch", source_key)

    for record_id, record in events.items():
        if not isinstance(record_id, str) or not record_id:
            _fail("invalid_record_id", source_key, record_id)
        if not isinstance(record, dict):
            _fail("invalid_record_shape", source_key, record_id)
        if "color_labels" not in record or "fingerprints" not in record:
            _fail("missing_required_field", source_key, record_id)

        labels = record["color_labels"]
        fingerprints = record["fingerprints"]
        if (
            not isinstance(labels, list)
            or len(labels) != 1
            or not all(isinstance(label, str) for label in labels)
            or labels[0] not in {"red", "blue"}
            or not isinstance(fingerprints, dict)
            or set(labels) != set(fingerprints)
        ):
            _fail("event_label_mismatch", source_key, record_id)

        fingerprint = fingerprints[labels[0]]
        if not isinstance(fingerprint, dict):
            _fail("invalid_record_shape", source_key, record_id)
        required = {"bg_color", "max_turn", "points"}
        if not required.issubset(fingerprint):
            _fail("missing_required_field", source_key, record_id)
        if not _is_color(fingerprint["bg_color"]):
            _fail("invalid_color", source_key, record_id)
        if type(fingerprint["max_turn"]) is not int:
            _fail("invalid_max_turn", source_key, record_id)
        _validate_points(fingerprint["points"], source_key, record_id)

    return _report(source_key, len(events))


def _validate_factor(payload: Any) -> SourceValidationReport:
    source_key = "factor"
    _require_exact_top_level(
        payload, {"meta", "horses", "factor_columns"}, source_key
    )
    if not isinstance(payload["meta"], dict):
        _fail("invalid_source_shape", source_key)
    horses = payload["horses"]
    columns = payload["factor_columns"]
    if not isinstance(horses, dict) or not isinstance(columns, dict):
        _fail("invalid_source_shape", source_key)
    if len(horses) != 202 or len(columns) != 248:
        _fail("record_count_mismatch", source_key)

    for record_id, record in horses.items():
        _validate_point_record(source_key, record_id, record)
    for record_id, record in columns.items():
        _validate_point_record(source_key, record_id, record)

    return _report(
        source_key,
        len(horses),
        {"factor_column_count": len(columns)},
    )


def _validate_resource(payload: Any) -> SourceValidationReport:
    source_key = "resource"
    if not isinstance(payload, dict):
        _fail("invalid_source_shape", source_key)
    if len(payload) != 291:
        _fail("record_count_mismatch", source_key)

    for record_id, record in payload.items():
        _validate_point_record(source_key, record_id, record)

    return _report(source_key, len(payload))


def _validate_race(payload: Any) -> SourceValidationReport:
    source_key = "race"
    _require_exact_top_level(payload, {"meta", "races"}, source_key)
    meta = payload["meta"]
    races = payload["races"]
    if not isinstance(meta, dict) or not isinstance(races, list):
        _fail("invalid_source_shape", source_key)
    if len(races) != 401:
        _fail("record_count_mismatch", source_key)
    if meta.get("total") != 401:
        _fail("metadata_count_mismatch", source_key)

    required = (
        "name",
        "grade",
        "season",
        "turf",
        "distance",
        "direction",
        "location",
        "entry_limit",
        "prize",
        "file_ref",
    )
    seen_rows: list[dict[str, Any]] = []
    for record_id, row in enumerate(races):
        if not isinstance(row, dict):
            _fail("invalid_record_shape", source_key, record_id)
        if any(field not in row or not row[field] for field in required):
            _fail("missing_required_field", source_key, record_id)
        grade = row["grade"]
        if (
            not isinstance(grade, str)
            or grade not in {"GI", "GII", "GIII", "OP"}
        ):
            _fail("invalid_race_grade", source_key, record_id)
        distance = row["distance"]
        if (
            not isinstance(distance, str)
            or _DECIMAL_PATTERN.fullmatch(distance) is None
            or not any(character != "0" for character in distance)
        ):
            _fail("invalid_race_distance", source_key, record_id)
        name = row["name"]
        if (
            not isinstance(name, str)
            or "\ufffd" in name
            or any(unicodedata.category(char) == "Cc" for char in name)
        ):
            _fail("invalid_text", source_key, record_id)
        if any(row == previous for previous in seen_rows):
            _fail("duplicate_race_row", source_key, record_id)
        seen_rows.append(row)

    return _report(source_key, len(races))


def _validate_skill(payload: Any) -> SourceValidationReport:
    source_key = "skill"
    _require_exact_top_level(payload, {"meta", "skills"}, source_key)
    meta = payload["meta"]
    skills = payload["skills"]
    if not isinstance(meta, dict) or not isinstance(skills, list):
        _fail("invalid_source_shape", source_key)
    if len(skills) != 581:
        _fail("record_count_mismatch", source_key)
    if meta.get("total") != 581:
        _fail("metadata_count_mismatch", source_key)

    required = {"name", "rare", "db_id", "icon_id", "type"}
    seen_db_ids: set[str] = set()
    for record_id, row in enumerate(skills):
        if not isinstance(row, dict):
            _fail("invalid_record_shape", source_key, record_id)
        if not required.issubset(row):
            _fail("missing_required_field", source_key, record_id)
        if not isinstance(row["name"], str) or not row["name"]:
            _fail("invalid_skill_name", source_key, record_id)
        db_id = row["db_id"]
        if (
            not isinstance(db_id, str)
            or not db_id
            or _DECIMAL_PATTERN.fullmatch(db_id) is None
        ):
            _fail("invalid_skill_db_id", source_key, record_id)
        if db_id in seen_db_ids:
            _fail("duplicate_skill_db_id", source_key, record_id)
        seen_db_ids.add(db_id)
        rarity = row["rare"]
        if (
            not isinstance(rarity, str)
            or rarity not in {"ノーマル", "レア", "固有"}
        ):
            _fail("invalid_skill_rarity", source_key, record_id)
        if type(row["type"]) is not int or not 0 <= row["type"] <= 8:
            _fail("invalid_skill_type", source_key, record_id)

    return _report(source_key, len(skills))


def _validate_training(payload: Any) -> SourceValidationReport:
    source_key = "training"
    top_level = {
        "meta",
        "templates",
        "by_character_and_context",
        "distribution_summary",
        "by_character",
        "distribution",
    }
    _require_exact_top_level(payload, top_level, source_key)
    if not isinstance(payload["meta"], dict):
        _fail("invalid_source_shape", source_key)
    templates = payload["templates"]
    if not isinstance(templates, list):
        _fail("invalid_source_shape", source_key)
    if len(templates) != 613:
        _fail("record_count_mismatch", source_key)

    required = {"char", "hex", "context", "threshold_value"}
    context_counts = {"base": 0, "bonus": 0, "alt": 0}
    seen_templates: set[tuple[str, str, int]] = set()
    for record_id, row in enumerate(templates):
        if not isinstance(row, dict):
            _fail("invalid_record_shape", source_key, record_id)
        if not required.issubset(row):
            _fail("missing_required_field", source_key, record_id)
        character = row["char"]
        if (
            not isinstance(character, str)
            or len(character) != 1
            or character not in "0123456789"
        ):
            _fail("invalid_training_character", source_key, record_id)
        encoded = row["hex"]
        if (
            not isinstance(encoded, str)
            or len(encoded) % 2 != 0
            or _HEX_PATTERN.fullmatch(encoded) is None
        ):
            _fail("invalid_training_hex", source_key, record_id)
        context = row["context"]
        if not isinstance(context, str) or context not in context_counts:
            _fail("invalid_training_context", source_key, record_id)
        threshold = row["threshold_value"]
        if type(threshold) is not int:
            _fail("invalid_training_threshold", source_key, record_id)
        identity = (character, encoded, threshold)
        if identity in seen_templates:
            _fail("duplicate_training_template", source_key, record_id)
        seen_templates.add(identity)
        context_counts[context] += 1

    if context_counts != {"base": 148, "bonus": 216, "alt": 249}:
        _fail("training_context_count_mismatch", source_key)

    return _report(
        source_key,
        len(templates),
        {
            "matcher_eligible": False,
            "metadata_authoritative": False,
            "canonical_decoder": "external_raw_lsb_variable_width",
        },
    )


def _validate_point_record(
    source_key: str, record_id: Any, record: Any
) -> None:
    if not isinstance(record_id, str) or not record_id:
        _fail("invalid_record_id", source_key, record_id)
    if not isinstance(record, dict):
        _fail("invalid_record_shape", source_key, record_id)
    if "points" not in record or "point_count" not in record:
        _fail("missing_required_field", source_key, record_id)
    points = record["points"]
    point_count = record["point_count"]
    if (
        not isinstance(points, list)
        or not points
        or type(point_count) is not int
        or point_count != len(points)
    ):
        _fail("point_count_mismatch", source_key, record_id)
    _validate_points(points, source_key, record_id)


def _validate_points(points: Any, source_key: str, record_id: Any) -> None:
    if not isinstance(points, list) or not points:
        _fail("invalid_points", source_key, record_id)
    for point in points:
        if not isinstance(point, dict) or not {"x", "y", "color"}.issubset(point):
            _fail("invalid_point_shape", source_key, record_id)
        x = point["x"]
        y = point["y"]
        if type(x) is not int or type(y) is not int:
            _fail("invalid_coordinate", source_key, record_id)
        if x < 0 or y < 0:
            _fail("negative_coordinate", source_key, record_id)
        if not _is_color(point["color"]):
            _fail("invalid_color", source_key, record_id)


def _require_exact_top_level(
    payload: Any, expected: set[str], source_key: str
) -> None:
    if not isinstance(payload, dict) or set(payload) != expected:
        _fail("invalid_source_shape", source_key)


def _is_color(value: Any) -> bool:
    return isinstance(value, str) and _COLOR_PATTERN.fullmatch(value) is not None


def _report(
    source_key: str,
    record_count: int,
    details: Mapping[str, Any] | None = None,
) -> SourceValidationReport:
    return SourceValidationReport(
        source_key=source_key,
        status="valid",
        record_count=record_count,
        details=MappingProxyType(dict(details or {})),
    )


def _fail(reason: str, source_key: str, record_id: Any = None) -> None:
    raise ReferenceSchemaError(reason, source_key, record_id)
