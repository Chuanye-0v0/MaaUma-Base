"""Pure parameterized decision rules for the unified L'Arc engine.

All functions read thresholds and activation flags exclusively from
``LarcStrategyParams`` (``params_for_goal(goal)``), with zero revision-string
branching or seed/hindsight special cases.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import math
from typing import Any

from core.auto_trainer.larc_strategy_extension import active_provider
from core.auto_trainer.larc_strategy_params import (
    LarcStrategyParams,
    PRESET_2_2,
    params_for_goal,
    unified_larc_enabled,
)
from core.auto_trainer.state import TRAINING_TYPES

TAB_TO_SLOT: dict[str, int] = {
    "guts": 0,
    "stamina": 1,
    "power": 2,
    "speed": 3,
    "wisdom": 4,
}
SLOT_TO_TAB: dict[int, str] = {slot: tab for tab, slot in TAB_TO_SLOT.items()}
STAT_SLOTS: tuple[int, ...] = (0, 1, 2, 3, 4)
FIRST_ARC_FREE_DAYS: tuple[int, ...] = (36, 37, 38, 39, 41)
SECOND_ARC_FREE_DAYS: tuple[int, ...] = (60, 61, 62, 63, 65)
PRE_FIRST_EXP_DAYS: tuple[int, ...] = (32, 34, 35)
PRE_SECOND_EXP_DAYS: tuple[int, ...] = (56, 57, 58)

_FIRST_ARC_SCOPE: ContextVar[dict[str, Any] | None] = ContextVar(
    "larc_first_arc_purchase_scope", default=None
)


def _num(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


# ---------------------------------------------------------------------------
# Deck & support-card inspection helpers
# ---------------------------------------------------------------------------

def zero_card_tabs(state: Any, support_profile: dict | None = None) -> frozenset[str]:
    """Return 0-card training tabs ONLY when all 6 deck slots are confirmed."""
    if state is None:
        return frozenset()
    from core.auto_trainer.larc_adaptation_strategy import deck_training_coverage

    coverage = deck_training_coverage(state, support_profile)
    if coverage.get("complete") is not True:
        return frozenset()
    counts = coverage.get("counts") or {}
    if sum(counts.values()) <= 0:
        return frozenset()
    return frozenset(tab for tab in TRAINING_TYPES if counts.get(tab, 0) == 0)


def decked_tabs(state: Any, support_profile: dict | None = None) -> frozenset[str]:
    """Return training tabs with >= 1 confirmed card in the deck."""
    if state is None:
        return frozenset()
    from core.auto_trainer.larc_adaptation_strategy import deck_training_coverage

    coverage = deck_training_coverage(state, support_profile)
    if not (coverage.get("complete") or coverage.get("known_slots", 0) >= 5):
        return frozenset()
    counts = coverage.get("counts") or {}
    return frozenset(tab for tab in TRAINING_TYPES if counts.get(tab, 0) > 0)


def has_mei_cost_reduction(state: Any, support_profile: dict | None = None) -> bool:
    """Whether Satake Mei / friend-group cost reduction is active in this run."""
    if state is None:
        return False
    sc = getattr(state, "scenario_state", None) or {}
    outing = sc.get("larc_friend_outing") or {}
    if outing.get("status") == "accepted" and outing.get("unlocked") is True:
        return True
    try:
        from core.auto_trainer.larc_friend_outing import bound_outing_progress

        prog = bound_outing_progress(state)
        if isinstance(prog, dict) and prog.get("unlocked") is True:
            return True
    except Exception:
        pass
    profile = (
        support_profile
        or sc.get("larc_support_card_profile")
        or sc.get("support_card_profile")
        or outing.get("support_card_profile")
    )
    if isinstance(profile, dict):
        cards = profile.get("cards") or ()
        known_non_friend = 0
        for card in cards:
            if not isinstance(card, dict):
                continue
            kind = card.get("type") or card.get("card_type")
            if kind in {"friend", "友人", "group", "团队"}:
                return True
            if isinstance(kind, str) and kind:
                known_non_friend += 1
        if profile.get("complete") is True or known_non_friend >= 6:
            return False
    try:
        from core.auto_trainer.larc_mei_contact import mei_present

        if any(mei_present(c) for c in (getattr(state, "training_options", None) or ())):
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Rule 1: First-Expedition (ordinals 36..41) conditional facility budget
# ---------------------------------------------------------------------------

def first_arc_entry_ap_estimate(
    levels: list[int] | tuple[int, ...] | None,
    points: int | float | None,
    now: int | None,
    *,
    state: Any = None,
    params: LarcStrategyParams = PRESET_2_2,
) -> float:
    """Estimate First-Expedition d36 entry-equivalent AP budget."""
    sc = (getattr(state, "scenario_state", None) or {}) if state is not None else {}
    recorded = sc.get("larc_first_arc_entry_ap")
    if _num(recorded) and recorded >= 0:
        return float(recorded)
    if not _num(points) or points < 0:
        return 0.0
    if not isinstance(levels, (list, tuple)) or len(levels) < 8:
        return float(points)
    elapsed = sum(1 for d in FIRST_ARC_FREE_DAYS if now is not None and d < now)
    spent = 0.0
    for slot in STAT_SLOTS:
        if levels[slot] == 3:
            spent += 200.0
    if levels[5] == 3:
        spent += 300.0
    elif levels[5] == 2:
        spent += 100.0
    if levels[6] == 3:
        spent += 300.0
    if levels[7] == 3:
        spent += 500.0
    elif levels[7] == 2:
        spent += 200.0
    return float(points) + spent - elapsed * params.first_arc_estimated_income_per_turn


def _resolve_levels_and_points(state: Any, observation: dict | None = None) -> tuple[list[int] | None, int | None]:
    if isinstance(observation, dict):
        lv = observation.get("levels")
        pts = observation.get("points")
        if isinstance(lv, (list, tuple)) and len(lv) == 10 and _num(pts):
            return list(lv), int(pts)
    sc = (getattr(state, "scenario_state", None) or {}) if state is not None else {}
    ctx = sc.get("larc_expedition_context") or {}
    if ctx.get("status") == "accepted" and isinstance(ctx.get("levels"), (list, tuple)) and len(ctx["levels"]) == 10:
        pts = ctx.get("points")
        if not _num(pts):
            pts = (sc.get("larc_growth_observation") or {}).get("adaptation_points")
        return list(ctx["levels"]), (int(pts) if _num(pts) else None)
    growth = sc.get("larc_growth_observation") or {}
    pts = growth.get("adaptation_points")
    return None, (int(pts) if _num(pts) else None)


def active_first_arc_scope() -> dict[str, Any] | None:
    scope = _FIRST_ARC_SCOPE.get()
    return scope if isinstance(scope, dict) and scope.get("active") else None


def current_first_arc_scope_key() -> tuple | None:
    scope = active_first_arc_scope()
    if scope is None:
        return None
    return (
        bool(scope.get("high_budget")),
        tuple(sorted(scope.get("zero_slots") or ())),
        tuple(sorted(scope.get("decked_slots") or ())),
    )


@contextmanager
def first_arc_purchase_scope(
    state: Any,
    goal: Any,
    period: dict | None = None,
    support_profile: dict | None = None,
    *,
    observation: dict | None = None,
):
    existing = active_first_arc_scope()
    if existing is not None:
        yield existing
        return
    params = params_for_goal(goal)
    if not params.first_arc_conditional_budget_enabled or state is None:
        yield None
        return
    from core.auto_trainer.larc_calendar import period_ordinal

    per_val = (
        (period.get("value") if isinstance(period, dict) and "value" in period else period)
        or ((getattr(state, "scenario_state", None) or {}).get("larc_period") or {}).get("value")
        or {}
    )
    now = period_ordinal(per_val)
    if now is None or not 36 <= now <= 41:
        yield None
        return
    z_tabs = zero_card_tabs(state, support_profile)
    d_tabs = decked_tabs(state, support_profile)
    levels, points = _resolve_levels_and_points(state, observation)
    entry_ap = first_arc_entry_ap_estimate(levels, points, now, state=state, params=params)
    high_budget = entry_ap >= params.first_arc_second_slot_ap_threshold
    scope = {
        "active": True,
        "now": now,
        "zero_tabs": z_tabs,
        "decked_tabs": d_tabs,
        "zero_slots": frozenset(TAB_TO_SLOT[t] for t in z_tabs if t in TAB_TO_SLOT),
        "decked_slots": frozenset(TAB_TO_SLOT[t] for t in d_tabs if t in TAB_TO_SLOT),
        "entry_ap_estimate": round(entry_ap, 2),
        "high_budget": high_budget,
        "params": params,
    }
    token = _FIRST_ARC_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _FIRST_ARC_SCOPE.reset(token)


def banned_on_day(slot: int, day: int | None) -> bool:
    """Hard single-slot filter on First Expedition (ordinals 36..41)."""
    scope = active_first_arc_scope()
    if scope is None or day is None or not 36 <= day <= 41:
        return False
    if not scope["high_budget"]:
        return slot in scope["zero_slots"]
    return False


def first_arc_basket_allowed(
    targets: list[int] | tuple[int, ...],
    owned: list[int] | tuple[int, ...],
    cash: int | float,
    cost: int | float,
    now: int | None,
) -> bool:
    """Validate a candidate upgrade basket on First Expedition (ordinals 36..41)."""
    scope = active_first_arc_scope()
    if scope is None or now is None or not 36 <= now <= 41:
        return True
    zero_slots: frozenset[int] = scope["zero_slots"]
    decked_slots: frozenset[int] = scope["decked_slots"]
    params: LarcStrategyParams = scope["params"]

    new_stat_lv3 = [s for s in STAT_SLOTS if targets[s] == 3 and owned[s] < 3]
    if not new_stat_lv3:
        return True
    new_zero_lv3 = [s for s in new_stat_lv3 if s in zero_slots]

    if not scope["high_budget"]:
        return len(new_zero_lv3) == 0

    total_stat_lv3 = sum(1 for s in STAT_SLOTS if targets[s] == 3)
    if total_stat_lv3 > params.first_arc_max_stat_lv3_high_budget:
        return False
    zero_stat_lv3 = sum(1 for s in zero_slots if targets[s] == 3)
    if zero_stat_lv3 > params.first_arc_max_zero_lv3_high_budget:
        return False
    if not new_zero_lv3:
        return True

    decked_stat_lv3 = sum(1 for s in decked_slots if targets[s] == 3)
    need_decked = 0
    if decked_stat_lv3 == 0 and decked_slots:
        need_decked = min((200 if targets[s] == 2 else 300) for s in decked_slots)
    need_pt = 0 if targets[5] == 3 else (200 if targets[5] == 2 else 300)
    need_lv2 = sum((200 if s == 6 else 100) for s in (0, 1, 2, 3, 6) if targets[s] == 1)
    need_will = 0 if targets[7] == 3 else (300 if targets[7] == 2 else params.first_arc_willpower_reserve)

    cash_after = float(cash) - float(cost)
    if cash_after < need_decked + need_lv2:
        return False
    rem_overseas = sum(1 for d in FIRST_ARC_FREE_DAYS if d >= now)
    projected_pool = cash_after + rem_overseas * params.first_arc_conservative_income_per_turn
    return projected_pool >= (need_decked + need_pt + need_lv2 + need_will)


def first_arc_entry_wisdom_check(
    state: Any,
    goal: Any,
    levels: list[int] | tuple[int, ...],
    targets: list[int] | tuple[int, ...],
    points: int | float,
    cost: int | float,
    now: int | None,
    *,
    item: Any = None,
    projection: dict[str, Any] | None = None,
    failure: int | float | None = None,
    after_funding: dict[str, Any] | None = None,
    pt_only_funding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Delegate optional first-expedition entry Wisdom purchase gating to the active strategy extension."""
    return active_provider().first_arc_entry_wisdom_check(
        params=params_for_goal(goal),
        state=state,
        goal=goal,
        levels=levels,
        targets=targets,
        points=points,
        cost=cost,
        now=now,
        item=item,
        projection=projection,
        failure=failure,
        after_funding=after_funding,
        pt_only_funding=pt_only_funding,
    )


