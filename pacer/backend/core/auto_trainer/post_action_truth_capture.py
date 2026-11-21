from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping

import cv2
import numpy as np

from core.auto_trainer.next_run_truth_capture import (
    TruthCaptureError,
    _validate_plain_path_chain,
    load_next_run_truth_capture,
)


SCHEMA_VERSION = "uma.post_action_truth_capture.v1"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_TRAINING_ACTIONS = frozenset(
    {
        "training.speed",
        "training.stamina",
        "training.power",
        "training.guts",
        "training.wisdom",
    }
)
_AUTHORIZATION_KEYS = frozenset(
    {
        "label",
        "allowed",
        "reason",
        "action_id",
        "coordinate_name",
        "decision_packet_sha256",
        "advisor_request_sha256",
        "action_plan_sha256",
        "perception_verdict_sha256",
        "perception_frame_sha256",
        "execution_frame_sha256",
        "underlying_gate_reason",
        "authorization_sha256",
    }
)
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "binding",
        "authority",
        "action_receipt",
        "pre_action_evidence",
        "post_action_evidence",
        "fragment_count_proposal",
        "manifest_sha256",
    }
)


class PostActionTruthCaptureError(ValueError):
    pass


def archive_post_action_truth_capture(
    *,
    turn_root: Path,
    turn_entry: dict[str, Any],
    pre_capture: dict[str, Any],
    turn_id: str,
    turn_index: int,
) -> dict[str, Any]:
    """Archive immutable post-action evidence without deriving fragment labels."""

    try:
        root = _resolve_root(turn_root)
        inputs = _validated_inputs(
            root=root,
            turn_entry=turn_entry,
            pre_capture=pre_capture,
            turn_id=turn_id,
            turn_index=turn_index,
        )
        output_dir = root / "post_action_truth_capture"
        manifest_path = output_dir / "capture_manifest.json"
        frame_path = output_dir / "post_action_frame.png"
        manifest = _build_manifest(
            inputs=inputs,
            turn_id=turn_id,
            turn_index=turn_index,
        )
        if output_dir.exists():
            loaded = load_post_action_truth_capture(manifest_path)
            if loaded != manifest:
                raise PostActionTruthCaptureError(
                    "post_action_archive_binding_mismatch"
                )
            if frame_path.read_bytes() != inputs["post_frame_bytes"]:
                raise PostActionTruthCaptureError(
                    "post_action_archive_frame_mismatch"
                )
            return _available_result(root, manifest_path, reused=True)

        staging_dir = Path(
            tempfile.mkdtemp(
                prefix=".post_action_truth_capture.tmp-",
                dir=root,
            )
        )
        try:
            with (staging_dir / frame_path.name).open("xb") as stream:
                stream.write(inputs["post_frame_bytes"])
            payload = _canonical_json_bytes(manifest)
            with (staging_dir / manifest_path.name).open("xb") as stream:
                stream.write(payload)
            staging_dir.rename(output_dir)
        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
        loaded = load_post_action_truth_capture(manifest_path)
        if loaded != manifest:
            raise PostActionTruthCaptureError(
                "post_action_archive_write_mismatch"
            )
        return _available_result(root, manifest_path, reused=False)
    except (
        OSError,
        TruthCaptureError,
        PostActionTruthCaptureError,
        TypeError,
        ValueError,
    ) as exc:
        return {
            "status": "unavailable",
            "reason": str(exc) or type(exc).__name__,
            "manifest_path": None,
            "manifest_sha256": None,
            "reused": False,
            "action_eligible": False,
        }


