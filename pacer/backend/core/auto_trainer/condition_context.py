"""Validated explicit-condition context for formal training packets."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping


CONDITION_PROBE_SCHEMA_VERSION = "condition_probe.v1"
CONDITION_CONTEXT_SCHEMA_VERSION = "condition_context.v1"
FORMAL_BINDING_SCHEMA_VERSION = "condition_probe.formal_binding.v2"
ABILITY_DETAILS_ARTIFACT = "ability_details.png"
FORMAL_CONDITION_REPORT_ARTIFACT = "condition_probe.report.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MIN_VERIFIED_MATCH_THRESHOLD = 0.985
_ALLOWED_CONDITIONS = {
    "chubby_tendency": {"speed_training_gain_blocked": True},
    "insomnia_tendency": {"random_vitality_loss_after_training": True},
    "ancestral_guidance_passion_zone": {
        "group_card_friendship_training_enabled": True,
        "prevents_insomnia_and_slacker": True,
    },
    "charm_circle": {"support_bond_gain_increased": True},
    "migraine": {"mood_gain_blocked": True},
}
_NEGATIVE_CONDITION_IDS = frozenset(
    {"chubby_tendency", "insomnia_tendency", "migraine"}
)


def load_condition_assessment(
    *,
    report_path: str | Path,
    artifact_path: str | Path,
    expected_source_sha256: str,
) -> dict[str, object]:
    """Load a hash-bound report while preserving abstention as unknown."""
    if not _is_sha256(expected_source_sha256):
        raise ValueError("condition_expected_source_sha256_invalid")
    report_file = Path(report_path).resolve(strict=False)
    artifact_file = Path(artifact_path).resolve(strict=False)
    try:
        report_bytes = report_file.read_bytes()
    except OSError as exc:
        raise ValueError("condition_report_missing") from exc
    try:
        raw = json.loads(report_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("condition_report_invalid_json") from exc
    if not isinstance(raw, dict):
        raise ValueError("condition_report_root_not_object")
    report = _extract_probe_report(raw, formal_live=False)
    source_sha256 = _validate_assessment_report(report)
    if source_sha256 != expected_source_sha256:
        raise ValueError("condition_report_source_sha256_mismatch")
    try:
        artifact_sha256 = hashlib.sha256(artifact_file.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError("condition_artifact_missing") from exc
    if artifact_sha256 != expected_source_sha256:
        raise ValueError("condition_artifact_sha256_mismatch")

    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    abstained = report["abstained"]
    conditions: tuple[dict[str, Any], ...]
    if abstained:
        status = "unknown"
        conditions = ()
    else:
        raw_conditions = report["conditions"]
        if not raw_conditions:
            status = "healthy"
        elif any(
            condition["condition_id"] in _NEGATIVE_CONDITION_IDS
            for condition in raw_conditions
        ):
            status = "negative"
        else:
            status = "registered"
        binding = {
            "condition_source_sha256": source_sha256,
            "report_sha256": report_sha256,
            "artifact_sha256": artifact_sha256,
        }
        conditions = tuple(
            {
                **copy.deepcopy(condition),
                "formal_context": copy.deepcopy(binding),
            }
            for condition in raw_conditions
        )
    warnings = report.get("warnings", [])
    if not isinstance(warnings, list):
        raise ValueError("condition_report_warnings_invalid")
    return {
        "status": status,
        "conditions": conditions,
        "source_sha256": source_sha256,
        "report_sha256": report_sha256,
        "artifact_sha256": artifact_sha256,
        "warnings": tuple(copy.deepcopy(warnings)),
    }


def condition_allows_clinic(assessment: Mapping[str, object]) -> bool:
    """Return whether a formally hash-bound negative condition is present."""
    if not isinstance(assessment, Mapping) or assessment.get("status") != "negative":
        return False
    source_sha256 = assessment.get("source_sha256")
    report_sha256 = assessment.get("report_sha256")
    artifact_sha256 = assessment.get("artifact_sha256")
    if (
        not _is_sha256(source_sha256)
        or not _is_sha256(report_sha256)
        or not _is_sha256(artifact_sha256)
        or source_sha256 != artifact_sha256
    ):
        return False
    conditions = assessment.get("conditions")
    if not isinstance(conditions, (list, tuple)):
        return False
    expected_context = {
        "condition_source_sha256": source_sha256,
        "report_sha256": report_sha256,
        "artifact_sha256": artifact_sha256,
    }
    for condition in conditions:
        if not isinstance(condition, Mapping):
            continue
        condition_id = condition.get("condition_id")
        if not isinstance(condition_id, str) or condition_id not in _NEGATIVE_CONDITION_IDS:
            continue
        if (
            condition.get("effects") == _ALLOWED_CONDITIONS[condition_id]
            and condition.get("source_sha256") == source_sha256
            and condition.get("formal_context") == expected_context
        ):
            return True
    return False


def load_explicit_condition_context(
    report_path: str | Path,
    *,
    required_turn_root: str | Path | None = None,
    formal_live: bool = False,
    formal_binding_key: str | bytes | None = None,
    expected_turn_id: str | None = None,
    expected_turn_index: int | None = None,
) -> dict[str, Any]:
    """Load a probe CLI report only when its screenshot provenance is intact."""
    report_path_raw = Path(report_path).absolute()
    turn_root_raw = Path(required_turn_root).absolute() if required_turn_root is not None else None
    if formal_live:
        if turn_root_raw is None or not _is_relative_to(report_path_raw, turn_root_raw):
            raise ValueError("condition_report_outside_turn_root")
        if _has_symlink_component(turn_root_raw, report_path_raw):
            raise ValueError("condition_report_symlink")
        if report_path_raw.parent.name != "condition_probe":
            raise ValueError("condition_report_not_in_condition_probe_directory")
        if report_path_raw.name != FORMAL_CONDITION_REPORT_ARTIFACT:
            raise ValueError("condition_report_path_invalid")
    path = report_path_raw.resolve(strict=False)
    turn_root = turn_root_raw.resolve(strict=False) if turn_root_raw is not None else None
    if formal_live and (turn_root is None or not _is_relative_to(path, turn_root)):
        raise ValueError("condition_report_outside_turn_root")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ValueError("condition_report_missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("condition_report_invalid_json") from exc
    if not isinstance(raw, dict):
        raise ValueError("condition_report_root_not_object")

    if formal_live:
        _validate_formal_binding_header(
            raw,
            formal_binding_key=formal_binding_key,
            expected_turn_id=expected_turn_id,
            expected_turn_index=expected_turn_index,
        )
    artifact_path = path.parent / ABILITY_DETAILS_ARTIFACT
    if formal_live:
        assert turn_root is not None
        if _has_symlink_component(turn_root, artifact_path):
            raise ValueError("condition_artifact_symlink")
        artifact_path = artifact_path.resolve(strict=False)
        if not _is_relative_to(artifact_path, turn_root):
            raise ValueError("condition_artifact_outside_turn_root")
    try:
        artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError("condition_artifact_missing") from exc
    if formal_live:
        _validate_formal_binding(
            raw,
            formal_binding_key=formal_binding_key,
            expected_turn_id=expected_turn_id,
            expected_turn_index=expected_turn_index,
            artifact_sha256=artifact_sha256,
        )
    report = _extract_probe_report(raw, formal_live=formal_live)
    source_sha256 = _validate_probe_report(report)
    if artifact_sha256 != source_sha256:
        raise ValueError("condition_artifact_sha256_mismatch")

    report_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    metadata = {
        "schema_version": CONDITION_CONTEXT_SCHEMA_VERSION,
        "source": {
            "report_path": str(path),
            "report_sha256": report_sha256,
            "condition_report_schema_version": CONDITION_PROBE_SCHEMA_VERSION,
            "condition_source_sha256": source_sha256,
        },
        "provenance": {
            "ability_details_path": str(artifact_path.resolve(strict=False)),
            "ability_details_sha256": artifact_sha256,
        },
    }
    if formal_live:
        binding = raw["formal_binding"]
        metadata["formal_binding"] = {
            "schema_version": binding["schema_version"],
            "turn_id": binding["turn_id"],
            "turn_index": binding["turn_index"],
            "artifact_sha256": binding["artifact_sha256"],
            "report_sha256": report_sha256,
            "condition_source_sha256": source_sha256,
            "signature": binding["signature"],
        }
    return {
        "conditions": copy.deepcopy(report["conditions"]),
        "metadata": metadata,
    }


def attach_formal_condition_binding(
    probe_report: dict[str, Any],
    *,
    turn_id: str,
    turn_index: int,
    artifact_path: str | Path,
    binding_key: str | bytes,
) -> dict[str, Any]:
    """Return a signed report envelope without persisting the process-local key."""
    if not isinstance(probe_report, dict):
        raise ValueError("condition_probe_report_root_not_object")
    if not isinstance(turn_id, str) or not turn_id:
        raise ValueError("condition_binding_turn_id_invalid")
    if (
        not isinstance(turn_index, int)
        or isinstance(turn_index, bool)
        or turn_index < 0
    ):
        raise ValueError("condition_binding_turn_index_invalid")
    artifact_sha256 = hashlib.sha256(Path(artifact_path).read_bytes()).hexdigest()
    unsigned = copy.deepcopy(probe_report)
    unsigned.pop("formal_binding", None)
    signed = copy.deepcopy(unsigned)
    signed["formal_binding"] = {
        "schema_version": FORMAL_BINDING_SCHEMA_VERSION,
        "turn_id": turn_id,
        "turn_index": turn_index,
        "artifact_sha256": artifact_sha256,
        "signature": _binding_signature(
            unsigned,
            turn_id=turn_id,
            turn_index=turn_index,
            artifact_sha256=artifact_sha256,
            binding_key=binding_key,
        ),
    }
    return signed


def merge_explicit_conditions(
    state_context: dict[str, Any], condition_context: dict[str, Any]
) -> dict[str, Any]:
    """Preserve the captured state while replacing only explicit condition evidence."""
    if not isinstance(state_context, dict):
        raise ValueError("state_context_root_not_object")
    conditions = condition_context.get("conditions")
    metadata = condition_context.get("metadata")
    if not isinstance(conditions, list) or not isinstance(metadata, dict):
        raise ValueError("condition_context_invalid")
    merged = copy.deepcopy(state_context)
    scenario_state = merged.get("scenario_state")
    if scenario_state is None:
        scenario_state = {}
    if not isinstance(scenario_state, dict):
        raise ValueError("scenario_state_root_not_object")
    scenario_state = copy.deepcopy(scenario_state)
    scenario_state["conditions"] = copy.deepcopy(conditions)
    scenario_state["condition_context"] = copy.deepcopy(metadata)
    merged["scenario_state"] = scenario_state
    return merged


def inspect_formally_bound_condition(
    scenario_state: dict[str, Any],
    *,
    expected_turn: int | None,
    condition_id: str,
    effects: dict[str, Any],
    formal_binding_key: str | bytes | None = None,
    required_turn_root: str | Path | None = None,
    expected_turn_id: str | None = None,
) -> dict[str, Any]:
    """Revalidate an explicit condition against this turn's formal packet."""
    evidence: dict[str, Any] = {
        "valid": False,
        "reason": "condition_not_present",
        "condition_id": condition_id,
        "expected_turn": expected_turn,
    }
    conditions = scenario_state.get("conditions")
    if not isinstance(conditions, list):
        evidence["reason"] = "condition_list_invalid"
        return evidence
    condition = next(
        (
            item
            for item in conditions
            if isinstance(item, dict)
            and item.get("condition_id") == condition_id
            and item.get("effects") == effects
        ),
        None,
    )
    if condition is None:
        return evidence
    metadata = scenario_state.get("condition_context")
    if not isinstance(metadata, dict):
        evidence["reason"] = "condition_context_missing"
        return evidence
    source = metadata.get("source")
    report_path = source.get("report_path") if isinstance(source, dict) else None
    if not isinstance(report_path, str) or not report_path:
        evidence["reason"] = "condition_context_source_invalid"
        return evidence
    if (
        formal_binding_key is None
        or required_turn_root is None
        or not isinstance(expected_turn_id, str)
        or not expected_turn_id
        or not isinstance(expected_turn, int)
        or isinstance(expected_turn, bool)
    ):
        evidence["reason"] = "condition_formal_revalidation_context_missing"
        return evidence
    try:
        canonical_turn_root = Path(required_turn_root).resolve(strict=False)
        revalidated = load_explicit_condition_context(
            report_path,
            required_turn_root=canonical_turn_root,
            formal_live=True,
            formal_binding_key=formal_binding_key,
            expected_turn_id=expected_turn_id,
            expected_turn_index=expected_turn,
        )
    except ValueError as exc:
        evidence["reason"] = "condition_formal_revalidation_failed"
        evidence["validation_error"] = str(exc)
        return evidence

    if (
        revalidated.get("conditions") != conditions
        or revalidated.get("metadata") != metadata
    ):
        evidence["reason"] = "condition_context_state_mismatch"
        return evidence

    binding = revalidated["metadata"]["formal_binding"]
    revalidated_source = revalidated["metadata"]["source"]

    evidence.update(
        {
            "valid": True,
            "reason": "accepted",
            "binding_turn_id": binding["turn_id"],
            "binding_turn_index": binding["turn_index"],
            "report_sha256": revalidated_source["report_sha256"],
            "condition_source_sha256": revalidated_source[
                "condition_source_sha256"
            ],
        }
    )
    return evidence


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _extract_probe_report(raw: dict[str, Any], *, formal_live: bool) -> dict[str, Any]:
    if raw.get("schema_version") == CONDITION_PROBE_SCHEMA_VERSION:
        if formal_live:
            raise ValueError("condition_report_script_output_required")
        return raw
    if (
        raw.get("is_valid") is not True
        or raw.get("status") != "completed"
        or raw.get("performed") is not True
        or raw.get("reason") != "completed"
    ):
        raise ValueError("condition_probe_not_completed")
    result = raw.get("result")
    if not isinstance(result, dict):
        raise ValueError("condition_probe_result_missing")
    report = result.get("condition_report")
    if not isinstance(report, dict):
        raise ValueError("condition_probe_report_missing")
    return report