# ---------------------------------------------------------------------------
# Rule 2: Dynamic pre-expedition vitality bands & pre-Niel safety
# ---------------------------------------------------------------------------

def courage_three_ready_for_second_arc(
    state: Any,
    *,
    owned_levels: list[int] | tuple[int, ...] | None = None,
    ap: int | float | None = None,
    params: LarcStrategyParams = PRESET_2_2,
) -> tuple[bool, str]:
    levels, points = _resolve_levels_and_points(state)
    if owned_levels is not None:
        levels = list(owned_levels)
    if _num(ap):
        points = int(ap)
    if isinstance(levels, (list, tuple)) and len(levels) >= 7:
        if levels[6] == 3:
            return True, "courage_three_owned_lv3"
        if levels[6] == 2 and _num(points) and points >= params.second_exp_courage3_funded_ap:
            return True, "courage_three_lv2_funded_for_entry"
        if levels[6] == 1 and _num(points) and points >= 500:
            return True, "courage_three_lv1_funded_for_entry"
    return False, "courage_three_unowned_or_unfunded"


def pre_expedition_energy_spec(
    state: Any,
    goal: Any,
    *,
    support_profile: dict | None = None,
    owned_levels: list[int] | tuple[int, ...] | None = None,
    ap: int | float | None = None,
) -> dict[str, Any] | None:
    """Return dynamic entry-vitality target band on ordinals 32,34,35 and 56,57,58."""
    params = params_for_goal(goal)
    if not params.pre_expedition_dynamic_bands_enabled or state is None:
        return None
    from core.auto_trainer.larc_calendar import period_ordinal

    per = ((getattr(state, "scenario_state", None) or {}).get("larc_period") or {}).get("value") or {}
    day = period_ordinal(per)
    if day in PRE_FIRST_EXP_DAYS:
        band = params.first_exp_entry_band
        return {
            "active": True,
            "current_ordinal": day,
            "entry_calendar": 36,
            "band": band,
            "band_low": band[0],
            "band_high": band[1],
            "deficit_weight": params.pre_expedition_band_deficit_weight,
            "quadratic_weight": params.pre_expedition_band_quadratic_weight,
            "overflow_weight": params.pre_expedition_band_overflow_weight,
            "mei_active": has_mei_cost_reduction(state, support_profile),
            "courage_ready": False,
            "basis": "first_expedition_near_full_entry_band_no_speculative_overseas_wisdom",
        }
    if day in PRE_SECOND_EXP_DAYS:
        mei_ok = has_mei_cost_reduction(state, support_profile)
        courage_ok, courage_reason = courage_three_ready_for_second_arc(
            state, owned_levels=owned_levels, ap=ap, params=params
        )
        band = params.second_exp_mei_courage_band if (mei_ok and courage_ok) else params.second_exp_fallback_band
        basis = (
            f"second_expedition_mei_and_{courage_reason}_band"
            if (mei_ok and courage_ok)
            else f"second_expedition_fallback_high_band(mei={mei_ok},{courage_reason})"
        )
        spec = {
            "active": True,
            "current_ordinal": day,
            "entry_calendar": 60,
            "band": band,
            "band_low": band[0],
            "band_high": band[1],
            "deficit_weight": params.pre_expedition_band_deficit_weight,
            "quadratic_weight": params.pre_expedition_band_quadratic_weight,
            "overflow_weight": params.pre_expedition_band_overflow_weight,
            "mei_active": mei_ok,
            "courage_ready": courage_ok,
            "basis": basis,
        }
        return active_provider().augment_pre_expedition_spec(params, spec, day, mei_ok, courage_ok)
    return None


