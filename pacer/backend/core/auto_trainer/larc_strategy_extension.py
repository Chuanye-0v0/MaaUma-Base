"""Clean Strategy Extension SPI for L'Arc public baseline and optional external packs.

When no external strategy pack is installed, the default no-op provider runs
purely on the public baseline parameters (up to revision 2.3). External packs
sitting under ``pacer/strategy_pack/`` or ``MAAUMA_STRATEGY_PACK_DIR`` register
presets, policy revisions, and advanced valuation hooks at bootstrap time.
"""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable


class StrategyExtensionProvider:
    """Default no-op strategy extension provider (public baseline only)."""

    def get_param_attr(self, params: Any, name: str) -> Any:
        raise AttributeError(name)

    def coerce_override(self, key: str, value: Any) -> tuple[bool, Any]:
        return False, None

    def get_module_symbol(self, name: str) -> Any:
        raise AttributeError(name)

    def resolve_auto_preset(self, revision: str, deck_types: Any) -> Any:
        return None

    def late_surplus_options(self, params: Any, value: dict[str, Any], ord_now: int | None) -> dict[str, Any] | None:
        return None

    def event_recovery_score(
        self,
        params: Any,
        state: Any,
        goal: Any,
        usable_points: float,
        max_points: float,
        base: float,
        conserve_energy: bool,
    ) -> float:
        return base

    def friendship_evidence_gates_enabled(self, params: Any) -> bool:
        return False

    def first_arc_reserve_decision(
        self,
        params: Any,
        state: Any,
        goal: Any,
        levels: Any,
        points: Any,
        now: int,
        balance_resources: bool,
    ) -> tuple[bool, dict[str, Any] | None, float] | None:
        return None

    def first_arc_race_ap_config(self, params: Any) -> tuple[bool, bool]:
        return False, False

    def first_arc_win_ap_expectation(
        self,
        state: Any,
        goal: Any = None,
        target_levels: Any = None,
    ) -> dict[str, Any]:
        return {}

    def rest_global_offers_enabled(self, params: Any) -> bool:
        return False

    def race_language_recheck_enabled(self, params: Any) -> bool:
        return False

    def purchase_bugfix_tie_enabled(self, params: Any) -> bool:
        return False

    def quote_wisdom_gate_prepare(self, **kwargs: Any) -> dict[str, Any] | None:
        return None

    def quote_wisdom_gate_check(self, ctx: dict[str, Any], offer: Any, income_q: dict[str, Any]) -> dict[str, Any] | None:
        return None

    def quote_wisdom_gate_finalize(self, ctx: dict[str, Any], **kwargs: Any) -> None:
        return None

    def purchase_wisdom_gate_prepare(self, **kwargs: Any) -> dict[str, Any] | None:
        return None

    def purchase_wisdom_gate_check(self, ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return {"active": False, "in_band": False, "allowed": True, "reason": "not_applicable"}

    def purchase_wisdom_gate_finalize(self, ctx: dict[str, Any], **kwargs: Any) -> None:
        return None

    def purchase_high_cash_bonus(self, **kwargs: Any) -> float:
        return 0.0

    def purchase_courage_deferred(self, **kwargs: Any) -> bool:
        return False

    def apply_recovery_policy_bonus(
        self,
        params: Any,
        row: dict[str, Any],
        rest_after: float | None,
        base_bonus: float,
    ) -> float:
        return base_bonus

    def last_overseas_timing_scale(self, params: Any) -> float:
        return 1.0

    def early_4p_ss_allowed(self, params: Any) -> bool:
        return False

    def short_window_energy_support_enabled(self, params: Any) -> bool:
        return False

    def trajectory_risk_enabled(self, params: Any) -> bool:
        return False

    def mei_comparison_enabled(self, params: Any) -> bool:
        return False

    def wisdom_reserve_scale(self, params: Any, state: Any, goal: Any, wisdom_cards: int) -> float:
        return 1.0

    def year1_scales(self, params: Any) -> tuple[float, float]:
        return 1.0, 1.0

    def year1_apply_tuning_bridge(
        self,
        params: Any,
        state: Any,
        goal: Any,
        scores: dict[str, float],
        evidence: dict[str, Any],
        combo: dict[str, Any],
        clean: list[dict[str, Any]],
        ss_preview: Any,
        select_fn: Callable[[dict[str, float]], str],
    ) -> tuple[dict[str, float], str, bool]:
        return scores, combo["selected"], False

    def tail_prepare_options(
        self,
        params: Any,
        state: Any,
        goal: Any,
        current: Any,
        terminal_options: dict[str, Any],
        zero_card_tabs: Any,
        attribute_plan_fn: Any,
    ) -> tuple[Any, dict[str, bool]]:
        return current, {"defer_courage_zero_stamina": False, "high_cash_monetization": False}

    def planner_init_extras(self, planner: Any, request: dict[str, Any]) -> bool:
        return False

    def planner_soft_ss_due(self, request: dict[str, Any]) -> bool:
        return request.get("soft_ss_due") is True

    def planner_adjust_terminal_value(
        self,
        planner: Any,
        value: float,
        node_end: Any,
        base_last: Any,
        kernel_mod: Any,
    ) -> float:
        return value

    def planner_terminal_node_bonus(
        self,
        planner: Any,
        node: Any,
        after: int,
        initial_completed: Any,
        bonus: Any,
    ) -> float:
        return 0.0

    def post_full_ss_preference(
        self,
        params: Any,
        preview: dict[str, Any],
        count: int,
        value: dict[str, Any],
    ) -> float:
        return 0.0

    def build_departure_request_extras(
        self,
        *,
        params: Any,
        request: dict[str, Any],
        state: Any,
        goal: Any,
        day: int,
        tabs: tuple[str, ...],
        period_at_day: Any,
    ) -> None:
        return None

    def departure_omit_ineligible_unbound(self, params: Any) -> bool:
        return False

    def augment_pre_expedition_spec(
        self,
        params: Any,
        spec: dict[str, Any],
        day: int,
        mei_ok: bool,
        courage_ok: bool,
    ) -> dict[str, Any]:
        return spec

    def first_arc_entry_wisdom_check(self, **kwargs: Any) -> dict[str, Any]:
        return {"active": False, "in_band": False, "allowed": True, "reason": "not_applicable"}

    def augment_year3_shortfall_payload(
        self,
        params: Any,
        payload: dict[str, Any],
        *,
        now: int | None,
        rem_second_exp: int,
        zero_tabs: list[str],
        decked_tabs: list[str],
        state: Any = None,
        goal: Any = None,
    ) -> dict[str, Any]:
        return payload

    def surplus_scarcity_factors(
        self,
        attributes: dict[str, dict[str, Any]],
        cfg: dict[str, Any],
        sta_eff_target: float,
        sta_residual: float,
    ) -> dict[str, float] | None:
        return None

    def trajectory_risk_posture(self, params: Any, state: Any, goal: Any) -> dict[str, Any] | None:
        return None

    def effective_failure_limit(self, params: Any, state: Any, goal: Any, base_limit: float) -> float:
        return base_limit

    def dynamic_risk_score_adjustment(
        self,
        params: Any,
        state: Any,
        goal: Any,
        valuation: dict[str, Any] | None,
        candidate: Any,
    ) -> float:
        return 0.0

    def stamina_lesson_quality_adjustment(
        self,
        params: Any,
        state: Any,
        candidate: Any,
        goal: Any,
        gain_result: dict[str, Any],
        components: dict[str, Any],
        *,
        energy_percent: float | None = None,
    ) -> dict[str, Any]:
        return {"score": 0.0, "mode": "inactive"}

    def stamina_deficit_scarcity(
        self,
        params: Any,
        state: Any,
        candidate: Any,
        goal: Any,
        gain_result: dict[str, Any],
    ) -> dict[str, Any]:
        return {"score": 0.0, "mode": "inactive"}

    def high_hp_wisdom_stamina_trade(
        self,
        params: Any,
        state: Any,
        candidate: Any,
        goal: Any,
        features: dict[str, Any] | None = None,
        *,
        gain_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"score": 0.0, "mode": "inactive"}

    def apply_weak_training_recovery_comparison(
        self,
        params: Any,
        state: Any,
        goal: Any,
        scored: list[dict[str, Any]],
        future_reference: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"active": False, "adjusted": [], "diagnostics": {"active": False}}

    def builtin_score_tuning(self, revision: str | None) -> dict[str, Any] | None:
        return None

    def has_builtin_score_tuning(self, revision: str | None) -> bool:
        return False


BUILTIN_WORKFLOW_MODE_IDS: tuple[str, ...] = (
    "direct_career",
    "cut_preserve",
    "cut_then_career",
    "ss_sieve_cycle",
    "ace_career_cycle",
)


class StrategyExtensionRegistry:
    """Registry managing installed strategy packs and the active extension provider."""

    def __init__(self) -> None:
        self.reset()
        self._reload_hooks: list[Callable[[StrategyExtensionRegistry], None]] = []

    def reset(self) -> None:
        self.provider: StrategyExtensionProvider = StrategyExtensionProvider()
        self.presets: dict[str, Any] = {}
        self.auto_routed_revisions: set[str] = set()
        self.policy_versions: dict[str, str] = {}
        self.head_bonuses: dict[str, dict[str, float]] = {}
        self.exports: dict[str, Any] = {}
        self.default_current_revision: str | None = None
        self.unlocked_workflow_modes: list[str] = ["direct_career"]
        self.allow_custom_workflows: bool = False
        self.weight_presets: list[dict[str, Any]] = []
        self.skill_ladder_presets: list[dict[str, Any]] = []
        self.loaded_packs: list[dict[str, Any]] = []
        self.load_errors: list[dict[str, str]] = []

    def register_workflow_modes(
        self,
        modes: Any = None,
        *,
        allow_custom: bool = False,
        unlock_all: bool = False,
    ) -> None:
        unlocked = set(self.unlocked_workflow_modes) | {"direct_career"}
        if unlock_all:
            unlocked.update(BUILTIN_WORKFLOW_MODE_IDS)
            self.allow_custom_workflows = True
        if allow_custom:
            self.allow_custom_workflows = True
        if modes is not None:
            for item in modes:
                mid = str(item).strip()
                if mid in BUILTIN_WORKFLOW_MODE_IDS:
                    unlocked.add(mid)
        self.unlocked_workflow_modes = [mid for mid in BUILTIN_WORKFLOW_MODE_IDS if mid in unlocked]

    def register_weight_preset(self, preset: dict[str, Any]) -> None:
        pid = str(preset.get("id") or "").strip()
        if not pid:
            return
        if any(existing.get("id") == pid for existing in self.weight_presets):
            return
        self.weight_presets.append(dict(preset))

    def register_skill_ladder_preset(self, preset: dict[str, Any]) -> None:
        pid = str(preset.get("id") or "").strip()
        if not pid:
            return
        if any(existing.get("id") == pid for existing in self.skill_ladder_presets):
            return
        self.skill_ladder_presets.append(deepcopy(preset))

    def get_skill_ladder_preset(self, preset_id: str) -> dict[str, Any] | None:
        pid = str(preset_id or "").strip()
        if not pid:
            return None
        for existing in self.skill_ladder_presets:
            if existing.get("id") == pid and isinstance(existing.get("policy"), dict):
                return deepcopy(existing["policy"])
        return None

    def set_provider(self, provider: StrategyExtensionProvider) -> None:
        self.provider = provider

    def register_preset(self, key: str, preset: Any) -> None:
        self.presets[str(key)] = preset

    def register_auto_routed(self, revisions: Any) -> None:
        for rev in revisions:
            self.auto_routed_revisions.add(str(rev))

    def register_policy_version(self, revision: str, policy_version: str) -> None:
        self.policy_versions[str(revision)] = str(policy_version)

    def register_head_bonuses(self, mapping: dict[str, dict[str, float]]) -> None:
        for rev, head in mapping.items():
            self.head_bonuses[str(rev)] = dict(head)

    def set_default_current_revision(self, revision: str) -> None:
        self.default_current_revision = str(revision)

    def export_symbol(self, name: str, value: Any) -> None:
        self.exports[str(name)] = value

    def add_reload_hook(self, fn: Callable[[StrategyExtensionRegistry], None]) -> None:
        if fn not in self._reload_hooks:
            self._reload_hooks.append(fn)

    def _notify_reload(self) -> None:
        for fn in list(self._reload_hooks):
            fn(self)


_REGISTRY = StrategyExtensionRegistry()
_BOOTSTRAPPED = False


def get_registry() -> StrategyExtensionRegistry:
    return _REGISTRY


def active_provider() -> StrategyExtensionProvider:
    ensure_bootstrapped()
    return _REGISTRY.provider


def discover_pack_dirs() -> list[Path]:
    """Find candidate external strategy pack directories in priority order."""
    env_raw = os.environ.get("MAAUMA_STRATEGY_PACK_DIR")
    if env_raw == "__NONE__":
        return []

    candidates: list[Path] = []
    if env_raw:
        for part in env_raw.split(os.pathsep):
            cleaned = part.strip()
            if cleaned:
                candidates.append(Path(cleaned))

    here = Path(__file__).resolve()
    backend_root = here.parents[2]
    pacer_root = backend_root.parent
    for base in (backend_root / "strategy_pack", pacer_root / "strategy_pack"):
        if base.is_dir():
            candidates.append(base)
            for child in sorted(base.iterdir()):
                if child.is_dir():
                    candidates.append(child)
    for packs_root in (backend_root / "strategy_packs", pacer_root / "strategy_packs"):
        if packs_root.is_dir():
            for child in sorted(packs_root.iterdir()):
                if child.is_dir():
                    candidates.append(child)

    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved not in seen and resolved.is_dir() and (resolved / "manifest.json").is_file():
            seen.add(resolved)
            deduped.append(resolved)
    return deduped


def _load_pack_dir(pack_dir: Path, registry: StrategyExtensionRegistry) -> None:
    manifest_path = pack_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("schema_version") != "maauma.strategy_pack.v1":
        raise ValueError(f"unsupported_strategy_pack_schema:{pack_dir}")
    pack_id = str(manifest.get("pack_id") or pack_dir.name).strip() or pack_dir.name
    if any(str(item.get("pack_id")) == pack_id for item in registry.loaded_packs):
        return
    entry_rel = str(manifest.get("entry") or "pack.py")
    entry_path = (pack_dir / entry_rel).resolve()
    if not entry_path.is_relative_to(pack_dir.resolve()) or not entry_path.is_file():
        raise ValueError(f"missing_strategy_pack_entry:{entry_path}")

    registry.register_workflow_modes(
        manifest.get("workflow_modes"),
        allow_custom=bool(manifest.get("allow_custom_workflows")),
        unlock_all=bool(manifest.get("unlock_all_workflow_modes")),
    )
    pack_root = pack_dir.resolve()
    raw_presets = manifest.get("weight_presets")
    if isinstance(raw_presets, list):
        for item in raw_presets:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or "").strip()
            pname = str(item.get("name") or "").strip()
            if not pid or not pname:
                continue
            profile_obj = None
            if isinstance(item.get("file"), str) and item["file"].strip():
                candidate = (pack_dir / item["file"].strip()).resolve()
                if candidate.is_relative_to(pack_root) and candidate.is_file():
                    loaded_profile = json.loads(candidate.read_text(encoding="utf-8-sig"))
                    if isinstance(loaded_profile, dict):
                        profile_obj = loaded_profile
            elif isinstance(item.get("profile"), dict):
                profile_obj = dict(item["profile"])
            if profile_obj is None:
                continue
            entry = {
                "id": pid,
                "name": pname,
                "summary": str(item.get("summary") or "").strip(),
                "recommended_revision": (
                    str(item["recommended_revision"]).strip()
                    if item.get("recommended_revision") is not None and str(item["recommended_revision"]).strip()
                    else None
                ),
                "profile": profile_obj,
                "pack_id": pack_id,
            }
            registry.register_weight_preset(entry)

    raw_ladders = manifest.get("skill_ladder_presets")
    if isinstance(raw_ladders, list):
        for item in raw_ladders:
            if not isinstance(item, dict):
                continue
            lid = str(item.get("id") or "").strip()
            lname = str(item.get("name") or "").strip()
            if not lid or not lname:
                continue
            policy_obj = None
            if isinstance(item.get("file"), str) and item["file"].strip():
                candidate = (pack_dir / item["file"].strip()).resolve()
                if candidate.is_relative_to(pack_root) and candidate.is_file():
                    loaded_policy = json.loads(candidate.read_text(encoding="utf-8-sig"))
                    if isinstance(loaded_policy, dict):
                        policy_obj = loaded_policy
            elif isinstance(item.get("policy"), dict):
                policy_obj = deepcopy(item["policy"])
            if policy_obj is None:
                continue
            registry.register_skill_ladder_preset(
                {
                    "id": lid,
                    "name": lname,
                    "summary": str(item.get("summary") or "").strip(),
                    "policy": policy_obj,
                    "pack_id": pack_id,
                }
            )

    pkg_name = f"_maauma_ext_pack_{pack_id}"
    spec = importlib.util.spec_from_file_location(
        pkg_name,
        entry_path,
        submodule_search_locations=[str(pack_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot_load_strategy_pack:{entry_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = module
    spec.loader.exec_module(module)
    register_fn = getattr(module, "register_strategy_pack", None)
    if callable(register_fn):
        register_fn(registry, manifest=manifest, pack_dir=pack_dir)
    registry.loaded_packs.append({"pack_id": pack_id, "dir": str(pack_dir), "version": manifest.get("version")})


def ensure_bootstrapped() -> StrategyExtensionRegistry:
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return _REGISTRY
    _BOOTSTRAPPED = True
    for pack_dir in discover_pack_dirs():
        try:
            _load_pack_dir(pack_dir, _REGISTRY)
        except Exception as exc:
            _REGISTRY.load_errors.append({"pack_dir": str(pack_dir), "error": f"{type(exc).__name__}: {exc}"})
    _REGISTRY._notify_reload()
    return _REGISTRY


def reload_extensions() -> StrategyExtensionRegistry:
    global _BOOTSTRAPPED
    _BOOTSTRAPPED = False
    _REGISTRY.reset()
    return ensure_bootstrapped()


def strategy_pack_status() -> dict[str, Any]:
    """Return a read-only summary of loaded strategy packs and registered revisions."""
    from core.auto_trainer.larc_strategy_versions import CURRENT_REVISION, REVISION_POLICY_VERSIONS

    reg = ensure_bootstrapped()
    return {
        "has_external_pack": bool(reg.loaded_packs),
        "loaded_packs": list(reg.loaded_packs),
        "load_errors": list(reg.load_errors),
        "current_revision": CURRENT_REVISION,
        "available_revisions": list(REVISION_POLICY_VERSIONS.keys()),
        "unlocked_workflow_modes": list(reg.unlocked_workflow_modes),
        "allow_custom_workflows": bool(reg.allow_custom_workflows),
        "weight_preset_count": len(reg.weight_presets),
        "weight_presets": [
            {
                key: preset[key]
                for key in ("id", "name", "summary", "recommended_revision", "pack_id")
                if preset.get(key) is not None
            }
            for preset in reg.weight_presets
        ],
        "skill_ladder_preset_count": len(reg.skill_ladder_presets),
        "skill_ladder_presets": [
            {
                key: preset[key]
                for key in ("id", "name", "summary", "pack_id")
                if preset.get(key) is not None
            }
            for preset in reg.skill_ladder_presets
        ],
    }
