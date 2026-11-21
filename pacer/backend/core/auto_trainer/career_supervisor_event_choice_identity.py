"""Fail-closed structured identity packets for event-choice shadow audits.

The packet intentionally does not decode screenshots or expose ROI geometry.  A
future reviewed candidate-to-U18 mapping may supply an identity; until then a
matched reference fingerprint remains an explicit abstention.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping

from core.apk_reference.evidence import ReferenceEvidence
from core.auto_trainer.career_supervisor_event_context import (
    EventOwnerContextAuthority,
    event_matches_owner_context,
)
from core.auto_trainer.career_supervisor_turn import TurnAuthorityBundle
from scripts.validate_event_knowledge import (
    EventKnowledgeValidationError,
    validate_snapshot,
)


EVENT_CHOICE_IDENTITY_SCHEMA = "uma.event_choice_identity.v1"
_SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_EVENT_LABELS = frozenset({"red", "blue"})


class EventChoiceIdentityError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EventTitleEvidenceBinding:
    """Expected reference binding; it is hashed, never serialized verbatim."""

    anchor_id: str
    roi_bbox: tuple[int, int, int, int]
    canonical_size: tuple[int, int]
    transform_id: str
    source_database_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.anchor_id, str) or not self.anchor_id:
            raise EventChoiceIdentityError("identity_anchor_invalid")
        if (
            not isinstance(self.roi_bbox, tuple)
            or len(self.roi_bbox) != 4
            or any(type(value) is not int or value < 0 for value in self.roi_bbox)
            or self.roi_bbox[2] <= self.roi_bbox[0]
            or self.roi_bbox[3] <= self.roi_bbox[1]
        ):
            raise EventChoiceIdentityError("identity_roi_invalid")
        if (
            not isinstance(self.canonical_size, tuple)
            or len(self.canonical_size) != 2
            or any(type(value) is not int or value <= 0 for value in self.canonical_size)
        ):
            raise EventChoiceIdentityError("identity_canonical_size_invalid")
        if not isinstance(self.transform_id, str) or not self.transform_id:
            raise EventChoiceIdentityError("identity_transform_invalid")
        _require_sha(self.source_database_sha256, "identity_database_sha_invalid")

    def to_binding_material(self) -> dict[str, object]:
        return {
            "anchor_id": self.anchor_id,
            "roi_bbox": list(self.roi_bbox),
            "canonical_size": list(self.canonical_size),
            "transform_id": self.transform_id,
            "source_database_sha256": self.source_database_sha256,
        }


@dataclass(frozen=True)
class EventChoiceIdentityRequest:
    authority: TurnAuthorityBundle
    run_session_id: str
    generation: int
    authority_bundle_sha256: str
    event_page_sha256: str
    evidence_binding: EventTitleEvidenceBinding
    evidence: ReferenceEvidence
    event_knowledge_schema_bytes: bytes
    event_knowledge_dataset_bytes: bytes
    ocr_title: str | None = None
    owner_context_authority: EventOwnerContextAuthority | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.authority, TurnAuthorityBundle):
            raise EventChoiceIdentityError("identity_authority_required")
        if not isinstance(self.run_session_id, str) or not self.run_session_id:
            raise EventChoiceIdentityError("identity_run_session_invalid")
        if type(self.generation) is not int or self.generation <= 0:
            raise EventChoiceIdentityError("identity_generation_invalid")
        _require_sha(self.authority_bundle_sha256, "identity_authority_sha_invalid")
        _require_sha(self.event_page_sha256, "identity_event_page_sha_invalid")
        if not isinstance(self.evidence_binding, EventTitleEvidenceBinding):
            raise EventChoiceIdentityError("identity_evidence_binding_required")
        if not isinstance(self.evidence, ReferenceEvidence):
            raise EventChoiceIdentityError("identity_evidence_required")
        if not isinstance(self.event_knowledge_schema_bytes, bytes):
            raise EventChoiceIdentityError("identity_schema_bytes_invalid")
        if not isinstance(self.event_knowledge_dataset_bytes, bytes):
            raise EventChoiceIdentityError("identity_dataset_bytes_invalid")
        if self.ocr_title is not None and not isinstance(self.ocr_title, str):
            raise EventChoiceIdentityError("identity_ocr_title_invalid")
        if self.owner_context_authority is not None and not isinstance(
            self.owner_context_authority,
            EventOwnerContextAuthority,
        ):
            raise EventChoiceIdentityError(
                "identity_owner_context_authority_invalid"
            )


@dataclass(frozen=True)
class EventChoiceIdentityPacket:
    schema_version: str
    run_session_id: str
    generation: int
    authority_bundle_sha256: str
    source_frame_sha256: str
    event_page_sha256: str
    evidence_binding_sha256: str
    dataset_sha256: str
    mapping_sha256: str
    status: Literal["identified", "abstain"]
    identity_kind: Literal[
        "story_id", "approved_alias", "high_confidence_event_id"
    ] | None
    identity_value: str | None
    action_eligible: bool
    abstain_reason: str | None
    owner_context_authority_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != EVENT_CHOICE_IDENTITY_SCHEMA:
            raise EventChoiceIdentityError("identity_packet_schema_invalid")
        if not isinstance(self.run_session_id, str) or not self.run_session_id:
            raise EventChoiceIdentityError("identity_packet_run_session_invalid")
        if type(self.generation) is not int or self.generation <= 0:
            raise EventChoiceIdentityError("identity_packet_generation_invalid")
        for value, code in (
            (self.authority_bundle_sha256, "identity_packet_authority_sha_invalid"),
            (self.source_frame_sha256, "identity_packet_frame_sha_invalid"),
            (self.event_page_sha256, "identity_packet_page_sha_invalid"),
            (self.evidence_binding_sha256, "identity_packet_binding_sha_invalid"),
            (self.dataset_sha256, "identity_packet_dataset_sha_invalid"),
            (self.mapping_sha256, "identity_packet_mapping_sha_invalid"),
        ):
            _require_sha(value, code)
        if self.status not in {"identified", "abstain"}:
            raise EventChoiceIdentityError("identity_packet_status_invalid")
        if self.action_eligible is not False:
            raise EventChoiceIdentityError("identity_packet_must_be_shadow_only")
        if self.owner_context_authority_sha256 is not None:
            _require_sha(
                self.owner_context_authority_sha256,
                "identity_packet_owner_context_sha_invalid",
            )
        if self.status == "abstain":
            if (
                self.identity_kind is not None
                or self.identity_value is not None
                or not isinstance(self.abstain_reason, str)
                or not self.abstain_reason
            ):
                raise EventChoiceIdentityError("identity_packet_abstain_invalid")
        elif (
            self.identity_kind
            not in {"story_id", "approved_alias", "high_confidence_event_id"}
            or not isinstance(self.identity_value, str)
            or not self.identity_value
            or self.abstain_reason is not None
        ):
            raise EventChoiceIdentityError("identity_packet_identified_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_session_id": self.run_session_id,
            "generation": self.generation,
            "authority_bundle_sha256": self.authority_bundle_sha256,
            "source_frame_sha256": self.source_frame_sha256,
            "event_page_sha256": self.event_page_sha256,
            "evidence_binding_sha256": self.evidence_binding_sha256,
            "dataset_sha256": self.dataset_sha256,
            "mapping_sha256": self.mapping_sha256,
            "status": self.status,
            "identity_kind": self.identity_kind,
            "identity_value": self.identity_value,
            "action_eligible": False,
            "abstain_reason": self.abstain_reason,
            "owner_context_authority_sha256": (
                self.owner_context_authority_sha256
            ),
        }


def build_event_choice_identity_packet(
    request: EventChoiceIdentityRequest,
) -> EventChoiceIdentityPacket:
    """Build a non-executable identity packet from precomputed evidence only."""
    if not isinstance(request, EventChoiceIdentityRequest):
        raise EventChoiceIdentityError("identity_request_required")
    authority = request.authority
    binding_sha256 = _binding_sha256(request.evidence_binding, request.evidence)
    owner_context = request.owner_context_authority
    if (
        owner_context is not None
        and not owner_context.matches_turn_authority(authority)
    ):
        mapping = _build_exact_alias_mapping(
            schema_bytes=request.event_knowledge_schema_bytes,
            dataset_bytes=request.event_knowledge_dataset_bytes,
            source_database_sha256=request.evidence_binding.source_database_sha256,
            owner_context_authority=None,
        )
        return _abstain(
            request,
            binding_sha256,
            mapping,
            "owner_context_authority_binding_mismatch",
        )
    mapping = _build_exact_alias_mapping(
        schema_bytes=request.event_knowledge_schema_bytes,
        dataset_bytes=request.event_knowledge_dataset_bytes,
        source_database_sha256=request.evidence_binding.source_database_sha256,
        owner_context_authority=owner_context,
    )
    if (
        request.run_session_id != authority.run_session_id
        or request.generation != authority.generation
        or request.authority_bundle_sha256 != authority.bundle_sha256
    ):
        return _abstain(
            request, binding_sha256, mapping, "authority_binding_mismatch"
        )
    if request.event_page_sha256 != authority.source_frame_sha256:
        return _abstain(
            request, binding_sha256, mapping, "event_page_sha_mismatch"
        )
    if request.evidence.status != "matched":
        return _abstain(request, binding_sha256, mapping, "evidence_not_matched")
    if (
        request.evidence.source_frame_sha256 != authority.source_frame_sha256
        or request.evidence.source_frame_sha256 != request.event_page_sha256
    ):
        return _abstain(
            request,
            binding_sha256,
            mapping,
            "evidence_frame_sha_mismatch",
        )
    if (
        request.evidence.source_database_sha256
        != request.evidence_binding.source_database_sha256
    ):
        return _abstain(
            request,
            binding_sha256,
            mapping,
            "evidence_database_sha_mismatch",
        )
    if not _roi_binding_matches(request.evidence, request.evidence_binding):
        return _abstain(
            request,
            binding_sha256,
            mapping,
            "evidence_roi_binding_mismatch",
        )
    if mapping.failure_reason is not None:
        return _abstain(request, binding_sha256, mapping, mapping.failure_reason)
    decoded = _decode_canonical_candidate(request.evidence.candidate_id)
    if decoded is None:
        return _abstain(
            request, binding_sha256, mapping, "candidate_mapping_unavailable"
        )
    event_key, _label = decoded
    mapped_event_ids = mapping.event_ids_by_alias.get(event_key, ())
    if not mapped_event_ids:
        return _abstain(
            request, binding_sha256, mapping, "candidate_mapping_unavailable"
        )
    if len(mapped_event_ids) != 1:
        return _abstain(
            request, binding_sha256, mapping, "candidate_alias_ambiguous"
        )
    identity_kind = (
        "high_confidence_event_id"
        if owner_context is not None
        else "approved_alias"
    )
    identity_value = (
        mapped_event_ids[0]
        if owner_context is not None
        else event_key
    )
    return EventChoiceIdentityPacket(
        schema_version=EVENT_CHOICE_IDENTITY_SCHEMA,
        run_session_id=authority.run_session_id,
        generation=authority.generation,
        authority_bundle_sha256=authority.bundle_sha256,
        source_frame_sha256=authority.source_frame_sha256,
        event_page_sha256=request.event_page_sha256,
        evidence_binding_sha256=binding_sha256,
        dataset_sha256=mapping.dataset_sha256,
        mapping_sha256=mapping.mapping_sha256,
        status="identified",
        identity_kind=identity_kind,
        identity_value=identity_value,
        action_eligible=False,
        abstain_reason=None,
        owner_context_authority_sha256=(
            owner_context.authority_sha256
            if owner_context is not None
            else None
        ),
    )


def _abstain(
    request: EventChoiceIdentityRequest,
    binding_sha256: str,
    mapping: _ExactAliasMapping,
    reason: str,
) -> EventChoiceIdentityPacket:
    authority = request.authority
    return EventChoiceIdentityPacket(
        schema_version=EVENT_CHOICE_IDENTITY_SCHEMA,
        run_session_id=authority.run_session_id,
        generation=authority.generation,
        authority_bundle_sha256=authority.bundle_sha256,
        source_frame_sha256=authority.source_frame_sha256,
        event_page_sha256=request.event_page_sha256,
        evidence_binding_sha256=binding_sha256,
        dataset_sha256=mapping.dataset_sha256,
        mapping_sha256=mapping.mapping_sha256,
        status="abstain",
        identity_kind=None,
        identity_value=None,
        action_eligible=False,
        abstain_reason=reason,
        owner_context_authority_sha256=(
            request.owner_context_authority.authority_sha256
            if request.owner_context_authority is not None
            else None
        ),
    )


def _roi_binding_matches(
    evidence: ReferenceEvidence,
    binding: EventTitleEvidenceBinding,
) -> bool:
    return (
        evidence.anchor_id == binding.anchor_id
        and evidence.roi_bbox == binding.roi_bbox
        and evidence.canonical_size == binding.canonical_size
        and evidence.transform_id == binding.transform_id
    )


@dataclass(frozen=True)
class _ExactAliasMapping:
    dataset_sha256: str
    mapping_sha256: str
    event_ids_by_alias: Mapping[str, tuple[str, ...]]
    failure_reason: str | None


def _build_exact_alias_mapping(
    *,
    schema_bytes: bytes,
    dataset_bytes: bytes,
    source_database_sha256: str,
    owner_context_authority: EventOwnerContextAuthority | None,
) -> _ExactAliasMapping:
    dataset_sha256 = hashlib.sha256(dataset_bytes).hexdigest()
    empty_mapping_sha256 = _canonical_sha(
        {
            "schema_version": "uma.event_choice_exact_alias_map.v1",
            "dataset_sha256": dataset_sha256,
            "source_database_sha256": source_database_sha256,
            "owner_context_authority_sha256": (
                owner_context_authority.authority_sha256
                if owner_context_authority is not None
                else None
            ),
            "entries": [],
        }
    )
    try:
        schema = json.loads(schema_bytes.decode("utf-8"))
        dataset = json.loads(dataset_bytes.decode("utf-8"))
        if not isinstance(schema, dict) or not isinstance(dataset, dict):
            raise ValueError("event_knowledge_root_invalid")
        validate_snapshot(dataset, schema)
        if dataset["apk_event_database_sha256"] != source_database_sha256:
            return _ExactAliasMapping(
                dataset_sha256,
                empty_mapping_sha256,
                {},
                "mapping_database_sha_mismatch",
            )
        grouped: dict[str, list[str]] = {}
        for event in dataset["events"]:
            alignment = event["alignment"]
            approved_alias = (
                alignment["method"] == "approved_alias"
                and alignment["status"] == "matched"
            )
            context_resolvable_exact = (
                owner_context_authority is not None
                and alignment["method"] == "exact"
                and alignment["status"] == "ambiguous"
            )
            if not (approved_alias or context_resolvable_exact):
                continue
            if (
                alignment["usable_for_action"] is not False
                or event["usable_for_action"] is not False
                or alignment["apk_db_sha256"] != source_database_sha256
            ):
                continue
            if owner_context_authority is not None:
                context_match = event_matches_owner_context(
                    event,
                    owner_context_authority,
                )
                if context_match.status != "matched":
                    continue
            apk_title = alignment.get("apk_title")
            if not isinstance(apk_title, str) or not apk_title:
                continue
            grouped.setdefault(apk_title, []).append(event["event_id"])
    except (
        EventKnowledgeValidationError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return _ExactAliasMapping(
            dataset_sha256,
            empty_mapping_sha256,
            {},
            "event_knowledge_dataset_invalid",
        )
    frozen = MappingProxyType(
        {
            alias: tuple(sorted(event_ids))
            for alias, event_ids in grouped.items()
        }
    )
    mapping_sha256 = _canonical_sha(
        {
            "schema_version": "uma.event_choice_exact_alias_map.v1",
            "dataset_sha256": dataset_sha256,
            "source_database_sha256": source_database_sha256,
            "owner_context_authority_sha256": (
                owner_context_authority.authority_sha256
                if owner_context_authority is not None
                else None
            ),
            "entries": [
                [alias, list(event_ids)]
                for alias, event_ids in sorted(frozen.items())
            ],
        }
    )
    return _ExactAliasMapping(
        dataset_sha256,
        mapping_sha256,
        frozen,
        None,
    )


def _decode_canonical_candidate(
    candidate_id: str | None,
) -> tuple[str, str] | None:
    if not isinstance(candidate_id, str):
        return None
    try:
        decoded = json.loads(candidate_id)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or not isinstance(decoded[0], str)
        or not decoded[0]
        or not isinstance(decoded[1], str)
        or decoded[1] not in _EVENT_LABELS
    ):
        return None
    canonical = json.dumps(
        decoded,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if canonical != candidate_id:
        return None
    return decoded[0], decoded[1]


def _binding_sha256(
    binding: EventTitleEvidenceBinding,
    evidence: ReferenceEvidence,
) -> str:
    return _canonical_sha(
        {
            "expected": binding.to_binding_material(),
            "candidate_id": evidence.candidate_id,
            "source_frame_sha256": evidence.source_frame_sha256,
            "matcher_version": evidence.matcher_version,
        }
    )


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _require_sha(value: object, code: str) -> None:
    if not isinstance(value, str) or _SHA_PATTERN.fullmatch(value) is None:
        raise EventChoiceIdentityError(code)
