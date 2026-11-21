"""Declarative parameter table for the unified L'Arc strategy engine (public baseline 2.1 ~ 2.3).

Extended strategy presets and parameter namespaces are registered dynamically via
``larc_strategy_extension`` when an external strategy pack is installed.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from core.auto_trainer.larc_strategy_extension import active_provider, ensure_bootstrapped, get_registry


@dataclass(frozen=True)
class LarcStrategyParams:
    # 1. Identity & Preset Metadata
    revision: str = "2.1"
    preset_id: str = "unified_v2_1"
    policy_version: str = "larc_unified_v2_1_preview"

    # 2. Attribute & High-Stat Valuation
    default_attribute_weights: tuple[tuple[str, float], ...] = (
        ("speed", 1.0),
        ("stamina", 1.0),
        ("power", 1.25),
        ("guts", 0.85),
        ("wisdom", 1.0),
    )
    default_after_target_credit: float = 0.5
    restore_high_stat_value: bool = True
    high_stat_scarcity: bool = False
    late_far_surplus_ordinals: tuple[int, ...] = (63, 65)
    late_far_surplus_credit: float = 0.25
    late_far_surplus_buffer: float = 100.0
    classic_speed_soft_target: int = 900

    # 3. Year-1 Unified Scoring (b197 / combo198)
    year1_coefficients: tuple[float, ...] = (
        75.52979772219078,
        9.51348319580222e-11,
        16.50673001638919,
        41.45537712816068,
        32.417387198205,
        -28.332240703073413,
        -1.1473719789045152e-16,
    )
    combo198_hp_alpha: float = 0.5
    combo198_cross80_price: float = 6.0
    year1_mei_junior_early_bonus: float = 5.0
    year1_mei_junior_late_bonus: float = 3.0

    # 4. Expedition Purchase & Cash Reserves
    arc1_defer_niel_power: bool = True
    arc1_language_deferral_enabled: bool = True
    arc1_keep_cash_route: bool = True
    arc1_optional_facility_requires_current_best: bool = True
    arc2_optional_next_facility: bool = True
    arc2_exclude_zero_card_future_reserve: bool = False
    purchase_beam_width: int = 12
    unfunded_shortfall_penalty_weight: float = 0.35
    deck_prior_weight: float = 0.5
    deck_upgrade_min_known_slots: int = 6

    # Rule 1 (2.2): First-Expedition (d36..41) conditional facility budget
    first_arc_conditional_budget_enabled: bool = False
    first_arc_second_slot_ap_threshold: float = 750.0
    first_arc_estimated_income_per_turn: float = 125.0
    first_arc_conservative_income_per_turn: float = 110.0
    first_arc_willpower_reserve: int = 400
    first_arc_max_stat_lv3_high_budget: int = 2
    first_arc_max_zero_lv3_high_budget: int = 1

    # 5. Energy, Recovery & Pre-Expedition Departure Planning
    default_failure_limit: float = 25.0
    risk_aversion_per_percent: float = 0.5
    future_discount: float = 0.95
    close_score_fraction: float = 0.05
    rest_scenarios: tuple[tuple[int, float], ...] = ((30, 0.25), (50, 0.5), (70, 0.25))
    domestic_terminal_energy_weight: float = 0.7
    overseas_terminal_energy_weight: float = 0.5
    departure_base_max_ops: int = 250_000
    departure_late_max_ops: int = 500_000
    first_entry_allow_tied_dominant_deck: bool = False

    # Rule 2 (2.2): Dynamic pre-expedition vitality bands & pre-Niel safety
    pre_expedition_dynamic_bands_enabled: bool = False
    first_exp_entry_band: tuple[float, float] = (88.0, 96.0)
    second_exp_mei_courage_band: tuple[float, float] = (80.0, 86.0)
    second_exp_fallback_band: tuple[float, float] = (88.0, 95.0)
    second_exp_courage3_funded_ap: int = 300
    pre_expedition_band_deficit_weight: float = 0.85
    pre_expedition_band_quadratic_weight: float = 0.012
    pre_expedition_band_overflow_weight: float = 0.18
    pre_niel_vitality_safety_enabled: bool = False
    pre_niel_safe_vitality: float = 42.0
    pre_niel_low_hp_penalty_per_pct: float = 1.10

    # 6. SS Choice & Matched Stock
    full_group_min_count: int = 5
    full_ss_base_preference: float = 12.0
    full_sss_base_preference: float = 16.0
    matched_stock_min_direct_new_full: int = 2
    matched_stock_nearbest_fraction: float = 0.08
    matched_stock_min_expected_new_full: float = 1.5

    # Rule 3 (2.2): Full 5-member guaranteed SS vitality conservation
    full_ss_vitality_conservation_enabled: bool = False
    full_ss_clamp_negative_continuity: bool = True
    full_ss_conserve_ratio: float = 0.16
    full_ss_conserve_low_hp_ratio: float = 0.08
    full_ss_safe_vitality: float = 52.0
    full_ss_conserve_cap: float = 14.0
    mediocre_max_single_rainbow_gain: int = 36
    mediocre_max_surplus_single_gain: int = 42
    mediocre_max_total_stat_gain: int = 58

    # 7. Rule 4 (2.2): Year-3 shortfall balance, 1180 smooth Stamina, R53 & R56
    year3_shortfall_balance_enabled: bool = False
    year3_shortfall_only: bool = True
    shortfall_floor: float = 60.0
    shortfall_full: float = 220.0
    shortfall_premium_cap: float = 0.85
    surplus_scarcity_factor: float = 0.50
    stamina_shortfall_trigger: float = 110.0
    stamina_smooth_target_default: float = 1180.0
    stamina_contest_distance_min: int = 2101
    second_exp_overseas_stat_credit_per_turn: float = 8.0
    year3_unlisted_settlement_stat_credit: float = 35.0
    r53_speed_stamina_tiebreak_enabled: bool = False
    r56_terminal_deck_effects_enabled: bool = False
    protect_early_pt50_on_fallback: bool = False

    # Opaque extension parameter pairs sorted by key so dataclass equality stays hashable/canonical
    ext: tuple[tuple[str, Any], ...] = ()

    def attribute_weights_dict(self) -> dict[str, float]:
        return dict(self.default_attribute_weights)

    def __getattr__(self, name: str) -> Any:
        return active_provider().get_param_attr(self, name)


def with_ext(params: LarcStrategyParams, **updates: Any) -> LarcStrategyParams:
    """Return a copy of ``params`` with canonicalized sorted ``ext`` parameter overrides."""
    merged = dict(params.ext)
    merged.update(updates)
    return replace(params, ext=tuple(sorted(merged.items())))


PRESET_2_1 = LarcStrategyParams()

PRESET_2_2 = LarcStrategyParams(
    revision="2.2",
    preset_id="unified_v2_2",
    policy_version="larc_unified_v2_2_preview",
    arc2_exclude_zero_card_future_reserve=True,
    deck_upgrade_min_known_slots=5,
    first_arc_conditional_budget_enabled=True,
    first_entry_allow_tied_dominant_deck=True,
    pre_expedition_dynamic_bands_enabled=True,
    pre_niel_vitality_safety_enabled=True,
    full_ss_vitality_conservation_enabled=True,
    year3_shortfall_balance_enabled=True,
    r53_speed_stamina_tiebreak_enabled=True,
    r56_terminal_deck_effects_enabled=True,
)

PRESET_2_3_PRO = replace(
    PRESET_2_2,
    revision="2.3",
    preset_id="unified_v2_3_pro",
    policy_version="larc_unified_v2_3_preview",
    default_failure_limit=28.0,
    protect_early_pt50_on_fallback=True,
)

DEFAULT_PARAMS = PRESET_2_1

BASE_PRESET_REGISTRY: dict[str, LarcStrategyParams] = {
    PRESET_2_1.revision: PRESET_2_1,
    PRESET_2_1.preset_id: PRESET_2_1,
    PRESET_2_2.revision: PRESET_2_2,
    PRESET_2_2.preset_id: PRESET_2_2,
    PRESET_2_3_PRO.revision: PRESET_2_3_PRO,
    "2.3_pro": PRESET_2_3_PRO,
    "unified_v2_3": PRESET_2_3_PRO,
    PRESET_2_3_PRO.preset_id: PRESET_2_3_PRO,
}

PRESET_REGISTRY: dict[str, LarcStrategyParams] = dict(BASE_PRESET_REGISTRY)
AUTO_ROUTED_REVISIONS: set[str] = set()

_TUPLE_PAIR_FIELDS = frozenset(
    {
        "first_exp_entry_band",
        "second_exp_mei_courage_band",
        "second_exp_fallback_band",
    }
)


def resolve_auto_preset(revision: str, deck_types: Any) -> LarcStrategyParams:
    """Dispatch an auto-routed preset revision using the active extension provider."""
    resolved = active_provider().resolve_auto_preset(revision, deck_types)
    if isinstance(resolved, LarcStrategyParams):
        return resolved
    return PRESET_REGISTRY.get(revision, DEFAULT_PARAMS)


def _extract_goal_deck_types(goal: Any) -> tuple[str, ...]:
    raw = getattr(goal, "_support_deck_types", None)
    if not raw:
        return ()
    return tuple(str(t).strip().lower() for t in raw if t)


def larc_enabled(goal: Any) -> bool:
    """Return True when ``goal`` targets the L'Arc scenario."""
    return getattr(goal, "scenario", None) == "larc"


