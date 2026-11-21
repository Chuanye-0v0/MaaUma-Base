from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import cv2


SCHEMA_VERSION = "uma.career_setup_allowlist.v1"
CURRENT_LIVE_MARKERS = (
    "u1_next_run_preflight_20260718",
    "after_scenario_continue_zero_click.png",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CareerSetupAllowlistError(ValueError):
    pass


def _require_sha256(value: object, field: str) -> str:
    normalized = str(value).lower()
    if not SHA256_RE.fullmatch(normalized):
        raise CareerSetupAllowlistError(f"invalid_sha256:{field}")
    return normalized


def _require_bbox(value: object) -> tuple[int, int, int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise CareerSetupAllowlistError("invalid_canonical_bbox")
    bbox = tuple(int(item) for item in value)
    x1, y1, x2, y2 = bbox
    if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1 or x2 > 720 or y2 > 1280:
        raise CareerSetupAllowlistError("invalid_canonical_bbox")
    return bbox


def _template_sha256(frame, bbox: tuple[int, int, int, int]) -> str:
    x1, y1, x2, y2 = bbox
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        raise CareerSetupAllowlistError("template_crop_empty")
    envelope = (
        int(crop.shape[0]).to_bytes(4, "big")
        + int(crop.shape[1]).to_bytes(4, "big")
        + int(crop.shape[2]).to_bytes(4, "big")
        + crop.tobytes(order="C")
    )
    return hashlib.sha256(envelope).hexdigest()


@dataclass(frozen=True)
class AllowlistSource:
    role: str
    path: str
    sha256: str
    created_before_current_live: bool

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "AllowlistSource":
        path = str(payload.get("path", ""))
        if any(marker.lower() in path.lower() for marker in CURRENT_LIVE_MARKERS):
            raise CareerSetupAllowlistError("current_live_source_forbidden")
        created_before = payload.get("created_before_current_live") is True
        if not created_before:
            raise CareerSetupAllowlistError("current_live_source_forbidden")
        expected_sha = _require_sha256(payload.get("sha256"), "source")
        source_path = Path(path)
        if not source_path.is_file():
            raise CareerSetupAllowlistError(f"source_missing:{path}")
        actual_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if actual_sha != expected_sha:
            raise CareerSetupAllowlistError(f"source_hash_mismatch:{path}")
        return cls(
            role=str(payload.get("role", "")),
            path=path,
            sha256=expected_sha,
            created_before_current_live=True,
        )


@dataclass(frozen=True)
class IdentityEvidence:
    identity_id: str
    display_name: str
    source_path: str
    source_sha256: str
    canonical_bbox: tuple[int, int, int, int]
    template_sha256: str
    evidence_status: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "IdentityEvidence":
        identity_id = str(payload.get("identity_id", "")).strip()
        display_name = str(payload.get("display_name", "")).strip()
        if not identity_id or not display_name:
            raise CareerSetupAllowlistError("identity_required")
        status = str(payload.get("evidence_status", ""))
        if status not in {"authoritative_visual", "verified_manifest"}:
            raise CareerSetupAllowlistError("identity_evidence_unverified")
        return cls(
            identity_id=identity_id,
            display_name=display_name,
            source_path=str(payload.get("source_path", "")),
            source_sha256=_require_sha256(payload.get("source_sha256"), "identity_source"),
            canonical_bbox=_require_bbox(payload.get("canonical_bbox")),
            template_sha256=_require_sha256(payload.get("template_sha256"), "template"),
            evidence_status=status,
        )


@dataclass(frozen=True)
class CareerSetupAllowlist:
    schema_version: str
    scenario_id: str
    trainee: IdentityEvidence
    inheritance_parents: tuple[IdentityEvidence, IdentityEvidence]
    support_cards: tuple[IdentityEvidence, ...]
    sources: tuple[AllowlistSource, ...]
    matcher_versions: tuple[tuple[str, str], ...]

    @classmethod
    def load(cls, path: str | Path) -> "CareerSetupAllowlist":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise CareerSetupAllowlistError("manifest_not_object")
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "CareerSetupAllowlist":
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise CareerSetupAllowlistError("schema_version_mismatch")
        if payload.get("scenario_id") != "grand_masters":
            raise CareerSetupAllowlistError("scenario_not_allowed")
        raw_sources = payload.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise CareerSetupAllowlistError("sources_required")
        sources = tuple(AllowlistSource.from_dict(item) for item in raw_sources)
        source_pairs = {(item.path, item.sha256) for item in sources}
        frame_cache: dict[str, object] = {}

        def identity(item: object) -> IdentityEvidence:
            if not isinstance(item, Mapping):
                raise CareerSetupAllowlistError("identity_not_object")
            evidence = IdentityEvidence.from_dict(item)
            if (evidence.source_path, evidence.source_sha256) not in source_pairs:
                raise CareerSetupAllowlistError("identity_source_unbound")
            frame = frame_cache.get(evidence.source_path)
            if frame is None:
                frame = cv2.imread(evidence.source_path, cv2.IMREAD_COLOR)
                if frame is None or frame.shape[:2] != (1280, 720):
                    raise CareerSetupAllowlistError("identity_source_frame_invalid")
                frame_cache[evidence.source_path] = frame
            if _template_sha256(frame, evidence.canonical_bbox) != evidence.template_sha256:
                raise CareerSetupAllowlistError(
                    f"template_hash_mismatch:{evidence.identity_id}"
                )
            return evidence

        trainee = identity(payload.get("trainee"))
        raw_parents = payload.get("inheritance_parents")
        if not isinstance(raw_parents, list) or len(raw_parents) != 2:
            raise CareerSetupAllowlistError("two_parents_required")
        parents = tuple(identity(item) for item in raw_parents)
        raw_cards = payload.get("support_cards")
        if not isinstance(raw_cards, list) or len(raw_cards) != 6:
            raise CareerSetupAllowlistError("six_support_cards_required")
        cards = tuple(identity(item) for item in raw_cards)
        raw_versions = payload.get("matcher_versions")
        if not isinstance(raw_versions, Mapping) or not raw_versions:
            raise CareerSetupAllowlistError("matcher_versions_required")
        versions = tuple(sorted((str(key), str(value)) for key, value in raw_versions.items()))
        return cls(
            schema_version=SCHEMA_VERSION,
            scenario_id="grand_masters",
            trainee=trainee,
            inheritance_parents=(parents[0], parents[1]),
            support_cards=cards,
            sources=sources,
            matcher_versions=versions,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["matcher_versions"] = dict(self.matcher_versions)
        return payload


__all__ = [
    "AllowlistSource",
    "CareerSetupAllowlist",
    "CareerSetupAllowlistError",
    "IdentityEvidence",
    "SCHEMA_VERSION",
]
