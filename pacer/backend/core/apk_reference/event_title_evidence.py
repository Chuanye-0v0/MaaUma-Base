"""Event-title candidate evidence over a caller-bound normalized ROI."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from types import MappingProxyType
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


_COLOR_PATTERN = re.compile(r"[0-9A-Fa-f]{6}\Z")
_EVENT_LABELS = frozenset({"red", "blue"})


def _create_sealed_construction():
    token = object()

    def validate(value: object | None) -> bool:
        return value is token

    def construct(
        matcher: SparseFingerprintMatcher,
        event_keys_by_candidate_id: Mapping[str, str],
    ) -> _EventTitleEvidenceMatcher:
        return _EventTitleEvidenceMatcher(
            matcher,
            event_keys_by_candidate_id,
            _token=token,
        )

    return validate, construct


_validate_construction_token, _construct_event_title_matcher = (
    _create_sealed_construction()
)
del _create_sealed_construction


class _EventTitleEvidenceMatcher:
    """Thin event-title wrapper around the generic sparse matcher."""

    __slots__ = (
        "_matcher",
        "_event_keys_by_candidate_id",
        "_candidate_ids",
        "_frozen_candidate_items",
        "_source_database_sha256",
        "_matcher_version",
    )

    def __init__(
        self,
        matcher: SparseFingerprintMatcher,
        event_keys_by_candidate_id: Mapping[str, str],
        *,
        _token: object | None = None,
    ):
        if not _validate_construction_token(_token):
            _config_fail("sealed_event_title_matcher")
        if not isinstance(matcher, SparseFingerprintMatcher):
            _config_fail("invalid_event_title_matcher")
        if (
            type(matcher._source_database_sha256) is not str
            or type(matcher._matcher_version) is not str
        ):
            _config_fail("invalid_event_title_provenance_text")
        candidate_map = _validated_candidate_map(event_keys_by_candidate_id)
        candidate_ids = {candidate.candidate_id for candidate in matcher._candidates}
        if any(type(candidate_id) is not str for candidate_id in candidate_ids):
            _config_fail("invalid_event_title_candidate_map")
        if set(candidate_map) != candidate_ids:
            _config_fail("event_title_candidate_mapping_mismatch")
        if any(
            not _event_candidate_identity_is_canonical(candidate_id, event_key)
            for candidate_id, event_key in candidate_map.items()
        ):
            _config_fail("invalid_event_title_candidate_map")
        self._matcher = matcher
        self._event_keys_by_candidate_id = MappingProxyType(candidate_map)
        self._candidate_ids = frozenset(candidate_ids)
        self._frozen_candidate_items = tuple(sorted(candidate_map.items()))
        self._source_database_sha256 = matcher._source_database_sha256
        self._matcher_version = matcher._matcher_version

    def match(
        self,
        roi_bgr: np.ndarray,
        binding: RoiBinding,
        *,
        ocr_title: str | None = None,
    ) -> ReferenceEvidence:
        if not self._wrapper_text_is_exact() or not _binding_text_is_exact(
            binding
        ):
            return self._bound_abstain(
                "event_title_evidence_binding_mismatch",
                binding,
                ReferenceEvidence.abstain("invalid_event_title_binding"),
            )
        try:
            evidence = self._matcher.match(roi_bgr, binding)
        except Exception:
            return ReferenceEvidence.abstain("event_title_matcher_failure")
        if type(evidence) is not ReferenceEvidence:
            return ReferenceEvidence.abstain("invalid_event_title_evidence")
        if not _dependency_evidence_integrity_is_exact(evidence):
            return ReferenceEvidence.abstain("invalid_event_title_evidence")
        if not _evidence_text_is_exact(evidence):
            return self._bound_abstain(
                "event_title_evidence_binding_mismatch",
                binding,
                evidence,
            )
        if not self._candidate_map_is_valid():
            return self._bound_abstain(
                "event_title_candidate_mapping_missing",
                binding,
                evidence,
            )
        if (
            evidence.candidate_id is not None
            and not self._candidate_id_is_indexed(evidence.candidate_id)
        ):
            return self._bound_abstain(
                "event_title_candidate_mapping_missing",
                binding,
                evidence,
            )
        try:
            evidence = self._normalize_dependency_evidence(evidence, binding)
        except Exception:
            return ReferenceEvidence.abstain("invalid_event_title_evidence")
        if evidence.status != "matched":
            return evidence

        try:
            event_key = self._event_keys_by_candidate_id[evidence.candidate_id]
        except Exception:
            return self._bound_abstain(
                "event_title_candidate_mapping_missing",
                binding,
                evidence,
            )
        if type(event_key) is not str or not event_key:
            return self._bound_abstain(
                "event_title_candidate_mapping_missing",
                binding,
                evidence,
            )
        if ocr_title is None:
            return evidence
        if type(ocr_title) is str and ocr_title == event_key:
            return evidence
        if type(ocr_title) is not str:
            return ReferenceEvidence.abstain(
                "invalid_event_title_ocr",
                candidate_id=evidence.candidate_id,
                score=evidence.score,
                runner_up_margin=evidence.runner_up_margin,
                **_bound_fields(evidence),
            )
        return ReferenceEvidence.abstain(
            "event_title_ocr_contradiction",
            candidate_id=evidence.candidate_id,
            score=evidence.score,
            runner_up_margin=evidence.runner_up_margin,
            **_bound_fields(evidence),
        )

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
            type(self._source_database_sha256) is str
            and type(self._matcher_version) is str
        )

    def _candidate_map_is_valid(self) -> bool:
        try:
            items = tuple(self._event_keys_by_candidate_id.items())
        except Exception:
            return False
        if (
            type(self._candidate_ids) is not frozenset
            or type(self._frozen_candidate_items) is not tuple
            or any(type(item) is not tuple or len(item) != 2 for item in items)
        ):
            return False
        if any(
            not _event_candidate_identity_is_canonical(candidate_id, event_key)
            for candidate_id, event_key in items
        ):
            return False
        return (
            tuple(sorted(items)) == self._frozen_candidate_items
            and frozenset(candidate_id for candidate_id, _ in items)
            == self._candidate_ids
        )

    def _candidate_id_is_indexed(self, candidate_id: Any) -> bool:
        return (
            type(candidate_id) is str
            and self._candidate_map_is_valid()
            and candidate_id in self._candidate_ids
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
            "event_title_evidence_binding_mismatch",
            binding,
            evidence,
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
        return all(value is None or value == wanted for value, wanted in zip(actual, expected))

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
            return ReferenceEvidence.abstain("invalid_event_title_evidence")

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
                reason,
                matcher_version=safe_matcher_version,
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
                reason,
                matcher_version=safe_matcher_version,
            )


def build_event_title_matcher(
    *,
    event_records: Mapping[str, Any] | Iterable[tuple[str, Any]],
    acceptance_threshold: float | int,
    margin_threshold: float | int,
    matcher_version: str,
    source_database_sha256: str,
    allowed_transform_ids: Iterable[str],
) -> _EventTitleEvidenceMatcher:
    """Validate event fingerprints and build a configured title matcher."""

    if (
        type(matcher_version) is not str
        or type(source_database_sha256) is not str
    ):
        _config_fail("invalid_event_title_provenance_text")
    candidates: list[SparseFingerprint] = []
    event_keys_by_candidate_id: dict[str, str] = {}
    for item in _snapshot_event_records(event_records):
        event_key, record = _validated_event_pair(item)
        label, fingerprint = _validated_event_record(record)
        candidate_id = json.dumps(
            [event_key, label], ensure_ascii=False, separators=(",", ":")
        )
        if candidate_id in event_keys_by_candidate_id:
            _config_fail("duplicate_event_title_candidate_id")
        candidates.append(
            SparseFingerprint(
                candidate_id=candidate_id,
                points=_validated_points(fingerprint),
            )
        )
        event_keys_by_candidate_id[candidate_id] = event_key

    matcher = SparseFingerprintMatcher(
        candidates=candidates,
        acceptance_threshold=acceptance_threshold,
        margin_threshold=margin_threshold,
        matcher_version=matcher_version,
        source_database_sha256=source_database_sha256,
        allowed_transform_ids=allowed_transform_ids,
    )
    return _construct_event_title_matcher(matcher, event_keys_by_candidate_id)


def match_event_title_candidate(
    *,
    matcher: _EventTitleEvidenceMatcher,
    roi_bgr: np.ndarray,
    binding: RoiBinding,
    ocr_title: str | None = None,
) -> ReferenceEvidence:
    """Return event-title evidence for an already normalized title ROI."""

    if type(matcher) is not _EventTitleEvidenceMatcher:
        return ReferenceEvidence.abstain("invalid_event_title_matcher")
    try:
        evidence = matcher.match(roi_bgr, binding, ocr_title=ocr_title)
    except Exception:
        return ReferenceEvidence.abstain("event_title_matcher_failure")
    if type(evidence) is not ReferenceEvidence:
        return ReferenceEvidence.abstain("invalid_event_title_evidence")
    return evidence


def _snapshot_event_records(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        _config_fail("invalid_event_records")
    try:
        if isinstance(value, Mapping):
            return tuple(value.items())
        return tuple(value)
    except Exception:
        _config_fail("invalid_event_records")


def _validated_candidate_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        _config_fail("invalid_event_title_candidate_map")
    try:
        candidate_map = dict(value.items())
    except Exception:
        _config_fail("invalid_event_title_candidate_map")
    if any(
        type(candidate_id) is not str
        or not candidate_id
        or type(event_key) is not str
        or not event_key
        for candidate_id, event_key in candidate_map.items()
    ):
        _config_fail("invalid_event_title_candidate_map")
    return candidate_map


def _event_candidate_identity_is_canonical(
    candidate_id: Any,
    event_key: Any,
) -> bool:
    if type(candidate_id) is not str or type(event_key) is not str:
        return False
    try:
        decoded = json.loads(candidate_id)
    except Exception:
        return False
    if type(decoded) is not list or len(decoded) != 2:
        return False
    decoded_event_key, label = decoded
    return (
        type(decoded_event_key) is str
        and bool(decoded_event_key)
        and decoded_event_key == event_key
        and type(label) is str
        and label in _EVENT_LABELS
        and json.dumps(
            [decoded_event_key, label],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        == candidate_id
    )


def _validated_event_pair(item: Any) -> tuple[str, Mapping[str, Any]]:
    if type(item) not in (list, tuple) or len(item) != 2:
        _config_fail("invalid_event_records")
    event_key, record = item
    if type(event_key) is not str or not event_key:
        _config_fail("invalid_event_records")
    if not isinstance(record, Mapping):
        _config_fail("invalid_event_records")
    return event_key, record


def _validated_event_record(
    record: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any]]:
    try:
        labels = record["color_labels"]
        fingerprints = record["fingerprints"]
    except Exception:
        _config_fail("invalid_event_records")

    if (
        type(labels) not in (list, tuple)
        or len(labels) != 1
        or type(labels[0]) is not str
        or labels[0] not in _EVENT_LABELS
    ):
        _config_fail("invalid_event_color_labels")
    label = labels[0]
    if not isinstance(fingerprints, Mapping):
        _config_fail("invalid_event_fingerprint_label")
    try:
        fingerprint_labels = set(fingerprints)
    except Exception:
        _config_fail("invalid_event_fingerprint_label")
    if fingerprint_labels != {label}:
        _config_fail("invalid_event_fingerprint_label")
    try:
        fingerprint = fingerprints[label]
    except Exception:
        _config_fail("invalid_event_fingerprint_label")
    if not isinstance(fingerprint, Mapping):
        _config_fail("invalid_event_points")

    try:
        bg_color = fingerprint["bg_color"]
        max_turn = fingerprint["max_turn"]
    except Exception:
        _config_fail("invalid_event_records")
    if type(bg_color) is not str or _COLOR_PATTERN.fullmatch(bg_color) is None:
        _config_fail("invalid_event_bg_color")
    if type(max_turn) is not int:
        _config_fail("invalid_event_max_turn")
    return label, fingerprint


def _validated_points(
    fingerprint: Mapping[str, Any],
) -> tuple[SparseColorPoint, ...]:
    try:
        points = fingerprint["points"]
    except Exception:
        _config_fail("invalid_event_points")
    if type(points) not in (list, tuple) or not points:
        _config_fail("invalid_event_points")

    result: list[SparseColorPoint] = []
    try:
        for point in points:
            if not isinstance(point, Mapping):
                _config_fail("invalid_event_points")
            result.append(
                SparseColorPoint(
                    x=point["x"],
                    y=point["y"],
                    color_hex=point["color"],
                )
            )
    except SparseFingerprintConfigError:
        _config_fail("invalid_event_points")
    except Exception:
        _config_fail("invalid_event_points")
    return tuple(result)


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
            evidence.candidate_id,
            evidence.anchor_id,
            evidence.transform_id,
            evidence.source_database_sha256,
            evidence.source_frame_sha256,
            evidence.matcher_version,
        )
    )


def _config_fail(reason: str) -> None:
    raise SparseFingerprintConfigError(reason) from None
