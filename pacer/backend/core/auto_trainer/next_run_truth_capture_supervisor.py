from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from core.auto_trainer.career_supervisor_turn import TurnAuthorityBundle
from core.auto_trainer.next_run_truth_capture import (
    TRAINING_TABS,
    TruthCaptureError,
    _validate_plain_path_chain,
    load_next_run_truth_capture,
    write_next_run_truth_capture,
)
from core.grand_masters_fragment_recognizer import (
    recognize_grand_masters_training_fragment,
)
from core.support_slot_recognizer import SupportSlotRecognizer


_AUTHORITY_KEYS = frozenset(
    {
        "schema_version",
        "run_session_id",
        "generation",
        "global_action_turn",
        "game_period",
        "source_frame_sha256",
        "perception_packet_sha256",
        "condition_authority_sha256",
        "goal_ledger_sha256",
        "race_plan_sha256",
        "training_packet_sha256",
        "scenario_packet_sha256",
        "unknown_fields",
        "hazard_flags",
        "bundle_sha256",
    }
)
_EVENT_CAPTURE_KEYS = frozenset(
    {
        "schema_version",
        "run_session_id",
        "generation",
        "global_action_turn",
        "game_period",
        "page_state",
        "source_artifact_path",
        "source_frame_sha256",
        "capture_ttl_turns",
        "expires_after_global_action_turn",
        "capture_metadata_sha256",
    }
)


class TruthCaptureSupervisorError(ValueError):
    pass