def unified_larc_enabled(goal: Any) -> bool:
    """Return True when ``goal`` uses the unified L'Arc engine (variant == 'baseline')."""
    if not larc_enabled(goal):
        return False
    policy = getattr(goal, "larc_policy", None)
    return isinstance(policy, Mapping) and policy.get("variant") == "baseline"


def params_for_goal(goal: Any) -> LarcStrategyParams:
    """Resolve the active ``LarcStrategyParams`` preset for ``goal``."""
    ensure_bootstrapped()
    base = DEFAULT_PARAMS
    policy = getattr(goal, "larc_policy", None)
    if isinstance(policy, dict):
        revision = policy.get("revision")
        if isinstance(revision, str) and revision in PRESET_REGISTRY:
            base = PRESET_REGISTRY[revision]
            if revision in AUTO_ROUTED_REVISIONS:
                auto_base = resolve_auto_preset(revision, _extract_goal_deck_types(goal))
                base = replace(
                    auto_base,
                    revision=base.revision,
                    preset_id=base.preset_id,
                    policy_version=base.policy_version,
                )
        overrides = policy.get("strategy22_params")
        if isinstance(overrides, dict) and overrides:
            provider = active_provider()
            public_valid: dict[str, Any] = {}
            ext_valid: dict[str, Any] = {}
            for key, val in overrides.items():
                if key != "ext" and key in base.__dataclass_fields__:
                    if key in _TUPLE_PAIR_FIELDS and isinstance(val, (list, tuple)) and len(val) == 2:
                        public_valid[key] = (float(val[0]), float(val[1]))
                    else:
                        public_valid[key] = val
                else:
                    accepted, coerced = provider.coerce_override(key, val)
                    if accepted:
                        ext_valid[key] = coerced
            if public_valid:
                base = replace(base, **public_valid)
            if ext_valid:
                base = with_ext(base, **ext_valid)
    return base


def _sync_extensions(registry: Any) -> None:
    PRESET_REGISTRY.clear()
    PRESET_REGISTRY.update(BASE_PRESET_REGISTRY)
    PRESET_REGISTRY.update(registry.presets)
    AUTO_ROUTED_REVISIONS.clear()
    AUTO_ROUTED_REVISIONS.update(registry.auto_routed_revisions)


get_registry().add_reload_hook(_sync_extensions)
_sync_extensions(ensure_bootstrapped())


def __getattr__(name: str) -> Any:
    return active_provider().get_module_symbol(name)
