"""Fail-closed composition of exact event-title evidence and staging knowledge.

The result is intentionally report-only.  It never selects an event option,
never exposes an option identifier, and cannot be used as an action input.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from core.apk_reference import match_event_title_candidate
from core.auto_trainer.career_supervisor_event_effect_candidates import (
    EventEffectCandidate,
    EventEffectCandidateError,
    build_event_effect_candidates,
)
from core.auto_trainer.career_supervisor_event_choice_identity import (
    EventChoiceIdentityPacket,
    EventChoiceIdentityRequest,
    EventTitleEvidenceBinding,
    build_event_choice_identity_packet,
)
from core.auto_trainer.career_supervisor_event_context import (
    EventOwnerContextAuthority,
)
from core.auto_trainer.career_supervisor_event_knowledge_shadow import (
    EventKnowledgeIdentity,
    EventKnowledgeShadowAudit,
    EventKnowledgeShadowRequest,
    load_event_knowledge_shadow,
)
from core.auto_trainer.career_supervisor_event_title_roi import (
    EventTitleRoiLocalization,
)
from core.auto_trainer.career_supervisor_turn import TurnAuthorityBundle


EVENT_IDENTITY_SHADOW_SCHEMA = "uma.event_identity_shadow.v1"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class EventIdentityShadowRequest:
    authority: TurnAuthorityBundle
    event_page_sha256: str
    localization: EventTitleRoiLocalization
    evidence_binding: EventTitleEvidenceBinding
    matcher: object
    apk_event_database_sha256: str
    schema_path: Path
    dataset_path: Path
    registry_path: Path
    expected_schema_sha256: str
    expected_registry_sha256: str
    owner_context_authority: EventOwnerContextAuthority | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.authority, TurnAuthorityBundle):
            raise ValueError("event_shadow_authority_required")
        for value, code in (
            (self.event_page_sha256, "event_shadow_page_sha_invalid"),
            (self.apk_event_database_sha256, "event_shadow_database_sha_invalid"),
            (self.expected_schema_sha256, "event_shadow_schema_sha_invalid"),
            (self.expected_registry_sha256, "event_shadow_registry_sha_invalid"),
        ):
            if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
                raise ValueError(code)
        if not isinstance(self.localization, EventTitleRoiLocalization):
            raise ValueError("event_shadow_localization_required")
        if not isinstance(self.evidence_binding, EventTitleEvidenceBinding):
            raise ValueError("event_shadow_evidence_binding_required")
        if not all(isinstance(path, Path) for path in (self.schema_path, self.dataset_path, self.registry_path)):
            raise ValueError("event_shadow_path_invalid")
        if self.owner_context_authority is not None and not isinstance(
            self.owner_context_authority,
            EventOwnerContextAuthority,
        ):
            raise ValueError("event_shadow_owner_context_authority_invalid")


@dataclass(frozen=True)
class EventKnowledgeReadOnlySummary:
    event_id: str
    option_count: int
    branch_complete: bool
    dataset_sha256: str
    action_eligible: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("event_shadow_summary_event_invalid")
        if type(self.option_count) is not int or self.option_count <= 0:
            raise ValueError("event_shadow_summary_option_count_invalid")
        if self.branch_complete is not True or self.action_eligible is not False:
            raise ValueError("event_shadow_summary_must_be_read_only")
        if not isinstance(self.dataset_sha256, str) or _SHA256_PATTERN.fullmatch(self.dataset_sha256) is None:
            raise ValueError("event_shadow_summary_dataset_sha_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "option_count": self.option_count,
            "branch_complete": True,
            "dataset_sha256": self.dataset_sha256,
            "action_eligible": False,
        }


@dataclass(frozen=True)
class EventIdentityShadowReport:
    status: Literal["available", "abstain"]
    source_frame_sha256: str
    event_page_sha256: str
    identity: EventChoiceIdentityPacket | None
    audit: EventKnowledgeShadowAudit | None
    summary: EventKnowledgeReadOnlySummary | None
    abstain_reasons: tuple[str, ...]
    effect_candidates: tuple[EventEffectCandidate, ...] = ()
    action_eligible: bool = False

    def __post_init__(self) -> None:
        if self.status not in {"available", "abstain"}:
            raise ValueError("event_shadow_report_status_invalid")
        for value in (self.source_frame_sha256, self.event_page_sha256):
            if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
                raise ValueError("event_shadow_report_sha_invalid")
        if self.action_eligible is not False:
            raise ValueError("event_shadow_report_must_be_read_only")
        if not isinstance(self.abstain_reasons, tuple) or any(
            not isinstance(reason, str) or not reason for reason in self.abstain_reasons
        ):
            raise ValueError("event_shadow_report_reasons_invalid")
        if not isinstance(self.effect_candidates, tuple) or any(
            not isinstance(candidate, EventEffectCandidate)
            for candidate in self.effect_candidates
        ):
            raise ValueError("event_shadow_effect_candidates_invalid")
        if self.status == "available":
            if self.identity is None or self.audit is None or self.summary is None:
                raise ValueError("event_shadow_available_parts_missing")
        elif self.summary is not None or self.effect_candidates:
            raise ValueError("event_shadow_abstain_payload_present")

    def to_dict(self) -> dict[str, object]:
        """Serialize only non-executable diagnostics and summary fields."""

        payload: dict[str, object] = {
            "schema_version": EVENT_IDENTITY_SHADOW_SCHEMA,
            "status": self.status,
            "source_frame_sha256": self.source_frame_sha256,
            "event_page_sha256": self.event_page_sha256,
            "action_eligible": False,
            "abstain_reasons": list(self.abstain_reasons),
        }
        if self.identity is not None:
            payload["identity"] = self.identity.to_dict()
        if self.audit is not None:
            payload["knowledge_audit"] = {
                "match_status": self.audit.match_status,
                "matched_event_id": self.audit.matched_event_id,
                "dataset_sha256": self.audit.dataset_sha256,
                "schema_sha256": self.audit.schema_sha256,
                "registry_sha256": self.audit.registry_sha256,
                "abstain_reasons": list(self.audit.abstain_reasons),
                "action_eligible": False,
            }
        if self.summary is not None:
            payload["summary"] = self.summary.to_dict()
        if self.effect_candidates:
            payload["effect_candidates"] = [
                candidate.to_dict() for candidate in self.effect_candidates
            ]
        return payload


def build_event_identity_shadow_report(
    request: EventIdentityShadowRequest,
) -> EventIdentityShadowReport:
    """Return a same-frame exact-identity knowledge summary or abstain.

    This function is intentionally disconnected from planners and executors.
    """

    if not isinstance(request, EventIdentityShadowRequest):
        raise ValueError("event_shadow_request_required")
    authority = request.authority
    if request.event_page_sha256 != authority.source_frame_sha256:
        return _abstain(request, "event_page_sha_mismatch")
    localization = request.localization
    if localization.status != "located" or localization.binding is None or localization.roi_bgr is None:
        return _abstain(
            request,
            f"localization_{localization.abstain_reason or 'unavailable'}",
        )
    if localization.binding.source_frame_sha256 != authority.source_frame_sha256:
        return _abstain(request, "localization_frame_sha_mismatch")
    if not _binding_matches(localization, request.evidence_binding):
        return _abstain(request, "localization_binding_mismatch")
    if request.evidence_binding.source_database_sha256 != request.apk_event_database_sha256:
        return _abstain(request, "evidence_database_sha_mismatch")

    try:
        schema_bytes = request.schema_path.read_bytes()
        dataset_bytes = request.dataset_path.read_bytes()
        registry_bytes = request.registry_path.read_bytes()
    except OSError:
        return _abstain(request, "event_knowledge_input_unreadable")
    if hashlib.sha256(schema_bytes).hexdigest() != request.expected_schema_sha256:
        return _abstain(request, "schema_sha_mismatch")
    if hashlib.sha256(registry_bytes).hexdigest() != request.expected_registry_sha256:
        return _abstain(request, "registry_sha_mismatch")

    evidence = match_event_title_candidate(
        matcher=request.matcher,
        roi_bgr=localization.roi_bgr,
        binding=localization.binding,
    )
    try:
        identity = build_event_choice_identity_packet(
            EventChoiceIdentityRequest(
                authority=authority,
                run_session_id=authority.run_session_id,
                generation=authority.generation,
                authority_bundle_sha256=authority.bundle_sha256,
                event_page_sha256=request.event_page_sha256,
                evidence_binding=request.evidence_binding,
                evidence=evidence,
                event_knowledge_schema_bytes=schema_bytes,
                event_knowledge_dataset_bytes=dataset_bytes,
                owner_context_authority=request.owner_context_authority,
            )
        )
    except Exception:
        return _abstain(request, "identity_packet_unavailable")
    if identity.status != "identified":
        return _abstain(request, identity.abstain_reason or "identity_unavailable", identity=identity)

    try:
        audit = load_event_knowledge_shadow(
            schema_path=request.schema_path,
            dataset_path=request.dataset_path,
            registry_path=request.registry_path,
            request=EventKnowledgeShadowRequest(
                authority=authority,
                event_page_sha256=request.event_page_sha256,
                identity=EventKnowledgeIdentity(
                    kind=identity.identity_kind,
                    value=identity.identity_value,
                ),
                apk_event_database_sha256=request.apk_event_database_sha256,
            ),
        )
    except Exception:
        return _abstain(request, "knowledge_audit_unavailable", identity=identity)
    if (
        audit.dataset_sha256 != identity.dataset_sha256
        or audit.source_frame_sha256 != authority.source_frame_sha256
        or audit.event_page_sha256 != request.event_page_sha256
    ):
        return _abstain(request, "knowledge_binding_mismatch", identity=identity, audit=audit)
    if audit.match_status != "matched" or audit.matched_event_id is None:
        return _abstain(request, "knowledge_identity_unavailable", identity=identity, audit=audit)

    event = _matched_event(dataset_bytes, audit.matched_event_id)
    if event is None:
        return _abstain(request, "knowledge_event_unavailable", identity=identity, audit=audit)
    completeness_reason = _branch_completeness_reason(event)
    if completeness_reason is not None:
        return _abstain(request, completeness_reason, identity=identity, audit=audit)
    dataset_sha256 = hashlib.sha256(dataset_bytes).hexdigest()
    try:
        effect_candidates = build_event_effect_candidates(event, dataset_sha256)
    except EventEffectCandidateError as exc:
        return _abstain(
            request,
            f"effect_candidates_{exc.code}",
            identity=identity,
            audit=audit,
        )
    summary = EventKnowledgeReadOnlySummary(
        event_id=audit.matched_event_id,
        option_count=len(event["options"]),
        branch_complete=True,
        dataset_sha256=dataset_sha256,
    )
    if len(effect_candidates) != summary.option_count:
        return _abstain(
            request,
            "effect_candidates_option_count_mismatch",
            identity=identity,
            audit=audit,
        )
    return EventIdentityShadowReport(
        status="available",
        source_frame_sha256=authority.source_frame_sha256,
        event_page_sha256=request.event_page_sha256,
        identity=identity,
        audit=audit,
        summary=summary,
        abstain_reasons=audit.abstain_reasons,
        effect_candidates=effect_candidates,
    )


def _binding_matches(
    localization: EventTitleRoiLocalization,
    binding: EventTitleEvidenceBinding,
) -> bool:
    localized_binding = localization.binding
    return localized_binding is not None and (
        localized_binding.anchor_id == binding.anchor_id
        and localized_binding.roi_bbox == binding.roi_bbox
        and localized_binding.canonical_size == binding.canonical_size
        and localized_binding.transform_id == binding.transform_id
    )


def _matched_event(dataset_bytes: bytes, event_id: str) -> dict[str, object] | None:
    try:
        dataset = json.loads(dataset_bytes.decode("utf-8"))
        events = dataset["events"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    matches = [event for event in events if event.get("event_id") == event_id]
    return matches[0] if len(matches) == 1 and isinstance(matches[0], dict) else None


def _branch_completeness_reason(event: dict[str, object]) -> str | None:
    if event.get("event_kind") != "choice":
        return "event_kind_not_choice"
    options = event.get("options")
    if not isinstance(options, list) or not options:
        return "options_incomplete"
    for option in options:
        if not isinstance(option, dict) or not isinstance(option.get("option_id"), str):
            return "options_incomplete"
        branches = option.get("conditional_branches")
        if not isinstance(branches, list):
            return "options_incomplete"
        if branches:
            return "conditional_branches_present"
        effects = option.get("effects")
        if not isinstance(effects, list):
            return "options_incomplete"
        if any(isinstance(effect, dict) and isinstance(effect.get("value"), dict) for effect in effects):
            return "range_effect_present"
    return None


def _abstain(
    request: EventIdentityShadowRequest,
    reason: str,
    *,
    identity: EventChoiceIdentityPacket | None = None,
    audit: EventKnowledgeShadowAudit | None = None,
) -> EventIdentityShadowReport:
    return EventIdentityShadowReport(
        status="abstain",
        source_frame_sha256=request.authority.source_frame_sha256,
        event_page_sha256=request.event_page_sha256,
        identity=identity,
        audit=audit,
        summary=None,
        abstain_reasons=(reason,),
    )
