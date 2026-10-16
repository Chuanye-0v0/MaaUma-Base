"""Explicit all-of factor goals and a task-scoped normal-training TP ceiling.

Weights rank candidates; they never turn a missing required factor into a hit.
Outside an opt-in seed cycle this policy changes no existing task behavior.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
_POLICY = ContextVar('umaa_seed_policy', default=None)

def validate_targets(value, known_keys=None):
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ValueError('请设置至少一个目标因子')
    seen, targets = (set(), [])
    for row in value:
        if not isinstance(row, dict) or set(row) != {'key', 'min_stars'} or (not isinstance(row['key'], str)) or (not row['key']) or (row['key'] in seen) or (type(row['min_stars']) is not int) or (not 1 <= row['min_stars'] <= 3) or (known_keys is not None and row['key'] not in known_keys):
            raise ValueError('目标因子或最低星级无效')
        seen.add(row['key'])
        targets.append(dict(row))
    return targets

def targets_met(rows, targets):
    targets = validate_targets(targets)
    if not isinstance(rows, list) or not rows:
        return False
    seen = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('key'), str) or row['key'] in seen or (type(row.get('stars')) is not int) or (not 1 <= row['stars'] <= 3):
            return False
        seen[row['key']] = row['stars']
    return all((seen.get(row['key'], 0) >= row['min_stars'] for row in targets))

@contextmanager
def seed_policy(targets, remaining_tp):
    if remaining_tp is not None and (type(remaining_tp) is not int or remaining_tp < 0):
        raise ValueError('seed_tp_budget_invalid')
    token = _POLICY.set(dict(targets=validate_targets(targets) if targets else None, remaining_tp=remaining_tp))
    try:
        yield
    finally:
        _POLICY.reset(token)

def current_targets():
    policy = _POLICY.get()
    return deepcopy(policy['targets']) if policy else None

def check_training_tp(cost):
    policy = _POLICY.get()
    if policy and policy['remaining_tp'] is not None and (type(cost) is not int or cost > policy['remaining_tp']):
        raise RuntimeError('seed_total_tp_budget_exhausted')