def load_post_action_truth_capture(
    manifest_path: Path,
) -> dict[str, Any]:
    if not isinstance(manifest_path, Path):
        raise PostActionTruthCaptureError(
            "post_action_manifest_path_invalid"
        )
    try:
        manifest_path = manifest_path.resolve(strict=True)
        raw = manifest_path.read_bytes()
        manifest = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError()
            ),
        )
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise PostActionTruthCaptureError(
            "post_action_manifest_invalid"
        ) from exc
    if (
        manifest_path.name != "capture_manifest.json"
        or manifest_path.parent.name != "post_action_truth_capture"
        or type(manifest) is not dict
        or set(manifest) != _TOP_LEVEL_KEYS
        or raw != _canonical_json_bytes(manifest)
    ):
        raise PostActionTruthCaptureError(
            "post_action_manifest_invalid"
        )
    expected_hash = manifest.get("manifest_sha256")
    body = {
        key: value
        for key, value in manifest.items()
        if key != "manifest_sha256"
    }
    if not _is_sha(expected_hash) or expected_hash != _canonical_sha(body):
        raise PostActionTruthCaptureError(
            "post_action_manifest_sha_mismatch"
        )
    root = manifest_path.parent.parent
    _validate_loaded_manifest(
        manifest,
        root=root,
        manifest_path=manifest_path,
    )
    return manifest


def _validated_inputs(
    *,
    root: Path,
    turn_entry: dict[str, Any],
    pre_capture: dict[str, Any],
    turn_id: str,
    turn_index: int,
) -> dict[str, Any]:
    if (
        type(turn_entry) is not dict
        or type(turn_id) is not str
        or not turn_id
        or type(turn_index) is not int
        or turn_index < 0
        or turn_entry.get("turn_id") != turn_id
        or turn_entry.get("turn_index") != turn_index
    ):
        raise PostActionTruthCaptureError("turn_binding_invalid")
    pre_manifest_path, pre_manifest, pre_manifest_sha = (
        _load_pre_capture(
            root=root,
            pre_capture=pre_capture,
            turn_id=turn_id,
            turn_index=turn_index,
        )
    )
    authorization, execution, authorized_frame = (
        _execution_authorization(turn_entry)
    )
    if (
        authorization["perception_frame_sha256"]
        != pre_manifest["authority"]["source_frame_sha256"]
    ):
        raise PostActionTruthCaptureError(
            "pre_action_frame_binding_mismatch"
        )
    post_path, post_bytes, post_sha, captured_at = _post_frame(
        root=root,
        frame_record=authorized_frame,
    )
    if (
        post_sha
        == authorization["perception_frame_sha256"]
        or post_sha == authorization["execution_frame_sha256"]
    ):
        raise PostActionTruthCaptureError(
            "post_action_fresh_frame_required"
        )
    authority_frame = _authority_frame(
        root=root,
        pre_manifest_path=pre_manifest_path,
        pre_manifest=pre_manifest,
    )
    return {
        "binding": dict(pre_manifest["binding"]),
        "authority": dict(pre_manifest["authority"]),
        "authorization": authorization,
        "pre_manifest_path": pre_manifest_path,
        "pre_manifest_sha256": pre_manifest_sha,
        "authority_frame_path": authority_frame["path"],
        "authority_frame_sha256": authority_frame["sha256"],
        "post_source_path": post_path,
        "post_frame_bytes": post_bytes,
        "post_frame_sha256": post_sha,
        "post_captured_at": captured_at,
    }


def _load_pre_capture(
    *,
    root: Path,
    pre_capture: dict[str, Any],
    turn_id: str,
    turn_index: int,
) -> tuple[Path, dict[str, Any], str]:
    if (
        type(pre_capture) is not dict
        or pre_capture.get("status") != "available"
        or pre_capture.get("action_eligible") is not False
        or not _is_sha(pre_capture.get("manifest_sha256"))
    ):
        raise PostActionTruthCaptureError(
            "pre_action_truth_capture_unavailable"
        )
    relative = _relative_path(
        pre_capture.get("manifest_path"),
        "pre_action_manifest_path_invalid",
    )
    path = _existing_file(
        root,
        root / relative,
        "pre_action_manifest_path_invalid",
    )
    raw = path.read_bytes()
    raw_sha = hashlib.sha256(raw).hexdigest()
    if raw_sha != pre_capture["manifest_sha256"]:
        raise PostActionTruthCaptureError(
            "pre_action_manifest_sha_mismatch"
        )
    manifest = load_next_run_truth_capture(path)
    binding = manifest.get("binding")
    if (
        type(binding) is not dict
        or binding.get("turn_id") != turn_id
        or binding.get("global_action_turn") != turn_index
    ):
        raise PostActionTruthCaptureError(
            "pre_action_turn_binding_mismatch"
        )
    return path, manifest, raw_sha


