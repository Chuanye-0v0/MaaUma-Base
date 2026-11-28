"""Bounded, deterministic optimizer for offline final skill planning."""
from __future__ import annotations
import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from core.skill_planner.candidate_set import AggregatedSkillCandidate, SkillCandidateSet, candidate_set_sha_matches
from core.skill_planner.catalog import SkillCatalog
from core.skill_planner.models import PlannedPurchase, SkillCatalogRow, SkillPurchaseContext, SkillPurchasePlan, SkippedSkill
from core.skill_planner.preference_rules import EffectiveSkillRule, resolve_effective_skill_rule
from core.skill_planner.preferences import SkillPreferenceProfile
from core.skill_planner.scoring import base_score_and_bucket, condition_matches, hint_discounted_cost, is_covered_by_owned_upper
PLAN_SCHEMA = 'uma_skill_purchase_plan.v1'
_SHA256_PATTERN = re.compile('[0-9a-f]{64}')

@dataclass(frozen=True, slots=True)
class OptimizerLimits:
    max_candidates: int = 48
    max_states: int = 100000

    def __post_init__(self) -> None:
        if type(self.max_candidates) is not int or type(self.max_states) is not int or self.max_candidates < 1 or (self.max_states < 1):
            raise ValueError('optimizer_limits_invalid')

@dataclass(frozen=True, slots=True)
class GlobalOptimizationResult:
    purchases: tuple[PlannedPurchase, ...]
    skipped: tuple[SkippedSkill, ...]
    unmet_required_skill_ids: tuple[str, ...]
    spent_skill_pt: int
    remaining_skill_pt: int
    objective: tuple[object, ...]
    explored_states: int
    status: str

@dataclass(frozen=True, slots=True)
class _Candidate:
    skill: AggregatedSkillCandidate
    row: SkillCatalogRow
    rule: EffectiveSkillRule
    hint_level: int
    final_cost: int
    utility: float
    bucket: str

@dataclass(frozen=True, slots=True)
class _Option:
    root_id: str
    selected_ids: frozenset[str]

def optimize_final_skill_build(context: SkillPurchaseContext, catalog: SkillCatalog, profile: SkillPreferenceProfile, candidate_set: SkillCandidateSet, *, limits: OptimizerLimits=OptimizerLimits()) -> SkillPurchasePlan:
    """Optimize one final build entirely offline; it never authorizes execution."""
    mismatch = _candidate_set_mismatch(context, profile, candidate_set)
    if mismatch is not None:
        return _blocked_plan(context, profile, candidate_set, mismatch)
    candidates, skipped = _build_candidates(context, catalog, profile, candidate_set)
    if len(candidates) > limits.max_candidates:
        return _blocked_plan(context, profile, candidate_set, 'optimizer_candidate_limit_exceeded', skipped=skipped)
    must_targets = tuple(sorted((item.skill.skill_id for item in candidates.values() if item.rule.mode == 'must_buy_if_safe'), key=lambda skill_id: (-candidates[skill_id].rule.must_priority, skill_id)))
    options = _build_options(candidates, context.owned_skills, skipped, allow_gold_without_lower=context.policy.allow_gold_without_lower)
    if profile.filler_policy == 'spend_all_safe':
        core_options = tuple((option for option in options if _is_core_candidate(candidates[option.root_id])))
        core_selected, core_explored, limit_exceeded = _search_options(core_options, candidates, context, profile, must_targets, limits)
        if limit_exceeded:
            selected = core_selected
            explored_states = core_explored
        else:
            remaining_state_limit = max(1, limits.max_states - core_explored + 1)
            selected, filler_explored, limit_exceeded = _search_options(options, candidates, context, profile, must_targets, OptimizerLimits(max_candidates=limits.max_candidates, max_states=remaining_state_limit), initial_selected=core_selected)
            explored_states = core_explored + filler_explored - 1
    else:
        selected, explored_states, limit_exceeded = _search_options(options, candidates, context, profile, must_targets, limits)
    bounded = limit_exceeded and bool(selected) and (profile.profile_id == 'harvest-terminal-spenddown')
    if limit_exceeded and (not bounded):
        return _blocked_plan(context, profile, candidate_set, 'optimizer_state_limit_exceeded', skipped=skipped, explored_states=explored_states)
    objective = _objective(selected, candidates, context, profile, must_targets)
    purchases = _purchases_for_selection(selected, candidates)
    spent = sum((item.final_cost for item in purchases))
    remaining = context.current_skill_pt - spent
    unmet = tuple((skill_id for skill_id in must_targets if skill_id not in selected))
    status = 'partial' if unmet else 'ok'
    warnings = ['unmet_required_skills'] if unmet else []
    if bounded:
        warnings.append('optimizer_state_limit_best_available')
    if profile.filler_policy == 'spend_all_safe' and remaining > 0:
        warnings.extend(_spenddown_warnings(selected, options, candidates, context, profile, candidate_set))
    plan = SkillPurchasePlan(schema_version=PLAN_SCHEMA, target_mode=context.target_mode, trigger=context.trigger, status=status, purchases=list(purchases), skipped=sorted(skipped, key=lambda item: (item.skill_id, item.reason_code)), reserved_skill_pt=_reserved_skill_pt(context, profile), spent_skill_pt=spent, remaining_skill_pt=remaining, warnings=warnings, risk_flags=list(candidate_set.risk_flags), explain='Offline global optimizer selected a dependency-safe final build.' + (f" Spend-down stopped: {', '.join(warnings)}." if warnings else ''), profile_id=profile.profile_id, profile_sha256=profile.profile_sha256, candidate_set_sha256=candidate_set.candidate_set_sha256, unmet_required_skill_ids=list(unmet), optimizer_status='bounded_best_available' if bounded else 'ok', optimizer_explored_states=explored_states, execution_allowed=False)
    return replace(plan, plan_sha256=_plan_sha(plan, objective))

