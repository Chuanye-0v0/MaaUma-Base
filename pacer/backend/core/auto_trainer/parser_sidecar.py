from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.auto_trainer.capture_adapter import load_capture_group_raws
from core.auto_trainer.state import TRAINING_TYPES
from core.training_gain_reliability import (
    classify_reliable_exact_gain,
    validate_dynamic_gain_column,
)


SIDECAR_SCHEMA_VERSION = "ug_parent_training_options_sidecar.v1"
SUPPORT_RIGHT_LANE_SHADOW_SCHEMA_VERSION = "support_right_lane_shadow_sidecar.v1"
LIVE_SUPPORT_RIGHT_LANE_SHADOW_ENABLED = False
STAT_KEYS = ("speed", "stamina", "power", "guts", "wisdom", "skill_pt")
_EXPECTED_TRAINING_GAIN_KEYS = {
    "speed": {"speed", "power", "skill_pt"},
    "stamina": {"stamina", "guts", "skill_pt"},
    "power": {"stamina", "power", "skill_pt"},
    "guts": {"speed", "power", "guts", "skill_pt"},
    "wisdom": {"speed", "wisdom", "skill_pt"},
}
_FRAGMENT_PRODUCTION_ALIASES = {
    "no_new_fragments": "no_new_fragment",
}


def _canonicalize_fragment_production(
    scenario_mechanics: dict[str, Any],
) -> dict[str, Any]:
    grand_masters = scenario_mechanics.get("grand_masters")
    if not isinstance(grand_masters, dict):
        return scenario_mechanics
    production = grand_masters.get("production")
    if isinstance(production, str):
        grand_masters["production"] = _FRAGMENT_PRODUCTION_ALIASES.get(
            production,
            production,
        )
    return scenario_mechanics


def build_training_options_sidecar(
    *,
    manifest_path: str | Path,
    execution_root: str | Path,
    actual_group_id: str,
    output_path: str | Path,
    parser: Any | None = None,
    support_right_lane_recognizer: Any | None = None,
    state_context: dict[str, Any] | None = None,
    formal_capture: bool = True,
    overwrite: bool = False,
    pretty: bool = False,
) -> dict[str, Any]:
    output_path = Path(output_path)
    if output_path.exists() and not overwrite:
        return {
            "is_valid": False,
            "errors": [f"output_exists:{output_path}"],
            "warnings": [],
            "output_path": str(output_path),
        }

    raw_by_tab = load_capture_group_raws(
        manifest_path=manifest_path,
        execution_root=execution_root,
        actual_group_id=actual_group_id,
        formal_capture=formal_capture,
    )
    _validate_formal_sidecar_context(
        state_context=state_context,
        group_turn=raw_by_tab[TRAINING_TYPES[0]]["turn_index"],
        formal_capture=formal_capture,
    )
    parser = parser or _default_parser()
    scenario_state = state_context.get("scenario_state") if isinstance(state_context, dict) else None
    scenario = scenario_state.get("scenario") if isinstance(scenario_state, dict) else None
    shadow_enabled = (
        LIVE_SUPPORT_RIGHT_LANE_SHADOW_ENABLED
        or os.environ.get("UMA_SUPPORT_RIGHT_LANE_SHADOW") == "1"
    )
    if scenario != "larc" and support_right_lane_recognizer is None and shadow_enabled:
        support_right_lane_recognizer = _default_support_right_lane_recognizer()

    training_options: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    for tab in TRAINING_TYPES:
        raw = raw_by_tab[tab]
        precheck_consensus: dict[str, Any] | None = None
        try:
            bind_larc = getattr(parser, "bind_larc_capture", None)
            if scenario == "larc" and callable(bind_larc):
                bind_larc(raw)
            parsed = _parse_for_scenario(parser, raw["absolute_path"], scenario)
            option = _normalize_parsed_option(parsed, fallback_tab=tab, scenario=scenario)
            _apply_scenario_context_to_option(option, state_context)
            precheck_consensus = _apply_stable_precheck_gain_consensus(
                option=option,
                parser=parser,
                execution_root=Path(execution_root),
                tab=tab,
                scenario=scenario,
            )
            if option["training_type"] == "unknown":
                option["training_type"] = tab
                option["unknown_fields"] = [
                    field for field in option["unknown_fields"] if field != "training_type"
                ]
            elif option["training_type"] != tab:
                warnings.append(
                    f"parser_training_type_mismatch:{tab}:{option['training_type']}"
                )
                option["training_type"] = tab
        except Exception as exc:
            errors.append(f"parse_failed:{tab}:{type(exc).__name__}:{exc}")
            option = {
                "training_type": tab,
                "stats_gain": {},
                "base_gain": {},
                "fragment_or_scenario_gain": {},
                "total_gain": {},
                "gain_breakdown": _empty_gain_breakdown(),
                "confidence": 0.0,
                "unknown_fields": ["parse_failed"],
            }
        raw_data = dict(option.get("raw_data", {})) if isinstance(option.get("raw_data"), dict) else {}
        raw_data["capture"] = raw
        if precheck_consensus is not None:
            raw_data["stable_precheck_gain_consensus"] = precheck_consensus
        option["raw_data"] = raw_data
        option["support_right_lane_shadow"] = _abstained_support_right_lane_shadow(
            reason="larc_uses_unified_participants", source_sha256=raw.get("sha256"),
        ) if scenario == "larc" else _build_support_right_lane_shadow(
            image_path=Path(raw["absolute_path"]),
            source_sha256=raw.get("sha256"),
            option=option,
            recognizer=support_right_lane_recognizer,
        )
        training_options.append(option)

    if scenario == "grand_masters":
        _aggregate_grand_masters_fragment_layers(training_options)

    sidecar = {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "actual_group_id": actual_group_id,
        "source_manifest": str(Path(manifest_path)),
        "execution_root": str(Path(execution_root)),
        "state_context": _clean_json_object(state_context or {}),
        "training_options": training_options,
        "warnings": warnings,
        "errors": errors,
        "is_valid": not errors,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2 if pretty else None),
        encoding="utf-8",
    )
    return {
        "is_valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "output_path": str(output_path),
        "training_option_count": len(training_options),
    }