def _validate_probe_report(report: dict[str, Any]) -> str:
    if report.get("schema_version") != CONDITION_PROBE_SCHEMA_VERSION:
        raise ValueError("condition_report_schema_invalid")
    if report.get("abstained") is not False:
        raise ValueError("condition_report_abstained")
    source = report.get("source")
    if not isinstance(source, dict):
        raise ValueError("condition_report_source_invalid")
    source_sha256 = source.get("sha256")
    if not isinstance(source_sha256, str) or not _SHA256_RE.fullmatch(source_sha256):
        raise ValueError("condition_report_source_sha256_invalid")
    page = report.get("page")
    if (
        not isinstance(page, dict)
        or page.get("value") != "ability_details"
        or page.get("abstained") is not False
    ):
        raise ValueError("condition_report_page_invalid")
    conditions = report.get("conditions")
    if not isinstance(conditions, list):
        raise ValueError("condition_report_conditions_invalid")
    if not conditions:
        _validate_empty_state(report.get("empty_state"), page=page)
        return source_sha256
    for condition in conditions:
        if not isinstance(condition, dict):
            raise ValueError("condition_report_condition_invalid")
        condition_id = condition.get("condition_id")
        if condition_id not in _ALLOWED_CONDITIONS:
            raise ValueError("condition_report_condition_not_whitelisted")
        if condition.get("effects") != _ALLOWED_CONDITIONS[condition_id]:
            raise ValueError("condition_report_condition_effects_invalid")
        if condition.get("source_sha256") != source_sha256:
            raise ValueError("condition_report_condition_source_sha256_mismatch")
    return source_sha256