def entry_band_adjustment_from_contract(entry_bar_pct: float, contract: dict[str, Any] | None) -> float:
    """Score adjustment at expedition entry boundary (d36 or d60) from HP% bar."""
    if not isinstance(contract, dict) or not contract.get("active"):
        return 0.0
    low = float(contract["band_low"])
    high = float(contract["band_high"])
    adj = 0.0
    if entry_bar_pct < low:
        deficit = low - entry_bar_pct
        w_lin = float(contract.get("deficit_weight", 0.85))
        w_quad = float(contract.get("quadratic_weight", 0.012))
        adj -= (w_lin * deficit + w_quad * deficit * deficit)
    elif entry_bar_pct > high:
        overflow = entry_bar_pct - high
        w_over = float(contract.get("overflow_weight", 0.18))
        adj -= (w_over * overflow)
    two_floor = contract.get("two_lesson_floor")
    if isinstance(two_floor, (int, float)):
        step_bonus = float(contract.get("two_lesson_step_bonus", 4.5))
        step_slope = float(contract.get("two_lesson_step_slope", 0.35))
        hw = float(contract.get("smooth_half_width", 0.0) or 0.0)
        floor = float(two_floor)
        if hw > 0.0:
            if entry_bar_pct >= floor + hw:
                adj += step_bonus
            elif entry_bar_pct >= floor - hw:
                adj += step_bonus * ((entry_bar_pct - floor) / hw)
            else:
                adj -= (step_bonus + step_slope * ((floor - hw) - entry_bar_pct))
        else:
            if entry_bar_pct >= floor:
                adj += step_bonus
            else:
                adj -= (step_bonus + step_slope * (floor - entry_bar_pct))
    return adj


