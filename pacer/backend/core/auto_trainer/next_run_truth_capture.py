from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from core.auto_trainer.career_supervisor_turn import TurnAuthorityBundle


SCHEMA_VERSION = "uma.next_run_truth_capture.v1"
TRAINING_TABS = ("speed", "stamina", "power", "guts", "wisdom")
GROUP_KINDS = {
    "authority_frame": "authority_frame",
    "training_pages": "training_frame",
    "event_pages": "event_frame",
    "support_avatar_proposals": "support_avatar_crop",
    "support_hint_proposals": "support_hint_crop",
    "support_bond_proposals": "support_bond_crop",
    "fragment_proposals": "fragment_crop",
}
FRAME_KINDS = frozenset({"authority_frame", "training_frame", "event_frame"})
CROP_KINDS = frozenset(GROUP_KINDS.values()) - FRAME_KINDS
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
_INPUT_ARTIFACT_KEYS = frozenset(
    {
        "artifact_id",
        "artifact_kind",
        "run_session_id",
        "generation",
        "global_action_turn",
        "turn_id",
        "authority_bundle_sha256",
        "source_path",
        "raw_bytes_sha256",
        "source_frame_sha256",
        "coordinate_space",
        "training_type",
        "bbox_xyxy",
        "annotation_status",
        "eligible_for_training",
        "action_eligible",
    }
)
_ARCHIVED_ARTIFACT_KEYS = _INPUT_ARTIFACT_KEYS | frozenset({"archive_path"})
_TOP_LEVEL_KEYS = frozenset(
    {"schema_version", "binding", "authority", "groups", "artifacts"}
)
_BINDING_KEYS = frozenset(
    {"run_session_id", "generation", "global_action_turn", "turn_id"}
)
_AUTHORITY_KEYS = frozenset(
    {
        "authority_bundle_sha256",
        "source_frame_sha256",
        "perception_packet_sha256",
        "condition_authority_sha256",
        "goal_ledger_sha256",
        "race_plan_sha256",
        "training_packet_sha256",
        "scenario_packet_sha256",
    }
)
_CODEC_SUFFIXES = {
    "png": frozenset({".png"}),
    "jpeg": frozenset({".jpg", ".jpeg"}),
}
_MAX_IMAGE_PIXELS = 32_000_000


class TruthCaptureError(ValueError):
    pass


