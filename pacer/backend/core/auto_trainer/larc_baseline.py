"""Pure configuration for the unified L'Arc baseline and explicit migration.

These functions never read or change a goal, device, resource permission or
saved file. Callers persist migration records separately from the strict policy.
"""
from copy import deepcopy
from core.auto_trainer import larc_strategy_versions as _versions
from core.auto_trainer.larc_strategy_versions import (
    CURRENT_REVISION, LEGACY_REVISION, policy_version_for_revision, validate_revision, revision_action,
)


UNIFIED_VARIANT = 'baseline'
UNIFIED_VERSION = 'larc_unified_v1_1_preview'
# Retain the old constant for integrations which describe the published 1.1
# baseline. Runtime labels must come from policy_version(goal), not this alias.
LATEST_UNIFIED_VERSION = policy_version_for_revision(CURRENT_REVISION)
LEGACY_VARIANTS = {'alpha', 'baseline_v118', 'beta', 'resource_v119'}
UNIFIED_FEATURES = {
    'strategy_refinement': True,
    'training_balance_v2': True,
    'overseas_purchase_v2': True,
    'overseas_purchase_v3': True,
    'strategy_v4': True,
}


def _task_mode(task_mode):
    if not isinstance(task_mode, str) or task_mode not in {'career', 'cut'}:
        raise ValueError('larc_baseline_task_mode_unknown')
    return task_mode


def _default_credit_for_revision(rev):
    if rev == '1.8.4':
        return .2
    from core.auto_trainer.larc_strategy_params import PRESET_REGISTRY
    if isinstance(rev, str) and rev in PRESET_REGISTRY:
        return PRESET_REGISTRY[rev].default_after_target_credit
    return .5


def normalize_larc_policy(policy=None, task_mode='career'):
    """Normalize new baseline settings; explicit legacy variants keep behavior.

    None means a new configuration. A supplied dictionary without a variant
    retains the historical implicit-alpha semantics, including an empty save.
    """
    _task_mode(task_mode)
    new_configuration = policy is None
    if new_configuration:
        policy = {}
    if not isinstance(policy, dict):
        raise ValueError('larc_policy_must_be_object')
    result = deepcopy(policy)
    variant = result.get('variant', UNIFIED_VARIANT if new_configuration else 'alpha')
    from core.auto_trainer.larc_policy import validate_policy
    if variant in LEGACY_VARIANTS:
        validate_policy(result)
        return result
    if variant != UNIFIED_VARIANT:
        raise ValueError('larc_policy_variant_unknown')
    result['variant'] = UNIFIED_VARIANT
    # A supplied save without this field is the already-published behavior.
    # None is the only implicit new-configuration entry point.
    result.setdefault('revision', _versions.CURRENT_REVISION if new_configuration else LEGACY_REVISION)
    for key, value in UNIFIED_FEATURES.items():
        if key in result and result[key] is not value:
            raise ValueError('larc_unified_explicit_upgrade_required:' + key)
        result.setdefault(key, value)
    # Keep explicit advanced preferences. These are defaults, not an average
    # of the old branches or a replacement of user scoring profiles.
    result.setdefault('attribute_weights', {})
    from core.auto_trainer.larc_strategy_params import AUTO_ROUTED_REVISIONS
    if result.get('revision') not in AUTO_ROUTED_REVISIONS:
        result.setdefault('after_target_credit', _default_credit_for_revision(result.get('revision')))
    result.setdefault('ss_planning_horizon',
                      'junior_cutoff_discard' if task_mode == 'cut' else 'full_career')
    validate_policy(result)
    return result


def upgrade_larc_policy(policy=None, task_mode='career', *, revision=None):
    """Explicitly adopt the unified model, with the exact previous policy saved.

    A caller must invoke this for an intentional upgrade. Legacy false model
    switches are changed here and listed in the returned migration; merely
    loading or normalizing an old saved policy never changes those switches.
    """
    _task_mode(task_mode)
    if revision is None:
        revision = _versions.CURRENT_REVISION
    validate_revision(revision)
    if policy is not None and not isinstance(policy, dict):
        raise ValueError('larc_policy_must_be_object')
    original = deepcopy(policy)
    previous = deepcopy(policy or {})
    from core.auto_trainer.larc_policy import validate_policy
    validation = deepcopy(previous)
    if validation.get('variant') == UNIFIED_VARIANT:
        for key in UNIFIED_FEATURES:
            if validation.get(key) is False:
                validation[key] = True  # This API explicitly upgrades these frozen flags.
    validate_policy(validation)
    upgraded = {**previous, 'variant': UNIFIED_VARIANT, 'revision': revision, **UNIFIED_FEATURES}
    old_rev = (previous.get('revision', LEGACY_REVISION)
               if previous.get('variant') == UNIFIED_VARIANT else None)
    if old_rev is not None and old_rev != revision and 'after_target_credit' in upgraded:
        cur_atc = upgraded['after_target_credit']
        if isinstance(cur_atc, (int, float)) and abs(float(cur_atc) - _default_credit_for_revision(old_rev)) < 1e-6:
            del upgraded['after_target_credit']
    upgraded = normalize_larc_policy(upgraded, task_mode)
    changes = []
    for key in sorted(set(previous) | set(upgraded)):
        if key not in previous or previous[key] != upgraded.get(key):
            changes.append({'field': key, 'before_present': key in previous,
                            'before': deepcopy(previous.get(key)),
                            'after': deepcopy(upgraded.get(key)),
                            'reason': 'explicit_unified_baseline_upgrade'})
    from_revision = (previous.get('revision', LEGACY_REVISION)
                     if previous.get('variant') == UNIFIED_VARIANT else None)
    from types import SimpleNamespace
    from core.auto_trainer.larc_policy import policy_version
    previous_version = (policy_version(SimpleNamespace(scenario='larc', larc_policy=previous))
                        if policy is not None else None)
    return {'policy': upgraded,
            'migration': {'schema_version': 'uma.larc_policy_migration.v1',
                'mode': 'explicit_upgrade', 'task_mode': task_mode,
                'from_variant': previous.get('variant', 'alpha') if policy is not None else None,
                'from_revision': from_revision, 'to_revision': revision,
                'revision_action': revision_action(from_revision, revision),
                'from_policy_version': previous_version,
                'to_variant': UNIFIED_VARIANT, 'to_policy_version': policy_version_for_revision(revision),
                'original_policy': original, 'changes': changes,
                'goal_targets_unchanged': True, 'execution_permissions_unchanged': True,
                'resource_limits_unchanged': True}}