def _candidate_set_mismatch(context: SkillPurchaseContext, profile: SkillPreferenceProfile, candidate_set: SkillCandidateSet) -> str | None:
    if candidate_set.profile_sha256 != profile.profile_sha256:
        return 'candidate_set_profile_sha_mismatch'
    if candidate_set.source_capture_sha256 != context.source_capture_sha256:
        return 'candidate_set_capture_sha_mismatch'
    if not candidate_set_sha_matches(candidate_set):
        return 'candidate_set_sha_mismatch'
    if any((not _candidate_protocol_valid(item) for item in candidate_set.candidates)):
        return 'candidate_set_candidate_invalid'
    return None

def _build_candidates(context: SkillPurchaseContext, catalog: SkillCatalog, profile: SkillPreferenceProfile, candidate_set: SkillCandidateSet) -> tuple[dict[str, _Candidate], list[SkippedSkill]]:
    candidates: dict[str, _Candidate] = {}
    skipped: list[SkippedSkill] = []
    owned = set(context.owned_skills)
    minimum_utility = 45.0 if profile.base_profile == 'parent' else 50.0
    for skill in candidate_set.candidates:
        row = catalog.maybe_get(skill.skill_id)
        if row is None:
            skipped.append(_skip(skill.skill_id, skill.skill_id, 'unknown_skill'))
            continue
        if skill.recognition_confidence < context.policy.min_confidence:
            skipped.append(_skip(row.skill_id, row.name_zh_cn, 'recognition_confidence_low'))
            continue
        if skill.skill_id in owned:
            skipped.append(_skip(row.skill_id, row.name_zh_cn, 'already_owned'))
            continue
        if is_covered_by_owned_upper(row.skill_id, row, owned, catalog.rows):
            skipped.append(_skip(row.skill_id, row.name_zh_cn, 'covered_by_upper'))
            continue
        if not row.is_verified and (not context.policy.allow_unverified_skills):
            skipped.append(_skip(row.skill_id, row.name_zh_cn, 'unverified_not_allowed'))
            continue
        if context.policy.require_condition_match and (not condition_matches(context, row)):
            skipped.append(_skip(row.skill_id, row.name_zh_cn, 'condition_mismatch'))
            continue
        try:
            rule = resolve_effective_skill_rule(row, profile)
        except ValueError:
            skipped.append(_skip(row.skill_id, row.name_zh_cn, 'effective_preference_rule_conflict'))
            continue
        if rule.mode == 'avoid':
            skipped.append(_skip(row.skill_id, row.name_zh_cn, 'preference_avoid'))
            continue
        hint_level, final_cost = _hint_and_cost(context, skill)
        utility, bucket = _utility_and_bucket(row, context, profile)
        utility += rule.prefer_weight
        if profile.filler_policy == 'spend_all_safe' and rule.mode != 'must_buy_if_safe' and (utility <= 0.0):
            skipped.append(_skip(row.skill_id, row.name_zh_cn, 'nonpositive_utility'))
            continue
        if rule.mode != 'must_buy_if_safe' and profile.filler_policy == 'safe_useful_only' and (utility < minimum_utility):
            skipped.append(_skip(row.skill_id, row.name_zh_cn, 'below_minimum_useful_score'))
            continue
        candidates[row.skill_id] = _Candidate(skill=skill, row=row, rule=rule, hint_level=hint_level, final_cost=final_cost, utility=utility, bucket=bucket)
    return _apply_marginal_utilities(candidates, catalog, context, profile, skipped, minimum_utility=minimum_utility)