def _validate_assessment_report(report: dict[str, Any]) -> str:
    """Validate conclusive or structurally abstained probe semantics."""
    if report.get("abstained") is False:
        source_sha256 = _validate_probe_report(report)
        conditions = report["conditions"]
        if conditions:
            _validate_nonconclusive_empty_state(report.get("empty_state"))
        return source_sha256
    if report.get("schema_version") != CONDITION_PROBE_SCHEMA_VERSION:
        raise ValueError("condition_report_schema_invalid")
    if report.get("abstained") is not True:
        raise ValueError("condition_report_abstained_invalid")
    source = report.get("source")
    if not isinstance(source, dict) or not _is_sha256(source.get("sha256")):
        raise ValueError("condition_report_source_sha256_invalid")
    page = report.get("page")
    if not isinstance(page, dict) or not isinstance(page.get("abstained"), bool):
        raise ValueError("condition_report_page_invalid")
    page_value = page.get("value")
    if page.get("abstained") is False and page_value != "ability_details":
        raise ValueError("condition_report_page_invalid")
    if page.get("abstained") is True and page_value is not None and not isinstance(
        page_value, str
    ):
        raise ValueError("condition_report_page_invalid")
    page_confidence = page.get("confidence")
    if (
        not _is_finite_number(page_confidence)
        or not 0.0 <= float(page_confidence) <= 1.0
    ):
        raise ValueError("condition_report_page_invalid")
    conditions = report.get("conditions")
    if not isinstance(conditions, list) or conditions:
        raise ValueError("condition_report_abstained_conditions_invalid")
    _validate_nonconclusive_empty_state(report.get("empty_state"))
    return source["sha256"]


