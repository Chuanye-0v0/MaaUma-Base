"""Local factor recognition, weighted choice and bounded reroll policy.

The first result is free. Additional costs come only from fresh recognized
controls, never from a hardcoded monthly-card cost schedule. This module does
not touch devices; the existing harvest executor owns every dispatched input.
"""
from __future__ import annotations
from desktop.umaa_public_context import endpoint as _public_endpoint
import math
import re
import cv2
import numpy as np
FACTOR_ACTIONS = {'factor_reroll', 'confirm_factor_reroll', 'cancel_factor_reroll', 'select_original_factors', 'select_new_factors'}

def normalize(text):
    return re.sub('[\\s○〇◎！!。，,、]', '', str(text))

def score_factors(rows, profile):
    rules = {r['key']: r for r in profile['weights']}
    stars = profile['star_weights']
    parts = []
    for row in rows:
        count = row.get('stars')
        rule = rules.get(row.get('key'))
        if rule is None or type(count) is not int or (not 1 <= count <= 3) or (count < rule['min_stars']):
            continue
        contribution = rule['weight'] * stars[count - 1]
        if not math.isfinite(contribution):
            raise ValueError('factor_score_invalid')
        parts.append(dict(key=row['key'], stars=count, score=contribution))
    return dict(score=sum((p['score'] for p in parts)), contributions=parts)

def prefer_new(original, new, profile):
    """Equal scores keep the already-retained result; no unnecessary change."""
    return score_factors(new, profile)['score'] > score_factors(original, profile)['score']

def factor_signature(rows):
    return sorted(((r['key'], r['stars']) for r in rows))

def _stars(image, cy):
    crop = image[max(0, cy - 20):min(image.shape[0], cy + 21), 520:646]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([12, 105, 160]), np.array([42, 255, 255]))
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    blobs = [s for s in stats[1:] if 70 <= s[cv2.CC_STAT_AREA] <= 900 and 8 <= s[cv2.CC_STAT_WIDTH] <= 34 and (8 <= s[cv2.CC_STAT_HEIGHT] <= 40)]
    return len(blobs) if 1 <= len(blobs) <= 3 else None

def _row_kind(image, cy):
    h, s, v = cv2.cvtColor(image[max(0, cy - 3):cy + 4, 350:430], cv2.COLOR_BGR2HSV).reshape(-1, 3).mean(axis=0)
    if s > 70 and 85 < h < 120:
        return 'blue'
    if s > 50 and 135 < h < 179:
        return 'pink'
    if s > 70 and 30 < h < 85:
        return 'green'
    return 'white'

def read_factors(image, tokens, library, profile=None):
    """Names from row OCR, filled stars from pixels; no fuzzy identity guessing."""
    by_name = {}
    for row in library['factors']:
        by_name.setdefault(normalize(row['name']), []).append(row['key'])
    for rule in (profile or {}).get('weights', []):
        if rule['key'].startswith('custom:'):
            by_name.setdefault(normalize(rule['key'][7:]), []).append(rule['key'])
    rows = []
    unknown = []
    for token in tokens:
        box = token.get('bbox', [])
        if len(box) != 4 or not 45 <= box[0] < 480 or (not 145 <= box[1] < 900) or (box[2] > 510):
            continue
        cy = int((box[1] + box[3]) / 2)
        count = _stars(image, cy)
        if count is None:
            continue
        if token.get('confidence', 0) < 0.85:
            unknown.append(dict(name=token.get('text'), stars=count, reason='factor_name_low_confidence'))
            continue
        name = normalize(token.get('text', ''))
        matches = by_name.get(name, [])
        kind = _row_kind(image, cy)
        custom = [k for k in matches if k.startswith('custom:')]
        matches = custom or [k for k in matches if k.startswith(kind + ':')]
        if len(set(matches)) != 1:
            unknown.append(dict(name=token.get('text'), stars=count))
            continue
        rows.append(dict(key=matches[0], name=token['text'], stars=count, y=cy, confidence=token['confidence']))
    last = max((r['y'] for r in rows), default=900)
    tail = image[min(900, last + 45):915, 80:635]
    complete = bool(rows) and tail.size > 0 and (tail.shape[0] >= 45) and (float(np.mean(np.min(tail, axis=2) > 225)) > 0.85) and (not unknown) and (len({r['key'] for r in rows}) == len(rows))
    return dict(rows=rows, unknown=unknown, complete=complete)