def pre_niel_vitality_adjustment(state: Any, goal: Any, valuation: dict[str, Any], candidate: Any) -> float:
    """Prevent ordinal-39 low-HP crash into forced d41 overseas rest."""
    params = params_for_goal(goal)
    if not params.pre_niel_vitality_safety_enabled or state is None:
        return 0.0
    from core.auto_trainer.larc_calendar import period_ordinal

    per = ((getattr(state, "scenario_state", None) or {}).get("larc_period") or {}).get("value") or {}
    if period_ordinal(per) != 39:
        return 0.0
    plan = valuation.get("planning") or {}
    if getattr(candidate, "training_type", None) == "wisdom":
        if not _num(getattr(state, "vitality", None)):
            return 0.0
        delta = float(plan.get("training_vitality_percent")) if _num(plan.get("training_vitality_percent")) else 0.0
        after = max(0.0, min(100.0, float(state.vitality) + delta))
        raw_failure = valuation.get("failure_rate")
        failure = float(raw_failure) if _num(raw_failure) else float(getattr(candidate, "failure_rate", 0.0) or 0.0)
        if after >= params.pre_niel_safe_vitality:
            return 0.0
        if failure > 0.0 or after < params.pre_niel_safe_vitality - 8.0:
            return -round((params.pre_niel_safe_vitality - after) * params.pre_niel_low_hp_penalty_per_pct, 4)
        return 0.0
    delta = plan.get("training_vitality_percent")
    if not _num(delta) or delta >= 0 or not _num(getattr(state, "vitality", None)):
        return 0.0
    after = max(0.0, min(100.0, float(state.vitality) + float(delta)))
    if after >= params.pre_niel_safe_vitality:
        return 0.0
    return -round((params.pre_niel_safe_vitality - after) * params.pre_niel_low_hp_penalty_per_pct, 4)


