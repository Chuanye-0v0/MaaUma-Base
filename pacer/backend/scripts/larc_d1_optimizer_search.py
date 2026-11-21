"""Pacer-local access to D1's existing exact knapsack for small batches."""
import importlib.util
from pathlib import Path
import sys


def _cm_search_module():
    name = "_pacer_cm_skill_search"
    if name not in sys.modules:
        path = Path(__file__).resolve().parents[1] / "core/skill_planner/cm_skill_search.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def install_small_batch_exact_search(optimizer):
    """Keep D1 candidates/objective/limits; only remove the small-batch blind spot."""
    original=optimizer._search_options
    if getattr(original,'_pacer_small_batch_exact',False):
        return original
    cm = _cm_search_module()

    def search(options,candidates,context,profile,must_targets,limits,*,initial_selected=frozenset()):
        if cm.is_cm_profile(profile):
            return cm.search_cm_options(options, candidates, context, profile, must_targets, limits,
                                        initial_selected=initial_selected)
        # D1 already uses these exact algorithms above batch size 12. Its normal
        # terminal profile is exactly 12, where exhaustive subsets can explode.
        # Integer sums below 2**53 remain exact; fractional/near-rounding utility
        # must retain D1's original path and its unchanged fail-closed limit.
        exact=(profile.max_batch_size<=12
            and all(len(option.selected_ids)==1 for option in options)
            and all(candidate.utility.is_integer() for candidate in candidates.values())
            and sum(abs(candidate.utility) for candidate in candidates.values())<2**53)
        if exact:
            batch_cap=profile.max_batch_size
            if context.policy.max_purchase_count is not None:
                batch_cap=min(batch_cap,context.policy.max_purchase_count)
            if context.policy.allow_gold_without_lower:
                grouped=getattr(optimizer,'_search_upgrade_groups',None)
                if grouped is not None:
                    result=grouped(options,candidates,context,profile,must_targets,limits,
                        initial_selected,batch_cap)
                    if result is not None:
                        return result
            else:
                independent=getattr(optimizer,'_search_independent_options',None)
                if independent is not None:
                    return independent(options,candidates,context,profile,must_targets,limits,
                        initial_selected=initial_selected,batch_cap=batch_cap)
        return original(options,candidates,context,profile,must_targets,limits,
            initial_selected=initial_selected)

    search._pacer_small_batch_exact=True
    search._pacer_original_search=original
    search._pacer_cm_core_candidate = cm.is_cm_core_candidate
    optimizer._search_options=search
    return search