def observe_factor_controls(image, tokens, observed, context, library):
    """Enrich only owned result/modal pages; unconfigured harvest stays unchanged."""
    preset = context.get('harvest_preset') or {}
    profile = preset.get('factor_profile')
    if not profile or context.get('mode') != 'harvest' or (not context.get('harvest_active')):
        return observed
    good = [t for t in tokens if t.get('confidence', 0) >= 0.95 and len(t.get('bbox', [])) == 4]

    def one(names, roi):
        found = [t for t in good if normalize(t.get('text', '')) in {normalize(n) for n in names} and roi[0] <= t['bbox'][0] and (roi[1] <= t['bbox'][1]) and (t['bbox'][2] <= roi[2]) and (t['bbox'][3] <= roi[3])]
        return found[0] if len(found) == 1 else None

    def add(action, token):
        if token is None:
            return
        from core.autonomous_training.harvest_observer import _control_enabled
        b = token['bbox']
        patch = image[max(0, b[1] - 5):min(image.shape[0], b[3] + 5), max(0, b[0] - 10):min(image.shape[1], b[2] + 10)]
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV) if patch.size else None
        white = float(np.mean((hsv[:, :, 1] < 35) & (hsv[:, :, 2] > 220))) if hsv is not None else 0
        if not (_control_enabled(image, token) or white > 0.58):
            return
        observed['actions'][action] = dict(point=[int((b[0] + b[2]) / 2), int((b[1] + b[3]) / 2)])
    modal = one(['重新获得因子确认', '重新获取因子确认'], (110, 150, 610, 450))
    if modal is not None:
        observed.update(page='factor_reroll_confirm', actions={})
        add('confirm_factor_reroll', one(['确定', '确认'], (390, 750, 650, 1240)))
        add('cancel_factor_reroll', one(['取消'], (60, 750, 340, 1240)))
    elif observed.get('page') != 'factor_result':
        return observed
    if observed.get('page') == 'factor_result':
        observed['factor_observation'] = read_factors(image, tokens, library, profile)
        add('factor_reroll', one(['重新获得因子', '重新获取因子'], (40, 1080, 360, 1240)))
        for action, names in [('select_original_factors', ['原来的因子', '原因子']), ('select_new_factors', ['新获得的因子', '新因子'])]:
            add(action, one(names, (50, 80, 670, 240)))
    costs = []
    roi = (40, 400, 680, 1150) if modal is not None else (40, 1140, 360, 1240)
    for t in good:
        b = t['bbox']
        if not (roi[0] <= b[0] and roi[1] <= b[1] and (b[2] <= roi[2]) and (b[3] <= roi[3])):
            continue
        text = normalize(t['text'])
        if re.fullmatch('(?:本次)?免费(?:重新获得)?', text):
            costs.append(dict(resource='free', amount=0))
        match = re.fullmatch('(训练值|体力|TP|宝石|萝卜)(?:消耗|使用)?(\\d+)', text)
        if match:
            costs.append(dict(resource='tp' if match[1] in {'训练值', '体力', 'TP'} else 'jewel', amount=int(match[2])))
    observed['factor_reroll_cost'] = costs[0] if len(costs) == 1 else None
    return observed

def _affordable(cost, preset, state):
    if not isinstance(cost, dict) or set(cost) != {'resource', 'amount'}:
        return False
    resource, amount = (cost['resource'], cost['amount'])
    if resource == 'free':
        return type(amount) is int and amount == 0
    if resource not in {'tp', 'jewel'} or type(amount) is not int or amount <= 0:
        return False
    return state.get(resource + '_spent', 0) + amount <= preset.get(resource + '_budget', 0)