# ---------------------------------------------------------------------------
# Rule 3: Full 5-member SS vitality conservation (n == 5 ONLY)
# ---------------------------------------------------------------------------

def mediocre_training(
    row: dict[str, Any] | None,
    candidate: Any,
    state: Any,
    *,
    goal: Any = None,
) -> bool:
    """Return True ONLY for 0-rainbow or weak/surplus single-rainbow spending trainings."""
    if not isinstance(row, dict) or candidate is None:
        return False
    tab = row.get("training_type") or getattr(candidate, "training_type", None)
    if tab not in TRAINING_TYPES or tab == "wisdom":
        return False
    plan = row.get("planning") or {}
    delta = plan.get("training_vitality_percent")
    if not _num(delta):
        delta = getattr(candidate, "vitality_delta", None)
    if not _num(delta) or delta >= 0:
        return False

    params = params_for_goal(goal)
    gains = getattr(candidate, "stats_gain", None) or {}
    primary_gain = max(0, int(gains.get(tab, 0) or 0))
    total_stat_gain = sum(max(0, int(gains.get(k, 0) or 0)) for k in TRAINING_TYPES)

    effects = ((getattr(candidate, "scenario_mechanics", None) or {}).get("larc") or {}).get("training_effects") or {}
    friendship = effects.get("friendship_training")
    if friendship is None:
        friendship = ((row.get("features") or {}).get("friendship_training") or {}).get("value")
    if friendship is None:
        from core.auto_trainer.larc_friendship_evidence import friendship_for_gate
        friendship = friendship_for_gate(candidate, goal)

    participants_info = ((getattr(candidate, "scenario_mechanics", None) or {}).get("larc") or {}).get(
        "training_participants"
    ) or {}
    portraits = participants_info.get("visible_portrait_count")
    if type(portraits) is not int:
        portraits = len((getattr(candidate, "raw_data", None) or {}).get("support_cards", {}).get("portraits") or ())
    support_count = row.get("support_count")
    if type(support_count) is not int:
        support_count = getattr(candidate, "support_count", None)

    if friendship is False:
        return True
    if total_stat_gain > params.mediocre_max_total_stat_gain:
        return False
    if type(portraits) is int and portraits >= 3 and primary_gain >= 32:
        return False
    if type(support_count) is int and support_count >= 3 and primary_gain >= 32:
        return False
    if primary_gain <= params.mediocre_max_single_rainbow_gain:
        return True
    if primary_gain <= params.mediocre_max_surplus_single_gain and (type(portraits) is not int or portraits <= 2):
        attr_info = ((row.get("attribute_plan") or {}).get("attributes") or {}).get(tab) or {}
        target = attr_info.get("target")
        est = (attr_info.get("projected_final") or {}).get("estimate")
        if _num(target) and _num(est) and est >= target:
            return True
    return False


