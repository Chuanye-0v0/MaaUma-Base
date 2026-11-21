"""Offline-only formal event-capture to read-only identity-shadow producer.

This boundary accepts only a formal capture that is already tied to a
``TurnAuthorityBundle``.  It reads the corresponding local screenshot,
localizes its title ROI, and uses the verified APK event database plus the
staging knowledge snapshot to produce a non-executable shadow report.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import cv2
import numpy as np

from core.apk_reference import SOURCES, build_event_title_matcher, load_verified_json
from core.auto_trainer.career_supervisor_event_choice_identity import (
    EventTitleEvidenceBinding,
)
from core.auto_trainer.career_supervisor_event_identity_shadow import (
    EventIdentityShadowReport,
    EventIdentityShadowRequest,
    build_event_identity_shadow_report,
)
from core.auto_trainer.career_supervisor_event_context import (
    EventOwnerContextAuthority,
)
from core.auto_trainer.career_supervisor_event_title_roi import (
    localize_offline_event_title_roi,
)
from core.auto_trainer.career_supervisor_turn import TurnAuthorityBundle


_FORMAL_CAPTURE_FIELDS = frozenset(
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
_MATCHER_VERSION = "uma.event_identity_perception.local_fingerprint.v1"
_ACCEPTANCE_THRESHOLD = 0.98
_MARGIN_THRESHOLD = 0.10


@dataclass(frozen=True)
class EventIdentityPerceptionRequest:
    """All local inputs required to produce one same-frame shadow report."""

    authority: TurnAuthorityBundle
    formal_capture: Mapping[str, object]
    artifact_path: Path
    artifact_root: Path
    apk_root: Path
    schema_path: Path
    dataset_path: Path
    registry_path: Path
    expected_schema_sha256: str
    expected_dataset_sha256: str
    expected_registry_sha256: str
    owner_context_authority: EventOwnerContextAuthority | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.authority, TurnAuthorityBundle):
            raise ValueError("event_identity_perception_authority_required")
        if not isinstance(self.formal_capture, Mapping):
            raise ValueError("event_identity_perception_capture_required")
        if not all(
            isinstance(path, Path)
            for path in (
                self.artifact_path,
                self.artifact_root,
                self.apk_root,
                self.schema_path,
                self.dataset_path,
                self.registry_path,
            )
        ):
            raise ValueError("event_identity_perception_path_invalid")
        if any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in (
                self.expected_schema_sha256,
                self.expected_dataset_sha256,
                self.expected_registry_sha256,
            )
        ):
            raise ValueError("event_identity_perception_expected_sha_invalid")
        if self.owner_context_authority is not None and not isinstance(
            self.owner_context_authority,
            EventOwnerContextAuthority,
        ):
            raise ValueError(
                "event_identity_perception_owner_context_authority_invalid"
            )


def produce_event_identity_shadow_report(
    request: EventIdentityPerceptionRequest,
) -> EventIdentityShadowReport:
    """Produce an action-incapable shadow or abstain without side effects."""

    if not isinstance(request, EventIdentityPerceptionRequest):
        raise ValueError("event_identity_perception_request_required")
    if not _formal_capture_matches_authority(request):
        return _abstain(request, "formal_capture_binding_invalid")
    if not _artifact_path_is_within_root(request):
        return _abstain(request, "event_shadow_artifact_path_invalid")
    try:
        artifact_bytes = request.artifact_path.read_bytes()
        artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    except OSError:
        return _abstain(request, "event_shadow_artifact_unreadable")
    if artifact_sha256 != request.authority.source_frame_sha256:
        return _abstain(request, "event_shadow_artifact_sha_mismatch")
    if not _knowledge_inputs_match_pins(request):
        return _abstain(request, "event_identity_perception_input_sha_mismatch")
    frame = _read_event_frame(artifact_bytes)
    if frame is None:
        return _abstain(request, "event_shadow_frame_decode_failed")
    localization = localize_offline_event_title_roi(
        frame,
        source_frame_sha256=request.authority.source_frame_sha256,
    )
    if localization.status != "located" or localization.binding is None:
        return _abstain(
            request,
            f"localization_{localization.abstain_reason or 'unavailable'}",
        )
    try:
        event_database = load_verified_json("event", root=request.apk_root)
        event_records = event_database["events"]
        matcher = build_event_title_matcher(
            event_records=event_records,
            acceptance_threshold=_ACCEPTANCE_THRESHOLD,
            margin_threshold=_MARGIN_THRESHOLD,
            matcher_version=_MATCHER_VERSION,
            source_database_sha256=SOURCES["event"].sha256,
            allowed_transform_ids=(localization.binding.transform_id,),
        )
        evidence_binding = EventTitleEvidenceBinding(
            anchor_id=localization.binding.anchor_id,
            roi_bbox=localization.binding.roi_bbox,
            canonical_size=localization.binding.canonical_size,
            transform_id=localization.binding.transform_id,
            source_database_sha256=SOURCES["event"].sha256,
        )
    except Exception:
        return _abstain(request, "event_identity_perception_inputs_unavailable")
    try:
        return build_event_identity_shadow_report(
            EventIdentityShadowRequest(
                authority=request.authority,
                event_page_sha256=request.authority.source_frame_sha256,
                localization=localization,
                evidence_binding=evidence_binding,
                matcher=matcher,
                apk_event_database_sha256=SOURCES["event"].sha256,
                schema_path=request.schema_path,
                dataset_path=request.dataset_path,
                registry_path=request.registry_path,
                expected_schema_sha256=request.expected_schema_sha256,
                expected_registry_sha256=request.expected_registry_sha256,
                owner_context_authority=request.owner_context_authority,
            )
        )
    except Exception:
        return _abstain(request, "event_identity_perception_unavailable")


def _formal_capture_matches_authority(request: EventIdentityPerceptionRequest) -> bool:
    capture = request.formal_capture
    if set(capture) != _FORMAL_CAPTURE_FIELDS:
        return False
    metadata_sha256 = capture.get("capture_metadata_sha256")
    material = {
        key: value
        for key, value in capture.items()
        if key != "capture_metadata_sha256"
    }
    authority = request.authority
    if (
        not isinstance(metadata_sha256, str)
        or metadata_sha256 != _canonical_sha256(material)
        or capture.get("schema_version") != "uma.event_choice_formal_capture.v1"
        or capture.get("run_session_id") != authority.run_session_id
        or type(capture.get("generation")) is not int
        or capture.get("generation") != authority.generation
        or type(capture.get("global_action_turn")) is not int
        or capture.get("global_action_turn") != authority.global_action_turn
        or capture.get("game_period") != authority.game_period
        or capture.get("game_period") != "unknown"
        or capture.get("page_state") != "event_choice"
        or not isinstance(capture.get("source_frame_sha256"), str)
        or capture.get("source_frame_sha256") != authority.source_frame_sha256
        or type(capture.get("capture_ttl_turns")) is not int
        or capture.get("capture_ttl_turns") != 0
        or type(capture.get("expires_after_global_action_turn")) is not int
        or capture.get("expires_after_global_action_turn") != authority.global_action_turn
        or not isinstance(capture.get("source_artifact_path"), str)
    ):
        return False
    try:
        return Path(capture["source_artifact_path"]).resolve(strict=True) == request.artifact_path.resolve(strict=True)
    except OSError:
        return False


def _read_event_frame(artifact_bytes: bytes) -> np.ndarray | None:
    return cv2.imdecode(np.frombuffer(artifact_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)


def _artifact_path_is_within_root(request: EventIdentityPerceptionRequest) -> bool:
    try:
        raw_artifact = Path(os.path.abspath(request.artifact_path))
        raw_root = Path(os.path.abspath(request.artifact_root))
        relative_artifact = raw_artifact.relative_to(raw_root)
    except OSError:
        return False
    except ValueError:
        return False
    if raw_root.is_symlink() or not relative_artifact.parts:
        return False
    raw_current = raw_root
    for part in relative_artifact.parts:
        raw_current = raw_current / part
        if raw_current.is_symlink():
            return False
    try:
        artifact = raw_artifact.resolve(strict=True)
        root = raw_root.resolve(strict=True)
    except OSError:
        return False
    return artifact != root and root in artifact.parents


def _knowledge_inputs_match_pins(request: EventIdentityPerceptionRequest) -> bool:
    inputs = (
        (request.schema_path, request.expected_schema_sha256),
        (request.dataset_path, request.expected_dataset_sha256),
        (request.registry_path, request.expected_registry_sha256),
    )
    try:
        return all(
            hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha256
            for path, expected_sha256 in inputs
        )
    except OSError:
        return False


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _abstain(
    request: EventIdentityPerceptionRequest,
    reason: str,
) -> EventIdentityShadowReport:
    return EventIdentityShadowReport(
        status="abstain",
        source_frame_sha256=request.authority.source_frame_sha256,
        event_page_sha256=request.authority.source_frame_sha256,
        identity=None,
        audit=None,
        summary=None,
        abstain_reasons=(reason,),
        action_eligible=False,
    )
