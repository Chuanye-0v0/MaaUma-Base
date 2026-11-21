"""Immutable cached condition authority with fresh formal turn bindings."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.auto_trainer.condition_context import (
    ABILITY_DETAILS_ARTIFACT,
    FORMAL_CONDITION_REPORT_ARTIFACT,
    _NEGATIVE_CONDITION_IDS,
    _extract_probe_report,
    _validate_assessment_report,
    attach_formal_condition_binding,
    load_condition_assessment,
    load_explicit_condition_context,
)


AUTHORITY_SCHEMA_VERSION = "condition_authority.v1"
AUTHORITY_FILES = ("probe.report.json", ABILITY_DETAILS_ARTIFACT, "manifest.json")
_MANIFEST_KEYS = frozenset(
    {"schema_version", "authority_id", "report_sha256", "artifact_sha256", "status"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CACHEABLE_STATUSES = frozenset({"healthy", "registered", "negative"})
_REPARSE_POINT_ATTRIBUTE = 0x400


@dataclass(frozen=True)
class ConditionAuthority:
    authority_id: str
    report_sha256: str
    artifact_sha256: str
    status: str
    report_path: Path
    artifact_path: Path
    manifest_path: Path


@dataclass(frozen=True)
class ConditionAuthorityMaterialization:
    report_path: Path
    artifact_path: Path


@dataclass(frozen=True)
class _ValidatedAuthorityInput:
    report_bytes: bytes
    artifact_bytes: bytes
    report_sha256: str
    artifact_sha256: str
    status: str
    assessment: Mapping[str, object]


@dataclass(frozen=True)
class _CachedAuthority:
    authority: ConditionAuthority
    report_bytes: bytes
    artifact_bytes: bytes
    assessment: Mapping[str, object]


class ConditionAuthorityCache:
    def __init__(self, dashboard_run_dir: str | Path) -> None:
        self.dashboard_run_dir = Path(dashboard_run_dir).absolute()
        self.root = self.dashboard_run_dir / ".runtime" / "condition_authority"

    def store_verified(
        self,
        *,
        report_path: str | Path,
        artifact_path: str | Path,
        assessment: Mapping[str, object],
    ) -> ConditionAuthority:
        validated = _validate_authority_input(
            report_path=Path(report_path),
            artifact_path=Path(artifact_path),
            assessment=assessment,
        )
        return _store_content_addressed_authority(
            self.dashboard_run_dir, validated
        )

    def materialize_formal_context(
        self,
        *,
        authority_id: str,
        turn_root: str | Path,
        turn_id: str,
        turn_index: int,
        binding_key: str,
    ) -> ConditionAuthorityMaterialization:
        cached = _load_cached_authority(self.dashboard_run_dir, authority_id)
        return _write_and_validate_turn_binding(
            cached=cached,
            turn_root=Path(turn_root),
            turn_id=turn_id,
            turn_index=turn_index,
            binding_key=binding_key,
        )

    def validate(self, authority_id: str) -> ConditionAuthority:
        """Validate one cached authority without materializing a turn binding."""
        return _load_cached_authority(
            self.dashboard_run_dir, authority_id
        ).authority


def _validate_authority_input(
    *, report_path: Path, artifact_path: Path, assessment: Mapping[str, object]
) -> _ValidatedAuthorityInput:
    if not isinstance(assessment, Mapping):
        raise ValueError("condition_authority_assessment_invalid")
    report_bytes = _read_untrusted_file_snapshot(report_path)
    artifact_bytes = _read_untrusted_file_snapshot(artifact_path)
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    revalidated = _assessment_from_snapshot(report_bytes, artifact_sha256)
    if dict(assessment) != revalidated:
        raise ValueError("condition_authority_assessment_mismatch")
    status = revalidated["status"]
    if status not in _CACHEABLE_STATUSES:
        raise ValueError("condition_authority_assessment_status_invalid")
    return _ValidatedAuthorityInput(
        report_bytes=report_bytes,
        artifact_bytes=artifact_bytes,
        report_sha256=hashlib.sha256(report_bytes).hexdigest(),
        artifact_sha256=artifact_sha256,
        status=status,
        assessment=copy.deepcopy(revalidated),
    )


def _store_content_addressed_authority(
    dashboard_run_dir: Path, validated: _ValidatedAuthorityInput
) -> ConditionAuthority:
    root = _ensure_cache_root(dashboard_run_dir)
    directory = root / validated.report_sha256
    _reject_reparse_component(root, directory)
    manifest = {
        "schema_version": AUTHORITY_SCHEMA_VERSION,
        "authority_id": validated.report_sha256,
        "report_sha256": validated.report_sha256,
        "artifact_sha256": validated.artifact_sha256,
        "status": validated.status,
    }
    try:
        directory.mkdir()
    except FileExistsError:
        cached = _load_cached_authority(dashboard_run_dir, validated.report_sha256)
        if (
            cached.report_bytes != validated.report_bytes
            or cached.artifact_bytes != validated.artifact_bytes
            or cached.authority.status != validated.status
        ):
            raise ValueError("condition_authority_existing_content_conflict")
        return cached.authority
    _write_exclusive(directory / AUTHORITY_FILES[0], validated.report_bytes)
    _write_exclusive(directory / AUTHORITY_FILES[1], validated.artifact_bytes)
    _write_exclusive(directory / AUTHORITY_FILES[2], _canonical_json(manifest))
    return _authority_from_directory(directory, manifest)


def _load_cached_authority(
    dashboard_run_dir: Path, authority_id: str
) -> _CachedAuthority:
    if not _is_sha256(authority_id):
        raise ValueError("condition_authority_id_invalid")
    dashboard = Path(dashboard_run_dir).absolute()
    _reject_existing_ancestor_reparse(dashboard)
    runtime = dashboard / ".runtime"
    _reject_reparse_component(dashboard, runtime)
    root = runtime / "condition_authority"
    _reject_reparse_component(dashboard, root)
    directory = root / authority_id
    _reject_reparse_component(dashboard, directory)
    try:
        manifest_bytes = _read_boundary_file_snapshot(directory / "manifest.json")
        report_bytes = _read_boundary_file_snapshot(directory / "probe.report.json")
        artifact_bytes = _read_boundary_file_snapshot(directory / ABILITY_DETAILS_ARTIFACT)
    except OSError as exc:
        raise ValueError("condition_authority_missing") from exc
    manifest = _parse_strict_object(
        manifest_bytes, "condition_authority_manifest_invalid_json"
    )
    if set(manifest) != _MANIFEST_KEYS:
        raise ValueError("condition_authority_manifest_keys_invalid")
    if manifest.get("schema_version") != AUTHORITY_SCHEMA_VERSION:
        raise ValueError("condition_authority_manifest_schema_invalid")
    if manifest.get("authority_id") != authority_id:
        raise ValueError("condition_authority_manifest_id_mismatch")
    if (
        manifest.get("report_sha256") != hashlib.sha256(report_bytes).hexdigest()
        or manifest.get("report_sha256") != authority_id
    ):
        raise ValueError("condition_authority_report_sha256_mismatch")
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    if manifest.get("artifact_sha256") != artifact_sha256:
        raise ValueError("condition_authority_artifact_sha256_mismatch")
    assessment = _assessment_from_snapshot(report_bytes, artifact_sha256)
    if manifest.get("status") != assessment["status"]:
        raise ValueError("condition_authority_manifest_assessment_mismatch")
    if manifest.get("status") not in _CACHEABLE_STATUSES:
        raise ValueError("condition_authority_manifest_status_invalid")
    return _CachedAuthority(
        authority=_authority_from_directory(directory, manifest),
        report_bytes=report_bytes,
        artifact_bytes=artifact_bytes,
        assessment=assessment,
    )


def _write_and_validate_turn_binding(
    *,
    cached: _CachedAuthority,
    turn_root: Path,
    turn_id: str,
    turn_index: int,
    binding_key: str,
) -> ConditionAuthorityMaterialization:
    probe_root = _ensure_turn_probe_root(turn_root)
    artifact_path = probe_root / ABILITY_DETAILS_ARTIFACT
    report_path = probe_root / FORMAL_CONDITION_REPORT_ARTIFACT
    _write_or_match(artifact_path, cached.artifact_bytes)
    raw_report = _parse_strict_object(
        cached.report_bytes, "condition_authority_report_invalid_json"
    )
    try:
        raw_report = _extract_probe_report(raw_report, formal_live=False)
    except ValueError as exc:
        raise ValueError("condition_authority_report_invalid_json") from exc
    unsigned = {
        "is_valid": True,
        "status": "completed",
        "performed": True,
        "reason": "completed",
        "result": {"condition_report": raw_report},
    }
    signed = attach_formal_condition_binding(
        unsigned,
        turn_id=turn_id,
        turn_index=turn_index,
        artifact_path=artifact_path,
        binding_key=binding_key,
    )
    _write_or_match(report_path, _canonical_json(signed))
    load_explicit_condition_context(
        report_path,
        required_turn_root=turn_root,
        formal_live=True,
        formal_binding_key=binding_key,
        expected_turn_id=turn_id,
        expected_turn_index=turn_index,
    )
    return ConditionAuthorityMaterialization(report_path=report_path, artifact_path=artifact_path)


def _assessment_from_snapshot(
    report_bytes: bytes, artifact_sha256: str
) -> dict[str, object]:
    raw = _parse_strict_object(report_bytes, "condition_authority_report_invalid_json")
    try:
        report = _extract_probe_report(raw, formal_live=False)
        source_sha256 = _validate_assessment_report(report)
    except ValueError as exc:
        raise ValueError("condition_authority_report_invalid") from exc
    if source_sha256 != artifact_sha256:
        raise ValueError("condition_authority_artifact_sha256_mismatch")
    if report["abstained"]:
        status = "unknown"
        conditions: tuple[dict[str, Any], ...] = ()
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
            "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            "artifact_sha256": artifact_sha256,
        }
        conditions = tuple(
            {**copy.deepcopy(condition), "formal_context": copy.deepcopy(binding)}
            for condition in raw_conditions
        )
    warnings = report.get("warnings", [])
    if not isinstance(warnings, list):
        raise ValueError("condition_authority_report_invalid")
    return {
        "status": status,
        "conditions": conditions,
        "source_sha256": source_sha256,
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "artifact_sha256": artifact_sha256,
        "warnings": tuple(copy.deepcopy(warnings)),
    }


def _cache_root(dashboard_run_dir: Path) -> Path:
    return Path(dashboard_run_dir).absolute() / ".runtime" / "condition_authority"


def _ensure_cache_root(dashboard_run_dir: Path) -> Path:
    dashboard = Path(dashboard_run_dir).absolute()
    _ensure_directory(dashboard)
    runtime = dashboard / ".runtime"
    _ensure_directory(runtime)
    root = runtime / "condition_authority"
    _ensure_directory(root)
    return root


def _ensure_turn_probe_root(turn_root: Path) -> Path:
    turn = Path(turn_root).absolute()
    _ensure_directory(turn)
    probe = turn / "condition_probe"
    _ensure_directory(probe)
    return probe


def _ensure_directory(path: Path) -> None:
    _reject_existing_ancestor_reparse(path)
    _reject_reparse_component(path, path)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError("condition_authority_directory_create_failed") from exc
    _reject_reparse_component(path, path)
    _reject_existing_ancestor_reparse(path)
    if not path.is_dir():
        raise ValueError("condition_authority_directory_invalid")


def _authority_from_directory(directory: Path, manifest: Mapping[str, Any]) -> ConditionAuthority:
    return ConditionAuthority(
        authority_id=manifest["authority_id"],
        report_sha256=manifest["report_sha256"],
        artifact_sha256=manifest["artifact_sha256"],
        status=manifest["status"],
        report_path=directory / "probe.report.json",
        artifact_path=directory / ABILITY_DETAILS_ARTIFACT,
        manifest_path=directory / "manifest.json",
    )


def _read_untrusted_file_snapshot(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError("condition_authority_input_missing") from exc


def _read_boundary_file_snapshot(path: Path) -> bytes:
    if _is_link_or_reparse(path):
        raise ValueError("condition_authority_symlink")
    return path.read_bytes()


def _write_exclusive(path: Path, payload: bytes) -> None:
    if _is_link_or_reparse(path):
        raise ValueError("condition_authority_symlink")
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()


def _write_or_match(path: Path, payload: bytes) -> None:
    if _is_link_or_reparse(path):
        raise ValueError("condition_authority_symlink")
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        _write_exclusive(path, payload)
        return
    if existing != payload:
        raise ValueError("condition_authority_output_conflict")


def _parse_strict_object(payload: bytes, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_members,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(reason) from exc
    if not isinstance(value, dict):
        raise ValueError(reason)
    return value


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _reject_reparse_component(root: Path, target: Path) -> None:
    root = root.absolute()
    target = target.absolute()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError("condition_authority_path_outside_root") from exc
    current = root
    if _is_link_or_reparse(current):
        raise ValueError("condition_authority_symlink")
    for part in relative.parts:
        current = current / part
        if _is_link_or_reparse(current):
            raise ValueError("condition_authority_symlink")


def _reject_existing_ancestor_reparse(path: Path) -> None:
    """Treat any extant ancestor of a public output boundary as untrusted."""
    chain = list(reversed(Path(path).absolute().parents)) + [Path(path).absolute()]
    for component in chain:
        if _is_link_or_reparse(component):
            raise ValueError("condition_authority_symlink")


def _is_link_or_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attributes & _REPARSE_POINT_ATTRIBUTE)
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _reject_duplicate_json_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("condition_authority_duplicate_json_member")
        result[key] = value
    return result


def _reject_nonstandard_json_constant(value: str) -> object:
    raise ValueError(f"condition_authority_nonstandard_json_constant:{value}")