def _apply_marginal_utilities(candidates: dict[str, _Candidate], catalog: SkillCatalog, context: SkillPurchaseContext, profile: SkillPreferenceProfile, skipped: list[SkippedSkill], *, minimum_utility: float) -> tuple[dict[str, _Candidate], list[SkippedSkill]]:
    owned = set(context.owned_skills)
    adjusted = dict(candidates)
    for skill_id in sorted(candidates):
        candidate = candidates[skill_id]
        if candidate.row.mechanics is None or candidate.row.semantic_provenance is None or (not candidate.row.semantic_provenance.live_eligible):
            continue
        lower_ids = tuple(candidate.row.relations.get('lower', []))
        active_lower_ids = tuple((lower_id for lower_id in lower_ids if lower_id in owned or lower_id in candidates))
        if not active_lower_ids:
            continue
        lower_utilities: list[float] = []
        for lower_id in active_lower_ids:
            lower_row = catalog.maybe_get(lower_id)
            if lower_row is None or lower_row.mechanics is None or lower_row.semantic_provenance is None or (not lower_row.semantic_provenance.live_eligible):
                continue
            lower_utility, _ = _utility_and_bucket(lower_row, context, profile)
            lower_utilities.append(lower_utility)
        if not lower_utilities:
            continue
        marginal_utility = max(0.0, candidate.utility - max(lower_utilities))
        if marginal_utility <= 0.0 and candidate.rule.mode != 'must_buy_if_safe':
            adjusted.pop(skill_id, None)
            skipped.append(_skip(candidate.row.skill_id, candidate.row.name_zh_cn, 'nonpositive_marginal_utility'))
            continue
        if candidate.rule.mode != 'must_buy_if_safe' and profile.filler_policy == 'safe_useful_only' and (marginal_utility < minimum_utility):
            adjusted.pop(skill_id, None)
            skipped.append(_skip(candidate.row.skill_id, candidate.row.name_zh_cn, 'below_minimum_marginal_utility'))
            continue
        adjusted[skill_id] = replace(candidate, utility=marginal_utility)
    return (adjusted, skipped)

def _hint_and_cost(context: SkillPurchaseContext, skill: AggregatedSkillCandidate) -> tuple[int, int]:
    hint_level = skill.observed_hint_level
    if hint_level is None:
        hint_level = context.hint_levels.get(skill.skill_id, 0)
    if skill.cost_basis == 'observed_final':
        return (int(hint_level), skill.display_cost)
    return (int(hint_level), hint_discounted_cost(skill.display_cost, hint_level))

