"""Shared fail-closed validation for decision-safe training gain columns."""

from __future__ import annotations

import math
import re
from typing import Any


_STAT_ATTRIBUTES = {"speed", "stamina", "power", "guts", "wisdom"}
# Each verified gain row has one plus and at most three digits. Two rows may
# sum above 999; this is a reader-format bound, not a gameplay gain estimate.
_MAX_GAIN_ROW = 999
_GAIN_KEYS = ("speed", "stamina", "power", "guts", "wisdom", "skill_pt")
_FORMAL_EMBEDDED_DECISION_GAIN_FIELDS = (
    "stats_gain",
    "base_gain",
    "bonus_gain",
    "fragment_or_scenario_gain",
    "total_gain",
    "stats_gain_total",
    "gain_breakdown",
    "stats_gain_ocr",
)


def classify_reliable_exact_gain(
    column: dict[str, Any] | None,
    *,
    attribute_id: str | None = None,
) -> dict[str, Any]:
    """Return a reliable canonical gain only when its exact contributors verify."""
    if not isinstance(column, dict):
        return {"status": "abstain", "value": None, "reason": "column_missing"}
    attribute = str(attribute_id or column.get("attribute_id") or "")
    if attribute not in _STAT_ATTRIBUTES and attribute != "skill_pt":
        return {"status": "abstain", "value": None, "reason": "unknown_attribute"}
    base = _positive_int(column.get("base_value"))
    bonus = _positive_int(column.get("bonus_value"))
    layered = base is not None or bonus is not None
    canonical = _positive_gain(column.get("canonical_value"), layered)
    if (
        canonical is None
        or column.get("status") != "accepted"
        or column.get("canonical_status") != "accepted"
    ):
        return {"status": "abstain", "value": None, "reason": "canonical_not_accepted"}

    if base is not None or bonus is not None:
        closed_skill_pt_total = (
            _positive_gain(base + bonus, True)
            if attribute == "skill_pt"
            and base is not None
            and bonus is not None
            else None
        )
        if closed_skill_pt_total is not None:
            canonical = closed_skill_pt_total
        elif (base or 0) + (bonus or 0) != canonical:
            return {"status": "abstain", "value": None, "reason": "layer_not_conserved"}
        contributor_rows = [
            (kind, value)
            for kind, value in (("base", base), ("bonus", bonus))
            if value is not None
        ]
    else:
        contributor_rows = [("single", canonical)]

    rows = column.get("rows")
    if not isinstance(rows, list):
        return {"status": "abstain", "value": None, "reason": "contributors_missing"}
    accepted_rows = _accepted_rows(column)
    accepted_row_details = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("status") == "accepted"
    ]
    unresolved_rows = column.get("unreliable_rows")
    if unresolved_rows is not None:
        if not isinstance(unresolved_rows, list) or any(
            _unresolved_row_may_contain_gain(
                row,
                accepted_rows=accepted_row_details,
            )
            for row in unresolved_rows
        ):
            return {
                "status": "abstain",
                "value": None,
                "reason": "unresolved_row_fragments",
            }
    if accepted_rows != contributor_rows:
        return {"status": "abstain", "value": None, "reason": "row_contributors_mismatch"}

    candidates = column.get("candidates")
    if not isinstance(candidates, list):
        return {"status": "abstain", "value": None, "reason": "contributors_missing"}
    available = [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("status") == "accepted"
    ]
    if not available:
        return {"status": "abstain", "value": None, "reason": "contributors_missing"}
    if len(accepted_rows) != len(available):
        return {"status": "abstain", "value": None, "reason": "accepted_conflict"}
    for (_kind, value), candidate in zip(accepted_rows, available):
        if type(candidate.get("value")) is not int or candidate.get("value") != value:
            return {"status": "abstain", "value": None, "reason": "row_candidate_mismatch"}
        if not _candidate_is_reliable_exact(candidate, value):
            return {"status": "abstain", "value": None, "reason": "contributor_unreliable"}
    return {"status": "reliable_exact", "value": canonical, "reason": "accepted_contributors"}


def _positive_int(value: Any) -> int | None:
    return value if type(value) is int and 0 < value <= _MAX_GAIN_ROW else None


def _positive_gain(value: Any, layered: bool) -> int | None:
    limit = _MAX_GAIN_ROW * (2 if layered else 1)
    return value if type(value) is int and 0 < value <= limit else None