def _validate_nonconclusive_empty_state(empty_state: Any) -> None:
    if (
        not isinstance(empty_state, dict)
        or empty_state.get("value") is not None
        or empty_state.get("abstained") is not True
        or empty_state.get("verified") is True
        or not isinstance(empty_state.get("evidence"), dict)
        or not _is_finite_number(empty_state.get("confidence"))
        or float(empty_state["confidence"]) != 0.0
    ):
        raise ValueError("condition_report_empty_state_contradictory")


def _validate_empty_state(empty_state: Any, *, page: dict[str, Any]) -> None:
    if (
        not isinstance(empty_state, dict)
        or empty_state.get("value") != "no_conditions"
        or empty_state.get("abstained") is not False
    ):
        raise ValueError("condition_report_conditions_invalid")
    page_confidence = page.get("confidence")
    confidence = empty_state.get("confidence")
    evidence = empty_state.get("evidence")
    if (
        not _is_finite_number(page_confidence)
        or float(page_confidence) < _MIN_VERIFIED_MATCH_THRESHOLD
        or not _is_finite_number(confidence)
        or not isinstance(evidence, dict)
        or evidence.get("recognizer") != "verified_empty_state_roi_template"
    ):
        raise ValueError("condition_report_empty_state_invalid")
    for key in ("template_asset_sha256", "template_source_sha256"):
        value = evidence.get(key)
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise ValueError("condition_report_empty_state_invalid")
    provenance = evidence.get("template_provenance")
    roi = evidence.get("roi")
    score = evidence.get("template_score")
    threshold = evidence.get("threshold")
    if (
        not isinstance(provenance, str)
        or not provenance
        or not _is_valid_roi(roi)
        or not _is_finite_number(score)
        or not _is_finite_number(threshold)
        or not 0.0 <= float(score) <= 1.0
        or not _MIN_VERIFIED_MATCH_THRESHOLD <= float(threshold) <= 1.0
        or float(score) < float(threshold)
        or float(confidence) < float(threshold)
        or abs(float(confidence) - float(score)) > 0.0001
    ):
        raise ValueError("condition_report_empty_state_invalid")


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _is_valid_roi(value: Any) -> bool:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(not isinstance(item, int) or isinstance(item, bool) for item in value)
    ):
        return False
    x0, y0, x1, y1 = value
    return x0 >= 0 and y0 >= 0 and x1 > x0 and y1 > y0


