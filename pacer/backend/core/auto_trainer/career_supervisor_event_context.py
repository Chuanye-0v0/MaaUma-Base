"""Read-only event owner/context authority bound to one turn packet."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from core.auto_trainer.career_supervisor_turn import TurnAuthorityBundle
from core.auto_trainer.career_supervisor_prior_event_context import (
    PriorTurnEventContextAuthority,
)
from core.auto_trainer.run_trainee_identity_authority import (
    RunTraineeIdentityAuthority,
)


EVENT_OWNER_CONTEXT_SCHEMA = "uma.event_owner_context_authority.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OWNER_TOKEN_RE = re.compile(
    r"^owner:(?P<namespace>[a-z][a-z0-9_]*)=(?P<owner_id>[A-Za-z0-9_.:-]+)$"
)
_CONTEXT_TOKEN_RE = re.compile(
    r"^(?P<dimension>[a-z][a-z0-9_]*):(?P<value>[A-Za-z0-9_.:-]+)$"
)
_OWNER_NAMESPACES = {
    "trainee": frozenset({"trainee_id"}),
    "support": frozenset({"support_card_id", "support_chara_id"}),
    "scenario": frozenset({"scenario_id"}),
    "common": frozenset({"common_id"}),
}
_CONTEXT_DIMENSIONS = frozenset(
    {
        "calendar",
        "story_context",
        "race_result",
        "training_result",
        "extra_training_offer",
        "previous_training",
        "scenario_item_effect_only_in",
        "support_chain_step",
        "support_outing_step",
    }
)
_CONTEXT_FLAGS = frozenset(
    {
        "after_training_with_support",
        "support_chara_shared_event",
        "support_random_event",
    }
)
_ALL_CONTEXT_DIMENSIONS = _CONTEXT_DIMENSIONS | _CONTEXT_FLAGS


class EventOwnerContextError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EventOwnerContextAuthority:
    schema_version: str
    authority_bundle_sha256: str
    run_session_id: str
    generation: int
    global_action_turn: int
    owner_type: str
    owner_id_namespace: str
    owner_id: str
    context_tokens: tuple[str, ...]
    source_packet_sha256s: tuple[str, ...]
    unknown_dimensions: tuple[str, ...]
    authority_sha256: str
    action_eligible: bool = False
    usable_for_action: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != EVENT_OWNER_CONTEXT_SCHEMA:
            raise EventOwnerContextError("event_context_schema_invalid")
        _require_sha(
            self.authority_bundle_sha256,
            "event_context_turn_authority_sha_invalid",
        )
        if not isinstance(self.run_session_id, str) or not self.run_session_id:
            raise EventOwnerContextError("event_context_run_session_invalid")
        if type(self.generation) is not int or self.generation <= 0:
            raise EventOwnerContextError("event_context_generation_invalid")
        if (
            type(self.global_action_turn) is not int
            or self.global_action_turn < 0
        ):
            raise EventOwnerContextError("event_context_turn_invalid")
        _validate_owner(
            self.owner_type,
            self.owner_id_namespace,
            self.owner_id,
        )
        dimensions = _validate_context_tokens(
            self.context_tokens,
            owner_id_namespace=self.owner_id_namespace,
            owner_id=self.owner_id,
        )
        _validate_sha_tuple(self.source_packet_sha256s)
        _validate_unknown_dimensions(self.unknown_dimensions, dimensions)
        if self.action_eligible is not False or self.usable_for_action is not False:
            raise EventOwnerContextError("event_context_must_be_read_only")
        _require_sha(self.authority_sha256, "event_context_authority_sha_invalid")
        material = self.to_dict()
        material.pop("authority_sha256")
        if _canonical_sha(material) != self.authority_sha256:
            raise EventOwnerContextError("event_context_authority_sha_mismatch")

    def matches_turn_authority(self, authority: TurnAuthorityBundle) -> bool:
        return isinstance(authority, TurnAuthorityBundle) and (
            self.authority_bundle_sha256 == authority.bundle_sha256
            and self.run_session_id == authority.run_session_id
            and self.generation == authority.generation
            and self.global_action_turn == authority.global_action_turn
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority_bundle_sha256": self.authority_bundle_sha256,
            "run_session_id": self.run_session_id,
            "generation": self.generation,
            "global_action_turn": self.global_action_turn,
            "owner_type": self.owner_type,
            "owner_id_namespace": self.owner_id_namespace,
            "owner_id": self.owner_id,
            "context_tokens": list(self.context_tokens),
            "source_packet_sha256s": list(self.source_packet_sha256s),
            "unknown_dimensions": list(self.unknown_dimensions),
            "authority_sha256": self.authority_sha256,
            "action_eligible": False,
            "usable_for_action": False,
        }


@dataclass(frozen=True)
class EventOwnerContextMatch:
    status: Literal["matched", "conflict", "insufficient"]
    authority_sha256: str
    matched_conditions: tuple[str, ...]
    missing_dimensions: tuple[str, ...]
    conflict_dimensions: tuple[str, ...]
    reason_codes: tuple[str, ...]
    action_eligible: bool = False
    usable_for_action: bool = False

    def __post_init__(self) -> None:
        if self.status not in {"matched", "conflict", "insufficient"}:
            raise EventOwnerContextError("event_context_match_status_invalid")
        _require_sha(self.authority_sha256, "event_context_authority_sha_invalid")
        for values in (
            self.matched_conditions,
            self.missing_dimensions,
            self.conflict_dimensions,
            self.reason_codes,
        ):
            if (
                not isinstance(values, tuple)
                or any(not isinstance(value, str) or not value for value in values)
                or len(set(values)) != len(values)
            ):
                raise EventOwnerContextError("event_context_match_values_invalid")
        if self.action_eligible is not False or self.usable_for_action is not False:
            raise EventOwnerContextError("event_context_match_must_be_read_only")


def build_event_owner_context_authority(
    *,
    turn_authority: TurnAuthorityBundle,
    owner_type: str,
    owner_id_namespace: str,
    owner_id: str,
    context_tokens: tuple[str, ...],
    source_packet_sha256s: tuple[str, ...],
    unknown_dimensions: tuple[str, ...],
) -> EventOwnerContextAuthority:
    if not isinstance(turn_authority, TurnAuthorityBundle):
        raise EventOwnerContextError("event_context_turn_authority_required")
    _validate_owner(owner_type, owner_id_namespace, owner_id)
    normalized_tokens = tuple(sorted(context_tokens))
    dimensions = _validate_context_tokens(
        normalized_tokens,
        owner_id_namespace=owner_id_namespace,
        owner_id=owner_id,
    )
    normalized_sources = tuple(sorted(source_packet_sha256s))
    _validate_sha_tuple(normalized_sources)
    normalized_unknown = tuple(sorted(unknown_dimensions))
    _validate_unknown_dimensions(normalized_unknown, dimensions)
    payload: dict[str, object] = {
        "schema_version": EVENT_OWNER_CONTEXT_SCHEMA,
        "authority_bundle_sha256": turn_authority.bundle_sha256,
        "run_session_id": turn_authority.run_session_id,
        "generation": turn_authority.generation,
        "global_action_turn": turn_authority.global_action_turn,
        "owner_type": owner_type,
        "owner_id_namespace": owner_id_namespace,
        "owner_id": owner_id,
        "context_tokens": list(normalized_tokens),
        "source_packet_sha256s": list(normalized_sources),
        "unknown_dimensions": list(normalized_unknown),
        "action_eligible": False,
        "usable_for_action": False,
    }
    payload["authority_sha256"] = _canonical_sha(payload)
    return EventOwnerContextAuthority(
        schema_version=EVENT_OWNER_CONTEXT_SCHEMA,
        authority_bundle_sha256=turn_authority.bundle_sha256,
        run_session_id=turn_authority.run_session_id,
        generation=turn_authority.generation,
        global_action_turn=turn_authority.global_action_turn,
        owner_type=owner_type,
        owner_id_namespace=owner_id_namespace,
        owner_id=owner_id,
        context_tokens=normalized_tokens,
        source_packet_sha256s=normalized_sources,
        unknown_dimensions=normalized_unknown,
        authority_sha256=str(payload["authority_sha256"]),
    )


def build_trainee_event_owner_context_authority(
    *,
    turn_authority: TurnAuthorityBundle,
    trainee_authority: RunTraineeIdentityAuthority,
    context_tokens: tuple[str, ...],
    source_packet_sha256s: tuple[str, ...],
    unknown_dimensions: tuple[str, ...],
) -> EventOwnerContextAuthority:
    """Bind observed event context to the trainee accepted at career setup.

    This wrapper deliberately derives the owner tuple from the run authority;
    callers cannot substitute a title-derived or screenshot-guessed owner id.
    """

    if not isinstance(trainee_authority, RunTraineeIdentityAuthority):
        raise EventOwnerContextError("event_context_trainee_authority_required")
    if (
        not isinstance(turn_authority, TurnAuthorityBundle)
        or trainee_authority.run_session_id != turn_authority.run_session_id
    ):
        raise EventOwnerContextError(
            "event_context_trainee_authority_binding_mismatch"
        )
    if any(
        isinstance(token, str) and token.startswith("owner:")
        for token in context_tokens
    ):
        raise EventOwnerContextError("event_context_owner_token_supplied")
    return build_event_owner_context_authority(
        turn_authority=turn_authority,
        owner_type=trainee_authority.owner_type,
        owner_id_namespace=trainee_authority.owner_id_namespace,
        owner_id=trainee_authority.owner_id,
        context_tokens=(
            *context_tokens,
            (
                f"owner:{trainee_authority.owner_id_namespace}="
                f"{trainee_authority.owner_id}"
            ),
        ),
        source_packet_sha256s=(
            *source_packet_sha256s,
            trainee_authority.authority_sha256,
        ),
        unknown_dimensions=unknown_dimensions,
    )


def build_trainee_event_owner_context_from_prior(
    *,
    turn_authority: TurnAuthorityBundle,
    trainee_authority: RunTraineeIdentityAuthority,
    prior_context: PriorTurnEventContextAuthority,
) -> EventOwnerContextAuthority:
    if not isinstance(prior_context, PriorTurnEventContextAuthority):
        raise EventOwnerContextError("event_context_prior_authority_required")
    if not prior_context.matches_turn_authority(turn_authority):
        raise EventOwnerContextError(
            "event_context_prior_authority_binding_mismatch"
        )
    context_tokens, unknown_dimensions = prior_context.event_projection()
    return build_trainee_event_owner_context_authority(
        turn_authority=turn_authority,
        trainee_authority=trainee_authority,
        context_tokens=context_tokens,
        source_packet_sha256s=(prior_context.authority_sha256,),
        unknown_dimensions=unknown_dimensions,
    )


def event_matches_owner_context(
    event: Mapping[str, object],
    authority: EventOwnerContextAuthority,
) -> EventOwnerContextMatch:
    if not isinstance(authority, EventOwnerContextAuthority):
        raise EventOwnerContextError("event_context_authority_required")
    if not isinstance(event, Mapping):
        return _match(authority, "insufficient", reasons=("event_context_event_invalid",))
    owner = event.get("owner")
    conditions = event.get("trigger_conditions")
    if not isinstance(owner, Mapping) or not isinstance(conditions, list):
        return _match(authority, "insufficient", reasons=("event_context_event_invalid",))
    expected_owner = (
        authority.owner_type,
        authority.owner_id_namespace,
        authority.owner_id,
    )
    observed_owner = (
        owner.get("owner_type"),
        owner.get("owner_id_namespace"),
        owner.get("owner_id"),
    )
    if observed_owner != expected_owner:
        return _match(
            authority,
            "conflict",
            conflicts=("owner",),
            reasons=("event_owner_conflict",),
        )

    known = _token_by_dimension(authority.context_tokens)
    unknown = set(authority.unknown_dimensions)
    matched: list[str] = []
    missing: list[str] = []
    conflicts: list[str] = []
    unsupported = False
    for condition in conditions:
        if not isinstance(condition, str) or not condition:
            unsupported = True
            continue
        owner_match = _OWNER_TOKEN_RE.fullmatch(condition)
        if owner_match is not None:
            if (
                owner_match.group("namespace") != authority.owner_id_namespace
                or owner_match.group("owner_id") != authority.owner_id
            ):
                conflicts.append("owner")
            else:
                matched.append(condition)
            continue
        parsed = _CONTEXT_TOKEN_RE.fullmatch(condition)
        if condition in _CONTEXT_FLAGS:
            dimension = condition
        elif (
            parsed is None
            or parsed.group("dimension") not in _CONTEXT_DIMENSIONS
        ):
            unsupported = True
            continue
        else:
            dimension = parsed.group("dimension")
        known_token = known.get(dimension)
        if known_token == condition:
            matched.append(condition)
        elif known_token is not None:
            conflicts.append(dimension)
        elif dimension in unknown or dimension not in known:
            missing.append(dimension)

    if conflicts:
        return _match(
            authority,
            "conflict",
            matched=matched,
            conflicts=conflicts,
            reasons=("event_context_conflict",),
        )
    if unsupported or missing:
        reasons = ["event_context_incomplete"]
        if unsupported:
            reasons.append("event_context_condition_unsupported")
        return _match(
            authority,
            "insufficient",
            matched=matched,
            missing=missing,
            reasons=reasons,
        )
    return _match(authority, "matched", matched=matched)


def _match(
    authority: EventOwnerContextAuthority,
    status: Literal["matched", "conflict", "insufficient"],
    *,
    matched: list[str] | tuple[str, ...] = (),
    missing: list[str] | tuple[str, ...] = (),
    conflicts: list[str] | tuple[str, ...] = (),
    reasons: list[str] | tuple[str, ...] = (),
) -> EventOwnerContextMatch:
    return EventOwnerContextMatch(
        status=status,
        authority_sha256=authority.authority_sha256,
        matched_conditions=tuple(sorted(set(matched))),
        missing_dimensions=tuple(sorted(set(missing))),
        conflict_dimensions=tuple(sorted(set(conflicts))),
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def _validate_owner(owner_type: object, namespace: object, owner_id: object) -> None:
    if (
        not isinstance(owner_type, str)
        or owner_type not in _OWNER_NAMESPACES
        or not isinstance(namespace, str)
        or namespace not in _OWNER_NAMESPACES[owner_type]
        or not isinstance(owner_id, str)
        or not owner_id
        or re.fullmatch(r"[A-Za-z0-9_.:-]+", owner_id) is None
    ):
        raise EventOwnerContextError("event_context_owner_invalid")


def _validate_context_tokens(
    tokens: object,
    *,
    owner_id_namespace: str,
    owner_id: str,
) -> frozenset[str]:
    if (
        not isinstance(tokens, tuple)
        or not tokens
        or tuple(sorted(tokens)) != tokens
        or len(set(tokens)) != len(tokens)
    ):
        raise EventOwnerContextError("event_context_tokens_invalid")
    dimensions: list[str] = []
    owner_tokens = 0
    for token in tokens:
        if not isinstance(token, str) or not token:
            raise EventOwnerContextError("event_context_token_invalid")
        owner_match = _OWNER_TOKEN_RE.fullmatch(token)
        if owner_match is not None:
            owner_tokens += 1
            if (
                owner_match.group("namespace") != owner_id_namespace
                or owner_match.group("owner_id") != owner_id
            ):
                raise EventOwnerContextError("event_context_owner_token_mismatch")
            dimensions.append("owner")
            continue
        if token in _CONTEXT_FLAGS:
            dimensions.append(token)
            continue
        parsed = _CONTEXT_TOKEN_RE.fullmatch(token)
        if (
            parsed is None
            or parsed.group("dimension") not in _CONTEXT_DIMENSIONS
        ):
            raise EventOwnerContextError("event_context_token_invalid")
        dimensions.append(parsed.group("dimension"))
    if owner_tokens != 1:
        raise EventOwnerContextError("event_context_owner_token_missing")
    if len(dimensions) != len(set(dimensions)):
        raise EventOwnerContextError("event_context_dimension_duplicate")
    return frozenset(dimensions)


def _token_by_dimension(tokens: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for token in tokens:
        if token.startswith("owner:"):
            result["owner"] = token
        elif token in _CONTEXT_FLAGS:
            result[token] = token
        else:
            result[token.split(":", 1)[0]] = token
    return result


def _validate_sha_tuple(values: object) -> None:
    if (
        not isinstance(values, tuple)
        or not values
        or tuple(sorted(values)) != values
        or len(set(values)) != len(values)
        or any(
            not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
            for value in values
        )
    ):
        raise EventOwnerContextError("event_context_source_sha_invalid")


def _validate_unknown_dimensions(
    values: object,
    known_dimensions: frozenset[str],
) -> None:
    if (
        not isinstance(values, tuple)
        or tuple(sorted(values)) != values
        or len(set(values)) != len(values)
        or any(value not in _ALL_CONTEXT_DIMENSIONS for value in values)
    ):
        raise EventOwnerContextError("event_context_unknown_dimensions_invalid")
    if known_dimensions.intersection(values):
        raise EventOwnerContextError("event_context_unknown_dimension_conflict")


def _require_sha(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise EventOwnerContextError(code)
    return value


def _canonical_sha(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "EVENT_OWNER_CONTEXT_SCHEMA",
    "EventOwnerContextAuthority",
    "EventOwnerContextError",
    "EventOwnerContextMatch",
    "build_event_owner_context_authority",
    "build_trainee_event_owner_context_from_prior",
    "build_trainee_event_owner_context_authority",
    "event_matches_owner_context",
]
