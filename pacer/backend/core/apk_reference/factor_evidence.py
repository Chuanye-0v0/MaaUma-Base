"""Factor configuration evidence over caller-bound normalized ROIs."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .evidence import ReferenceEvidence
from .sparse_fingerprint import (
    RoiBinding,
    SparseColorPoint,
    SparseFingerprint,
    SparseFingerprintConfigError,
    SparseFingerprintMatcher,
)


_KINDS = frozenset({"horse", "factor_column"})
_SUMMARY_STATUSES = frozenset({"matched", "contradiction", "abstain"})


@dataclass(frozen=True, slots=True)
class FactorObservation:
    slot_id: str
    kind: str
    evidence: ReferenceEvidence
    production_ready: bool = field(default=False, init=False)
    usable_for_action: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _validate_slot_and_kind(self.slot_id, self.kind)
        _validate_factor_evidence(self.evidence, self.kind)


@dataclass(frozen=True, slots=True)
class FactorSlotVerification:
    slot_id: str
    kind: str
    evidence: ReferenceEvidence
    production_ready: bool = field(default=False, init=False)
    usable_for_action: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _validate_slot_and_kind(self.slot_id, self.kind)
        _validate_factor_evidence(self.evidence, self.kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "kind": self.kind,
            "evidence": self.evidence.to_dict(),
            "production_ready": False,
            "usable_for_action": False,
        }


@dataclass(frozen=True, slots=True)
class FactorConfigurationVerification:
    status: str
    slot_results: tuple[FactorSlotVerification, ...]
    reasons: tuple[str, ...] = ()
    production_ready: bool = field(default=False, init=False)
    usable_for_action: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if type(self.status) is not str or self.status not in _SUMMARY_STATUSES:
            _config_fail("invalid_factor_configuration_status")
        if type(self.slot_results) is not tuple or any(
            type(result) is not FactorSlotVerification
            for result in self.slot_results
        ):
            _config_fail("invalid_factor_slot_results")
        try:
            for result in self.slot_results:
                _validate_factor_slot_verification(result)
            slot_ids = [result.slot_id for result in self.slot_results]
        except Exception:
            _config_fail("invalid_factor_slot_results")
        if len(slot_ids) != len(set(slot_ids)):
            _config_fail("duplicate_factor_slot_result")
        if type(self.reasons) is not tuple or any(
            type(reason) is not str or not reason
            for reason in self.reasons
        ):
            _config_fail("invalid_factor_configuration_reasons")
        if self.status == "matched" and (
            not self.slot_results
            or any(
                result.evidence.status != "matched"
                for result in self.slot_results
            )
        ):
            _config_fail("invalid_matched_factor_configuration")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "slot_results": [result.to_dict() for result in self.slot_results],
            "reasons": list(self.reasons),
            "production_ready": False,
            "usable_for_action": False,
        }


def _create_sealed_construction():
    token = object()

    def validate(value: object | None) -> bool:
        return value is token

    def construct(
        matcher: SparseFingerprintMatcher,
        kind: str,
        candidate_ids: frozenset[str],
    ) -> _FactorFingerprintMatcher:
        return _FactorFingerprintMatcher(
            matcher,
            kind,
            candidate_ids,
            _token=token,
        )

    return validate, construct


_validate_construction_token, _construct_factor_matcher = (
    _create_sealed_construction()
)
del _create_sealed_construction


class _FactorFingerprintMatcher:
    __slots__ = (
        "_matcher",
        "_kind",
        "_candidate_ids",
        "_frozen_candidate_ids",
        "_source_database_sha256",
        "_matcher_version",
    )

    def __init__(
        self,
        matcher: SparseFingerprintMatcher,
        kind: str,
        candidate_ids: frozenset[str],
        *,
        _token: object | None = None,
    ):
        if not _validate_construction_token(_token):
            _config_fail("sealed_factor_matcher")
        if not isinstance(matcher, SparseFingerprintMatcher):
            _config_fail("invalid_factor_matcher")
        if (
            type(matcher._source_database_sha256) is not str
            or type(matcher._matcher_version) is not str
        ):
            _config_fail("invalid_factor_provenance_text")
        if type(kind) is not str or kind not in _KINDS:
            _config_fail("invalid_factor_kind")
        if type(candidate_ids) is not frozenset or any(
            type(candidate_id) is not str or not candidate_id
            for candidate_id in candidate_ids
        ):
            _config_fail("invalid_factor_candidate_ids")
        matcher_ids = frozenset(
            candidate.candidate_id for candidate in matcher._candidates
        )
        if any(type(candidate_id) is not str for candidate_id in matcher_ids):
            _config_fail("invalid_factor_candidate_ids")
        if candidate_ids != matcher_ids:
            _config_fail("factor_candidate_index_mismatch")
        self._matcher = matcher
        self._kind = kind
        self._candidate_ids = candidate_ids
        self._frozen_candidate_ids = candidate_ids
        self._source_database_sha256 = matcher._source_database_sha256
        self._matcher_version = matcher._matcher_version

    def match(
        self,
        roi_bgr: np.ndarray,
        binding: RoiBinding,
    ) -> ReferenceEvidence:
        if not self._wrapper_text_is_exact() or not _binding_text_is_exact(
            binding
        ):
            return self._bound_abstain(
                "factor_evidence_binding_mismatch",
                binding,
                ReferenceEvidence.abstain("invalid_factor_binding"),
            )
        try:
            evidence = self._matcher.match(roi_bgr, binding)
        except Exception:
            return ReferenceEvidence.abstain("factor_matcher_failure")
        if type(evidence) is not ReferenceEvidence:
            return ReferenceEvidence.abstain("invalid_factor_match_evidence")
        if not _dependency_evidence_integrity_is_exact(evidence):
            return ReferenceEvidence.abstain("invalid_factor_match_evidence")
        if not _evidence_text_is_exact(evidence):
            return self._bound_abstain(
                "factor_evidence_binding_mismatch", binding, evidence
            )
        if not self._candidate_index_is_exact():
            return self._bound_abstain(
                "factor_candidate_index_missing", binding, evidence
            )
        if (
            evidence.candidate_id is not None
            and not self._candidate_id_is_indexed(evidence.candidate_id)
        ):
            return self._bound_abstain(
                "factor_candidate_index_missing", binding, evidence
            )
        try:
            return self._normalize_dependency_evidence(evidence, binding)
        except Exception:
            return ReferenceEvidence.abstain("invalid_factor_match_evidence")

    def _binding_matches(
        self,
        evidence: ReferenceEvidence,
        binding: RoiBinding,
    ) -> bool:
        return (
            _binding_text_is_exact(binding)
            and _evidence_text_is_exact(evidence)
            and self._wrapper_text_is_exact()
            and (
                evidence.anchor_id == binding.anchor_id
                and evidence.roi_bbox == binding.roi_bbox
                and evidence.canonical_size == binding.canonical_size
                and evidence.transform_id == binding.transform_id
                and evidence.source_frame_sha256
                == binding.source_frame_sha256
                and evidence.source_database_sha256
                == self._source_database_sha256
                and evidence.matcher_version == self._matcher_version
            )
        )

    def _wrapper_text_is_exact(self) -> bool:
        return (
            type(self._kind) is str
            and self._kind in _KINDS
            and type(self._source_database_sha256) is str
            and type(self._matcher_version) is str
        )

    def _candidate_index_is_exact(self) -> bool:
        return (
            type(self._candidate_ids) is frozenset
            and type(self._frozen_candidate_ids) is frozenset
            and self._candidate_ids == self._frozen_candidate_ids
            and all(
                type(candidate_id) is str
                and bool(candidate_id)
                and _candidate_id_is_canonical(candidate_id, self._kind)
                for candidate_id in self._candidate_ids
            )
        )

    def _candidate_id_is_indexed(self, candidate_id: Any) -> bool:
        return (
            _candidate_id_is_canonical(candidate_id, self._kind)
            and self._candidate_index_is_exact()
            and candidate_id in self._frozen_candidate_ids
        )

    def _normalize_dependency_evidence(
        self,
        evidence: ReferenceEvidence,
        binding: RoiBinding,
    ) -> ReferenceEvidence:
        if self._binding_matches(evidence, binding):
            return self._rebuild_evidence(evidence, binding)
        if evidence.status == "abstain":
            if _is_true_minimal_abstain(evidence):
                return self._rebuild_evidence(evidence, None)
            if self._partial_binding_is_consistent(evidence, binding):
                return self._rebuild_evidence(evidence, binding)
        return self._bound_abstain(
            "factor_evidence_binding_mismatch", binding, evidence
        )

    def _partial_binding_is_consistent(
        self,
        evidence: ReferenceEvidence,
        binding: RoiBinding,
    ) -> bool:
        expected = (
            binding.anchor_id,
            binding.roi_bbox,
            binding.canonical_size,
            binding.transform_id,
            self._source_database_sha256,
            binding.source_frame_sha256,
            self._matcher_version,
        )
        actual = (
            evidence.anchor_id,
            evidence.roi_bbox,
            evidence.canonical_size,
            evidence.transform_id,
            evidence.source_database_sha256,
            evidence.source_frame_sha256,
            evidence.matcher_version,
        )
        return all(
            value is None or value == wanted
            for value, wanted in zip(actual, expected)
        )

    def _rebuild_evidence(
        self,
        evidence: ReferenceEvidence,
        binding: RoiBinding | None,
    ) -> ReferenceEvidence:
        fields = (
            {
                "anchor_id": None,
                "roi_bbox": None,
                "canonical_size": None,
                "transform_id": None,
                "source_database_sha256": None,
                "source_frame_sha256": None,
                "matcher_version": None,
            }
            if binding is None
            else {
                "anchor_id": binding.anchor_id,
                "roi_bbox": binding.roi_bbox,
                "canonical_size": binding.canonical_size,
                "transform_id": binding.transform_id,
                "source_database_sha256": self._source_database_sha256,
                "source_frame_sha256": binding.source_frame_sha256,
                "matcher_version": self._matcher_version,
            }
        )
        try:
            return ReferenceEvidence(
                status=evidence.status,
                candidate_id=evidence.candidate_id,
                score=evidence.score,
                runner_up_margin=evidence.runner_up_margin,
                usable_for_action=False,
                reasons=evidence.reasons,
                **fields,
            )
        except Exception:
            return ReferenceEvidence.abstain("invalid_factor_match_evidence")

    def _bound_abstain(
        self,
        reason: str,
        binding: RoiBinding,
        evidence: ReferenceEvidence,
    ) -> ReferenceEvidence:
        safe_matcher_version = (
            self._matcher_version
            if type(self._matcher_version) is str
            else None
        )
        if not _binding_text_is_exact(binding) or (
            type(self._source_database_sha256) is not str
        ):
            return ReferenceEvidence.abstain(
                reason, matcher_version=safe_matcher_version
            )
        try:
            return ReferenceEvidence.abstain(
                reason,
                candidate_id=(
                    evidence.candidate_id
                    if self._candidate_id_is_indexed(evidence.candidate_id)
                    else None
                ),
                score=evidence.score,
                runner_up_margin=evidence.runner_up_margin,
                anchor_id=binding.anchor_id,
                roi_bbox=binding.roi_bbox,
                canonical_size=binding.canonical_size,
                transform_id=binding.transform_id,
                source_database_sha256=self._source_database_sha256,
                source_frame_sha256=binding.source_frame_sha256,
                matcher_version=self._matcher_version,
            )
        except Exception:
            return ReferenceEvidence.abstain(
                reason, matcher_version=safe_matcher_version
            )


def build_horse_factor_matcher(
    *,
    records: Mapping[str, Any] | Iterable[tuple[str, Any]],
    acceptance_threshold: float | int,
    margin_threshold: float | int,
    matcher_version: str,
    source_database_sha256: str,
    allowed_transform_ids: Iterable[str],
) -> _FactorFingerprintMatcher:
    return _build_factor_matcher(
        kind="horse",
        records=records,
        acceptance_threshold=acceptance_threshold,
        margin_threshold=margin_threshold,
        matcher_version=matcher_version,
        source_database_sha256=source_database_sha256,
        allowed_transform_ids=allowed_transform_ids,
    )


def build_factor_column_matcher(
    *,
    records: Mapping[str, Any] | Iterable[tuple[str, Any]],
    acceptance_threshold: float | int,
    margin_threshold: float | int,
    matcher_version: str,
    source_database_sha256: str,
    allowed_transform_ids: Iterable[str],
) -> _FactorFingerprintMatcher:
    return _build_factor_matcher(
        kind="factor_column",
        records=records,
        acceptance_threshold=acceptance_threshold,
        margin_threshold=margin_threshold,
        matcher_version=matcher_version,
        source_database_sha256=source_database_sha256,
        allowed_transform_ids=allowed_transform_ids,
    )


def match_horse_factor_candidate(
    *,
    matcher: _FactorFingerprintMatcher,
    roi_bgr: np.ndarray,
    binding: RoiBinding,
) -> ReferenceEvidence:
    return _match_factor_candidate(
        expected_kind="horse",
        matcher=matcher,
        roi_bgr=roi_bgr,
        binding=binding,
    )


def match_factor_column_candidate(
    *,
    matcher: _FactorFingerprintMatcher,
    roi_bgr: np.ndarray,
    binding: RoiBinding,
) -> ReferenceEvidence:
    return _match_factor_candidate(
        expected_kind="factor_column",
        matcher=matcher,
        roi_bgr=roi_bgr,
        binding=binding,
    )


def verify_factor_configuration(
    *,
    observations: Iterable[FactorObservation],
    allowed_ids: Mapping[str, Iterable[tuple[str, str]]],
) -> FactorConfigurationVerification:
    try:
        observation_snapshot = _snapshot_observations(observations)
    except Exception:
        return _safe_configuration_abstain("invalid_factor_observations")
    try:
        allowed_snapshot = _snapshot_allowed_ids(allowed_ids)
    except Exception:
        return _safe_configuration_abstain("invalid_factor_allowlist")

    if not observation_snapshot and not allowed_snapshot:
        return _safe_configuration_abstain("empty_factor_configuration")

    observations_by_slot = {
        observation.slot_id: observation
        for observation in observation_snapshot
    }
    if len(observations_by_slot) != len(observation_snapshot):
        return _safe_configuration_abstain("duplicate_factor_observation_slot")

    results: list[FactorSlotVerification] = []
    for slot_id, allowed_pairs in allowed_snapshot:
        allowed_kind = allowed_pairs[0][0]
        observation = observations_by_slot.pop(slot_id, None)
        if observation is None:
            results.append(
                FactorSlotVerification(
                    slot_id=slot_id,
                    kind=allowed_kind,
                    evidence=ReferenceEvidence.abstain(
                        "missing_factor_observation"
                    ),
                )
            )
            continue
        results.append(
            _verify_observation(observation, allowed_kind, allowed_pairs)
        )

    for observation in observations_by_slot.values():
        evidence = observation.evidence
        if evidence.status == "matched":
            evidence = _contradiction(
                evidence, "factor_slot_allowlist_missing"
            )
        results.append(
            FactorSlotVerification(
                slot_id=observation.slot_id,
                kind=observation.kind,
                evidence=evidence,
            )
        )

    result_tuple = tuple(results)
    if any(result.evidence.status == "contradiction" for result in result_tuple):
        status = "contradiction"
        reasons = ("factor_configuration_contradiction",)
    elif result_tuple and all(
        result.evidence.status == "matched" for result in result_tuple
    ):
        status = "matched"
        reasons = ("factor_configuration_verified_not_production_ready",)
    else:
        status = "abstain"
        reasons = ("factor_configuration_incomplete_or_uncertain",)
    return FactorConfigurationVerification(
        status=status,
        slot_results=result_tuple,
        reasons=reasons,
    )


def _build_factor_matcher(
    *,
    kind: str,
    records: Mapping[str, Any] | Iterable[tuple[str, Any]],
    acceptance_threshold: float | int,
    margin_threshold: float | int,
    matcher_version: str,
    source_database_sha256: str,
    allowed_transform_ids: Iterable[str],
) -> _FactorFingerprintMatcher:
    if (
        type(matcher_version) is not str
        or type(source_database_sha256) is not str
    ):
        _config_fail("invalid_factor_provenance_text")
    candidates: list[SparseFingerprint] = []
    candidate_ids: set[str] = set()
    for item in _snapshot_records(records):
        raw_id, record = _validated_record_pair(item)
        candidate_id = _encode_candidate_id(kind, raw_id)
        if candidate_id in candidate_ids:
            _config_fail("duplicate_factor_candidate_id")
        candidates.append(
            SparseFingerprint(
                candidate_id=candidate_id,
                points=_validated_record_points(record),
            )
        )
        candidate_ids.add(candidate_id)
    matcher = SparseFingerprintMatcher(
        candidates=candidates,
        acceptance_threshold=acceptance_threshold,
        margin_threshold=margin_threshold,
        matcher_version=matcher_version,
        source_database_sha256=source_database_sha256,
        allowed_transform_ids=allowed_transform_ids,
    )
    return _construct_factor_matcher(matcher, kind, frozenset(candidate_ids))


def _match_factor_candidate(
    *,
    expected_kind: str,
    matcher: Any,
    roi_bgr: np.ndarray,
    binding: RoiBinding,
) -> ReferenceEvidence:
    if type(matcher) is not _FactorFingerprintMatcher:
        return ReferenceEvidence.abstain("invalid_factor_matcher")
    if type(matcher._kind) is not str or matcher._kind != expected_kind:
        return ReferenceEvidence.abstain("factor_matcher_kind_mismatch")
    try:
        evidence = matcher.match(roi_bgr, binding)
    except Exception:
        return ReferenceEvidence.abstain("factor_matcher_failure")
    if type(evidence) is not ReferenceEvidence:
        return ReferenceEvidence.abstain("invalid_factor_match_evidence")
    return evidence


def _snapshot_records(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        _config_fail("invalid_factor_records")
    try:
        if isinstance(value, Mapping):
            return tuple(value.items())
        return tuple(value)
    except Exception:
        _config_fail("invalid_factor_records")


def _validated_record_pair(item: Any) -> tuple[str, Mapping[str, Any]]:
    if type(item) not in (list, tuple) or len(item) != 2:
        _config_fail("invalid_factor_records")
    raw_id, record = item
    if type(raw_id) is not str or not raw_id:
        _config_fail("invalid_factor_records")
    if not isinstance(record, Mapping):
        _config_fail("invalid_factor_records")
    return raw_id, record


def _validated_record_points(
    record: Mapping[str, Any],
) -> tuple[SparseColorPoint, ...]:
    try:
        points = record["points"]
        point_count = record["point_count"]
    except Exception:
        _config_fail("invalid_factor_records")
    if type(points) not in (list, tuple) or not points:
        _config_fail("invalid_factor_points")
    if type(point_count) is not int or point_count != len(points):
        _config_fail("factor_point_count_mismatch")

    result: list[SparseColorPoint] = []
    try:
        for point in points:
            if not isinstance(point, Mapping):
                _config_fail("invalid_factor_points")
            color = point["color"]
            if type(color) is not str:
                _config_fail("invalid_factor_points")
            result.append(
                SparseColorPoint(
                    x=point["x"],
                    y=point["y"],
                    color_hex=color,
                )
            )
    except SparseFingerprintConfigError:
        _config_fail("invalid_factor_points")
    except Exception:
        _config_fail("invalid_factor_points")
    return tuple(result)


def _snapshot_observations(value: Any) -> tuple[FactorObservation, ...]:
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        _config_fail("invalid_factor_observations")
    try:
        snapshot = tuple(value)
    except Exception:
        _config_fail("invalid_factor_observations")
    if any(type(observation) is not FactorObservation for observation in snapshot):
        _config_fail("invalid_factor_observations")
    if any(
        observation.production_ready is not False
        or observation.usable_for_action is not False
        or type(observation.evidence) is not ReferenceEvidence
        or observation.evidence.usable_for_action is not False
        for observation in snapshot
    ):
        _config_fail("invalid_factor_observations")
    try:
        for observation in snapshot:
            _validate_slot_and_kind(observation.slot_id, observation.kind)
            _validate_factor_evidence(observation.evidence, observation.kind)
    except Exception:
        _config_fail("invalid_factor_observations")
    return snapshot


def _snapshot_allowed_ids(
    value: Any,
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    if not isinstance(value, Mapping):
        _config_fail("invalid_factor_allowlist")
    try:
        items = tuple(value.items())
    except Exception:
        _config_fail("invalid_factor_allowlist")

    result: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for item in items:
        if type(item) not in (list, tuple) or len(item) != 2:
            _config_fail("invalid_factor_allowlist")
        slot_id, pairs = item
        if type(slot_id) is not str or not slot_id:
            _config_fail("invalid_factor_allowlist")
        if type(pairs) not in (list, tuple) or not pairs:
            _config_fail("invalid_factor_allowlist")
        normalized: list[tuple[str, str]] = []
        for pair in pairs:
            if type(pair) not in (list, tuple) or len(pair) != 2:
                _config_fail("invalid_factor_allowlist")
            kind, raw_id = pair
            if (
                type(kind) is not str
                or kind not in _KINDS
                or type(raw_id) is not str
                or not raw_id
            ):
                _config_fail("invalid_factor_allowlist")
            normalized.append((kind, raw_id))
        kinds = {kind for kind, _ in normalized}
        if len(kinds) != 1:
            _config_fail("mixed_factor_kinds_in_slot")
        result.append((slot_id, tuple(normalized)))
    slot_ids = [slot_id for slot_id, _ in result]
    if len(slot_ids) != len(set(slot_ids)):
        _config_fail("duplicate_factor_allowlist_slot")
    return tuple(result)


def _verify_observation(
    observation: FactorObservation,
    allowed_kind: str,
    allowed_pairs: tuple[tuple[str, str], ...],
) -> FactorSlotVerification:
    evidence = observation.evidence
    if evidence.status == "matched":
        if observation.kind != allowed_kind:
            evidence = _contradiction(
                evidence, "factor_observation_kind_not_allowed"
            )
        else:
            allowed_candidates = {
                _encode_candidate_id(kind, raw_id)
                for kind, raw_id in allowed_pairs
            }
            if evidence.candidate_id in allowed_candidates:
                evidence = _matched_allowed(evidence)
            else:
                evidence = _contradiction(
                    evidence, "factor_candidate_outside_allowlist"
                )
    return FactorSlotVerification(
        slot_id=observation.slot_id,
        kind=observation.kind,
        evidence=evidence,
    )


def _matched_allowed(evidence: ReferenceEvidence) -> ReferenceEvidence:
    return ReferenceEvidence.matched(
        candidate_id=evidence.candidate_id,
        score=evidence.score,
        runner_up_margin=evidence.runner_up_margin,
        reasons=("configuration_candidate_allowed_not_selection",),
        **_bound_fields(evidence),
    )


def _contradiction(
    evidence: ReferenceEvidence,
    reason: str,
) -> ReferenceEvidence:
    return ReferenceEvidence.contradiction(
        candidate_id=evidence.candidate_id,
        score=evidence.score,
        runner_up_margin=evidence.runner_up_margin,
        reasons=(reason,),
        **_bound_fields(evidence),
    )


def _safe_configuration_abstain(
    reason: str,
) -> FactorConfigurationVerification:
    return FactorConfigurationVerification(
        status="abstain",
        slot_results=(),
        reasons=(reason,),
    )


def _validate_slot_and_kind(slot_id: Any, kind: Any) -> None:
    if type(slot_id) is not str or not slot_id:
        _config_fail("invalid_factor_slot_id")
    if type(kind) is not str or kind not in _KINDS:
        _config_fail("invalid_factor_kind")


def _validate_factor_slot_verification(result: Any) -> None:
    if (
        type(result) is not FactorSlotVerification
        or result.production_ready is not False
        or result.usable_for_action is not False
    ):
        _config_fail("invalid_factor_slot_results")
    _validate_slot_and_kind(result.slot_id, result.kind)
    _validate_factor_evidence(result.evidence, result.kind)


def _validate_factor_evidence(evidence: Any, kind: str) -> None:
    if type(evidence) is not ReferenceEvidence:
        _config_fail("invalid_factor_evidence")
    if not _dependency_evidence_integrity_is_exact(evidence):
        _config_fail("invalid_factor_evidence")
    if (
        evidence.candidate_id is not None
        and type(evidence.candidate_id) is not str
    ):
        _config_fail("invalid_factor_candidate_id")
    if not _evidence_text_is_exact(evidence):
        _config_fail("invalid_factor_evidence")
    if evidence.candidate_id is None:
        return
    candidate_kind, _ = _decode_candidate_id(evidence.candidate_id)
    if candidate_kind != kind:
        _config_fail("invalid_factor_candidate_id")


def _encode_candidate_id(kind: str, raw_id: str) -> str:
    return json.dumps([kind, raw_id], ensure_ascii=False, separators=(",", ":"))


def _decode_candidate_id(candidate_id: Any) -> tuple[str, str]:
    if type(candidate_id) is not str:
        _config_fail("invalid_factor_candidate_id")
    try:
        decoded = json.loads(candidate_id)
    except Exception:
        _config_fail("invalid_factor_candidate_id")
    if type(decoded) is not list or len(decoded) != 2:
        _config_fail("invalid_factor_candidate_id")
    candidate_kind, raw_id = decoded
    if (
        type(candidate_kind) is not str
        or candidate_kind not in _KINDS
        or type(raw_id) is not str
        or not raw_id
        or _encode_candidate_id(candidate_kind, raw_id) != candidate_id
    ):
        _config_fail("invalid_factor_candidate_id")
    return candidate_kind, raw_id


def _candidate_id_is_canonical(candidate_id: Any, kind: str) -> bool:
    try:
        candidate_kind, _ = _decode_candidate_id(candidate_id)
    except SparseFingerprintConfigError:
        return False
    return candidate_kind == kind


def _bound_fields(evidence: ReferenceEvidence) -> dict[str, Any]:
    return {
        "anchor_id": evidence.anchor_id,
        "roi_bbox": evidence.roi_bbox,
        "canonical_size": evidence.canonical_size,
        "transform_id": evidence.transform_id,
        "source_database_sha256": evidence.source_database_sha256,
        "source_frame_sha256": evidence.source_frame_sha256,
        "matcher_version": evidence.matcher_version,
    }


def _dependency_evidence_integrity_is_exact(
    evidence: ReferenceEvidence,
) -> bool:
    try:
        return (
            type(evidence.status) is str
            and evidence.status
            in {"matched", "ambiguous", "contradiction", "abstain"}
            and evidence.usable_for_action is False
            and type(evidence.reasons) is tuple
            and all(type(reason) is str and bool(reason) for reason in evidence.reasons)
            and _optional_exact_int_tuple(evidence.roi_bbox, 4)
            and _optional_exact_int_tuple(evidence.canonical_size, 2)
            and (
                evidence.score is None
                or type(evidence.score) in (int, float)
            )
            and (
                evidence.runner_up_margin is None
                or type(evidence.runner_up_margin) in (int, float)
            )
        )
    except Exception:
        return False


def _optional_exact_int_tuple(value: Any, size: int) -> bool:
    return value is None or (
        type(value) is tuple
        and len(value) == size
        and all(type(item) is int for item in value)
    )


def _is_true_minimal_abstain(evidence: ReferenceEvidence) -> bool:
    return evidence.status == "abstain" and all(
        value is None
        for value in (
            evidence.candidate_id,
            evidence.anchor_id,
            evidence.roi_bbox,
            evidence.canonical_size,
            evidence.transform_id,
            evidence.source_database_sha256,
            evidence.source_frame_sha256,
            evidence.matcher_version,
        )
    )


def _binding_text_is_exact(binding: Any) -> bool:
    return isinstance(binding, RoiBinding) and all(
        type(value) is str
        for value in (
            binding.anchor_id,
            binding.transform_id,
            binding.source_frame_sha256,
        )
    )


def _evidence_text_is_exact(evidence: ReferenceEvidence) -> bool:
    return all(
        value is None or type(value) is str
        for value in (
            evidence.status,
            evidence.candidate_id,
            evidence.anchor_id,
            evidence.transform_id,
            evidence.source_database_sha256,
            evidence.source_frame_sha256,
            evidence.matcher_version,
            *evidence.reasons,
        )
    )


def _config_fail(reason: str) -> None:
    raise SparseFingerprintConfigError(reason) from None