def _validate_formal_sidecar_context(
    *,
    state_context: dict[str, Any] | None,
    group_turn: Any,
    formal_capture: bool,
) -> None:
    if not formal_capture:
        return
    if not isinstance(state_context, dict) or type(state_context.get("turn")) is not int:
        raise ValueError("capture_context_turn_invalid")
    if state_context["turn"] != group_turn:
        raise ValueError("capture_context_turn_mismatch")


def _aggregate_grand_masters_fragment_layers(options: list[dict[str, Any]]) -> None:
    """Attach one turn-global inventory and one per-tab prospective output.

    A slot enters inventory only when every captured tab independently calls it
    filled.  This prevents the selected card's changing fourth tile from being
    promoted into the shared table.
    """
    if len(options) != len(TRAINING_TYPES) or {option.get("training_type") for option in options} != set(TRAINING_TYPES):
        for option in options:
            gm = option.get("scenario_mechanics", {}).get("grand_masters")
            if isinstance(gm, dict):
                gm["fragment_inventory"] = {"capacity": 8, "filled_slots": None, "slots": [], "status": "abstain", "reason": "incomplete_or_duplicate_five_tab_group"}
        return
    mechanics = [
        option.get("scenario_mechanics", {}).get("grand_masters")
        for option in options
        if isinstance(option.get("scenario_mechanics"), dict)
    ]
    if not mechanics or not all(isinstance(item, dict) for item in mechanics):
        return
    slot_lists = [item.get("inventory_state", {}).get("evidence", {}).get("slots", []) for item in mechanics]
    stable_slots: list[dict[str, Any]] = []
    incomplete = False
    for index in range(8):
        observed = [slots[index] for slots in slot_lists if isinstance(slots, list) and len(slots) > index]
        if not observed:
            incomplete = True
            continue
        if len(observed) != len(options):
            incomplete = True
            continue
        stable_state = _stable_fragment_slot_state(observed)
        if stable_state is None:
            incomplete = True
            continue
        if stable_state == "filled":
            stable_slots.append(
                {
                    "slot": index,
                    "color": "unknown",
                    "kind": "unknown",
                    "status": "stable_slot_class",
                    "evidence": {"observations": len(observed)},
                }
            )
    effect_values = [
        True if item.get("wisdom_effect_active") is True or item.get("state") == "effect_active"
        else False if item.get("wisdom_effect_active") is False
        else None
        for item in mechanics
    ]
    effect_active = True if all(value is True for value in effect_values) else False if all(value is False for value in effect_values) else None
    inventory = {"capacity": 8, "filled_slots": None if incomplete else len(stable_slots), "slots": stable_slots, "status": "partial" if incomplete else "accepted_cross_tab_stable"}
    if effect_active is True and inventory["filled_slots"] != inventory["capacity"]:
        inventory = {
            **inventory,
            "filled_slots": None,
            "status": "partial",
            "reason": "effect_active_inventory_not_complete",
        }
    for option, gm in zip(options, mechanics):
        gm["fragment_inventory"] = dict(inventory)
        gm["wisdom_effect_active"] = effect_active
        output = gm.get("prospective_fragment_output")
        if inventory["filled_slots"] == 8:
            gm["prospective_fragment_output"] = {"status": "abstain", "reason": "inventory_full_no_observed_new_output"}
        elif isinstance(output, dict):
            color, count, kind = output.get("color"), output.get("count"), output.get("kind")
            if color and isinstance(count, int) and not kind:
                gm["prospective_fragment_output"] = {**output, "kind": "unknown", "status": "partial"}
            elif output.get("status") in {"accepted", "partial", "abstain"}:
                gm["prospective_fragment_output"] = dict(output)
            else:
                gm["prospective_fragment_output"] = {"status": "abstain", "reason": "invalid_prospective_structure"}
        else:
            gm["prospective_fragment_output"] = {"status": "abstain", "reason": "no_nonlegacy_prospective_output"}


def _stable_fragment_slot_state(observed: list[dict[str, Any]]) -> str | None:
    """Return a conservative cross-tab occupancy class for one fixed slot.

    Template filenames are candidate evidence only; different source templates
    may represent the same occupancy.  An abstained page participates only
    when its candidate class and quality bounds agree across all five pages.
    """
    states: list[str] = []
    for slot in observed:
        declared = slot.get("state")
        if declared in {"filled", "empty"} and not slot.get("abstained"):
            states.append(declared)
            continue
        evidence = slot.get("evidence")
        if not isinstance(evidence, dict):
            return None
        template = str(evidence.get("template") or "")
        best_score = evidence.get("best_score")
        coverage = evidence.get("coverage")
        if not isinstance(best_score, (int, float)) or not isinstance(coverage, (int, float)):
            return None
        if best_score > 0.16 or coverage < 0.8:
            return None
        if "fragment_slot_filled_" in template:
            states.append("filled")
        elif "fragment_slot_empty_" in template:
            # Filled artwork can be closer to an empty template when its icon
            # differs from the available fixtures.  The lower coverage is a
            # second, pixel-derived occupancy signal and must agree on all tabs.
            states.append("filled" if coverage < 0.895 else "empty")
        else:
            return None
    return states[0] if states and len(set(states)) == 1 else None


def _default_parser() -> Any:
    from core.training_parser import TrainingParser

    return TrainingParser(use_mimo=False, recognition_mode="lightweight")


def _parse_for_scenario(parser: Any, image_path: str | Path, scenario: str | None) -> Any:
    parse = getattr(parser, "parse_for_scenario", None)
    if callable(parse):
        return parse(image_path, scenario=scenario)
    return parser.parse(image_path)


def _default_support_right_lane_recognizer() -> Any:
    from core.support_right_lane_recognizer import SupportRightLaneRecognizer

    return SupportRightLaneRecognizer()