def conserve_full_ss(
    training_row: dict[str, Any] | None,
    candidate: Any,
    state: Any,
    *,
    continuity_score: float,
    recovery_percent: float,
    ss_numeric_score: float,
    goal: Any = None,
) -> tuple[float, float, dict[str, Any] | None]:
    """Compute (continuity_correction, conserve_bonus, detail) for guaranteed n==5 SS."""
    params = params_for_goal(goal)
    if not params.full_ss_vitality_conservation_enabled or state is None:
        return 0.0, 0.0, None
    if not mediocre_training(training_row, candidate, state, goal=goal):
        return 0.0, 0.0, None

    plan = (training_row or {}).get("planning") or {}
    delta = plan.get("training_vitality_percent")
    if not _num(delta):
        delta = getattr(candidate, "vitality_delta", -18.0)
    spend = abs(min(0.0, float(delta)))

    continuity_fix = 0.0
    if params.full_ss_clamp_negative_continuity and recovery_percent >= 0.0 and continuity_score < 0.0:
        continuity_fix = round(-float(continuity_score), 4)

    from core.auto_trainer.larc_calendar import period_ordinal

    per = ((getattr(state, "scenario_state", None) or {}).get("larc_period") or {}).get("value") or {}
    day = period_ordinal(per)
    near_exp = day in (*PRE_FIRST_EXP_DAYS, *PRE_SECOND_EXP_DAYS)
    low_hp = _num(getattr(state, "vitality", None)) and float(state.vitality) <= params.full_ss_safe_vitality

    ratio = params.full_ss_conserve_ratio + (params.full_ss_conserve_low_hp_ratio if (near_exp or low_hp) else 0.0)
    scale = max(float(ss_numeric_score), 18.0) / 20.0
    conserve_bonus = round(min(params.full_ss_conserve_cap, spend * ratio * scale), 4)

    detail = {
        "active": True,
        "mediocre_training_action_id": (training_row or {}).get("action_id"),
        "training_vitality_spend_percent": round(spend, 3),
        "continuity_correction": continuity_fix,
        "conservation_bonus": conserve_bonus,
        "near_expedition": near_exp,
        "low_vitality": low_hp,
    }
    return continuity_fix, conserve_bonus, detail


# ---------------------------------------------------------------------------
# Rule 4: Year-3 shortfall balance, 0-card exclusion & 1180 smooth Stamina
# ---------------------------------------------------------------------------

def is_stamina_contest(goal: Any) -> bool:
    """Whether the Goal explicitly requests 2101m+ high-Stamina contest scaling."""
    if goal is None:
        return False
    params = params_for_goal(goal)
    targets = getattr(goal, "target_stats", None) or {}
    sta_target = targets.get("stamina", 1200)
    if _num(sta_target) and float(sta_target) > 1200.0:
        return True
    policy = getattr(goal, "larc_policy", None) or {}
    if isinstance(policy, dict):
        if policy.get("stamina_contest") is True:
            return True
        dist = policy.get("distance_m") or policy.get("race_distance_m")
        if _num(dist) and int(dist) >= params.stamina_contest_distance_min:
            return True
    dist_attr = getattr(goal, "distance_m", None)
    if _num(dist_attr) and int(dist_attr) >= params.stamina_contest_distance_min:
        return True
    return False


def effective_stamina_target(goal: Any) -> float:
    """Return 1180 smooth threshold for standard <=1200 Stamina goals, else Goal target."""
    targets = (getattr(goal, "target_stats", None) or {}) if goal is not None else {}
    raw_target = float(targets.get("stamina", 1200.0))
    params = params_for_goal(goal)
    if not params.year3_shortfall_balance_enabled or is_stamina_contest(goal):
        return raw_target
    return min(raw_target, params.stamina_smooth_target_default)