def _candidate_protocol_valid(skill: AggregatedSkillCandidate) -> bool:
    if type(skill.skill_id) is not str or not skill.skill_id:
        return False
    if type(skill.display_cost) is not int or skill.display_cost <= 0:
        return False
    if not _is_finite_probability(skill.recognition_confidence):
        return False
    if type(skill.cost_basis) is not str or skill.cost_basis not in {'base', 'observed_final'}:
        return False
    if skill.observed_hint_level is not None and (not _is_int_in_range(skill.observed_hint_level, 0, 5)):
        return False
    if skill.observed_discount_percent is not None and (not _is_int_in_range(skill.observed_discount_percent, 0, 100)):
        return False
    return _is_sha(skill.identity_binding_sha256)

def _is_finite_probability(value: object) -> bool:
    return type(value) in {int, float} and math.isfinite(float(value)) and (0.0 <= float(value) <= 1.0)

def _is_int_in_range(value: object, lower: int, upper: int) -> bool:
    return type(value) is int and lower <= value <= upper

def _utility_and_bucket(row: SkillCatalogRow, context: SkillPurchaseContext, profile: SkillPreferenceProfile) -> tuple[float, str]:
    base, bucket = base_score_and_bucket(row, context.target_mode)
    if row.base_utility is not None and math.isfinite(row.base_utility):
        base = row.base_utility
    if row.type == 'speed' and row.conditions.get('style') not in (None, ['any']):
        base += 2.0
    return (float(base), bucket)

def _build_options(candidates: dict[str, _Candidate], owned_skills: list[str], skipped: list[SkippedSkill], *, allow_gold_without_lower: bool) -> tuple[_Option, ...]:
    owned = set(owned_skills)
    options: list[_Option] = []
    for root_id in sorted(candidates, key=lambda item: _option_sort_key(candidates[item])):
        closure, reason = _prerequisite_closure(root_id, candidates, owned, allow_gold_without_lower=allow_gold_without_lower)
        if reason is not None:
            root = candidates[root_id]
            skipped.append(_skip(root_id, root.row.name_zh_cn, reason))
            continue
        options.append(_Option(root_id=root_id, selected_ids=frozenset(closure)))
    return tuple(options)

def _option_sort_key(candidate: _Candidate) -> tuple[int, float, int, str]:
    must_rank = -candidate.rule.must_priority if candidate.rule.mode == 'must_buy_if_safe' else 0
    return (must_rank, -candidate.utility, candidate.final_cost, candidate.skill.skill_id)

def _is_core_candidate(candidate: _Candidate) -> bool:
    return candidate.bucket != 'cheap_filler' or candidate.rule.mode == 'must_buy_if_safe' or candidate.rule.prefer_weight > 0.0

def _prerequisite_closure(root_id: str, candidates: dict[str, _Candidate], owned: set[str], *, allow_gold_without_lower: bool) -> tuple[set[str], str | None]:
    selected: set[str] = set()
    visiting: set[str] = set()

    def visit(skill_id: str) -> str | None:
        if skill_id in owned:
            return None
        if skill_id in visiting:
            return 'prerequisite_cycle'
        candidate = candidates.get(skill_id)
        if candidate is None:
            return 'missing_prerequisite'
        if skill_id in selected:
            return None
        visiting.add(skill_id)
        if not allow_gold_without_lower:
            for lower_id in sorted(candidate.row.relations.get('lower', [])):
                error = visit(lower_id)
                if error is not None:
                    return error
        visiting.remove(skill_id)
        selected.add(skill_id)
        return None
    return (selected, visit(root_id))

