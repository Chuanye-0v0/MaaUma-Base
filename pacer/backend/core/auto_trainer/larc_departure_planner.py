"""Unified pre-expedition joint departure route planner (d32/34/35 & d56/57/58).

Consolidates basket quotes, partial current-gain lower bounds, finite equal-budget
route planning with remaining foreign energy frontier (H) and entry-finance (C),
and real-observation request binding onto ``larc_expedition_kernel`` and
``LarcStrategyParams`` with zero historical revision branching.
"""
from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from itertools import combinations
import json
from math import fsum, isfinite
from time import perf_counter

from core.auto_trainer import larc_calendar as CAL
from core.auto_trainer import larc_expedition_kernel as K
from core.auto_trainer.larc_opportunity_forecast import display_gain, raw_gain
from core.auto_trainer.larc_strategy_params import DEFAULT_PARAMS, params_for_goal, unified_larc_enabled
from core.auto_trainer.larc_tail263 import consumer as retained
from core.auto_trainer.rejection_policy import is_hard_rejected
from core.auto_trainer.state import STAT_KEYS, TRAINING_TYPES

STRATEGY_ID = "1.8"
DISPLAY_LABEL = "1.8"
PRESET_ID = "unified_v1_8"
POLICY_VERSION = "larc_unified_v1_8_preview"
BASELINE_ARITHMETIC_REVISION = "1.7.26.3"
SOURCE_CANDIDATE = "R5-C"
BASE_COMPUTATION_MAX_OPS = DEFAULT_PARAMS.departure_base_max_ops
LATE_COMPUTATION_MAX_OPS = DEFAULT_PARAMS.departure_late_max_ops
AUTOMATIC_CULL_ENABLED = False

VERSION = STRATEGY_ID
SCHEMA = "larc.departure_route_request.v1"
STATS = K.STATS
TABS = retained.TABS
DAYS = (32, 34, 35, 56, 57, 58)
PLAN_KEY = "larc_strategy18_plan"
BASIS = "current_verified_gain_lower_bound_unknown_attributes_not_observed_zero"
PROXY_KEYS = {
    "net_charge",
    "newly_full",
    "ss_preparation",
    "cycle_reward_preparation",
    "charming_charge_progress",
    "mei_expected_charge",
}
number = retained.number


def enabled_for_revision(revision):
    return revision == STRATEGY_ID


def enabled_for_goal(goal):
    return unified_larc_enabled(goal)


def identity():
    return {
        "strategy_id": STRATEGY_ID,
        "display_label": DISPLAY_LABEL,
        "preset_id": PRESET_ID,
        "goal_revision": STRATEGY_ID,
        "policy_version": POLICY_VERSION,
        "source_candidate": SOURCE_CANDIDATE,
        "baseline_arithmetic_revision": BASELINE_ARITHMETIC_REVISION,
        "automatic_cull_enabled": AUTOMATIC_CULL_ENABLED,
    }


def num(x):
    return type(x) in (int, float) and isfinite(x)


def quote_late_baskets(owned, source):
    """Finite conditional c60 quotes from existing rules, never a live shop receipt."""
    from core.auto_trainer.larc_adaptation import selected_upgrade_cost

    slots = [i for i in (0, 1, 2, 3, 4, 6, 7) if owned[i] in (1, 2)]
    plans = []
    for size in (1, 2):
        for chosen in combinations(slots, size):
            if sum(i < 5 for i in chosen) > 1:
                continue
            levels = list(owned)
            buys = []
            for i in chosen:
                after = levels[:]
                after[i] = 3
                cost = selected_upgrade_cost(levels, after)
                if type(cost) is not int or cost <= 0:
                    raise ValueError("late_existing_rule_price_unavailable")
                buys.append({"slot": i, "from_level": levels[i], "to_level": 3, "cost": cost})
                levels = after
            total = sum(b["cost"] for b in buys)
            if selected_upgrade_cost(owned, levels) != total:
                raise ValueError("late_existing_rule_price_sum_mismatch")
            plans.append(
                {
                    "plan_id": "+".join(str(i) + ":3" for i in chosen),
                    "targets": levels,
                    "cost": total,
                    "atomic_buys": buys,
                    "legal": True,
                    "price_basis": "existing_rule_reference",
                }
            )
    plans.append(
        {
            "plan_id": "hold",
            "targets": list(owned),
            "cost": 0,
            "atomic_buys": [],
            "legal": True,
            "price_basis": "existing_rule_reference",
        }
    )
    assert len(plans) <= 29
    return {
        "schema_version": "larc.entry_investment_policy.v1",
        "calendar": 60,
        "selection_mode": "joint_basket_action",
        "source": source,
        "basis": "finite_rule_quotes_for_simulated_entry_menu_not_actual_future_purchase",
        "entry_unlocked_levels": list(owned),
        "alternatives": plans,
        "price_rule": "existing_selected_upgrade_cost",
        "no_hypothetical_paid_or_locked_upgrade": True,
        "actual_purchase_execution_changed": False,
        "forecast_only": True,
    }


def lower_bound(tab, gains, current_stats, numeric, unread_attributes, verified_zero=()):
    """Convert an already validated numeric verdict to a current-only vector."""
    if not unread_attributes or any(key not in TRAINING_TYPES for key in unread_attributes):
        return None
    if numeric.get("usable") is not True or numeric.get("failure_rate_verified") is not True:
        return None
    primary = numeric.get("primary_gain")
    if type(primary) is not int or primary < 0 or tab in unread_attributes:
        return None
    used = {key: value for key, value in gains.items() if key in STAT_KEYS}
    verified = numeric.get("verified_gains") or {}
    if set(used).intersection(unread_attributes) or any(
        type(value) is not int or value < 0 or verified.get(key) != value for key, value in used.items()
    ):
        return None
    pt = used.get("skill_pt")
    if type(pt) is not int or used.get(tab, 0) != primary:
        return None
    known = {key: used[key] for key in TRAINING_TYPES if key in used}
    for key in verified_zero:
        if key not in unread_attributes:
            known.setdefault(key, 0)
    known.setdefault(tab, primary)
    mask = {key: key not in known for key in TRAINING_TYPES}
    return (
        {key: raw_gain(current_stats[key], known.get(key, 0)) for key in TRAINING_TYPES},
        pt,
        {
            "basis": BASIS,
            "unknown_gain_mask": mask,
            "verified_display_gains": {**known, "skill_pt": pt},
            "unread_gain_attributes": sorted(set(unread_attributes)),
        },
    )


def complete_current_sample(state, sample):
    """Build a local copy for the H request. The historical sample stays strict."""
    from core.auto_trainer.larc_candidate_quality import _has_verified_zero_column, numeric_evidence
    from core.auto_trainer.larc_gain_evidence import gain_evidence

    raw, points, evidence = {}, {}, {}
    for candidate in state.training_options:
        tab = candidate.training_type
        if tab in sample["raw_gains"]:
            continue
        reading = gain_evidence(candidate)
        if reading["unverified_used_attributes"] or not reading["unresolved_rows"]:
            continue
        unread = [row["attribute"] for row in reading["unresolved_rows"]]
        data = candidate.raw_data.get("stats_gain_ocr") or {}
        columns = data.get("columns", [])
        zeros = [
            key
            for key in TRAINING_TYPES
            if key not in candidate.stats_gain and _has_verified_zero_column(data, columns, key)
        ]
        partial = lower_bound(tab, candidate.stats_gain, state.current_stats, numeric_evidence(candidate), unread, zeros)
        if partial is not None:
            raw[tab], points[tab], evidence[tab] = partial
    if not evidence:
        return sample, evidence
    return (
        {
            **sample,
            "raw_gains": {**sample["raw_gains"], **raw},
            "skill_points": {**sample["skill_points"], **points},
        },
        evidence,
    )


def _source_binding(source, request, *, past=False):
    if not isinstance(source, dict) or source.get("run_session_id") != request["run_session_id"]:
        return False
    t = source.get("turn_index")
    return type(t) is int and (t < request["turn_index"] if past else t == request["turn_index"])


@dataclass(frozen=True)
class Effect:
    action: object
    outcomes: tuple
    income: float | None = None
    cost: float = 0.0
    newly_full: int = 0
    mei_stage: int | None = None
    components: tuple = ()
    proxy_fractions: tuple = ()
    source_basis: str = ""


@dataclass(frozen=True)
class Node:
    physical: object
    cash: float
    ready_lower: int
    held_offer_left: bool
    mei_index: int
    deferred: int
    owned_levels: tuple = ()
    entry_applied: bool = False
    entry_plan_id: str | None = None
    entry_cost: float = 0.0
    entry_cash_before: float | None = None
    ss_wins: int | None = None


@dataclass(frozen=True)
class Menu:
    source_id: str
    source_turn: int
    source_calendar: int
    effects: tuple


def _parse_effect(data, request, *, past=False):
    if not isinstance(data, dict) or not _source_binding(data.get("source"), request, past=past):
        raise ValueError("action_source_run_or_turn")
    kind = data.get("kind")
    aid = data.get("action_id")
    if kind not in ("train", "ss", "mei_outing", "rest") or not isinstance(aid, str):
        raise ValueError("action_kind_or_id")
    gains = data.get("raw_gain")
    pt = data.get("Pt")
    p = data.get("p_percent")
    if (
        not isinstance(gains, (list, tuple))
        or len(gains) != 5
        or any(not num(x) or x < 0 for x in gains)
        or not num(pt)
        or pt < 0
    ):
        raise ValueError("complete_preview_raw_equivalent_or_Pt_unknown")
    if not num(p) or not 0 <= p <= 100:
        raise ValueError("risk_unknown")
    hp = data.get("hp_delta_points")
    cap = data.get("cap_delta_points", 0.0)
    interval = data.get("hp_delta_bounds")
    if hp is None and isinstance(interval, (list, tuple)) and len(interval) == 2 and all(num(x) for x in interval):
        hp = min(interval)
    if not num(hp) or not num(cap) or cap < 0:
        raise ValueError("HP_points_or_cap_unknown")
    outcomes = data.get("recovery_outcomes")
    if outcomes is None:
        outcomes = ((1.0, hp, cap),)
    else:
        outcomes = tuple(tuple(x) for x in outcomes)
        if (
            not outcomes
            or any(len(x) != 3 or not all(num(v) for v in x) or x[0] < 0 or x[2] < 0 for x in outcomes)
            or abs(sum(x[0] for x in outcomes) - 1) > 1e-8
        ):
            raise ValueError("declared_recovery_outcomes_invalid")
    t = data.get("training_type")
    t = STATS.index(t) if t in STATS else t
    if kind == "train" and (type(t) is not int or not 0 <= t < 5):
        raise ValueError("training_type_unknown")
    if past and kind == "train" and (not num(data.get("anchor_hp_points")) or type(data.get("training_level_prior")) is not int):
        raise ValueError("past_risk_anchor_or_declared_level_unknown")
    legacy = data.get("legacy_residual", 0.0)
    if not num(legacy) or (legacy and not data.get("legacy_residual_basis")):
        raise ValueError("legacy_residual_requires_explicit_basis")
    action = K.Action(
        aid,
        kind,
        tuple(gains),
        pt,
        hp,
        cap,
        p,
        training_type=t,
        anchor_hp=data.get("anchor_hp_points"),
        public_training_level=data.get("training_level_prior"),
        allowed=data.get("allowed") is True,
        baseline_score=data.get("baseline_score"),
        residual=legacy,
        assumptions=tuple(data.get("assumptions", ())),
    )
    newly = data.get("newly_full_lower_bound", 0)
    if type(newly) is not int or not 0 <= newly <= 15:
        raise ValueError("newly_full_lower_bound_unknown")
    if newly and not data.get("newly_full_basis"):
        raise ValueError("newly_full_basis_required")
    components = tuple((k, float(v)) for k, v in (data.get("components") or {}).items() if num(v))
    fractions = tuple(
        (k, float(v))
        for k, v in (data.get("modeled_proxy_fractions") or {}).items()
        if k in PROXY_KEYS and num(v) and 0 <= v <= 1
    )
    income = data.get("conditional_income_points")
    if income is not None and (not num(income) or income < 0):
        raise ValueError("income_not_nonnegative_or_unknown")
    cost = data.get("current_basket_cost", 0.0)
    if not num(cost) or cost < 0 or (past and cost):
        raise ValueError("future_or_negative_payment_not_supported")
    return Effect(
        action,
        outcomes,
        income,
        cost,
        newly,
        data.get("mei_stage"),
        components,
        fractions,
        str(data.get("basis", "")),
    )