def write_next_run_truth_capture(
    output_dir: Path,
    *,
    authority: object,
    turn_id: str,
    source_root: Path,
    artifact_groups: object,
) -> Path:
    if not isinstance(output_dir, Path) or not isinstance(source_root, Path):
        raise TruthCaptureError("capture_path_invalid")
    if output_dir.exists():
        raise TruthCaptureError("capture_output_exists")
    if not isinstance(authority, TurnAuthorityBundle):
        raise TruthCaptureError("turn_authority_invalid")
    if type(turn_id) is not str or not turn_id:
        raise TruthCaptureError("turn_id_invalid")
    if type(artifact_groups) is not dict or set(artifact_groups) != set(GROUP_KINDS):
        raise TruthCaptureError("artifact_groups_invalid")
    try:
        _validate_plain_path_chain(
            source_root,
            source_root,
            code="source_root_reparse_point",
        )
        root = source_root.resolve(strict=True)
    except OSError as exc:
        raise TruthCaptureError("source_root_invalid") from exc
    if not root.is_dir() or root.is_symlink():
        raise TruthCaptureError("source_root_invalid")

    binding = _binding(authority, turn_id)
    normalized_by_group: dict[str, list[dict[str, Any]]] = {}
    payload_by_id: dict[str, bytes] = {}
    seen_ids: set[str] = set()
    for group_name, expected_kind in GROUP_KINDS.items():
        raw_items = artifact_groups[group_name]
        if type(raw_items) is not list:
            raise TruthCaptureError("artifact_group_invalid")
        items: list[dict[str, Any]] = []
        for raw_item in raw_items:
            item, payload = _normalize_source_artifact(
                raw_item,
                expected_kind=expected_kind,
                binding=binding,
                authority_bundle_sha256=authority.bundle_sha256,
                source_root=root,
            )
            artifact_id = item["artifact_id"]
            if artifact_id in seen_ids:
                raise TruthCaptureError("artifact_id_duplicate")
            seen_ids.add(artifact_id)
            items.append(item)
            payload_by_id[artifact_id] = payload
        normalized_by_group[group_name] = items

    authority_frames = normalized_by_group["authority_frame"]
    if len(authority_frames) != 1:
        raise TruthCaptureError("authority_frame_required")
    if (
        authority_frames[0]["raw_bytes_sha256"]
        != authority.source_frame_sha256
    ):
        raise TruthCaptureError("authority_frame_sha_mismatch")

    training_pages = normalized_by_group["training_pages"]
    if len(training_pages) not in {0, len(TRAINING_TABS)}:
        raise TruthCaptureError("training_page_set_invalid")
    if training_pages:
        by_tab = {
            item["training_type"]: item
            for item in training_pages
            if item["training_type"] in TRAINING_TABS
        }
        if (
            len(by_tab) != len(TRAINING_TABS)
            or set(by_tab) != set(TRAINING_TABS)
        ):
            raise TruthCaptureError("training_page_set_invalid")
        normalized_by_group["training_pages"] = [
            by_tab[tab] for tab in TRAINING_TABS
        ]

    frames_by_sha: dict[str, list[dict[str, Any]]] = {}
    for group_name in ("authority_frame", "training_pages", "event_pages"):
        for item in normalized_by_group[group_name]:
            digest = item["raw_bytes_sha256"]
            frames_by_sha.setdefault(digest, []).append(item)
    for group_name in (
        "support_avatar_proposals",
        "support_hint_proposals",
        "support_bond_proposals",
        "fragment_proposals",
    ):
        for item in normalized_by_group[group_name]:
            parent = _matching_parent_frame(
                frames_by_sha.get(item["source_frame_sha256"], []),
                training_type=item["training_type"],
            )
            if parent is None:
                raise TruthCaptureError("artifact_parent_frame_missing")
    _validate_crop_pixel_integrity(
        normalized_by_group,
        payload_by_id=payload_by_id,
        path_field="source_path",
    )

    artifacts: list[dict[str, Any]] = []
    groups: dict[str, dict[str, Any]] = {}
    for group_name in GROUP_KINDS:
        group_items = normalized_by_group[group_name]
        artifact_ids: list[str] = []
        for item in group_items:
            suffix = Path(item["source_path"]).suffix.lower() or ".bin"
            archive_path = f"artifacts/{item['artifact_id']}{suffix}"
            artifacts.append({**item, "archive_path": archive_path})
            artifact_ids.append(item["artifact_id"])
        group: dict[str, Any] = {
            "status": "available" if group_items else "unavailable",
            "artifact_ids": artifact_ids,
        }
        if group_name == "training_pages":
            group["training_types"] = (
                list(TRAINING_TABS) if group_items else []
            )
        groups[group_name] = group

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "binding": binding,
        "authority": _authority_summary(authority),
        "groups": groups,
        "artifacts": artifacts,
    }
    encoded = _canonical_json_bytes(manifest)
    output_target = Path(os.path.abspath(os.fspath(output_dir)))
    if _path_lexists(output_target):
        raise TruthCaptureError("capture_output_exists")
    output_parent = _ensure_plain_directory(
        output_target.parent,
        code="capture_output_reparse_point",
    )
    staging: Path | None = None
    staging_prefix = f".{output_target.name}.truth-capture-v1-"
    try:
        staging = Path(
            tempfile.mkdtemp(
                prefix=staging_prefix,
                dir=output_parent,
            )
        )
        _validate_plain_path_chain(
            output_parent,
            staging,
            code="capture_output_reparse_point",
        )
        for artifact in artifacts:
            destination = staging / artifact["archive_path"]
            destination_parent = _ensure_plain_directory(
                destination.parent,
                code="capture_output_reparse_point",
                boundary=staging,
            )
            with destination.open("xb") as handle:
                handle.write(payload_by_id[artifact["artifact_id"]])
            _validate_plain_path_chain(
                staging,
                destination_parent,
                code="capture_output_reparse_point",
            )
            _validate_plain_path_chain(
                staging,
                destination,
                code="capture_output_reparse_point",
            )
        staged_manifest = staging / "capture_manifest.json"
        with staged_manifest.open("xb") as handle:
            handle.write(encoded)
        _validate_plain_path_chain(
            staging,
            staged_manifest,
            code="capture_output_reparse_point",
        )
        load_next_run_truth_capture(
            staged_manifest,
            expected_authority=authority,
            expected_turn_id=turn_id,
        )
        _validate_plain_path_chain(
            Path(output_parent.anchor),
            output_parent,
            code="capture_output_reparse_point",
        )
        if _path_lexists(output_target):
            raise TruthCaptureError("capture_output_exists")
        try:
            os.rename(staging, output_target)
        except OSError as exc:
            if _path_lexists(output_target):
                raise TruthCaptureError("capture_output_exists") from exc
            raise
        staging = None
        manifest_path = output_target / "capture_manifest.json"
        load_next_run_truth_capture(
            manifest_path,
            expected_authority=authority,
            expected_turn_id=turn_id,
        )
        return manifest_path
    finally:
        _cleanup_owned_capture_staging(
            output_parent=output_parent,
            staging=staging,
            expected_prefix=staging_prefix,
        )