def year3_shortfall_payload(state: Any, goal: Any) -> dict[str, Any] | None:
    """Build JSON-serializable Year-3-only shortfall config for ``attribute_plan``."""
    params = params_for_goal(goal)
    if not params.year3_shortfall_balance_enabled or state is None:
        return None
    from core.auto_trainer.larc_calendar import period_ordinal
    from core.auto_trainer.larc_recovery import period_value

    per = period_value(state)
    year = per.get("year_index")
    if params.year3_shortfall_only and year != 3:
        return None
    if year not in {2, 3}:
        return None

    now = period_ordinal(per)
    rem_second_exp = sum(1 for d in SECOND_ARC_FREE_DAYS if now is not None and d >= now)
    settlement_credit = params.year3_unlisted_settlement_stat_credit if (now is None or now <= 65) else 0.0
    z_tabs = sorted(zero_card_tabs(state))
    d_tabs = sorted(decked_tabs(state))

    credits: dict[str, float] = {}
    for key in TRAINING_TYPES:
        overseas_credit = 0.0
        if rem_second_exp > 0 and key not in z_tabs:
            factor = 1.0 if (not d_tabs or key in d_tabs) else 0.5
            overseas_credit = rem_second_exp * params.second_exp_overseas_stat_credit_per_turn * factor
        credits[key] = round(settlement_credit + overseas_credit, 3)

    payload = {
        "active": True,
        "year_index": year,
        "zero_tabs": z_tabs,
        "effective_stamina_target": effective_stamina_target(goal),
        "shortfall_credits": credits,
        "shortfall_floor": params.shortfall_floor,
        "shortfall_full": params.shortfall_full,
        "shortfall_premium_cap": params.shortfall_premium_cap,
        "surplus_scarcity_factor": params.surplus_scarcity_factor,
        "stamina_shortfall_trigger": params.stamina_shortfall_trigger,
    }
    return active_provider().augment_year3_shortfall_payload(
        params,
        payload,
        now=now,
        rem_second_exp=rem_second_exp,
        zero_tabs=z_tabs,
        decked_tabs=d_tabs,
    )


def shortfall_premium(
    premium: float,
    residual: float,
    stage_weight: float,
    *,
    key: str,
    cfg: dict[str, Any],
) -> float:
    """Apply Year-3 shortfall premium while strictly excluding 0-card dump stats."""
    if key in (cfg.get("zero_tabs") or ()):
        return premium
    floor = float(cfg.get("shortfall_floor", 60.0))
    full = float(cfg.get("shortfall_full", 220.0))
    cap = float(cfg.get("shortfall_premium_cap", 0.85))
    if residual <= floor:
        return premium
    span = max(1.0, full - floor)
    pressure = min(1.0, (residual - floor) / span)
    extra = 0.42 * float(stage_weight) * pressure
    return min(cap, premium + extra)


def surplus_scarcity_factors(attributes: dict[str, dict[str, Any]], cfg: dict[str, Any]) -> dict[str, float]:
    """Dampen >1200 scarcity on satisfied/on-track stats when Stamina still trails in Year 3."""
    sta = attributes.get("stamina") or {}
    sta_eff_target = float(cfg.get("effective_stamina_target", sta.get("target", 1200.0)))
    sta_credit = float((cfg.get("shortfall_credits") or {}).get("stamina", 0.0))
    sta_est = float((sta.get("projected_final") or {}).get("estimate", sta.get("current", 0.0))) + sta_credit
    sta_residual = max(0.0, sta_eff_target - sta_est)
    ext = active_provider().surplus_scarcity_factors(attributes, cfg, sta_eff_target, sta_residual)
    if ext is not None:
        return ext
    trigger = float(cfg.get("stamina_shortfall_trigger", 110.0))
    if sta_residual < trigger:
        return {}
    factor = float(cfg.get("surplus_scarcity_factor", 0.50))
    out: dict[str, float] = {}
    for key, row in attributes.items():
        if key == "stamina":
            continue
        target = float(row.get("target", 0.0))
        est = float((row.get("projected_final") or {}).get("estimate", 0.0))
        cur = float(row.get("current", 0.0))
        if target > 0 and est >= target and cur >= min(1200.0, target * 0.85):
            out[key] = factor
    return out


# ---------------------------------------------------------------------------
# Clock Quota & Live Stat Helpers
# ---------------------------------------------------------------------------


def resolve_clocks_remaining(state: Any) -> tuple[int, bool]:
    """Return confirmed in-career retry clocks remaining without conflating account inventory."""
    if state is None:
        return 0, False
    direct = getattr(state, "clocks_remaining", None)
    if type(direct) is int and direct >= 0:
        return direct, True
    sc = getattr(state, "scenario_state", None)
    if not isinstance(sc, dict):
        return 0, False
    explicit = sc.get("clocks_remaining")
    if type(explicit) is int and explicit >= 0:
        return explicit, True
    scr = sc.get("simulated_clock_retry")
    if isinstance(scr, dict) and scr.get("enabled") is True:
        cap = scr.get("max_retries_per_career")
        used = scr.get("used_total")
        if type(cap) is int and type(used) is int and cap >= 0 and used >= 0:
            return max(0, cap - used), True
    return 0, False


def _read_stat_value(stats: Any, key: str) -> float | None:
    if stats is None:
        return None
    val = stats.get(key) if isinstance(stats, dict) else getattr(stats, key, None)
    return float(val) if _num(val) and float(val) >= 0.0 else None


