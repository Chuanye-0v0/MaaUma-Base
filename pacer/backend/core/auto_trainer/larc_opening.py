from __future__ import annotations

from copy import deepcopy
from typing import Any

_BASELINE_CACHE: dict = {}
_SCENARIO_CACHE_KEYS = (
    'larc_period', 'larc_ss_preview', 'larc_ss_entry', 'larc_expedition_context',
    'larc_growth_observation', 'larc_strategy181_observation', 'larc_ss_nonfull_entry_current',
    'larc_supporter_goal', 'larc_fan_plan', 'larc_friend_outing', 'larc_attribute_history',
    'larc_career_joint_intent', 'larc_first_expedition_reference', 'larc_ss_wins',
    'support_card_profile', 'larc_observed_deck',
)

from core.auto_trainer.config import UGParentGoal
from core.auto_trainer.grand_masters import StrategyResult
from core.auto_trainer.larc_supporter_goal import deadline_larc_supporter_goal
from core.auto_trainer.larc_training_value import (
    forecast_evaluation, score_training_value, training_hard_issues, training_soft_issues,
)
from core.auto_trainer.rejection_policy import discount_soft_score, is_hard_rejected
from core.auto_trainer.thresholds import MIN_TRAINING_PAGES
from core.auto_trainer.larc_friend_outing import recovery_candidates
from core.auto_trainer.larc_policy import policy_version, strategy_v4_enabled
from core.auto_trainer.larc_attribute_plan import planning_evaluation
from core.auto_trainer.larc_recovery_timing import apply_recovery_timing_preference
from core.auto_trainer.larc_training_preferences import apply_training_preferences
from core.auto_trainer.state import STAT_KEYS, TRAINING_TYPES, TrainingCandidate, TrainingTurnState