def archive_supervised_turn_truth_capture(
    *,
    turn_root: Path,
    turn_entry: dict[str, Any],
    turn_id: str,
    turn_index: int,
) -> dict[str, Any]:
    """Best-effort, non-actionable evidence archive for one supervised turn."""

    try:
        root = _resolve_turn_root(turn_root)
        authority = _authority_from_turn_entry(
            turn_entry,
            turn_id=turn_id,
            turn_index=turn_index,
        )
        training_pages = _training_pages_from_turn_entry(
            root,
            turn_entry,
            authority=authority,
        )
        event_pages = _event_pages_from_turn_entry(
            root,
            turn_entry,
            authority=authority,
        )
        output_dir = root / "truth_capture"
        manifest_path = output_dir / "capture_manifest.json"
        if output_dir.exists():
            if output_dir.is_symlink():
                raise TruthCaptureSupervisorError("capture_output_invalid")
            manifest = load_next_run_truth_capture(
                manifest_path,
                expected_authority=authority,
                expected_turn_id=turn_id,
            )
            _validate_existing_training_closure(manifest)
            return _available_result(
                root,
                manifest_path,
                reused=True,
            )
        groups = {
            "authority_frame": [
                _frame_artifact(
                    artifact_id="authority-frame",
                    artifact_kind="authority_frame",
                    path=training_pages["speed"],
                    root=root,
                    authority=authority,
                    turn_id=turn_id,
                    training_type=None,
                    coordinate_space="raw_720",
                )
            ],
            "training_pages": [
                _frame_artifact(
                    artifact_id=f"training-{tab}",
                    artifact_kind="training_frame",
                    path=training_pages[tab],
                    root=root,
                    authority=authority,
                    turn_id=turn_id,
                    training_type=tab,
                    coordinate_space="raw_720",
                )
                for tab in TRAINING_TABS
            ],
            "event_pages": [
                _frame_artifact(
                    artifact_id=f"event-{index:03d}",
                    artifact_kind="event_frame",
                    path=path,
                    root=root,
                    authority=authority,
                    turn_id=turn_id,
                    training_type=None,
                    coordinate_space="event_route_source_frame",
                )
                for index, path in enumerate(event_pages, start=1)
            ],
            "support_avatar_proposals": [],
            "support_hint_proposals": [],
            "support_bond_proposals": [],
            "fragment_proposals": [],
        }
        proposal_root: Path | None = None
        try:
            proposal_groups, proposal_root = _build_truth_crop_proposals(
                root=root,
                training_pages=training_pages,
                authority=authority,
                turn_id=turn_id,
            )
            groups.update(proposal_groups)
            manifest_path = write_next_run_truth_capture(
                output_dir,
                authority=authority,
                turn_id=turn_id,
                source_root=root,
                artifact_groups=groups,
            )
            manifest = load_next_run_truth_capture(
                manifest_path,
                expected_authority=authority,
                expected_turn_id=turn_id,
            )
            _validate_existing_training_closure(manifest)
            return _available_result(root, manifest_path, reused=False)
        finally:
            _cleanup_owned_proposal_root(root, proposal_root)
    except (
        OSError,
        TruthCaptureError,
        TruthCaptureSupervisorError,
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


def _resolve_turn_root(turn_root: Path) -> Path:
    if not isinstance(turn_root, Path):
        raise TruthCaptureSupervisorError("turn_root_invalid")
    try:
        _validate_plain_path_chain(
            turn_root,
            turn_root,
            code="turn_root_reparse_point",
        )
        root = turn_root.resolve(strict=True)
    except (OSError, TruthCaptureError) as exc:
        raise TruthCaptureSupervisorError("turn_root_invalid") from exc
    if not root.is_dir():
        raise TruthCaptureSupervisorError("turn_root_invalid")
    return root


def _authority_from_turn_entry(
    turn_entry: dict[str, Any],
    *,
    turn_id: str,
    turn_index: int,
) -> TurnAuthorityBundle:
    if (
        type(turn_entry) is not dict
        or type(turn_id) is not str
        or not turn_id
        or type(turn_index) is not int
        or turn_index < 0
    ):
        raise TruthCaptureSupervisorError("turn_binding_invalid")
    report = turn_entry.get("report")
    stages = report.get("stages") if type(report) is dict else None
    stage = (
        stages.get("perception_reliability")
        if type(stages) is dict
        else None
    )
    payload = stage.get("turn_authority") if type(stage) is dict else None
    if type(payload) is not dict or set(payload) != _AUTHORITY_KEYS:
        raise TruthCaptureSupervisorError("turn_authority_unavailable")
    if type(payload.get("unknown_fields")) is not list or type(
        payload.get("hazard_flags")
    ) is not list:
        raise TruthCaptureSupervisorError("turn_authority_invalid")
    try:
        authority = TurnAuthorityBundle(
            **{
                **payload,
                "unknown_fields": tuple(payload["unknown_fields"]),
                "hazard_flags": tuple(payload["hazard_flags"]),
            }
        )
    except (TypeError, ValueError) as exc:
        raise TruthCaptureSupervisorError("turn_authority_invalid") from exc
    if (
        authority.global_action_turn != turn_index
        or not turn_id.startswith(f"{authority.run_session_id}_turn_")
        or stage.get("authority_bundle_sha256") != authority.bundle_sha256
    ):
        raise TruthCaptureSupervisorError("turn_authority_binding_mismatch")
    return authority


def _training_pages_from_turn_entry(
    root: Path,
    turn_entry: dict[str, Any],
    *,
    authority: TurnAuthorityBundle,
) -> dict[str, Path]:
    report = turn_entry["report"]
    stages = report["stages"]
    capture = stages.get("capture")
    output_root = capture.get("output_root") if type(capture) is dict else None
    if type(output_root) is not str or not output_root:
        raise TruthCaptureSupervisorError("training_capture_unavailable")
    capture_root = _contained_path(root, Path(output_root), directory=True)
    raw_dir = _contained_path(root, capture_root / "raw_720", directory=True)
    pages: dict[str, Path] = {}
    for tab in TRAINING_TABS:
        page = _contained_path(root, raw_dir / f"{tab}.png", directory=False)
        pages[tab] = page
    if hashlib.sha256(pages["speed"].read_bytes()).hexdigest() != (
        authority.source_frame_sha256
    ):
        raise TruthCaptureSupervisorError("authority_frame_sha_mismatch")
    return pages


def _event_pages_from_turn_entry(
    root: Path,
    turn_entry: dict[str, Any],
    *,
    authority: TurnAuthorityBundle,
) -> list[Path]:
    steps = turn_entry.get("route_steps")
    if steps is None:
        return []
    if type(steps) is not list:
        raise TruthCaptureSupervisorError("event_route_steps_invalid")
    pages: list[Path] = []
    seen: set[Path] = set()
    for step in steps:
        if type(step) is not dict or step.get("kind") != "event_choice":
            continue
        report = step.get("report")
        capture = report.get("formal_capture") if type(report) is dict else None
        if type(capture) is not dict:
            continue
        metadata_sha = capture.get("capture_metadata_sha256")
        metadata = {
            key: value
            for key, value in capture.items()
            if key != "capture_metadata_sha256"
        }
        if (
            set(capture) != _EVENT_CAPTURE_KEYS
            or capture.get("schema_version") != "uma.event_choice_formal_capture.v1"
            or capture.get("run_session_id") != authority.run_session_id
            or capture.get("generation") != authority.generation
            or capture.get("global_action_turn") != authority.global_action_turn
            or capture.get("expires_after_global_action_turn")
            != authority.global_action_turn
            or capture.get("capture_ttl_turns") != 0
            or capture.get("page_state") != "event_choice"
            or type(metadata_sha) is not str
            or metadata_sha != _canonical_sha256(metadata)
        ):
            raise TruthCaptureSupervisorError("event_capture_binding_mismatch")
        source_path = capture.get("source_artifact_path")
        source_sha = capture.get("source_frame_sha256")
        if type(source_path) is not str or type(source_sha) is not str:
            raise TruthCaptureSupervisorError("event_capture_invalid")
        page = _contained_path(root, Path(source_path), directory=False)
        if hashlib.sha256(page.read_bytes()).hexdigest() != source_sha:
            raise TruthCaptureSupervisorError("event_capture_sha_mismatch")
        if page not in seen:
            seen.add(page)
            pages.append(page)
    return pages


def _contained_path(root: Path, path: Path, *, directory: bool) -> Path:
    try:
        _validate_plain_path_chain(
            root,
            path,
            code="artifact_path_reparse_point",
        )
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except TruthCaptureError as exc:
        raise TruthCaptureSupervisorError(str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise TruthCaptureSupervisorError("artifact_path_invalid") from exc
    wrong_type = (
        not resolved.is_dir()
        if directory
        else not resolved.is_file()
    )
    if wrong_type:
        raise TruthCaptureSupervisorError("artifact_path_invalid")
    return resolved


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _frame_artifact(
    *,
    artifact_id: str,
    artifact_kind: str,
    path: Path,
    root: Path,
    authority: TurnAuthorityBundle,
    turn_id: str,
    training_type: str | None,
    coordinate_space: str,
) -> dict[str, Any]:
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    return {
        "artifact_id": artifact_id,
        "artifact_kind": artifact_kind,
        "run_session_id": authority.run_session_id,
        "generation": authority.generation,
        "global_action_turn": authority.global_action_turn,
        "turn_id": turn_id,
        "authority_bundle_sha256": authority.bundle_sha256,
        "source_path": path.relative_to(root).as_posix(),
        "raw_bytes_sha256": digest,
        "source_frame_sha256": digest,
        "coordinate_space": coordinate_space,
        "training_type": training_type,
        "bbox_xyxy": None,
        "annotation_status": "proposal",
        "eligible_for_training": False,
        "action_eligible": False,
    }


def _build_truth_crop_proposals(
    *,
    root: Path,
    training_pages: dict[str, Path],
    authority: TurnAuthorityBundle,
    turn_id: str,
) -> tuple[dict[str, list[dict[str, Any]]], Path | None]:
    """Materialize only explicit local-recognizer geometry as blind crops.

    This deliberately does not promote labels or recognition confidence.  The
    parent capture writer independently reparses every crop and checks that
    its decoded pixels are an exact slice of the SHA-bound training frame.
    """

    groups: dict[str, list[dict[str, Any]]] = {
        "support_avatar_proposals": [],
        "support_hint_proposals": [],
        "support_bond_proposals": [],
        "fragment_proposals": [],
    }
    pending: list[tuple[Path, bytes, dict[str, Any]]] = []
    try:
        recognizer: SupportSlotRecognizer | None = SupportSlotRecognizer()
    except Exception:
        # A proposal generator is never allowed to prevent the frame archive.
        recognizer = None

    for tab in TRAINING_TABS:
        page = training_pages[tab]
        image = cv2.imread(str(page), cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            continue
        frame_sha = hashlib.sha256(page.read_bytes()).hexdigest()
        if recognizer is not None:
            try:
                result = recognizer.recognize_array(image, image_path=page)
            except Exception:
                result = None
            if isinstance(result, dict):
                _collect_support_crop_proposals(
                    pending=pending,
                    image=image,
                    result=result,
                    tab=tab,
                    frame_sha=frame_sha,
                    authority=authority,
                    turn_id=turn_id,
                )
        try:
            fragment = recognize_grand_masters_training_fragment(image)
        except Exception:
            fragment = None
        if isinstance(fragment, dict):
            _collect_fragment_crop_proposal(
                pending=pending,
                image=image,
                fragment=fragment,
                tab=tab,
                frame_sha=frame_sha,
                authority=authority,
                turn_id=turn_id,
            )

    if not pending:
        return groups, None
    proposal_root: Path | None = None
    try:
        proposal_root = Path(
            tempfile.mkdtemp(
                prefix=".truth_crop_proposals_v1-",
                dir=root,
            )
        )
        written: list[dict[str, Any]] = []
        for relative_path, payload, artifact in pending:
            rebased = Path(proposal_root.name).joinpath(*relative_path.parts[1:])
            destination = root / rebased
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as handle:
                handle.write(payload)
            artifact = {**artifact, "source_path": rebased.as_posix()}
            written.append(artifact)
        for artifact in written:
            groups[_proposal_group_for_kind(artifact["artifact_kind"])].append(artifact)
    except OSError:
        _cleanup_owned_proposal_root(root, proposal_root)
        return _empty_proposal_groups(), None
    return groups, proposal_root


def _empty_proposal_groups() -> dict[str, list[dict[str, Any]]]:
    return {
        "support_avatar_proposals": [],
        "support_hint_proposals": [],
        "support_bond_proposals": [],
        "fragment_proposals": [],
    }


def _cleanup_owned_proposal_root(root: Path, proposal_root: Path | None) -> None:
    if proposal_root is None:
        return
    try:
        if (
            proposal_root.parent.resolve(strict=True) != root
            or not proposal_root.name.startswith(".truth_crop_proposals_v1-")
            or proposal_root.is_symlink()
        ):
            return
        shutil.rmtree(proposal_root)
    except OSError:
        # A unique leftover cannot poison a retry and cleanup is best-effort.
        return


def _collect_support_crop_proposals(
    *,
    pending: list[tuple[Path, bytes, dict[str, Any]]],
    image: np.ndarray,
    result: dict[str, Any],
    tab: str,
    frame_sha: str,
    authority: TurnAuthorityBundle,
    turn_id: str,
) -> None:
    slots = result.get("support_slots")
    if type(slots) is not list:
        return
    roi = result.get("roi")
    for index, slot in enumerate(slots, start=1):
        if type(slot) is not dict:
            continue
        _queue_crop_proposal(
            pending=pending,
            image=image,
            bbox=slot.get("bbox"),
            artifact_kind="support_avatar_crop",
            group_name="support_avatar_proposals",
            tab=tab,
            ordinal=index,
            frame_sha=frame_sha,
            authority=authority,
            turn_id=turn_id,
        )
        hint_evidence = slot.get("hint_badge_evidence")
        hint_bbox = hint_evidence.get("bbox") if type(hint_evidence) is dict else None
        _queue_crop_proposal(
            pending=pending,
            image=image,
            bbox=hint_bbox,
            artifact_kind="support_hint_crop",
            group_name="support_hint_proposals",
            tab=tab,
            ordinal=index,
            frame_sha=frame_sha,
            authority=authority,
            turn_id=turn_id,
        )
        bond = slot.get("bond_bar")
        evidence = bond.get("evidence") if type(bond) is dict else None
        bond_bbox = _bond_bbox_in_frame(
            source=slot.get("bond_bar_source"),
            input_bbox=evidence.get("input_bbox") if type(evidence) is dict else None,
            roi=roi,
        )
        _queue_crop_proposal(
            pending=pending,
            image=image,
            bbox=bond_bbox,
            artifact_kind="support_bond_crop",
            group_name="support_bond_proposals",
            tab=tab,
            ordinal=index,
            frame_sha=frame_sha,
            authority=authority,
            turn_id=turn_id,
        )


def _collect_fragment_crop_proposal(
    *,
    pending: list[tuple[Path, bytes, dict[str, Any]]],
    image: np.ndarray,
    fragment: dict[str, Any],
    tab: str,
    frame_sha: str,
    authority: TurnAuthorityBundle,
    turn_id: str,
) -> None:
    evidence = fragment.get("evidence")
    if type(evidence) is not dict:
        return
    height, width = image.shape[:2]
    # The fragment recognizer reports canonical geometry.  Raw_720 proposals
    # may only use it when no normalization was applied to this exact frame.
    if (
        evidence.get("coordinate_space") != "canonical_720x1280_pixels"
        or evidence.get("source_size") != [width, height]
        or evidence.get("normalized") is not False
    ):
        return
    _queue_crop_proposal(
        pending=pending,
        image=image,
        bbox=evidence.get("badge_roi"),
        artifact_kind="fragment_crop",
        group_name="fragment_proposals",
        tab=tab,
        ordinal=1,
        frame_sha=frame_sha,
        authority=authority,
        turn_id=turn_id,
    )


def _bond_bbox_in_frame(*, source: object, input_bbox: object, roi: object) -> list[int] | None:
    if source != "detected" or not _valid_bbox(input_bbox):
        return None
    if (
        type(roi) is not list
        or len(roi) != 4
        or any(type(value) is not int for value in roi)
    ):
        return None
    x1, y1, x2, y2 = input_bbox
    return [roi[0] + x1, roi[1] + y1, roi[0] + x2, roi[1] + y2]


def _queue_crop_proposal(
    *,
    pending: list[tuple[Path, bytes, dict[str, Any]]],
    image: np.ndarray,
    bbox: object,
    artifact_kind: str,
    group_name: str,
    tab: str,
    ordinal: int,
    frame_sha: str,
    authority: TurnAuthorityBundle,
    turn_id: str,
) -> None:
    if not _valid_bbox(bbox):
        return
    x1, y1, x2, y2 = bbox
    height, width = image.shape[:2]
    if x2 > width or y2 > height:
        return
    crop = image[y1:y2, x1:x2]
    if crop.size == 0:
        return
    encoded_ok, encoded = cv2.imencode(".png", crop)
    if not encoded_ok:
        return
    payload = encoded.tobytes()
    stem = artifact_kind.removesuffix("_crop").replace("_", "-")
    artifact_id = f"{stem}-{tab}-{ordinal:03d}"
    relative = Path("truth_crop_proposals_v1") / group_name / f"{tab}-{ordinal:03d}.png"
    pending.append(
        (
            relative,
            payload,
            {
                "artifact_id": artifact_id,
                "artifact_kind": artifact_kind,
                "run_session_id": authority.run_session_id,
                "generation": authority.generation,
                "global_action_turn": authority.global_action_turn,
                "turn_id": turn_id,
                "authority_bundle_sha256": authority.bundle_sha256,
                "source_path": relative.as_posix(),
                "raw_bytes_sha256": hashlib.sha256(payload).hexdigest(),
                "source_frame_sha256": frame_sha,
                "coordinate_space": "raw_720",
                "training_type": tab,
                "bbox_xyxy": list(bbox),
                "annotation_status": "proposal",
                "eligible_for_training": False,
                "action_eligible": False,
            },
        )
    )


def _valid_bbox(value: object) -> bool:
    return (
        type(value) is list
        and len(value) == 4
        and all(type(item) is int and item >= 0 for item in value)
        and value[0] < value[2]
        and value[1] < value[3]
    )


def _proposal_group_for_kind(kind: str) -> str:
    return {
        "support_avatar_crop": "support_avatar_proposals",
        "support_hint_crop": "support_hint_proposals",
        "support_bond_crop": "support_bond_proposals",
        "fragment_crop": "fragment_proposals",
    }[kind]


def _validate_existing_training_closure(manifest: dict[str, Any]) -> None:
    training = manifest.get("groups", {}).get("training_pages")
    if (
        type(training) is not dict
        or training.get("status") != "available"
        or training.get("training_types") != list(TRAINING_TABS)
        or len(training.get("artifact_ids", [])) != len(TRAINING_TABS)
    ):
        raise TruthCaptureSupervisorError("training_page_closure_invalid")


def _available_result(
    root: Path,
    manifest_path: Path,
    *,
    reused: bool,
) -> dict[str, Any]:
    raw = manifest_path.read_bytes()
    return {
        "status": "available",
        "reason": None,
        "manifest_path": manifest_path.relative_to(root).as_posix(),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "reused": reused,
        "action_eligible": False,
    }
