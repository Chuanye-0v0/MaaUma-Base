"""Bounded skill-family knapsack for compiled CM profiles."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace


def is_cm_profile(profile):
    return profile.profile_id.endswith("-arc-target-cm")


def is_cm_core_candidate(candidate):
    # The compiler emits positive adjustments for core and negative
    # adjustments for filler. An explicit must-buy also makes a skill core.
    return candidate.rule.mode == "must_buy_if_safe" or candidate.rule.prefer_weight > 0


def _bind_groundwork(options, profile):
    by_id = {option.root_id: option for option in options}
    linked = {rule.selector.value: rule.priority for rule in profile.rules
              if rule.selector.kind == "skill_id" and rule.mode == "prefer" and rule.priority in {-400, -410}}
    sources = {sid for sid, priority in linked.items() if priority == -410}
    if sources <= by_id.keys():
        support = frozenset().union(*(by_id[sid].selected_ids for sid in sources))
        return tuple(replace(option, selected_ids=option.selected_ids | support)
                     if linked.get(option.root_id) == -400 else option for option in options)
    return tuple(option for option in options if linked.get(option.root_id) != -400)


def _families(options, candidates, initial):
    parents = {sid: sid for sid in candidates}

    def root(sid):
        while sid != parents[sid]:
            sid = parents[sid]
        return sid

    for sid, candidate in candidates.items():
        for key in ("lower", "supersedes"):
            for lower in candidate.row.relations.get(key, []):
                if lower in parents:
                    parents[root(lower)] = root(sid)
    for option in options:
        for sid in option.selected_ids:
            parents[root(sid)] = root(option.root_id)
    groups = defaultdict(list)
    for option in options:
        groups[root(option.root_id)].append(option.selected_ids)
    initial_groups = defaultdict(set)
    for sid in initial:
        initial_groups[root(sid)].add(sid)
    return [(frozenset(initial_groups[key]), groups[key]) for key in sorted(groups.keys() | initial_groups.keys())]


@dataclass(frozen=True, slots=True)
class _Choice:
    ids: frozenset
    cost: int
    must_bits: int
    utility: float


def _family_choices(initial, options, candidates, must_bits, cost_cap, batch_cap, auto_lower):
    """Enumerate actual buyable subsets only within one upgrade family."""
    conflicts = {
        sid: set(candidate.row.relations.get("lower", [])) | set(candidate.row.relations.get("supersedes", []))
        for sid, candidate in candidates.items() if auto_lower and candidate.skill.cost_basis == "observed_final"
    }
    subsets = {initial}
    for option in options:
        for previous in tuple(subsets):
            merged = previous | option
            added = merged - initial
            if len(added) > batch_cap or any(conflicts.get(sid, set()) & merged for sid in merged):
                continue
            if sum(candidates[sid].final_cost for sid in added) <= cost_cap:
                subsets.add(merged)
    return [_Choice(ids, sum(candidates[sid].final_cost for sid in ids),
                    sum(must_bits.get(sid, 0) for sid in ids),
                    sum(candidates[sid].utility for sid in sorted(ids)))
            for ids in sorted((ids - initial for ids in subsets), key=lambda ids: tuple(sorted(ids)))]


def _prune_core_frontier(states):
    """A lower-cost equal-count build with greater priority/utility dominates."""
    result, best_by_count = {}, {}
    for key in sorted(states):
        state = states[key]
        count = key[1]
        score = state.must_bits, state.utility
        if count not in best_by_count or score > best_by_count[count]:
            best_by_count[count] = score
            result[key] = state
    return result


def search_cm_options(options, candidates, context, profile, must_targets, limits, *,
                      initial_selected=frozenset()):
    """Use the existing options, costs and prerequisites in the two search passes."""
    options = _bind_groundwork(options, profile)
    max_states = min(limits.max_states, 50000)
    batch_cap = profile.max_batch_size
    if context.policy.max_purchase_count is not None:
        batch_cap = min(batch_cap, context.policy.max_purchase_count)
    reserve = max(context.policy.reserve_skill_pt, profile.reserve_skill_pt)
    cost_cap = max(0, context.current_skill_pt - reserve)
    if context.policy.trigger_budget is not None:
        cost_cap = min(cost_cap, context.policy.trigger_budget)
    initial_spent = sum(candidates[sid].final_cost for sid in initial_selected)
    if (not options or batch_cap <= len(initial_selected)
            or cost_cap <= initial_spent):
        return initial_selected, 1, False
    spenddown = profile.filler_policy == "spend_all_safe" and (
        bool(initial_selected) or any(not is_cm_core_candidate(candidates[option.root_id]) for option in options))
    must_bits = {sid: 1 << index for index, sid in enumerate(reversed(must_targets))}
    initial = _Choice(initial_selected, initial_spent,
                      sum(must_bits.get(sid, 0) for sid in initial_selected),
                      sum(candidates[sid].utility for sid in sorted(initial_selected)))
    states = {(initial_spent, len(initial_selected)): initial}
    explored = 1
    for seed, family in _families(options, candidates, initial_selected):
        choices = _family_choices(seed, family, candidates, must_bits,
                                  cost_cap - initial_spent, batch_cap - len(initial_selected),
                                  context.policy.allow_gold_without_lower)
        if len(choices) == 1 and not choices[0].ids:
            continue
        following = {}
        for (cost, count), previous in states.items():
            for option in choices:
                key = cost + option.cost, count + len(option.ids)
                if key[0] > cost_cap or key[1] > batch_cap:
                    continue
                score = previous.must_bits | option.must_bits, previous.utility + option.utility
                incumbent = following.get(key)
                if incumbent is not None and score < (incumbent.must_bits, incumbent.utility):
                    continue
                merged = previous.ids | option.ids
                if (incumbent is None or score > (incumbent.must_bits, incumbent.utility)
                        or tuple(sorted(merged)) > tuple(sorted(incumbent.ids))):
                    following[key] = _Choice(merged, key[0], *score)
        # Core values protect the race build. The filler pass keeps these
        # locked and searches for the largest affordable spend on other skills.
        states = following if spenddown else _prune_core_frontier(following)
        explored += len(states)
        if explored > max_states:
            return initial_selected, max_states, True
    if not states:
        return initial_selected, explored, False
    best = max(states.values(), key=lambda state: (
        state.must_bits, (state.cost, state.utility) if spenddown else (state.utility, -state.cost),
        tuple(sorted(state.ids))))
    return best.ids, explored, False