def _build_support_right_lane_shadow(
    *,
    image_path: Path,
    source_sha256: Any,
    option: dict[str, Any],
    recognizer: Any,
) -> dict[str, Any]:
    normalized_sha = (
        source_sha256.lower()
        if isinstance(source_sha256, str)
        and len(source_sha256) == 64
        and all(character in "0123456789abcdefABCDEF" for character in source_sha256)
        else None
    )
    if recognizer is None:
        if (
            not LIVE_SUPPORT_RIGHT_LANE_SHADOW_ENABLED
            and os.environ.get("UMA_SUPPORT_RIGHT_LANE_SHADOW") != "1"
        ):
            return _abstained_support_right_lane_shadow(
                reason="shadow_disabled_by_default",
                source_sha256=normalized_sha,
            )
        recognizer = _default_support_right_lane_recognizer()
    try:
        result = recognizer.recognize(image_path)
    except Exception as exc:
        return _abstained_support_right_lane_shadow(
            reason=f"recognizer_exception:{type(exc).__name__}",
            source_sha256=normalized_sha,
        )
    if not isinstance(result, dict):
        return _abstained_support_right_lane_shadow(
            reason="recognizer_result_not_object",
            source_sha256=normalized_sha,
        )
    recognizer_schema = result.get("schema_version")
    if result.get("status") != "accepted":
        reason = result.get("reason")
        return _abstained_support_right_lane_shadow(
            reason=reason if isinstance(reason, str) and reason else "recognizer_abstained",
            source_sha256=normalized_sha,
            recognizer_schema_version=recognizer_schema,
        )
    heads = result.get("visible_portrait_count")
    hints = result.get("verified_red_exclamation_count")
    counts_valid = (
        type(heads) is int
        and 0 <= heads <= 6
        and type(hints) is int
        and 0 <= hints <= heads
    )
    if (
        recognizer_schema != "support_right_lane_count_shadow.v1"
        or result.get("production_ready") is not False
        or not counts_valid
        or normalized_sha is None
    ):
        return _abstained_support_right_lane_shadow(
            reason="recognizer_result_invalid",
            source_sha256=normalized_sha,
            recognizer_schema_version=recognizer_schema,
        )
    primary_support = option.get("support_count")
    primary_hints = option.get("red_dot_count")
    return {
        "schema_version": SUPPORT_RIGHT_LANE_SHADOW_SCHEMA_VERSION,
        "recognizer_schema_version": recognizer_schema,
        "status": "accepted",
        "reason": str(result.get("reason") or "fixed_right_lane_evaluated"),
        "source_sha256": normalized_sha,
        "visible_portrait_count": heads,
        "verified_red_exclamation_count": hints,
        "primary_support_count_match": (
            heads == primary_support if type(primary_support) is int else None
        ),
        "primary_red_dot_count_match": (
            hints == primary_hints if type(primary_hints) is int else None
        ),
        "production_ready": False,
    }


def _abstained_support_right_lane_shadow(
    *,
    reason: str,
    source_sha256: str | None,
    recognizer_schema_version: Any = None,
) -> dict[str, Any]:
    return {
        "schema_version": SUPPORT_RIGHT_LANE_SHADOW_SCHEMA_VERSION,
        "recognizer_schema_version": (
            recognizer_schema_version
            if isinstance(recognizer_schema_version, str)
            else None
        ),
        "status": "abstained",
        "reason": reason,
        "source_sha256": source_sha256,
        "visible_portrait_count": None,
        "verified_red_exclamation_count": None,
        "primary_support_count_match": None,
        "primary_red_dot_count_match": None,
        "production_ready": False,
    }


def _apply_stable_precheck_gain_consensus(
    *,
    option: dict[str, Any],
    parser: Any,
    execution_root: Path,
    tab: str,
    scenario: str | None = "grand_masters",
) -> dict[str, Any] | None:
    unknown_fields = option.get("unknown_fields")
    stats_gain_unreliable = isinstance(unknown_fields, list) and any(
        field in unknown_fields
        for field in (
            "local_template_incomplete",
            "local_ocr_value_unreliable",
            "stats_gain_unreliable",
            "stats_gain_partial",
            "stats_gain",
        )
    )
    if float(option.get("confidence", 0.0) or 0.0) >= 0.65 and not stats_gain_unreliable:
        return None

    expected_keys = _EXPECTED_TRAINING_GAIN_KEYS.get(tab)
    if expected_keys is None:
        return None

    # Precheck frames are only a recovery path.  Once the selected live frame
    # already has a strict, decision-safe primary gain, weaker prechecks must
    # not erase it or lower the option confidence.
    primary_column = _stats_gain_columns_by_attr(option.get("stats_gain_ocr")).get(tab)
    primary_value = _clean_stats_mapping(option.get("stats_gain")).get(tab)
    if _dynamic_gain_is_reliable(option.get("stats_gain_ocr"), primary_column, tab, primary_value):
        return None
    primary_strict = classify_reliable_exact_gain(primary_column, attribute_id=tab)
    if (
        primary_strict.get("status") == "reliable_exact"
        and _clean_stats_mapping(option.get("stats_gain")).get(tab)
        == primary_strict.get("value")
    ):
        return None

    candidate_paths = sorted(
        (execution_root / "raw_720").glob(f"{tab}.attempt-*.precheck_*.png")
    )
    if len(candidate_paths) < 2:
        return None

    candidates: list[tuple[Path, dict[str, Any], dict[str, int]]] = []
    for path in candidate_paths:
        try:
            candidate = _normalize_parsed_option(
                _parse_for_scenario(parser, path, scenario),
                fallback_tab=tab,
                scenario=scenario,
            )
        except Exception:
            return None
        candidates.append(
            (path, candidate, _accepted_precheck_gain_fields(candidate, expected_keys))
        )

    # A field that is conservative-partial in one frame may still be safe to
    # promote when every precheck independently reports the same internally
    # conserved value.  Do not use this fallback for a missing primary field.
    candidates = [
        (path, candidate, _with_stable_conserved_fields(candidate, accepted, expected_keys))
        for path, candidate, accepted in candidates
    ]

    field_votes: dict[str, list[tuple[Path, dict[str, Any], int]]] = {
        key: [] for key in expected_keys
    }
    for path, candidate, accepted_fields in candidates:
        for key, value in accepted_fields.items():
            field_votes[key].append((path, candidate, value))

    consensus_fields: dict[str, tuple[Path, dict[str, Any], int]] = {}
    conflicting_fields: dict[str, list[int]] = {}
    for key, votes in field_votes.items():
        values = {value for _, _, value in votes}
        if len(values) > 1:
            conflicting_fields[key] = sorted(values)
            continue
        missing_candidates = [
            candidate
            for _, candidate, accepted_fields in candidates
            if key not in accepted_fields
        ]
        vote_states = [
            _precheck_field_vote_state(candidate, key)
            for candidate in missing_candidates
        ]
        rejected_values = {
            value
            for candidate, state in zip(missing_candidates, vote_states)
            if state == "explicitly_rejected"
            if (value := _precheck_field_observed_value(candidate, key)) is not None
        }
        unresolved_values = {
            value
            for candidate, state in zip(missing_candidates, vote_states)
            if state == "missing"
            if (value := _precheck_field_observed_value(candidate, key)) is not None
        }

        # Only accepted OCR values vote.  A known rejected multi-digit token
        # is not a missing frame; it carries no vote.  Conversely, an
        # unresolved token with a numeric value is still ambiguous and must
        # block a primary-field promotion.  Non-primary fields also stay out
        # when rejected evidence itself disagrees across frames.
        if unresolved_values:
            conflicting_fields[key] = sorted(values | unresolved_values)
            continue
        if key != tab and len(rejected_values) > 1:
            conflicting_fields[key] = sorted(values | rejected_values)
            continue
        if votes:
            consensus_fields[key] = max(
                votes,
                key=lambda item: (
                    float(item[1].get("confidence", 0.0) or 0.0),
                    str(item[0]),
                ),
            )

    if tab in conflicting_fields:
        _demote_ambiguous_precheck_primary(option, tab)
        return None
    if tab not in consensus_fields:
        _demote_ambiguous_precheck_primary(option, tab)
        return None

    selected_path, selected, _ = max(
        candidates,
        key=lambda item: (float(item[1].get("confidence", 0.0) or 0.0), str(item[0])),
    )
    consensus_total = {
        key: value for key, (_path, _candidate, value) in consensus_fields.items()
    }
    option["stats_gain"] = dict(consensus_total)
    option["base_gain"] = {
        key: value
        for key, (_path, candidate, _value) in consensus_fields.items()
        if (value := _clean_stats_mapping(candidate.get("base_gain")).get(key, 0)) > 0
    }
    option["bonus_gain"] = {
        key: value
        for key, (_path, candidate, _value) in consensus_fields.items()
        if (value := _clean_stats_mapping(candidate.get("bonus_gain")).get(key, 0)) > 0
    }
    option["fragment_or_scenario_gain"] = dict(option["bonus_gain"])
    option["stats_gain_total"] = dict(consensus_total)
    option["total_gain"] = dict(consensus_total)
    option["stats_gain_ocr"] = selected.get("stats_gain_ocr")
    option["confidence"] = max(float(option.get("confidence", 0.0) or 0.0), 0.7)
    if isinstance(unknown_fields, list):
        for field in (
            "local_template_incomplete",
            "local_ocr_value_unreliable",
            "stats_gain_unreliable",
            "stats_gain_partial",
            "stats_gain",
        ):
            _remove_unknown(unknown_fields, field)
    option["gain_breakdown"] = _build_gain_breakdown(
        stats_gain=option["stats_gain"],
        base_gain=option["base_gain"],
        fragment_or_scenario_gain=option["bonus_gain"],
        total_gain=option["total_gain"],
        stats_gain_ocr=option["stats_gain_ocr"],
        unknown_fields=unknown_fields if isinstance(unknown_fields, list) else [],
    )
    return {
        "candidate_count": len(candidates),
        "candidate_paths": [str(path.relative_to(execution_root)) for path, _, _ in candidates],
        "selected_path": str(selected_path.relative_to(execution_root)),
        "stats_gain": consensus_total,
        "consensus_fields": {
            key: {
                "accepted_count": len(field_votes[key]),
                "source_path": str(path.relative_to(execution_root)),
                "value": value,
            }
            for key, (path, _candidate, value) in consensus_fields.items()
        },
        "conflicting_fields": conflicting_fields,
    }