def _execution_authorization(
    turn_entry: dict[str, Any],
) -> tuple[
    dict[str, Any],
    Mapping[str, object],
    dict[str, Any],
]:
    report = turn_entry.get("report")
    stages = report.get("stages") if type(report) is dict else None
    live_loop = stages.get("live_loop") if type(stages) is dict else None
    execution = (
        live_loop.get("execution")
        if type(live_loop) is dict
        else None
    )
    if (
        not isinstance(execution, Mapping)
        or execution.get("performed") is not True
        or execution.get("reason") != "executed"
    ):
        raise PostActionTruthCaptureError("execution_not_performed")
    safety_checks = execution.get("safety_checks")
    matches = (
        [
            item
            for item in safety_checks
            if type(item) is dict
            and item.get("label") == "structured_action_safety"
        ]
        if type(safety_checks) is list
        else []
    )
    if len(matches) != 1 or set(matches[0]) != _AUTHORIZATION_KEYS:
        raise PostActionTruthCaptureError(
            "structured_authorization_required"
        )
    authorization = dict(matches[0])
    if (
        authorization.get("allowed") is not True
        or authorization.get("reason")
        != "structured_execution_authorized"
    ):
        raise PostActionTruthCaptureError(
            "structured_authorization_not_allowed"
        )
    if authorization.get("action_id") not in _TRAINING_ACTIONS:
        raise PostActionTruthCaptureError("training_action_required")
    target = execution.get("target")
    expected_target = str(authorization["action_id"]).split(".", 1)[1]
    if target != expected_target:
        raise PostActionTruthCaptureError(
            "training_action_target_mismatch"
        )
    expected_coordinate = f"training_menu.{expected_target}_decide"
    if authorization.get("coordinate_name") != expected_coordinate:
        raise PostActionTruthCaptureError(
            "training_action_coordinate_mismatch"
        )
    tap_plan = execution.get("tap_plan")
    if (
        type(tap_plan) is not list
        or len(tap_plan) < 2
        or type(tap_plan[1]) is not dict
        or tap_plan[1].get("coordinate_name") != expected_coordinate
    ):
        raise PostActionTruthCaptureError(
            "training_action_tap_plan_mismatch"
        )
    for key in (
        "decision_packet_sha256",
        "advisor_request_sha256",
        "action_plan_sha256",
        "perception_verdict_sha256",
        "perception_frame_sha256",
        "execution_frame_sha256",
        "authorization_sha256",
    ):
        if not _is_sha(authorization.get(key)):
            raise PostActionTruthCaptureError(
                "structured_authorization_sha_invalid"
            )
    supplied = authorization["authorization_sha256"]
    material = {
        key: value
        for key, value in authorization.items()
        if key not in {"label", "authorization_sha256"}
    }
    if supplied != _canonical_sha(material):
        raise PostActionTruthCaptureError(
            "structured_authorization_sha_mismatch"
        )
    authorized_frame = execution.get("structured_post_action_frame")
    if (
        type(authorized_frame) is not dict
        or set(authorized_frame)
        != {
            "path",
            "sha256",
            "captured_at",
            "structured_action_safety",
        }
        or authorized_frame.get("structured_action_safety")
        != {
            key: value
            for key, value in authorization.items()
            if key != "label"
        }
    ):
        raise PostActionTruthCaptureError(
            "structured_post_action_frame_binding_invalid"
        )
    frame_identity = {
        key: authorized_frame[key]
        for key in ("path", "sha256", "captured_at")
    }
    fresh_frames = execution.get("fresh_frames")
    if (
        type(fresh_frames) is not list
        or sum(
            type(item) is dict and item == frame_identity
            for item in fresh_frames
        )
        != 1
    ):
        raise PostActionTruthCaptureError(
            "structured_post_action_frame_not_unique"
        )
    return authorization, execution, authorized_frame