def _search_options(options: tuple[_Option, ...], candidates: dict[str, _Candidate], context: SkillPurchaseContext, profile: SkillPreferenceProfile, must_targets: tuple[str, ...], limits: OptimizerLimits, *, initial_selected: frozenset[str]=frozenset()) -> tuple[frozenset[str], int, bool]:
    states: dict[frozenset[str], None] = {initial_selected: None}
    explored = 1
    batch_cap = profile.max_batch_size
    if context.policy.max_purchase_count is not None:
        batch_cap = min(batch_cap, context.policy.max_purchase_count)
    if profile.max_batch_size > 12:
        if context.policy.allow_gold_without_lower and all((len(option.selected_ids) == 1 for option in options)) and all((candidate.utility.is_integer() for candidate in candidates.values())) and (sum((abs(candidate.utility) for candidate in candidates.values())) < 2 ** 53):
            grouped = _search_upgrade_groups(options, candidates, context, profile, must_targets, limits, initial_selected, batch_cap)
            if grouped is not None:
                return grouped
        all_selected = initial_selected.union(*(option.selected_ids for option in options))
        if len(all_selected) <= batch_cap and all((candidates[item].utility >= 0 for item in all_selected - initial_selected)) and _within_budget(all_selected, candidates, context, profile) and (not (context.policy.allow_gold_without_lower and _contains_auto_included_lower_overlap(all_selected, candidates))):
            explored = 1 + int(all_selected != initial_selected)
            return (all_selected, min(explored, limits.max_states), explored > limits.max_states)
        if all((len(option.selected_ids) == 1 for option in options)) and (not context.policy.allow_gold_without_lower) and all((candidate.utility.is_integer() for candidate in candidates.values())) and (sum((abs(candidate.utility) for candidate in candidates.values())) < 2 ** 53):
            return _search_independent_options(options, candidates, context, profile, must_targets, limits, initial_selected=initial_selected, batch_cap=batch_cap)
    for option in options:
        for selected in tuple(sorted(states, key=lambda item: tuple(sorted(item)))):
            merged = selected | option.selected_ids
            if merged == selected or len(merged) > batch_cap:
                continue
            if context.policy.allow_gold_without_lower and _contains_auto_included_lower_overlap(merged, candidates):
                continue
            if not _within_budget(merged, candidates, context, profile):
                continue
            if merged not in states:
                if explored >= limits.max_states:
                    return (max(states, key=lambda s: _objective(s, candidates, context, profile, must_targets)), explored, True)
                states[merged] = None
                explored += 1
    best = max(states, key=lambda selected: _objective(selected, candidates, context, profile, must_targets))
    return (best, explored, False)

def _search_upgrade_groups(options: tuple[_Option, ...], candidates: dict[str, _Candidate], context: SkillPurchaseContext, profile: SkillPreferenceProfile, must_targets: tuple[str, ...], limits: OptimizerLimits, initial_selected: frozenset[str], batch_cap: int) -> tuple[frozenset[str], int, bool] | None:
    """Multiple-choice knapsack: one displayed upgrade per linear skill family."""
    ids = [option.root_id for option in options]
    neighbors = {key: set() for key in ids}
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            if _contains_auto_included_lower_overlap(frozenset((left, right)), candidates):
                neighbors[left].add(right)
                neighbors[right].add(left)
    groups, seen = ([], set())
    for key in ids:
        if key in seen:
            continue
        group, pending = (set(), [key])
        while pending:
            item = pending.pop()
            if item not in group:
                group.add(item)
                pending.extend(neighbors[item] - group)
        if any((group - {item} - neighbors[item] for item in group)):
            return None
        seen.update(group)
        groups.append([item for item in ids if item in group])
    objective = lambda selected: _objective(selected, candidates, context, profile, must_targets)
    count_matters = len(initial_selected) + len(groups) > batch_cap
    initial_cost = sum((candidates[item].final_cost for item in initial_selected))
    states = {(initial_cost, len(initial_selected) if count_matters else 0): initial_selected}
    explored = 1
    for group in groups:
        if any((item in initial_selected for item in group)):
            continue
        next_states = dict(states)
        for (cost, count), selected in states.items():
            for item in group:
                merged = selected | {item}
                if len(merged) > batch_cap or not _within_budget(merged, candidates, context, profile) or _contains_auto_included_lower_overlap(merged, candidates):
                    continue
                key = (cost + candidates[item].final_cost, count + 1 if count_matters else 0)
                previous = next_states.get(key)
                if previous is None or objective(merged) > objective(previous):
                    if explored >= limits.max_states:
                        return (max(next_states.values(), key=objective), explored, True)
                    next_states[key] = merged
                    explored += 1
        states = next_states
    return (max(states.values(), key=objective), explored, False)

