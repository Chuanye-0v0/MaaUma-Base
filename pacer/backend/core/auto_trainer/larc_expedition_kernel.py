"""Shared pure terminal-stat, HP, schedule, and cashflow kernel for L'Arc.

Used by both ``larc_departure_planner`` (pre-expedition ordinals 32..35, 56..58)
and ``larc_tail263`` (second-expedition ordinals 60..65). Contains zero device,
OCR, or execution side effects.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from functools import lru_cache
from math import floor, isfinite
from time import perf_counter

from core.auto_trainer.larc_opportunity_forecast import effective_far_surplus_edge

STATS = ("speed", "stamina", "power", "guts", "wisdom")
ZERO = (0.0,) * 5
TAIL_FREE_DAYS = (60, 61, 62, 63, 65)
C1_FIXED_RACE_INCOMES = ((59, 100), (64, 100))

RESIDUAL_ALLOW = frozenset({
    "effective_bond",
    "friend_startup",
    "friend_coverage_preparation",
    "training_hint_bond",
    "net_charge",
    "newly_full",
    "ss_preparation",
    "cycle_reward_preparation",
    "charming_charge_progress",
    "future_recovery_cycle_progress",
    "mei_expected_charge",
    "mei_initial_contact_preference",
    "conditional_fan_route",
    "baseline_support",
    "battery_release_opportunity",
    "expectation_training_progress",
    "supporter_deadline_preference",
    "first_charming_preference",
    "full_group_preference",
    "stamina_lesson_quality",
})


def number(x) -> bool:
    return type(x) in (int, float) and isfinite(x)


@dataclass(frozen=True)
class State:
    raw: tuple[float, ...]
    hp: float
    cap: float
    pt: float
    caps_raw: tuple[float, ...] | None = None
    held_ss: bool = False
    outings: int = 0


@dataclass(frozen=True)
class Action:
    action_id: str
    kind: str
    raw_gain: tuple[float, ...] = ZERO
    pt: float = 0.0
    hp_delta: float = 0.0
    cap_delta: float = 0.0
    p_percent: float | None = 0.0
    training_type: int | None = None
    anchor_hp: float | None = None
    public_training_level: int | None = None
    allowed: bool = True
    rejected_reason: str | None = None
    baseline_score: float | None = None
    residual: float = 0.0
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class JointMenu:
    source_id: str
    source_calendar: int
    actions: tuple[Action, ...]


@dataclass(frozen=True)
class ScheduledTransition:
    calendar: int
    stats_gain: tuple[float, ...] = ZERO
    stats_unit: str = "raw"
    pt: float = 0.0
    hp_points: float = 0.0
    cap_points: float = 0.0
    label: str = ""
    condition: str = "condition_on_normal_settlement"
    basis: str = "explicit_caller_contract_not_observed_future"


@dataclass(frozen=True)
class Current:
    state: State
    calendar: int
    future_free_days: tuple[int, ...]
    actions: tuple[Action, ...]
    menus: tuple[JointMenu, ...]
    targets: tuple[float, ...]
    importance: tuple[float, ...] = (1.0,) * 5
    pt_factor: float = 0.85
    after_target_credit: float = 0.5
    failure_limit: float = 25.0
    risk_aversion_per_percent: float = 0.5
    assumptions: tuple[str, ...] = ()
    scheduled_transitions: tuple[ScheduledTransition, ...] = ()
    far_surplus_credit: float | None = None
    far_surplus_buffer: float = 100.0
    far_surplus_exact_buffer: bool = False
    scarcity_factors: tuple[float, ...] = (1.0,) * 5
    # Optional extension fields (defaults preserve baseline utility).
    deficit_convexity: float = 0.0
    pt_soft_cap: float | None = None
    pt_over_cap_factor: float = 1.0
    loh_deficit_kappas: tuple[float, ...] = (0.0,) * 5
    loh_deficit_starts: tuple[float, ...] = (60.0,) * 5
    loh_deficit_full: float = 220.0


def display(raw: float) -> float:
    return raw if raw <= 1200 else 1200 + floor((raw - 1200) / 2 + 1e-9)


def inverse_display(value: float) -> float:
    return value if value <= 1200 else 1200 + 2 * (value - 1200)


def raw_for_display_gain(current_display: float, gain: float) -> float:
    return inverse_display(current_display + gain) - inverse_display(current_display)


def utility(s: State, c: Current) -> float:
    cap = getattr(c, "pt_soft_cap", None)
    if cap:
        # Optional soft skill-budget cap for extension profiles.
        total = c.pt_factor * (min(s.pt, cap) + getattr(c, "pt_over_cap_factor", 1.0) * max(0.0, s.pt - cap))
    else:
        total = c.pt_factor * s.pt
    lam = getattr(c, "deficit_convexity", 0.0)
    sfs = c.scarcity_factors if len(c.scarcity_factors) == 5 else (1.0,) * 5
    d_kappas = getattr(c, "loh_deficit_kappas", (0.0,) * 5)
    d_starts = getattr(c, "loh_deficit_starts", (60.0,) * 5)
    d_full = float(getattr(c, "loh_deficit_full", 220.0))
    for idx, (raw, target, w, sf) in enumerate(zip(s.raw, c.targets, c.importance, sfs)):
        x = display(raw)
        inside = min(x, target)
        total += w * (inside + c.after_target_credit * max(0.0, x - target) + sf * max(0.0, inside - 1200.0))
        if c.far_surplus_credit is not None:
            edge = effective_far_surplus_edge(target, c.far_surplus_buffer, c.far_surplus_exact_buffer)
            total -= w * max(0.0, x - edge) * max(0.0, c.after_target_credit - c.far_surplus_credit)
        if lam and target > 0:
            # Optional convex deficit penalty for extension profiles.
            d = max(0.0, (target - x) / target)
            total -= w * lam * target * d * d / 2.0
        dk = float(d_kappas[idx]) if idx < len(d_kappas) else 0.0
        if dk > 0.0 and target > 0.0:
            ds = float(d_starts[idx]) if idx < len(d_starts) else 60.0
            over_gap = max(0.0, (target - x) - ds)
            if over_gap > 0.0:
                span = max(1.0, d_full - ds)
                quad = (over_gap * over_gap / (2.0 * span)) if over_gap <= span else (over_gap - 0.5 * span)
                total -= w * dk * quad
    return total


def success(s: State, a: Action) -> State:
    cap = max(1.0, s.cap + a.cap_delta)
    gains = tuple(max(0.0, x + g) for x, g in zip(s.raw, a.raw_gain))
    if s.caps_raw is not None:
        gains = tuple(min(x, lim) for x, lim in zip(gains, s.caps_raw))
    return replace(
        s,
        raw=gains,
        hp=max(0.0, min(cap, s.hp + a.hp_delta)),
        cap=cap,
        pt=s.pt + a.pt,
        held_ss=s.held_ss and a.kind != "ss",
        outings=max(0, s.outings - (a.kind == "mei_outing")),
    )


def failed(s: State, a: Action) -> State:
    return s


def apply_scheduled(s: State, c: Current, after: int, through: int) -> State:
    for event in sorted(c.scheduled_transitions, key=lambda x: x.calendar):
        if after < event.calendar <= through:
            gains = event.stats_gain
            if event.stats_unit == "display":
                gains = tuple(raw_for_display_gain(display(x), g) for x, g in zip(s.raw, gains))
            elif event.stats_unit != "raw":
                raise ValueError("scheduled_stats_unit_unknown")
            s = success(
                s,
                Action(
                    "scheduled:" + event.label,
                    "scheduled",
                    gains,
                    event.pt,
                    event.hp_points,
                    event.cap_points,
                ),
            )
    return s


def terminal_utility(s: State, c: Current, through: int) -> float:
    return utility(apply_scheduled(s, c, through, 67), c)


def jp_curve(training: int, hp_points: float, public_level: int) -> float:
    if hp_points > 60:
        return 0.0
    b = (-3.77, -3.74, -3.76, -3.81333, -3.286)[training]
    cc = (130.0, 127.0, 129.0, 133.5, 80.2)[training]
    return max(0.0, min(99.0, 0.0245 * hp_points ** 2 + b * hp_points + cc + 0.5 * (public_level - 1)))


def future_risk(a: Action, hp: float) -> float | None:
    if a.kind != "train":
        return 0.0
    if a.p_percent is None or a.anchor_hp is None or a.training_type is None or a.public_training_level is None:
        return None
    v = (
        a.p_percent
        + jp_curve(a.training_type, hp, a.public_training_level)
        - jp_curve(a.training_type, a.anchor_hp, a.public_training_level)
    )
    return float(floor(max(0.0, min(100.0, v)) + 0.5))


class BudgetExceeded(RuntimeError):
    pass


class Evaluator:
    def __init__(self, current: Current, max_ops: int):
        self.c, self.max_ops, self.ops = current, max_ops, 0
        self.cache = {}

    def tick(self):
        self.ops += 1
        if self.ops > self.max_ops:
            raise BudgetExceeded

    def actions(self, s: State, menu: JointMenu, day: int) -> tuple[Action, ...]:
        out = []
        for a in menu.actions:
            p = future_risk(a, s.hp)
            if p is not None and p <= self.c.failure_limit:
                out.append(replace(a, p_percent=p))
        out.append(Action("future:rest", "rest", hp_delta=50.0, assumptions=("overseas_rest50_no_mood_or_coin_value",)))
        return tuple(out)

    def immediate(self, s: State, a: Action, through_day: int = 65) -> float:
        self.tick()
        p = a.p_percent / 100.0
        return (
            (1 - p) * terminal_utility(success(s, a), self.c, through_day)
            + p * terminal_utility(failed(s, a), self.c, through_day)
            - self.c.risk_aversion_per_percent * a.p_percent
        )

    def choose(self, s: State, menu: JointMenu, day_index: int) -> Action:
        key = (s, menu.source_id, day_index)
        if key in self.cache:
            return self.cache[key]
        day = self.c.future_free_days[day_index]
        options = self.actions(s, menu, day)

        def q(a):
            if day_index + 1 == len(self.c.future_free_days):
                return self.immediate(s, a, day)
            p = a.p_percent / 100.0
            result = 0.0
            for prob, ns in ((1 - p, success(s, a)), (p, failed(s, a))):
                if not prob:
                    continue
                next_day = self.c.future_free_days[day_index + 1]
                ready = apply_scheduled(ns, self.c, day, next_day - 1)
                value = sum(max(self.immediate(ready, b, next_day) for b in self.actions(ready, m, next_day)) for m in self.c.menus)
                result += prob * value / len(self.c.menus)
            return result - self.c.risk_aversion_per_percent * a.p_percent

        chosen = max(options, key=lambda a: (q(a), a.action_id))
        self.cache[key] = chosen
        return chosen

    def rollout(self, s: State, path: int, stratum: int):
        route, aversion = [], 0.0
        n = len(self.c.menus)
        previous = self.c.calendar
        for k, day in enumerate(self.c.future_free_days):
            s = apply_scheduled(s, self.c, previous, day - 1)
            menu = self.c.menus[(path + k) % n]
            a = self.choose(s, menu, k)
            u = ((stratum + 0.5) / 4.0 + k * 0.38196601125) % 1.0
            ok = u >= a.p_percent / 100.0
            route.append({
                "day": day,
                "menu": menu.source_id,
                "action": a.action_id,
                "hp_before": round(s.hp, 4),
                "risk": a.p_percent,
                "success_quadrature": ok,
            })
            s = success(s, a) if ok else failed(s, a)
            aversion += self.c.risk_aversion_per_percent * a.p_percent
            previous = day
        s = apply_scheduled(s, self.c, previous, 67)
        return utility(s, self.c) - aversion, route, s


# ---------------------------------------------------------------------------
# Scheduled Terminal Contract (normal_schedule)
# ---------------------------------------------------------------------------

def normal_schedule(
    *,
    race_bonus_percent: float,
    final_level: int,
    mei_unlocked: bool,
    mei_completed: bool,
    mei_event_multiplier: float,
    reporter_band: str,
):
    if final_level not in (0, 1, 2, 3) or reporter_band not in ("low", "mid", "high"):
        raise ValueError("schedule_current_condition_unknown")
    multiplier = 1 + race_bonus_percent / 100

    def race(n, pt):
        return floor(n * multiplier), floor(pt * multiplier)

    a, p = race(5, 45)
    b, q = race(10, 40)
    events = [
        ScheduledTransition(
            59,
            (a + b + 10.0,) * 5,
            pt=p + q,
            label="calendar59_two_races_and_flat10",
            condition="model_scripted_calendar_normal_progress",
            basis="C1_fixed_source_runRace_plus_flat",
        ),
        ScheduledTransition(
            64,
            (float(race(7, 40)[0]),) * 5,
            pt=race(7, 40)[1],
            label="calendar64_race",
            condition="model_scripted_calendar_normal_progress",
            basis="C1_fixed_source_runRace",
        ),
    ]
    a, p = race(30, 140) if final_level >= 2 else race(10, 60)
    flat = 20 + 5 + (5 if final_level >= 1 else 0)
    events.append(
        ScheduledTransition(
            66,
            (float(a + flat),) * 5,
            pt=p + 60,
            label="final_race_and_ending_base_no_free_future_purchase",
            condition="condition_on_final_win_and_normal_settlement_current_owned_level_only",
            basis="C1_fixed_source_settleRace_checkEventAfterTrain_not_repeat_at67",
        )
    )
    if mei_unlocked:
        power = int(15 * mei_event_multiplier)
        guts = int((25 if mei_completed else 18) * mei_event_multiplier)
        pt = int((30 if mei_completed else 20) * mei_event_multiplier)
        events.append(
            ScheduledTransition(
                66,
                (0.0, 0.0, float(power), float(guts), 0.0),
                pt=pt,
                label="Mei_ending_completed" if mei_completed else "Mei_ending_uncompleted",
                condition="condition_on_normal_settlement_current_and_root_stage_cannot_cross5",
                basis="C1_ending_effect_times_explicit_declared_profile_event_multiplier",
            )
        )
    raw, pt = {"low": (0.0, 5), "mid": (3.0, 10), "high": (5.0, 20)}[reporter_band]
    events.append(
        ScheduledTransition(
            66,
            (raw,) * 5,
            pt=pt,
            label="reporter_ending_" + reporter_band,
            condition="condition_on_normal_settlement_explicit_reporter_band_assumption",
            basis="C1_ending_reporter_bond_threshold100_80",
        )
    )
    return tuple(events)


# ---------------------------------------------------------------------------
# Conditional Cashflow Reserve (reserve_for_action)
# ---------------------------------------------------------------------------

def reserve_for_action(
    now,
    levels_after_basket,
    current_conditional_income,
    current_p_percent,
    future_training_share,
    *,
    branch="ordinary",
    end_day=66,
    respect_observed_locks=True,
    skip_language=False,
    fixed_race_incomes=None,
    cash_before=None,
    basket_cost=None,
    existing_legacy_reserve=None,
    rules=None,
):
    """Return minimum post-basket cash needed before forecast incomes arrive."""
    base = {
        "schema_version": "larc.action_cashflow_reserve.v1",
        "planning_only": True,
        "new_payment_authority": False,
        "does_not_rollback_paid_basket": True,
        "future_income_is_not_current_cash": True,
        "diagnostics": [],
    }
    if (
        type(now) is not int
        or type(end_day) is not int
        or not isinstance(levels_after_basket, (list, tuple))
        or len(levels_after_basket) != 10
        or any(type(v) is not int or not 0 <= v <= (3 if i < 8 else 2) for i, v in enumerate(levels_after_basket))
        or not number(future_training_share)
        or not 0 <= future_training_share <= 1
    ):
        return {
            **base,
            "status": "legacy_fallback",
            "required_post_basket_reserve": existing_legacy_reserve,
            "diagnostics": ["unknown_scope_levels_or_share_keep_existing_reserve"],
        }
    if rules is None:
        from core.auto_trainer import larc_adaptation_schedule as rules
    kwargs = {"end_day": end_day, "respect_observed_locks": respect_observed_locks, "skip_language": skip_language}
    legacy = rules.future_reserve(levels_after_basket, now, branch, future_training_share, include_today_income=False, **kwargs)
    debts = [
        d
        for d in rules.obligations(levels_after_basket, now, respect_observed_locks=respect_observed_locks, skip_language=skip_language)
        if d[1] <= end_day
    ]
    deadlines = sorted({d[1] for d in debts})
    events = []
    if number(current_conditional_income) and current_conditional_income >= 0 and number(current_p_percent) and 0 <= current_p_percent <= 100:
        amount = current_conditional_income * (1 - current_p_percent / 100)
        events.append({
            "calendar": now,
            "available_phase": "after_current_action",
            "amount": amount,
            "kind": "current_training_success_expected",
            "conditional_amount": current_conditional_income,
            "p_percent": current_p_percent,
            "guaranteed": False,
            "basis": "explicit_current_conditional_income_times_success_probability",
        })
    else:
        base["diagnostics"].append("current_income_or_p_unknown_no_current_credit_original_future_prior_retained")
    for date in rules.free_action_days(now + 1, end_day):
        events.append({
            "calendar": date,
            "available_phase": "after_future_free_action",
            "amount": rules.income_at(date, branch, future_training_share),
            "kind": "legacy_future_training_prior",
            "guaranteed": False,
            "basis": "unchanged_formal_income_at_and_training_share",
        })
    seen = set()
    for event in fixed_race_incomes or ():
        if not isinstance(event, (list, tuple)) or len(event) != 2 or type(event[0]) is not int or not number(event[1]) or event[1] < 0:
            base["diagnostics"].append("invalid_explicit_fixed_income_skipped")
            continue
        date, amount = event
        if date not in (59, 64):
            base["diagnostics"].append("non59_64_fixed_income_not_in_declared_contract_skipped")
            continue
        if date in seen:
            base["diagnostics"].append("duplicate_fixed_income_not_double_counted")
            continue
        seen.add(date)
        if not now < date < end_day:
            continue
        events.append({
            "calendar": date,
            "available_phase": "after_fixed_race",
            "amount": amount,
            "kind": "explicit_fixed_C1_race_income",
            "guaranteed": False,
            "basis": "fixed_C1_jp_reference_condition_on_reaching_scripted_post_race_event_CN_unverified",
        })
    table = []
    required = 0.0
    for deadline in deadlines:
        payments = [{"slot": slot, "deadline": due, "cost": cost} for slot, due, cost in debts if due <= deadline]
        eligible = [e for e in events if e["calendar"] < deadline]
        expense = sum(p["cost"] for p in payments)
        income = sum(e["amount"] for e in eligible)
        need = max(0.0, expense - income)
        required = max(required, need)
        table.append({
            "deadline": deadline,
            "payment_phase": "before_race_or_action",
            "cumulative_original_obligations": payments,
            "cumulative_expense": expense,
            "eligible_prior_income_events": eligible,
            "eligible_prior_income_total": income,
            "minimum_initial_post_basket_cash": need,
            "same_day_income_cannot_fund_this_deadline": [e for e in events if e["calendar"] == deadline],
        })
    payable = (cash_before >= basket_cost) if number(cash_before) and number(basket_cost) and basket_cost >= 0 else None
    post = cash_before - basket_cost if payable is not None else None
    for row in table:
        row["cash_after_cumulative_payments_expected"] = (
            post + row["eligible_prior_income_total"] - row["cumulative_expense"]
        ) if post is not None else None
    return {
        **base,
        "status": "conditional_expected_forecast",
        "legacy_reserve": legacy,
        "required_post_basket_reserve": required,
        "reserve_difference": required - legacy,
        "income_events": sorted(events, key=lambda e: (e["calendar"], e["kind"])),
        "deadline_cashflows": table,
        "current_actual_cash": cash_before,
        "today_basket_cost": basket_cost,
        "today_purchase_payable_from_actual_cash_only": payable,
        "post_basket_cash_assuming_payment": post,
        "conditional_forecast_funded": bool(payable and post >= required) if payable is not None else None,
        "income_mode": "expected_not_guaranteed",
        "future_training_share_unchanged": future_training_share,
        "branch_unchanged": branch,
        "known_fixed_income_opt_in": fixed_race_incomes is not None,
        "rule_source": getattr(rules, "__file__", None),
        "CN_verified": False,
    }


# ---------------------------------------------------------------------------
# Tail Free-Action Rollout Ranker (ordinals 60..65)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=64)
def _rank_cached(current, max_ops=250000, _departure_test_only=False):
    return _rank_uncached(current, max_ops=max_ops, _departure_test_only=_departure_test_only)


def _rank(current, max_ops=250000, *, _departure_test_only=False):
    try:
        return deepcopy(_rank_cached(current, max_ops, _departure_test_only))
    except TypeError:
        return _rank_uncached(current, max_ops=max_ops, _departure_test_only=_departure_test_only)


def _rank_uncached(current, max_ops=250000, *, _departure_test_only=False):
    start = perf_counter()
    eligible = [a for a in current.actions if a.allowed]

    def fallback(reason, operations=0):
        return {
            "active": False,
            "reason": reason,
            "operations": operations,
            "seconds": perf_counter() - start,
            "ranking": [
                {"action_id": a.action_id, "score": a.baseline_score}
                for a in sorted(eligible, key=lambda a: a.baseline_score or 0, reverse=True)
            ],
        }

    if current.calendar in TAIL_FREE_DAYS:
        expected = TAIL_FREE_DAYS[TAIL_FREE_DAYS.index(current.calendar) + 1 :]
    elif _departure_test_only and current.calendar == 58:
        expected = TAIL_FREE_DAYS
    else:
        return fallback("outside_tail_free_action_calendar")
    if current.future_free_days != expected:
        return fallback("future_free_actions_not_strict_tail_suffix")
    if not eligible:
        return fallback("no_originally_eligible_current_actions")
    if type(current.failure_limit) not in (int, float) or not 0 <= current.failure_limit <= 100:
        return fallback("original_failure_limit_out_of_range_or_unknown")
    if any(not number(a.p_percent) or not 0 <= a.p_percent <= 100 or not number(a.hp_delta) for a in eligible):
        return fallback("current_pool_numeric_or_risk_unknown")
    if any(a.p_percent > current.failure_limit for a in eligible):
        return fallback("caller_changed_current_risk_eligibility")
    if expected:
        if len(current.menus) not in (4, 5):
            return fallback("four_or_five_observed_joint_menus_required")
        if any(type(m.source_calendar) is not int or m.source_calendar > current.calendar for m in current.menus):
            return fallback("future_reference_menu_not_observed")
        if any(len(m.actions) != 5 or any(future_risk(a, current.state.hp) is None for a in m.actions) for m in current.menus):
            return fallback("incomplete_joint_menu")
    if any(
        e.calendar not in (59, 64, 66, 67) or len(e.stats_gain) != 5 or e.stats_unit not in ("raw", "display")
        for e in current.scheduled_transitions
    ):
        return fallback("scheduled_transition_contract_unknown")

    ev = Evaluator(current, max_ops)
    base = terminal_utility(current.state, current, current.calendar)
    rows = []
    paths = len(current.menus) if expected else 0
    try:
        for a in eligible:
            p = a.p_percent / 100.0
            aversion = current.risk_aversion_per_percent * a.p_percent
            mean_terminal = [0.0] * 7
            representative = None
            if not expected:
                value = ev.immediate(current.state, a, current.calendar) + aversion
                for prob, ns in ((1 - p, success(current.state, a)), (p, failed(current.state, a))):
                    if not prob:
                        continue
                    end = apply_scheduled(ns, current, current.calendar, 67)
                    for j, num_val in enumerate([*(display(x) for x in end.raw), end.pt, end.hp]):
                        mean_terminal[j] += prob * num_val
                    if representative is None:
                        representative = {
                            "route": [],
                            "terminal_display": [display(x) for x in end.raw],
                            "terminal_pt": end.pt,
                            "terminal_hp": end.hp,
                        }
            else:
                value = 0.0
                for prob, ns in ((1 - p, success(current.state, a)), (p, failed(current.state, a))):
                    if not prob:
                        continue
                    branch = []
                    for path in range(paths):
                        for stratum in range(4):
                            v, route, end = ev.rollout(ns, path, stratum)
                            branch.append(v)
                            for j, num_val in enumerate([*(display(x) for x in end.raw), end.pt, end.hp]):
                                mean_terminal[j] += prob * num_val / (paths * 4.0)
                            if representative is None:
                                representative = {
                                    "route": route,
                                    "terminal_display": [display(x) for x in end.raw],
                                    "terminal_pt": end.pt,
                                    "terminal_hp": end.hp,
                                }
                    value += prob * sum(branch) / len(branch)
            numeric_hp = value - base
            direct = (
                (1 - p) * terminal_utility(success(current.state, a), current, current.calendar)
                + p * terminal_utility(failed(current.state, a), current, current.calendar)
                - base
            )
            residual_credit = a.residual * (1 - p if a.kind == "train" else 1.0)
            rows.append({
                "action_id": a.action_id,
                "score": numeric_hp + residual_credit - aversion,
                "unified_terminal_delta": numeric_hp,
                "current_residual": residual_credit,
                "current_residual_success_conditional_raw": a.residual,
                "current_risk_aversion": -aversion,
                "baseline_score": a.baseline_score,
                "current_direct_expected_terminal_utility": direct,
                "future_contribution_including_future_aversion": numeric_hp - direct,
                "mean_terminal_display": mean_terminal[:5],
                "mean_terminal_pt": mean_terminal[5],
                "mean_terminal_hp": mean_terminal[6],
                "current_success_display": [display(x) for x in success(current.state, a).raw],
                "current_success_hp": success(current.state, a).hp,
                "assumptions": a.assumptions,
                "representative_not_forecast": representative,
            })
    except BudgetExceeded:
        return fallback("whole_pool_compute_budget_exceeded", ev.ops)

    return {
        "active": True,
        "kind": "research_causal_rollout_not_execution_policy",
        "ranking": sorted(rows, key=lambda r: r["score"], reverse=True),
        "rejected": [{"action_id": a.action_id, "reason": a.rejected_reason} for a in current.actions if not a.allowed],
        "operations": ev.ops,
        "cache_states": len(ev.cache),
        "seconds": perf_counter() - start,
        "future_steps": len(expected),
        "total_main_actions": 1 + len(expected),
        "policy_lookahead": 2,
        "scenario_paths": paths,
        "outcome_strata": 4 if expected else 0,
        "terminal_hp_credit": 0,
        "assumptions": current.assumptions,
        "scheduled_transitions": [
            {
                "calendar": e.calendar,
                "stats_gain": e.stats_gain,
                "stats_unit": e.stats_unit,
                "pt": e.pt,
                "hp_points": e.hp_points,
                "cap_points": e.cap_points,
                "label": e.label,
                "condition": e.condition,
                "basis": e.basis,
            }
            for e in current.scheduled_transitions
        ],
        "current_calendar": current.calendar,
        "future_purchase_policy": "hold_current_paid_levels",
    }


def rank(current, max_ops=250000):
    """Active only for actual tail free days; never runs a world or executes."""
    return _rank(current, max_ops)