class LarcOpeningStrategy:
    """Local full-run policy; retains its original class name for integrations."""

    MIN_CONFIDENCE = 0.65
    DEFAULT_FAILURE_LIMIT = 25.0

    def __init__(self, goal: UGParentGoal):
        self.goal = goal

    @staticmethod
    def failure_limit(goal: UGParentGoal, state=None) -> float:
        from core.auto_trainer.larc_strategy_rules import effective_failure_limit
        return float(effective_failure_limit(goal, state))

    def validation_stop(self, state: TrainingTurnState) -> StrategyResult | None:
        """State eligibility only; do not score candidates before SS capture."""
        if state.screen != "training_menu":
            return self._stop("Unsupported screen", "unsupported_screen")
        if self.goal.scenario != "larc" or state.scenario_state.get("scenario") != "larc":
            return self._stop("Scenario binding is not L'Arc", "scenario_binding_mismatch")
        if deadline_larc_supporter_goal(state.scenario_state.get("larc_supporter_goal")):
            return self._stop(
                "Supporter goal deadline unresolved: no executable SS; ordinary training/rest/outing forbidden",
                "goal_deadline_unresolved",
            )
        period = state.scenario_state.get("larc_period", {})
        if not 1 <= state.turn <= self.goal.total_turns:
            return self._stop("L'Arc collection is outside the observed sampling period", "larc_collection_turn_limit")
        if period.get("status") == "verified":
            value = period.get("value", {})
            year, month = value.get("year_index"), value.get("month")
            if year != 1 and not (
                year in {2, 3} and value.get("stage") == {2: "classic", 3: "senior"}[year]
                and type(month) is int and 1 <= month <= 12
                and value.get("half") in {"first", "second"}
            ):
                return self._stop("Current game date is not verified", "larc_collection_period_required")
        elif state.turn > 2:
            return self._stop("Current game year is not verified", "larc_collection_period_required")
        if len({item.training_type for item in state.training_options}) < MIN_TRAINING_PAGES:
            return self._stop(
                "Too few training pages remain readable after collection",
                "larc_training_pages_insufficient",
            )
        if type(state.vitality) is not int or not 0 <= state.vitality <= 100:
            return self._stop("Current vitality is unreadable", "larc_numeric_state_unreliable")
        return None

    @planning_evaluation
    def evaluate(self, state: TrainingTurnState) -> StrategyResult:
        if self._unknown_attributes(state) or len(state.training_options) != len(TRAINING_TYPES):
            return self.evaluate_baseline(state)
        result = self._evaluate_ranked(state)
        if result.reason == 'year1_b197_unified':
            return result
        from core.auto_trainer.larc_year1_engine import fallback_reason as b197_fallback_reason
        fallback = b197_fallback_reason()
        if fallback:
            from dataclasses import replace
            result = replace(result, reason='year1_b197_fallback_parent:' + fallback + '; ' + result.reason)
        from core.auto_trainer.larc_strategy_params import unified_larc_enabled
        if unified_larc_enabled(self.goal):
            from core.auto_trainer.larc_return_budget import apply_local
            result = apply_local(state, self.goal, result)
            from core.auto_trainer.larc_joint_career.binding import apply_local as apply_joint_career
            return apply_joint_career(state, self.goal, result)
        return result

    def _evaluate_ranked(self, state: TrainingTurnState) -> StrategyResult:
        baseline = self.evaluate_baseline(state)
        if baseline.reason == 'year1_b197_unified':
            return baseline
        from core.auto_trainer.larc_tail263.production import apply_ranking
        inherited = apply_ranking(state, self.goal, baseline)
        from core.auto_trainer.larc_departure_planner import apply_ranking as apply_departure18
        return apply_departure18(state, self.goal, inherited)

    @planning_evaluation
    @forecast_evaluation
    def evaluate_baseline(self, state: TrainingTurnState) -> StrategyResult:
        cache_key = None
        try:
            if isinstance(state.scenario_state, dict):
                sc = state.scenario_state
                from core.auto_trainer.larc_strategy_params import params_for_goal
                from core.auto_trainer.larc_strategy_rules import current_first_arc_scope_key
                cache_key = (
                    id(state),
                    id(self.goal),
                    params_for_goal(self.goal),
                    state.screen,
                    state.turn,
                    state.vitality,
                    getattr(state, 'motivation', None),
                    sc.get('motivation'),
                    tuple(sorted((state.current_stats or {}).items())),
                    tuple(id(c) for c in (state.training_options or ())),
                    tuple(state.warnings or ()),
                    tuple(state.errors or ()),
                    tuple((k, id(sc[k])) for k in sorted(sc)),
                    current_first_arc_scope_key(),
                )
        except Exception:
            cache_key = None
        if cache_key is not None and cache_key in _BASELINE_CACHE:
            cached_res, cached_fb = _BASELINE_CACHE[cache_key]
            try:
                from core.auto_trainer.larc_year1_engine import _FALLBACK
                _FALLBACK.set(cached_fb)
            except Exception:
                pass
            return deepcopy(cached_res)
        res = self._evaluate_baseline_uncached(state)
        if cache_key is not None:
            if len(_BASELINE_CACHE) >= 32:
                _BASELINE_CACHE.clear()
            try:
                from core.auto_trainer.larc_year1_engine import fallback_reason as _b197_fb
                fb_val = _b197_fb()
            except Exception:
                fb_val = None
            _BASELINE_CACHE[cache_key] = (deepcopy(res), fb_val)
        return res

    def _evaluate_baseline_uncached(self, state: TrainingTurnState) -> StrategyResult:
        from core.auto_trainer.larc_ss_opportunity import with_planning_horizon
        state=with_planning_horizon(state,self.goal)
        stopped = self.validation_stop(state)
        if stopped is not None:
            return stopped
        unknown = self._unknown_attributes(state)
        if unknown:
            partial = self._evaluate_partial_attributes(state, unknown)
            from core.auto_trainer.larc_year1_engine import enabled as b197_enabled, choose as choose_b197, decorate_rows as decorate_b197_rows
            if b197_enabled(self.goal):
                b197 = choose_b197(state, self.goal, partial.scored_candidates)
                if b197 is not None:
                    rows = decorate_b197_rows(partial.scored_candidates, b197)
                    candidate = next((row for row in rows if row.get('action_id') == b197['action_id']), None)
                    if candidate is not None:
                        action = candidate.get('action', 'train' if candidate['action_id'].startswith('train:') else 'rest')
                        target = candidate.get('target', candidate.get('training_type') if action == 'train' else None)
                        return StrategyResult(action=action, target=target, confidence=candidate.get('source_confidence', 1.0),
                            reason='year1_b197_unified', risk_flags=list(candidate.get('risk_flags', [])), scored_candidates=rows)
                from dataclasses import replace
                return replace(partial, reason='year1_b197_fallback_partial_no_scorable_pool: ' + partial.reason)
            return partial
        from core.auto_trainer.larc_ss_energy import applicable
        from core.auto_trainer.larc_training_value import _future_numeric_values
        # This synchronous baseline owns one unchanged-state short-energy
        # reference. Every energy consumer still receives an independent copy.
        reference = _future_numeric_values(state, self.goal) if applicable(state, self.goal) else None
        scored = [self._score_candidate(state, item, future_reference=reference) for item in state.training_options]
        scored.extend(recovery_candidates(state, self.goal, future_reference=reference))
        apply_recovery_timing_preference(state, self.goal, scored)
        apply_training_preferences(state, self.goal, scored)
        from core.auto_trainer.larc_score_tuning import apply as apply_score_tuning
        tuning=apply_score_tuning(state,self.goal,scored)
        from core.auto_trainer.larc_close_training_balance import apply_close_training_balance
        apply_close_training_balance(state, self.goal, scored)
        from core.auto_trainer.larc_strategy_params import unified_larc_enabled
        if unified_larc_enabled(self.goal):
            from core.auto_trainer.larc_recovery_policy import apply_recovery_rules
            apply_recovery_rules(state, self.goal, scored)
            from core.auto_trainer.larc_strategy_rules import apply_weak_training_recovery_comparison
            apply_weak_training_recovery_comparison(state, self.goal, scored, reference)
        from core.auto_trainer.larc_year1_engine import choose as choose_b197, decorate_rows as decorate_b197_rows
        b197 = choose_b197(state, self.goal, scored)
        if b197 is not None:
            selected = next((row for row in scored if row.get('action_id') == b197['action_id']), None)
            if selected is not None:
                scored = decorate_b197_rows(scored, b197)
                candidate = next(row for row in scored if row.get('action_id') == b197['action_id'])
                action = candidate.get('action', 'train' if candidate['action_id'].startswith('train:') else 'rest')
                target = candidate.get('target', candidate.get('training_type') if action == 'train' else None)
                return StrategyResult(action=action, target=target, confidence=candidate.get('source_confidence', 1.0),
                    reason='year1_b197_unified', risk_flags=list(candidate.get('risk_flags', [])), scored_candidates=scored)
        from core.auto_trainer.larc_timeline_workflow import selection_key
        eligible = [item for item in scored if not is_hard_rejected(item)]
        eligible.sort(key=selection_key)
        if not eligible or eligible[0]["score"] <= 0 and not tuning.get('changed'):
            for item in scored:
                if not is_hard_rejected(item) and item["score"] <= 0:
                    item["risk_flags"].append("nonpositive_training_value")
            reason = "Observed options contain no compliant positive-value training; use rest"
            return StrategyResult(
                action="rest",
                target=None,
                confidence=0.9,
                reason=reason,
                risk_flags=[],
                scored_candidates=sorted((item for item in scored if item["action_id"] != "rest.normal"),
                                         key=lambda item: item["action_id"]) + [{
                    "action_id": "rest.normal", "score": 0.0, "rejected": False,
                    "policy_version": policy_version(self.goal),
                    "reason": reason, "risk_flags": [],
                }],
            )

        best = eligible[0]
        from core.auto_trainer.larc_boundary_readiness import boundary_readiness
        boundary = boundary_readiness(state, self.goal, eligible)
        if boundary.get('changed'):
            best = next(row for row in eligible if row['action_id']==boundary['selected_action_id'])
        if boundary.get('reason') not in {'revision_not_enabled','not_last_free_domestic_action'}:
            best.setdefault('planning',{})['boundary_readiness']=boundary
        from core.auto_trainer.larc_ss_phase import year1_full_comparison_status
        fair_full = year1_full_comparison_status(state, self.goal)
        if fair_full['active']:
            # The SS comparator selects from this same complete tier/score
            # pool. A later normal decision must keep the same local target.
            best = min(eligible, key=selection_key)
            best.setdefault('planning', {})['year1_full_local_pool'] = {
                'basis': 'same_complete_current_tier_score_pool_as_SS_comparison',
                'selected_action_id': best['action_id'], 'selected_score': best['score'],
                'extra_preference_override_applied': False,
            }
        if best.get("action") in {"outing", "rest"}:
            reason = "Compared phase-adjusted training, available Mei outing and ordinary recovery"
            if all(is_hard_rejected(row) for row in scored if row["action_id"].startswith("train:")):
                reason = "Observed no compliant positive-value training; selected available recovery"
            return StrategyResult(action=best["action"], target=best["target"],
                confidence=best["source_confidence"],
                reason=reason,
                risk_flags=list(best["risk_flags"]),
                scored_candidates=sorted(scored, key=selection_key))
        return StrategyResult(
            action="train",
            target=best["training_type"],
            confidence=best["source_confidence"],
            reason=(
                f"L'Arc local policy selected {best['training_type']} "
                "by dynamic attribute gaps, phase-adjusted gains, startup progress, failure risk and energy value"
            ),
            risk_flags=list(best["risk_flags"]),
            scored_candidates=sorted(
                scored, key=selection_key
            ),
        )

    def _score_candidate(
        self, state: TrainingTurnState, candidate: TrainingCandidate, *, future_reference=None
    ) -> dict[str, Any]:
        soft_flags = training_soft_issues(candidate, min_confidence=self.MIN_CONFIDENCE,
            strategy_v4=strategy_v4_enabled(self.goal))
        source_confidence = candidate.confidence
        failure_limit = self.failure_limit(self.goal, state)
        hard_flags = training_hard_issues(candidate, failure_limit)
        risk_flags = [*soft_flags, *hard_flags]

        authorized_support = candidate.raw_data.get("effective_support_count")
        support_known = type(authorized_support) is int and authorized_support >= 0
        if not support_known:
            risk_flags.append("support_count_unknown")

        valuation = score_training_value(state, candidate, self.goal, future_reference=future_reference)
        from core.auto_trainer.larc_strategy_rules import dynamic_risk_score_adjustment, pre_niel_vitality_adjustment
        niel_adj = pre_niel_vitality_adjustment(state, self.goal, valuation, candidate)
        if niel_adj:
            valuation['components']['pre_niel_vitality_safety'] = niel_adj
            valuation['score'] = round(valuation['score'] + niel_adj, 4)
        dyn_risk_adj = dynamic_risk_score_adjustment(state, self.goal, valuation, candidate)
        if dyn_risk_adj:
            valuation['components']['dynamic_risk_posture'] = dyn_risk_adj
            valuation['score'] = round(valuation['score'] + dyn_risk_adj, 4)
        if soft_flags:
            score = discount_soft_score(valuation['score'], True)
            valuation['components']['soft_risk_discount'] = round(score - valuation['score'], 4)
            valuation['score'] = score
        support_value = (valuation["components"]["effective_bond"]
                         if valuation["features"]["effective_support_bond"]["value"] is not None else None)
        return {
            **valuation,
            "action_id": candidate.action_id,
            "training_type": candidate.training_type,
            "rejected": bool(hard_flags),
            "hard_rejected": bool(hard_flags),
            "soft_risk": bool(soft_flags),
            "hard_flags": hard_flags,
            "soft_flags": soft_flags,
            "source_confidence": source_confidence,
            "failure_rate": candidate.failure_rate,
            "failure_rate_limit": failure_limit,
            "support_count": authorized_support if support_known else None,
            "support_count_known": support_known,
            "support_value": support_value,
            "risk_flags": risk_flags,
        }

    @staticmethod
    def _unknown_attributes(state):
        return [key for key in STAT_KEYS if type(state.current_stats.get(key)) is not int
                or state.current_stats[key] < 0]

    def _evaluate_partial_attributes(self, state, unknown):
        from core.auto_trainer.larc_gain_evidence import gain_evidence
        from core.auto_trainer.larc_phase import phase_weights
        from core.auto_trainer.larc_recovery import points_to_percent, recovery_value

        # Use only visible gains and energy. Absolute attribute gaps and
        # forecasts are not inferred from missing main-menu numbers.
        weights = phase_weights(state, self.goal)
        failure_limit = self.failure_limit(self.goal, state)
        rows = []
        for option in state.training_options:
            unverified = set(gain_evidence(option)['unverified_used_attributes'])
            numeric = sum(max(0, gain) * weights['stats'] for key, gain in option.stats_gain.items()
                          if key in TRAINING_TYPES and key not in unverified and type(gain) is int)
            points = option.stats_gain.get('skill_pt')
            if 'skill_pt' not in unverified and type(points) is int:
                numeric += max(0, points) * weights['skill_pt']
            rate = option.failure_rate
            if rate is not None:
                numeric = numeric * (1 - rate / 100) - rate * .5
            cost = option.vitality_delta
            if cost is None:
                cost = 5 if option.training_type == 'wisdom' else -20
            energy = recovery_value(state, points_to_percent(state, cost))
            hard = training_hard_issues(option, failure_limit)
            soft = ['current_attributes_unreadable', *training_soft_issues(option)]
            score = discount_soft_score(round(numeric + energy, 4), True)
            rows.append({'action_id': option.action_id, 'action': 'train', 'target': option.training_type,
                'training_type': option.training_type, 'score': score, 'failure_rate': rate,
                'failure_rate_limit': failure_limit, 'rejected': bool(hard), 'hard_rejected': bool(hard),
                'soft_risk': True, 'risk_flags': [*hard, *soft], 'hard_flags': hard, 'soft_flags': soft,
                'unknown_fields': list(unknown), 'source_confidence': option.confidence,
                'policy_version': policy_version(self.goal), 'score_basis': 'discounted_visible_gains_without_attribute_gaps'})
        rows.append({'action_id': 'rest.normal', 'action': 'rest', 'target': None, 'score': 0.,
                     'policy_version': policy_version(self.goal), 'source_confidence': .9,
                     'risk_flags': [], 'hard_rejected': False})
        rows.sort(key=lambda row: row['score'], reverse=True)
        selected = next(row for row in rows if not is_hard_rejected(row))
        return StrategyResult(action=selected['action'], target=selected['target'],
            confidence=selected['source_confidence'], reason='Compared visible gains while current attributes were unreadable',
            risk_flags=selected['risk_flags'], scored_candidates=rows)

    @staticmethod
    def _stop(reason: str, flag: str) -> StrategyResult:
        return StrategyResult(
            action="stop",
            target=None,
            confidence=0.0,
            reason=reason,
            risk_flags=[flag],
            scored_candidates=[],
        )