def _search_independent_options(options: tuple[_Option, ...], candidates: dict[str, _Candidate], context: SkillPurchaseContext, profile: SkillPreferenceProfile, must_targets: tuple[str, ...], limits: OptimizerLimits, *, initial_selected: frozenset[str], batch_cap: int) -> tuple[frozenset[str], int, bool]:
    """Keep the best exact-integer-utility build for each cost/count pair.

    Independent future choices add the same cost, utility and must-buy bits to
    either build, so dominated subsets cannot become optimal later. Dependency
    bundles, auto-included lower skills and fractional utilities use the
    original exhaustive path instead.
    """
    initial_cost = sum((candidates[item].final_cost for item in initial_selected))
    states = {(initial_cost, len(initial_selected)): initial_selected}
    explored = 1
    objective = lambda selected: _objective(selected, candidates, context, profile, must_targets)
    for option in options:
        if option.root_id in initial_selected:
            continue
        next_states = dict(states)
        for (cost, count), selected in states.items():
            merged = selected | option.selected_ids
            if count >= batch_cap or not _within_budget(merged, candidates, context, profile):
                continue
            key = (cost + candidates[option.root_id].final_cost, count + 1)
            previous = next_states.get(key)
            if previous is None or objective(merged) > objective(previous):
                if explored >= limits.max_states:
                    return (max(next_states.values(), key=objective), explored, True)
                next_states[key] = merged
                explored += 1
        states = next_states
    return (max(states.values(), key=objective), explored, False)

def _contains_auto_included_lower_overlap(selected: frozenset[str], candidates: dict[str, _Candidate]) -> bool:
    """Reject clicks on both an upper tier and its auto-included lower tiers."""
    return any((any((lower_id in selected for lower_id in candidate.row.relations.get('supersedes', []) + candidate.row.relations.get('lower', []))) for skill_id, candidate in candidates.items() if skill_id in selected))

def _within_budget(selected: frozenset[str], candidates: dict[str, _Candidate], context: SkillPurchaseContext, profile: SkillPreferenceProfile) -> bool:
    spent = sum((candidates[skill_id].final_cost for skill_id in selected))
    if context.current_skill_pt - spent < _reserved_skill_pt(context, profile):
        return False
    return context.policy.trigger_budget is None or spent <= context.policy.trigger_budget

def _spenddown_warnings(selected: frozenset[str], options: tuple[_Option, ...], candidates: dict[str, _Candidate], context: SkillPurchaseContext, profile: SkillPreferenceProfile, candidate_set: SkillCandidateSet) -> list[str]:
    batch_cap = profile.max_batch_size
    if context.policy.max_purchase_count is not None:
        batch_cap = min(batch_cap, context.policy.max_purchase_count)
    incremental_costs = [sum((candidates[skill_id].final_cost for skill_id in option.selected_ids - selected)) for option in options if option.selected_ids - selected and len(selected | option.selected_ids) <= batch_cap]
    warnings: list[str] = []
    if len(selected) >= batch_cap and any((option.selected_ids - selected for option in options)):
        warnings.append('spenddown_batch_cap_reached')
    elif not incremental_costs:
        warnings.append('spenddown_safe_candidates_exhausted')
    else:
        remaining = context.current_skill_pt - sum((candidates[skill_id].final_cost for skill_id in selected))
        if remaining < min(incremental_costs):
            warnings.append('spenddown_remainder_below_cheapest_safe_candidate')
        else:
            warnings.append('spenddown_safe_candidate_omitted')
    if candidate_set.abstained:
        warnings.append('spenddown_upstream_candidates_abstained')
    return warnings

def _objective(selected: frozenset[str], candidates: dict[str, _Candidate], context: SkillPurchaseContext, profile: SkillPreferenceProfile, must_targets: tuple[str, ...]) -> tuple[object, ...]:
    spent = sum((candidates[skill_id].final_cost for skill_id in selected))
    utility = sum((candidates[skill_id].utility for skill_id in selected))
    remaining = context.current_skill_pt - spent
    return (tuple((int(skill_id in selected) for skill_id in must_targets)), round(utility, 6), spent if profile.filler_policy == 'spend_all_safe' else 0, -remaining, tuple(sorted(selected)))