def load_next_run_truth_capture(
    manifest_path: Path,
    *,
    expected_authority: object | None = None,
    expected_turn_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(manifest_path, Path):
        raise TruthCaptureError("capture_manifest_path_invalid")
    _validate_plain_path_chain(
        Path(Path(os.path.abspath(os.fspath(manifest_path))).anchor),
        manifest_path,
        code="capture_manifest_reparse_point",
    )
    if not manifest_path.is_file():
        raise TruthCaptureError("capture_manifest_unreadable")
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise TruthCaptureError("capture_manifest_invalid") from exc
    if type(manifest) is not dict or raw != _canonical_json_bytes(manifest):
        raise TruthCaptureError("capture_manifest_invalid")
    _validate_archived_manifest(
        manifest,
        manifest_root=manifest_path.parent,
        expected_authority=expected_authority,
        expected_turn_id=expected_turn_id,
    )
    return manifest


def _binding(
    authority: TurnAuthorityBundle,
    turn_id: str,
) -> dict[str, Any]:
    return {
        "run_session_id": authority.run_session_id,
        "generation": authority.generation,
        "global_action_turn": authority.global_action_turn,
        "turn_id": turn_id,
    }


def _authority_summary(authority: TurnAuthorityBundle) -> dict[str, Any]:
    return {
        "authority_bundle_sha256": authority.bundle_sha256,
        "source_frame_sha256": authority.source_frame_sha256,
        "perception_packet_sha256": authority.perception_packet_sha256,
        "condition_authority_sha256": authority.condition_authority_sha256,
        "goal_ledger_sha256": authority.goal_ledger_sha256,
        "race_plan_sha256": authority.race_plan_sha256,
        "training_packet_sha256": authority.training_packet_sha256,
        "scenario_packet_sha256": authority.scenario_packet_sha256,
    }


def _normalize_source_artifact(
    raw_item: object,
    *,
    expected_kind: str,
    binding: dict[str, Any],
    authority_bundle_sha256: str,
    source_root: Path,
) -> tuple[dict[str, Any], bytes]:
    if type(raw_item) is not dict or set(raw_item) != _INPUT_ARTIFACT_KEYS:
        raise TruthCaptureError("artifact_schema_invalid")
    item = dict(raw_item)
    if item.get("artifact_kind") != expected_kind:
        raise TruthCaptureError("artifact_kind_invalid")
    artifact_id = item.get("artifact_id")
    if type(artifact_id) is not str or _ARTIFACT_ID_RE.fullmatch(artifact_id) is None:
        raise TruthCaptureError("artifact_id_invalid")
    if any(item.get(key) != value for key, value in binding.items()):
        raise TruthCaptureError("artifact_turn_binding_mismatch")
    if item.get("authority_bundle_sha256") != authority_bundle_sha256:
        raise TruthCaptureError("artifact_authority_binding_mismatch")
    _validate_non_actionable(item)
    _validate_coordinate_and_bbox(item)
    source_relative = _relative_artifact_path(item.get("source_path"))
    source_candidate = source_root / source_relative
    _validate_plain_path_chain(
        source_root,
        source_candidate,
        code="artifact_source_reparse_point",
    )
    try:
        source = source_candidate.resolve(strict=True)
        source.relative_to(source_root)
    except (OSError, ValueError) as exc:
        raise TruthCaptureError("artifact_source_path_invalid") from exc
    if not source.is_file() or source.is_symlink():
        raise TruthCaptureError("artifact_source_path_invalid")
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise TruthCaptureError("artifact_source_unreadable") from exc
    raw_sha = item.get("raw_bytes_sha256")
    source_sha = item.get("source_frame_sha256")
    if not _is_sha(raw_sha) or hashlib.sha256(payload).hexdigest() != raw_sha:
        raise TruthCaptureError("artifact_raw_sha_mismatch")
    if not _is_sha(source_sha):
        raise TruthCaptureError("artifact_source_frame_sha_invalid")
    if expected_kind in FRAME_KINDS and source_sha != raw_sha:
        raise TruthCaptureError("artifact_source_frame_sha_mismatch")
    item["source_path"] = source_relative.as_posix()
    return item, payload


def _validate_non_actionable(item: dict[str, Any]) -> None:
    if (
        item.get("annotation_status") != "proposal"
        or item.get("eligible_for_training") is not False
        or item.get("action_eligible") is not False
    ):
        raise TruthCaptureError("artifact_actionability_invalid")


def _validate_coordinate_and_bbox(item: dict[str, Any]) -> None:
    coordinate_space = item.get("coordinate_space")
    if (
        type(coordinate_space) is not str
        or not coordinate_space
        or len(coordinate_space) > 96
    ):
        raise TruthCaptureError("artifact_coordinate_space_invalid")
    kind = item["artifact_kind"]
    training_type = item.get("training_type")
    bbox = item.get("bbox_xyxy")
    if kind == "training_frame":
        if training_type not in TRAINING_TABS or bbox is not None:
            raise TruthCaptureError("training_artifact_geometry_invalid")
    elif kind in {"authority_frame", "event_frame"}:
        if training_type is not None or bbox is not None:
            raise TruthCaptureError("frame_artifact_geometry_invalid")
    else:
        if training_type not in TRAINING_TABS:
            raise TruthCaptureError("crop_training_type_invalid")
        if (
            type(bbox) is not list
            or len(bbox) != 4
            or any(type(value) is not int or value < 0 for value in bbox)
            or bbox[0] >= bbox[2]
            or bbox[1] >= bbox[3]
        ):
            raise TruthCaptureError("artifact_bbox_invalid")


def _relative_artifact_path(value: object) -> Path:
    if type(value) is not str or not value or "\\" in value:
        raise TruthCaptureError("artifact_source_path_invalid")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise TruthCaptureError("artifact_source_path_invalid")
    return path


def _validate_archived_manifest(
    manifest: dict[str, Any],
    *,
    manifest_root: Path,
    expected_authority: object | None,
    expected_turn_id: str | None,
) -> None:
    if (
        set(manifest) != _TOP_LEVEL_KEYS
        or manifest.get("schema_version") != SCHEMA_VERSION
    ):
        raise TruthCaptureError("capture_manifest_schema_invalid")
    binding = manifest.get("binding")
    authority_summary = manifest.get("authority")
    groups = manifest.get("groups")
    artifacts = manifest.get("artifacts")
    if type(binding) is not dict or set(binding) != _BINDING_KEYS:
        raise TruthCaptureError("capture_binding_invalid")
    if (
        type(binding.get("run_session_id")) is not str
        or not binding["run_session_id"]
        or type(binding.get("generation")) is not int
        or binding["generation"] <= 0
        or type(binding.get("global_action_turn")) is not int
        or binding["global_action_turn"] < 0
        or type(binding.get("turn_id")) is not str
        or not binding["turn_id"]
    ):
        raise TruthCaptureError("capture_binding_invalid")
    if type(authority_summary) is not dict or set(authority_summary) != _AUTHORITY_KEYS:
        raise TruthCaptureError("capture_authority_invalid")
    if any(
        not _is_sha(authority_summary.get(key))
        for key in (
            "authority_bundle_sha256",
            "source_frame_sha256",
            "perception_packet_sha256",
        )
    ):
        raise TruthCaptureError("capture_authority_invalid")
    for key, value in authority_summary.items():
        if key.endswith("_sha256") and value is not None and not _is_sha(value):
            raise TruthCaptureError("capture_authority_invalid")
    if expected_turn_id is not None and binding["turn_id"] != expected_turn_id:
        raise TruthCaptureError("capture_turn_id_mismatch")
    if expected_authority is not None:
        if not isinstance(expected_authority, TurnAuthorityBundle):
            raise TruthCaptureError("expected_authority_invalid")
        if binding != _binding(expected_authority, binding["turn_id"]):
            raise TruthCaptureError("capture_authority_binding_mismatch")
        if authority_summary != _authority_summary(expected_authority):
            raise TruthCaptureError("capture_authority_binding_mismatch")
    if type(groups) is not dict or set(groups) != set(GROUP_KINDS):
        raise TruthCaptureError("capture_groups_invalid")
    if type(artifacts) is not list:
        raise TruthCaptureError("capture_artifacts_invalid")
    by_id: dict[str, dict[str, Any]] = {}
    frames_by_sha: dict[str, list[dict[str, Any]]] = {}
    payload_by_id: dict[str, bytes] = {}
    root = manifest_root.resolve(strict=True)
    for artifact in artifacts:
        if type(artifact) is not dict or set(artifact) != _ARCHIVED_ARTIFACT_KEYS:
            raise TruthCaptureError("archived_artifact_schema_invalid")
        artifact_id = artifact.get("artifact_id")
        if (
            type(artifact_id) is not str
            or _ARTIFACT_ID_RE.fullmatch(artifact_id) is None
            or artifact_id in by_id
        ):
            raise TruthCaptureError("archived_artifact_id_invalid")
        if any(artifact.get(key) != value for key, value in binding.items()):
            raise TruthCaptureError("archived_artifact_turn_binding_mismatch")
        if (
            artifact.get("authority_bundle_sha256")
            != authority_summary["authority_bundle_sha256"]
            or not _is_sha(artifact.get("authority_bundle_sha256"))
        ):
            raise TruthCaptureError("archived_artifact_authority_mismatch")
        _validate_non_actionable(artifact)
        _validate_coordinate_and_bbox(artifact)
        _relative_artifact_path(artifact.get("source_path"))
        if not _is_sha(artifact.get("raw_bytes_sha256")) or not _is_sha(
            artifact.get("source_frame_sha256")
        ):
            raise TruthCaptureError("archived_artifact_sha_invalid")
        archive_relative = _relative_artifact_path(artifact.get("archive_path"))
        archive_candidate = root / archive_relative
        _validate_plain_path_chain(
            root,
            archive_candidate,
            code="archived_artifact_reparse_point",
        )
        try:
            archived = archive_candidate.resolve(strict=True)
            archived.relative_to(root)
        except (OSError, ValueError) as exc:
            raise TruthCaptureError("archived_artifact_path_invalid") from exc
        if not archived.is_file() or archived.is_symlink():
            raise TruthCaptureError("archived_artifact_path_invalid")
        try:
            archived_payload = archived.read_bytes()
            actual_sha = hashlib.sha256(archived_payload).hexdigest()
        except OSError as exc:
            raise TruthCaptureError("archived_artifact_unreadable") from exc
        if actual_sha != artifact["raw_bytes_sha256"]:
            raise TruthCaptureError("archived_artifact_sha_mismatch")
        if (
            artifact["artifact_kind"] in FRAME_KINDS
            and artifact["source_frame_sha256"]
            != artifact["raw_bytes_sha256"]
        ):
            raise TruthCaptureError("archived_source_frame_sha_mismatch")
        by_id[artifact_id] = artifact
        payload_by_id[artifact_id] = archived_payload
        if artifact["artifact_kind"] in FRAME_KINDS:
            frames_by_sha.setdefault(
                artifact["raw_bytes_sha256"],
                [],
            ).append(artifact)

    for group_name, expected_kind in GROUP_KINDS.items():
        group = groups[group_name]
        expected_keys = (
            {"status", "artifact_ids", "training_types"}
            if group_name == "training_pages"
            else {"status", "artifact_ids"}
        )
        if type(group) is not dict or set(group) != expected_keys:
            raise TruthCaptureError("capture_group_schema_invalid")
        ids = group.get("artifact_ids")
        if (
            type(ids) is not list
            or len(ids) != len(set(ids))
            or any(
                type(item_id) is not str
                or item_id not in by_id
                or by_id[item_id]["artifact_kind"] != expected_kind
                for item_id in ids
            )
        ):
            raise TruthCaptureError("capture_group_members_invalid")
        expected_status = "available" if ids else "unavailable"
        if group.get("status") != expected_status:
            raise TruthCaptureError("capture_group_status_invalid")
        if group_name == "authority_frame" and len(ids) != 1:
            raise TruthCaptureError("authority_frame_required")
        if group_name == "training_pages":
            training_types = group.get("training_types")
            if len(ids) == 0:
                if training_types != []:
                    raise TruthCaptureError("training_page_set_invalid")
            elif (
                len(ids) != len(TRAINING_TABS)
                or training_types != list(TRAINING_TABS)
                or [by_id[item_id]["training_type"] for item_id in ids]
                != list(TRAINING_TABS)
            ):
                raise TruthCaptureError("training_page_set_invalid")
    grouped_ids = [
        item_id
        for group_name in GROUP_KINDS
        for item_id in groups[group_name]["artifact_ids"]
    ]
    if len(grouped_ids) != len(artifacts) or set(grouped_ids) != set(by_id):
        raise TruthCaptureError("capture_group_coverage_invalid")
    authority_id = groups["authority_frame"]["artifact_ids"][0]
    if (
        by_id[authority_id]["raw_bytes_sha256"]
        != authority_summary["source_frame_sha256"]
    ):
        raise TruthCaptureError("authority_frame_sha_mismatch")
    for artifact in artifacts:
        if artifact["artifact_kind"] not in CROP_KINDS:
            continue
        parent = _matching_parent_frame(
            frames_by_sha.get(artifact["source_frame_sha256"], []),
            training_type=artifact["training_type"],
        )
        if parent is None:
            raise TruthCaptureError("artifact_parent_frame_missing")
    grouped_artifacts = {
        group_name: [by_id[item_id] for item_id in groups[group_name]["artifact_ids"]]
        for group_name in GROUP_KINDS
    }
    _validate_crop_pixel_integrity(
        grouped_artifacts,
        payload_by_id=payload_by_id,
        path_field="archive_path",
    )


def _validate_plain_path_chain(
    root: Path,
    target: Path,
    *,
    code: str,
) -> None:
    """Reject every symlink/junction/reparse component from root through target."""

    lexical_root = Path(os.path.abspath(os.fspath(root)))
    lexical_target = Path(os.path.abspath(os.fspath(target)))
    try:
        relative = lexical_target.relative_to(lexical_root)
    except ValueError as exc:
        raise TruthCaptureError(code) from exc
    current = lexical_root
    components = [current]
    for part in relative.parts:
        current = current / part
        components.append(current)
    for component in components:
        try:
            metadata = component.lstat()
        except OSError as exc:
            raise TruthCaptureError(code) from exc
        attributes = getattr(metadata, "st_file_attributes", 0)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or component.is_symlink()
            or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise TruthCaptureError(code)


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _ensure_plain_directory(
    path: Path,
    *,
    code: str,
    boundary: Path | None = None,
) -> Path:
    directory = Path(os.path.abspath(os.fspath(path)))
    lexical_boundary = (
        Path(os.path.abspath(os.fspath(boundary)))
        if boundary is not None
        else Path(directory.anchor)
    )
    try:
        directory.relative_to(lexical_boundary)
    except ValueError as exc:
        raise TruthCaptureError(code) from exc

    missing: list[Path] = []
    current = directory
    while not _path_lexists(current):
        if current == lexical_boundary or current.parent == current:
            raise TruthCaptureError(code)
        missing.append(current)
        current = current.parent
    _validate_plain_path_chain(lexical_boundary, current, code=code)
    if not current.is_dir():
        raise TruthCaptureError(code)

    for component in reversed(missing):
        try:
            component.mkdir()
        except FileExistsError:
            pass
        _validate_plain_path_chain(
            lexical_boundary,
            component,
            code=code,
        )
        if not component.is_dir():
            raise TruthCaptureError(code)
    return directory


def _cleanup_owned_capture_staging(
    *,
    output_parent: Path,
    staging: Path | None,
    expected_prefix: str,
) -> None:
    if staging is None:
        return
    try:
        if (
            Path(os.path.abspath(os.fspath(staging.parent))) != output_parent
            or not staging.name.startswith(expected_prefix)
            or staging.is_symlink()
        ):
            return
        shutil.rmtree(staging)
    except OSError:
        return


def _matching_parent_frame(
    candidates: list[dict[str, Any]],
    *,
    training_type: str | None,
) -> dict[str, Any] | None:
    for candidate in candidates:
        if (
            training_type is None
            or candidate.get("training_type") == training_type
        ):
            return candidate
    return None


def _validate_crop_pixel_integrity(
    artifacts_by_group: dict[str, list[dict[str, Any]]],
    *,
    payload_by_id: dict[str, bytes],
    path_field: str,
) -> None:
    """Validate crop proposals against decoded parent pixels.

    OpenCV decodes every image with IMREAD_UNCHANGED, then normalizes uint8
    grayscale/BGR/BGRA into uint8 RGBA.  The actual payload magic must identify
    a supported PNG/JPEG codec and must agree with the path suffix.  File
    metadata and encoder byte layout are otherwise ignored, but the alpha
    channel remains part of pixel content.  Lossless formats require exact
    RGBA equality.  If either real payload is JPEG,
    alpha must still match exactly and a conservative RGB luminance and
    blurred-RGB tolerance accounts for JPEG re-encoding while remaining
    fail-closed for displaced or unrelated pixels.
    """

    frames = [
        artifact
        for group_name in ("authority_frame", "training_pages", "event_pages")
        for artifact in artifacts_by_group[group_name]
    ]
    decoded_by_artifact_id: dict[str, tuple[np.ndarray, str]] = {}
    frames_by_sha: dict[str, list[dict[str, Any]]] = {}
    for frame in frames:
        decoded_by_artifact_id[frame["artifact_id"]] = _decode_rgba(
            payload_by_id[frame["artifact_id"]],
            path=frame[path_field],
            code="artifact_parent_image_decode_failed",
        )
        frames_by_sha.setdefault(frame["raw_bytes_sha256"], []).append(frame)

    for group_name in (
        "support_avatar_proposals",
        "support_hint_proposals",
        "support_bond_proposals",
        "fragment_proposals",
    ):
        for crop in artifacts_by_group[group_name]:
            parent = _matching_parent_frame(
                frames_by_sha.get(crop["source_frame_sha256"], []),
                training_type=crop["training_type"],
            )
            if parent is None:
                raise TruthCaptureError("artifact_parent_frame_missing")
            parent_pixels, parent_codec = decoded_by_artifact_id[
                parent["artifact_id"]
            ]
            crop_pixels, crop_codec = _decode_rgba(
                payload_by_id[crop["artifact_id"]],
                path=crop[path_field],
                code="artifact_image_decode_failed",
            )
            x1, y1, x2, y2 = crop["bbox_xyxy"]
            height, width = parent_pixels.shape[:2]
            if x2 > width or y2 > height:
                raise TruthCaptureError("artifact_bbox_out_of_bounds")
            if crop_pixels.shape[:2] != (y2 - y1, x2 - x1):
                raise TruthCaptureError("artifact_crop_dimensions_mismatch")
            expected = parent_pixels[y1:y2, x1:x2]
            if not _normalized_pixels_match(
                crop_pixels,
                expected,
                lossy=(crop_codec == "jpeg" or parent_codec == "jpeg"),
            ):
                raise TruthCaptureError("artifact_crop_pixels_mismatch")


def _decode_rgba(
    payload: bytes,
    *,
    path: str,
    code: str,
) -> tuple[np.ndarray, str]:
    codec = _payload_codec(payload)
    if codec is None:
        raise TruthCaptureError("artifact_codec_unsupported")
    suffix = Path(path).suffix.lower()
    if suffix not in _CODEC_SUFFIXES[codec]:
        raise TruthCaptureError("artifact_codec_extension_mismatch")
    try:
        encoded = np.frombuffer(payload, dtype=np.uint8)
        decoded = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    except (cv2.error, ValueError):
        decoded = None
    if (
        decoded is None
        or decoded.dtype != np.uint8
        or decoded.shape[0] <= 0
        or decoded.shape[1] <= 0
        or decoded.shape[0] * decoded.shape[1] > _MAX_IMAGE_PIXELS
    ):
        raise TruthCaptureError(code)
    if decoded.ndim == 2:
        rgb = cv2.cvtColor(decoded, cv2.COLOR_GRAY2RGB)
        alpha = np.full((*decoded.shape, 1), 255, dtype=np.uint8)
        return np.concatenate((rgb, alpha), axis=2), codec
    if decoded.ndim != 3:
        raise TruthCaptureError(code)
    if decoded.shape[2] == 1:
        gray = decoded[:, :, 0]
        rgb = cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
        alpha = np.full((*gray.shape, 1), 255, dtype=np.uint8)
        return np.concatenate((rgb, alpha), axis=2), codec
    if decoded.shape[2] == 3:
        return cv2.cvtColor(decoded, cv2.COLOR_BGR2RGBA), codec
    if decoded.shape[2] == 4:
        return cv2.cvtColor(decoded, cv2.COLOR_BGRA2RGBA), codec
    raise TruthCaptureError(code)


def _payload_codec(payload: bytes) -> str | None:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    return None


def _normalized_pixels_match(
    actual: np.ndarray,
    expected: np.ndarray,
    *,
    lossy: bool,
) -> bool:
    if actual.shape != expected.shape:
        return False
    if not lossy:
        return bool(np.array_equal(actual, expected))
    if not np.array_equal(actual[:, :, 3], expected[:, :, 3]):
        return False
    actual_rgb = actual[:, :, :3]
    expected_rgb = expected[:, :, :3]
    actual_gray = cv2.cvtColor(actual_rgb, cv2.COLOR_RGB2GRAY)
    expected_gray = cv2.cvtColor(expected_rgb, cv2.COLOR_RGB2GRAY)
    luminance_delta = np.abs(
        actual_gray.astype(np.int16) - expected_gray.astype(np.int16)
    )
    if (
        float(luminance_delta.mean()) > 4.0
        or float(np.quantile(luminance_delta, 0.99)) > 16.0
    ):
        return False
    if float(actual_gray.std()) >= 2.0 or float(expected_gray.std()) >= 2.0:
        correlation = float(
            cv2.matchTemplate(
                actual_gray,
                expected_gray,
                cv2.TM_CCOEFF_NORMED,
            )[0, 0]
        )
        if not np.isfinite(correlation) or correlation < 0.995:
            return False
    blurred_actual = cv2.GaussianBlur(actual_rgb, (5, 5), 0)
    blurred_expected = cv2.GaussianBlur(expected_rgb, (5, 5), 0)
    rgb_delta = np.abs(
        blurred_actual.astype(np.int16) - blurred_expected.astype(np.int16)
    )
    return (
        float(rgb_delta.mean()) <= 8.0
        and float(np.quantile(rgb_delta, 0.99)) <= 32.0
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_key")
        value[key] = item
    return value


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TruthCaptureError("capture_json_invalid") from exc


def _is_sha(value: object) -> bool:
    return type(value) is str and _SHA_RE.fullmatch(value) is not None