def _with_stable_conserved_fields(
    option: dict[str, Any],
    accepted: dict[str, int],
    expected_keys: set[str],
) -> dict[str, int]:
    result = dict(accepted)
    stats_gain = _clean_stats_mapping(option.get("stats_gain"))
    total_gain = _clean_stats_mapping(option.get("total_gain"))
    base_gain = _clean_stats_mapping(option.get("base_gain"))
    bonus_gain = _clean_stats_mapping(option.get("bonus_gain"))
    columns = _stats_gain_columns_by_attr(option.get("stats_gain_ocr"))
    for key in expected_keys:
        value = stats_gain.get(key)
        if value is None or total_gain.get(key) != value:
            continue
        if base_gain.get(key, 0) + bonus_gain.get(key, 0) != value:
            continue
        strict = classify_reliable_exact_gain(columns.get(key), attribute_id=key)
        if strict.get("status") != "reliable_exact" or strict.get("value") != value:
            continue
        result.setdefault(key, value)
    return result


def _canonical_conserved_field_is_reliable(
    column: dict[str, Any] | None,
    value: int,
) -> bool:
    strict = classify_reliable_exact_gain(
        column,
        attribute_id=str(column.get("attribute_id") or "") if isinstance(column, dict) else None,
    )
    return strict.get("status") == "reliable_exact" and strict.get("value") == value


def _demote_ambiguous_precheck_primary(option: dict[str, Any], tab: str) -> None:
    option["stats_gain"].pop(tab, None)
    unknown_fields = option.get("unknown_fields")
    if isinstance(unknown_fields, list):
        _append_unknown(unknown_fields, "stats_gain_partial")
    option["confidence"] = min(float(option.get("confidence", 0.0) or 0.0), 0.64)
    option["gain_breakdown"] = _build_gain_breakdown(
        stats_gain=option["stats_gain"],
        base_gain=_clean_stats_mapping(option.get("base_gain")),
        fragment_or_scenario_gain=_clean_stats_mapping(option.get("bonus_gain")),
        total_gain=_clean_stats_mapping(option.get("total_gain")),
        stats_gain_ocr=option.get("stats_gain_ocr"),
        unknown_fields=unknown_fields if isinstance(unknown_fields, list) else [],
    )


def _precheck_field_vote_state(option: dict[str, Any], key: str) -> str:
    """Classify a precheck field without letting unsafe values cast a vote."""
    column = _stats_gain_columns_by_attr(option.get("stats_gain_ocr")).get(key)
    if not isinstance(column, dict):
        return "missing"
    if str(column.get("status") or "") in {"rejected", "unreliable"}:
        return "explicitly_rejected"
    # A confirmed '+' with an unreliable multi-digit token is an explicit
    # refusal of that layer.  It remains distinct from a missing observation.
    for candidate in column.get("candidates") or []:
        if not isinstance(candidate, dict) or candidate.get("value") is None:
            continue
        tokens = [str(token) for token in candidate.get("tokens") or []]
        if sum(token.isdigit() for token in tokens[1:]) < 2:
            continue
        matches = candidate.get("component_matches")
        if not isinstance(matches, list) or not matches:
            continue
        if len(matches) != len(tokens):
            return "missing"
        if all(
            isinstance(match, dict) and bool(match.get("reliable"))
            for match in matches
        ):
            continue
        plus = matches[0]
        if isinstance(plus, dict) and bool(plus.get("reliable")):
            return "explicitly_rejected"
    return "missing"


