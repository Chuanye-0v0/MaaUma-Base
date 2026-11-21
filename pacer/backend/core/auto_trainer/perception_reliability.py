"""Fail-closed projection of untrusted perception data.

The security boundary is untrusted structured data plus the public production
loaders below. Arbitrary Python code already executing in this process is
trusted code and is outside this module's threat model; strategy APIs must
accept :class:`PerceptionDecisionView`, never the full bundle or audit view.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

from core.auto_trainer.career_supervisor_turn import TurnAuthorityBundle


BUNDLE_SCHEMA_VERSION = "uma.perception_reliability_bundle.v1"
GATE_AUTHORIZATION_SCHEMA_VERSION = "uma.perception_gate_authorizations.v1"
FIELD_EVIDENCE_SCHEMA = "uma.perception_reliability.synthetic.v1"
NO_GATE_ID = "no-production-gate"
TRAINING_TABS = ("speed", "stamina", "power", "guts", "wisdom")
TRUSTED_GATE_MANIFEST_ID = "perception-gates-20260810-v1"
TRUSTED_GATE_MANIFEST_SHA256 = (
    "451d48629ca02f8a20e612f3287ba004cfc83a89f518cc5e74f69e3a87d95eaa"
)
TRUSTED_GATE_MANIFEST_RELATIVE_PATH = Path(
    "config/perception_gate_authorizations.json"
)

_FIELD_KEYS = frozenset(
    {
        "path",
        "status",
        "value",
        "turn_id",
        "source_frame_sha256",
        "evidence_schema",
        "gate_id",
        "reasons",
    }
)
_BUNDLE_KEYS = frozenset(
    {
        "schema_version",
        "run_session_id",
        "generation",
        "global_action_turn",
        "turn_id",
        "authority_bundle_sha256",
        "training_packet_sha256",
        "gate_manifest_id",
        "gate_manifest_sha256",
        "training",
    }
)
_TAB_KEYS = frozenset({"source_frame_sha256", "fields"})
_GATE_REGISTRY_KEYS = frozenset(
    {"schema_version", "manifest_id", "authorizations"}
)
_GATE_ENTRY_KEYS = frozenset(
    {"gate_id", "evidence_schema", "value_type", "verdict"}
)
_SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_PATTERN = re.compile(r"^[a-z0-9_.-]+$")
_SLOT_FIELD_PATTERN = re.compile(
    r"^support\.(identity|hint|bond_band)\.([0-5])$"
)
_MAX_GATE_MANIFEST_BYTES = 1_000_000

JsonScalar = str | int | float | bool | None
FieldStatus = Literal["decision_safe", "shadow", "unknown"]


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA_PATTERN.fullmatch(value) is not None


def _is_token(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and _TOKEN_PATTERN.fullmatch(value) is not None
    )


def _is_json_scalar(value: object) -> bool:
    if value is None:
        return True
    if type(value) in {str, int, bool}:
        return type(value) is not str or bool(value)
    return type(value) is float and math.isfinite(value)


def _has_exact_string_keys(
    value: object,
    expected: frozenset[str],
) -> bool:
    return bool(
        type(value) is dict
        and len(value) == len(expected)
        and all(type(key) is str for key in value)
        and frozenset(value) == expected
    )


def _closed_reasons(value: object) -> tuple[str, ...] | None:
    if type(value) is not list:
        return None
    reasons: list[str] = []
    for item in value:
        if not _is_token(item) or item in reasons:
            return None
        reasons.append(item)
    return tuple(reasons)


def _expected_value_type(path: object) -> str | None:
    if type(path) is not str:
        return None
    parts = path.split(".", 2)
    if (
        len(parts) != 3
        or parts[0] != "training"
        or parts[1] not in TRAINING_TABS
    ):
        return None
    field = parts[2]
    fixed = {
        "failure_rate": "integer",
        "vitality_delta": "number",
        "support.visible_count": "integer",
        "support.effective_count": "integer",
        "support.hint_count": "integer",
        "fragment.count": "integer",
        "fragment.color": "string",
        "fragment.kind": "string",
    }
    if field in fixed:
        return fixed[field]
    if field.startswith("training_gain."):
        stat = field.removeprefix("training_gain.")
        return "integer" if stat in {*TRAINING_TABS, "skill_pt"} else None
    match = _SLOT_FIELD_PATTERN.fullmatch(field)
    if match is None:
        return None
    return {
        "identity": "string",
        "hint": "boolean",
        "bond_band": "string",
    }[match.group(1)]


def _value_matches_type(value: object, value_type: str) -> bool:
    if value_type == "integer":
        return type(value) is int
    if value_type == "number":
        return type(value) is int or (
            type(value) is float and math.isfinite(value)
        )
    if value_type == "string":
        return type(value) is str and bool(value)
    if value_type == "boolean":
        return type(value) is bool
    return False


def _json_object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


@dataclass(frozen=True)
class _GateAuthorization:
    gate_id: str
    evidence_schema: str
    value_type: str
    verdict: str


@dataclass(frozen=True, init=False)
class GateAuthorizationRegistry:
    manifest_id: str
    manifest_sha256: str
    is_valid: bool
    is_production_trusted: bool
    errors: tuple[str, ...]
    __authorizations: Mapping[str, _GateAuthorization]

    def _entry(self, path: object) -> _GateAuthorization | None:
        if not self.is_valid or type(path) is not str:
            return None
        return self.__authorizations.get(path)

    def recognizes_contract(
        self,
        path: object,
        *,
        gate_id: object,
        evidence_schema: object,
    ) -> bool:
        entry = self._entry(path)
        return bool(
            entry is not None
            and type(gate_id) is str
            and gate_id == entry.gate_id
            and type(evidence_schema) is str
            and evidence_schema == entry.evidence_schema
        )

    def recognizes(
        self,
        path: object,
        *,
        gate_id: object,
        evidence_schema: object,
        value: object,
    ) -> bool:
        entry = self._entry(path)
        return bool(
            self.recognizes_contract(
                path,
                gate_id=gate_id,
                evidence_schema=evidence_schema,
            )
            and entry is not None
            and _value_matches_type(value, entry.value_type)
        )

    def authorizes(
        self,
        path: object,
        *,
        gate_id: object,
        evidence_schema: object,
        value: object,
    ) -> bool:
        entry = self._entry(path)
        return bool(
            entry is not None
            and entry.verdict == "decision_safe"
            and self.recognizes(
                path,
                gate_id=gate_id,
                evidence_schema=evidence_schema,
                value=value,
            )
        )


def _new_gate_registry(
    *,
    manifest_id: str,
    manifest_sha256: str,
    is_valid: bool,
    is_production_trusted: bool,
    errors: tuple[str, ...],
    authorizations: Mapping[str, _GateAuthorization],
) -> GateAuthorizationRegistry:
    instance = object.__new__(GateAuthorizationRegistry)
    object.__setattr__(instance, "manifest_id", manifest_id)
    object.__setattr__(instance, "manifest_sha256", manifest_sha256)
    object.__setattr__(instance, "is_valid", is_valid)
    object.__setattr__(
        instance,
        "is_production_trusted",
        is_production_trusted,
    )
    object.__setattr__(instance, "errors", errors)
    object.__setattr__(
        instance,
        "_GateAuthorizationRegistry__authorizations",
        MappingProxyType(dict(authorizations)),
    )
    return instance


def _invalid_gate_registry(
    reason: str,
    *,
    manifest_id: object,
    manifest_sha256: object,
) -> GateAuthorizationRegistry:
    return _new_gate_registry(
        manifest_id=(
            manifest_id if _is_token(manifest_id) else "invalid-manifest"
        ),
        manifest_sha256=(
            manifest_sha256 if _is_sha256(manifest_sha256) else "0" * 64
        ),
        is_valid=False,
        is_production_trusted=False,
        errors=(reason,),
        authorizations={},
    )


def _load_gate_authorization_registry_from_pinned_bytes(
    manifest_bytes: object,
    *,
    expected_manifest_id: str,
    expected_manifest_sha256: str,
    production_trusted: bool,
) -> GateAuthorizationRegistry:
    """Load a frozen gate manifest only after verifying its actual bytes."""

    invalid = lambda reason: _invalid_gate_registry(
        reason,
        manifest_id=expected_manifest_id,
        manifest_sha256=expected_manifest_sha256,
    )
    if (
        not _is_token(expected_manifest_id)
        or not _is_sha256(expected_manifest_sha256)
    ):
        return invalid("expected_gate_manifest_invalid")
    if (
        type(manifest_bytes) is not bytes
        or not manifest_bytes
        or len(manifest_bytes) > _MAX_GATE_MANIFEST_BYTES
    ):
        return invalid("gate_manifest_bytes_invalid")
    actual_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_sha256 != expected_manifest_sha256:
        return invalid("gate_manifest_hash_mismatch")
    try:
        value = json.loads(
            manifest_bytes.decode("utf-8"),
            object_pairs_hook=_json_object_without_duplicates,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return invalid("gate_manifest_json_invalid")
    if (
        not _has_exact_string_keys(value, _GATE_REGISTRY_KEYS)
        or value.get("schema_version")
        != GATE_AUTHORIZATION_SCHEMA_VERSION
    ):
        return invalid("gate_registry_schema_invalid")
    if (
        type(value.get("manifest_id")) is not str
        or value["manifest_id"] != expected_manifest_id
    ):
        return invalid("gate_manifest_binding_mismatch")
    raw_authorizations = value.get("authorizations")
    if type(raw_authorizations) is not dict:
        return invalid("gate_authorizations_invalid")

    authorizations: dict[str, _GateAuthorization] = {}
    for path, raw in raw_authorizations.items():
        expected_type = _expected_value_type(path)
        if (
            expected_type is None
            or not _has_exact_string_keys(raw, _GATE_ENTRY_KEYS)
            or not _is_token(raw.get("gate_id"))
            or not _is_token(raw.get("evidence_schema"))
            or raw.get("value_type") != expected_type
            or raw.get("verdict") not in {"decision_safe", "shadow"}
        ):
            return invalid("gate_authorization_entry_invalid")
        authorizations[path] = _GateAuthorization(
            gate_id=raw["gate_id"],
            evidence_schema=raw["evidence_schema"],
            value_type=raw["value_type"],
            verdict=raw["verdict"],
        )
    return _new_gate_registry(
        manifest_id=value["manifest_id"],
        manifest_sha256=actual_sha256,
        is_valid=True,
        is_production_trusted=production_trusted,
        errors=(),
        authorizations=authorizations,
    )


def load_trusted_gate_authorization_registry(
    manifest_bytes: object,
) -> GateAuthorizationRegistry:
    """Load bytes against the immutable production trust anchor."""

    return _load_gate_authorization_registry_from_pinned_bytes(
        manifest_bytes,
        expected_manifest_id=TRUSTED_GATE_MANIFEST_ID,
        expected_manifest_sha256=TRUSTED_GATE_MANIFEST_SHA256,
        production_trusted=True,
    )


def load_trusted_gate_authorization_registry_from_workspace(
    workspace_root: object,
) -> GateAuthorizationRegistry:
    if not isinstance(workspace_root, Path):
        return _invalid_gate_registry(
            "workspace_root_invalid",
            manifest_id=TRUSTED_GATE_MANIFEST_ID,
            manifest_sha256=TRUSTED_GATE_MANIFEST_SHA256,
        )
    try:
        raw = (
            workspace_root / TRUSTED_GATE_MANIFEST_RELATIVE_PATH
        ).read_bytes()
    except OSError:
        return _invalid_gate_registry(
            "gate_manifest_unavailable",
            manifest_id=TRUSTED_GATE_MANIFEST_ID,
            manifest_sha256=TRUSTED_GATE_MANIFEST_SHA256,
        )
    return load_trusted_gate_authorization_registry(raw)


@dataclass(frozen=True, init=False)
class FieldObservation:
    path: str
    status: FieldStatus
    value: JsonScalar
    turn_id: str
    source_frame_sha256: str
    evidence_schema: str
    gate_id: str
    reasons: tuple[str, ...]


def _new_field_observation(
    *,
    path: str,
    status: FieldStatus,
    value: JsonScalar,
    turn_id: str,
    source_frame_sha256: str,
    evidence_schema: str,
    gate_id: str,
    reasons: tuple[str, ...],
) -> FieldObservation:
    instance = object.__new__(FieldObservation)
    for name, item in (
        ("path", path),
        ("status", status),
        ("value", value),
        ("turn_id", turn_id),
        ("source_frame_sha256", source_frame_sha256),
        ("evidence_schema", evidence_schema),
        ("gate_id", gate_id),
        ("reasons", reasons),
    ):
        object.__setattr__(instance, name, item)
    return instance


def _unknown_field(
    *,
    path: object,
    turn_id: object,
    source_frame_sha256: object,
    reason: object,
) -> FieldObservation:
    return _new_field_observation(
        path=path if _is_token(path) else "unknown.field",
        status="unknown",
        value=None,
        turn_id=turn_id if _is_token(turn_id) else "unknown-turn",
        source_frame_sha256=(
            source_frame_sha256
            if _is_sha256(source_frame_sha256)
            else "0" * 64
        ),
        evidence_schema=FIELD_EVIDENCE_SCHEMA,
        gate_id=NO_GATE_ID,
        reasons=(reason if _is_token(reason) else "field_invalid",),
    )


def _parse_field_observation(
    value: object,
    *,
    expected_path: str,
    expected_turn_id: str,
    expected_source_frame_sha256: str,
    gate_registry: GateAuthorizationRegistry,
) -> FieldObservation:
    unknown = lambda reason: _unknown_field(
        path=expected_path,
        turn_id=expected_turn_id,
        source_frame_sha256=expected_source_frame_sha256,
        reason=reason,
    )
    if _expected_value_type(expected_path) is None:
        return unknown("field_path_not_allowed")
    if not _has_exact_string_keys(value, _FIELD_KEYS):
        return unknown("field_schema_invalid")
    if type(value.get("path")) is not str or value["path"] != expected_path:
        return unknown("field_path_mismatch")
    if (
        type(value.get("turn_id")) is not str
        or value["turn_id"] != expected_turn_id
    ):
        return unknown("field_turn_mismatch")
    if (
        type(value.get("source_frame_sha256")) is not str
        or value["source_frame_sha256"] != expected_source_frame_sha256
        or not _is_sha256(value["source_frame_sha256"])
    ):
        return unknown("field_source_frame_mismatch")
    status = value.get("status")
    if (
        type(status) is not str
        or status not in {"decision_safe", "shadow", "unknown"}
    ):
        return unknown("field_status_invalid")
    if not _is_token(value.get("evidence_schema")):
        return unknown("field_evidence_schema_invalid")
    if not _is_token(value.get("gate_id")):
        return unknown("field_gate_id_invalid")
    reasons = _closed_reasons(value.get("reasons"))
    if reasons is None:
        return unknown("field_reasons_invalid")
    raw_value = value.get("value")

    if status == "decision_safe":
        if reasons or raw_value is None:
            return unknown("decision_safe_contract_invalid")
        if not gate_registry.authorizes(
            expected_path,
            gate_id=value["gate_id"],
            evidence_schema=value["evidence_schema"],
            value=raw_value,
        ):
            return unknown("field_gate_unauthorized")
        stored_value = raw_value
    elif status == "shadow":
        if (
            not reasons
            or raw_value is None
            or not gate_registry.recognizes(
                expected_path,
                gate_id=value["gate_id"],
                evidence_schema=value["evidence_schema"],
                value=raw_value,
            )
        ):
            return unknown("shadow_contract_invalid")
        stored_value = raw_value
    else:
        if (
            not reasons
            or raw_value is not None
            or not gate_registry.recognizes_contract(
                expected_path,
                gate_id=value["gate_id"],
                evidence_schema=value["evidence_schema"],
            )
        ):
            return unknown("unknown_contract_invalid")
        stored_value = None

    return _new_field_observation(
        path=value["path"],
        status=status,
        value=stored_value,
        turn_id=value["turn_id"],
        source_frame_sha256=value["source_frame_sha256"],
        evidence_schema=value["evidence_schema"],
        gate_id=value["gate_id"],
        reasons=reasons,
    )


@dataclass(frozen=True, init=False)
class DecisionFieldObservation:
    path: str
    status: FieldStatus
    decision_value: JsonScalar
    turn_id: str
    source_frame_sha256: str
    evidence_schema: str
    gate_id: str
    reasons: tuple[str, ...]


def _decision_field(observation: FieldObservation) -> DecisionFieldObservation:
    instance = object.__new__(DecisionFieldObservation)
    for name, value in (
        ("path", observation.path),
        ("status", observation.status),
        (
            "decision_value",
            observation.value
            if observation.status == "decision_safe"
            else None,
        ),
        ("turn_id", observation.turn_id),
        ("source_frame_sha256", observation.source_frame_sha256),
        ("evidence_schema", observation.evidence_schema),
        ("gate_id", observation.gate_id),
        ("reasons", observation.reasons),
    ):
        object.__setattr__(instance, name, value)
    return instance


@dataclass(frozen=True)
class _TrainingTabReliability:
    source_frame_sha256: str
    fields: Mapping[str, FieldObservation]


@dataclass(frozen=True, init=False)
class PerceptionReliabilityBundle:
    run_session_id: str
    generation: int
    global_action_turn: int
    turn_id: str
    authority_bundle_sha256: str
    training_packet_sha256: str
    gate_manifest_id: str
    gate_manifest_sha256: str
    is_schema_valid: bool
    is_decision_eligible: bool
    invalid_tabs: tuple[str, ...]
    errors: tuple[str, ...]
    __training: Mapping[str, _TrainingTabReliability]

    def _authority_matches(self, authority: object) -> bool:
        return bool(
            type(authority) is TurnAuthorityBundle
            and authority.run_session_id == self.run_session_id
            and authority.generation == self.generation
            and authority.global_action_turn == self.global_action_turn
            and authority.bundle_sha256 == self.authority_bundle_sha256
            and authority.training_packet_sha256
            == self.training_packet_sha256
        )

    def _audit_training_field(
        self,
        tab: object,
        field: object,
        *,
        authority: TurnAuthorityBundle,
        expected_turn_id: object,
        expected_source_frame_sha256: object,
    ) -> FieldObservation:
        if type(tab) is not str or type(field) is not str:
            return _unknown_field(
                path="unknown.field",
                turn_id=expected_turn_id,
                source_frame_sha256=expected_source_frame_sha256,
                reason="query_field_invalid",
            )
        path = f"training.{tab}.{field}"
        unknown = lambda reason: _unknown_field(
            path=path,
            turn_id=expected_turn_id,
            source_frame_sha256=expected_source_frame_sha256,
            reason=reason,
        )
        if not self.is_schema_valid:
            return unknown("bundle_invalid")
        if not self._authority_matches(authority):
            return unknown("query_authority_mismatch")
        if not self.is_decision_eligible:
            return unknown("bundle_authority_binding_invalid")
        if type(expected_turn_id) is not str or expected_turn_id != self.turn_id:
            return unknown("query_turn_mismatch")
        if tab not in TRAINING_TABS or not _is_token(field):
            return unknown("query_field_invalid")
        training_tab = self.__training.get(tab)
        if training_tab is None:
            return unknown("tab_missing")
        if (
            not _is_sha256(expected_source_frame_sha256)
            or expected_source_frame_sha256
            != training_tab.source_frame_sha256
        ):
            return unknown("query_source_frame_mismatch")
        return training_tab.fields.get(field) or unknown("field_missing")

def _new_bundle(
    *,
    run_session_id: str,
    generation: int,
    global_action_turn: int,
    turn_id: str,
    authority_bundle_sha256: str,
    training_packet_sha256: str,
    gate_manifest_id: str,
    gate_manifest_sha256: str,
    is_schema_valid: bool,
    is_decision_eligible: bool,
    invalid_tabs: tuple[str, ...],
    errors: tuple[str, ...],
    training: Mapping[str, _TrainingTabReliability],
) -> PerceptionReliabilityBundle:
    instance = object.__new__(PerceptionReliabilityBundle)
    for name, value in (
        ("run_session_id", run_session_id),
        ("generation", generation),
        ("global_action_turn", global_action_turn),
        ("turn_id", turn_id),
        ("authority_bundle_sha256", authority_bundle_sha256),
        ("training_packet_sha256", training_packet_sha256),
        ("gate_manifest_id", gate_manifest_id),
        ("gate_manifest_sha256", gate_manifest_sha256),
        ("is_schema_valid", is_schema_valid),
        ("is_decision_eligible", is_decision_eligible),
        ("invalid_tabs", invalid_tabs),
        ("errors", errors),
    ):
        object.__setattr__(instance, name, value)
    object.__setattr__(
        instance,
        "_PerceptionReliabilityBundle__training",
        MappingProxyType(dict(training)),
    )
    return instance


def _invalid_bundle(
    expected_turn_id: object,
    reason: str,
    *,
    authority: TurnAuthorityBundle | None = None,
    gate_registry: GateAuthorizationRegistry | None = None,
) -> PerceptionReliabilityBundle:
    return _new_bundle(
        run_session_id=(
            authority.run_session_id
            if type(authority) is TurnAuthorityBundle
            else "invalid-run"
        ),
        generation=(
            authority.generation
            if type(authority) is TurnAuthorityBundle
            else 0
        ),
        global_action_turn=(
            authority.global_action_turn
            if type(authority) is TurnAuthorityBundle
            else 0
        ),
        turn_id=(
            expected_turn_id
            if _is_token(expected_turn_id)
            else "unknown-turn"
        ),
        authority_bundle_sha256=(
            authority.bundle_sha256
            if type(authority) is TurnAuthorityBundle
            else "0" * 64
        ),
        training_packet_sha256=(
            authority.training_packet_sha256
            if type(authority) is TurnAuthorityBundle
            and authority.training_packet_sha256 is not None
            else "0" * 64
        ),
        gate_manifest_id=(
            gate_registry.manifest_id
            if type(gate_registry) is GateAuthorizationRegistry
            else "invalid-manifest"
        ),
        gate_manifest_sha256=(
            gate_registry.manifest_sha256
            if type(gate_registry) is GateAuthorizationRegistry
            else "0" * 64
        ),
        is_schema_valid=False,
        is_decision_eligible=False,
        invalid_tabs=(),
        errors=(reason,),
        training={},
    )


def load_perception_reliability_bundle(
    value: object,
    *,
    authority: TurnAuthorityBundle,
    expected_turn_id: str,
    expected_frame_sha256_by_tab: Mapping[str, str],
    gate_registry: GateAuthorizationRegistry,
) -> PerceptionReliabilityBundle:
    """Validate untrusted structured perception into a decision-safe view."""

    if (
        type(authority) is not TurnAuthorityBundle
        or authority.training_packet_sha256 is None
    ):
        return _invalid_bundle(expected_turn_id, "authority_invalid")
    if (
        type(gate_registry) is not GateAuthorizationRegistry
        or not gate_registry.is_valid
        or not gate_registry.is_production_trusted
    ):
        return _invalid_bundle(
            expected_turn_id,
            "gate_registry_invalid",
            authority=authority,
        )
    if (
        not _has_exact_string_keys(
            expected_frame_sha256_by_tab,
            frozenset(TRAINING_TABS),
        )
        or any(
            not _is_sha256(expected_frame_sha256_by_tab.get(tab))
            for tab in TRAINING_TABS
        )
    ):
        return _invalid_bundle(
            expected_turn_id,
            "expected_frames_invalid",
            authority=authority,
            gate_registry=gate_registry,
        )
    if (
        not _has_exact_string_keys(value, _BUNDLE_KEYS)
        or type(value.get("schema_version")) is not str
        or value["schema_version"] != BUNDLE_SCHEMA_VERSION
    ):
        return _invalid_bundle(
            expected_turn_id,
            "bundle_schema_invalid",
            authority=authority,
            gate_registry=gate_registry,
        )
    scalar_types_valid = (
        _is_token(value.get("run_session_id"))
        and type(value.get("generation")) is int
        and value["generation"] >= 0
        and type(value.get("global_action_turn")) is int
        and value["global_action_turn"] >= 0
        and _is_token(value.get("turn_id"))
        and _is_sha256(value.get("authority_bundle_sha256"))
        and _is_sha256(value.get("training_packet_sha256"))
        and _is_token(value.get("gate_manifest_id"))
        and _is_sha256(value.get("gate_manifest_sha256"))
    )
    if not scalar_types_valid:
        return _invalid_bundle(
            expected_turn_id,
            "bundle_scalar_types_invalid",
            authority=authority,
            gate_registry=gate_registry,
        )
    raw_training = value.get("training")
    if (
        type(raw_training) is not dict
        or len(raw_training) != len(TRAINING_TABS)
        or any(type(key) is not str for key in raw_training)
        or frozenset(raw_training) != frozenset(TRAINING_TABS)
    ):
        return _invalid_bundle(
            expected_turn_id,
            "training_tabs_invalid",
            authority=authority,
            gate_registry=gate_registry,
        )

    binding_valid = (
        value["run_session_id"] == authority.run_session_id
        and value["generation"] == authority.generation
        and value["global_action_turn"] == authority.global_action_turn
        and value["turn_id"] == expected_turn_id
        and value["authority_bundle_sha256"] == authority.bundle_sha256
        and value["training_packet_sha256"]
        == authority.training_packet_sha256
        and value["gate_manifest_id"] == gate_registry.manifest_id
        and value["gate_manifest_sha256"]
        == gate_registry.manifest_sha256
    )

    training: dict[str, _TrainingTabReliability] = {}
    invalid_tabs: list[str] = []
    for tab in TRAINING_TABS:
        raw_tab = raw_training.get(tab)
        expected_sha = expected_frame_sha256_by_tab[tab]
        if (
            not _has_exact_string_keys(raw_tab, _TAB_KEYS)
            or not _is_sha256(raw_tab.get("source_frame_sha256"))
            or type(raw_tab.get("fields")) is not dict
        ):
            invalid_tabs.append(tab)
            training[tab] = _TrainingTabReliability(
                source_frame_sha256=expected_sha,
                fields=MappingProxyType({}),
            )
            continue
        source_sha = raw_tab["source_frame_sha256"]
        if source_sha != expected_sha:
            invalid_tabs.append(tab)
        fields: dict[str, FieldObservation] = {}
        for field_name, raw_field in raw_tab["fields"].items():
            if not _is_token(field_name):
                invalid_tabs.append(tab)
                fields = {}
                break
            full_path = f"training.{tab}.{field_name}"
            fields[field_name] = _parse_field_observation(
                raw_field,
                expected_path=full_path,
                expected_turn_id=expected_turn_id,
                expected_source_frame_sha256=source_sha,
                gate_registry=gate_registry,
            )
        training[tab] = _TrainingTabReliability(
            source_frame_sha256=source_sha,
            fields=MappingProxyType(fields),
        )

    decision_eligible = binding_valid and not invalid_tabs
    errors: list[str] = []
    if not binding_valid:
        errors.append("bundle_authority_binding_invalid")
    if invalid_tabs:
        errors.append("bundle_tab_binding_invalid")
    return _new_bundle(
        run_session_id=value["run_session_id"],
        generation=value["generation"],
        global_action_turn=value["global_action_turn"],
        turn_id=value["turn_id"],
        authority_bundle_sha256=value["authority_bundle_sha256"],
        training_packet_sha256=value["training_packet_sha256"],
        gate_manifest_id=value["gate_manifest_id"],
        gate_manifest_sha256=value["gate_manifest_sha256"],
        is_schema_valid=True,
        is_decision_eligible=decision_eligible,
        invalid_tabs=tuple(dict.fromkeys(invalid_tabs)),
        errors=tuple(errors),
        training=training,
    )


@dataclass(frozen=True, init=False)
class PerceptionDecisionView:
    __bundle: PerceptionReliabilityBundle

    @property
    def is_schema_valid(self) -> bool:
        return self.__bundle.is_schema_valid

    @property
    def is_decision_eligible(self) -> bool:
        return self.__bundle.is_decision_eligible

    @property
    def errors(self) -> tuple[str, ...]:
        return self.__bundle.errors

    def training_field(
        self,
        tab: str,
        field: str,
        *,
        authority: TurnAuthorityBundle,
        expected_turn_id: str,
        expected_source_frame_sha256: str,
    ) -> DecisionFieldObservation:
        return _decision_field(
            self.__bundle._audit_training_field(
                tab,
                field,
                authority=authority,
                expected_turn_id=expected_turn_id,
                expected_source_frame_sha256=expected_source_frame_sha256,
            )
        )


def build_perception_decision_view(
    bundle: object,
) -> PerceptionDecisionView:
    if type(bundle) is not PerceptionReliabilityBundle:
        raise TypeError("validated perception bundle required")
    instance = object.__new__(PerceptionDecisionView)
    object.__setattr__(
        instance,
        "_PerceptionDecisionView__bundle",
        bundle,
    )
    return instance


@dataclass(frozen=True, init=False)
class PerceptionAuditProjection:
    __bundle: PerceptionReliabilityBundle

    def training_field(
        self,
        tab: str,
        field: str,
        *,
        authority: TurnAuthorityBundle,
        expected_turn_id: str,
        expected_source_frame_sha256: str,
    ) -> FieldObservation:
        return self.__bundle._audit_training_field(
            tab,
            field,
            authority=authority,
            expected_turn_id=expected_turn_id,
            expected_source_frame_sha256=expected_source_frame_sha256,
        )


def build_perception_audit_projection(
    bundle: object,
) -> PerceptionAuditProjection:
    if type(bundle) is not PerceptionReliabilityBundle:
        raise TypeError("validated perception bundle required")
    instance = object.__new__(PerceptionAuditProjection)
    object.__setattr__(
        instance,
        "_PerceptionAuditProjection__bundle",
        bundle,
    )
    return instance


__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "GATE_AUTHORIZATION_SCHEMA_VERSION",
    "DecisionFieldObservation",
    "FieldObservation",
    "GateAuthorizationRegistry",
    "PerceptionAuditProjection",
    "PerceptionDecisionView",
    "PerceptionReliabilityBundle",
    "TRAINING_TABS",
    "build_perception_audit_projection",
    "build_perception_decision_view",
    "load_perception_reliability_bundle",
    "load_trusted_gate_authorization_registry",
    "load_trusted_gate_authorization_registry_from_workspace",
]
