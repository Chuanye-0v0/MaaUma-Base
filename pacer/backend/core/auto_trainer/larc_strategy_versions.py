"""Boundary revision registry and goal policy serialization contract for L'Arc.

External boundary functions preserve saved ``goal.json`` loading and UI preset
mapping while extended revisions are registered dynamically by external packs.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from core.auto_trainer.larc_strategy_extension import ensure_bootstrapped, get_registry
from core.auto_trainer.larc_strategy_params import AUTO_ROUTED_REVISIONS, DEFAULT_PARAMS, PRESET_REGISTRY

UNIFIED_VARIANT = "baseline"
LEGACY_REVISION = "1.1"
BASE_CURRENT_REVISION = "2.3"

BASE_REVISION_POLICY_VERSIONS = {
    LEGACY_REVISION: "larc_unified_v1_1_preview",
    "1.2": "larc_unified_v1_2_preview",
    "1.3": "larc_unified_v1_3_preview",
    "1.4": "larc_unified_v1_4_preview",
    "1.5": "larc_unified_v1_5_preview",
    "1.6": "larc_unified_v1_6_preview",
    "1.6.1": "larc_unified_v1_6_1_preview",
    "1.7": "larc_unified_v1_7_preview",
    "1.7.1": "larc_unified_v1_7_1_preview",
    "1.7.2": "larc_unified_v1_7_2_preview",
    "1.7.3": "larc_unified_v1_7_3_preview",
    "1.7.4": "larc_unified_v1_7_4_preview",
    "1.7.4.1": "larc_unified_v1_7_4_1_preview",
    "1.7.5": "larc_unified_v1_7_5_preview",
    "1.7.5.2": "larc_unified_v1_7_5_2_preview",
    "1.7.7": "larc_unified_v1_7_7_preview",
    "1.7.7.1": "larc_unified_v1_7_7_1_preview",
    "1.7.7.2": "larc_unified_v1_7_7_2_preview",
    "1.7.8": "larc_unified_v1_7_8_preview",
    "1.7.8.1": "larc_unified_v1_7_8_1_preview",
    "1.7.9": "larc_unified_v1_7_9_preview",
    "1.7.11": "larc_unified_v1_7_11_preview",
    "1.7.13": "larc_unified_v1_7_13_preview",
    "1.7.15": "larc_unified_v1_7_15_preview",
    "1.7.16.1": "larc_unified_v1_7_16_1_preview",
    "1.7.26.3": "larc_unified_v1_7_26_3_preview",
    "1.8": "larc_unified_v1_8_preview",
    "1.8.1": "larc_unified_v1_8_1_preview",
    "1.8.2": "larc_unified_v1_8_2_preview",
    "1.8.3": "larc_unified_v1_8_3_preview",
    "1.8.4": "larc_unified_v1_8_4_preview",
    "1.8.5": "larc_unified_v1_8_5_preview",
    "1.8.5.1": "larc_unified_v1_8_5_1_preview",
    "1.9": "larc_unified_v1_9_preview",
    "1.9.2": "larc_unified_v1_9_2_preview",
    "1.9.2.1": "larc_unified_v1_9_2_1_preview",
    "1.9.3": "larc_unified_v1_9_3_preview",
    "1.9.4": "larc_unified_v1_9_4_preview",
    "1.9.5": "larc_unified_v1_9_5_preview",
    "1.9.6": "larc_unified_v1_9_6_preview",
    "1.9.7": "larc_unified_v1_9_7_preview",
    "1.9.8": "larc_unified_v1_9_8_preview",
    "1.9.9": "larc_unified_v1_9_9_preview",
    "1.9.10": "larc_unified_v1_9_10_preview",
    "1.9.11": "larc_unified_v1_9_11_preview",
    "1.9.12": "larc_unified_v1_9_12_preview",
    "2.0": "larc_unified_v2_0_preview",
    "2.1": "larc_unified_v2_1_preview",
    "2.2": "larc_unified_v2_2_preview",
    "2.3": "larc_unified_v2_3_preview",
}

REVISION_POLICY_VERSIONS: dict[str, str] = dict(BASE_REVISION_POLICY_VERSIONS)
CURRENT_REVISION: str = BASE_CURRENT_REVISION
REVISION_PRESETS: dict[str, str] = {}
PRESET_REVISIONS: dict[str, str] = {}


def _sync_versions(registry: object) -> None:
    global CURRENT_REVISION
    REVISION_POLICY_VERSIONS.clear()
    REVISION_POLICY_VERSIONS.update(BASE_REVISION_POLICY_VERSIONS)
    REVISION_POLICY_VERSIONS.update(getattr(registry, "policy_versions", {}) or {})
    default_rev = getattr(registry, "default_current_revision", None)
    CURRENT_REVISION = default_rev if (default_rev and default_rev in REVISION_POLICY_VERSIONS) else BASE_CURRENT_REVISION
    REVISION_PRESETS.clear()
    REVISION_PRESETS.update(
        {revision: "unified_v" + revision.replace(".", "_") for revision in REVISION_POLICY_VERSIONS}
    )
    PRESET_REVISIONS.clear()
    PRESET_REVISIONS.update({preset: revision for revision, preset in REVISION_PRESETS.items()})


get_registry().add_reload_hook(_sync_versions)
_sync_versions(ensure_bootstrapped())


def preset_for_revision(revision):
    return REVISION_PRESETS[validate_revision(revision)]


def revision_for_preset(preset):
    if type(preset) is not str or preset not in PRESET_REVISIONS:
        raise ValueError("larc_policy_preset_unknown")
    return PRESET_REVISIONS[preset]


def validate_revision(revision):
    if type(revision) is not str or revision not in REVISION_POLICY_VERSIONS:
        raise ValueError("larc_policy_revision_unknown")
    return revision


def validate_policy_revision(policy):
    if not isinstance(policy, Mapping):
        raise ValueError("larc_policy_must_be_object")
    if "revision" not in policy:
        return
    validate_revision(policy["revision"])
    if policy.get("variant", "alpha") != UNIFIED_VARIANT:
        raise ValueError("larc_policy_revision_requires_baseline")


def policy_version_for_revision(revision):
    return REVISION_POLICY_VERSIONS[validate_revision(revision)]


def strategy_revision(goal):
    """Return a unified revision string, or None for legacy non-unified policies."""
    policy = getattr(goal, "larc_policy", {})
    validate_policy_revision(policy)
    if getattr(goal, "scenario", None) != "larc" or policy.get("variant") != UNIFIED_VARIANT:
        return None
    return policy.get("revision", LEGACY_REVISION)


def revision_action(previous, selected):
    """Describe explicit selection by registered revision order."""
    validate_revision(selected)
    revisions = tuple(REVISION_POLICY_VERSIONS)
    if previous in REVISION_POLICY_VERSIONS:
        change = revisions.index(selected) - revisions.index(previous)
        return "upgrade" if change > 0 else "rollback" if change < 0 else "select"
    return "upgrade" if selected != LEGACY_REVISION else "select"


def _revision_version_key(revision: str) -> tuple[int, ...]:
    """Parse numeric prefix components cleanly for suffix revisions."""
    parts: list[int] = []
    for part in str(revision).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
        if digits != part:
            break
    return tuple(parts)


def behavior_base_revision(goal):
    """Boundary metadata helper for career review exports."""
    revision = strategy_revision(goal)
    if revision is not None and _revision_version_key(revision) >= (1, 8):
        return "1.7.26.3"
    return revision


def new_goal_policy_defaults(policy):
    """Fill absent weights ONLY at new-run construction, before its snapshot."""
    result = deepcopy(policy)
    if not isinstance(result, dict):
        return result
    rev = result.get("revision")
    if rev in AUTO_ROUTED_REVISIONS:
        return result
    if type(rev) is str and rev in REVISION_POLICY_VERSIONS:
        ver_tuple = _revision_version_key(rev)
        preset = PRESET_REGISTRY.get(rev, DEFAULT_PARAMS)
        if ver_tuple >= (1, 8, 5):
            result.setdefault("after_target_credit", preset.default_after_target_credit)
        if ver_tuple >= (1, 8, 3) and rev != "1.8.4":
            defaults = preset.attribute_weights_dict()
            if isinstance(result.get("attribute_weights", {}), dict):
                result["attribute_weights"] = {**defaults, **result.get("attribute_weights", {})}
    return result


def new_strategy_enabled(goal):
    """Return True when ``goal`` enables the unified L'Arc strategy engine."""
    return strategy_revision(goal) is not None