def _precheck_field_observed_value(option: dict[str, Any], key: str) -> int | None:
    value = _clean_stats_mapping(option.get("total_gain")).get(key)
    return value if isinstance(value, int) else None


def _accepted_precheck_gain_fields(
    option: dict[str, Any],
    expected_keys: set[str],
) -> dict[str, int]:
    total_gain = _clean_stats_mapping(option.get("total_gain"))
    columns = _stats_gain_columns_by_attr(option.get("stats_gain_ocr"))
    accepted: dict[str, int] = {}
    base_gain = _clean_stats_mapping(option.get("base_gain"))
    bonus_gain = _clean_stats_mapping(option.get("bonus_gain"))
    for key in expected_keys:
        value = total_gain.get(key)
        column = columns.get(key)
        if not isinstance(value, int) or not isinstance(column, dict):
            continue
        if (
            classify_reliable_exact_gain(column, attribute_id=key).get("status") != "reliable_exact"
            or classify_reliable_exact_gain(column, attribute_id=key).get("value") != value
        ):
            continue
        if base_gain.get(key, 0) + bonus_gain.get(key, 0) != value:
            continue
        accepted[key] = value
    return accepted


def _column_accepted_components_are_reliable(column: dict[str, Any]) -> bool:
    return classify_reliable_exact_gain(
        column,
        attribute_id=str(column.get("attribute_id") or ""),
    ).get("status") == "reliable_exact"


