"""Shadow-only trainee identity authority produced at career setup."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.auto_trainer.career_setup_contracts import (
    CareerSetupPagePacket,
    canonical_sha256,
)


REGISTRY_SCHEMA = "uma.trainee_owner_registry.v1"
AUTHORITY_SCHEMA = "uma.run_trainee_identity_authority.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_TRAINEE_ID_RE = re.compile(r"^[0-9]{4}$")


class RunTraineeIdentityError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _require_sha(value: object, code: str) -> str:
    normalized = str(value).lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise RunTraineeIdentityError(code)
    return normalized


def _canonical_sha(value: object) -> str:
    return canonical_sha256(value)


@dataclass(frozen=True)
class TraineeOwnerRegistryEntry:
    identity_id: str
    trainee_id: str
    factor_key_prefix: str
    source_roles: tuple[str, ...]


@dataclass(frozen=True)
class TraineeOwnerRegistry:
    schema_version: str
    entries: tuple[TraineeOwnerRegistryEntry, ...]
    source_artifacts: tuple[tuple[str, str, str], ...]
    registry_sha256: str
    production_ready: bool = False
    action_eligible: bool = False
    usable_for_action: bool = False

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        project_root: str | Path,
    ) -> "TraineeOwnerRegistry":
        registry_path = Path(path)
        raw = registry_path.read_bytes()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunTraineeIdentityError("trainee_registry_json_invalid") from exc
        if not isinstance(payload, Mapping):
            raise RunTraineeIdentityError("trainee_registry_not_object")
        if payload.get("schema_version") != REGISTRY_SCHEMA:
            raise RunTraineeIdentityError("trainee_registry_schema_invalid")
        if (
            payload.get("production_ready") is not False
            or payload.get("action_eligible") is not False
            or payload.get("usable_for_action") is not False
        ):
            raise RunTraineeIdentityError("trainee_registry_must_be_read_only")

        # Keep the check lexical: ``data`` is an intentional evidence Junction
        # in the integration worktree and resolving it would escape to the
        # shared evidence root before the relative-path policy is evaluated.
        root = Path(project_root).absolute()
        raw_sources = payload.get("source_artifacts")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise RunTraineeIdentityError("trainee_registry_sources_missing")
        sources: list[tuple[str, str, str]] = []
        source_payloads: dict[str, object] = {}
        for item in raw_sources:
            if not isinstance(item, Mapping):
                raise RunTraineeIdentityError("trainee_registry_source_invalid")
            role = str(item.get("role", "")).strip()
            relative = str(item.get("path", "")).strip()
            if (
                not role
                or not relative
                or "\\" in relative
                or relative.startswith("/")
                or ".." in Path(relative).parts
            ):
                raise RunTraineeIdentityError("trainee_registry_source_invalid")
            source_path = (root / relative).absolute()
            try:
                source_path.relative_to(root)
            except ValueError as exc:
                raise RunTraineeIdentityError(
                    "trainee_registry_source_invalid"
                ) from exc
            if not source_path.is_file():
                raise RunTraineeIdentityError("trainee_registry_source_missing")
            expected = _require_sha(
                item.get("sha256"),
                "trainee_registry_source_sha_invalid",
            )
            actual = hashlib.sha256(source_path.read_bytes()).hexdigest()
            if actual != expected:
                raise RunTraineeIdentityError("trainee_registry_source_sha_mismatch")
            if role in source_payloads:
                raise RunTraineeIdentityError("trainee_registry_source_role_duplicate")
            try:
                source_payloads[role] = json.loads(
                    source_path.read_text(encoding="utf-8")
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RunTraineeIdentityError(
                    "trainee_registry_source_json_invalid"
                ) from exc
            sources.append((role, relative, expected))

        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise RunTraineeIdentityError("trainee_registry_entries_missing")
        entries: list[TraineeOwnerRegistryEntry] = []
        seen_identity: set[str] = set()
        seen_trainee: set[str] = set()
        available_roles = set(source_payloads)
        for item in raw_entries:
            if not isinstance(item, Mapping):
                raise RunTraineeIdentityError("trainee_registry_entry_invalid")
            identity_id = str(item.get("identity_id", "")).strip()
            trainee_id = str(item.get("trainee_id", "")).strip()
            factor_prefix = str(item.get("factor_key_prefix", "")).strip()
            raw_roles = item.get("source_roles")
            if (
                _IDENTITY_RE.fullmatch(identity_id) is None
                or _TRAINEE_ID_RE.fullmatch(trainee_id) is None
                or factor_prefix != trainee_id
                or not isinstance(raw_roles, list)
                or not raw_roles
                or any(not isinstance(role, str) or not role for role in raw_roles)
            ):
                raise RunTraineeIdentityError("trainee_registry_entry_invalid")
            roles = tuple(raw_roles)
            if len(set(roles)) != len(roles) or not set(roles) <= available_roles:
                raise RunTraineeIdentityError("trainee_registry_entry_source_invalid")
            if identity_id in seen_identity or trainee_id in seen_trainee:
                raise RunTraineeIdentityError("trainee_registry_entry_duplicate")
            _verify_entry_evidence(
                identity_id=identity_id,
                trainee_id=trainee_id,
                source_roles=roles,
                source_payloads=source_payloads,
            )
            entries.append(
                TraineeOwnerRegistryEntry(
                    identity_id=identity_id,
                    trainee_id=trainee_id,
                    factor_key_prefix=factor_prefix,
                    source_roles=roles,
                )
            )
            seen_identity.add(identity_id)
            seen_trainee.add(trainee_id)
        return cls(
            schema_version=REGISTRY_SCHEMA,
            entries=tuple(entries),
            source_artifacts=tuple(sources),
            registry_sha256=hashlib.sha256(raw).hexdigest(),
        )

    def entry(self, identity_id: str) -> TraineeOwnerRegistryEntry:
        for item in self.entries:
            if item.identity_id == identity_id:
                return item
        raise RunTraineeIdentityError("trainee_identity_not_registered")


def _verify_entry_evidence(
    *,
    identity_id: str,
    trainee_id: str,
    source_roles: tuple[str, ...],
    source_payloads: Mapping[str, object],
) -> None:
    if "factor_key_inventory" not in source_roles:
        raise RunTraineeIdentityError("trainee_registry_factor_evidence_missing")
    factor_payload = source_payloads["factor_key_inventory"]
    if not isinstance(factor_payload, Mapping):
        raise RunTraineeIdentityError("trainee_registry_factor_evidence_invalid")
    horses = factor_payload.get("horses")
    if (
        not isinstance(horses, Mapping)
        or not any(str(key).startswith(trainee_id) for key in horses)
    ):
        raise RunTraineeIdentityError("trainee_registry_factor_key_missing")

    if "career_setup_identity_fixture" in source_roles:
        setup = source_payloads["career_setup_identity_fixture"]
        trainee = setup.get("trainee") if isinstance(setup, Mapping) else None
        if (
            not isinstance(trainee, Mapping)
            or trainee.get("identity_id") != identity_id
        ):
            raise RunTraineeIdentityError(
                "trainee_registry_career_identity_mismatch"
            )
    if "reviewed_event_owner_fixture" in source_roles:
        fixture = source_payloads["reviewed_event_owner_fixture"]
        events = fixture.get("events") if isinstance(fixture, Mapping) else None
        if not isinstance(events, list) or not any(
            isinstance(event, Mapping)
            and isinstance(event.get("owner"), Mapping)
            and event["owner"].get("owner_id") == trainee_id
            for event in events
        ):
            raise RunTraineeIdentityError("trainee_registry_event_owner_missing")


@dataclass(frozen=True)
class RunTraineeIdentityAuthority:
    schema_version: str
    run_session_id: str
    career_setup_packet_sha256: str
    source_frame_sha256: str
    career_setup_allowlist_sha256: str
    registry_sha256: str
    identity_id: str
    owner_type: str
    owner_id_namespace: str
    owner_id: str
    authority_sha256: str
    action_eligible: bool = False
    usable_for_action: bool = False

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> "RunTraineeIdentityAuthority":
        if not isinstance(payload, Mapping):
            raise RunTraineeIdentityError("trainee_authority_payload_invalid")
        expected_fields = frozenset(
            {
                "schema_version",
                "run_session_id",
                "career_setup_packet_sha256",
                "source_frame_sha256",
                "career_setup_allowlist_sha256",
                "registry_sha256",
                "identity_id",
                "owner_type",
                "owner_id_namespace",
                "owner_id",
                "authority_sha256",
                "action_eligible",
                "usable_for_action",
            }
        )
        if set(payload) != expected_fields:
            raise RunTraineeIdentityError("trainee_authority_fields_invalid")
        return cls(**dict(payload))  # type: ignore[arg-type]

    def __post_init__(self) -> None:
        if self.schema_version != AUTHORITY_SCHEMA:
            raise RunTraineeIdentityError("trainee_authority_schema_invalid")
        if not isinstance(self.run_session_id, str) or not self.run_session_id:
            raise RunTraineeIdentityError("trainee_run_session_invalid")
        _require_sha(
            self.career_setup_packet_sha256,
            "trainee_packet_sha_invalid",
        )
        _require_sha(self.source_frame_sha256, "trainee_source_frame_sha_invalid")
        _require_sha(
            self.career_setup_allowlist_sha256,
            "trainee_allowlist_sha_invalid",
        )
        _require_sha(self.registry_sha256, "trainee_registry_sha_invalid")
        if _IDENTITY_RE.fullmatch(self.identity_id) is None:
            raise RunTraineeIdentityError("trainee_identity_invalid")
        if (
            self.owner_type != "trainee"
            or self.owner_id_namespace != "trainee_id"
            or _TRAINEE_ID_RE.fullmatch(self.owner_id) is None
        ):
            raise RunTraineeIdentityError("trainee_owner_invalid")
        if self.action_eligible is not False or self.usable_for_action is not False:
            raise RunTraineeIdentityError("trainee_authority_must_be_read_only")
        _require_sha(self.authority_sha256, "trainee_authority_sha_invalid")
        material = self.to_dict()
        material.pop("authority_sha256")
        if _canonical_sha(material) != self.authority_sha256:
            raise RunTraineeIdentityError("trainee_authority_sha_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_session_id": self.run_session_id,
            "career_setup_packet_sha256": self.career_setup_packet_sha256,
            "source_frame_sha256": self.source_frame_sha256,
            "career_setup_allowlist_sha256": self.career_setup_allowlist_sha256,
            "registry_sha256": self.registry_sha256,
            "identity_id": self.identity_id,
            "owner_type": self.owner_type,
            "owner_id_namespace": self.owner_id_namespace,
            "owner_id": self.owner_id,
            "authority_sha256": self.authority_sha256,
            "action_eligible": False,
            "usable_for_action": False,
        }


def build_run_trainee_identity_authority(
    *,
    run_session_id: str,
    packet: CareerSetupPagePacket,
    career_setup_allowlist_sha256: str,
    registry: TraineeOwnerRegistry,
) -> RunTraineeIdentityAuthority:
    if not isinstance(packet, CareerSetupPagePacket) or not _packet_is_accepted(packet):
        raise RunTraineeIdentityError("trainee_packet_not_accepted")
    if not isinstance(registry, TraineeOwnerRegistry):
        raise RunTraineeIdentityError("trainee_registry_required")
    allowlist_sha = _require_sha(
        career_setup_allowlist_sha256,
        "trainee_allowlist_sha_invalid",
    )
    identity_id = packet.identities[0]
    entry = registry.entry(identity_id)
    payload: dict[str, object] = {
        "schema_version": AUTHORITY_SCHEMA,
        "run_session_id": run_session_id,
        "career_setup_packet_sha256": canonical_sha256(packet.to_dict()),
        "source_frame_sha256": packet.source_sha256,
        "career_setup_allowlist_sha256": allowlist_sha,
        "registry_sha256": registry.registry_sha256,
        "identity_id": identity_id,
        "owner_type": "trainee",
        "owner_id_namespace": "trainee_id",
        "owner_id": entry.trainee_id,
        "action_eligible": False,
        "usable_for_action": False,
    }
    payload["authority_sha256"] = _canonical_sha(payload)
    return RunTraineeIdentityAuthority(**payload)  # type: ignore[arg-type]


def _packet_is_accepted(packet: CareerSetupPagePacket) -> bool:
    return bool(
        packet.page_family == "trainee"
        and packet.page_state == "trainee_select_ready"
        and packet.action_id == "safe_advance.trainee_continue"
        and len(packet.identities) == 1
        and packet.identities[0] != "unknown"
        and len(packet.identity_matches) == 1
        and packet.identity_matches[0] is True
        and not packet.errors
        and _SHA256_RE.fullmatch(packet.source_sha256) is not None
    )


__all__ = [
    "AUTHORITY_SCHEMA",
    "REGISTRY_SCHEMA",
    "RunTraineeIdentityAuthority",
    "RunTraineeIdentityError",
    "TraineeOwnerRegistry",
    "TraineeOwnerRegistryEntry",
    "build_run_trainee_identity_authority",
]