def _post_frame(
    *,
    root: Path,
    frame_record: dict[str, Any],
) -> tuple[Path, bytes, str, float]:
    latest = {
        key: frame_record.get(key)
        for key in ("path", "sha256", "captured_at")
    }
    if (
        type(latest) is not dict
        or set(latest) != {"path", "sha256", "captured_at"}
        or not _is_sha(latest.get("sha256"))
    ):
        raise PostActionTruthCaptureError(
            "post_action_fresh_frame_invalid"
        )
    path_value = latest.get("path")
    if type(path_value) is not str or not path_value:
        raise PostActionTruthCaptureError(
            "post_action_fresh_frame_invalid"
        )
    candidate = Path(path_value)
    if not candidate.is_absolute():
        candidate = root / candidate
    path = _existing_file(
        root,
        candidate,
        "post_action_frame_path_invalid",
    )
    if path.suffix.lower() != ".png":
        raise PostActionTruthCaptureError(
            "post_action_frame_codec_invalid"
        )
    payload = path.read_bytes()
    actual_sha = hashlib.sha256(payload).hexdigest()
    if actual_sha != latest["sha256"]:
        raise PostActionTruthCaptureError(
            "post_action_frame_sha_mismatch"
        )
    image = cv2.imdecode(
        np.frombuffer(payload, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    if image is None or image.size == 0:
        raise PostActionTruthCaptureError(
            "post_action_frame_decode_failed"
        )
    captured_at = latest.get("captured_at")
    if (
        isinstance(captured_at, bool)
        or not isinstance(captured_at, (int, float))
        or not math.isfinite(float(captured_at))
        or float(captured_at) < 0
    ):
        raise PostActionTruthCaptureError(
            "post_action_capture_time_invalid"
        )
    return path, payload, actual_sha, float(captured_at)


def _authority_frame(
    *,
    root: Path,
    pre_manifest_path: Path,
    pre_manifest: dict[str, Any],
) -> dict[str, object]:
    matches = [
        item
        for item in pre_manifest.get("artifacts", [])
        if type(item) is dict
        and item.get("artifact_kind") == "authority_frame"
    ]
    if len(matches) != 1:
        raise PostActionTruthCaptureError(
            "pre_action_authority_frame_required"
        )
    item = matches[0]
    archive_relative = _relative_path(
        item.get("archive_path"),
        "pre_action_authority_frame_path_invalid",
    )
    path = _existing_file(
        root,
        pre_manifest_path.parent / archive_relative,
        "pre_action_authority_frame_path_invalid",
    )
    payload = path.read_bytes()
    sha = hashlib.sha256(payload).hexdigest()
    if (
        sha != item.get("raw_bytes_sha256")
        or sha != item.get("source_frame_sha256")
        or sha != pre_manifest["authority"]["source_frame_sha256"]
    ):
        raise PostActionTruthCaptureError(
            "pre_action_authority_frame_sha_mismatch"
        )
    return {"path": path, "sha256": sha}


def _build_manifest(
    *,
    inputs: dict[str, Any],
    turn_id: str,
    turn_index: int,
) -> dict[str, Any]:
    authorization = inputs["authorization"]
    root = inputs["pre_manifest_path"].parent.parent
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "binding": {
            **inputs["binding"],
            "turn_id": turn_id,
            "global_action_turn": turn_index,
        },
        "authority": inputs["authority"],
        "action_receipt": {
            "structured_authorization": authorization,
            "performed": True,
            "result_reason": "executed",
        },
        "pre_action_evidence": {
            "capture_manifest_path": inputs[
                "pre_manifest_path"
            ].relative_to(root).as_posix(),
            "capture_manifest_sha256": inputs[
                "pre_manifest_sha256"
            ],
            "authority_frame_path": inputs[
                "authority_frame_path"
            ].relative_to(root).as_posix(),
            "authority_frame_sha256": inputs[
                "authority_frame_sha256"
            ],
        },
        "post_action_evidence": {
            "archive_path": "post_action_frame.png",
            "source_frame_sha256": inputs["post_frame_sha256"],
            "captured_at": inputs["post_captured_at"],
            "page_state": "unclassified",
        },
        "fragment_count_proposal": {
            "status": "unavailable",
            "reason": "post_action_page_state_unclassified",
            "value": None,
            "annotation_status": "proposal",
            "eligible_for_training": False,
            "action_eligible": False,
            "production_ready": False,
        },
    }
    manifest["manifest_sha256"] = _canonical_sha(manifest)
    return manifest


def _validate_loaded_manifest(
    manifest: dict[str, Any],
    *,
    root: Path,
    manifest_path: Path,
) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise PostActionTruthCaptureError(
            "post_action_schema_invalid"
        )
    binding = manifest.get("binding")
    authority = manifest.get("authority")
    if (
        type(binding) is not dict
        or set(binding)
        != {
            "run_session_id",
            "generation",
            "global_action_turn",
            "turn_id",
        }
        or type(binding.get("run_session_id")) is not str
        or not binding["run_session_id"]
        or type(binding.get("generation")) is not int
        or binding["generation"] < 0
        or type(binding.get("global_action_turn")) is not int
        or binding["global_action_turn"] < 0
        or type(binding.get("turn_id")) is not str
        or not binding["turn_id"]
        or type(authority) is not dict
    ):
        raise PostActionTruthCaptureError(
            "post_action_binding_invalid"
        )
    action = manifest.get("action_receipt")
    if (
        type(action) is not dict
        or set(action)
        != {
            "structured_authorization",
            "performed",
            "result_reason",
        }
        or action.get("performed") is not True
        or action.get("result_reason") != "executed"
    ):
        raise PostActionTruthCaptureError(
            "post_action_receipt_invalid"
        )
    authorization = action.get("structured_authorization")
    _validate_loaded_authorization(authorization)
    pre = manifest.get("pre_action_evidence")
    if (
        type(pre) is not dict
        or set(pre)
        != {
            "capture_manifest_path",
            "capture_manifest_sha256",
            "authority_frame_path",
            "authority_frame_sha256",
        }
        or not _is_sha(pre.get("capture_manifest_sha256"))
        or not _is_sha(pre.get("authority_frame_sha256"))
    ):
        raise PostActionTruthCaptureError(
            "pre_action_evidence_invalid"
        )
    pre_manifest_path = _existing_file(
        root,
        root
        / _relative_path(
            pre["capture_manifest_path"],
            "pre_action_manifest_path_invalid",
        ),
        "pre_action_manifest_path_invalid",
    )
    if (
        hashlib.sha256(pre_manifest_path.read_bytes()).hexdigest()
        != pre["capture_manifest_sha256"]
    ):
        raise PostActionTruthCaptureError(
            "pre_action_manifest_sha_mismatch"
        )
    pre_manifest = load_next_run_truth_capture(pre_manifest_path)
    if (
        pre_manifest.get("binding") != binding
        or pre_manifest.get("authority") != authority
        or authority.get("source_frame_sha256")
        != authorization["perception_frame_sha256"]
    ):
        raise PostActionTruthCaptureError(
            "pre_action_manifest_binding_mismatch"
        )
    authority_frame_path = _existing_file(
        root,
        root
        / _relative_path(
            pre["authority_frame_path"],
            "pre_action_authority_frame_path_invalid",
        ),
        "pre_action_authority_frame_path_invalid",
    )
    if (
        hashlib.sha256(authority_frame_path.read_bytes()).hexdigest()
        != pre["authority_frame_sha256"]
        or pre["authority_frame_sha256"]
        != authority["source_frame_sha256"]
    ):
        raise PostActionTruthCaptureError(
            "pre_action_authority_frame_sha_mismatch"
        )
    post = manifest.get("post_action_evidence")
    if (
        type(post) is not dict
        or set(post)
        != {
            "archive_path",
            "source_frame_sha256",
            "captured_at",
            "page_state",
        }
        or post.get("archive_path") != "post_action_frame.png"
        or not _is_sha(post.get("source_frame_sha256"))
        or post.get("page_state") != "unclassified"
        or isinstance(post.get("captured_at"), bool)
        or not isinstance(post.get("captured_at"), (int, float))
        or not math.isfinite(float(post["captured_at"]))
        or float(post["captured_at"]) < 0
        or post["source_frame_sha256"]
        in {
            authorization["perception_frame_sha256"],
            authorization["execution_frame_sha256"],
        }
    ):
        raise PostActionTruthCaptureError(
            "post_action_evidence_invalid"
        )
    post_path = _existing_file(
        root,
        manifest_path.parent / post["archive_path"],
        "post_action_archive_frame_invalid",
    )
    post_bytes = post_path.read_bytes()
    if (
        hashlib.sha256(post_bytes).hexdigest()
        != post["source_frame_sha256"]
        or cv2.imdecode(
            np.frombuffer(post_bytes, dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        is None
    ):
        raise PostActionTruthCaptureError(
            "post_action_archive_frame_invalid"
        )
    proposal = manifest.get("fragment_count_proposal")
    if proposal != {
        "status": "unavailable",
        "reason": "post_action_page_state_unclassified",
        "value": None,
        "annotation_status": "proposal",
        "eligible_for_training": False,
        "action_eligible": False,
        "production_ready": False,
    }:
        raise PostActionTruthCaptureError(
            "fragment_count_proposal_invalid"
        )


def _validate_loaded_authorization(value: object) -> None:
    if (
        type(value) is not dict
        or set(value) != _AUTHORIZATION_KEYS
        or value.get("label") != "structured_action_safety"
        or value.get("allowed") is not True
        or value.get("reason")
        != "structured_execution_authorized"
        or value.get("action_id") not in _TRAINING_ACTIONS
    ):
        raise PostActionTruthCaptureError(
            "structured_authorization_invalid"
        )
    for key in (
        "decision_packet_sha256",
        "advisor_request_sha256",
        "action_plan_sha256",
        "perception_verdict_sha256",
        "perception_frame_sha256",
        "execution_frame_sha256",
        "authorization_sha256",
    ):
        if not _is_sha(value.get(key)):
            raise PostActionTruthCaptureError(
                "structured_authorization_sha_invalid"
            )
    supplied = value["authorization_sha256"]
    material = {
        key: item
        for key, item in value.items()
        if key not in {"label", "authorization_sha256"}
    }
    if supplied != _canonical_sha(material):
        raise PostActionTruthCaptureError(
            "structured_authorization_sha_mismatch"
        )


def _resolve_root(turn_root: Path) -> Path:
    if not isinstance(turn_root, Path):
        raise PostActionTruthCaptureError("turn_root_invalid")
    try:
        _validate_plain_path_chain(
            turn_root,
            turn_root,
            code="turn_root_reparse_point",
        )
        root = turn_root.resolve(strict=True)
    except (OSError, TruthCaptureError) as exc:
        raise PostActionTruthCaptureError("turn_root_invalid") from exc
    if not root.is_dir() or root.is_symlink():
        raise PostActionTruthCaptureError("turn_root_invalid")
    return root


def _existing_file(
    root: Path,
    candidate: Path,
    code: str,
) -> Path:
    try:
        _validate_plain_path_chain(root, candidate, code=code)
        path = candidate.resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError, TruthCaptureError) as exc:
        raise PostActionTruthCaptureError(code) from exc
    if not path.is_file() or path.is_symlink():
        raise PostActionTruthCaptureError(code)
    return path


def _relative_path(value: object, code: str) -> Path:
    if type(value) is not str or not value:
        raise PostActionTruthCaptureError(code)
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise PostActionTruthCaptureError(code)
    return path


def _available_result(
    root: Path,
    manifest_path: Path,
    *,
    reused: bool,
) -> dict[str, Any]:
    return {
        "status": "available",
        "reason": None,
        "manifest_path": manifest_path.relative_to(root).as_posix(),
        "manifest_sha256": hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest(),
        "reused": reused,
        "action_eligible": False,
    }


def _canonical_sha(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PostActionTruthCaptureError(
            "post_action_manifest_invalid"
        ) from exc


def _reject_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _is_sha(value: object) -> bool:
    return type(value) is str and _SHA_RE.fullmatch(value) is not None
