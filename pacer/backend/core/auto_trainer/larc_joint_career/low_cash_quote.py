"""Finite joint cash/HP quote for first-camp entry (ordinal 36)."""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from math import isfinite
from statistics import mean
from time import perf_counter

from core.auto_trainer.larc_strategy_params import params_for_goal, unified_larc_enabled
from core.auto_trainer.rejection_policy import is_hard_rejected

DEPTH = ContextVar("larc_joint192_low_quote_depth", default=0)


class Unsupported(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, states=80000, edges=500000, seconds=25.0):
        self.limits = states, edges, seconds
        self.states = self.edges = self.tail_calls = 0
        self.start = perf_counter()

    def tick(self, states=0, edges=0, tail=False):
        self.states += states
        self.edges += edges
        self.tail_calls += bool(tail)
        if (
            self.states > self.limits[0]
            or self.edges > self.limits[1]
            or perf_counter() - self.start > self.limits[2]
        ):
            raise BudgetExceeded("whole_quote_joint_route_budget_exceeded")

    def audit(self):
        return dict(
            states=self.states,
            edges=self.edges,
            tail_calls=self.tail_calls,
            seconds=perf_counter() - self.start,
            limits=self.limits,
        )


@dataclass(frozen=True)
class Result:
    raw: float
    gap: float
    cash: float
    levels: tuple
    purchases: tuple = ()
    path: tuple = ()
    feasible: bool = True
    halted_at: object = None
    shortfall_weight: float = 0.35

    @property
    def value(self):
        return self.raw - self.shortfall_weight * self.gap