def _accepted_rows(column: dict[str, Any]) -> list[tuple[str, int]]:
    rows = column.get("rows")
    if not isinstance(rows, list):
        return []
    accepted: list[tuple[str, int]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "accepted":
            continue
        value = row.get("value")
        if type(value) is not int:
            return []
        accepted.append((str(row.get("kind") or ""), value))
    return accepted


def _unresolved_row_may_contain_gain(
    row: Any,
    *,
    accepted_rows: list[dict[str, Any]],
) -> bool:
    if not isinstance(row, dict):
        return True
    if type(row.get("raw_value")) is int or type(row.get("value")) is int:
        return True
    tokens = row.get("tokens")
    if not isinstance(tokens, list):
        return True
    if (
        row.get("status") != "unreadable"
        or row.get("reason") != "row_fragment_only"
        or len(tokens) != 1
        or not str(tokens[0]).isdigit()
    ):
        return True
    score = row.get("score")
    if (
        type(score) not in {int, float}
        or not math.isfinite(float(score))
        or not 0.0 <= float(score) < 0.4
    ):
        return True
    fragment_roi = _strict_xyxy(row.get("roi"))
    if fragment_roi is None:
        return True
    fragment_x1, fragment_y1, fragment_x2, fragment_y2 = fragment_roi
    for accepted in accepted_rows:
        if accepted.get("kind") != row.get("kind"):
            continue
        accepted_roi = _strict_xyxy(accepted.get("roi"))
        if accepted_roi is None:
            continue
        accepted_x1, accepted_y1, accepted_x2, accepted_y2 = accepted_roi
        # A bare low-score glyph left of a same-row verified +N is decoration.
        if (
            fragment_x2 < accepted_x1
            and fragment_y1 < accepted_y2
            and accepted_y1 < fragment_y2
        ):
            return False
        # A very low-score glyph separated by at least half a row height from
        # the right edge of an already closed +N token is the trailing sparkle,
        # not another digit.  A near-adjacent glyph remains fail-closed.
        accepted_height = accepted_y2 - accepted_y1
        if (
            float(score) < 0.2
            and fragment_x1 > accepted_x2
            and fragment_x1 - accepted_x2 >= max(12, accepted_height // 2)
            and fragment_y1 < accepted_y2
            and accepted_y1 < fragment_y2
        ):
            return False
    return True


def _strict_xyxy(value: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    if any(type(item) is not int for item in value):
        return None
    x1, y1, x2, y2 = value
    if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _candidate_is_reliable_exact(candidate: dict[str, Any], value: int) -> bool:
    tokens = candidate.get("tokens")
    matches = candidate.get("component_matches")
    if not isinstance(tokens, list) or not isinstance(matches, list) or len(tokens) != len(matches):
        return False
    normalized = [str(token) for token in tokens]
    if not 2 <= len(normalized) <= 4 or normalized[0] != "+" or not all(
        len(token) == 1 and token in "0123456789" for token in normalized[1:]
    ):
        return False
    if int("".join(normalized[1:])) != value:
        return False
    if all(
        isinstance(match, dict)
        and match.get("best_digit") == token
        and match.get("reliable") is True
        for token, match in zip(normalized, matches)
    ):
        return True
    return _candidate_has_verified_highlighted_four_corroboration(
        candidate,
        value=value,
        tokens=normalized,
        matches=matches,
    )


def _candidate_has_verified_highlighted_four_corroboration(
    candidate: dict[str, Any],
    *,
    value: int,
    tokens: list[str],
    matches: list[Any],
) -> bool:
    """Accept the reviewed guts +4 only when both independent channels bind.

    The primary template channel must still produce the exact ordered token;
    the red-component topology supplies the independent evidence that its low
    scores alone cannot provide.  Every evidence field is revalidated here so
    a reason string or partially copied payload cannot bypass the Safety Gate.
    """

    if (
        value != 4
        or tokens != ["+", "4"]
        or candidate.get("column") != "guts"
        or candidate.get("reason") != "highlighted_top_red_topology_corroborated"
        or len(matches) != 2
    ):
        return False
    primary_thresholds = (("+", 0.40, 0.05), ("4", 0.38, 0.02))
    for match, (token, min_score, min_margin) in zip(matches, primary_thresholds):
        if not isinstance(match, dict) or match.get("best_digit") != token:
            return False
        if not _finite_number_at_least(match.get("score"), min_score):
            return False
        if not _finite_number_at_least(match.get("margin"), min_margin):
            return False

    evidence = candidate.get("topology_evidence")
    if not isinstance(evidence, dict):
        return False
    if evidence != {
        "schema_version": "highlighted_gain_topology.v1",
        "source": "red_component_topology",
        "column": "guts",
        "tokens": ["+", "4"],
        "value": 4,
        "inner_offsets": [19],
        "plus_bbox": evidence.get("plus_bbox"),
        "digit_bbox": evidence.get("digit_bbox"),
    }:
        return False

    plus_bbox = _strict_xywh(evidence.get("plus_bbox"))
    digit_bbox = _strict_xywh(evidence.get("digit_bbox"))
    if plus_bbox is None or digit_bbox is None:
        return False
    plus_x, plus_y, plus_w, plus_h = plus_bbox
    digit_x, digit_y, digit_w, digit_h = digit_bbox
    return (
        30 <= plus_w <= 42
        and 28 <= plus_h <= 42
        and 25 <= plus_y <= 52
        and 35 <= digit_w <= 45
        and 50 <= digit_h <= 58
        and 15 <= digit_y <= 38
        and 5 <= digit_x - (plus_x + plus_w) <= 28
    )


def _strict_xywh(value: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    if any(type(item) is not int for item in value):
        return None
    x, y, width, height = value
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        return None
    return x, y, width, height


def _finite_number_at_least(value: Any, minimum: float) -> bool:
    if type(value) not in {int, float}:
        return False
    numeric = float(value)
    return math.isfinite(numeric) and numeric >= minimum


def revalidate_formal_gain_option(option: dict[str, Any]) -> dict[str, Any]:
    """Rebuild decision gains from current top-level OCR evidence only."""
    data = dict(option)
    declared_mappings = {
        field: data.get(field)
        for field in (
            "stats_gain",
            "base_gain",
            "bonus_gain",
            "total_gain",
            "stats_gain_total",
        )
    }
    unknown_declared_sources: dict[str, set[str]] = {}
    for field, values in declared_mappings.items():
        if not isinstance(values, dict):
            continue
        for attribute in values:
            if type(attribute) is str and attribute not in _GAIN_KEYS:
                unknown_declared_sources.setdefault(attribute, set()).add(field)
    stats_gain = _strict_gain_mapping(data.get("stats_gain"))
    base_gain = _strict_gain_mapping(data.get("base_gain"))
    bonus_gain = _strict_gain_mapping(data.get("bonus_gain"))
    total_gain = _strict_gain_mapping(data.get("total_gain"))
    stats_gain_total = _strict_gain_mapping(data.get("stats_gain_total"))
    stats_gain_ocr = data.get("stats_gain_ocr")
    columns, duplicate_attributes, unknown_attributes = _columns_by_attribute(stats_gain_ocr)

    verified_stats: dict[str, int] = {}
    verified_base: dict[str, int] = {}
    verified_bonus: dict[str, int] = {}
    verified_total: dict[str, int] = {}
    breakdown: dict[str, dict[str, Any]] = {}
    provenance: dict[str, dict[str, str]] = {}
    had_unverified_claim = False
    unknown_fields = _normalized_unknown_fields(data.get("unknown_fields"))

    for unknown_attribute, sources in sorted(unknown_declared_sources.items()):
        _record_unknown_attribute(
            unknown_fields,
            provenance,
            unknown_attribute,
            source="top_level_declared_gain:" + ",".join(sorted(sources)),
        )
    for unknown_attribute in unknown_attributes:
        _record_unknown_attribute(
            unknown_fields,
            provenance,
            unknown_attribute,
            source="top_level_stats_gain_ocr",
        )

    for attribute in _GAIN_KEYS:
        column = columns.get(attribute)
        has_claim = any(
            isinstance(values, dict) and attribute in values
            for values in declared_mappings.values()
        )
        invalid_declared_type = any(
            isinstance(values, dict)
            and attribute in values
            and type(values[attribute]) is not int
            for values in declared_mappings.values()
        )
        strict = classify_reliable_exact_gain(column, attribute_id=attribute)
        dynamic_value = _strict_dynamic_ocr_value(
            stats_gain_ocr,
            column,
            attribute=attribute,
            field_reliability=data.get("field_reliability"),
            declared_stats=stats_gain,
            declared_total=total_gain,
            declared_stats_total=stats_gain_total,
        )
        if dynamic_value is not None:
            strict = {"status": "reliable_exact", "value": dynamic_value}
            reason = ("revalidated_lightweight_ocr" if stats_gain_ocr.get("source") == "own_template_v4"
                      else "revalidated_dynamic_ocr")
        else:
            if (
                isinstance(stats_gain_ocr, dict)
                and stats_gain_ocr.get("source") in {"ppocrv5_dynamic", "own_template_v4"}
                and isinstance(column, dict)
            ):
                strict = {
                    "status": "abstain",
                    "value": None,
                    "reason": "dynamic_rows_not_verified",
                }
            reason = _formal_gain_reason(
                strict=strict,
                column=column,
                attribute=attribute,
                stats_gain=stats_gain,
                base_gain=base_gain,
                bonus_gain=bonus_gain,
                total_gain=total_gain,
                stats_gain_total=stats_gain_total,
                has_top_level_ocr=isinstance(stats_gain_ocr, dict),
                invalid_declared_type=invalid_declared_type,
                duplicate_attribute=attribute in duplicate_attributes,
            )
            if (
                _is_dynamic_ocr_column(stats_gain_ocr, column)
                and _dynamic_ocr_reliability(data.get("field_reliability"), attribute)
                != "reliable"
                and not invalid_declared_type
                and attribute not in duplicate_attributes
            ):
                reason = "dynamic_ocr_reliability_not_verified"
        reliable = reason == "revalidated_top_level_ocr"
        if reason in {"revalidated_dynamic_ocr", "revalidated_lightweight_ocr"}:
            reliable = True
        if reliable:
            value = strict["value"]
            verified_stats[attribute] = value
            verified_total[attribute] = value
            base_value = base_gain.get(attribute, 0)
            bonus_value = bonus_gain.get(attribute, 0)
            if base_value:
                verified_base[attribute] = base_value
            if bonus_value:
                verified_bonus[attribute] = bonus_value
        elif has_claim:
            had_unverified_claim = True
        if reason == "invalid_declared_type":
            invalid_type_field = f"stats_gain_invalid_type:{attribute}"
            if invalid_type_field not in unknown_fields:
                unknown_fields.append(invalid_type_field)
        if reason == "duplicate_attribute":
            duplicate_field = f"stats_gain_duplicate_attribute:{attribute}"
            if duplicate_field not in unknown_fields:
                unknown_fields.append(duplicate_field)

        secondary_scores = [row['confidence'] for row in (column.get('rows', []) if isinstance(column, dict) else [])
            if isinstance(row, dict) and row.get('recognition_route') == 'secondary'
            and type(row.get('confidence')) in (int, float) and math.isfinite(row['confidence'])]
        breakdown[attribute] = {
            "base_gain": verified_base.get(attribute, 0),
            "fragment_or_scenario_gain": verified_bonus.get(attribute, 0),
            "total_gain": verified_total.get(attribute, 0),
            "confidence": min(secondary_scores) if reliable and secondary_scores else 0.9 if reliable else 0.0,
            "partial": not reliable,
            "reliable": reliable,
            "status": "accepted" if reliable else "abstain",
        }
        if reliable and secondary_scores:
            breakdown[attribute]['confidence_basis'] = 'secondary_recognition_confidence'
        if has_claim or column is not None or attribute in duplicate_attributes:
            provenance[attribute] = {
                "source": "top_level_stats_gain_ocr",
                "reason": reason,
            }

    if had_unverified_claim and "stats_gain_unverified_provenance" not in unknown_fields:
        unknown_fields.append("stats_gain_unverified_provenance")
    raw_data = dict(data.get("raw_data", {})) if isinstance(data.get("raw_data"), dict) else {}
    embedded_gain_fields = {
        field: raw_data.pop(field)
        for field in _FORMAL_EMBEDDED_DECISION_GAIN_FIELDS
        if field in raw_data
    }
    if embedded_gain_fields:
        audit = (
            dict(raw_data.get("formal_sidecar_audit", {}))
            if isinstance(raw_data.get("formal_sidecar_audit"), dict)
            else {}
        )
        audit["untrusted_embedded_gain_fields"] = embedded_gain_fields
        raw_data["formal_sidecar_audit"] = audit
    raw_data["formal_gain_provenance"] = provenance

    data["stats_gain"] = verified_stats
    data["base_gain"] = verified_base
    data["bonus_gain"] = verified_bonus
    data["fragment_or_scenario_gain"] = verified_bonus
    data["total_gain"] = verified_total
    data["stats_gain_total"] = verified_total
    data["gain_breakdown"] = breakdown
    data["unknown_fields"] = _normalized_unknown_fields(unknown_fields)
    data["raw_data"] = raw_data
    return data


def _is_dynamic_ocr_column(
    stats_gain_ocr: Any,
    column: dict[str, Any] | None,
) -> bool:
    return bool(
        isinstance(stats_gain_ocr, dict)
        and (stats_gain_ocr.get("source"), stats_gain_ocr.get("reader")) in (
            ("ppocrv5_dynamic", "ppocrv5_mobile"),
            ("own_template_v4", "own_template_v4"),
        )
        and isinstance(column, dict)
    )


def validate_dynamic_gain_column(
    stats_gain_ocr: Any,
    column: Any,
    *,
    attribute: str,
    value: Any,
) -> bool:
    """Replay the two-slot contract for explicitly supported local gain readers."""
    if not _is_dynamic_ocr_column(stats_gain_ocr, column):
        return False
    if attribute not in _GAIN_KEYS:
        return False
    if type(value) is not int or value <= 0:
        return False
    if (
        column.get("attribute_id") != attribute
        or column.get("status") != "accepted"
        or column.get("canonical_status") != "accepted"
        or type(column.get("canonical_value")) is not int
        or column.get("canonical_value") != value
        or column.get("reason") != "gain_rows_verified"
    ):
        return False
    rows = column.get("rows")
    if not isinstance(rows, list) or len(rows) != 2:
        return False
    accepted: list[int] = []
    for expected_index, row in enumerate(rows):
        if not isinstance(row, dict) or type(row.get("row_index")) is not int:
            return False
        if row["row_index"] != expected_index:
            return False
        occupancy = row.get("occupancy")
        status = row.get("status")
        row_value = row.get("value")
        if occupancy == "absent":
            if status != "absent" or row_value is not None or row.get("raw_text") not in ("", None):
                return False
            continue
        if occupancy != "present" or status != "accepted" or type(row_value) is not int:
            return False
        if row_value < 0 or row_value > _MAX_GAIN_ROW:
            return False
        if row.get('recognition_route') == 'secondary' or row.get('engine') == 'local_rapidocr_numeric':
            if not _valid_secondary_gain_row(row):
                return False
        raw_text = row.get("raw_text")
        if not isinstance(raw_text, str) or not re.fullmatch(r"\+[0-9]{1,3}", raw_text.strip()):
            return False
        if int(raw_text.strip()[1:]) != row_value:
            return False
        accepted.append(row_value)
    return bool(accepted) and sum(accepted) == value


def _valid_secondary_gain_row(row: dict[str, Any]) -> bool:
    primary, secondary = row.get('primary'), row.get('secondary')
    score, minimum = row.get('confidence'), row.get('min_score')
    return bool(isinstance(primary, dict) and primary.get('status') != 'accepted'
        and isinstance(secondary, dict) and secondary.get('status') == 'accepted'
        and row.get('recognition_route') == 'secondary' and row.get('gain_layout_confirmed') is True
        and row.get('engine') == secondary.get('engine') == 'local_rapidocr_numeric'
        and type(secondary.get('value')) is int and secondary['value'] == row['value']
        and type(score) in (int, float) and math.isfinite(score)
        and type(minimum) in (int, float) and math.isfinite(minimum) and 0 <= minimum <= score <= 1
        and secondary.get('confidence') == score and secondary.get('min_score') == minimum)


def _dynamic_ocr_reliability(field_reliability: Any, attribute: str) -> Any:
    if not isinstance(field_reliability, dict):
        return None
    stats_quality = field_reliability.get("stats_gain")
    if not isinstance(stats_quality, dict):
        return None
    return stats_quality.get(attribute)


def _strict_dynamic_ocr_value(
    stats_gain_ocr: Any,
    column: dict[str, Any] | None,
    *,
    attribute: str,
    field_reliability: Any,
    declared_stats: dict[str, int],
    declared_total: dict[str, int],
    declared_stats_total: dict[str, int],
) -> int | None:
    if not _is_dynamic_ocr_column(stats_gain_ocr, column):
        return None
    if _dynamic_ocr_reliability(field_reliability, attribute) != "reliable":
        return None
    if not validate_dynamic_gain_column(
        stats_gain_ocr,
        column,
        attribute=attribute,
        value=column.get("canonical_value") if isinstance(column, dict) else None,
    ):
        return None
    assert isinstance(column, dict)
    value = int(column["canonical_value"])
    for declared in (declared_stats, declared_total, declared_stats_total):
        if attribute in declared and declared[attribute] != value:
            return None
    return value


def _strict_gain_mapping(values: Any) -> dict[str, int]:
    if not isinstance(values, dict):
        return {}
    return {
        key: value
        for key, value in values.items()
        if key in _GAIN_KEYS and type(value) is int and value > 0
    }


def _normalized_unknown_fields(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return sorted({value for value in values if type(value) is str and value})


def _record_unknown_attribute(
    unknown_fields: list[str],
    provenance: dict[str, dict[str, str]],
    attribute: str,
    *,
    source: str,
) -> None:
    unknown_field = f"stats_gain_unknown_attribute:{attribute}"
    if unknown_field not in unknown_fields:
        unknown_fields.append(unknown_field)
    existing = provenance.get(attribute)
    if existing and existing.get("reason") == "unknown_attribute":
        sources = set(str(existing.get("source") or "").split("|"))
        sources.add(source)
        provenance[attribute] = {
            "source": "|".join(sorted(item for item in sources if item)),
            "reason": "unknown_attribute",
        }
        return
    provenance[attribute] = {
        "source": source,
        "reason": "unknown_attribute",
    }


def _columns_by_attribute(
    stats_gain_ocr: Any,
) -> tuple[dict[str, dict[str, Any]], set[str], list[str]]:
    if not isinstance(stats_gain_ocr, dict) or not isinstance(stats_gain_ocr.get("columns"), list):
        return {}, set(), []
    columns: dict[str, dict[str, Any]] = {}
    duplicate_attributes: set[str] = set()
    unknown_attributes: list[str] = []
    seen_attributes: set[str] = set()
    for column in stats_gain_ocr["columns"]:
        if not isinstance(column, dict):
            continue
        attribute_id = column.get("attribute_id")
        if type(attribute_id) is not str or not attribute_id:
            unknown_attribute = "<invalid_attribute_id>"
            if unknown_attribute not in unknown_attributes:
                unknown_attributes.append(unknown_attribute)
            continue
        if attribute_id not in _GAIN_KEYS:
            if attribute_id not in unknown_attributes:
                unknown_attributes.append(attribute_id)
            continue
        if attribute_id in seen_attributes:
            duplicate_attributes.add(attribute_id)
            columns.pop(attribute_id, None)
            continue
        seen_attributes.add(attribute_id)
        columns[attribute_id] = column
    return columns, duplicate_attributes, unknown_attributes


def _formal_gain_reason(
    *,
    strict: dict[str, Any],
    column: dict[str, Any] | None,
    attribute: str,
    stats_gain: dict[str, int],
    base_gain: dict[str, int],
    bonus_gain: dict[str, int],
    total_gain: dict[str, int],
    stats_gain_total: dict[str, int],
    has_top_level_ocr: bool,
    invalid_declared_type: bool,
    duplicate_attribute: bool,
) -> str:
    if duplicate_attribute:
        return "duplicate_attribute"
    if invalid_declared_type:
        return "invalid_declared_type"
    if not has_top_level_ocr:
        return "top_level_ocr_missing"
    if strict.get("status") != "reliable_exact":
        return f"strict_classifier:{strict.get('reason') or 'abstain'}"
    value = strict.get("value")
    if type(value) is not int:
        return "strict_classifier:invalid_value"
    if not isinstance(column, dict):
        return "top_level_ocr_missing"
    column_base = column.get("base_value")
    column_bonus = column.get("bonus_value")
    if (
        attribute == "skill_pt"
        and _positive_int(column_base) is not None
        and _positive_int(column_bonus) is not None
    ):
        if (
            base_gain.get(attribute) != column_base
            or bonus_gain.get(attribute) != column_bonus
        ):
            return "layered_field_mismatch"
        return "revalidated_top_level_ocr"
    if stats_gain.get(attribute) != value:
        return "stats_gain_mismatch"
    if total_gain.get(attribute) != value or stats_gain_total.get(attribute) != value:
        return "total_gain_mismatch"
    has_layers = type(column_base) is int or type(column_bonus) is int
    if has_layers:
        if (
            base_gain.get(attribute, 0) != (column_base or 0)
            or bonus_gain.get(attribute, 0) != (column_bonus or 0)
        ):
            return "layered_field_mismatch"
    elif base_gain.get(attribute, 0) or bonus_gain.get(attribute, 0):
        return "unlayered_field_mismatch"
    return "revalidated_top_level_ocr"
