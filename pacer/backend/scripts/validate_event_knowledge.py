"""Validate a local, action-incapable event knowledge staging snapshot.

JSON Schema ``format`` values remain Draft 2020-12 annotations and are not
asserted here. Security-relevant source URLs are validated by explicit local
rules instead.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any


EXPECTED_SCHEMA_ID = "https://uma.local/schema/event-knowledge/v1"
EXPECTED_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
EXPECTED_SCHEMA_VERSION = "uma.event_knowledge.v1"
EXPECTED_APK_SHA256 = (
    "8438858dcbad89a7467d6338881db975134512da441acd015c1cc119692b76db"
)
STORY_EVENT_ID = re.compile(r"jp\.single_mode_story:([0-9]+)\Z")
OBSERVED_EVENT_ID = re.compile(r"cn\.observed:[0-9a-f]{12}\Z")
UNVERIFIED_EVENT_ID = re.compile(r"source\.unverified:[0-9a-f]{12}\Z")
SOURCE_FIELDS = {
    "source_url",
    "source_title",
    "retrieved_at",
    "region",
    "server",
    "release_status",
    "source_confidence",
}
RELEASE_STATES = {"current", "future", "unknown"}
OWNER_TYPES = {"trainee", "support", "scenario", "common"}
REVIEW_STATES = {"cross_checked", "single_source", "needs_review"}
ALIGNMENT_STATES = {"matched", "ambiguous", "unmatched"}
ALIGNMENT_METHODS = {"exact", "approved_alias", "none"}
EVENT_KINDS = {
    "choice",
    "no_choice",
    "outgoing",
    "training_after",
    "random",
    "chain",
}
EFFECT_KINDS = {
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
    "outing_unavailable",
    "chain_end",
}
NUMERIC_EFFECT_KINDS = {
    "speed",
    "stamina",
    "power",
    "guts",
    "wit",
    "energy",
    "max_energy",
    "mood",
    "skill_points",
}
OWNER_NAMESPACES = {
    "trainee": {"trainee_id"},
    "support": {"support_card_id", "support_chara_id"},
    "scenario": {"scenario_id"},
    "common": {"common_id"},
}
CN_RELEASE_REGISTRY_STATES = {
    "current",
    "announced_future",
    "upstream_only",
    "unknown_cn",
}


class EventKnowledgeValidationError(ValueError):
    """A snapshot failed a stable, fail-closed validation rule."""

    def __init__(self, reason: str, event_id: str | None = None):
        self.reason = reason
        self.event_id = event_id
        suffix = f": {event_id}" if event_id else ""
        super().__init__(f"{reason}{suffix}")


def validate_snapshot(snapshot: Any, schema: Any) -> dict[str, Any]:
    """Validate staging invariants and return a deterministic safe summary."""
    if (
        not isinstance(schema, dict)
        or schema.get("$id") != EXPECTED_SCHEMA_ID
        or schema.get("$schema") != EXPECTED_SCHEMA_DIALECT
    ):
        _fail("invalid_schema")
    if not isinstance(snapshot, dict):
        _fail("invalid_snapshot_shape")
    if snapshot.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        _fail("invalid_schema_version")
    if snapshot.get("usable_for_action") is not False:
        _fail("snapshot_action_usable")
    if snapshot.get("apk_event_database_sha256") != EXPECTED_APK_SHA256:
        _fail("apk_sha256_mismatch")

    events = snapshot.get("events")
    if not isinstance(events, list) or not events:
        _fail("invalid_events")

    seen_event_ids: set[str] = set()
    owner_counts: Counter[str] = Counter()
    release_counts: Counter[str] = Counter()
    review_counts: Counter[str] = Counter()
    alignment_counts: Counter[str] = Counter()
    same_owner_titles: dict[
        tuple[str, str, str, str], list[tuple[str, str, frozenset[str]]]
    ] = {}

    for event in events:
        if not isinstance(event, dict):
            _fail("invalid_event_shape")
        event_id = event.get("event_id")
        match = STORY_EVENT_ID.fullmatch(event_id) if isinstance(event_id, str) else None
        if not (
            (match is not None and event.get("story_id") == match.group(1))
            or (
                (OBSERVED_EVENT_ID.fullmatch(event_id or "") or UNVERIFIED_EVENT_ID.fullmatch(event_id or ""))
                and event.get("story_id") is None
            )
        ):
            _fail("invalid_event_id", event_id if isinstance(event_id, str) else None)
        if event_id in seen_event_ids:
            _fail("duplicate_event_id", event_id)
        seen_event_ids.add(event_id)
        if event.get("usable_for_action") is not False:
            _fail("event_action_usable", event_id)

        owner_type = _validate_owner(event.get("owner"), event_id)
        owner_counts[owner_type] += 1

        titles = event.get("titles")
        if not isinstance(titles, dict) or not titles:
            _fail("missing_titles", event_id)
        canonical_title = _validate_titles(titles, event_id)
        event_kind = event.get("event_kind")
        if event_kind not in EVENT_KINDS:
            _fail("invalid_event_kind", event_id)

        source_domains = _validate_sources(event.get("sources"), event_id)
        high_risk = _validate_options(event.get("options"), event_kind, event_id)

        availability = event.get("region_availability")
        if not isinstance(availability, list) or not availability:
            _fail("missing_availability", event_id)
        for item in availability:
            if not isinstance(item, dict):
                _fail("invalid_availability", event_id)
            release_status = item.get("release_status")
            if release_status not in RELEASE_STATES:
                _fail("invalid_release_status", event_id)
            if type(item.get("live_eligible")) is not bool:
                _fail("invalid_live_eligible", event_id)
            if (
                item.get("region") == "cn"
                and release_status in {"future", "unknown"}
                and item.get("live_eligible") is not False
            ):
                _fail("future_or_unknown_live_eligible", event_id)
            if item.get("region") == "cn":
                release_counts[release_status] += 1

        review = event.get("review_status")
        review_state = review.get("state") if isinstance(review, dict) else None
        if review_state not in REVIEW_STATES:
            _fail("invalid_review_status", event_id)
        if review_state == "cross_checked" and high_risk and len(source_domains) < 2:
            _fail("insufficient_high_risk_sources", event_id)
        review_counts[review_state] += 1

        alignment = event.get("alignment")
        if not isinstance(alignment, dict):
            _fail("missing_alignment", event_id)
        alignment_state = alignment.get("status")
        if alignment_state not in ALIGNMENT_STATES:
            _fail("invalid_alignment_status", event_id)
        if alignment.get("method") not in ALIGNMENT_METHODS:
            _fail("invalid_alignment_method", event_id)
        if alignment.get("apk_db_sha256") != EXPECTED_APK_SHA256:
            _fail("alignment_apk_sha256_mismatch", event_id)
        if (
            alignment_state in {"ambiguous", "unmatched"}
            and alignment.get("usable_for_action") is not False
        ):
            _fail("unsafe_alignment", event_id)
        alignment_counts[alignment_state] += 1
        trigger_conditions = event.get("trigger_conditions")
        trigger_set = (
            frozenset(trigger_conditions)
            if isinstance(trigger_conditions, list)
            and all(isinstance(item, str) for item in trigger_conditions)
            else frozenset()
        )
        owner = event["owner"]
        collision_key = (
            owner["owner_type"],
            owner["owner_id_namespace"],
            owner["owner_id"],
            _normalize_title(canonical_title),
        )
        same_owner_titles.setdefault(collision_key, []).append(
            (event_id, alignment_state, trigger_set)
        )

    _validate_same_owner_title_collisions(same_owner_titles)
    _validate_schema_contract(snapshot, schema)

    return {
        "alignment_counts": dict(sorted(alignment_counts.items())),
        "cn_release_status_counts": dict(sorted(release_counts.items())),
        "dataset_version": snapshot.get("dataset_version"),
        "event_count": len(events),
        "owner_counts": dict(sorted(owner_counts.items())),
        "review_counts": dict(sorted(review_counts.items())),
        "schema_version": snapshot.get("schema_version"),
        "usable_for_action": False,
    }


def validate_release_registry(registry: Any) -> dict[str, Any]:
    """Validate the evidence-state registry without granting live authority."""
    if not isinstance(registry, dict):
        _fail("invalid_release_registry")
    if registry.get("registry_version") != "uma.cn_release_registry.v1":
        _fail("invalid_release_registry_version")
    if registry.get("usable_for_action") is not False:
        _fail("release_registry_action_usable")
    defined_statuses = registry.get("defined_statuses")
    if (
        not isinstance(defined_statuses, list)
        or set(defined_statuses) != CN_RELEASE_REGISTRY_STATES
        or len(defined_statuses) != len(CN_RELEASE_REGISTRY_STATES)
    ):
        _fail("invalid_release_registry_statuses")
    status_definitions = registry.get("status_definitions")
    if (
        not isinstance(status_definitions, dict)
        or set(status_definitions) != CN_RELEASE_REGISTRY_STATES
        or not all(_nonempty_string(value) for value in status_definitions.values())
    ):
        _fail("invalid_release_registry_definitions")
    entries = registry.get("entries")
    if not isinstance(entries, list) or not entries:
        _fail("invalid_release_registry_entries")

    seen_subjects: set[tuple[str, str]] = set()
    status_counts: Counter[str] = Counter()
    for entry in entries:
        if not isinstance(entry, dict):
            _fail("invalid_release_registry_entry")
        namespace = entry.get("subject_namespace")
        subject_id = entry.get("subject_id")
        if namespace not in {
            "trainee_id",
            "support_card_id",
            "support_chara_id",
            "scenario_id",
            "common_id",
        } or not isinstance(subject_id, str) or not subject_id:
            _fail("invalid_release_registry_subject")
        subject = (namespace, subject_id)
        if subject in seen_subjects:
            _fail("duplicate_release_registry_subject")
        seen_subjects.add(subject)

        status = entry.get("status")
        if status not in CN_RELEASE_REGISTRY_STATES:
            _fail("invalid_release_registry_status")
        evidence = entry.get("positive_cn_release_evidence")
        if not isinstance(evidence, list) or not all(
            isinstance(item, str) and item for item in evidence
        ):
            _fail("invalid_release_registry_evidence")
        if status in {"current", "announced_future"} and not evidence:
            _fail("missing_positive_cn_release_evidence")
        if type(entry.get("live_eligible")) is not bool:
            _fail("invalid_release_registry_live_eligible")
        if entry["live_eligible"] is not False:
            _fail("release_registry_live_eligible")
        status_counts[status] += 1

    return {
        "defined_statuses": sorted(CN_RELEASE_REGISTRY_STATES),
        "entry_count": len(entries),
        "registry_version": registry["registry_version"],
        "status_counts": dict(sorted(status_counts.items())),
        "usable_for_action": False,
    }


def _validate_owner(owner: Any, event_id: str) -> str:
    if not isinstance(owner, dict):
        _fail("invalid_owner", event_id)
    owner_type = owner.get("owner_type")
    owner_id = owner.get("owner_id")
    if owner_type not in OWNER_TYPES or not isinstance(owner_id, str) or not owner_id:
        _fail("invalid_owner", event_id)
    if owner.get("owner_id_namespace") not in OWNER_NAMESPACES[owner_type]:
        _fail("owner_namespace_mismatch", event_id)
    return owner_type


def _validate_sources(sources: Any, event_id: str) -> set[str]:
    if not isinstance(sources, list) or not sources:
        _fail("missing_sources", event_id)
    domains: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or not SOURCE_FIELDS.issubset(source):
            _fail("missing_source_field", event_id)
        if source.get("release_status") not in RELEASE_STATES:
            _fail("invalid_source_release_status", event_id)
        source_url = source.get("source_url")
        match = re.match(r"https?://([^/]+)", source_url) if isinstance(source_url, str) else None
        if match is None:
            _fail("invalid_source_url", event_id)
        domains.add(match.group(1).casefold())
    return domains


def _validate_titles(titles: dict[str, Any], event_id: str) -> str:
    canonical_titles = [
        value
        for locale in ("ja", "zh_cn", "zh_tw", "en")
        if _nonempty_string(value := titles.get(locale))
    ]
    if not canonical_titles:
        _fail("missing_titles", event_id)
    aliases = titles.get("aliases", [])
    if not isinstance(aliases, list):
        _fail("invalid_titles", event_id)
    aliases_text = []
    for alias in aliases:
        if (
            not isinstance(alias, dict)
            or not _nonempty_string(alias.get("locale"))
            or not _nonempty_string(alias.get("text"))
            or (
                "source" in alias
                and not _nonempty_string(alias.get("source"))
            )
        ):
            _fail("invalid_titles", event_id)
        aliases_text.append(alias["text"])
    canonical_keys = {_normalize_title(title) for title in canonical_titles}
    alias_keys = [_normalize_title(title) for title in aliases_text]
    if len(alias_keys) != len(set(alias_keys)) or canonical_keys.intersection(alias_keys):
        _fail("duplicate_event_title", event_id)
    return titles.get("zh_cn") or titles.get("ja") or canonical_titles[0]


def _normalize_title(title: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", title).split()).casefold()


def _validate_same_owner_title_collisions(
    collisions: dict[
        tuple[str, str, str, str], list[tuple[str, str, frozenset[str]]]
    ],
) -> None:
    for records in collisions.values():
        if len(records) < 2:
            continue
        states = [state for _, state, _ in records]
        trigger_sets = [conditions for _, _, conditions in records]
        if (
            any(state != "ambiguous" for state in states)
            or any(not conditions for conditions in trigger_sets)
            or len(set(trigger_sets)) != len(trigger_sets)
        ):
            _fail("unsafe_same_owner_title_collision", records[0][0])


def _validate_options(options: Any, event_kind: str, event_id: str) -> bool:
    if not isinstance(options, list) or not options:
        _fail("missing_options", event_id)
    indexes = [
        option.get("index") for option in options if isinstance(option, dict)
    ]
    if event_kind == "no_choice" and indexes != [0]:
        _fail("invalid_no_choice_option_index", event_id)
    if event_kind != "no_choice" and 0 in indexes:
        _fail("choice_option_index_zero", event_id)
    high_risk = False
    seen_option_ids: set[str] = set()
    seen_option_indexes: set[int] = set()
    for option in options:
        if not isinstance(option, dict):
            _fail("invalid_option", event_id)
        option_id = option.get("option_id")
        option_index = option.get("index")
        if not isinstance(option_id, str) or not option_id:
            _fail("invalid_option_id", event_id)
        if option_id in seen_option_ids:
            _fail("duplicate_option_id", event_id)
        seen_option_ids.add(option_id)
        if type(option_index) is not int or option_index < 0:
            _fail("invalid_option_index", event_id)
        if option_index in seen_option_indexes:
            _fail("duplicate_option_index", event_id)
        seen_option_indexes.add(option_index)
        effects = option.get("effects")
        branches = option.get("conditional_branches")
        if not isinstance(effects, list) or not isinstance(branches, list):
            _fail("invalid_option", event_id)
        for effect in effects:
            high_risk = _validate_effect(effect, event_id) or high_risk
        seen_branch_outcomes: set[str] = set()
        seen_branch_condition_sets: set[tuple[str, ...]] = set()
        for branch in branches:
            high_risk = True
            if not isinstance(branch, dict):
                _fail("invalid_branch", event_id)
            outcome = branch.get("outcome")
            if not _nonempty_string(outcome):
                _fail("invalid_branch", event_id)
            if outcome in seen_branch_outcomes:
                _fail("duplicate_branch_outcome", event_id)
            seen_branch_outcomes.add(outcome)
            conditions = branch.get("conditions", [])
            if not isinstance(conditions, list) or not all(
                _nonempty_string(condition) for condition in conditions
            ):
                _fail("invalid_branch", event_id)
            condition_set = tuple(sorted(conditions))
            if condition_set and condition_set in seen_branch_condition_sets:
                _fail("duplicate_branch_conditions", event_id)
            if condition_set:
                seen_branch_condition_sets.add(condition_set)
            probability = branch.get("probability")
            if probability is not None:
                if (
                    type(probability) not in {int, float}
                    or not 0 <= probability <= 1
                    or not isinstance(branch.get("probability_source"), str)
                    or not branch["probability_source"]
                ):
                    _fail("unreviewed_probability", event_id)
            branch_effects = branch.get("effects")
            if not isinstance(branch_effects, list) or not branch_effects:
                _fail("invalid_branch", event_id)
            for effect in branch_effects:
                high_risk = _validate_effect(effect, event_id) or high_risk
        _validate_branch_resolution(effects, branches, event_id)
    return high_risk


def _validate_branch_resolution(
    base_effects: list[Any],
    branches: list[Any],
    event_id: str,
) -> None:
    condition_by_outcome = {
        "passion_zone_active_skill_points": "passion_zone_active",
        "passion_zone_inactive_skill_points": "passion_zone_inactive",
    }
    resolved: dict[str, dict[str, Any]] = {}
    for branch in branches:
        outcome = branch.get("outcome") if isinstance(branch, dict) else None
        expected_condition = condition_by_outcome.get(outcome)
        if expected_condition is None:
            continue
        conditions = branch.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            _fail("missing_branch_resolution", event_id)
        if conditions != [expected_condition]:
            _fail("unknown_branch_resolution", event_id)
        resolved[expected_condition] = branch

    if not {"passion_zone_active", "passion_zone_inactive"}.issubset(resolved):
        return
    resolved_kinds = {
        effect.get("kind")
        for condition in ("passion_zone_active", "passion_zone_inactive")
        for effect in resolved[condition]["effects"]
        if isinstance(effect, dict) and effect.get("op") == "add"
    }
    if any(
        isinstance(effect, dict)
        and effect.get("op") == "add"
        and effect.get("kind") in resolved_kinds
        for effect in base_effects
    ):
        _fail("overlapping_branch_resolution", event_id)


def _validate_effect(effect: Any, event_id: str) -> bool:
    if not isinstance(effect, dict) or not isinstance(effect.get("kind"), str):
        _fail("invalid_effect", event_id)
    kind = effect["kind"]
    if kind not in EFFECT_KINDS:
        _fail("invalid_effect_kind", event_id)
    op = effect.get("op")
    allowed_ops = {
        **{numeric_kind: {"add", "set"} for numeric_kind in NUMERIC_EFFECT_KINDS},
        "bond": {"add"},
        "skill_hint": {"acquire"},
        "status_add": {"acquire"},
        "status_remove": {"remove"},
        "scenario_item": {"acquire"},
        "passion_zone": {"acquire"},
        "outing_unlock": {"acquire"},
        "outing_unavailable": {"acquire"},
        "chain_end": {"end"},
    }
    if op not in allowed_ops[kind]:
        _fail("invalid_effect_op", event_id)

    if kind in NUMERIC_EFFECT_KINDS | {"bond"} and "value" not in effect:
        _fail("effect_missing_value", event_id)
    if kind == "bond" and not _nonempty_string(effect.get("owner_id")):
        _fail("effect_missing_owner_id", event_id)
    if kind == "skill_hint":
        if not _nonempty_string(effect.get("name_ja")):
            _fail("effect_missing_name_ja", event_id)
        if type(effect.get("level")) is not int or effect["level"] < 1:
            _fail("effect_missing_level", event_id)
    if kind in {"status_add", "status_remove"} and not _nonempty_string(
        effect.get("status_name")
    ):
        _fail("effect_missing_status_name", event_id)
    if kind == "scenario_item":
        if not _nonempty_string(effect.get("item")):
            _fail("effect_missing_item", event_id)
        if effect.get("color") not in {"red", "blue", "yellow"}:
            _fail("effect_missing_color", event_id)

    high_risk = effect["kind"] in {
        "status_add",
        "status_remove",
        "scenario_item",
        "passion_zone",
        "outing_unlock",
        "outing_unavailable",
        "chain_end",
    }
    value = effect.get("value")
    if kind in NUMERIC_EFFECT_KINDS | {"bond"} and not (
        type(value) is int or isinstance(value, dict)
    ):
        _fail("invalid_effect_value", event_id)
    base_value = effect.get("base_value")
    if base_value is not None:
        if type(base_value) is not int or kind not in NUMERIC_EFFECT_KINDS | {"bond"}:
            _fail("invalid_base_value", event_id)
        if type(value) is int and base_value != value:
            _fail("base_value_mismatch", event_id)
    if not isinstance(value, dict):
        return high_risk
    high_risk = True
    if (
        type(value.get("min")) is not int
        or type(value.get("max")) is not int
        or value["min"] > value["max"]
    ):
        _fail("invalid_effect_range", event_id)
    if base_value is not None and not value["min"] <= base_value <= value["max"]:
        _fail("base_value_out_of_range", event_id)
    scaling_context = effect.get("scaling_context")
    if (
        not isinstance(scaling_context, dict)
        or not isinstance(scaling_context.get("depends_on"), list)
        or not scaling_context["depends_on"]
    ):
        _fail("range_without_scaling_context", event_id)
    return high_risk


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


class _SchemaContractViolation(ValueError):
    pass


def _validate_schema_contract(instance: Any, schema: dict[str, Any]) -> None:
    """Execute the asserted Draft 2020-12 subset used by this schema.

    The ``format`` keyword remains annotation-only, matching the dialect's
    default behavior; explicit source URL rules cover the safety-relevant URI.
    """
    if not any(key in schema for key in ("type", "properties", "$ref", "allOf")):
        return
    try:
        _apply_schema(instance, schema, schema)
    except _SchemaContractViolation:
        _fail("schema_contract_violation")


def _apply_schema(instance: Any, rule: Any, root: dict[str, Any]) -> None:
    if rule is True:
        return
    if rule is False or not isinstance(rule, dict):
        raise _SchemaContractViolation
    if "$ref" in rule:
        _apply_schema(instance, _resolve_local_ref(root, rule["$ref"]), root)
    for child in rule.get("allOf", []):
        _apply_schema(instance, child, root)
    if "if" in rule and _schema_matches(instance, rule["if"], root):
        if "then" in rule:
            _apply_schema(instance, rule["then"], root)

    if "type" in rule and not _matches_json_type(instance, rule["type"]):
        raise _SchemaContractViolation
    if "const" in rule and not _json_equal(instance, rule["const"]):
        raise _SchemaContractViolation
    if "enum" in rule and not any(_json_equal(instance, item) for item in rule["enum"]):
        raise _SchemaContractViolation
    if "oneOf" in rule:
        if sum(_schema_matches(instance, child, root) for child in rule["oneOf"]) != 1:
            raise _SchemaContractViolation

    if isinstance(instance, dict):
        required = rule.get("required", [])
        if any(key not in instance for key in required):
            raise _SchemaContractViolation
        if len(instance) < rule.get("minProperties", 0):
            raise _SchemaContractViolation
        properties = rule.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                _apply_schema(value, properties[key], root)
            elif rule.get("additionalProperties") is False:
                raise _SchemaContractViolation
            elif isinstance(rule.get("additionalProperties"), dict):
                _apply_schema(value, rule["additionalProperties"], root)
    elif isinstance(instance, list):
        if len(instance) < rule.get("minItems", 0):
            raise _SchemaContractViolation
        if "items" in rule:
            for item in instance:
                _apply_schema(item, rule["items"], root)
    elif isinstance(instance, str):
        if len(instance) < rule.get("minLength", 0):
            raise _SchemaContractViolation
        if "pattern" in rule and re.search(rule["pattern"], instance) is None:
            raise _SchemaContractViolation
    elif type(instance) in {int, float}:
        if "minimum" in rule and instance < rule["minimum"]:
            raise _SchemaContractViolation
        if "maximum" in rule and instance > rule["maximum"]:
            raise _SchemaContractViolation


def _schema_matches(instance: Any, rule: Any, root: dict[str, Any]) -> bool:
    try:
        _apply_schema(instance, rule, root)
    except _SchemaContractViolation:
        return False
    return True


def _resolve_local_ref(root: dict[str, Any], reference: Any) -> Any:
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise _SchemaContractViolation
    current: Any = root
    for token in reference[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or token not in current:
            raise _SchemaContractViolation
        current = current[token]
    return current


def _matches_json_type(instance: Any, expected: Any) -> bool:
    expected_types = expected if isinstance(expected, list) else [expected]
    checks = {
        "null": instance is None,
        "boolean": type(instance) is bool,
        "object": isinstance(instance, dict),
        "array": isinstance(instance, list),
        "string": isinstance(instance, str),
        "integer": type(instance) is int,
        "number": type(instance) in {int, float},
    }
    return any(checks.get(name, False) for name in expected_types)


def _json_equal(left: Any, right: Any) -> bool:
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    return left == right


def _fail(reason: str, event_id: str | None = None) -> None:
    raise EventKnowledgeValidationError(reason, event_id)


def _load_json(path: Path) -> Any:
    try:
        payload = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _fail("missing_file")
    except UnicodeDecodeError:
        _fail("invalid_utf8")
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        _fail("invalid_json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    arguments = parser.parse_args(argv)

    try:
        schema = _load_json(arguments.schema)
        snapshot = _load_json(arguments.input)
        summary = validate_snapshot(snapshot, schema)
    except EventKnowledgeValidationError as error:
        print(
            json.dumps(
                {"event_id": error.event_id, "reason": error.reason},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