class Planner:
    def __init__(self, request, max_ops, *, reserve_policy=None):
        self.r = request
        self.ops = 0
        self.max_ops = max_ops
        self.cache = {}
        self.notes = []
        if (
            request.get("schema_version") != SCHEMA
            or not isinstance(request.get("run_session_id"), str)
            or type(request.get("turn_index")) is not int
        ):
            raise ValueError("request_binding_schema")
        now = request.get("calendar_ordinal")
        end = request.get("last_free_day")
        if request.get("period_status") != "verified" or type(now) is not int or type(end) is not int or CAL.is_overseas(now):
            raise ValueError("verified_domestic_departure_scope_required")
        self.days = tuple(CAL.free_action_days(now, end + 1))
        if not self.days or self.days[0] != now or not any(CAL.is_overseas(d) for d in self.days) or len(self.days) > 8:
            raise ValueError("bounded_departure_free_action_budget")
        self.stop = request.get("settlement_through", end)
        if type(self.stop) is not int or self.stop < end:
            raise ValueError("window_settlement_boundary")
        self.goal = request["goal"]
        limit = self.goal.get("failure_limit")
        if request.get("risk_limit_contract") == "configured_goal_risk_v1":
            if not num(limit) or not 0 <= limit <= 100:
                raise ValueError("configured_risk_limit_must_be_percent_0_to_100")
        elif limit != 25:
            raise ValueError("original_risk25_scope_only_keep_original_goal")
        initial = request["state"]
        raw = initial.get("raw_equivalent")
        hp = initial.get("HP_points")
        cap = initial.get("capacity_points")
        pt = initial.get("Pt")
        cash = initial.get("cash")
        if (
            not isinstance(raw, (list, tuple))
            or len(raw) != 5
            or any(not num(x) or x < 0 for x in raw)
            or not all(num(x) for x in (hp, cap, pt, cash))
            or not 0 < cap <= 120
            or not 0 <= hp <= cap
            or cash < 0
        ):
            raise ValueError("current_state_units_or_cash_unknown")
        owned = initial.get("owned_levels")
        if (
            not isinstance(owned, (list, tuple))
            or len(owned) != 10
            or any(type(x) is not int or not 0 <= x <= (3 if i < 8 else 2) for i, x in enumerate(owned))
        ):
            raise ValueError("current_owned_levels_unknown")
        self.owned = tuple(owned)
        targets = self.goal.get("targets")
        importance = self.goal.get("importance", [1.0] * 5)
        if len(targets or ()) != 5 or len(importance) != 5 or any(not num(x) for x in (*targets, *importance)):
            raise ValueError("goal_targets_or_importance")
        caps = initial.get("caps_raw")
        if caps is not None and (len(caps) != 5 or any(not num(x) for x in caps)):
            raise ValueError("caps_unknown_or_invalid")
        sfs = self.goal.get("scarcity_factors")
        if sfs is None:
            scarcity_tuple = (1.0,) * 5
        elif len(sfs) != 5 or any(not num(x) for x in sfs):
            raise ValueError("goal_scarcity_factors")
        else:
            scarcity_tuple = tuple(float(x) for x in sfs)
        fsc = self.goal.get("far_surplus_credit")
        if fsc is not None and not num(fsc):
            raise ValueError("goal_far_surplus_credit")
        fsb = self.goal.get("far_surplus_buffer", 100.0)
        if not num(fsb):
            raise ValueError("goal_far_surplus_buffer")
        self.valuation = K.Current(
            K.State(tuple(raw), hp, cap, pt, tuple(caps) if caps is not None else None),
            now,
            (),
            (),
            (),
            tuple(targets),
            tuple(importance),
            pt_factor=self.goal.get("pt_factor", 0.85),
            after_target_credit=self.goal.get("after_target_credit", 0.5),
            failure_limit=limit,
            risk_aversion_per_percent=self.goal.get("risk_aversion_per_percent", 0.5),
            far_surplus_credit=float(fsc) if fsc is not None else None,
            far_surplus_buffer=float(fsb),
            scarcity_factors=scarcity_tuple,
        )
        from core.auto_trainer.larc_strategy_extension import active_provider

        self.arc2_reserve = None
        self._has_terminal_override = active_provider().planner_init_extras(self, request)
        self.root = tuple(_parse_effect(x, request) for x in request.get("current_actions", []))
        if len({e.action.action_id for e in self.root}) != len(self.root):
            raise ValueError("duplicate_current_action_id")
        trains = [e for e in self.root if e.action.kind == "train"]
        if len(trains) != 5 or {e.action.training_type for e in trains} != set(range(5)):
            raise ValueError("current_joint_five_trainings_required")
        self.menus = {"domestic": [], "overseas": []}
        for phase, rows in (request.get("past_menus_by_phase") or {}).items():
            if phase not in self.menus:
                continue
            for row in rows:
                source = row.get("source") or {}
                day = row.get("source_calendar")
                try:
                    if not _source_binding(source, request, past=True) or type(day) is not int or day >= now:
                        raise ValueError("not_strict_past_menu")
                    if any((x.get("source") or {}).get("turn_index") != source["turn_index"] for x in row["actions"]):
                        raise ValueError("past_joint_menu_mixed_turns")
                    effects = tuple(_parse_effect(x, request, past=True) for x in row["actions"])
                    if (
                        len(effects) != 5
                        or {e.action.training_type for e in effects} != set(range(5))
                        or any(e.action.kind != "train" for e in effects)
                    ):
                        raise ValueError("complete_joint_five_past_trainings_required")
                    self.menus[phase].append(Menu(str(row["source_id"]), source["turn_index"], day, effects))
                except (KeyError, TypeError, ValueError) as exc:
                    self.notes.append({"menu": row.get("source_id"), "reason": str(exc)})
        for day in self.days[1:]:
            if not self.menus[self.phase(day)]:
                raise ValueError("strict_past_projected_menu_missing_for_" + self.phase(day))
        self.ss = request.get("ss") or {}
        ready = self.ss.get("ready_count_lower_bound", 0)
        if type(ready) is not int or not 0 <= ready <= 15:
            raise ValueError("current_ready_lower_bound_unknown")
        self.held = None
        self.generated = None
        if self.ss.get("held_offer") is not None:
            self.held = _parse_effect(self.ss["held_offer"], request)
            if self.held.action.kind != "ss" or self.ss.get("held_member_count") != 5 or ready < 5:
                raise ValueError("held_offer_not_current_full5")
        if self.ss.get("generated_offer_prior") is not None:
            packet = self.ss["generated_offer_prior"]
            if packet.get("estimated_not_current_offer") is not True:
                raise ValueError("generated_offer_must_be_explicit_past_prior")
            self.generated = _parse_effect(packet, request, past=True)
            if self.generated.action.kind != "ss":
                raise ValueError("generated_offer_prior_kind")
        self.mei = request.get("mei") or {}
        self.outings = ()
        if self.mei.get("unlocked") is True:
            if self.mei.get("status") != "accepted" or not _source_binding(self.mei.get("source"), request):
                raise ValueError("current_Mei_source_not_accepted")
            completed = self.mei.get("completed")
            if type(completed) is not int or not 0 <= completed <= 5:
                raise ValueError("Mei_completed_unknown")
            self.outings = tuple(_parse_effect(x, request) for x in self.mei.get("next_stages", []))
            if any(
                e.action.kind != "mei_outing" or e.mei_stage != completed + i + 1 or e.mei_stage > 5
                for i, e in enumerate(self.outings)
            ):
                raise ValueError("Mei_stage_sequence_invalid")
        obligation = self.ss.get("deadline_obligation") or {}
        self.limit = obligation.get("max_deferrals") if obligation.get("enforced") is True else None
        self.debt_days = frozenset(obligation.get("scope_days", [])) if self.limit is not None else frozenset()
        if self.limit is not None and (self.limit not in (0, 1) or any(d not in self.days or CAL.is_overseas(d) for d in self.debt_days)):
            raise ValueError("explicit_stage_deadline_scope_invalid")
        deferred = obligation.get("already_deferred") if self.limit is not None else 0
        if type(deferred) is not int or deferred < 0:
            raise ValueError("deadline_debt_unknown_no_new_grace")
        self.initial = Node(self.valuation.state, cash, ready, self.held is not None, 0, deferred)
        self.events = tuple(request.get("scheduled_events") or ())
        if any(
            type(e.get("calendar")) is not int
            or e["calendar"] < now
            or e["calendar"] > self.stop
            or (e["calendar"] == now and e.get("application_phase") != "after_main_action")
            for e in self.events
        ):
            raise ValueError("scheduled_event_outside_explicit_future_window")
        for event in self.events:
            phase = event.get("application_phase", "between_free_actions")
            if phase not in ("between_free_actions", "after_main_action") or (
                phase == "after_main_action" and event["calendar"] not in self.days
            ):
                raise ValueError("scheduled_event_application_phase_invalid")
            if "cash_after_event" in event:
                raise ValueError("scheduled_cash_must_be_explicit_income_delta_not_absolute_balance")
            if any(not num(event.get(k, 0.0)) for k in ("Pt", "HP_points", "capacity_points", "cash_income_points")):
                raise ValueError("scheduled_numeric_units_unknown")
            if event.get("cash_income_points", 0.0) < 0:
                raise ValueError("scheduled_income_must_be_nonnegative_delta")
            if event.get("condition") not in (
                "observed_fixed_rule",
                "condition_on_normal_progress",
                "condition_on_normal_settlement",
            ):
                raise ValueError("scheduled_event_condition_unknown")
            if len(event.get("stats_gain", [0] * 5)) != 5 or event.get("stats_unit", "raw") not in ("raw", "display"):
                raise ValueError("scheduled_event_units")
        self.completion_bonus = request.get("terminal_mei_completion_bonus")
        initial_completed = self.mei.get("completed") if self.mei.get("unlocked") is True else 0
        can_complete = (
            type(initial_completed) is int
            and initial_completed < 5
            and initial_completed + min(len(self.outings), sum(not CAL.is_overseas(d) for d in self.days)) >= 5
        )
        if can_complete and self.stop >= 66 and self.completion_bonus is None:
            raise ValueError("stage5_terminal_completion_delta_contract_required")
        if self.completion_bonus is not None:
            bonus = self.completion_bonus
            if (
                bonus.get("calendar") not in (66, 67)
                or bonus.get("base_mei_completed") is not False
                or bonus.get("condition") != "condition_on_normal_settlement"
                or bonus.get("stats_unit") not in ("raw", "display")
                or len(bonus.get("stats_gain", ())) != 5
                or any(not num(v) for v in bonus["stats_gain"])
                or not num(bonus.get("Pt"))
                or not bonus.get("basis")
            ):
                raise ValueError("terminal_Mei_completion_delta_contract_invalid")
        self.terminal = request.get("terminal_energy_contract") or {}
        self.entry_energy_contract = request.get("expedition_entry_energy_contract") or {}
        self.entry_band_calendar = (
            self.entry_energy_contract.get("entry_calendar")
            if self.entry_energy_contract.get("active") is True
            else None
        )
        self.terminal_hp_covered = self.stop >= 65 or num(self.terminal.get("legacy_coefficient"))
        if not self.terminal_hp_covered:
            self.notes.append({"reason": "window_after_HP_value_uncovered_not_complete_career_Q"})
        self.entry_policy = None
        self.owned_menus = {}
        self._owned_menu_index = {}
        self.late_joint = False
        self.first_entry_adaptive = False
        self.reserve_policy = reserve_policy
        # An SS-deadline node without any priced SS effect may keep other actions when enabled.
        self.soft_ss_due = active_provider().planner_soft_ss_due(request)
        self.soft_due_relaxed = 0
        self.soft_rest_fallbacks = 0
        self.entry_choices = {}
        self.entry_finance_by_paid = {}
        self.post_paid_numeric_cache = {}
        self.post_paid_numeric_hits = 0
        self._choice_value_cache = {}
        self._parsed_rest = {}
        self.numeric_memo_enabled = request.get("late_numeric_memo", True) is not False
        self.entry_joint_stats = {"calls": 0, "cache_hits": 0, "Q_pairs": 0, "finance_checks": 0}
        self.entry_joint_examples = []
        self.first_entry_stats = {"calls": 0, "cache_hits": 0, "Q_pairs": 0, "unpriced_baskets": {}}
        self.first_entry_examples = []
        if request.get("entry_investment_policy") is not None:
            self.install_entry_policy(request["entry_investment_policy"])
        frontier = request.get("remaining_energy_frontier") or {}
        self.frontier_enabled = frontier.get("enabled") is True
        self.frontier_ops = 0
        self.frontier_tables = {}
        self.frontier_training_tables = {}
        self.frontier_rewards = {}
        if self.late_joint and not self.frontier_enabled:
            raise ValueError("late_joint_requires_original_H_continuation")
        if self.first_entry_adaptive and not self.frontier_enabled:
            raise ValueError("first_entry_adaptive_requires_original_H_continuation")
        if self.frontier_enabled:
            if frontier.get("grid_points") != 5:
                raise ValueError("remaining_energy_frontier_grid5_only")
            if any(not CAL.is_overseas(d) for d in self.days[3:]):
                raise ValueError("remaining_energy_frontier_requires_foreign_suffix")
            self.frontier_anchor = self.terminal_value(self.after_main(self.initial, self.days[0]), self.days[0])[1].physical
            self.frontier_anchor_utility = K.utility(self.frontier_anchor, self.valuation)

    @staticmethod
    def valid_levels(value):
        return (
            isinstance(value, (list, tuple))
            and len(value) == 10
            and all(type(v) is int and 0 <= v <= (3 if i < 8 else 2) for i, v in enumerate(value))
        )

    def install_entry_policy(self, policy):
        late = isinstance(policy, dict) and policy.get("calendar") == 60 and policy.get("selection_mode") == "joint_basket_action"
        entry = 60 if late else 36
        if (
            not isinstance(policy, dict)
            or policy.get("schema_version") != "larc.entry_investment_policy.v1"
            or policy.get("calendar") != entry
            or entry not in self.days
            or self.days[0] >= entry
            or not _source_binding(policy.get("source"), self.r)
            or not policy.get("basis")
        ):
            raise ValueError("entry_policy_source_or_scope_invalid")
        if late and not callable(self.reserve_policy):
            raise ValueError("late_entry_requires_pure_reserve_callback")
        unlocked = policy.get("entry_unlocked_levels")
        if not self.valid_levels(unlocked) or any(
            b < a or (b != a and not (a == 0 and b == 1)) for a, b in zip(self.owned, unlocked)
        ):
            raise ValueError("entry_unlock_must_be_declared_monotone0to1_only")
        if late and tuple(unlocked) != self.owned:
            raise ValueError("late_numeric_owned_must_not_assume_conditional_slot8_unlock")
        if tuple(unlocked) != self.owned and not policy.get("unlock_basis"):
            raise ValueError("entry_unlock_basis_required_not_current_owned_fact")
        plans = policy.get("alternatives")
        if not isinstance(plans, list) or not plans:
            raise ValueError("entry_ordered_policy_alternatives_required")
        seen = set()
        targets_needed = set()
        for p in plans:
            target = p.get("targets")
            cost = p.get("cost")
            buys = p.get("atomic_buys")
            if (
                not isinstance(p.get("plan_id"), str)
                or p["plan_id"] in seen
                or not self.valid_levels(target)
                or any(b < a for a, b in zip(unlocked, target))
                or not num(cost)
                or cost < 0
                or p.get("legal") is not True
                or p.get("price_basis") not in ("existing_rule_reference", "accepted_conditional_entry_quote")
                or not isinstance(buys, list)
            ):
                raise ValueError("entry_plan_levels_cost_legality_or_identity_invalid")
            changed = {i for i, (a, b) in enumerate(zip(unlocked, target)) if b != a}
            if late and (len(changed) > 2 or sum(i < 5 for i in changed) > 1 or any(target[i] != 3 for i in changed)):
                raise ValueError("late_basket_two_slots_one_facility_to3_only")
            paid = set()
            total = 0.0
            for buy in buys:
                slot = buy.get("slot")
                if (
                    type(slot) is not int
                    or not 0 <= slot < 8
                    or slot in paid
                    or buy.get("from_level") != unlocked[slot]
                    or unlocked[slot] < 1
                    or buy.get("to_level") != target[slot]
                    or target[slot] <= unlocked[slot]
                    or not num(buy.get("cost"))
                    or buy["cost"] <= 0
                ):
                    raise ValueError("entry_atomic_quote_unknown_locked_or_duplicate")
                paid.add(slot)
                total += buy["cost"]
            if changed != paid or abs(total - cost) > 1e-8:
                raise ValueError("entry_atomic_quote_sum_or_changed_levels_mismatch")
            seen.add(p["plan_id"])
            targets_needed.add(tuple(target))
        last = plans[-1]
        if last["cost"] != 0 or tuple(last["targets"]) != tuple(unlocked) or last["atomic_buys"]:
            raise ValueError("entry_policy_requires_last_zero_cost_hold")
        base = self.menus["overseas"]
        byid = {m.source_id: m for m in base}
        if len(byid) != len(base):
            raise ValueError("entry_base_joint_menu_source_id_ambiguous")
        variants = self.r.get("menus_by_owned")
        if not isinstance(variants, list):
            raise ValueError("entry_complete_menus_by_owned_required")
        for variant in variants:
            owned = variant.get("owned_levels")
            if not self.valid_levels(owned) or tuple(owned) in self.owned_menus:
                raise ValueError("entry_menu_owned_key_invalid_or_duplicate")
            rows = variant.get("overseas")
            if not isinstance(rows, list) or len(rows) != len(base):
                raise ValueError("entry_owned_menu_count_mismatch")
            parsed = {}
            for row in rows:
                sid = row.get("source_id")
                original = byid.get(sid)
                source = row.get("source") or {}
                if (
                    original is None
                    or sid in parsed
                    or not _source_binding(source, self.r, past=True)
                    or source.get("turn_index") != original.source_turn
                    or row.get("source_calendar") != original.source_calendar
                    or any((a.get("source") or {}).get("turn_index") != original.source_turn for a in row.get("actions", []))
                ):
                    raise ValueError("entry_owned_menu_not_same_strict_past_source")
                effects = tuple(_parse_effect(a, self.r, past=True) for a in row.get("actions", []))
                if (
                    len(effects) != 5
                    or {e.action.training_type for e in effects} != set(range(5))
                    or any(e.action.kind != "train" for e in effects)
                ):
                    raise ValueError("entry_owned_menu_joint_five_required")
                old = {e.action.training_type: e for e in original.effects}
                for e in effects:
                    prior = old[e.action.training_type].action
                    if (
                        e.action.p_percent != prior.p_percent
                        or e.action.anchor_hp != prior.anchor_hp
                        or e.action.public_training_level != prior.public_training_level
                    ):
                        raise ValueError("entry_projection_changed_past_risk_anchor")
                parsed[sid] = Menu(sid, original.source_turn, original.source_calendar, effects)
            owned_key = tuple(owned)
            menu_tuple = tuple(parsed[m.source_id] for m in base)
            self.owned_menus[owned_key] = menu_tuple
            for m in menu_tuple:
                self._owned_menu_index[(owned_key, m.source_id)] = m
        if not targets_needed <= set(self.owned_menus):
            raise ValueError("entry_paid_or_hold_owned_menu_missing")
        self.entry_policy = policy
        self.late_joint = late
        self.first_entry_adaptive = entry == 36 and policy.get("selection_mode") == "adaptive_menu_remaining_h"
        if late and any(
            e.action.kind == "ss" and e.action.allowed and e.action.p_percent != 0
            for e in (*self.root, *((self.held,) if self.held else ()), *((self.generated,) if self.generated else ()))
        ):
            raise ValueError("late_joint_SS_win_counter_requires_known_all5_success")
        wins = self.r["state"].get("ss_wins") if late else None
        if wins is not None and (type(wins) is not int or wins < 0):
            raise ValueError("known_SS_wins_must_be_nonnegative_integer")
        self.initial = replace(self.initial, owned_levels=self.owned, ss_wins=wins)

    def prepare_entry(self, node, index):
        if self.late_joint or self.first_entry_adaptive:
            return node
        if self.entry_policy is None or node.entry_applied or self.days[index] < 36:
            return node
        if self.days[index] != 36:
            raise ValueError("entry_payment_missed_first_overseas_main")
        plan = next(p for p in self.entry_policy["alternatives"] if p["cost"] <= node.cash)
        return self.entry_paid_node(node, plan)

    @staticmethod
    def entry_paid_node(node, plan):
        return replace(
            node,
            cash=node.cash - plan["cost"],
            owned_levels=tuple(plan["targets"]),
            entry_applied=True,
            entry_plan_id=plan["plan_id"],
            entry_cost=plan["cost"],
            entry_cash_before=node.cash,
        )

    def pending_first_entry(self, node, index):
        return self.first_entry_adaptive and self.days[index] == 36 and node is not None and not node.entry_applied

    def first_entry_quotes(self, node, index):
        plans = self.entry_policy["alternatives"]
        payable = tuple(i for i, p in enumerate(plans) if p["cost"] <= node.cash)
        physical = replace(node.physical, pt=0.0)
        key = (physical, node.owned_levels, node.mei_index, index, payable)
        if key in self.entry_choices:
            self.first_entry_stats["cache_hits"] += 1
            return self.entry_choices[key]
        self.first_entry_stats["calls"] += 1
        quote_node = replace(node, physical=physical)
        proposals = {menu.source_id: [] for menu in self.menus["overseas"]}
        diagnostics = []
        for i, plan in enumerate(plans):
            cost = plan["cost"]
            row = {"plan_id": plan["plan_id"], "cost": cost}
            diagnostics.append(row)
            if i not in payable:
                row["status"] = "unaffordable_before_first_course"
                continue
            paid = self.entry_paid_node(quote_node, plan)
            menus = self.owned_menus[paid.owned_levels]
            courses = [self.available(paid, index, menu) for menu in menus]
            missing = [menu.source_id for menu, choices in zip(menus, courses) if not choices]
            if missing:
                row["unpriced_past_sources"] = missing
                skipped = self.first_entry_stats["unpriced_baskets"]
                skipped[plan["plan_id"]] = skipped.get(plan["plan_id"], 0) + len(missing)
            row.update(
                status="priced" if len(missing) < len(menus) else "no_priceable_current_course",
                menus_priced=len(menus) - len(missing),
            )
            self.first_entry_stats["Q_pairs"] += sum(map(len, courses))
            for menu, choices in zip(menus, courses):
                if choices:
                    winner = max(
                        ((self.entry_action_value(paid, index, e), e.action.action_id, e) for e in choices),
                        key=lambda item: item[:2],
                    )
                    score, aid, effect = winner
                    proposals[menu.source_id].append((score, -cost, aid, plan, effect))
        if any(not choices for choices in proposals.values()):
            raise ValueError("first_entry_past_menu_has_no_payable_basket_and_course")
        best = {source_id: max(choices, key=lambda item: item[:3]) for source_id, choices in proposals.items()}
        self.entry_choices[key] = best
        if len(self.first_entry_examples) < 8:
            pt_value = self.valuation.pt_factor * node.physical.pt
            self.first_entry_examples.append(
                {
                    "HP_points": node.physical.hp,
                    "cash_before": node.cash,
                    "selected_by_menu": [
                        {
                            "source_id": source_id,
                            "plan_id": plan["plan_id"],
                            "action_id": aid,
                            "cash_after_payment": node.cash - plan["cost"],
                            "score": score + pt_value,
                        }
                        for source_id, (score, _, aid, plan, _) in best.items()
                    ],
                    "baskets": diagnostics,
                }
            )
        return best

    def first_entry_value(self, node, index):
        quotes = self.first_entry_quotes(node, index)
        return fsum(q[0] for q in quotes.values()) / len(quotes) + self.valuation.pt_factor * node.physical.pt

    @staticmethod
    def phase(day):
        return "overseas" if CAL.is_overseas(day) else "domestic"

    def tick(self):
        self.ops += 1
        if self.ops > self.max_ops:
            raise K.BudgetExceeded()

    def _future_rest(self, key):
        if key not in self._parsed_rest:
            rest = (self.r.get("future_recovery") or {}).get(key)
            self._parsed_rest[key] = _parse_effect(rest, self.r) if rest is not None else None
        return self._parsed_rest[key]

    @staticmethod
    def _with_future_risk(e, p):
        a = e.action
        act = K.Action(
            a.action_id, a.kind, a.raw_gain, a.pt, a.hp_delta, a.cap_delta,
            p, a.training_type, a.anchor_hp, a.public_training_level,
            a.allowed, a.rejected_reason, a.baseline_score, a.residual, a.assumptions,
        )
        return Effect(act, e.outcomes, e.income, e.cost, 0, e.mei_stage, (), (), e.source_basis)

    def physical(self, s, a):
        max_cap = max(0.0, 120.0 - s.cap)
        if a.cap_delta <= max_cap:
            return K.success(s, a)
        return K.success(s, K.Action(
            a.action_id, a.kind, a.raw_gain, a.pt, a.hp_delta, max_cap,
            a.p_percent, a.training_type, a.anchor_hp, a.public_training_level,
            a.allowed, a.rejected_reason, a.baseline_score, a.residual, a.assumptions,
        ))

    def apply_event(self, node, event):
        s = node.physical
        gain = tuple(event.get("stats_gain", [0.0] * 5))
        if event.get("stats_unit", "raw") == "display":
            gain = tuple(K.raw_for_display_gain(K.display(x), g) for x, g in zip(s.raw, gain))
        s = self.physical(
            s,
            K.Action(
                "fixed:" + str(event.get("label")),
                "scheduled",
                gain,
                event.get("Pt", 0.0),
                event.get("HP_points", 0.0),
                event.get("capacity_points", 0.0),
            ),
        )
        return replace(node, physical=s, cash=node.cash + (event.get("cash_income_points", 0.0) or 0.0))

    def scheduled(self, node, after, through, *, include_future_after_main=False):
        for event in sorted(self.events, key=lambda e: e["calendar"]):
            if after < event["calendar"] <= through and (
                include_future_after_main or event.get("application_phase", "between_free_actions") == "between_free_actions"
            ):
                node = self.apply_event(node, event)
        return node

    def after_main(self, node, day):
        for event in self.events:
            if event["calendar"] == day and event.get("application_phase") == "after_main_action":
                node = self.apply_event(node, event)
        return node

    def terminal_value(self, node, after):
        last = self.scheduled(node, after, self.stop, include_future_after_main=True)
        bonus = self.completion_bonus
        initial_completed = self.mei.get("completed") if self.mei.get("unlocked") is True else 0
        if (
            bonus is not None
            and type(initial_completed) is int
            and initial_completed < 5
            and initial_completed + node.mei_index >= 5
            and after < bonus["calendar"] <= self.stop
        ):
            gain = tuple(bonus["stats_gain"])
            if bonus["stats_unit"] == "display":
                gain = tuple(K.raw_for_display_gain(K.display(x), g) for x, g in zip(last.physical.raw, gain))
            last = replace(
                last,
                physical=self.physical(last.physical, K.Action("terminal:Mei_completed_delta", "scheduled", gain, bonus["Pt"])),
            )
        value = K.utility(last.physical, self.valuation)
        coefficient = self.terminal.get("legacy_coefficient")
        if self.stop < 65 and num(coefficient):
            bar = last.physical.hp * 100 / last.physical.cap
            value += coefficient * (2 * bar - 0.008 * bar * bar)
        return value, last

    def ss_effect(self, node):
        if node.ready_lower < 5:
            return None
        return self.held if node.held_offer_left else self.generated

    def available(self, node, index, menu=None):
        day = self.days[index]
        domestic = not CAL.is_overseas(day)
        limit = self.valuation.failure_limit
        due = node.ready_lower >= 5 and self.limit is not None and day in self.debt_days and node.deferred >= self.limit
        if due and self.soft_ss_due and (not domestic or self.ss_effect(node) is None):
            # The obligation cannot be priced on this branch (no held/generated SS effect).
            self.soft_due_relaxed += 1
            due = False
        first_adapt = self.first_entry_adaptive and day == 36 and node.entry_applied
        if index == 0:
            eligible = []
            for e in self.root:
                a = e.action
                if not a.allowed or a.p_percent > limit or e.cost > node.cash:
                    continue
                if first_adapt and not self.coupled_entry(e, self.entry_policy["entry_unlocked_levels"], node.owned_levels):
                    continue
                if a.kind == "ss" and (not domestic or self.ss_effect(node) is None):
                    continue
                if a.kind == "mei_outing" and (
                    not domestic or node.mei_index >= len(self.outings) or e.mei_stage != self.outings[node.mei_index].mei_stage
                ):
                    continue
                if due and a.kind != "ss":
                    continue
                eligible.append(e)
            return eligible
        eligible = []
        hp = node.physical.hp
        cash = node.cash
        unlocked = self.entry_policy["entry_unlocked_levels"] if first_adapt else None
        if not due:
            for e in menu.effects:
                a = e.action
                if not a.allowed or e.cost or e.cost > cash:
                    continue
                if first_adapt and not self.coupled_entry(e, unlocked, node.owned_levels):
                    continue
                p = K.future_risk(a, hp)
                if p is not None and p <= limit:
                    eligible.append(self._with_future_risk(e, p))
        if domestic:
            offer = self.ss_effect(node)
            if offer is not None:
                a = offer.action
                if a.allowed and a.p_percent <= limit and not offer.cost and offer.cost <= cash:
                    if not first_adapt or self.coupled_entry(offer, unlocked, node.owned_levels):
                        eligible.append(offer)
            mei_available = node.mei_index < len(self.outings)
            if mei_available and not due:
                m = self.outings[node.mei_index]
                a = m.action
                if a.allowed and a.p_percent <= limit and not m.cost and m.cost <= cash:
                    if not first_adapt or self.coupled_entry(m, unlocked, node.owned_levels):
                        eligible.append(m)
        else:
            mei_available = False
        recovery = self.r.get("future_recovery") or {}
        if not mei_available or recovery.get("allow_rest_with_Mei") is True:
            rest = self._future_rest("domestic" if domestic else "overseas")
            if rest is not None and (not due or rest.action.kind == "ss"):
                a = rest.action
                if a.allowed and a.p_percent <= limit and not rest.cost and rest.cost <= cash:
                    if (a.kind != "ss" or (domestic and self.ss_effect(node) is not None)) and (
                        not first_adapt or self.coupled_entry(rest, unlocked, node.owned_levels)
                    ):
                        eligible.append(rest)
        return eligible

    def transitions(self, node, e, index):
        a = e.action
        debt = node.deferred
        ready = node.ready_lower
        held = node.held_offer_left
        mei = node.mei_index
        if a.kind == "ss":
            ready = max(0, ready - 5)
            held = False
            debt = 0
        elif a.kind == "mei_outing":
            mei += 1
        if a.kind != "ss" and node.ready_lower >= 5 and self.days[index] in self.debt_days:
            debt += 1
        p = a.p_percent / 100.0
        day = self.days[index]
        out = []
        base_ready = min(15, ready)
        base_debt = 0 if (node.ready_lower < 5 and base_ready >= 5) else debt
        base_ss_wins = node.ss_wins
        for prob, ok in ((1 - p, True), (p, False)):
            if prob <= 0:
                continue
            outcomes = e.outcomes if ok else ((1.0, 0.0, 0.0),)
            for weight, hp, cap in outcomes:
                if weight <= 0:
                    continue
                if hp == a.hp_delta and cap == a.cap_delta:
                    act = a
                else:
                    act = K.Action(
                        a.action_id, a.kind, a.raw_gain, a.pt, hp, cap,
                        a.p_percent, a.training_type, a.anchor_hp, a.public_training_level,
                        a.allowed, a.rejected_reason, a.baseline_score, a.residual, a.assumptions,
                    )
                if ok:
                    physical = self.physical(node.physical, act)
                    nxtready = ready + (e.newly_full if a.kind == "train" and index == 0 else 0)
                    nxtready = 15 if nxtready > 15 else nxtready
                    nextdebt = 0 if (node.ready_lower < 5 and nxtready >= 5) else debt
                    cash = node.cash - e.cost + (e.income if e.income is not None else 0.0)
                    next_ss_wins = base_ss_wins + 5 if a.kind == "ss" and base_ss_wins is not None else base_ss_wins
                else:
                    physical = K.failed(node.physical, act)
                    nxtready = base_ready
                    nextdebt = base_debt
                    cash = node.cash - e.cost
                    next_ss_wins = base_ss_wins
                nxt_node = Node(
                    physical, cash, nxtready, held, mei, nextdebt,
                    node.owned_levels, node.entry_applied, node.entry_plan_id,
                    node.entry_cost, node.entry_cash_before, next_ss_wins,
                )
                out.append((prob * weight, self.after_main(nxt_node, day), ok))
        return out

    def expected_immediate(self, node, e, index):
        self.tick()
        day = self.days[index]
        return (
            sum(prob * self.terminal_value(nxt, day)[0] for prob, nxt, _ in self.transitions(node, e, index))
            - self.valuation.risk_aversion_per_percent * e.action.p_percent
        )

    def frontier_tick(self):
        self.frontier_ops += 1
        self.tick()

    def frontier_hp_credit(self, hp, cap):
        coefficient = self.terminal.get("legacy_coefficient")
        if self.stop >= 65 or not num(coefficient):
            return 0.0
        bar = hp * 100 / cap
        return coefficient * (2 * bar - 0.008 * bar * bar)

    def entry_band_adjustment(self, node):
        if self.entry_band_calendar is None:
            return 0.0
        from core.auto_trainer.larc_strategy_rules import entry_band_adjustment_from_contract

        return entry_band_adjustment_from_contract(node.physical.hp * 100.0 / node.physical.cap, self.entry_energy_contract)

    @staticmethod
    def frontier_hp_effect(hp, cap, delta, cap_delta=0.0):
        cap = max(1.0, min(120.0, cap + cap_delta))
        return max(0.0, min(cap, hp + delta)), cap

    def frontier_fixed_hp(self, hp, cap, after, through, *, post_main=False, terminal=False):
        for event in sorted(self.events, key=lambda x: x["calendar"]):
            phase = event.get("application_phase", "between_free_actions")
            match = (
                (event["calendar"] == after and phase == "after_main_action")
                if post_main
                else (after < event["calendar"] <= through and (terminal or phase == "between_free_actions"))
            )
            if match:
                hp, cap = self.frontier_hp_effect(hp, cap, event.get("HP_points", 0.0), event.get("capacity_points", 0.0))
        return hp, cap

    def frontier_reward(self, action):
        key = (action.raw_gain, action.pt)
        if key not in self.frontier_rewards:
            self.frontier_tick()
            self.frontier_rewards[key] = (
                K.utility(K.success(self.frontier_anchor, replace(action, hp_delta=0.0, cap_delta=0.0)), self.valuation)
                - self.frontier_anchor_utility
            )
        return self.frontier_rewards[key]

    def frontier_menus(self, index, owned):
        if not CAL.is_overseas(self.days[index]):
            raise ValueError("frontier_domestic_inventory_not_modeled")
        return self.owned_menus[owned] if self.entry_policy is not None else self.menus["overseas"]

    def frontier_table(self, index, cap, owned):
        key = (index, cap, owned)
        if key in self.frontier_tables:
            return self.frontier_tables[key]
        grid = tuple(float(h) for h in range(0, int(cap) // 5 * 5 + 1, 5))
        if not grid or grid[-1] != cap:
            grid = (*grid, float(cap))
        menus = self.frontier_menus(index, owned)
        day = self.days[index]
        rest = self._future_rest("overseas")
        values = []
        training_values = []
        for hp in grid:
            menu_values = []
            menu_training_counts = []
            for menu in menus:
                options = (*menu.effects, *((rest,) if rest is not None else ()))
                best = float("-inf")
                best_training_count = None
                for effect in options:
                    a = effect.action
                    if a.kind not in ("train", "rest") or not a.allowed or effect.cost:
                        continue
                    p = K.future_risk(a, hp)
                    if p is None or p > self.valuation.failure_limit:
                        continue
                    self.frontier_tick()
                    value = 0.0
                    expected_later_training = 0.0
                    reward = self.frontier_reward(a)
                    for probability, ok in ((1 - p / 100.0, True), (p / 100.0, False)):
                        if probability <= 0:
                            continue
                        outcomes = effect.outcomes if ok else ((1.0, 0.0, 0.0),)
                        for weight, delta, dcap in outcomes:
                            if weight <= 0:
                                continue
                            self.frontier_tick()
                            nh, nc = self.frontier_hp_effect(hp, cap, delta, dcap)
                            nh, nc = self.frontier_fixed_hp(nh, nc, day, day, post_main=True)
                            if index + 1 < len(self.days):
                                nh, nc = self.frontier_fixed_hp(nh, nc, day, self.days[index + 1] - 1)
                                future = self.frontier_value(index + 1, nh, nc, owned)
                                count = self.frontier_training_value(index + 1, nh, nc, owned) if self.late_joint else 0.0
                            else:
                                nh, nc = self.frontier_fixed_hp(nh, nc, day, self.stop, terminal=True)
                                future = self.frontier_hp_credit(nh, nc)
                                count = 0.0
                            value += probability * weight * ((reward if ok else 0.0) + future)
                            if self.late_joint:
                                expected_later_training += probability * weight * count
                    value -= self.valuation.risk_aversion_per_percent * p
                    if self.late_joint and value > best:
                        best_training_count = (1.0 if a.kind == "train" else 0.0) + expected_later_training
                    best = max(best, value)
                menu_values.append(best)
                if self.late_joint:
                    menu_training_counts.append(best_training_count)
            values.append(sum(menu_values) / len(menu_values))
            if self.late_joint:
                training_values.append(
                    sum(menu_training_counts) / len(menu_training_counts)
                    if all(num(c) for c in menu_training_counts)
                    else float("nan")
                )
        self.frontier_tables[key] = (grid, tuple(values))
        if self.late_joint:
            self.frontier_training_tables[key] = (grid, tuple(training_values))
        return self.frontier_tables[key]

    def frontier_value(self, index, hp, cap, owned):
        grid, values = self.frontier_table(index, cap, owned)
        at = max(0, bisect_right(grid, hp + 1e-9) - 1)
        return values[at]

    def frontier_training_value(self, index, hp, cap, owned):
        self.frontier_table(index, cap, owned)
        grid, values = self.frontier_training_tables[(index, cap, owned)]
        at = max(0, bisect_right(grid, hp + 1e-9) - 1)
        value = values[at]
        if not num(value):
            raise ValueError("frontier_has_no_feasible_training_count_policy")
        return value

    def finance_training_share(self, paid, e, index):
        remaining = len(self.days) - index - 1
        if remaining == 0:
            return 0.0, {"expected_training_attempts": 0.0, "remaining_actions": 0, "share": 0.0}
        count = 0.0
        for probability, nxt, _ in self.transitions(paid, e, index):
            before = self.scheduled(nxt, self.days[index], self.days[index + 1] - 1)
            count += probability * self.frontier_training_value(
                index + 1, before.physical.hp, before.physical.cap, paid.owned_levels
            )
        share = count / remaining
        return share, {
            "expected_training_attempts": count,
            "remaining_actions": remaining,
            "share": share,
            "basis": "same_maximizing_H_policy_same_probability_and_floor_grid; current_course_excluded; attempts_not_successes",
            "not_actual_future_or_guaranteed_income": True,
        }

    def expected_with_frontier(self, node, e, index):
        if not self.frontier_enabled or index + 1 >= len(self.days):
            return self.expected_immediate(node, e, index)
        self.tick()
        day = self.days[index]
        value = 0.0
        for probability, nxt, _ in self.transitions(node, e, index):
            before = self.scheduled(nxt, day, self.days[index + 1] - 1)
            band_adj = self.entry_band_adjustment(before) if self.days[index + 1] == self.entry_band_calendar else 0.0
            if self.pending_first_entry(before, index + 1):
                value += probability * (self.first_entry_value(before, index + 1) + band_adj)
                continue
            before = self.prepare_entry(before, index + 1)
            if self.pending_late_entry(before, index + 1):
                future = sum(self.choose_entry(before, index + 1, m.source_id)["score"] for m in self.menus["overseas"]) / len(
                    self.menus["overseas"]
                )
                value += probability * (future + band_adj)
                continue
            terminal, last = self.terminal_value(nxt, day)
            owned = before.owned_levels if self.entry_policy is not None else self.owned
            future = self.frontier_value(index + 1, before.physical.hp, before.physical.cap, owned)
            if not num(future):
                raise ValueError("frontier_has_no_feasible_foreign_continuation")
            value += probability * (terminal - self.frontier_hp_credit(last.physical.hp, last.physical.cap) + future + band_adj)
        return value - self.valuation.risk_aversion_per_percent * e.action.p_percent

    def menus_for(self, index, node=None):
        if self.pending_late_entry(node, index) or self.pending_first_entry(node, index):
            return self.menus["overseas"]
        if self.entry_policy is not None and self.phase(self.days[index]) == "overseas":
            if node is None or not node.entry_applied:
                raise ValueError("entry_menus_requested_before_branch_payment")
            return self.owned_menus[node.owned_levels]
        return self.menus[self.phase(self.days[index])]

    def pending_late_entry(self, node, index):
        return self.late_joint and self.days[index] == 60 and node is not None and not node.entry_applied

    def entry_action_value(self, node, index, e):
        memo = self.numeric_memo_enabled
        key = ("entry_one_course_H", node.physical, node.owned_levels, node.mei_index, index, e)
        if memo and key in self.post_paid_numeric_cache:
            self.post_paid_numeric_hits += 1
            return self.post_paid_numeric_cache[key]
        value = self.expected_with_frontier(node, e, index)
        if memo:
            self.post_paid_numeric_cache[key] = value
        return value

    def choice_value(self, node, index, e):
        memo = self.late_joint and self.numeric_memo_enabled and node.entry_applied and self.days[index] >= 60
        key = ("online_two_step", node.physical, node.owned_levels, node.mei_index, index, e) if memo else None
        if memo and key in self.post_paid_numeric_cache:
            self.post_paid_numeric_hits += 1
            return self.post_paid_numeric_cache[key]
        exact_key = (node, index, e) if self.numeric_memo_enabled else None
        if exact_key is not None and exact_key in self._choice_value_cache:
            return self._choice_value_cache[exact_key]
        value = self.uncached_choice_value(node, index, e)
        if memo:
            self.post_paid_numeric_cache[key] = value
        if exact_key is not None:
            self._choice_value_cache[exact_key] = value
        return value

    def uncached_choice_value(self, node, index, e):
        self.tick()
        if index == len(self.days) - 1:
            return self.expected_immediate(node, e, index)
        total = 0.0
        for prob, nxt, _ in self.transitions(node, e, index):
            nxt = self.scheduled(nxt, self.days[index], self.days[index + 1] - 1)
            band_adj = self.entry_band_adjustment(nxt) if self.days[index + 1] == self.entry_band_calendar else 0.0
            if self.pending_first_entry(nxt, index + 1):
                total += prob * (self.first_entry_value(nxt, index + 1) + band_adj)
                continue
            nxt = self.prepare_entry(nxt, index + 1)
            if self.pending_late_entry(nxt, index + 1):
                values = [self.choose_entry(nxt, index + 1, m.source_id)["score"] for m in self.menus["overseas"]]
            else:
                values = []
                for following in self.menus_for(index + 1, nxt):
                    choices = self.available(nxt, index + 1, following)
                    if not choices:
                        values.append(float("-inf"))
                    else:
                        values.append(max(self.expected_with_frontier(nxt, b, index + 1) for b in choices))
            total += prob * (sum(values) / len(values) + band_adj)
        return total - self.valuation.risk_aversion_per_percent * e.action.p_percent

    def choose(self, node, index, menu):
        if self.pending_late_entry(node, index):
            raise ValueError("joint_entry_requires_paid_node_and_action_together")
        key = (node, index, menu.source_id)
        if key in self.cache:
            return self.cache[key]
        options = self.available(node, index, menu)
        if not options and self.soft_ss_due and index > 0:
            rest = self._future_rest("domestic" if not CAL.is_overseas(self.days[index]) else "overseas")
            if rest is not None:
                self.soft_rest_fallbacks += 1
                options = [rest]
        if not options:
            raise ValueError("explicit_obligation_or_eligibility_has_no_priced_future_action")
        answer = max(options, key=lambda e: (self.choice_value(node, index, e), e.action.action_id))
        self.cache[key] = answer
        return answer

    @staticmethod
    def coupled_entry(effect, before, after):
        changed = [i for i in range(5) if after[i] > before[i]]
        return not changed or (
            len(changed) == 1 and effect.action.kind == "train" and (3, 1, 2, 0, 4)[effect.action.training_type] == changed[0]
        )

    def representative_after_entry(self, paid, e, index, source_id):
        branches = self.transitions(paid, e, index)
        node = branches[0][1]
        previous = self.days[index]
        route = []
        order = [m.source_id for m in self.menus["overseas"]]
        start = order.index(source_id)
        for k in range(index + 1, len(self.days)):
            day = self.days[k]
            node = self.scheduled(node, previous, day - 1)
            menu = self.menus_for(k, node)[(start + k - index) % len(order)]
            action = self.choose(node, k, menu)
            outcomes = self.transitions(node, action, k)
            u = (0.125 + k * 0.38196601125) % 1.0
            cumulative = 0.0
            chosen = outcomes[-1]
            for branch in outcomes:
                cumulative += branch[0]
                if u < cumulative:
                    chosen = branch
                    break
            before = node
            node = chosen[1]
            route.append(self.step_readout(before, node, action, day, chosen[2], menu.source_id))
            previous = day
        share = sum(s["kind"] == "train" for s in route) / len(route) if route else 0.0
        return share, route

    def choose_entry(self, node, index, source_id):
        if not self.pending_late_entry(node, index):
            raise ValueError("joint_entry_only_once_at60_before_first_main")
        key = (node, index, source_id)
        if key in self.entry_choices:
            self.entry_joint_stats["cache_hits"] += 1
            return self.entry_choices[key]
        self.entry_joint_stats["calls"] += 1
        proposals = []
        for plan in self.entry_policy["alternatives"]:
            cost = plan["cost"]
            if cost > node.cash:
                continue
            owned = tuple(plan["targets"])
            paid = Node(
                node.physical, node.cash - cost, node.ready_lower, node.held_offer_left,
                node.mei_index, node.deferred, owned, True, plan["plan_id"], cost, node.cash, node.ss_wins,
            )
            menu = self._owned_menu_index.get((owned, source_id)) or next(
                m for m in self.owned_menus[owned] if m.source_id == source_id
            )
            changed = [i for i in range(5) if owned[i] > node.owned_levels[i]]
            if not changed:
                effects = self.available(paid, index, menu)
            elif len(changed) == 1:
                target_slot = changed[0]
                limit = self.valuation.failure_limit
                hp = paid.physical.hp
                effects = []
                for e in menu.effects:
                    a = e.action
                    if (3, 1, 2, 0, 4)[a.training_type] != target_slot or not a.allowed or e.cost:
                        continue
                    p = K.future_risk(a, hp)
                    if p is not None and p <= limit:
                        effects.append(self._with_future_risk(e, p))
            else:
                effects = ()
            for effect in effects:
                self.entry_joint_stats["Q_pairs"] += 1
                q = self.entry_action_value(paid, index, effect)
                proposals.append((q, cost, effect.action.action_id, plan, paid, effect))
        proposals.sort(key=lambda p: p[2], reverse=True)
        proposals.sort(key=lambda p: (-p[0], p[1]))
        financial = []
        for q, cost, aid, plan, paid, effect in proposals:
            if cost == 0:
                share = None
                count_info = None
                quote = {"status": "empty_basket_no_financial_gate", "required_post_basket_reserve": None}
                reserve = None
                funded = True
            else:
                share, count_info = self.finance_training_share(paid, effect, index)
                context = {
                    "levels_after": list(paid.owned_levels),
                    "cash_before": node.cash,
                    "basket_cost": cost,
                    "current_conditional_income": effect.income,
                    "p_percent": effect.action.p_percent,
                    "future_training_share": share,
                    "ss_wins": node.ss_wins,
                }
                self.entry_joint_stats["finance_checks"] += 1
                try:
                    quote = self.reserve_policy(context)
                except Exception as exc:
                    quote = {"status": "unavailable", "required_post_basket_reserve": None, "error_type": type(exc).__name__}
                reserve = quote.get("required_post_basket_reserve") if isinstance(quote, dict) else None
                funded = num(reserve) and paid.cash >= reserve
            row = {
                "plan_id": plan["plan_id"],
                "action_id": aid,
                "Q": q,
                "entry_score_mode": "one_course_plus_remaining_R4_H_absolute",
                "basket_cost": cost,
                "cash_before": node.cash,
                "cash_after_payment": paid.cash,
                "future_training_share": share,
                "H_training_expectation": count_info,
                "current_income_conditional_not_payable_cash": effect.income,
                "SS_wins_known_or_planned": node.ss_wins,
                "finance": quote,
                "allowed_by_reserve_gate": funded,
                "empty_basket_bypass": cost == 0,
            }
            financial.append(row)
            if funded:
                result = {
                    "score": q,
                    "paid_node": paid,
                    "effect": effect,
                    "plan": plan,
                    "finance_row": row,
                    "finance_H_training_expectation": count_info,
                    "financial_rows_checked": financial,
                }
                self.entry_choices[key] = result
                self.entry_finance_by_paid[(paid, index, source_id, aid)] = row
                if len(self.entry_joint_examples) < 8:
                    self.entry_joint_examples.append({"source_id": source_id, **row})
                return result
        raise ValueError("joint_entry_no_cash_and_reserve_supported_plan")

    def rollout(self, node, path, stratum):
        route = []
        penalty = 0.0
        previous = self.days[0]
        for index in range(1, len(self.days)):
            day = self.days[index]
            node = self.scheduled(node, previous, day - 1)
            if day == self.entry_band_calendar:
                penalty -= self.entry_band_adjustment(node)
            node = self.prepare_entry(node, index)
            menus = self.menus_for(index, node)
            menu = menus[(path + index - 1) % len(menus)]
            if self.pending_first_entry(node, index):
                _, _, _, plan, e = self.first_entry_quotes(node, index)[menu.source_id]
                node = self.entry_paid_node(node, plan)
            elif self.pending_late_entry(node, index):
                decision = self.choose_entry(node, index, menu.source_id)
                node = decision["paid_node"]
                e = decision["effect"]
            else:
                e = self.choose(node, index, menu)
            branches = self.transitions(node, e, index)
            u = ((stratum + 0.5) / 4.0 + index * 0.38196601125) % 1.0
            cumulative = 0.0
            chosen = branches[-1]
            for branch in branches:
                cumulative += branch[0]
                if u < cumulative:
                    chosen = branch
                    break
            before = node
            node = chosen[1]
            penalty += self.valuation.risk_aversion_per_percent * e.action.p_percent
            route.append(self.step_readout(before, node, e, day, chosen[2], menu.source_id))
            previous = day
        value, last = self.terminal_value(node, previous)
        return value - penalty, route, last

    def residual(self, e):
        allowed = K.RESIDUAL_ALLOW | {"mood_recovery"}
        if (self.r.get("future_recovery") or {}).get("allow_rest_with_Mei") is True:
            allowed = allowed | {"mei_recovery_preference", "weak_training_two_step_adjustment"}
        values = dict(e.components)
        drop = {}
        generated_modeled = self.generated is not None and self.initial.ready_lower < 5 and self.initial.ready_lower + e.newly_full >= 5
        if generated_modeled:
            fraction = min(e.newly_full, 5 - self.initial.ready_lower) / max(1, e.newly_full)
            requested = dict(e.proxy_fractions)
            requested.setdefault("newly_full", fraction)
            requested.setdefault("ss_preparation", 1.0)
            drop = {key: values.get(key, 0.0) * frac for key, frac in requested.items()}
        return e.action.residual + sum(v for k, v in values.items() if k in allowed) - sum(drop.values()), drop, generated_modeled

    def step_readout(self, before, after, e, day, ok, menu):
        result = {
            "calendar": day,
            "action_id": e.action.action_id,
            "kind": e.action.kind,
            "menu": menu,
            "success_scenario": ok,
            "HP_before": before.physical.hp,
            "HP_after": after.physical.hp,
            "capacity_after": after.physical.cap,
            "cash_before": before.cash,
            "current_cost": e.cost,
            "conditional_income": e.income,
            "cash_after": after.cash,
            "ready_before": before.ready_lower,
            "ready_after": after.ready_lower,
            "held_offer_left": after.held_offer_left,
            "ss_consumed": e.action.kind == "ss",
            "current_SS_token_consumed": e.action.kind == "ss" and before.held_offer_left,
            "current_SS_token_id": self.ss.get("token_id") if e.action.kind == "ss" and before.held_offer_left else None,
            "past_prior_qualified_SS_consumed": e.action.kind == "ss" and not before.held_offer_left,
            "Mei_stage": e.mei_stage,
            "Mei_index_after": after.mei_index,
            "deferred_after": after.deferred,
            "risk_percent": e.action.p_percent,
        }
        if self.entry_policy is not None:
            result.update(owned_levels_after=after.owned_levels, entry_policy_applied=after.entry_applied)
            if day == self.entry_policy["calendar"] and before.entry_applied:
                result["entry_forecast_purchase_before_main"] = {
                    "plan_id": before.entry_plan_id,
                    "cost": before.entry_cost,
                    "cash_before_payment": before.entry_cash_before,
                    "cash_after_payment": before.entry_cash_before - before.entry_cost,
                    "target_levels": before.owned_levels,
                    "main_actions_consumed": 0,
                    "purchase_verified": False,
                }
        if self.late_joint:
            result["ss_wins_known_or_planned_after"] = after.ss_wins
            if day == 60:
                finance = self.entry_finance_by_paid.get((before, self.days.index(day), menu, e.action.action_id))
                if finance is not None:
                    result["entry_finance_forecast"] = finance
        return result

    def _forced_ss_entry_hp_delta(self, e):
        if (
            self.generated is not None
            or self.limit != 0
            or len(self.days) < 3
            or self.days[0] != 34
            or self.days[1] != 35
            or self.days[1] not in self.debt_days
            or not CAL.is_overseas(self.days[2])
            or self.entry_band_calendar != 36
            or self.entry_energy_contract.get("active") is not True
            or e.action.kind != "train"
            or e.action.hp_delta >= 0
            or not (self.initial.ready_lower < 5 and self.initial.ready_lower + e.newly_full >= 5)
        ):
            return 0.0
        cap = max(1.0, float(self.initial.physical.cap))
        after_hp_pct = max(0.0, min(cap, float(self.initial.physical.hp) + float(e.action.hp_delta))) * 100.0 / cap
        band_low = float(self.entry_energy_contract.get("band_low", 88.0))
        if after_hp_pct >= band_low:
            return 0.0
        from core.auto_trainer.larc_strategy_rules import entry_band_adjustment_from_contract

        effective_entry_hp = min(100.0, after_hp_pct + 5.0)
        optimistic_entry_hp = min(100.0, after_hp_pct + 45.0)
        real_adj = entry_band_adjustment_from_contract(effective_entry_hp, self.entry_energy_contract)
        opt_adj = entry_band_adjustment_from_contract(optimistic_entry_hp, self.entry_energy_contract)
        if real_adj >= opt_adj:
            return 0.0
        return round((real_adj - opt_adj) * (1.0 - e.action.p_percent / 100.0), 4)

    def rank(self):
        from core.auto_trainer.larc_strategy_extension import active_provider

        provider = active_provider()
        rows = []
        base_raw, base_last = self.terminal_value(self.after_main(self.initial, self.days[0]), self.days[0])
        base = (
            provider.planner_adjust_terminal_value(self, base_raw, base_last, base_last, K)
            if self._has_terminal_override
            else base_raw
        )
        paths = max(len(v) for v in self.menus.values())
        for e in self.available(self.initial, 0):
            mean = 0.0
            representative = None
            term = []
            for prob, nxt, ok in self.transitions(self.initial, e, 0):
                vals = []
                for path in range(paths):
                    for stratum in range(4):
                        v, route, last = self.rollout(nxt, path, stratum)
                        if self._has_terminal_override:
                            v = provider.planner_adjust_terminal_value(self, v, last, base_last, K)
                        vals.append(v)
                        term.append((prob / (paths * 4), last))
                        if representative is None:
                            representative = {
                                "first": self.step_readout(self.initial, nxt, e, self.days[0], ok, "current_visible"),
                                "future": route,
                                "last_state": self.node_readout(last),
                            }
                mean += prob * sum(vals) / len(vals)
            residual, drop, generated = self.residual(e)
            conditional_residual = residual * (1 - e.action.p_percent / 100 if e.action.kind in ("train", "ss") else 1.0)
            forced_ss_entry_hp_delta = self._forced_ss_entry_hp_delta(e)
            score = (
                mean
                - base
                + conditional_residual
                - self.valuation.risk_aversion_per_percent * e.action.p_percent
                + forced_ss_entry_hp_delta
            )
            meanstate = {
                "stats_display": [sum(w * K.display(n.physical.raw[i]) for w, n in term) for i in range(5)],
                "Pt": sum(w * n.physical.pt for w, n in term),
                "HP": sum(w * n.physical.hp for w, n in term),
                "cash": sum(w * n.cash for w, n in term),
            }
            rows.append(
                {
                    "action_id": e.action.action_id,
                    "score": score,
                    "original_baseline_score": e.action.baseline_score,
                    "expected_window_utility_delta": mean - base,
                    "current_residual_once": conditional_residual,
                    "current_risk_aversion": -self.valuation.risk_aversion_per_percent * e.action.p_percent,
                    **({"forced_ss_blocks_entry_recovery": forced_ss_entry_hp_delta} if forced_ss_entry_hp_delta != 0.0 else {}),
                    "modeled_proxy_removed": drop,
                    "generated_SS_reward_modeled_from_past_prior": generated,
                    "mean_window_end": meanstate,
                    "representative_not_forecast": representative,
                }
            )
        rows.sort(key=lambda r: (-r["score"], r["action_id"]))
        if not rows:
            raise ValueError("no_originally_eligible_priced_root_action")
        result = {
            "active": True,
            "schema_version": "larc.departure_route_result.v1",
            "feature": VERSION,
            "selected_action_id": rows[0]["action_id"],
            "first_action_only": True,
            "execution_authorized": False,
            "rankings": rows,
            "operations": self.ops,
            "cache_states": len(self.cache),
            "free_action_days": self.days,
            "equal_main_action_budget": len(self.days),
            "fixed_events_not_main_actions": True,
            "terminal_HP_covered": self.terminal_hp_covered,
            "terminal_HP_credit_zero_at_end65": self.stop >= 65,
            "terminal_Mei_completion_delta_contract": self.completion_bonus,
            "deadline_obligation": self.ss.get("deadline_obligation"),
            "new_global_full5_force_rule": False,
            "stock_contract": {
                "current_group_consumed_once": True,
                "generated_recharge_after_SS_modeled": False,
                "root_known_newly_full_qualification_only": True,
                "unpriced_new_full_does_not_become_fake_offer": True,
            },
            "future_policy": "263-style_online_two_step_over_strict_past_phase_menus_shared_quadrature",
            "future_layout_guaranteed": False,
            "next_turn_replan_required": True,
            "cash_policy": "current_cost_before_current_success_income; future_purchases_hold_input_owned_levels",
            "adaptation_points_not_converted_into_extra_attributes": True,
            "diagnostics": self.notes,
        }
        if self.entry_policy is not None:
            result["entry_investment_forecast"] = {
                "calendar": self.entry_policy["calendar"],
                "priority": [p["plan_id"] for p in self.entry_policy["alternatives"]],
                "basis": self.entry_policy["basis"],
                "cash_payment_before_first_overseas_main": True,
                "policy_chosen_by_caller": True,
                "real_purchase_execution_changed": False,
                "menus_by_owned_count": len(self.owned_menus),
            }
            result["cash_policy"] = "conditional_entry_debit_then_matching_owned_menus"
        if self.first_entry_adaptive:
            result["entry_investment_forecast"].update(
                selection_mode="adaptive_menu_remaining_h",
                comparison_basis="mean_of_each_current_menu_best_payable_basket_and_coupled_course_plus_remaining_H",
                same_past_menus_for_each_basket=True,
                first_arc_reserve_gate_added=False,
                statistics=dict(self.first_entry_stats),
                examples=list(self.first_entry_examples),
            )
        if self.late_joint:
            result["cash_policy"] = "visible_past_menu60_joint_basket_action_paid_once_then_hold; no_cash_linear_bonus"
            result["late_entry_joint"] = {
                "statistics": dict(self.entry_joint_stats),
                "examples_first8": self.entry_joint_examples,
                "same_entry_chooser_used_by_H_and_rollout": True,
                "entry_Q_is_absolute_no_added_terminal_or_F": True,
                "entry_score_mode": "one_course_plus_remaining_R4_H_absolute; later61plus_online_two_step_H",
                "reserve_share_mode": "same_H_policy_expected_remaining_training_attempt_fraction; empty_share_null",
                "post_paid_numeric_memo": {
                    "enabled": self.numeric_memo_enabled,
                    "hits": self.post_paid_numeric_hits,
                    "entries": len(self.post_paid_numeric_cache),
                },
                "conditional_slot8_debt_only_no_numeric_owned_upgrade": True,
                "actual_purchase_execution_changed": False,
            }
        if self.frontier_enabled:
            result["future_policy"] = "two_step_with_remaining_foreign_energy_frontier"
            result["remaining_energy_frontier"] = {
                "enabled": True,
                "grid_points": 5,
                "operations": self.frontier_ops,
                "tables": len(self.frontier_tables),
                "reward_anchor": "request_current_plus_declared_fixed_rewards",
                "risk_and_recovery": "unchanged_current_model_rules",
                "only_guides_future_online_choices": True,
                "extra_root_HP_or_frontier_credit": False,
                "terminal_HP_credit_replaced_not_added": True,
                "future_layout_guaranteed": False,
            }
        return result

    @staticmethod
    def node_readout(node):
        result = {
            "stats_raw_equivalent": node.physical.raw,
            "stats_display": [K.display(x) for x in node.physical.raw],
            "Pt": node.physical.pt,
            "HP": node.physical.hp,
            "capacity": node.physical.cap,
            "cash": node.cash,
            "ready_lower": node.ready_lower,
            "held_offer_left": node.held_offer_left,
            "Mei_index": node.mei_index,
            "deferred": node.deferred,
        }
        if node.owned_levels:
            result.update(
                owned_levels=node.owned_levels,
                entry_applied=node.entry_applied,
                entry_plan_id=node.entry_plan_id,
                entry_cost=node.entry_cost,
                entry_cash_before=node.entry_cash_before,
            )
        if node.ss_wins is not None:
            result["ss_wins_known_or_planned"] = node.ss_wins
        return result


def _rank_routes(request, *, max_ops=BASE_COMPUTATION_MAX_OPS, reserve_policy=None):
    start = perf_counter()
    planner = None
    try:
        planner = Planner(request, max_ops, reserve_policy=reserve_policy)
        result = planner.rank()
    except (K.BudgetExceeded, ValueError, KeyError, TypeError, IndexError) as exc:
        result = {
            "active": False,
            "feature": VERSION,
            "reason": "whole_pool_compute_budget_exceeded" if isinstance(exc, K.BudgetExceeded) else str(exc),
            "selected_action_id": None,
            "execution_authorized": False,
            "new_stop_required": False,
            "keep_original_263_or161": True,
        }
        if planner is not None and planner.frontier_enabled:
            result["remaining_energy_frontier"] = {
                "enabled": True,
                "operations": planner.frontier_ops,
                "tables": len(planner.frontier_tables),
                "whole_pool_fallback": True,
            }
    policy = request.get("entry_investment_policy") or {}
    if not result.get("active") and policy.get("calendar") == 60 and policy.get("selection_mode") == "joint_basket_action":
        result["fallback_policy"] = "frozen_r4_h_same_state"
        if planner is not None:
            result.update(
                operations=planner.ops,
                frontier_operations=planner.frontier_ops,
                entry_joint_statistics=dict(planner.entry_joint_stats),
            )
    result["strategy_id"] = STRATEGY_ID
    result["policy_version"] = POLICY_VERSION
    result["seconds"] = perf_counter() - start
    return result


def _late(request):
    policy = request.get("entry_investment_policy") or {}
    return policy.get("calendar") == 60 and policy.get("selection_mode") == "joint_basket_action"


def _same_current(request, base):
    if not isinstance(base, dict) or _late(base):
        return False
    fields = (
        "run_session_id",
        "turn_index",
        "calendar_ordinal",
        "period_status",
        "goal",
        "current_actions",
        "last_free_day",
        "settlement_through",
        "ss",
        "mei",
        "terminal_mei_completion_bonus",
        "terminal_energy_contract",
        "expedition_entry_energy_contract",
        "remaining_energy_frontier",
    )
    if any(request.get(key) != base.get(key) for key in fields):
        return False
    state_fields = ("raw_equivalent", "HP_points", "capacity_points", "Pt", "cash", "owned_levels", "caps_raw")
    a, b = request.get("state") or {}, base.get("state") or {}
    return all(a.get(key) == b.get(key) for key in state_fields)


def _stamp(result, effective_impl, **extra):
    output = dict(result)
    output.update(
        strategy_id=STRATEGY_ID,
        policy_version=POLICY_VERSION,
        effective_impl=effective_impl,
        execution_authorized=False,
        purchase_authorized=False,
        **extra,
    )
    return output


def evaluate_request(request, *, reserve_policy=None, base_request=None, max_ops=None, strategy_id=STRATEGY_ID):
    if strategy_id != STRATEGY_ID:
        return _stamp(
            {"active": False, "reason": "strategy_id_not_1.8_keep_registered_revision", "new_stop_required": False},
            None,
            requested_strategy_id=strategy_id,
        )
    late = _late(request)
    budget = max_ops if max_ops is not None else (LATE_COMPUTATION_MAX_OPS if late else BASE_COMPUTATION_MAX_OPS)
    primary = _rank_routes(request, max_ops=budget, reserve_policy=reserve_policy)
    if primary.get("active"):
        return _stamp(
            primary,
            "departure_joint_C" if late else "departure_joint_H",
            computation_budget=budget,
            used_H_fallback=False,
        )
    reason = primary.get("reason")
    if late and base_request is not None:
        if not _same_current(request, base_request):
            return _stamp(
                primary,
                None,
                computation_budget=budget,
                used_H_fallback=False,
                H_fallback_coverage="caller_base_current_binding_mismatch",
                delegate_to_existing_flow=True,
            )
        fallback = _rank_routes(base_request, max_ops=BASE_COMPUTATION_MAX_OPS)
        if fallback.get("active"):
            return _stamp(
                fallback,
                "departure_joint_H",
                used_H_fallback=True,
                computation_budget=BASE_COMPUTATION_MAX_OPS,
                finance_attempt={"active": False, "reason": reason, "operations": primary.get("operations")},
                same_current_base_verified=True,
            )
    return _stamp(
        primary,
        None,
        computation_budget=budget,
        used_H_fallback=False,
        H_fallback_coverage="caller_base_request_missing" if late and base_request is None else "unavailable",
        delegate_to_existing_flow=True,
    )


def rank_routes(request, *, reserve_policy=None, base_request=None, max_ops=None, strategy_id=STRATEGY_ID):
    return evaluate_request(
        request,
        reserve_policy=reserve_policy,
        base_request=base_request,
        max_ops=max_ops,
        strategy_id=strategy_id,
    )


def evaluate_tail(current, *, max_ops=BASE_COMPUTATION_MAX_OPS):
    return _stamp(K.rank(current, max_ops=max_ops), "tail263_reference", computation_budget=max_ops)


# --- Production Real-Observation Binding & Ranking Application ---


def in_scope(state, goal):
    period = (state.scenario_state or {}).get("larc_period") or {}
    return enabled_for_goal(goal) and period.get("status") == "verified" and CAL.period_ordinal(period.get("value") or {}) in DAYS


def binding(state, goal):
    """Bind accepted five-page facts and caller Goal, never a wall-clock cache."""
    run = retained._source(state)
    facts = {
        "run_session_id": run,
        "turn_index": state.turn,
        "period": (state.scenario_state or {}).get("larc_period"),
        "vitality": state.vitality,
        "stats": state.current_stats,
        "goal": asdict(goal),
        "pages": [],
    }
    for candidate in sorted(state.training_options, key=lambda c: c.training_type):
        capture = (candidate.raw_data or {}).get("capture") or {}
        if not isinstance(capture.get("sha256"), str) or not capture["sha256"]:
            raise ValueError("current_five_page_capture_hash_missing")
        facts["pages"].append(
            {
                "training": candidate.training_type,
                "capture": capture,
                "gains": candidate.stats_gain,
                "risk": candidate.failure_rate,
                "larc": (candidate.scenario_mechanics or {}).get("larc"),
            }
        )
    return {
        "run_session_id": run,
        "turn_index": state.turn,
        "source_fingerprint": sha256(
            json.dumps(facts, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
        ).hexdigest(),
    }


def cached_plan(state, goal):
    value = (state.scenario_state or {}).get(PLAN_KEY)
    try:
        if isinstance(value, dict) and value.get("binding") == binding(state, goal):
            return deepcopy(value)
    except (ValueError, TypeError, KeyError):
        pass
    return None


def bind_plan(state, report):
    return replace(state, scenario_state={**state.scenario_state, PLAN_KEY: deepcopy(report)})


def _effect_packet(source, aid, kind, raw, pt, hp, **extra):
    return {
        "action_id": aid,
        "kind": kind,
        "raw_gain": list(raw),
        "Pt": pt,
        "hp_delta_points": hp,
        "p_percent": 0,
        "allowed": True,
        "source": deepcopy(source),
        "basis": "accepted_current_or_strict_past_formal_projection",
        **extra,
    }


def _context(state):
    from core.auto_trainer.larc_tail263.production import _context as current_context

    cc = current_context(state)
    run = retained._source(state)
    growth = (state.scenario_state or {}).get("larc_growth_observation") or {}
    gs = growth.get("source") or {}
    main_hash = growth.get("source_frame_sha256")
    main_bound = (
        growth.get("status") == "accepted"
        and growth.get("turn_index") == state.turn
        and (growth.get("run_session_id") == run or (gs.get("run_session_id") == run and gs.get("turn_index") == state.turn))
        and isinstance(main_hash, str)
        and bool(main_hash)
        and (not gs or gs.get("sha256") == main_hash)
        and type(growth.get("adaptation_points")) is int
        and growth["adaptation_points"] >= 0
    )
    if main_bound and cc.get("status") == "accepted" and cc.get("run_session_id") == run and retained.levels(cc.get("levels")):
        cc.update(
            points=growth["adaptation_points"],
            balance_current=True,
            basis="same_turn_accepted_main_growth_AP_and_same_run_confirmed_owned_levels",
            balance_source={"run_session_id": run, "turn_index": state.turn, "sha256": main_hash},
        )
    if not (
        cc.get("status") == "accepted"
        and cc.get("run_session_id") == run
        and cc.get("through_turn") == state.turn
        and retained.levels(cc.get("levels"))
        and cc.get("balance_current") is True
        and number(cc.get("points"))
        and cc["points"] >= 0
    ):
        raise ValueError("accepted_current_owned_and_wallet_required")
    return cc


def _past_records(state, run):
    from core.auto_trainer.larc_phase_memory_io import _planned

    context = state.scenario_state or {}
    history = context.get("larc_attribute_inputs") or {}
    domestic = []
    if history.get("run_session_id") == run:
        for sample in history.get("training_history", []):
            rec = deepcopy(sample)
            if rec.get("run_session_id") != run:
                continue
            turn = rec.get("turn_index")
            day = CAL.period_ordinal(rec.get("period") or {})
            if type(turn) is not int or turn >= state.turn or day is None or CAL.is_overseas(day) or _planned(rec):
                continue
            rec["calendar_ordinal"] = day
            domestic.append(rec)
    archive = context.get("larc_first_expedition_reference") or {}
    camp = []
    if archive.get("run_session_id") == run:
        for rec in archive.get("records", []):
            if (
                rec.get("run_session_id") == run
                and type(rec.get("turn_index")) is int
                and rec["turn_index"] < state.turn
                and rec.get("calendar_ordinal") in (36, 37, 38, 39, 41)
                and not _planned(rec)
            ):
                camp.append(deepcopy(rec))
    return domestic[-8:], camp


def _menus(records, state, owned, expectation, dest, capacity):
    """Formal gain/cost projection of complete strict-past joint layouts."""
    from core.auto_trainer.larc_training_forecast import project_gains

    run = retained._source(state)
    output, skipped = [], []
    for rec in records:
        try:
            old_owned = rec.get("known_owned_levels")
            if not retained.levels(old_owned):
                raise ValueError("past_owned_unknown")
            actions = []
            for tab in TABS:
                old = deepcopy((rec.get("forecast_metadata") or {}).get(tab) or {})
                energy = old.get("energy_observation") or {}
                es = energy.get("source") or {}
                risk = (rec.get("risk_observations") or {}).get(tab) or {}
                rs = risk.get("source") or {}
                if (
                    energy.get("status") != "accepted"
                    or energy.get("unit") != "points"
                    or not all(es.get(k) == rs.get(k) and es.get(k) is not None for k in ("run_session_id", "turn_index", "sha256"))
                    or es.get("run_session_id") != run
                    or es.get("turn_index") != rec["turn_index"]
                    or not number(risk.get("failure_rate"))
                    or not number(old.get("expectation"))
                ):
                    raise ValueError("past_energy_risk_expectation_source_unknown")
                request = energy.get("requested_delta_points")
                if request is None and energy.get("clipped") is False:
                    request = energy.get("applied_delta_points")
                before = energy.get("before_points")
                if (
                    not isinstance(request, (tuple, list))
                    or len(request) != 2
                    or not all(number(v) for v in request)
                    or not isinstance(before, (tuple, list))
                    or len(before) != 2
                    or not all(number(v) for v in before)
                ):
                    raise ValueError("past_HP_anchor_or_request_unknown")
                raw = rec["raw_gains"][tab]
                if set(raw) != set(TABS) or any(not number(raw[k]) for k in TABS):
                    raise ValueError("past_complete_five_gain_columns_required")
                gains = {k: display_gain(state.current_stats[k], raw[k]) for k in TABS}
                gains["skill_pt"] = rec["skill_points"][tab]
                old.update(period=rec["period"], levels=list(old_owned), purchase_v3=True)
                target = {**old, "period": CAL.period_at(dest), "levels": list(owned), "expectation": expectation}
                pg, report = project_gains(state.current_stats, gains, tab, old, target)
                hp = min(request)
                hp *= report["cost_factor"] if hp < 0 else 1
                level = risk.get("public_training_level")
                if level is None:
                    level = 6
                actions.append(
                    _effect_packet(
                        {"run_session_id": run, "turn_index": rec["turn_index"]},
                        "future:" + tab,
                        "train",
                        [raw_gain(state.current_stats[k], pg[k]) for k in TABS],
                        pg["skill_pt"],
                        hp,
                        p_percent=risk["failure_rate"],
                        training_type=tab,
                        anchor_hp_points=sum(before) / 2,
                        training_level_prior=level,
                        assumptions=[
                            "strict_past_layout_not_future_page",
                            "formal_project_gains_estimate",
                            "conservative_HP_range_endpoint",
                            "JP_level6_prior_if_public_level_unknown",
                        ],
                    )
                )
            output.append(
                {
                    "source_id": "past-T" + str(rec["turn_index"]) + "-to-" + str(dest),
                    "source": {"run_session_id": run, "turn_index": rec["turn_index"]},
                    "source_calendar": rec["calendar_ordinal"],
                    "actions": actions,
                }
            )
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            skipped.append({"turn": rec.get("turn_index"), "reason": str(exc)})
    return output[-5:], skipped


def _ss(state, preview, source, comparison=None):
    from core.auto_trainer.larc_recovery import ss_recovery
    from core.auto_trainer.larc_ss_visible import ss_victory_evidence, visible_ss_participants

    if not isinstance(preview, dict) or preview.get("status") != "accepted":
        raise ValueError("SS_preview_pending_keep_original_comparison")
    ps = preview.get("source") or (preview.get("participants_observation") or {}).get("source") or {}
    if ps.get("run_session_id") != source["run_session_id"] or ps.get("turn_index") != state.turn or not ps.get("sha256"):
        raise ValueError("current_SS_preview_source_unbound")
    count = preview.get("ready_count")
    if type(count) is not int or not 0 <= count <= 5:
        raise ValueError("SS_ready_count_unknown")
    result = {"ready_count_lower_bound": count, "held_offer": None, "held_member_count": None}
    if count != 5:
        return result
    rows = visible_ss_participants(preview)
    gains = preview.get("stats_gain") or {}
    if (
        len(rows) != 5
        or not ss_victory_evidence(preview)["guaranteed"]
        or any(not number(gains.get(k)) or gains[k] < 0 for k in (*TABS, "skill_pt"))
        or any((r.get("current_reward") or {}).get("status") != "accepted" for r in rows)
    ):
        raise ValueError("full_SS_complete_guaranteed_preview_unavailable_keep_original")
    recovery = ss_recovery(preview)
    income = preview.get("adaptation_points_if_all_win")
    income = income if number(income) and income >= 0 else None
    comparison = comparison or {}
    result.update(
        held_member_count=5,
        held_offer=_effect_packet(
            source,
            "ss.full",
            "ss",
            [raw_gain(state.current_stats[k], gains[k]) for k in TABS],
            gains["skill_pt"],
            recovery["points"],
            cap_delta_points=recovery["max_increase"],
            conditional_income_points=income,
            baseline_score=comparison.get("score"),
            components=deepcopy(comparison.get("components") or {}),
            assumptions=[
                "accepted_current_full5_only",
                "host_ss_recovery_rule_not_future_observed_reward",
                "one_current_offer_token_no_generated_offer_without_past_receipt",
            ],
        ),
    )
    return result


def _mei(state, rows, source):
    from core.auto_trainer.larc_friend_outing import (
        bound_outing_inputs,
        bound_outing_packet,
        bound_outing_progress,
        outing_effects,
    )
    from core.auto_trainer.run_support_profile import outing_inputs

    packet = bound_outing_packet(state)
    progress_only = packet is None
    if progress_only:
        if any(not is_hard_rejected(r) and r.get("action_id", "").startswith("outing.satake_mei.") for r in rows):
            raise ValueError("eligible_Mei_packet_unavailable")
        packet = bound_outing_progress(state)
        if packet is None:
            return {"unlocked": False, "completed": None, "next_stages": []}
        bonuses = outing_inputs(packet["support_card_profile"], packet["run_session_id"], confirmed_outing=True) or {}
    else:
        bonuses = bound_outing_inputs(packet) or {}
    stages = []
    byid = {r.get("action_id"): r for r in rows}
    for stage in range(packet["completed"] + 1, 6):
        fx = outing_effects(stage, recovery_bonus=bonuses.get("recovery"), event_bonus=bonuses.get("event"))
        aid = "outing.satake_mei." + str(stage)
        row = byid.get(aid) or {}
        stages.append(
            _effect_packet(
                source,
                aid,
                "mei_outing",
                [raw_gain(state.current_stats[k], fx["stats"].get(k, [0, 0])[0]) for k in TABS],
                0,
                fx["vitality_points"][0],
                mei_stage=stage,
                allowed=(progress_only or (aid in byid and not is_hard_rejected(row))) if stage == packet["completed"] + 1 else True,
                baseline_score=row.get("score"),
                components=deepcopy(row.get("components") or {}),
                support_effect_resolution=deepcopy(bonuses.get("resolution") or {}),
                assumptions=[
                    "continuous_confirmed_Mei_stage",
                    "resolved_support_effects_are_planning_inputs_not_observed_rewards",
                ],
            )
        )
    return {
        "status": "accepted",
        "source": source,
        "unlocked": True,
        "completed": packet["completed"],
        "next_stages": stages,
        **({"current_outing_unavailable": True} if progress_only else {}),
    }


def host_reserve_for_entry(context):
    """The existing production263 reserve; never credits unverified 59/64 cash."""
    from core.auto_trainer.larc_adaptation_schedule import future_reserve

    owned = context["levels_after"]
    legacy = future_reserve(
        owned,
        60,
        "ordinary",
        context["future_training_share"],
        include_today_income=False,
        end_day=66,
        respect_observed_locks=True,
    )
    report = K.reserve_for_action(
        60,
        owned,
        context.get("current_conditional_income"),
        context.get("p_percent"),
        context["future_training_share"],
        branch="ordinary",
        end_day=66,
        respect_observed_locks=True,
        fixed_race_incomes=None,
        cash_before=context["cash_before"],
        basket_cost=context["basket_cost"],
        existing_legacy_reserve=legacy,
    )
    report.update(
        purchase_authorized=False,
        basis="retained263_production_reserve_no_unverified_fixed_race_credit",
        future_cash_not_current_wallet=True,
        conditional_slot8_unlock_assumed=False,
        ss_wins_known_or_planned=context.get("ss_wins"),
    )
    return report


def _first_entry_policy(state, owned, source, *, compare_menus=False, goal=None):
    """Quote the existing Pt and deck-dominant facility prices at entry."""
    from core.auto_trainer.larc_adaptation import TRAINING_UPGRADES, selected_upgrade_cost
    from core.auto_trainer.larc_adaptation_schedule import UNLOCK_DAYS
    from core.auto_trainer.larc_adaptation_strategy import deck_training_coverage

    coverage = deck_training_coverage(state)
    if coverage.get("complete") is not True:
        raise ValueError("first_entry_deck_coverage_unknown_hold_owned_scenario")
    counts = coverage["counts"]
    top = max(counts.values())
    kinds = [k for k in TABS if counts[k] == top]
    allow_tie = params_for_goal(goal).first_entry_allow_tied_dominant_deck
    if top <= 0 or (len(kinds) != 1 and not allow_tie):
        raise ValueError("first_entry_deck_dominant_type_tie_original_priority_unknown")
    unlocked = list(owned)
    for index, due in UNLOCK_DAYS.items():
        if due == 36 and unlocked[index] == 0:
            unlocked[index] = 1
    selected_kinds = kinds if (allow_tie and compare_menus) else [kinds[0]]
    for k in selected_kinds:
        if unlocked[TRAINING_UPGRADES[k][1]] < 1 or unlocked[5] < 1:
            raise ValueError("first_entry_facility_or_Pt_unlock_uncovered")
    plans = []
    alternatives = [("pt3_plus_" + k + "3", (5, TRAINING_UPGRADES[k][1])) for k in selected_kinds]
    alternatives.append(("pt3", (5,)))
    if compare_menus:
        alternatives.extend((k + "3", (TRAINING_UPGRADES[k][1],)) for k in selected_kinds)
    for label, slots in (*alternatives, ("hold", ())):
        target = list(unlocked)
        buys = []
        for index in dict.fromkeys(slots):
            if target[index] == 3:
                continue
            before = list(target)
            target[index] = 3
            amount = selected_upgrade_cost(before, target)
            if type(amount) is not int or amount <= 0:
                raise ValueError("first_entry_existing_rule_quote_unavailable")
            buys.append({"slot": index, "from_level": before[index], "to_level": 3, "cost": amount})
        plans.append(
            {
                "plan_id": label,
                "targets": target,
                "cost": sum(b["cost"] for b in buys),
                "atomic_buys": buys,
                "legal": True,
                "price_basis": "existing_rule_reference",
            }
        )
    return {
        "schema_version": "larc.entry_investment_policy.v1",
        "calendar": 36,
        "source": source,
        "entry_unlocked_levels": unlocked,
        "unlock_basis": "existing_host_UNLOCK_DAYS_conditional0to1_not_current_paid_levels",
        "alternatives": plans,
        "deck_coverage": coverage,
        **({"selection_mode": "adaptive_menu_remaining_h"} if compare_menus else {}),
        "basis": (
            "strict_past_menu_conditional_basket_plus_remaining_H_actual_owner_reobserves"
            if compare_menus
            else "conditional_Pt3_then_confirmed_deck_dominant_facility3_priority_actual_owner_reobserves"
        ),
        "forecast_only": True,
        "actual_purchase_execution_changed": False,
    }


def build_request(state, goal, baseline, *, ss_preview, ss_comparison=None):
    from core.auto_trainer.larc_attribute_plan import history_sample
    from core.auto_trainer.larc_energy_observation import training_energy_observation
    from core.auto_trainer.larc_opening import LarcOpeningStrategy
    from core.auto_trainer.larc_phase import phase_weights
    from core.auto_trainer.larc_risk import risk_value
    from core.auto_trainer.larc_ss_deadline import deadline_status
    from core.auto_trainer.larc_strategy_rules import pre_expedition_energy_spec
    from core.auto_trainer.larc_tail263.production import _incomes, scenario

    if not in_scope(state, goal):
        raise ValueError("outside1.8_departure_scope")
    failure_limit = LarcOpeningStrategy.failure_limit(goal, state)
    if not number(failure_limit) or not 0 <= failure_limit <= 100:
        raise ValueError("configured_risk_limit_must_be_percent_0_to_100")
    context = state.scenario_state or {}
    if context.get("simulation") or "simulation_truth" in context:
        raise ValueError("real_semantic_state_required_no_model_truth")
    if baseline.action not in ("train", "rest", "outing"):
        raise ValueError("original_stop_or_handoff_preserved")
    rows = baseline.scored_candidates
    if any(
        (r.get("score_workflow") or {}).get("status") == "matched"
        or (r.get("score_workflow") or {}).get("priority_rank", 0) != 0
        for r in rows
    ):
        raise ValueError("explicit_workflow_priority_preserved")
    bind = binding(state, goal)
    run, turn = bind["run_session_id"], state.turn
    source = {"run_session_id": run, "turn_index": turn}
    day = CAL.period_ordinal(context["larc_period"]["value"])
    unreadable = retained.unreadable_failure_exclusions(state)
    if unreadable:
        return None, {"current_forecast_exclusions": unreadable}
    cc = _context(state)
    owned = cc["levels"]
    assumed = scenario(state, goal)
    capacity = assumed["capacity_points"]
    current = history_sample(state)
    current_gain_evidence = {}
    if day < 36:
        current, current_gain_evidence = complete_current_sample(state, current)
    retained._accepted_current({"status": "accepted", "run_session_id": run, "sample": current}, run, turn, state)
    byid = {r.get("action_id"): r for r in rows}
    incomes = _incomes(state)
    actions = []
    from core.auto_trainer.larc_strategy_extension import active_provider

    provider = active_provider()
    omitted_unbound = set()
    omit_ineligible = provider.departure_omit_ineligible_unbound(params_for_goal(goal))
    for candidate in state.training_options:
        tab = candidate.training_type
        row = byid.get(candidate.action_id)
        if row is None or row.get("failure_rate") != candidate.failure_rate:
            raise ValueError("current_baseline_pool_mismatch:" + tab)
        if training_energy_observation(candidate, state).get("status") != "accepted":
            # Skip an unreadable candidate only when already ineligible under failure caps and enabled.
            if omit_ineligible and (
                is_hard_rejected(row)
                or (number(candidate.failure_rate) and candidate.failure_rate > failure_limit)
            ):
                omitted_unbound.add(candidate.action_id)
                continue
            raise ValueError("current_energy_capture_unbound:" + tab)
        feature = (row.get("features") or {}).get("newly_full") or {}
        newly = feature.get("known_value")
        newly = newly if type(newly) is int and 0 <= newly <= 15 else 0
        gain_basis = current_gain_evidence.get(tab)
        actions.append(
            _effect_packet(
                source,
                candidate.action_id,
                "train",
                [current["raw_gains"][tab][k] for k in TABS],
                current["skill_points"][tab],
                retained._current_hp(candidate, run, turn, capacity),
                p_percent=candidate.failure_rate,
                allowed=not is_hard_rejected(row) and candidate.failure_rate <= failure_limit,
                training_type=tab,
                components=deepcopy(row.get("components") or {}),
                baseline_score=row.get("score"),
                newly_full_lower_bound=newly,
                newly_full_basis="same_current_visible_newly_full_feature_lower_bound",
                conditional_income_points=(incomes.get(candidate.action_id) or {}).get("conditional_amount"),
                **({"current_gain_evidence": gain_basis, "assumptions": [gain_basis["basis"]]} if gain_basis else {}),
            )
        )
    rest = byid.get("rest.normal")
    rest_effect = _effect_packet(
        source,
        "rest.normal",
        "rest",
        [0] * 5,
        0,
        50,
        recovery_outcomes=[[0.25, 30, 0], [0.5, 50, 0], [0.25, 70, 0]],
        assumptions=["retained_formal_rest_planning_weights_not_game_probabilities"],
    )
    if rest:
        actions.append(
            {
                **rest_effect,
                "allowed": not is_hard_rejected(rest),
                "baseline_score": rest.get("score"),
                "components": deepcopy(rest.get("components") or {}),
            }
        )
    mei = _mei(state, rows, source)
    if mei["next_stages"] and not mei.get("current_outing_unavailable"):
        actions.append(deepcopy(mei["next_stages"][0]))
    ss = _ss(state, ss_preview, source, ss_comparison)
    if ss["held_offer"]:
        actions.append(deepcopy(ss["held_offer"]))
    supported = {a["action_id"] for a in actions} | omitted_unbound
    if any(not is_hard_rejected(r) and r.get("action_id") not in supported for r in rows):
        raise ValueError("eligible_action_unmodeled_original_pool_kept")
    deadline = deadline_status(state, goal, ss_preview)
    limit = deadline.get("max_deferred_main_actions")
    debt = 0 if ss["held_offer"] is None else deadline.get("deferred_main_actions")
    if deadline.get("force_current_ss"):
        limit, debt = 0, 0
    if limit is not None and (type(debt) is not int or debt < 0):
        raise ValueError("existing_SS_deadline_debt_unknown")
    ss["deadline_obligation"] = {
        "enforced": limit is not None,
        "max_deferrals": limit,
        "already_deferred": debt,
        "scope_days": list(CAL.free_action_days(day, 36 if day < 36 else 60)),
    }
    domestic, camp = _past_records(state, run)
    growth = context.get("larc_growth_observation") or {}
    gs = growth.get("source") or {}
    expectation = (
        growth.get("expectation")
        if (
            growth.get("status") == "accepted"
            and growth.get("turn_index") == turn
            and (growth.get("run_session_id") == run or (gs.get("run_session_id") == run and gs.get("turn_index") == turn))
        )
        else None
    )
    if not number(expectation):
        raise ValueError("current_expectation_required_for_past_projection")
    foreign_sources = camp or domestic
    dom, domskip = _menus(domestic, state, owned, expectation, day, capacity)
    foreign, foreignskip = _menus(foreign_sources, state, owned, expectation, 60 if day >= 56 else 36, capacity)
    weights = phase_weights(state, goal)
    policy_dict = getattr(goal, "larc_policy", None) if isinstance(getattr(goal, "larc_policy", None), dict) else {}
    params = params_for_goal(goal)
    from core.auto_trainer.larc_policy import attribute_options
    attr_opts = attribute_options(goal)
    eff_weights = attr_opts.get("attribute_weights") or policy_dict.get("attribute_weights") or {}
    eff_credit = attr_opts.get("after_target_credit", policy_dict.get("after_target_credit", params.default_after_target_credit))
    request = {
        "schema_version": "larc.departure_route_request.v1",
        "run_session_id": run,
        "turn_index": turn,
        "calendar_ordinal": day,
        "period_status": "verified",
        "last_free_day": 65 if day >= 56 else 41,
        "settlement_through": 67 if day >= 56 else 41,
        "state": {
            "raw_equivalent": [K.inverse_display(state.current_stats[k]) for k in TABS],
            "HP_points": state.vitality * capacity / 100,
            "capacity_points": capacity,
            "Pt": state.current_stats["skill_pt"],
            "cash": cc["points"],
            "owned_levels": list(owned),
        },
        "goal": {
            "targets": [goal.target_stats[k] for k in TABS],
            "importance": [eff_weights.get(k, 1.0) for k in TABS],
            "pt_factor": weights["skill_pt"],
            "after_target_credit": eff_credit,
            "failure_limit": int(failure_limit) if failure_limit == int(failure_limit) else failure_limit,
            "risk_aversion_per_percent": risk_value(0.0, 1.0)["preference_cost"],
        },
        "current_actions": actions,
        "past_menus_by_phase": {"domestic": dom, "overseas": foreign},
        "ss": ss,
        "mei": mei,
        "remaining_energy_frontier": {"enabled": True, "grid_points": 5},
        "future_recovery": {
            "domestic": deepcopy(rest_effect),
            "overseas": _effect_packet(
                source,
                "rest.normal",
                "rest",
                [0] * 5,
                0,
                50,
                assumptions=["retained263_foreign_rest50_conditional_not_CN_observed"],
            ),
            "allow_rest_with_Mei": bool(provider.mei_comparison_enabled(params)),
        },
        "scheduled_events": [],
        "terminal_energy_contract": {},
        "assumptions": [
            "raw_equivalent_not_observed_game_raw",
            "unknown_stat_caps_unbounded_valuation_scenario",
            "capacity_bounds_midpoint_scenario",
            "strict_past_formal_projection_not_future_layout",
            "retained263_conditional_schedule_CN_unverified",
            "no_unknown_income_filled_as_observed_zero",
        ],
    }
    provider.build_departure_request_extras(
        params=params,
        request=request,
        state=state,
        goal=goal,
        day=day,
        tabs=TABS,
        period_at_day=CAL.period_at,
    )
    if failure_limit != LarcOpeningStrategy.DEFAULT_FAILURE_LIMIT:
        request["risk_limit_contract"] = "configured_goal_risk_v1"
    energy_spec = pre_expedition_energy_spec(state, goal, owned_levels=owned, ap=cc["points"])
    if energy_spec is not None:
        request["expedition_entry_energy_contract"] = energy_spec
    first_policy_gap = None
    if day < 36:
        from core.auto_trainer.larc_recovery_routes import terminal_energy_weight

        coefficient = terminal_energy_weight(
            state,
            goal,
            {
                "kind": "rolling_recovery",
                "steps": len(tuple(CAL.free_action_days(day, 42))) - 1,
                "action_calendar": [{"period": CAL.period_at(41)}],
            },
        )
        request["terminal_energy_contract"] = {
            "legacy_coefficient": coefficient,
            "basis": "existing_formal_rolling_continuation_after_first_camp41",
        }
        try:
            compare_menus = day in (34, 35)
            policy = _first_entry_policy(state, owned, source, compare_menus=compare_menus, goal=goal)
            request["entry_investment_policy"] = policy
            request["menus_by_owned"] = [
                {
                    "owned_levels": list(levels),
                    "overseas": _menus(foreign_sources, state, list(levels), expectation, 36, capacity)[0],
                }
                for levels in sorted({tuple(p["targets"]) for p in policy["alternatives"]})
            ]
        except (ValueError, TypeError, KeyError) as exc:
            first_policy_gap = str(exc)
    else:
        terminal = assumed["terminal"]
        schedule = K.normal_schedule(final_level=owned[9], **terminal)
        request["scheduled_events"] = [
            {
                "calendar": e.calendar,
                "label": e.label,
                "condition": "condition_on_normal_settlement" if e.calendar == 66 else "condition_on_normal_progress",
                "stats_gain": list(e.stats_gain),
                "stats_unit": e.stats_unit,
                "Pt": e.pt,
                "HP_points": e.hp_points,
                "capacity_points": e.cap_points,
                "basis": e.basis + ";" + e.condition,
            }
            for e in schedule
            if e.calendar > day
        ]
        if mei["unlocked"] and mei["completed"] < 5:
            before = K.normal_schedule(final_level=owned[9], **{**terminal, "mei_unlocked": True, "mei_completed": False})
            after = K.normal_schedule(final_level=owned[9], **{**terminal, "mei_unlocked": True, "mei_completed": True})
            a = next(e for e in before if e.label.startswith("Mei_ending"))
            b = next(e for e in after if e.label.startswith("Mei_ending"))
            request["terminal_mei_completion_bonus"] = {
                "calendar": 66,
                "base_mei_completed": False,
                "condition": "condition_on_normal_settlement",
                "stats_unit": "raw",
                "stats_gain": [y - x for x, y in zip(a.stats_gain, b.stats_gain)],
                "Pt": b.pt - a.pt,
                "basis": "retained263_completed_minus_uncompleted_Mei_ending_delta",
            }
    coverage = {
        "current_training_pages": 5,
        "past_domestic_menus": len(dom),
        "past_overseas_menus": len(foreign),
        "past_menu_skipped": domskip + foreignskip,
        "capacity_scenario": assumed,
        "cash_basis": cc.get("basis"),
        "finance": "host_original_reserve_no_unverified59_64_income",
        "first_entry_investment_coverage": first_policy_gap or ("conditional_host_rule_policy" if day < 36 else "not_first_entry"),
        "first_camp_fixed_race_rewards": "uncovered_no_C1_model_only_cash_injected" if day < 36 else None,
    }
    if current_gain_evidence:
        coverage["current_gain_lower_bounds"] = current_gain_evidence
    if request.get("risk_limit_contract"):
        coverage["configured_risk_limit"] = {
            "percent": failure_limit,
            "basis": "current_goal_for_current_and_forecast_training",
            "current_baseline_rejections_preserved": True,
        }
    return request, coverage


def augment_finance(base, state):
    if base["calendar_ordinal"] < 56:
        return base, None
    request = deepcopy(base)
    context = state.scenario_state or {}
    wins = context.get("larc_ss_wins") or {}
    if (
        wins.get("status") == "accepted"
        and wins.get("run_session_id") == base["run_session_id"]
        and type(wins.get("through_turn")) is int
        and wins["through_turn"] <= state.turn
        and type(wins.get("value")) is int
        and wins["value"] >= 0
    ):
        request["state"]["ss_wins"] = wins["value"]
    owned = base["state"]["owned_levels"]
    policy = quote_late_baskets(owned, {"run_session_id": base["run_session_id"], "turn_index": state.turn})
    policy["basis"] = "conditional_calendar60_existing_formal_rule_quotes_actual_purchase_owner_reobserves"
    request["entry_investment_policy"] = policy
    domestic, camp = _past_records(state, base["run_session_id"])
    growth = context["larc_growth_observation"]
    request["menus_by_owned"] = [
        {
            "owned_levels": list(levels),
            "overseas": _menus(
                camp or domestic,
                state,
                list(levels),
                growth["expectation"],
                60,
                base["state"]["capacity_points"],
            )[0],
        }
        for levels in sorted({tuple(p["targets"]) for p in policy["alternatives"]})
    ]
    request["late_finance_rule_basis"] = "existing_production263_host_reserve; future_estimates_not_current_payment"
    return request, host_reserve_for_entry


def _host_identity(goal):
    p = params_for_goal(goal)
    if p.revision != STRATEGY_ID:
        return {
            "strategy_id": p.revision,
            "policy_version": p.policy_version,
            "parent_strategy_id": STRATEGY_ID,
        }
    return {"strategy_id": STRATEGY_ID, "policy_version": POLICY_VERSION}


def evaluate(state, goal, baseline=None, *, ss_preview=None, ss_comparison=None, max_ops=None):
    """Called once by the SS owner before it executes either branch."""
    report = {
        "schema_version": "larc.strategy18_production.v1",
        **_host_identity(goal),
        "active": False,
        "execution_authorized": False,
        "purchase_authorized": False,
        "new_stop_required": False,
        "coverage": {},
    }
    if not in_scope(state, goal):
        return {**report, "reason": "outside1.8_departure_scope"}
    try:
        report["binding"] = binding(state, goal)
        saved = cached_plan(state, goal)
        if saved is not None:
            return saved
        if baseline is None:
            from core.auto_trainer.larc_opening import LarcOpeningStrategy

            baseline = LarcOpeningStrategy(goal).evaluate_baseline(state)
        base, coverage = build_request(state, goal, baseline, ss_preview=ss_preview, ss_comparison=ss_comparison)
        if base is None:
            return {
                **report,
                "reason": "optional18_failure_rate_unreadable",
                "advice": None,
                "baseline_advice": {"action": baseline.action, "target": baseline.target},
                "coverage": coverage,
                "delegate_to_existing_flow": True,
            }
        finance_gap = None
        try:
            request, reserve = augment_finance(base, state)
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
            request, reserve = base, None
            finance_gap = str(exc)
        output = evaluate_request(request, base_request=base, reserve_policy=reserve, max_ops=max_ops)
        if finance_gap:
            output.update(used_H_fallback=output.get("active") is True, finance_attempt={"active": False, "reason": finance_gap})
        keep = (
            "active",
            "reason",
            "effective_impl",
            "used_H_fallback",
            "finance_attempt",
            "H_fallback_coverage",
            "delegate_to_existing_flow",
            "operations",
            "computation_budget",
            "free_action_days",
            "diagnostics",
            "entry_investment_forecast",
            "remaining_energy_frontier",
        )
        report.update({k: deepcopy(output[k]) for k in keep if k in output})
        score_keys = (
            "action_id",
            "score",
            "expected_window_utility_delta",
            "current_residual_once",
            "current_risk_aversion",
            "soft_risk_discount",
        )
        ranks = output.get("rankings", [])
        soft_actions = {r.get("action_id") for r in baseline.scored_candidates if r.get("soft_risk") is True}
        adjusted = retained.discount_soft_ranking(ranks, soft_actions)
        report["ranking"] = [{k: deepcopy(r[k]) for k in score_keys if k in r} for r in adjusted]
        joint = output.get("late_entry_joint") or {}
        if joint:
            report["finance"] = {
                "statistics": joint.get("statistics"),
                "basis": "conditional_entry_estimates_not_payments",
                "examples": [
                    {
                        **{
                            k: deepcopy(r.get(k))
                            for k in (
                                "plan_id",
                                "action_id",
                                "Q",
                                "cash_before",
                                "basket_cost",
                                "current_income_conditional_not_payable_cash",
                                "allowed_by_reserve_gate",
                                "empty_basket_bypass",
                            )
                        },
                        "required_post_basket_reserve": (r.get("finance") or {}).get("required_post_basket_reserve"),
                    }
                    for r in joint.get("examples_first8", [])[:3]
                ],
            }
        report["coverage"] = {**coverage, **(output.get("coverage") or {})}
        report["advice"] = adjusted[0]["action_id"] if adjusted is not ranks else output.get("selected_action_id")
        report["baseline_advice"] = {"action": baseline.action, "target": baseline.target}
        report["request_digest"] = sha256(json.dumps(request, sort_keys=True, allow_nan=False).encode()).hexdigest()
        report["current_action_ids"] = [a["action_id"] for a in request["current_actions"] if a["allowed"]]
        if report["active"] and report["advice"] not in report["current_action_ids"]:
            report.update(active=False, reason="planner_action_not_current_legal_pool", advice=None)
    except (ValueError, KeyError, TypeError, IndexError, ArithmeticError) as exc:
        report.update(active=False, reason=str(exc), advice=None, delegate_to_existing_flow=True)
    return report


def summary(report):
    fields = (
        "schema_version",
        "strategy_id",
        "policy_version",
        "parent_strategy_id",
        "active",
        "reason",
        "advice",
        "effective_impl",
        "used_H_fallback",
        "finance_attempt",
        "H_fallback_coverage",
        "coverage",
        "binding",
        "operations",
        "computation_budget",
        "request_digest",
        "finance",
        "entry_investment_forecast",
        "assumptions",
        "current_action_ids",
        "delegate_to_existing_flow",
    )
    value = {k: deepcopy(report[k]) for k in fields if k in report}
    value["top_ranking"] = deepcopy((report.get("ranking") or [])[:3])
    value.update(execution_authorized=False, purchase_authorized=False, new_stop_required=False)
    return value


def apply_ranking(state, goal, baseline):
    if not in_scope(state, goal):
        return baseline
    report = cached_plan(state, goal) or {
        "active": False,
        **_host_identity(goal),
        "reason": "joint_SS_comparison_pending_original_pool_kept",
    }
    rows = deepcopy(baseline.scored_candidates)
    for row in rows:
        row.setdefault("planning", {})["departure18"] = summary(report)
    if not report.get("active"):
        return replace(baseline, scored_candidates=rows)
    aid = report.get("advice")
    if aid == "ss.full":
        return replace(baseline, scored_candidates=rows)
    winner = next((r for r in rows if r.get("action_id") == aid and not is_hard_rejected(r)), None)
    if winner is None:
        return replace(baseline, scored_candidates=rows)
    ranks = {r["action_id"]: r for r in report.get("ranking", [])}
    for row in rows:
        rank = ranks.get(row.get("action_id"))
        if rank is not None and number(rank.get("score")):
            row["baseline161_score"] = row.get("score")
            row["score"] = rank["score"]
            row["components"] = {
                k: rank[k]
                for k in (
                    "expected_window_utility_delta",
                    "current_residual_once",
                    "current_risk_aversion",
                    "soft_risk_discount",
                )
                if k in rank
            }
            row["score_basis"] = "1.8_equal_budget_departure_joint_value"
    action = winner.get("action") or ("train" if aid.startswith("train:") else "rest")
    target = winner.get("target") if action != "train" else aid.split(":", 1)[1]
    from core.auto_trainer.larc_timeline_workflow import selection_key

    return replace(
        baseline,
        action=action,
        target=target,
        scored_candidates=sorted(rows, key=selection_key),
        confidence=winner.get("source_confidence", baseline.confidence),
        risk_flags=list(winner.get("risk_flags") or []),
        reason="1.8 joint departure selection from the same accepted SS/current five-page pool",
    )
