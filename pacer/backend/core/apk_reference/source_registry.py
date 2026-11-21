"""Pinned APK reference source metadata and verified JSON loading."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True)
class ReferenceSourceSpec:
    key: str
    filename: str
    sha256: str
    role: str


class ReferenceSourceError(ValueError):
    """A reference source could not pass a verified loading step."""

    def __init__(self, reason: str, key: str):
        self.reason = reason
        self.key = key
        super().__init__(f"{reason}: {key}")


SOURCES: Mapping[str, ReferenceSourceSpec] = MappingProxyType({
    "event": ReferenceSourceSpec(
        key="event",
        filename="event_database.json",
        sha256="8438858dcbad89a7467d6338881db975134512da441acd015c1cc119692b76db",
        role="production_candidate",
    ),
    "factor": ReferenceSourceSpec(
        key="factor",
        filename="factor_database.json",
        sha256="7d745789008d4fb44e7d77e0d20f867f7f2f747a7d639b8f3f1ae4e3e9aa9d66",
        role="production_candidate",
    ),
    "resource": ReferenceSourceSpec(
        key="resource",
        filename="resource_library.json",
        sha256="c4cbba7c0bce6501349f1e212f350b340d3a6311adc4bb5db25cf5a1e6a00d01",
        role="quarantined",
    ),
    "race": ReferenceSourceSpec(
        key="race",
        filename="race_database.json",
        sha256="50d7088f10d0517741c45bbb6b215b173375b537c631b10607415742757916a8",
        role="metadata_only",
    ),
    "skill": ReferenceSourceSpec(
        key="skill",
        filename="skill_database.json",
        sha256="34e0be5e78af7e843bdfa3d2db11441c07b75aaf0e3a700c17741d70464e46a5",
        role="candidate_ledger",
    ),
    "training": ReferenceSourceSpec(
        key="training",
        filename="training_templates.json",
        sha256="fd320d72cc8be8476cac6abf1a4f71dc9cc7ced84cc37d37de1a9348be13a167",
        role="audit_only",
    ),
})


def load_verified_json(
    key: str,
    root: Path = Path("data/apk_extracted"),
    registry: Mapping[str, ReferenceSourceSpec] = SOURCES,
) -> Any:
    """Load JSON only after its bytes match the registered SHA-256 digest."""
    try:
        spec = registry[key]
    except KeyError:
        raise ReferenceSourceError("unknown_source", key) from None

    try:
        payload = (root / spec.filename).read_bytes()
    except FileNotFoundError:
        raise ReferenceSourceError("missing_file", key) from None

    if hashlib.sha256(payload).hexdigest() != spec.sha256:
        raise ReferenceSourceError("source_sha256_mismatch", key)

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ReferenceSourceError("invalid_utf8", key) from None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise ReferenceSourceError("invalid_json", key) from None