def _normalize_parsed_option(
    parsed: Any, *, fallback_tab: str, scenario: str | None = "grand_masters"
) -> dict[str, Any]:
    if hasattr(parsed, "to_dict"):
        data = parsed.to_dict()
    elif isinstance(parsed, dict):
        data = dict(parsed)
    else:
        raise ValueError(f"parser_result_not_supported:{type(parsed).__name__}")
    if scenario != "grand_masters":
        data = dict(data)
        for key in ("fragment_color", "fragment_stat", "fragment_available", "fragment_state", "fragment_count"):
            data[key] = None
        data["fragments"] = []
        mechanics = _clean_json_object(data.get("scenario_mechanics"))
        mechanics.pop("grand_masters", None)
        data["scenario_mechanics"] = mechanics
        if isinstance(data.get("unknown_fields"), list):
            data["unknown_fields"] = [key for key in data["unknown_fields"] if key != "fragment_color"]
        if isinstance(data.get("field_reliability"), dict):
            data["field_reliability"] = dict(data["field_reliability"])
            data["field_reliability"].pop("fragment", None)
    unknown_fields = (
        list(data.get("unknown_fields", []))
        if isinstance(data.get("unknown_fields", []), list)
        else []
    )
    stats_gain = _clean_stats_mapping(data.get("stats_gain"))
    base_gain = _clean_stats_mapping(data.get("base_gain"))
    bonus_gain = _clean_stats_mapping(data.get("bonus_gain"))
    stats_gain_total = _clean_stats_mapping(data.get("stats_gain_total"))

    stats_gain_ocr = data.get("stats_gain_ocr")
    if not isinstance(stats_gain_ocr, dict):
        # Nested raw_mimo is audit-only, never a formal OCR provenance source.
        stats_gain_ocr = None
        if stats_gain:
            _append_unknown(unknown_fields, "stats_gain_unverified_provenance")
    stats_gain = _reliable_exact_stats_gain(stats_gain, stats_gain_ocr)

    if _stats_gain_is_unreliable(data, unknown_fields):
        if _partial_stats_gain_is_usable(stats_gain, stats_gain_ocr):
            _remove_unknown(unknown_fields, "local_template_incomplete")
            _remove_unknown(unknown_fields, "local_ocr_value_unreliable")
            _remove_unknown(unknown_fields, "stats_gain_unreliable")
            _append_unknown(unknown_fields, "stats_gain_partial")
        else:
            stats_gain = {}
            _append_unknown(unknown_fields, "stats_gain_unreliable")

    failure_rate = data.get("failure_rate")
    if "failure_rate" in unknown_fields or "failure_rate_unreliable" in unknown_fields:
        failure_rate = None
    failure_rate_ocr = data.get("failure_rate_ocr")
    if not isinstance(failure_rate_ocr, dict):
        failure_rate_ocr = None
    field_reliability = data.get("field_reliability")
    if not isinstance(field_reliability, dict):
        field_reliability = None
    elif isinstance(stats_gain_ocr, dict):
        field_reliability = dict(field_reliability)
        stats_quality = field_reliability.get("stats_gain")
        stats_quality = dict(stats_quality) if isinstance(stats_quality, dict) else {}
        columns = _stats_gain_columns_by_attr(stats_gain_ocr)
        for key in STAT_KEYS:
            if _dynamic_gain_is_reliable(
                stats_gain_ocr,
                columns.get(key),
                key,
                stats_gain.get(key),
            ):
                stats_quality[key] = "reliable"
                continue
            strict = classify_reliable_exact_gain(columns.get(key), attribute_id=key)
            if strict.get("status") == "reliable_exact" and stats_gain.get(key) == strict.get("value"):
                stats_quality[key] = "reliable"
            elif key in columns:
                stats_quality[key] = "partial"
            else:
                stats_quality[key] = "unreliable"
        field_reliability["stats_gain"] = stats_quality

    support_count = _optional_int(data.get("support_count"))
    if (
        support_count is not None
        and (support_count < 0 or support_count > 6)
    ) or any(
        field in unknown_fields
        for field in ("support_detection_unreliable", "support_count_unreliable")
    ):
        support_count = None
        _append_unknown(unknown_fields, "support_count_unreliable")

    hint_count = _optional_int(data.get("hint_count"))
    hint_count_invalid = hint_count is not None and (hint_count < 0 or hint_count > 6)
    hint_count_explicitly_unreliable = "hint_count_unreliable" in unknown_fields
    hint_detection_unreliable = "hint_detection_unreliable" in unknown_fields
    if hint_count_invalid or hint_count_explicitly_unreliable:
        hint_count = None
        _append_unknown(unknown_fields, "hint_count_unreliable")
    elif hint_count is None and hint_detection_unreliable:
        _append_unknown(unknown_fields, "hint_count_unreliable")

    fragment_available = data.get("fragment_available")
    fragment_state = data.get("fragment_state")
    fragment_count = data.get("fragment_count")
    fragment_color = data.get("fragment_color")
    fragment_stat = data.get("fragment_stat")
    fragments = _clean_fragments(data.get("fragments"))
    scenario_mechanics = _canonicalize_fragment_production(
        _clean_json_object(data.get("scenario_mechanics"))
    )
    if fragment_available is False or fragment_state in {"full", "effect_active"}:
        fragment_color = None
        fragment_stat = None
        fragments = []
        if not fragment_state:
            fragment_state = "effect_active"
    if fragments and not scenario_mechanics.get("grand_masters"):
        scenario_mechanics["grand_masters"] = {
            "mechanic": "fragments",
            "state": fragment_state,
            "fragments": fragments,
        }
    if fragment_state in {"full", "effect_active"}:
        grand_masters = scenario_mechanics.get("grand_masters")
        if not isinstance(grand_masters, dict):
            grand_masters = {}
        grand_masters["mechanic"] = "fragments"
        grand_masters["state"] = fragment_state
        grand_masters["fragments"] = []
        grand_masters["production"] = (
            "effect_active_only"
            if fragment_state == "effect_active"
            else "no_new_fragment"
        )
        scenario_mechanics["grand_masters"] = grand_masters

    gain_breakdown = _build_gain_breakdown(
        stats_gain=stats_gain,
        base_gain=base_gain,
        fragment_or_scenario_gain=bonus_gain,
        total_gain=stats_gain_total,
        stats_gain_ocr=stats_gain_ocr,
        unknown_fields=unknown_fields,
    )

    support_slots = (
        data.get("support_slots", [])
        if isinstance(data.get("support_slots", []), list)
        else []
    )
    entity_counts = {
        field: _bounded_optional_count(data.get(field))
        for field in (
            "visible_portrait_count",
            "effective_support_count",
            "non_support_npc_count",
            "unresolved_portrait_count",
        )
    }
    option = {
        "training_type": str(data.get("training_type") or fallback_tab),
        "stats_gain": stats_gain,
        "base_gain": base_gain,
        "bonus_gain": bonus_gain,
        "fragment_or_scenario_gain": bonus_gain,
        "stats_gain_total": stats_gain_total,
        "total_gain": stats_gain_total,
        "gain_breakdown": gain_breakdown,
        "failure_rate": failure_rate,
        "support_count": support_count,
        **entity_counts,
        "hint_count": hint_count,
        "friendship": data.get("friendship"),
        "fragment_color": fragment_color,
        "fragment_stat": fragment_stat,
        "fragment_available": fragment_available,
        "fragment_state": fragment_state,
        "fragment_count": fragment_count,
        "fragments": fragments,
        "scenario_mechanics": scenario_mechanics,
        "vitality_delta": data.get("vitality_delta"),
        "confidence": float(data.get("confidence", 0.0) or 0.0),
        "unknown_fields": unknown_fields,
        "stats_gain_ocr": stats_gain_ocr,
        "support_slots": support_slots,
    }
    if failure_rate_ocr is not None:
        option["failure_rate_ocr"] = dict(failure_rate_ocr)
    if field_reliability is not None:
        option["field_reliability"] = dict(field_reliability)
    option["head_count"] = support_count
    option["hint_badge_count"] = hint_count
    option["red_dot_count"] = hint_count
    option["identity_candidates"] = _collect_identity_candidates(support_slots)
    # The shared score also counts GM fragments and optional contextual fields.
    # Complete verified L'Arc gain/failure evidence uses the existing usable
    # gain floor without pretending unknown friendship or vitality is known.
    if (scenario == "larc"
            and set(stats_gain) == _EXPECTED_TRAINING_GAIN_KEYS.get(option["training_type"])
            and type(failure_rate) is int and 0 <= failure_rate <= 100
            and isinstance(failure_rate_ocr, dict) and failure_rate_ocr.get("status") == "accepted"
            and all(_dynamic_gain_is_reliable(stats_gain_ocr,
                    _stats_gain_columns_by_attr(stats_gain_ocr).get(key), key, value)
                    for key, value in stats_gain.items())):
        option["confidence"] = max(option["confidence"], 0.65)
    return option


def _apply_scenario_context_to_option(
    option: dict[str, Any],
    state_context: dict[str, Any] | None,
) -> None:
    if not isinstance(state_context, dict):
        return
    scenario_state = state_context.get("scenario_state")
    if not isinstance(scenario_state, dict):
        return
    if scenario_state.get("scenario") != "grand_masters":
        return

    if _passion_zone_is_explicitly_enabled(scenario_state):
        _promote_self_consistent_layered_totals(option)

    fragment_count = _optional_int(scenario_state.get("fragment_count"))
    fragment_state_text = str(scenario_state.get("fragment_state") or "")
    fragments_full = bool(fragment_count is not None and fragment_count >= 8)
    effect_active = "effect_active" in fragment_state_text
    full_or_effect = "full_or_effect_active" in fragment_state_text
    full_state = fragments_full or fragment_state_text == "full" or full_or_effect
    if not (full_state or effect_active):
        return

    scenario_mechanics = option.get("scenario_mechanics")
    if not isinstance(scenario_mechanics, dict):
        scenario_mechanics = {}
    grand_masters = scenario_mechanics.get("grand_masters")
    if not isinstance(grand_masters, dict):
        grand_masters = {}
    legacy_state = "effect_active" if effect_active and not full_or_effect else "full"
    grand_masters["legacy_state_context"] = {"state": legacy_state, "fragment_count": fragment_count}
    recognized_state = grand_masters.get("state") or option.get("fragment_state")
    if recognized_state and recognized_state != legacy_state:
        grand_masters["legacy_conflict"] = True
        grand_masters["prospective_fragment_output"] = {"status": "abstain", "reason": "legacy_state_context_conflict"}
    else:
        grand_masters["legacy_conflict"] = False
    scenario_mechanics["grand_masters"] = grand_masters
    option["scenario_mechanics"] = scenario_mechanics