def next_factor_action(observed, context):
    """Retain the best proven list. Unknown costs/layouts stop paid rerolls only."""
    preset = context.get('harvest_preset') or {}
    profile = preset.get('factor_profile')
    if not profile:
        return None
    state = context.setdefault('factor_selection', dict(rolls=0, tp_spent=0, jewel_spent=0, history=[], done=False))
    if state.get('done'):
        return None
    if state.get('pending'):
        if observed.get('page') != 'factor_reroll_confirm':
            raise RuntimeError('factor_reroll_submission_pending')
        fresh = observed.get('factor_reroll_cost')
        if fresh != state['pending'] or not _affordable(fresh, preset, state):
            return 'cancel_factor_reroll'
        return 'confirm_factor_reroll'
    if observed.get('page') != 'factor_result':
        return None
    parsed = observed.get('factor_observation') or {}
    rows = parsed.get('rows', [])
    if not parsed.get('complete'):
        if state.get('needs_comparison') or state.get('await_selection'):
            raise RuntimeError('factor_comparison_incomplete')
        state.update(done=True, limit_reason='factor_list_not_fully_recognized')
        state['unrecognized_factor_evidence'] = {k: observed.get(k) for k in ('screenshot_path', 'frame_sha256', 'factor_observation')}
        return None
    result = score_factors(rows, profile)
    score = result['score']
    original = state.get('retained')
    from core.factor_goals import targets_met
    goals = context.get('factor_goal')
    if state.get('needs_comparison'):
        if not {'select_original_factors', 'select_new_factors'} <= set(observed['actions']):
            raise RuntimeError('factor_comparison_controls_unverified')
        old_hit = bool(goals and targets_met(original['rows'], goals))
        new_hit = bool(goals and targets_met(rows, goals))
        use_new = new_hit and (not old_hit) or (new_hit == old_hit and score > original['score'])
        selected = 'select_new_factors' if use_new else 'select_original_factors'
        state['comparison_decision'] = dict(action=selected, original_score=original['score'], new_score=score)
        if selected == 'select_new_factors':
            state['retained'] = dict(rows=rows, score=score, screenshot_path=observed.get('screenshot_path'), frame_sha256=observed.get('frame_sha256'))
        state['needs_comparison'] = False
        state['await_selection'] = True
        return selected
    if state.get('await_selection'):
        if factor_signature(rows) != factor_signature(state['retained']['rows']):
            raise RuntimeError('factor_selected_list_unverified')
        state.pop('await_selection')
    if original is None:
        state['retained'] = dict(rows=rows, score=score, screenshot_path=observed.get('screenshot_path'), frame_sha256=observed.get('frame_sha256'))
    if not state['history'] or state['history'][-1]['roll'] != state['rolls']:
        state['history'].append(dict(roll=state['rolls'], **result, rows=rows, screenshot_path=observed.get('screenshot_path'), frame_sha256=observed.get('frame_sha256')))
    if goals and targets_met(state['retained']['rows'], goals):
        state.update(done=True, target_met=True, limit_reason='target_factors_met')
        return None
    if state['rolls'] >= preset.get('rerolls', 0):
        state.update(done=True, limit_reason='configured_reroll_limit')
        return None
    cost = observed.get('factor_reroll_cost')
    if 'factor_reroll' not in observed['actions'] or not _affordable(cost, preset, state):
        state.update(done=True, limit_reason='reroll_unavailable_or_budget')
        return None
    return 'factor_reroll'

def authorize_factor_action(action, observed, context):
    from core.autonomous_training.harvest_direct_settlement import completion_is_bound
    if context.get('mode') != 'harvest' or not context.get('harvest_active') or (not completion_is_bound(context, observed.get('endpoint'))) or context.get('factor_choice_submitted') or (observed.get('endpoint') != _public_endpoint()) or (action not in observed.get('actions', {})):
        raise RuntimeError('factor_action_owner_unverified')
    state = context.get('factor_selection') or {}
    preset = context.get('harvest_preset') or {}
    if action == 'factor_reroll':
        if state.get('done') or state.get('pending') or state.get('rolls', 0) >= preset.get('rerolls', 0) or (not _affordable(observed.get('factor_reroll_cost'), preset, state)):
            raise RuntimeError('factor_reroll_budget_unverified')
    elif action == 'confirm_factor_reroll':
        if observed.get('page') != 'factor_reroll_confirm' or not state.get('pending') or observed.get('factor_reroll_cost') != state['pending'] or (not _affordable(state['pending'], preset, state)):
            raise RuntimeError('factor_reroll_cost_changed')
    elif action.startswith('select_'):
        if state.get('comparison_decision', {}).get('action') != action:
            raise RuntimeError('factor_choice_not_decided')

def mark_factor_dispatch(action, observed, context):
    """Commit intent before input; failures never manufacture a reusable budget."""
    state = context['factor_selection']
    if action == 'factor_reroll':
        state['pending'] = dict(observed['factor_reroll_cost'])
    elif action == 'confirm_factor_reroll':
        if state.get('confirmation_submitted'):
            raise RuntimeError('factor_reroll_already_submitted')
        state['confirmation_submitted'] = True

def record_factor_receipt(action, receipt, context):
    if receipt.get('performed') is not True or receipt.get('action_id') != action:
        raise RuntimeError('factor_input_outcome_unknown')
    state = context['factor_selection']
    if action == 'factor_reroll' and state.get('pending', {}).get('resource') == 'free' and (receipt.get('observation', {}).get('page') == 'factor_result'):
        state.pop('pending')
        state['rolls'] += 1
        state['needs_comparison'] = True
    elif action == 'cancel_factor_reroll':
        state.pop('pending', None)
        state.update(done=True, limit_reason='reroll_cost_not_authorized')
    elif action == 'confirm_factor_reroll':
        cost = state.pop('pending')
        state.pop('confirmation_submitted', None)
        if cost['resource'] != 'free':
            state[cost['resource'] + '_spent'] += cost['amount']
        state['rolls'] += 1
        state['needs_comparison'] = True