def _average(results):
    representative = sorted(results, key=lambda r: r.value)[len(results) // 2]
    return Result(
        mean(r.raw for r in results),
        mean(r.gap for r in results),
        mean(r.cash for r in results),
        representative.levels,
        representative.purchases,
        representative.path,
        all(r.feasible for r in results),
        representative.halted_at,
        representative.shortfall_weight,
    )


def _action(row):
    return row.get("after_action_id") or ("train:" + row["training"] if row.get("training") else None)


def _score(row):
    value = row.get("training_score_after_purchase")
    return row.get("action_score_after_purchase") if value is None else value


def filter_globals(result, purchase_rows, current_rows, selection_key):
    output = []
    for row in result:
        phases = set(row.get("purchase_phases") or [])
        if phases != {"expedition_skill_pt"}:
            output.append(row)
            continue
        choices = []
        for key, base in current_rows.items():
            if base.get("hard_rejected", base.get("rejected")) is not False:
                continue
            same = [
                r
                for r in purchase_rows
                if set(r.get("purchase_phases") or []) == phases
                and _action(r) == key
                and type(_score(r)) in (int, float)
                and isfinite(_score(r))
            ]
            if not same:
                raise Unsupported("joint_v2_projected_global_pool_incomplete:" + key)
            choices.append({**base, "score": max(_score(r) for r in same), "action_id": key})
        if not choices:
            raise Unsupported("joint_v2_projected_global_pool_empty")
        best = min(choices, key=selection_key)
        allowed = _action(row) == best["action_id"]
        new = {
            **row,
            "joint_global_current_action_binding": {
                "projected_current_best": best["action_id"],
                "candidate_after_action": _action(row),
                "allowed": allowed,
                "actual_training_still_reobserved_not_forced": True,
            },
        }
        if not allowed and not is_hard_rejected(row):
            new.update(
                rejected=True,
                hard_rejected=False,
                v3_rejection_reason="joint_global_after_action_not_projected_current_best",
            )
        output.append(new)
    return output


def proven_current_rejection(row, *, purchase_rows, current_rows, goal):
    from core.auto_trainer.larc_purchase_training_coupling import current_action_coupling
    from core.auto_trainer.larc_timeline_workflow import selection_key

    detail = current_action_coupling(row, purchase_rows=purchase_rows, current_rows=current_rows, goal=goal)
    if detail.get("enabled") is not True:
        return None
    if (
        detail.get("enabled") is True
        and detail.get("active") is True
        and detail.get("allowed") is False
        and detail.get("reason") == "specific_upgrade_not_used_by_current_best_action"
    ):
        return {"gate": "original_facility_current_action_coupling", "detail": detail}
    if set(row.get("purchase_phases") or []) != {"expedition_skill_pt"}:
        return None
    try:
        filtered = filter_globals([row], purchase_rows, current_rows, selection_key)[0]
    except Unsupported:
        return None
    detail = filtered.get("joint_global_current_action_binding") or {}
    if detail.get("allowed") is False:
        return {"gate": "frozen_v2_global_current_action_binding", "detail": detail}
    return None


class Planner:
    def __init__(self, provider, budget, *, branch, language_deferred, tail_share):
        self.p, self.budget, self.branch = provider, budget, branch
        self.language_deferred, self.tail_share = language_deferred, tail_share
        self.solve = lru_cache(maxsize=None)(self._solve)
        self.closure = lru_cache(maxsize=None)(self._closure)
        self.tail = lru_cache(maxsize=None)(self._tail)

    def _closure(self, cash, owned, day):
        result = {(cash, owned): ()}
        for slot in range(8):
            for (balance, levels), actions in list(result.items()):
                price = self.p.price(slot, levels[slot])
                if price is None or price > balance:
                    continue
                self.budget.tick(edges=1)
                target = list(levels)
                target[slot] = 3
                key = balance - price, tuple(target)
                result.setdefault(key, actions + ((slot, price),))
        return tuple((balance, levels, actions) for (balance, levels), actions in result.items())

    def _tail(self, cash, owned):
        self.budget.tick(tail=True)
        return self.p.tail(cash, owned, self.branch, self.tail_share, self.language_deferred)

    def start(self, cash, owned, hp):
        if cash < 0:
            raise ValueError("current_purchase_cannot_use_future_income")
        return self.solve(37, float(cash), tuple(owned), round(min(100.0, max(0.0, hp)), 1), True)

    def _solve(self, day, cash, owned, hp, ordinary_wisdom_left):
        self.budget.tick(states=1)
        levels = list(owned)
        for slot, unlock in self.p.unlocks.items():
            if levels[slot] == 0 and unlock <= day:
                levels[slot] = 1
        due = [
            i
            for i, level in enumerate(levels)
            if i != 9 and level == 1 and self.p.due(i) <= day and not (i == 4 and self.language_deferred)
        ]
        expense = sum(self.p.level_two_costs[i] for i in due)
        if expense > cash:
            return Result(
                0.0,
                expense - cash,
                cash,
                tuple(levels),
                feasible=False,
                halted_at={"ordinal": day, "shortfall": expense - cash, "unpaid_slots": due},
                shortfall_weight=self.p.shortfall_weight,
            )
        cash -= expense
        for i in due:
            levels[i] = 2
        owned = tuple(levels)
        if day == 42:
            return self.tail(cash, owned)
        if day not in (37, 38, 39, 41):
            return self.solve(day + 1, cash, owned, hp, ordinary_wisdom_left)
        choices_by_layout = []
        for layout in self.p.layout_indices(day):
            options = []
            for balance, target, purchases in self.closure(cash, owned, day):
                offers = self.p.offers(day, layout, target)
                train_reference = max((o["numeric"] for o in offers if o["tab"] != "wisdom"), default=0.0)
                for offer in offers:
                    wisdom = offer["tab"] == "wisdom"
                    ordinary = wisdom and not offer["observed_friendship"]
                    if ordinary and (not ordinary_wisdom_left or self.p.ordinary_wisdom_heal <= 0):
                        continue
                    delta = self.p.ordinary_wisdom_heal if ordinary else offer["hp_delta"]
                    if delta < 0 and hp < -delta:
                        continue
                    self.budget.tick(edges=1)
                    value = min(offer["numeric"], train_reference * 0.5) if ordinary else offer["numeric"]
                    value = self.p.risk_value(value, offer.get("risk_metadata"), hp)
                    value *= self.p.readiness(hp)
                    next_hp = round(min(100.0, max(0.0, hp + delta)), 1)
                    income = 50.0 if self.branch == "conservative" else 100.0
                    child = self.solve(
                        day + 1,
                        balance + income,
                        target,
                        next_hp,
                        ordinary_wisdom_left and not ordinary,
                    )
                    record = (day, "train:" + offer["tab"], hp, next_hp, balance, balance + income, income)
                    options.append(
                        Result(
                            value + child.raw,
                            child.gap,
                            child.cash,
                            child.levels,
                            tuple((day, i, cost) for i, cost in purchases) + child.purchases,
                            (record,) + child.path,
                            child.feasible,
                            child.halted_at,
                            self.p.shortfall_weight,
                        )
                    )
                self.budget.tick(edges=1)
                next_hp = round(min(100.0, hp + self.p.rest_heal), 1)
                child = self.solve(day + 1, balance + 100.0, target, next_hp, ordinary_wisdom_left)
                record = (day, "rest", hp, next_hp, balance, balance + 100.0, 100.0)
                options.append(
                    Result(
                        child.raw,
                        child.gap,
                        child.cash,
                        child.levels,
                        tuple((day, i, cost) for i, cost in purchases) + child.purchases,
                        (record,) + child.path,
                        child.feasible,
                        child.halted_at,
                        self.p.shortfall_weight,
                    )
                )
            choices_by_layout.append(max(options, key=lambda r: r.value))
        return _average(choices_by_layout)


class Provider:
    def __init__(self, installation, state, goal, levels, days, legacy_utility):
        from core.auto_trainer.larc_attribute_plan import attribute_plan
        from core.auto_trainer.larc_capacity import planning_capacity
        from core.auto_trainer.larc_recovery import PLANNING_START_ENERGY, estimated_training_cost
        from core.auto_trainer.larc_training_forecast import training_scenarios
        from core.auto_trainer.larc_training_value import _future_numeric_values

        self.installation, self.m, self.state, self.goal = installation, installation.m, state, goal
        self.shortfall_weight = params_for_goal(goal).unfunded_shortfall_penalty_weight
        self.initial_levels, self.days = tuple(levels), tuple(days)
        self.legacy_utility, self.indices = legacy_utility, {d: i for i, d in enumerate(days)}
        self.prefix = tuple(d for d in days if d in (37, 38, 39, 41))
        if self.prefix != (37, 38, 39, 41):
            raise Unsupported("joint_P0_requires_complete_firstcamp_calendar")
        reference = _future_numeric_values(state, goal)
        window = {
            "steps": 4,
            "future_trainings": 4,
            "kind": "joint_firstcamp_P0",
            "action_calendar": [{"period": self.m.period_at(d), "kind": "overseas"} for d in self.prefix],
        }
        forecast = training_scenarios(
            state,
            goal,
            reference,
            window,
            estimated_training_cost(state)[0],
            cases=[{"name": "owned", "levels": list(levels), "cost": 0, "weight": 1.0}],
        )
        if not forecast.get("active"):
            raise Unsupported("joint_future_layouts_unavailable")
        self.layouts = forecast["scenarios"][0]["calendar_layouts"]
        self.context = attribute_plan(state, goal)
        self.capacity = planning_capacity(state)
        self.rest_heal = 50 * 100 / self.capacity
        self.reserve = PLANNING_START_ENERGY[goal.risk_tolerance]
        self.ordinary_wisdom_heal = 0.0
        self.unlocks, self.level_two_costs = self.m.UNLOCK_DAYS, self.m.LEVEL_TWO_COSTS
        self.offers = lru_cache(maxsize=None)(self._offers)
        self.planners = {}

    def price(self, slot, level):
        return self.m._cost(slot, level)

    def due(self, slot):
        return self.m.mandatory_due(slot, 36)

    def layout_indices(self, day):
        values = self.layouts[self.prefix.index(day)]
        if not values:
            raise Unsupported("joint_empty_forecast_layout")
        return (0,) if len(values) == 1 else (0, len(values) // 2)

    def readiness(self, hp):
        shortfall = min(1.0, max(0.0, (self.reserve - hp) / max(1.0, self.reserve)))
        return 1.0 - {"low": 0.45, "medium": 0.35, "high": 0.25}[self.goal.risk_tolerance] * shortfall

    def risk_value(self, value, metadata, hp):
        from core.auto_trainer.larc_risk import risk_at_energy

        return risk_at_energy(value, metadata, hp)["value"]

    def _offers(self, day, sample, owned):
        from core.auto_trainer.larc_training_forecast import numeric_at_period, project_gains

        result = []
        for row in self.layouts[self.prefix.index(day)][sample]:
            source = {
                **row.get("destination_metadata", {}),
                "period": self.m.period_at(day),
                "levels": list(self.initial_levels),
                "purchase_v3": True,
            }
            gains, factors = project_gains(
                self.state.current_stats,
                row["gains"],
                row["tab"],
                source,
                {**source, "levels": list(owned)},
            )
            numeric = (
                numeric_at_period(
                    self.state,
                    self.goal,
                    gains,
                    self.m.period_at(day),
                    attribute_context=self.context,
                )
                * row.get("forecast_discount", 1.0)
            )
            friend = (
                row["tab"] == "wisdom"
                and row.get("wisdom_friendship_opportunity") is True
                and isinstance(row.get("wisdom_recovery_percent"), (int, float))
                and row["wisdom_recovery_percent"] > 0
            )
            delta = (
                (row["wisdom_recovery_percent"] if friend else 0.0)
                if row["tab"] == "wisdom"
                else (-max(1.0, row["cost"] * factors["cost_factor"]))
            )
            result.append({
                "tab": row["tab"],
                "numeric": numeric,
                "hp_delta": delta,
                "observed_friendship": friend,
                "risk_metadata": row.get("risk_metadata", {}),
            })
        return tuple(result)

    def tail(self, cash, owned, branch, share, deferred):
        return Result(0.0, 0.0, cash, tuple(owned), shortfall_weight=self.shortfall_weight)

    def planner(self, branch, deferred, share):
        key = branch, deferred, share
        if key not in self.planners:
            self.planners[key] = Planner(
                self,
                self.installation.frame["budget"],
                branch=branch,
                language_deferred=deferred,
                tail_share=share,
            )
        return self.planners[key]


class Quote:
    def __init__(self, module, parent):
        self.m, self.original_recommend = module, parent
        self.original_routes, self.original_utility = module._routes, module._utility_model
        self.original_value, self.original_targeted = module._value, module._targeted_language_rows
        self.max_states, self.max_seconds = 80000, 25.0
        self.tail_mode, self.frame = "cut42", None
        self.records, self.binding_records = [], []

    def recommend(self, observation, period, state, goal, legacy, *, support_profile=None, race_day=False):
        args = observation, period, state, goal, legacy
        kwargs = dict(support_profile=support_profile, race_day=race_day)
        frame = {
            "budget": Budget(states=self.max_states, seconds=self.max_seconds),
            "queue": self.queued(observation, legacy.get("considered", [])),
            "cursor": 0,
            "legacy": legacy,
            "observation": observation,
            "rows": [],
            "provider": None,
            "base_rows": None,
        }
        self.frame, fallback = frame, None
        try:
            result = self.original_recommend(*args, **kwargs, _joint=self)
        except (Unsupported, BudgetExceeded) as error:
            fallback = str(error)
        finally:
            self.frame = None
        if fallback is not None:
            result = self.original_recommend(*args, **kwargs)
        elif frame["rows"]:
            result = {
                **result,
                "reason": (
                    "joint_P0_investment_preferred"
                    if result.get("phase")
                    else "joint_P0_no_current_investment_improvement"
                ),
                "joint_value_scope": {
                    "current_calendar": 36,
                    "future_free_dates": [37, 38, 39, 41],
                    "tail_mode": "cut42",
                    "matched_current_action_increment": True,
                    "legacy_diagnostics_not_joint_decomposition": True,
                },
            }
        return {
            **result,
            "joint192_low": {
                "applied": bool(frame["rows"]) and fallback is None,
                "whole_quote_fallback": fallback,
                "budget": frame["budget"].audit(),
                "rows": frame["rows"],
                "global_binding": deepcopy(self.binding_records),
                "forecast_not_execution_or_payment": True,
                "future_risk_elsewhere_unknown_conditional_readiness": True,
                "future_attribute_marginals_fixed_like_parent": True,
                "mean_cash_and_representative_path_are_different_summaries": True,
            },
        }

    def prepare_current(self, purchase_rows, current_rows, goal):
        self.frame["current_gate_inputs"] = (purchase_rows, current_rows, goal)

    def unpriced(self, row, hp, income):
        inputs = self.frame.get("current_gate_inputs")
        if inputs is None:
            return None
        purchase_rows, current_rows, goal = inputs
        proof = proven_current_rejection(row, purchase_rows=purchase_rows, current_rows=current_rows, goal=goal)
        if proof is None:
            return None
        result = []
        for branch in ("conservative", "ordinary"):
            result.append({
                "actions": [],
                "future_value": 0.0,
                "unfunded_estimate": 0.0,
                "friendship_goal_shortfall": None,
                "points_at_end_estimate": None,
                "final_levels": None,
                "income_branch": branch,
                "forecast_only": True,
                "current_income": income,
                "conditional_on_continuing": True,
                "continuation_feasible": False,
                "halted_at": None,
                "friendship_purchase_ordinal": None,
                "friendship_already_owned": False,
                "current_training_success": True,
                "weight": 0.5,
                "joint_incremental_value": 0.0,
                "joint_reference_absolute_value": None,
                "joint_prefix_path": (),
                "joint_reference_prefix_path": (),
                "future_not_priced_because_existing_current_action_gate": True,
                "current_gate_proof": proof,
                "future_purchase_action_space": {
                    "mode": "unpriced_existing_current_action_gate",
                    "not_an_estimate_of_zero_future_value": True,
                    "purchases_use_main_actions": False,
                },
            })
            self.frame["rows"].append({
                "action_id": row.get("after_action_id") or "train:" + str(row.get("training")),
                "phases": row["purchase_phases"],
                "branch": branch,
                "current_after_HP_percent": hp,
                "current_after_income": income,
                "future_not_priced_because_existing_current_action_gate": True,
                "current_gate_proof": proof,
            })
        return result

    def bind_current(self, result, *, purchase_rows, current_rows, goal):
        from core.auto_trainer.larc_timeline_workflow import selection_key

        updated = filter_globals(result, purchase_rows, current_rows, selection_key)
        self.binding_records.extend(
            deepcopy(r["joint_global_current_action_binding"])
            for r in updated
            if "joint_global_current_action_binding" in r
        )
        output = []
        for row in updated:
            routes = row.get("purchase_routes_v3") or []
            if routes and routes[0].get("future_not_priced_because_existing_current_action_gate"):
                if row.get("rejected") is not True:
                    raise Unsupported("joint_proven_current_gate_not_reconfirmed")
                row = {
                    **row,
                    "future_not_priced_because_existing_current_action_gate": True,
                    "future_current_gate_proof": routes[0]["current_gate_proof"],
                }
            output.append(row)
        return output

    def queued(self, observation, rows):
        result = []
        for row in rows:
            if "score" not in row:
                continue
            try:
                plan = self.m.build_adaptation_plan(observation, phase=row["phase"])
            except ValueError:
                continue
            result.append((row, plan))
        return result

    def targeted(self, *args, **kwargs):
        rows = self.original_targeted(*args, **kwargs)
        frame = self.frame
        if frame is not None:
            frame["queue"].extend(self.queued(frame["observation"], rows))
        return rows

    def utility(self, state, goal, levels, days, **kwargs):
        legacy, unknown = self.original_utility(state, goal, levels, days, **kwargs)
        frame = self.frame
        if frame is None:
            return legacy, unknown
        from core.auto_trainer.larc_opening import LarcOpeningStrategy

        rows = LarcOpeningStrategy(goal).evaluate(state).scored_candidates
        frame["base_rows"] = {r["action_id"]: r for r in rows if not r.get("rejected")}
        frame["provider"] = Provider(self, state, goal, levels, days, legacy)
        wise = frame["base_rows"].get("train:wisdom", {})
        delta = wise.get("planning", {}).get("training_vitality_percent")
        if isinstance(delta, (float, int)) and delta > 0:
            frame["provider"].ordinary_wisdom_heal = delta * 0.65
        return legacy, unknown

    def routes(self, *args, **kwargs):
        frame = self.frame
        if frame is None:
            return self.original_routes(*args, **kwargs)
        now, points, original, days, utility = args
        if now != 36 or not (kwargs.get("focused_first_cycle") and kwargs.get("same_day_purchases")):
            raise Unsupported("joint_unsupported_route_context")
        failure = kwargs.get("failure")
        if type(failure) not in (int, float) or not isfinite(failure) or failure != 0:
            raise Unsupported("joint_current_failure_HP_unknown_keep_original_quote")
        if kwargs["initial"][6] != original[6]:
            raise Unsupported("joint_current_courage_HP_delta_not_covered")
        if not kwargs["phases"] and kwargs["initial_cost"] == 0:
            action = frame["legacy"]["baseline_action"]
        else:
            if frame["cursor"] >= len(frame["queue"]):
                raise Unsupported("joint_current_row_binding_exhausted")
            row, plan = frame["queue"][frame["cursor"]]
            frame["cursor"] += 1
            if (
                list(row["purchase_phases"]) != list(kwargs["phases"])
                or plan["cost"] != kwargs["initial_cost"]
                or list(plan["targets"]) != list(kwargs["initial"])
            ):
                raise Unsupported("joint_current_row_binding_mismatch")
            action = row.get("after_action_id") or "train:" + str(row.get("training"))
        base = frame["base_rows"].get(action)
        if base is None:
            raise Unsupported("joint_current_action_HP_unavailable:" + str(action))
        hp = base.get("planning", {}).get("energy_horizon", {}).get("after_vitality_percent")
        if not isinstance(hp, (float, int)):
            raise Unsupported("joint_current_action_after_HP_unavailable:" + str(action))
        current_income = kwargs["current_income"]
        if action in ("rest.normal", "rest"):
            current_income = 100
        if kwargs["phases"] or kwargs["initial_cost"]:
            unpriced = self.unpriced(row, hp, current_income)
            if unpriced is not None:
                return unpriced
        provider = frame["provider"]
        output = []
        for branch in ("conservative", "ordinary"):
            planner = provider.planner(branch, kwargs.get("allow_language_deferral", False), kwargs["share"])
            post = planner.start(points - kwargs["initial_cost"] + current_income, kwargs["initial"], hp)
            reference = planner.start(points + current_income, original, hp)
            delta = post.value - reference.value
            output.append({
                "actions": [
                    {"ordinal": d, "phases": [s] if isinstance(s, str) else [self.m.PHASES[s]], "cost": c}
                    for d, s, c in post.purchases
                ],
                "future_value": post.raw,
                "unfunded_estimate": post.gap,
                "friendship_goal_shortfall": None,
                "points_at_end_estimate": post.cash,
                "final_levels": list(post.levels),
                "income_branch": branch,
                "forecast_only": True,
                "current_income": current_income,
                "conditional_on_continuing": True,
                "continuation_feasible": post.feasible,
                "halted_at": post.halted_at,
                "friendship_purchase_ordinal": next(
                    (d for d, s, c in post.purchases if s == 7 or s == "willpower_three"), None
                ),
                "friendship_already_owned": original[7] == 3,
                "current_training_success": True,
                "weight": 0.5,
                "joint_incremental_value": delta,
                "joint_reference_absolute_value": reference.value,
                "joint_prefix_path": post.path,
                "joint_reference_prefix_path": reference.path,
                "future_purchase_action_space": {
                    "mode": "joint_firstcamp_cash_HP_nonanticipative_DP",
                    "training_utility_and_income_same_action": True,
                    "purchases_use_main_actions": False,
                },
            })
            frame["rows"].append({
                "action_id": action,
                "phases": kwargs["phases"],
                "branch": branch,
                "current_after_HP_percent": hp,
                "current_after_income": current_income,
                "post_value": post.value,
                "matched_no_purchase_value": reference.value,
                "delta": delta,
                "post_prefix_path": post.path,
                "counter_prefix_path": reference.path,
                "post_gap": post.gap,
                "counter_gap": reference.gap,
            })
        return output

    def value(self, rows):
        if self.frame is not None and rows and all("joint_incremental_value" in r for r in rows):
            return sum(r["weight"] * r["joint_incremental_value"] for r in rows)
        return self.original_value(rows)


def recommend(observation, period, state, goal, legacy, *, parent, support_profile=None, race_day=False):
    from core.auto_trainer import larc_purchase_v3 as purchase

    kwargs = dict(support_profile=support_profile, race_day=race_day)
    now = purchase.period_ordinal((period or {}).get("value") or {})
    if (
        not unified_larc_enabled(goal)
        or now != 36
        or DEPTH.get()
        or race_day
        or not isinstance(observation, dict)
        or observation.get("pending_cost") != 0
    ):
        return parent(observation, period, state, goal, legacy, **kwargs)
    token = DEPTH.set(DEPTH.get() + 1)
    try:
        return Quote(purchase, parent).recommend(observation, period, state, goal, legacy, **kwargs)
    finally:
        DEPTH.reset(token)