def _passion_zone_is_explicitly_enabled(scenario_state: dict[str, Any]) -> bool:
    condition_context = scenario_state.get("condition_context")
    if not isinstance(condition_context, dict):
        return False
    if condition_context.get("schema_version") != "condition_context.v1":
        return False
    source = condition_context.get("source")
    if not isinstance(source, dict):
        return False
    if not all(
        isinstance(source.get(key), str) and bool(source.get(key))
        for key in ("report_path", "report_sha256", "condition_source_sha256")
    ):
        return False
    condition_source_sha256 = source["condition_source_sha256"]
    conditions = scenario_state.get("conditions")
    if not isinstance(conditions, list):
        return False
    return any(
        isinstance(condition, dict)
        and condition.get("condition_id") == "ancestral_guidance_passion_zone"
        and isinstance(condition.get("effects"), dict)
        and condition["effects"].get("group_card_friendship_training_enabled") is True
        and condition.get("source_sha256") == condition_source_sha256
        for condition in conditions
    )


def _promote_self_consistent_layered_totals(option: dict[str, Any]) -> None:
    base_gain = _clean_stats_mapping(option.get("base_gain"))
    bonus_gain = _clean_stats_mapping(option.get("bonus_gain"))
    total_gain = _clean_stats_mapping(option.get("total_gain"))
    columns = _stats_gain_columns_by_attr(option.get("stats_gain_ocr"))
    if not total_gain or not columns:
        return

    promoted: dict[str, int] = {}
    for key, total_value in total_gain.items():
        if total_value > 199:
            continue
        base_value = int(base_gain.get(key, 0) or 0)
        bonus_value = int(bonus_gain.get(key, 0) or 0)
        if base_value + bonus_value != total_value:
            continue
        column = columns.get(key)
        if not isinstance(column, dict):
            continue
        strict = classify_reliable_exact_gain(column, attribute_id=key)
        if strict.get("status") != "reliable_exact" or strict.get("value") != total_value:
            continue
        promoted[key] = total_value

    if set(promoted) != set(total_gain):
        return

    option["stats_gain"] = promoted
    expected_keys = _EXPECTED_TRAINING_GAIN_KEYS.get(str(option.get("training_type")))
    failure_rate = _optional_int(option.get("failure_rate"))
    if (
        expected_keys is not None
        and set(promoted) == expected_keys
        and failure_rate is not None
        and 0 <= failure_rate <= 100
    ):
        option["confidence"] = max(
            float(option.get("confidence", 0.0) or 0.0),
            0.65,
        )
    unknown_fields = option.get("unknown_fields")
    if isinstance(unknown_fields, list):
        for field in (
            "local_template_incomplete",
            "local_ocr_value_unreliable",
            "stats_gain_unreliable",
            "stats_gain_partial",
            "stats_gain",
        ):
            _remove_unknown(unknown_fields, field)
    option["gain_breakdown"] = _build_gain_breakdown(
        stats_gain=promoted,
        base_gain=base_gain,
        fragment_or_scenario_gain=bonus_gain,
        total_gain=total_gain,
        stats_gain_ocr=option.get("stats_gain_ocr"),
        unknown_fields=unknown_fields if isinstance(unknown_fields, list) else [],
    )
    scenario_mechanics = option.get("scenario_mechanics")
    if not isinstance(scenario_mechanics, dict):
        scenario_mechanics = {}
    scenario_mechanics["condition_bound_layered_gain"] = {
        "condition_id": "ancestral_guidance_passion_zone",
        "status": "verified_self_consistent",
        "promoted_stats": sorted(promoted),
    }
    option["scenario_mechanics"] = scenario_mechanics


def _clean_stats_mapping(values: Any) -> dict[str, int]:
    if not isinstance(values, dict):
        return {}
    return {
        key: value
        for key, value in values.items()
        if key in STAT_KEYS
        and type(value) is int
        and value > 0
    }