def has_live_arc1_race_stats(state: Any) -> bool:
    """Return True when real speed/stamina/power stats are present on state (no default fallback)."""
    if state is None:
        return False
    stats = getattr(state, "current_stats", None)
    return all(_read_stat_value(stats, k) is not None for k in ("speed", "stamina", "power"))


def trajectory_risk_posture(state: Any, goal: Any) -> dict[str, Any]:
    """Delegate optional trajectory posture classification to the active strategy extension."""
    params = params_for_goal(goal)
    neutral = {
        "active": False,
        "posture": "neutral",
        "limit_delta_pct": 0.0,
        "aversion_scale": 1.0,
        "mediocre_ahead_aversion_scale": 1.0,
        "pace_delta": 0.0,
        "speed_delta": 0.0,
        "clocks_remaining": 0,
        "clocks_remaining_known": False,
    }
    ext = active_provider().trajectory_risk_posture(params, state, goal)
    return ext if ext is not None else neutral


def effective_failure_limit(
    goal: Any,
    state: Any = None,
    *,
    strict_legacy_domain: bool = False,
) -> float | None:
    """Single source of truth for allowed training failure percent across all evaluators."""
    params = params_for_goal(goal)
    configured = getattr(goal, "training_failure_rate_limit", None)
    if configured is None:
        base = float(params.default_failure_limit)
    else:
        if type(configured) not in (int, float) or not math.isfinite(configured) or not 0 <= configured <= 100:
            return None if strict_legacy_domain else float(params.default_failure_limit)
        if strict_legacy_domain and float(configured) > 25.0 and not unified_larc_enabled(goal):
            return None
        base = float(configured)

    return active_provider().effective_failure_limit(params, state, goal, base)


def dynamic_risk_score_adjustment(
    state: Any,
    goal: Any,
    valuation: dict[str, Any] | None,
    candidate: Any,
) -> float:
    """Delegate optional trajectory risk score adjustment to the active strategy extension."""
    return active_provider().dynamic_risk_score_adjustment(
        params_for_goal(goal), state, goal, valuation, candidate
    )


# ---------------------------------------------------------------------------
# Strategy Extension Rule Dispatchers
# ---------------------------------------------------------------------------

def first_arc_win_ap_expectation(
    state: Any,
    goal: Any = None,
    target_levels: list[int] | tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """Delegate optional 1st L'Arc post-race AP expectation to the active strategy extension."""
    return active_provider().first_arc_win_ap_expectation(state, goal, target_levels)


def _is_rainbow_candidate(candidate: Any) -> bool:
    explicit = getattr(candidate, "friendship", None)
    if explicit is True:
        return True
    if explicit is False:
        return False
    from core.auto_trainer.larc_recovery_timing import _friendship_evidence
    return _friendship_evidence(candidate) == "eligible_partner_observed"


def stamina_lesson_quality_adjustment(
    state: Any,
    candidate: Any,
    goal: Any,
    gain_result: dict[str, Any],
    components: dict[str, Any],
    *,
    energy_percent: float | None = None,
) -> dict[str, Any]:
    """Delegate optional Stamina quality adjustment to the active strategy extension."""
    return active_provider().stamina_lesson_quality_adjustment(
        params_for_goal(goal),
        state,
        candidate,
        goal,
        gain_result,
        components,
        energy_percent=energy_percent,
    )


def stamina_deficit_scarcity(
    state: Any,
    candidate: Any,
    goal: Any,
    gain_result: dict[str, Any],
) -> dict[str, Any]:
    """Delegate optional Stamina deficit scarcity adjustment to the active strategy extension."""
    return active_provider().stamina_deficit_scarcity(
        params_for_goal(goal),
        state,
        candidate,
        goal,
        gain_result,
    )


def high_hp_wisdom_stamina_trade(
    state: Any,
    candidate: Any,
    goal: Any,
    features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Delegate optional high-HP Wisdom/Stamina trade rule to the active strategy extension."""
    return active_provider().high_hp_wisdom_stamina_trade(
        params_for_goal(goal),
        state,
        candidate,
        goal,
        features=features,
    )


def apply_weak_training_recovery_comparison(
    state: Any,
    goal: Any,
    scored: list[dict[str, Any]],
    future_reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Delegate optional weak-training vs recovery comparison to the active strategy extension."""
    return active_provider().apply_weak_training_recovery_comparison(
        params_for_goal(goal),
        state,
        goal,
        scored,
        future_reference=future_reference,
    )
