"""Read-only, non-executable event-knowledge shadow audit records.

This module deliberately accepts only structured identity and SHA bindings.  It
does not accept images, coordinates, or an executor, and it can never promote
an event option into a live action.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.auto_trainer.career_supervisor_turn import TurnAuthorityBundle
from scripts.validate_event_knowledge import (
    EventKnowledgeValidationError,
    validate_release_registry,
    validate_snapshot,
)


EVENT_KNOWLEDGE_SHADOW_SCHEMA = "uma.event_knowledge_shadow.v1"
_SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_KINDS = frozenset(
    {"story_id", "approved_alias", "high_confidence_event_id"}
)
_MATCH_STATUSES = frozenset(
    {"matched", "unmatched", "ambiguous", "dataset_rejected"}
)


class EventKnowledgeShadowError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EventKnowledgeIdentity:
    """An exact identity produced by the structured event perception packet."""

    kind: Literal[
        "story_id", "approved_alias", "high_confidence_event_id"
    ]
    value: str

    def __post_init__(self) -> None:
        if self.kind not in _IDENTITY_KINDS:
            raise EventKnowledgeShadowError("event_identity_kind_invalid")
        if not isinstance(self.value, str) or not self.value.strip():
            raise EventKnowledgeShadowError("event_identity_value_invalid")

    @property
    def binding_sha256(self) -> str:
        return _canonical_sha({"kind": self.kind, "value": self.value})


@dataclass(frozen=True)
class EventKnowledgeShadowRequest:
    authority: TurnAuthorityBundle
    event_page_sha256: str
    identity: EventKnowledgeIdentity
    apk_event_database_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.authority, TurnAuthorityBundle):
            raise EventKnowledgeShadowError("event_knowledge_authority_required")
        _require_sha(self.event_page_sha256, "event_page_sha_invalid")
        if not isinstance(self.identity, EventKnowledgeIdentity):
            raise EventKnowledgeShadowError("event_identity_required")
        _require_sha(
            self.apk_event_database_sha256,
            "event_knowledge_apk_sha_invalid",
        )


@dataclass(frozen=True)
class EventKnowledgeShadowAudit:
    schema_version: str
    dataset_schema_version: str | None
    dataset_version: str | None
    dataset_sha256: str
    schema_sha256: str
    registry_sha256: str
    authority_bundle_sha256: str
    run_session_id: str
    generation: int
    source_frame_sha256: str
    event_page_sha256: str
    identity_binding_sha256: str
    match_status: Literal[
        "matched", "unmatched", "ambiguous", "dataset_rejected"
    ]
    matched_event_id: str | None
    shadow_option_ids: tuple[str, ...]
    action_eligible: bool
    abstain_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != EVENT_KNOWLEDGE_SHADOW_SCHEMA:
            raise EventKnowledgeShadowError("event_shadow_schema_invalid")
        if self.dataset_schema_version is not None and not isinstance(
            self.dataset_schema_version, str
        ):
            raise EventKnowledgeShadowError("event_dataset_schema_version_invalid")
        if self.dataset_version is not None and not isinstance(
            self.dataset_version, str
        ):
            raise EventKnowledgeShadowError("event_dataset_version_invalid")
        for value, code in (
            (self.dataset_sha256, "event_dataset_sha_invalid"),
            (self.schema_sha256, "event_schema_sha_invalid"),
            (self.registry_sha256, "event_registry_sha_invalid"),
            (self.authority_bundle_sha256, "event_authority_sha_invalid"),
            (self.source_frame_sha256, "event_source_frame_sha_invalid"),
            (self.event_page_sha256, "event_page_sha_invalid"),
            (self.identity_binding_sha256, "event_identity_binding_sha_invalid"),
        ):
            _require_sha(value, code)
        if not isinstance(self.run_session_id, str) or not self.run_session_id:
            raise EventKnowledgeShadowError("event_run_session_invalid")
        if type(self.generation) is not int or self.generation <= 0:
            raise EventKnowledgeShadowError("event_generation_invalid")
        if self.match_status not in _MATCH_STATUSES:
            raise EventKnowledgeShadowError("event_match_status_invalid")
        if self.matched_event_id is not None and (
            not isinstance(self.matched_event_id, str)
            or not self.matched_event_id
        ):
            raise EventKnowledgeShadowError("event_matched_id_invalid")
        if (
            not isinstance(self.shadow_option_ids, tuple)
            or any(
                not isinstance(option_id, str) or not option_id
                for option_id in self.shadow_option_ids
            )
            or len(set(self.shadow_option_ids)) != len(self.shadow_option_ids)
        ):
            raise EventKnowledgeShadowError("event_shadow_options_invalid")
        if self.action_eligible is not False:
            raise EventKnowledgeShadowError("event_shadow_must_not_be_executable")
        if (
            not isinstance(self.abstain_reasons, tuple)
            or not self.abstain_reasons
            or any(
                not isinstance(reason, str) or not reason
                for reason in self.abstain_reasons
            )
            or len(set(self.abstain_reasons)) != len(self.abstain_reasons)
        ):
            raise EventKnowledgeShadowError("event_abstain_reasons_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "dataset_schema_version": self.dataset_schema_version,
            "dataset_version": self.dataset_version,
            "dataset_sha256": self.dataset_sha256,
            "schema_sha256": self.schema_sha256,
            "registry_sha256": self.registry_sha256,
            "authority_bundle_sha256": self.authority_bundle_sha256,
            "run_session_id": self.run_session_id,
            "generation": self.generation,
            "source_frame_sha256": self.source_frame_sha256,
            "event_page_sha256": self.event_page_sha256,
            "identity_binding_sha256": self.identity_binding_sha256,
            "match_status": self.match_status,
            "matched_event_id": self.matched_event_id,
            "shadow_option_ids": list(self.shadow_option_ids),
            "action_eligible": False,
            "abstain_reasons": list(self.abstain_reasons),
        }


def load_event_knowledge_shadow(
    *,
    schema_path: Path,
    dataset_path: Path,
    registry_path: Path,
    request: EventKnowledgeShadowRequest,
) -> EventKnowledgeShadowAudit:
    """Load a validated staging deck and return a fail-closed shadow audit."""
    if not isinstance(request, EventKnowledgeShadowRequest):
        raise EventKnowledgeShadowError("event_shadow_request_required")
    schema_bytes = _read_bytes(schema_path, "event_schema_unreadable")
    dataset_bytes = _read_bytes(dataset_path, "event_dataset_unreadable")
    registry_bytes = _read_bytes(registry_path, "event_registry_unreadable")
    hashes = {
        "schema": hashlib.sha256(schema_bytes).hexdigest(),
        "dataset": hashlib.sha256(dataset_bytes).hexdigest(),
        "registry": hashlib.sha256(registry_bytes).hexdigest(),
    }
    schema = _decode_json(schema_bytes)
    dataset = _decode_json(dataset_bytes)
    registry = _decode_json(registry_bytes)
    dataset_schema_version = _text_or_none(dataset, "schema_version")
    dataset_version = _text_or_none(dataset, "dataset_version")
    if schema is None or dataset is None or registry is None:
        return _rejected_audit(
            request=request,
            hashes=hashes,
            dataset_schema_version=dataset_schema_version,
            dataset_version=dataset_version,
            reason="event_knowledge_json_decode_invalid",
        )
    if not all(isinstance(payload, dict) for payload in (schema, dataset, registry)):
        return _rejected_audit(
            request=request,
            hashes=hashes,
            dataset_schema_version=dataset_schema_version,
            dataset_version=dataset_version,
            reason="event_knowledge_json_root_invalid",
        )

    try:
        validate_snapshot(dataset, schema)
        validate_release_registry(registry)
    except EventKnowledgeValidationError:
        return _rejected_audit(
            request=request,
            hashes=hashes,
            dataset_schema_version=dataset_schema_version,
            dataset_version=dataset_version,
            reason="event_knowledge_schema_invalid",
        )
    except (KeyError, TypeError, ValueError):
        return _rejected_audit(
            request=request,
            hashes=hashes,
            dataset_schema_version=dataset_schema_version,
            dataset_version=dataset_version,
            reason="event_knowledge_validation_failed",
        )

    try:
        if dataset["apk_event_database_sha256"] != request.apk_event_database_sha256:
            return _rejected_audit(
                request=request,
                hashes=hashes,
                dataset_schema_version=dataset_schema_version,
                dataset_version=dataset_version,
                reason="event_knowledge_apk_hash_mismatch",
            )

        matches = tuple(
            event
            for event in dataset["events"]
            if _identity_matches(request.identity, event)
        )
        if not matches:
            return _audit(
                request=request,
                hashes=hashes,
                dataset_schema_version=dataset_schema_version,
                dataset_version=dataset_version,
                match_status="unmatched",
                abstain_reasons=("event_knowledge_identity_unmatched",),
            )
        if len(matches) != 1:
            return _audit(
                request=request,
                hashes=hashes,
                dataset_schema_version=dataset_schema_version,
                dataset_version=dataset_version,
                match_status="ambiguous",
                abstain_reasons=("event_knowledge_identity_ambiguous",),
            )

        event = matches[0]
        abstain_reasons = _shadow_abstain_reasons(event, registry, dataset)
        return _audit(
            request=request,
            hashes=hashes,
            dataset_schema_version=dataset_schema_version,
            dataset_version=dataset_version,
            match_status="matched",
            matched_event_id=event["event_id"],
            shadow_option_ids=tuple(
                option["option_id"] for option in event["options"]
            ),
            abstain_reasons=abstain_reasons,
        )
    except (KeyError, TypeError, ValueError):
        return _rejected_audit(
            request=request,
            hashes=hashes,
            dataset_schema_version=dataset_schema_version,
            dataset_version=dataset_version,
            reason="event_knowledge_validation_failed",
        )


def _rejected_audit(
    *,
    request: EventKnowledgeShadowRequest,
    hashes: dict[str, str],
    dataset_schema_version: str | None,
    dataset_version: str | None,
    reason: str,
) -> EventKnowledgeShadowAudit:
    return _audit(
        request=request,
        hashes=hashes,
        dataset_schema_version=dataset_schema_version,
        dataset_version=dataset_version,
        match_status="dataset_rejected",
        abstain_reasons=(reason,),
    )


def _audit(
    *,
    request: EventKnowledgeShadowRequest,
    hashes: dict[str, str],
    dataset_schema_version: str | None,
    dataset_version: str | None,
    match_status: Literal[
        "matched", "unmatched", "ambiguous", "dataset_rejected"
    ],
    abstain_reasons: tuple[str, ...],
    matched_event_id: str | None = None,
    shadow_option_ids: tuple[str, ...] = (),
) -> EventKnowledgeShadowAudit:
    authority = request.authority
    return EventKnowledgeShadowAudit(
        schema_version=EVENT_KNOWLEDGE_SHADOW_SCHEMA,
        dataset_schema_version=dataset_schema_version,
        dataset_version=dataset_version,
        dataset_sha256=hashes["dataset"],
        schema_sha256=hashes["schema"],
        registry_sha256=hashes["registry"],
        authority_bundle_sha256=authority.bundle_sha256,
        run_session_id=authority.run_session_id,
        generation=authority.generation,
        source_frame_sha256=authority.source_frame_sha256,
        event_page_sha256=request.event_page_sha256,
        identity_binding_sha256=request.identity.binding_sha256,
        match_status=match_status,
        matched_event_id=matched_event_id,
        shadow_option_ids=shadow_option_ids,
        action_eligible=False,
        abstain_reasons=abstain_reasons,
    )


def _shadow_abstain_reasons(
    event: dict[str, Any],
    registry: dict[str, Any],
    dataset: dict[str, Any],
) -> tuple[str, ...]:
    reasons = ["event_knowledge_dataset_shadow_only"]
    owner = event["owner"]
    registry_matches = tuple(
        entry
        for entry in registry["entries"]
        if entry["subject_namespace"] == owner["owner_id_namespace"]
        and entry["subject_id"] == owner["owner_id"]
    )
    cn_availability = tuple(
        availability
        for availability in event["region_availability"]
        if availability["region"] == "cn"
    )
    if (
        len(registry_matches) != 1
        or not cn_availability
        or registry_matches[0]["status"] != "current"
        or any(item["release_status"] != "current" for item in cn_availability)
    ):
        reasons.append("event_knowledge_cn_release_not_current")
    if (
        len(registry_matches) != 1
        or registry_matches[0]["live_eligible"] is not True
        or any(item["live_eligible"] is not True for item in cn_availability)
    ):
        reasons.append("event_knowledge_cn_not_live_eligible")
    if dataset["usable_for_action"] is not False or event["usable_for_action"] is not False:
        reasons.append("event_knowledge_actionability_invalid")
    return tuple(reasons)


def _identity_matches(
    identity: EventKnowledgeIdentity,
    event: dict[str, Any],
) -> bool:
    if identity.kind == "story_id":
        return event["story_id"] == identity.value
    if identity.kind == "high_confidence_event_id":
        return event["event_id"] == identity.value
    alignment = event["alignment"]
    return (
        alignment["method"] == "approved_alias"
        and alignment["status"] == "matched"
        and alignment["usable_for_action"] is False
        and event["usable_for_action"] is False
        and alignment.get("apk_title") == identity.value
    )


def _read_bytes(path: Path, error: str) -> bytes:
    if not isinstance(path, Path):
        raise EventKnowledgeShadowError(error)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise EventKnowledgeShadowError(error) from exc


def _decode_json(payload: bytes) -> Any | None:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _text_or_none(payload: Any, key: str) -> str | None:
    if isinstance(payload, dict) and isinstance(payload.get(key), str):
        return payload[key]
    return None


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
        raise EventKnowledgeShadowError(code)