def _purchases_for_selection(selected: frozenset[str], candidates: dict[str, _Candidate]) -> tuple[PlannedPurchase, ...]:
    ordered: list[str] = []
    visiting: set[str] = set()

    def visit(skill_id: str) -> None:
        if skill_id in ordered or skill_id in visiting:
            return
        visiting.add(skill_id)
        for lower_id in sorted(candidates[skill_id].row.relations.get('lower', [])):
            if lower_id in selected:
                visit(lower_id)
        visiting.remove(skill_id)
        ordered.append(skill_id)
    for skill_id in sorted(selected):
        visit(skill_id)
    return tuple((PlannedPurchase(skill_id=skill_id, name=candidates[skill_id].row.name_zh_cn, type=candidates[skill_id].row.type, rarity=candidates[skill_id].row.rarity, display_cost=candidates[skill_id].skill.display_cost, final_cost=candidates[skill_id].final_cost, hint_level=candidates[skill_id].hint_level, score=round(candidates[skill_id].utility, 6), priority_bucket=candidates[skill_id].bucket, reasons=[candidates[skill_id].bucket], risk_flags=['manual_verification_required'] if not candidates[skill_id].row.is_verified else [], marginal_utility=round(candidates[skill_id].utility, 6), utility_per_pt=round(candidates[skill_id].utility / candidates[skill_id].final_cost, 6)) for skill_id in ordered))

def _blocked_plan(context: SkillPurchaseContext, profile: SkillPreferenceProfile, candidate_set: SkillCandidateSet, status: str, *, skipped: list[SkippedSkill] | None=None, explored_states: int=0) -> SkillPurchasePlan:
    plan = SkillPurchasePlan(schema_version=PLAN_SCHEMA, target_mode=context.target_mode, trigger=context.trigger, status='blocked', skipped=sorted(skipped or [], key=lambda item: (item.skill_id, item.reason_code)), reserved_skill_pt=_reserved_skill_pt(context, profile), remaining_skill_pt=context.current_skill_pt, risk_flags=[status], explain='Offline global optimizer failed closed.', profile_id=profile.profile_id, profile_sha256=profile.profile_sha256, candidate_set_sha256=candidate_set.candidate_set_sha256, optimizer_status=status, optimizer_explored_states=explored_states, execution_allowed=False)
    return replace(plan, plan_sha256=_plan_sha(plan, ()))

def _skip(skill_id: str, name: str, reason_code: str) -> SkippedSkill:
    return SkippedSkill(skill_id, name, reason_code, reason_code)

def _reserved_skill_pt(context: SkillPurchaseContext, profile: SkillPreferenceProfile) -> int:
    return max(context.policy.reserve_skill_pt, profile.reserve_skill_pt)

def _plan_sha(plan: SkillPurchasePlan, objective: tuple[object, ...]) -> str:
    material = {'schema_version': plan.schema_version, 'target_mode': plan.target_mode, 'trigger': plan.trigger, 'profile_id': plan.profile_id, 'profile_sha256': plan.profile_sha256, 'candidate_set_sha256': plan.candidate_set_sha256, 'purchases': [item.to_dict() for item in plan.purchases], 'skipped': [item.to_dict() for item in plan.skipped], 'unmet_required_skill_ids': plan.unmet_required_skill_ids, 'spent_skill_pt': plan.spent_skill_pt, 'reserved_skill_pt': plan.reserved_skill_pt, 'remaining_skill_pt': plan.remaining_skill_pt, 'objective': objective, 'optimizer_status': plan.optimizer_status, 'optimizer_explored_states': plan.optimizer_explored_states, 'execution_allowed': False}
    payload = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()

def _is_sha(value: object) -> bool:
    return type(value) is str and _SHA256_PATTERN.fullmatch(value) is not None
__all__ = ['GlobalOptimizationResult', 'OptimizerLimits', 'optimize_final_skill_build']
