"""Build a packet-bound, fail-closed perception reliability artifact."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from core.auto_trainer.pacer_storage import _io
from typing import Any, Mapping

from core.auto_trainer.career_supervisor_turn import TurnAuthorityBundle
from core.auto_trainer.perception_reliability import (
    BUNDLE_SCHEMA_VERSION,
    GateAuthorizationRegistry,
    TRAINING_TABS,
)


_SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SUPPORT_COUNT_FIELDS = {
    "support.visible_count": {
        "source_key": "visible_portrait_count",
        "gate_id": "support-visible-count-20260729-v1",
        "evidence_schema": "support-visible-count-evidence.v1",
    },
    "support.effective_count": {
        "source_key": "effective_support_count",
        "gate_id": "support-effective-count-20260729-v1",
        "evidence_schema": "support-effective-count-evidence.v1",
    },
}
_GAIN_STATS = (*TRAINING_TABS, "skill_pt")
_GAIN_FIELDS = {
    f"training_gain.{stat}": {
        "source_key": stat,
        "gate_id": "training-gain-20260810-v1",
        "evidence_schema": "training-gain-evidence.v1",
    }
    for stat in _GAIN_STATS
}


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA_PATTERN.fullmatch(value) is not None


def _validate_frame_map(
    value: object,
) -> dict[str, str]:
    if (
        type(value) is not dict
        or set(value) != set(TRAINING_TABS)
        or any(
            type(tab) is not str or not _is_sha256(value.get(tab))
            for tab in TRAINING_TABS
        )
    ):
        raise ValueError("raw_frame_sha256_by_tab_invalid")
    return {tab: value[tab] for tab in TRAINING_TABS}


def _load_sidecar_options(
    sidecar: object,
    *,
    frames: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    options = sidecar.get("training_options") if type(sidecar) is dict else None
    if type(options) is not list or len(options) != len(TRAINING_TABS):
        raise ValueError("sidecar_training_tabs_invalid")
    by_tab: dict[str, dict[str, Any]] = {}
    for raw_option in options:
        if type(raw_option) is not dict:
            raise ValueError("sidecar_training_option_invalid")
        tab = raw_option.get("training_type")
        if type(tab) is not str or tab not in TRAINING_TABS or tab in by_tab:
            raise ValueError("sidecar_training_tabs_invalid")
        raw_data = raw_option.get("raw_data")
        capture = raw_data.get("capture") if type(raw_data) is dict else None
        capture_sha = capture.get("sha256") if type(capture) is dict else None
        if not _is_sha256(capture_sha) or capture_sha != frames[tab]:
            raise ValueError(f"sidecar_capture_sha_mismatch:{tab}")
        by_tab[tab] = raw_option
    if set(by_tab) != set(TRAINING_TABS):
        raise ValueError("sidecar_training_tabs_invalid")
    return by_tab


def _support_count_field(
    *,
    field_name: str,
    tab: str,
    option: Mapping[str, Any],
    source_frame_sha256: str,
    turn_id: str,
    gate_registry: GateAuthorizationRegistry,
) -> dict[str, Any]:
    specification = _SUPPORT_COUNT_FIELDS[field_name]
    path = f"training.{tab}.{field_name}"
    value = option.get(specification["source_key"])
    contract = {
        "path": path,
        "turn_id": turn_id,
        "source_frame_sha256": source_frame_sha256,
        "evidence_schema": specification["evidence_schema"],
        "gate_id": specification["gate_id"],
    }
    if (
        type(value) is not int
        or not 0 <= value <= 6
        or not gate_registry.recognizes(
            path,
            gate_id=specification["gate_id"],
            evidence_schema=specification["evidence_schema"],
            value=value,
        )
    ):
        return {
            **contract,
            "status": "unknown",
            "value": None,
            "reasons": ["source_value_unknown"],
        }
    if gate_registry.authorizes(
        path,
        gate_id=specification["gate_id"],
        evidence_schema=specification["evidence_schema"],
        value=value,
    ):
        return {
            **contract,
            "status": "decision_safe",
            "value": value,
            "reasons": [],
        }
    return {
        **contract,
        "status": "shadow",
        "value": value,
        "reasons": ["production_gate_shadow_only"],
    }


def _training_gain_field(
    *,
    field_name: str,
    tab: str,
    option: Mapping[str, Any],
    source_frame_sha256: str,
    turn_id: str,
    gate_registry: GateAuthorizationRegistry,
) -> dict[str, Any]:
    specification = _GAIN_FIELDS[field_name]
    stat = specification["source_key"]
    path = f"training.{tab}.{field_name}"
    contract = {
        "path": path,
        "turn_id": turn_id,
        "source_frame_sha256": source_frame_sha256,
        "evidence_schema": specification["evidence_schema"],
        "gate_id": specification["gate_id"],
    }
    breakdown = option.get("gain_breakdown")
    raw = breakdown.get(stat) if type(breakdown) is dict else None
    value = raw.get("total_gain") if type(raw) is dict else None
    if (
        type(value) is not int
        or not gate_registry.recognizes(
            path,
            gate_id=specification["gate_id"],
            evidence_schema=specification["evidence_schema"],
            value=value,
        )
    ):
        return {
            **contract,
            "status": "unknown",
            "value": None,
            "reasons": ["source_value_unknown"],
        }
    if (
        raw.get("reliable") is True
        and raw.get("status") == "accepted"
        and gate_registry.authorizes(
            path,
            gate_id=specification["gate_id"],
            evidence_schema=specification["evidence_schema"],
            value=value,
        )
    ):
        return {
            **contract,
            "status": "decision_safe",
            "value": value,
            "reasons": [],
        }
    return {
        **contract,
        "status": "shadow",
        "value": value,
        "reasons": ["gain_not_reliable"],
    }


def build_perception_reliability_artifact(
    *,
    sidecar: object,
    raw_frame_sha256_by_tab: object,
    authority: object,
    gate_registry: object,
    turn_id: object,
) -> dict[str, Any]:
    """Create the untrusted JSON artifact consumed by the strict loader."""

    if (
        type(authority) is not TurnAuthorityBundle
        or authority.training_packet_sha256 is None
    ):
        raise ValueError("turn_authority_invalid")
    if (
        type(gate_registry) is not GateAuthorizationRegistry
        or not gate_registry.is_valid
        or not gate_registry.is_production_trusted
    ):
        raise ValueError("gate_registry_invalid")
    if type(turn_id) is not str or not turn_id:
        raise ValueError("turn_id_invalid")
    frames = _validate_frame_map(raw_frame_sha256_by_tab)
    by_tab = _load_sidecar_options(sidecar, frames=frames)
    training: dict[str, Any] = {}
    for tab in TRAINING_TABS:
        fields = {
            field_name: _support_count_field(
                field_name=field_name,
                tab=tab,
                option=by_tab[tab],
                source_frame_sha256=frames[tab],
                turn_id=turn_id,
                gate_registry=gate_registry,
            )
            for field_name in _SUPPORT_COUNT_FIELDS
        }
        fields.update(
            {
                gain_field_name: _training_gain_field(
                    field_name=gain_field_name,
                    tab=tab,
                    option=by_tab[tab],
                    source_frame_sha256=frames[tab],
                    turn_id=turn_id,
                    gate_registry=gate_registry,
                )
                for gain_field_name in _GAIN_FIELDS
            }
        )
        training[tab] = {
            "source_frame_sha256": frames[tab],
            "fields": fields,
        }
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "run_session_id": authority.run_session_id,
        "generation": authority.generation,
        "global_action_turn": authority.global_action_turn,
        "turn_id": turn_id,
        "authority_bundle_sha256": authority.bundle_sha256,
        "training_packet_sha256": authority.training_packet_sha256,
        "gate_manifest_id": gate_registry.manifest_id,
        "gate_manifest_sha256": gate_registry.manifest_sha256,
        "training": training,
    }


def canonical_perception_reliability_bytes(
    artifact: object,
) -> bytes:
    if type(artifact) is not dict:
        raise ValueError("perception_reliability_artifact_invalid")
    try:
        encoded = json.dumps(
            artifact,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise ValueError("perception_reliability_artifact_invalid") from exc
    return encoded


def write_perception_reliability_artifact_exclusive(
    path: Path,
    artifact: object,
) -> str:
    """Write once, reread, and return the SHA of the actual persisted bytes."""

    if not isinstance(path, Path):
        raise TypeError("perception_reliability_path_invalid")
    payload = canonical_perception_reliability_bytes(artifact)
    _io(path.parent).mkdir(parents=True, exist_ok=True)
    try:
        with _io(path).open("xb") as handle:
            handle.write(payload)
            handle.flush()
    except FileExistsError as exc:
        raise FileExistsError(
            "perception_reliability_artifact_exists"
        ) from exc
    try:
        actual = _io(path).read_bytes()
    except OSError as exc:
        raise ValueError("perception_reliability_artifact_verify_failed") from exc
    if actual != payload:
        raise ValueError("perception_reliability_artifact_verify_failed")
    return hashlib.sha256(actual).hexdigest()


__all__ = [
    "build_perception_reliability_artifact",
    "canonical_perception_reliability_bytes",
    "write_perception_reliability_artifact_exclusive",
]
