"""Action-incapable evidence records for APK reference matching."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping


_STATUSES = frozenset({"matched", "ambiguous", "abstain", "contradiction"})
_SHA256_PATTERN = re.compile(r"[0-9A-Fa-f]{64}\Z")
_EVIDENCE_FIELDS = frozenset(
    {
        "status",
        "candidate_id",
        "score",
        "runner_up_margin",
        "anchor_id",
        "roi_bbox",
        "canonical_size",
        "transform_id",
        "source_database_sha256",
        "source_frame_sha256",
        "matcher_version",
        "usable_for_action",
        "reasons",
    }
)


class ReferenceEvidenceError(ValueError):
    """Evidence violates the action-incapable record contract."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ReferenceEvidence:
    status: str
    candidate_id: str | None = None
    score: float | None = None
    runner_up_margin: float | None = None
    anchor_id: str | None = None
    roi_bbox: tuple[int, int, int, int] | None = None
    canonical_size: tuple[int, int] | None = None
    transform_id: str | None = None
    source_database_sha256: str | None = None
    source_frame_sha256: str | None = None
    matcher_version: str | None = None
    usable_for_action: bool = False
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or self.status not in _STATUSES:
            _fail("invalid_evidence_status")
        if self.usable_for_action is not False:
            _fail("action_capability_forbidden")

        roi_bbox = _snapshot_sequence(self.roi_bbox, "invalid_roi_bbox")
        canonical_size = _snapshot_sequence(
            self.canonical_size, "invalid_canonical_size"
        )
        reasons = _snapshot_sequence(self.reasons, "invalid_evidence_reason")
        object.__setattr__(self, "roi_bbox", roi_bbox)
        object.__setattr__(self, "canonical_size", canonical_size)
        object.__setattr__(self, "reasons", reasons)

        _validate_status_requirements(self)
        _validate_reasons(reasons)
        _validate_optional_identifier(self.candidate_id, "invalid_candidate_id")

        required_for_nonabstain = (
            "anchor_id",
            "roi_bbox",
            "canonical_size",
            "transform_id",
            "source_database_sha256",
            "source_frame_sha256",
            "matcher_version",
        )
        if self.status != "abstain" and any(
            getattr(self, field) is None for field in required_for_nonabstain
        ):
            _fail("missing_evidence_field")

        _validate_optional_identifier(
            self.anchor_id, "invalid_evidence_identifier"
        )
        _validate_optional_identifier(
            self.transform_id, "invalid_evidence_identifier"
        )
        _validate_optional_identifier(
            self.matcher_version, "invalid_evidence_identifier"
        )
        _validate_optional_sha256(self.source_database_sha256)
        _validate_optional_sha256(self.source_frame_sha256)
        _validate_optional_roi_bbox(roi_bbox)
        _validate_optional_canonical_size(canonical_size)

        score = _validated_unit_interval(
            self.score, "invalid_evidence_score"
        )
        margin = _validated_unit_interval(
            self.runner_up_margin, "invalid_runner_up_margin"
        )
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "runner_up_margin", margin)

    @classmethod
    def matched(
        cls,
        candidate_id: str | None,
        score: float | int | None,
        *,
        anchor_id: str | None,
        roi_bbox: tuple[int, int, int, int] | list[int] | None,
        canonical_size: tuple[int, int] | list[int] | None,
        transform_id: str | None,
        source_database_sha256: str | None,
        source_frame_sha256: str | None,
        matcher_version: str | None,
        runner_up_margin: float | int | None = None,
        reasons: tuple[str, ...] | list[str] = (),
    ) -> ReferenceEvidence:
        return cls(
            status="matched",
            candidate_id=candidate_id,
            score=score,
            runner_up_margin=runner_up_margin,
            anchor_id=anchor_id,
            roi_bbox=roi_bbox,
            canonical_size=canonical_size,
            transform_id=transform_id,
            source_database_sha256=source_database_sha256,
            source_frame_sha256=source_frame_sha256,
            matcher_version=matcher_version,
            usable_for_action=False,
            reasons=reasons,
        )

    @classmethod
    def ambiguous(
        cls,
        *,
        anchor_id: str | None,
        roi_bbox: tuple[int, int, int, int] | list[int] | None,
        canonical_size: tuple[int, int] | list[int] | None,
        transform_id: str | None,
        source_database_sha256: str | None,
        source_frame_sha256: str | None,
        matcher_version: str | None,
        reasons: tuple[str, ...] | list[str] = (),
        candidate_id: str | None = None,
        score: float | int | None = None,
        runner_up_margin: float | int | None = None,
    ) -> ReferenceEvidence:
        return cls(
            status="ambiguous",
            candidate_id=candidate_id,
            score=score,
            runner_up_margin=runner_up_margin,
            anchor_id=anchor_id,
            roi_bbox=roi_bbox,
            canonical_size=canonical_size,
            transform_id=transform_id,
            source_database_sha256=source_database_sha256,
            source_frame_sha256=source_frame_sha256,
            matcher_version=matcher_version,
            usable_for_action=False,
            reasons=reasons,
        )

    @classmethod
    def contradiction(
        cls,
        *,
        anchor_id: str | None,
        roi_bbox: tuple[int, int, int, int] | list[int] | None,
        canonical_size: tuple[int, int] | list[int] | None,
        transform_id: str | None,
        source_database_sha256: str | None,
        source_frame_sha256: str | None,
        matcher_version: str | None,
        reasons: tuple[str, ...] | list[str] = (),
        candidate_id: str | None = None,
        score: float | int | None = None,
        runner_up_margin: float | int | None = None,
    ) -> ReferenceEvidence:
        return cls(
            status="contradiction",
            candidate_id=candidate_id,
            score=score,
            runner_up_margin=runner_up_margin,
            anchor_id=anchor_id,
            roi_bbox=roi_bbox,
            canonical_size=canonical_size,
            transform_id=transform_id,
            source_database_sha256=source_database_sha256,
            source_frame_sha256=source_frame_sha256,
            matcher_version=matcher_version,
            usable_for_action=False,
            reasons=reasons,
        )

    @classmethod
    def abstain(
        cls,
        reason: str,
        *,
        candidate_id: str | None = None,
        score: float | int | None = None,
        runner_up_margin: float | int | None = None,
        anchor_id: str | None = None,
        roi_bbox: tuple[int, int, int, int] | list[int] | None = None,
        canonical_size: tuple[int, int] | list[int] | None = None,
        transform_id: str | None = None,
        source_database_sha256: str | None = None,
        source_frame_sha256: str | None = None,
        matcher_version: str | None = None,
    ) -> ReferenceEvidence:
        return cls(
            status="abstain",
            candidate_id=candidate_id,
            score=score,
            runner_up_margin=runner_up_margin,
            anchor_id=anchor_id,
            roi_bbox=roi_bbox,
            canonical_size=canonical_size,
            transform_id=transform_id,
            source_database_sha256=source_database_sha256,
            source_frame_sha256=source_frame_sha256,
            matcher_version=matcher_version,
            usable_for_action=False,
            reasons=(reason,),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "candidate_id": self.candidate_id,
            "score": self.score,
            "runner_up_margin": self.runner_up_margin,
            "anchor_id": self.anchor_id,
            "roi_bbox": list(self.roi_bbox) if self.roi_bbox is not None else None,
            "canonical_size": (
                list(self.canonical_size)
                if self.canonical_size is not None
                else None
            ),
            "transform_id": self.transform_id,
            "source_database_sha256": self.source_database_sha256,
            "source_frame_sha256": self.source_frame_sha256,
            "matcher_version": self.matcher_version,
            "usable_for_action": self.usable_for_action,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> ReferenceEvidence:
        if not isinstance(payload, Mapping):
            _fail("invalid_evidence_payload")
        if any(field not in _EVIDENCE_FIELDS for field in payload):
            _fail("unknown_evidence_field")
        return cls(
            status=payload.get("status"),
            candidate_id=payload.get("candidate_id"),
            score=payload.get("score"),
            runner_up_margin=payload.get("runner_up_margin"),
            anchor_id=payload.get("anchor_id"),
            roi_bbox=payload.get("roi_bbox"),
            canonical_size=payload.get("canonical_size"),
            transform_id=payload.get("transform_id"),
            source_database_sha256=payload.get("source_database_sha256"),
            source_frame_sha256=payload.get("source_frame_sha256"),
            matcher_version=payload.get("matcher_version"),
            usable_for_action=payload.get("usable_for_action", False),
            reasons=payload.get("reasons", ()),
        )


def _validate_status_requirements(evidence: ReferenceEvidence) -> None:
    if evidence.status == "matched":
        if (
            not isinstance(evidence.candidate_id, str)
            or not evidence.candidate_id.strip()
        ):
            _fail("matched_candidate_required")
        if evidence.score is None:
            _fail("matched_score_required")
    elif evidence.status == "ambiguous" and not _has_nonempty_reason(
        evidence.reasons
    ):
        _fail("ambiguous_reason_required")
    elif evidence.status == "contradiction" and not _has_nonempty_reason(
        evidence.reasons
    ):
        _fail("contradiction_reason_required")
    elif evidence.status == "abstain" and not _has_nonempty_reason(
        evidence.reasons
    ):
        _fail("abstain_reason_required")


def _has_nonempty_reason(reasons: tuple[Any, ...] | None) -> bool:
    return bool(reasons) and any(
        isinstance(reason, str) and bool(reason.strip())
        for reason in reasons
    )


def _snapshot_sequence(value: Any, reason: str) -> tuple[Any, ...] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        _fail(reason)
    return tuple(value)


def _validate_reasons(reasons: tuple[Any, ...] | None) -> None:
    if reasons is None:
        _fail("invalid_evidence_reason")
    if any(
        not isinstance(reason, str) or not reason.strip()
        for reason in reasons
    ):
        _fail("invalid_evidence_reason")


def _validate_optional_identifier(value: Any, reason: str) -> None:
    if value is not None and (
        not isinstance(value, str) or not value.strip()
    ):
        _fail(reason)


def _validate_optional_sha256(value: Any) -> None:
    if value is not None and (
        not isinstance(value, str)
        or _SHA256_PATTERN.fullmatch(value) is None
    ):
        _fail("invalid_evidence_sha256")


def _validate_optional_roi_bbox(value: tuple[Any, ...] | None) -> None:
    if value is None:
        return
    if (
        len(value) != 4
        or any(type(coordinate) is not int for coordinate in value)
        or any(coordinate < 0 for coordinate in value)
        or value[2] <= value[0]
        or value[3] <= value[1]
    ):
        _fail("invalid_roi_bbox")


def _validate_optional_canonical_size(value: tuple[Any, ...] | None) -> None:
    if value is None:
        return
    if (
        len(value) != 2
        or any(type(dimension) is not int for dimension in value)
        or any(dimension <= 0 for dimension in value)
    ):
        _fail("invalid_canonical_size")


def _validated_unit_interval(value: Any, reason: str) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 1
        or not math.isfinite(value)
    ):
        _fail(reason)
    return float(value)


def _fail(reason: str) -> None:
    raise ReferenceEvidenceError(reason)