def _validate_formal_binding(
    raw: dict[str, Any],
    *,
    formal_binding_key: str | bytes | None,
    expected_turn_id: str | None,
    expected_turn_index: int | None,
    artifact_sha256: str,
) -> None:
    binding = _validate_formal_binding_header(
        raw,
        formal_binding_key=formal_binding_key,
        expected_turn_id=expected_turn_id,
        expected_turn_index=expected_turn_index,
    )
    if binding.get("artifact_sha256") != artifact_sha256:
        raise ValueError("condition_binding_artifact_sha256_mismatch")
    signature = binding["signature"]
    unsigned = copy.deepcopy(raw)
    unsigned.pop("formal_binding", None)
    expected_signature = _binding_signature(
        unsigned,
        turn_id=expected_turn_id,
        turn_index=expected_turn_index,
        artifact_sha256=artifact_sha256,
        binding_key=formal_binding_key,
    )
    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("condition_binding_signature_mismatch")


def _validate_formal_binding_header(
    raw: dict[str, Any],
    *,
    formal_binding_key: str | bytes | None,
    expected_turn_id: str | None,
    expected_turn_index: int | None,
) -> dict[str, Any]:
    if formal_binding_key is None:
        raise ValueError("condition_binding_key_missing")
    if not isinstance(expected_turn_id, str) or not expected_turn_id:
        raise ValueError("condition_binding_expected_turn_id_missing")
    if (
        not isinstance(expected_turn_index, int)
        or isinstance(expected_turn_index, bool)
        or expected_turn_index < 0
    ):
        raise ValueError("condition_binding_expected_turn_index_missing")
    binding = raw.get("formal_binding")
    if not isinstance(binding, dict):
        raise ValueError("condition_binding_missing")
    if binding.get("schema_version") != FORMAL_BINDING_SCHEMA_VERSION:
        raise ValueError("condition_binding_schema_invalid")
    if binding.get("turn_id") != expected_turn_id:
        raise ValueError("condition_binding_turn_id_mismatch")
    binding_turn_index = binding.get("turn_index")
    if not isinstance(binding_turn_index, int) or isinstance(
        binding_turn_index, bool
    ):
        raise ValueError("condition_binding_turn_index_invalid")
    if binding_turn_index != expected_turn_index:
        raise ValueError("condition_binding_turn_index_mismatch")
    signature = binding.get("signature")
    if not isinstance(signature, str) or not _SHA256_RE.fullmatch(signature):
        raise ValueError("condition_binding_signature_invalid")
    return binding


def _binding_signature(
    unsigned_report: dict[str, Any],
    *,
    turn_id: str,
    turn_index: int,
    artifact_sha256: str,
    binding_key: str | bytes,
) -> str:
    if isinstance(binding_key, str):
        key = binding_key.encode("utf-8")
    elif isinstance(binding_key, bytes):
        key = binding_key
    else:
        raise ValueError("condition_binding_key_invalid")
    if not key:
        raise ValueError("condition_binding_key_invalid")
    try:
        payload = json.dumps(
            {
                "artifact_sha256": artifact_sha256,
                "report": unsigned_report,
                "turn_id": turn_id,
                "turn_index": turn_index,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("condition_binding_payload_invalid") from exc
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _has_symlink_component(root: Path, path: Path) -> bool:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError:
        return True
    current = root.absolute()
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False
