"""Read-only projection of deterministic U18 event effects.

The objects in this module are diagnostics only.  They deliberately contain
no tap coordinates or executable action authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping, TypeAlias


JsonScalar: TypeAlias = str | int | None
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_EFFECT_FIELDS = frozenset(
    {
        "kind",
        "op",
        "value",
        "scaling_context",
        "owner_id",
        "skill_id",
        "name_ja",
        "name_zh_cn",
        "level",
        "status_name",
        "item",
        "color",
        "notes",
    }
)
_EFFECT_KINDS = frozenset(
    {
        "speed",
        "stamina",
        "power",
        "guts",
        "wit",
        "energy",
        "max_energy",
        "mood",
        "skill_points",
        "bond",
        "skill_hint",
        "status_add",
        "status_remove",
        "scenario_item",
        "passion_zone",
        "outing_unlock",
        "chain_end",
    }
)
_EFFECT_OPS = frozenset({"add", "set", "acquire", "remove", "end"})


class EventEffectCandidateError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EventEffectProjection:
    fields: tuple[tuple[str, JsonScalar], ...]

    def __post_init__(self) -> None:
        if not self.fields:
            raise EventEffectCandidateError("effect_fields_empty")
        keys = tuple(key for key, _value in self.fields)
        if len(set(keys)) != len(keys) or tuple(sorted(keys)) != keys:
            raise EventEffectCandidateError("effect_fields_not_canonical")
        payload = dict(self.fields)
        if payload.get("kind") not in _EFFECT_KINDS:
            raise EventEffectCandidateError("effect_kind_invalid")
        if payload.get("op") not in _EFFECT_OPS:
            raise EventEffectCandidateError("effect_op_invalid")
        if any(
            not isinstance(key, str)
            or key not in _EFFECT_FIELDS
            or isinstance(value, bool)
            or not isinstance(value, (str, int, type(None)))
            for key, value in self.fields
        ):
            raise EventEffectCandidateError("effect_scalar_invalid")

    def to_dict(self) -> dict[str, JsonScalar]:
        return dict(self.fields)


@dataclass(frozen=True)
class EventEffectCandidate:
    candidate_id: str
    event_id: str
    option_id: str
    option_index: int
    effects: tuple[EventEffectProjection, ...]
    dataset_sha256: str
    decision_safe: bool = False
    action_eligible: bool = False
    usable_for_action: bool = False

    def __post_init__(self) -> None:
        _require_sha(self.candidate_id, "candidate_id_invalid")
        _require_sha(self.dataset_sha256, "dataset_sha256_invalid")
        if not isinstance(self.event_id, str) or not self.event_id:
            raise EventEffectCandidateError("event_id_invalid")
        if not isinstance(self.option_id, str) or not self.option_id:
            raise EventEffectCandidateError("option_id_invalid")
        if (
            isinstance(self.option_index, bool)
            or not isinstance(self.option_index, int)
            or self.option_index < 0
        ):
            raise EventEffectCandidateError("option_index_invalid")
        if (
            not isinstance(self.effects, tuple)
            or not self.effects
            or any(
                not isinstance(effect, EventEffectProjection)
                for effect in self.effects
            )
        ):
            raise EventEffectCandidateError("option_effects_empty")
        if (
            self.decision_safe is not False
            or self.action_eligible is not False
            or self.usable_for_action is not False
        ):
            raise EventEffectCandidateError("candidate_must_be_read_only")

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "event_id": self.event_id,
            "option_id": self.option_id,
            "option_index": self.option_index,
            "effects": [effect.to_dict() for effect in self.effects],
            "dataset_sha256": self.dataset_sha256,
            "decision_safe": False,
            "action_eligible": False,
            "usable_for_action": False,
        }


def build_event_effect_candidates(
    event: Mapping[str, object],
    dataset_sha256: str,
) -> tuple[EventEffectCandidate, ...]:
    if not isinstance(event, Mapping):
        raise EventEffectCandidateError("event_invalid")
    _require_sha(dataset_sha256, "dataset_sha256_invalid")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise EventEffectCandidateError("event_id_invalid")
    if event.get("event_kind") != "choice":
        raise EventEffectCandidateError("event_kind_not_choice")
    options = event.get("options")
    if not isinstance(options, list) or not options:
        raise EventEffectCandidateError("options_incomplete")

    option_ids: set[str] = set()
    option_indexes: set[int] = set()
    candidates: list[EventEffectCandidate] = []
    for option in options:
        if not isinstance(option, Mapping):
            raise EventEffectCandidateError("option_invalid")
        option_id = option.get("option_id")
        option_index = option.get("index")
        if not isinstance(option_id, str) or not option_id:
            raise EventEffectCandidateError("option_id_invalid")
        if option_id in option_ids:
            raise EventEffectCandidateError("option_id_duplicate")
        option_ids.add(option_id)
        if (
            isinstance(option_index, bool)
            or not isinstance(option_index, int)
            or option_index < 0
        ):
            raise EventEffectCandidateError("option_index_invalid")
        if option_index in option_indexes:
            raise EventEffectCandidateError("option_index_duplicate")
        option_indexes.add(option_index)
        branches = option.get("conditional_branches")
        if not isinstance(branches, list):
            raise EventEffectCandidateError("conditional_branches_invalid")
        if branches:
            raise EventEffectCandidateError("conditional_branches_present")
        raw_effects = option.get("effects")
        if not isinstance(raw_effects, list) or not raw_effects:
            raise EventEffectCandidateError("option_effects_empty")
        effects = tuple(_project_effect(effect) for effect in raw_effects)
        material = {
            "schema_version": "uma.event_effect_candidate.v1",
            "event_id": event_id,
            "option_id": option_id,
            "option_index": option_index,
            "effects": [effect.to_dict() for effect in effects],
            "dataset_sha256": dataset_sha256,
        }
        candidates.append(
            EventEffectCandidate(
                candidate_id=_canonical_sha256(material),
                event_id=event_id,
                option_id=option_id,
                option_index=option_index,
                effects=effects,
                dataset_sha256=dataset_sha256,
            )
        )
    return tuple(sorted(candidates, key=lambda candidate: candidate.option_index))


def _project_effect(effect: object) -> EventEffectProjection:
    if not isinstance(effect, Mapping):
        raise EventEffectCandidateError("effect_invalid")
    if not {"kind", "op"}.issubset(effect):
        raise EventEffectCandidateError("effect_required_fields_missing")
    unknown = set(effect) - _EFFECT_FIELDS
    if unknown:
        raise EventEffectCandidateError("effect_unknown_field")
    if isinstance(effect.get("value"), Mapping):
        raise EventEffectCandidateError("effect_range_value_present")
    fields: list[tuple[str, JsonScalar]] = []
    for key in sorted(effect):
        value = effect[key]
        if isinstance(value, bool) or not isinstance(value, (str, int, type(None))):
            raise EventEffectCandidateError("effect_scalar_invalid")
        fields.append((key, value))
    return EventEffectProjection(tuple(fields))


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _require_sha(value: object, code: str) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise EventEffectCandidateError(code)
