"""Deterministic read-only scoring for complete event effect candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from core.auto_trainer.career_supervisor_event_effect_candidates import (
    EventEffectCandidate,
)


_SHA_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_NUMERIC_ADD_KINDS = frozenset(
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
    }
)


class EventLocalDecisionError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EventLocalDecisionBinding:
    authority_bundle_sha256: str
    source_frame_sha256: str
    event_page_sha256: str
    dataset_sha256: str

    def __post_init__(self) -> None:
        for value, code in (
            (
                self.authority_bundle_sha256,
                "event_local_authority_sha_invalid",
            ),
            (
                self.source_frame_sha256,
                "event_local_source_frame_sha_invalid",
            ),
            (
                self.event_page_sha256,
                "event_local_page_sha_invalid",
            ),
            (
                self.dataset_sha256,
                "event_local_dataset_sha_invalid",
            ),
        ):
            _require_sha(value, code)


@dataclass(frozen=True)
class EventLocalDecisionPolicy:
    weights: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.weights, tuple) or not self.weights:
            raise EventLocalDecisionError("event_local_weights_empty")
        keys = tuple(key for key, _weight in self.weights)
        if (
            tuple(sorted(keys)) != keys
            or len(set(keys)) != len(keys)
            or any(
                not isinstance(key, str)
                or key not in _SAFE_NUMERIC_ADD_KINDS
                or isinstance(weight, bool)
                or not isinstance(weight, int)
                for key, weight in self.weights
            )
        ):
            raise EventLocalDecisionError("event_local_weights_invalid")

    def weight_for(self, kind: str) -> int | None:
        return dict(self.weights).get(kind)


@dataclass(frozen=True)
class EventEffectScoreContribution:
    kind: str
    value: int
    weight: int
    score: int

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "value": self.value,
            "weight": self.weight,
            "score": self.score,
        }


@dataclass(frozen=True)
class EventOptionScore:
    candidate_id: str
    option_id: str
    option_index: int
    total: int
    contributions: tuple[EventEffectScoreContribution, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "option_id": self.option_id,
            "option_index": self.option_index,
            "total": self.total,
            "contributions": [
                contribution.to_dict()
                for contribution in self.contributions
            ],
        }


@dataclass(frozen=True)
class EventLocalDecisionShadow:
    status: Literal["available", "abstain"]
    binding: EventLocalDecisionBinding
    scores: tuple[EventOptionScore, ...]
    suggested_action_id: str | None
    fallback_reason: str | None
    executable: bool = False

    def __post_init__(self) -> None:
        if self.status not in {"available", "abstain"}:
            raise EventLocalDecisionError("event_local_status_invalid")
        if not isinstance(self.binding, EventLocalDecisionBinding):
            raise EventLocalDecisionError("event_local_binding_required")
        if not isinstance(self.scores, tuple) or any(
            not isinstance(score, EventOptionScore)
            for score in self.scores
        ):
            raise EventLocalDecisionError("event_local_scores_invalid")
        if self.executable is not False:
            raise EventLocalDecisionError("event_local_must_not_execute")
        if self.status == "available":
            if (
                not isinstance(self.suggested_action_id, str)
                or not self.suggested_action_id.startswith("event.option.")
                or self.fallback_reason is not None
                or not self.scores
            ):
                raise EventLocalDecisionError(
                    "event_local_available_payload_invalid"
                )
        elif (
            self.suggested_action_id is not None
            or not isinstance(self.fallback_reason, str)
            or not self.fallback_reason
        ):
            raise EventLocalDecisionError(
                "event_local_abstain_payload_invalid"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "uma.event_local_decision_shadow.v1",
            "status": self.status,
            "authority_bundle_sha256": (
                self.binding.authority_bundle_sha256
            ),
            "source_frame_sha256": self.binding.source_frame_sha256,
            "event_page_sha256": self.binding.event_page_sha256,
            "dataset_sha256": self.binding.dataset_sha256,
            "scores": [score.to_dict() for score in self.scores],
            "suggested_action_id": self.suggested_action_id,
            "fallback_reason": self.fallback_reason,
            "executable": False,
        }


def build_event_local_decision_shadow(
    *,
    binding: EventLocalDecisionBinding,
    candidates: tuple[EventEffectCandidate, ...],
    option_count: int,
    policy: EventLocalDecisionPolicy,
) -> EventLocalDecisionShadow:
    if not isinstance(binding, EventLocalDecisionBinding):
        raise EventLocalDecisionError("event_local_binding_required")
    if not isinstance(policy, EventLocalDecisionPolicy):
        raise EventLocalDecisionError("event_local_policy_required")
    if type(option_count) is not int or option_count <= 0:
        raise EventLocalDecisionError("event_local_option_count_invalid")
    if not isinstance(candidates, tuple) or any(
        not isinstance(candidate, EventEffectCandidate)
        for candidate in candidates
    ):
        raise EventLocalDecisionError("event_local_candidates_invalid")
    if (
        len(candidates) != option_count
        or tuple(candidate.option_index for candidate in candidates)
        != tuple(range(1, option_count + 1))
    ):
        return _abstain(binding, "event_local_option_set_incomplete")
    if any(
        candidate.dataset_sha256 != binding.dataset_sha256
        for candidate in candidates
    ):
        return _abstain(
            binding,
            "event_local_dataset_binding_mismatch",
        )
    event_ids = {candidate.event_id for candidate in candidates}
    if len(event_ids) != 1:
        return _abstain(binding, "event_local_event_mismatch")
    if any(
        candidate.decision_safe is not False
        or candidate.action_eligible is not False
        or candidate.usable_for_action is not False
        for candidate in candidates
    ):
        return _abstain(binding, "event_local_candidate_authority_invalid")

    scores: list[EventOptionScore] = []
    for candidate in candidates:
        contributions: list[EventEffectScoreContribution] = []
        for effect in candidate.effects:
            payload = effect.to_dict()
            kind = payload.get("kind")
            operation = payload.get("op")
            value = payload.get("value")
            if (
                not isinstance(kind, str)
                or kind not in _SAFE_NUMERIC_ADD_KINDS
                or operation != "add"
                or isinstance(value, bool)
                or not isinstance(value, int)
            ):
                return _abstain(
                    binding,
                    "event_local_unsupported_effect",
                )
            weight = policy.weight_for(kind)
            if weight is None:
                return _abstain(binding, "event_local_weight_missing")
            contributions.append(
                EventEffectScoreContribution(
                    kind=kind,
                    value=value,
                    weight=weight,
                    score=value * weight,
                )
            )
        scores.append(
            EventOptionScore(
                candidate_id=candidate.candidate_id,
                option_id=candidate.option_id,
                option_index=candidate.option_index,
                total=sum(item.score for item in contributions),
                contributions=tuple(contributions),
            )
        )

    best_total = max(score.total for score in scores)
    best = [score for score in scores if score.total == best_total]
    if len(best) != 1:
        return _abstain(
            binding,
            "event_local_score_tie",
            scores=tuple(scores),
        )
    return EventLocalDecisionShadow(
        status="available",
        binding=binding,
        scores=tuple(scores),
        suggested_action_id=f"event.option.{best[0].option_index}",
        fallback_reason=None,
    )


def _abstain(
    binding: EventLocalDecisionBinding,
    reason: str,
    *,
    scores: tuple[EventOptionScore, ...] = (),
) -> EventLocalDecisionShadow:
    return EventLocalDecisionShadow(
        status="abstain",
        binding=binding,
        scores=scores,
        suggested_action_id=None,
        fallback_reason=reason,
    )


def _require_sha(value: object, code: str) -> None:
    if not isinstance(value, str) or _SHA_PATTERN.fullmatch(value) is None:
        raise EventLocalDecisionError(code)