def _collect_identity_candidates(support_slots: list[Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for slot_index, slot in enumerate(support_slots):
        if not isinstance(slot, dict):
            continue
        slot_number = _optional_int(slot.get("slot"))
        if slot_number is None:
            slot_number = slot_index
        raw_candidates = slot.get("identity_candidates")
        if isinstance(raw_candidates, list) and raw_candidates:
            for candidate in raw_candidates:
                if not isinstance(candidate, dict):
                    continue
                payload = _clean_json_object(candidate)
                payload["slot"] = slot_number
                if "matched_card_id" not in payload:
                    payload["matched_card_id"] = (
                        slot.get("matched_card_id") if payload.get("status") == "known" else None
                    )
                candidates.append(payload)
            continue

        identity = slot.get("identity")
        if not isinstance(identity, dict):
            identity = {}
        payload = {
            "slot": slot_number,
            "card_id": slot.get("card_id"),
            "matched_card_id": slot.get("matched_card_id"),
            "character_id": slot.get("character_id", "unknown"),
            "display_name": slot.get("identity_display_name", ""),
            "type": slot.get("identity_card_type", "unknown"),
            "confidence": identity.get("confidence", slot.get("confidence", 0.0)),
            "status": identity.get("status", slot.get("identity_status", "unknown")),
            "identity_state": identity.get(
                "identity_state",
                slot.get("identity_state", "unknown_identity"),
            ),
            "reason": identity.get("reason", slot.get("identity_reason")),
            "source": identity.get("source", slot.get("identity_source")),
        }
        candidates.append(_clean_json_object(payload))
    return candidates


def _empty_gain_breakdown() -> dict[str, dict[str, Any]]:
    return {
        key: {
            "base_gain": 0,
            "fragment_or_scenario_gain": 0,
            "total_gain": 0,
            "confidence": 0.0,
            "partial": True,
            "reliable": False,
            "status": "missing",
        }
        for key in STAT_KEYS
    }


def _build_gain_breakdown(
    *,
    stats_gain: dict[str, int],
    base_gain: dict[str, int],
    fragment_or_scenario_gain: dict[str, int],
    total_gain: dict[str, int],
    stats_gain_ocr: dict[str, Any] | None,
    unknown_fields: list[Any],
) -> dict[str, dict[str, Any]]:
    columns = _stats_gain_columns_by_attr(stats_gain_ocr)
    breakdown: dict[str, dict[str, Any]] = {}
    for key in STAT_KEYS:
        base_value = int(base_gain.get(key, 0) or 0)
        fragment_value = int(fragment_or_scenario_gain.get(key, 0) or 0)
        total_value = int(total_gain.get(key, 0) or 0)
        if total_value <= 0 and (base_value > 0 or fragment_value > 0):
            total_value = base_value + fragment_value

        column = columns.get(key)
        reliable_value = int(stats_gain.get(key, 0) or 0)
        reliable = reliable_value > 0 and reliable_value == total_value
        status = str(column.get("status") or "") if isinstance(column, dict) else ""
        if not status:
            status = "accepted" if reliable else ("partial" if total_value > 0 else "missing")
        elif not reliable and total_value > 0:
            status = "partial"

        partial = not reliable
        confidence = _gain_breakdown_confidence(
            reliable=reliable,
            partial=partial,
            status=status,
            column=column,
            unknown_fields=unknown_fields,
        )
        breakdown[key] = {
            "base_gain": base_value,
            "fragment_or_scenario_gain": fragment_value,
            "total_gain": total_value,
            "confidence": confidence,
            "partial": partial,
            "reliable": reliable,
            "status": status,
        }
    return breakdown


def _stats_gain_columns_by_attr(
    stats_gain_ocr: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(stats_gain_ocr, dict):
        return {}
    columns = stats_gain_ocr.get("columns")
    if not isinstance(columns, list):
        return {}
    by_attr: dict[str, dict[str, Any]] = {}
    for column in columns:
        if not isinstance(column, dict):
            continue
        attr = str(column.get("attribute_id") or "")
        if attr in STAT_KEYS:
            by_attr[attr] = column
    return by_attr


def _gain_breakdown_confidence(
    *,
    reliable: bool,
    partial: bool,
    status: str,
    column: dict[str, Any] | None,
    unknown_fields: list[Any],
) -> float:
    if reliable:
        return 0.9
    if status == "missing":
        return 0.0
    if status == "partial":
        return 0.5
    if status in {"candidate", "empty"}:
        return 0.0
    if partial or any(
        field in unknown_fields
        for field in ("local_template_incomplete", "local_ocr_value_unreliable", "stats_gain_partial")
    ):
        return 0.5
    if isinstance(column, dict) and column.get("status") == "accepted":
        return 0.7
    return 0.0


def _clean_fragments(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    fragments: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        color = item.get("color")
        stat = item.get("stat") or item.get("kind")
        count = _optional_int(item.get("count"))
        confidence = item.get("confidence")
        if not color or not stat:
            continue
        fragment: dict[str, Any] = {
            "color": str(color),
            "stat": str(stat),
            "kind": str(item.get("kind") or stat),
            "count": count if count is not None and count > 0 else 1,
            "confidence": float(confidence)
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
            else 0.0,
        }
        source_crop = item.get("source_crop")
        if isinstance(source_crop, dict):
            fragment["source_crop"] = _clean_json_object(source_crop)
        fragments.append(fragment)
    return fragments


def _clean_json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): _clean_json_value(item)
        for key, item in value.items()
        if isinstance(key, str)
    }


def _clean_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _clean_json_object(value)
    if isinstance(value, list):
        return [_clean_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _append_unknown(unknown_fields: list[Any], field: str) -> None:
    if field not in unknown_fields:
        unknown_fields.append(field)


def _remove_unknown(unknown_fields: list[Any], field: str) -> None:
    while field in unknown_fields:
        unknown_fields.remove(field)


def _optional_int(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return None


def _bounded_optional_count(value: Any) -> int | None:
    parsed = _optional_int(value)
    return parsed if parsed is not None and 0 <= parsed <= 6 else None


def _stats_gain_is_unreliable(data: dict[str, Any], unknown_fields: list[Any]) -> bool:
    raw_mimo = data.get("raw_mimo")
    value_unreliable = (
        isinstance(raw_mimo, dict)
        and raw_mimo.get("value_unreliable") is True
    )
    return bool(
        value_unreliable
        or "local_template_incomplete" in unknown_fields
        or "local_ocr_value_unreliable" in unknown_fields
    )


def _partial_stats_gain_is_usable(
    stats_gain: dict[str, Any],
    stats_gain_ocr: dict[str, Any] | None,
) -> bool:
    if not isinstance(stats_gain, dict) or not stats_gain:
        return False
    if not isinstance(stats_gain_ocr, dict):
        return False
    columns = stats_gain_ocr.get("columns")
    if not isinstance(columns, list):
        return False

    by_attr = {
        column.get("attribute_id"): column
        for column in columns
        if isinstance(column, dict) and column.get("attribute_id")
    }
    matched = 0
    for key, value in stats_gain.items():
        if key == "skill_pt":
            limit = 60
        else:
            limit = 40
        if type(value) is not int:
            return False
        numeric = value
        if numeric <= 0 or numeric > limit:
            return False
        column = by_attr.get(key)
        strict = classify_reliable_exact_gain(column, attribute_id=key)
        if strict.get("status") != "reliable_exact" or strict.get("value") != numeric:
            return False
        matched += 1
    return matched > 0


def _reliable_exact_stats_gain(
    stats_gain: dict[str, Any],
    stats_gain_ocr: dict[str, Any] | None,
) -> dict[str, int]:
    columns = _stats_gain_columns_by_attr(stats_gain_ocr)
    reliable: dict[str, int] = {}
    dynamic_source = (
        isinstance(stats_gain_ocr, dict)
        and stats_gain_ocr.get("source") in {"ppocrv5_dynamic", "own_template_v4"}
    )
    for key, value in stats_gain.items():
        if _dynamic_gain_is_reliable(stats_gain_ocr, columns.get(key), key, value):
            reliable[key] = value
            continue
        if dynamic_source and isinstance(columns.get(key), dict):
            continue
        strict = classify_reliable_exact_gain(columns.get(key), attribute_id=key)
        if strict.get("status") == "reliable_exact" and strict.get("value") == value:
            reliable[key] = value
    return reliable


def _dynamic_gain_is_reliable(
    stats_gain_ocr: dict[str, Any] | None,
    column: dict[str, Any] | None,
    key: str,
    value: Any,
) -> bool:
    """Validate the bounded two-row local gain provenance shape."""
    return validate_dynamic_gain_column(
        stats_gain_ocr,
        column,
        attribute=key,
        value=value,
    )
