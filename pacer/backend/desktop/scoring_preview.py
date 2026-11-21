"""Deterministic local score explanation, never a current game observation.

stdin: {profile: <exported scoring_profile>, scenario: <SCENARIOS key>}
stdout: one JSON response; no network, credential, screenshot or device access.
"""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import hashlib
import json
import math
import sys

from core.auto_trainer.config import UGParentGoal
from core.auto_trainer.state import TrainingCandidate, TrainingTurnState, STAT_KEYS, TRAINING_TYPES
from core.auto_trainer.larc_opening import LarcOpeningStrategy
from core.auto_trainer.larc_ss_choice import choose_larc_ss
from core.auto_trainer import larc_score_tuning as tuning
from core.auto_trainer.larc_phase import PHASE_WEIGHTS, phase, phase_weights
from core.auto_trainer.larc_timeline_workflow import resolve, action_kind
from core.auto_trainer.larc_user_preferences import values
from desktop.scoring_exchange import checked_profile

SCHEMA = 'uma.readable_score_preview.v1'
SAMPLE_NOTE = '合成示例，调用当前真实评分器；不是当前游戏，不代表实局最优方案。'
SCENARIOS = {
    'junior': ('第一年 · 初级年', 1, 5, 70),
    'classic_domestic': ('第二年 · 国内', 2, 5, 68),
    'classic_expedition': ('第二年 · 远征', 2, 8, 65),
    'senior_domestic': ('第三年 · 国内', 3, 5, 64),
    'senior_expedition': ('第三年 · 远征', 3, 8, 60),
    'low_vitality': ('第二年 · 低体力', 2, 5, 20),
}
TRAINING_LABELS = dict(zip(TRAINING_TYPES, ('速度训练', '耐力训练', '力量训练', '毅力训练', '智力训练')))
PHASE_LABELS = {
    'junior_early': '初级年前半', 'junior_late': '初级年后半',
    'junior_unspecified': '初级年（月份未知）', 'classic_domestic': '经典年国内',
    'classic_expedition': '经典年远征', 'after_first_arc': '首次凯旋门后',
    'senior_domestic': '高级年国内', 'senior_expedition': '高级年远征', 'baseline': '通用阶段',
}


def _gain_column(key, amount):
    tokens = ['+', *str(amount)]
    return {'attribute_id': key, 'status': 'accepted', 'canonical_status': 'accepted',
        'canonical_value': amount, 'rows': [{'kind': 'single', 'status': 'accepted', 'value': amount}],
        'candidates': [{'status': 'accepted', 'value': amount, 'tokens': tokens,
            'component_matches': [{'best_digit': token, 'reliable': True} for token in tokens]}]}


def sample_state(key):
    """The accepted fields below are synthetic fixtures, never OCR claims."""
    if key not in SCENARIOS:
        raise ValueError('请选择六个示例场景之一。')
    label, year, month, vitality = SCENARIOS[key]
    run, turn = 'synthetic-readable-score-preview', 20
    fixture = Path(__file__).with_name('scoring_preview_evidence.txt')
    source = {'run_session_id': run, 'turn_index': turn, 'absolute_path': str(fixture),
              'path': str(fixture), 'sha256': hashlib.sha256(fixture.read_bytes()).hexdigest()}
    period = {'year_index': year, 'month': month, 'half': 'first',
              'stage': {1: 'junior', 2: 'classic', 3: 'senior'}[year]}
    options = []
    for i, kind in enumerate(TRAINING_TYPES):
        gains = {kind: 20 + i * 4, 'skill_pt': 8 + i}
        if kind != 'speed':
            gains['speed'] = 6 + i
        failure = (16 + i if vitality < 30 else 2 + i) if kind != 'wisdom' else 0
        if vitality < 30 and kind == 'speed':
            failure = 28  # A real evaluator rejection remains visible in this sample.
        people = []
        for slot in range(1 + i % 3):
            is_friend = slot == 1 and i % 2 == 1
            people.append({'slot': slot, 'role': {'status': 'accepted', 'kind': 'friend' if is_friend else 'support'},
                'member_ref': {'member_id': 'sample-member-' + str(slot)},
                'identity': {'status': 'known', 'character_id': 'satake_mei' if is_friend else 'gold_ship' if slot == 0 else 'sample_support'},
                'bond': {'status': 'accepted', 'color': 'green', 'fill_ratio_estimate': .45 if is_friend else .65},
                'charge': {'status': 'accepted', 'capacity': 3, 'filled_cells': 1 if slot else 2, 'preview_cells': 1},
                'training_hint': {'status': 'accepted', 'present': slot == 0 and i % 2 == 0},
                'ss_reward': {'status': 'accepted', 'kind': 'stats', 'amount': 10, 'earned': False}})
        options.append(TrainingCandidate(training_type=kind, stats_gain=gains,
            failure_rate=failure, vitality_delta=5 if kind == 'wisdom' else -18 - i,
            support_count=len(people), confidence=.99,
            raw_data={'effective_support_count': len(people), 'capture': deepcopy(source),
                'stats_gain_ocr': {'columns': [_gain_column(k, v) for k, v in gains.items()]},
                'failure_rate_ocr': {'status': 'accepted', 'selected_tab': kind, 'value': failure}},
            scenario_mechanics={'larc': {'training_participants': {
                'schema_version': 'larc_training_participants.v1', 'status': 'accepted',
                'training_type': kind, 'participants': people}}}))
    stats = dict(zip(STAT_KEYS, (250, 220, 240, 200, 230, 150))) if year == 1 else dict(zip(STAT_KEYS, (680, 530, 600, 390, 570, 250)))
    if year == 3:
        stats = dict(zip(STAT_KEYS, (1120, 790, 920, 520, 1000, 400)))
    context = {'scenario': 'larc', 'mood': 'good', 'larc_remaining_turns': 12,
        'larc_period': {'status': 'verified', 'value': period},
        'larc_ss_entry': {'status': 'accepted', 'stage': 'entry', 'ready_count': 4, 'source': deepcopy(source)},
        'larc_friend_outing': {'schema_version': 'larc_friend_outing.v1', 'status': 'accepted',
            'character': 'satake_mei', 'unlocked': True, 'completed': 1, 'pending': None,
            'source': deepcopy(source)}}
    state = TrainingTurnState(screen='training_menu', turn=turn, vitality=vitality,
        current_stats=stats, training_options=options, scenario_state=context)
    preview = {'status': 'accepted', 'stage': 'preview', 'kind': 'SSS', 'ready_count': 5,
        'stats_gain': {**dict.fromkeys(TRAINING_TYPES, 12), 'skill_pt': 15},
        'supporter_points_if_all_win': 100,
        'participants_observation': {'status': 'accepted', 'participants': [
            {'slot': i, 'win_guaranteed': True,
             'bond': {'status': 'accepted', 'color': 'green', 'fill_ratio_estimate': .65},
             'current_reward': {'status': 'accepted', 'kind': 'stats', 'amount': 10}}
            for i in range(5)]}}
    return state, preview, {'key': key, 'label': label, 'period': period,
        'vitality': vitality, 'remaining_turns': 12}


def _goal(profile, *, workflow_disabled=False):
    from core.auto_trainer.larc_strategy_versions import CURRENT_REVISION
    preferences = deepcopy(profile['preferences'])
    if workflow_disabled:
        # Match the user's actual off switch, including its stored legacy mode.
        flow = preferences.get('workflow')
        if isinstance(flow, dict):
            flow['enabled'] = False
            if isinstance(flow.get('editor_graph'), dict):
                flow['editor_graph']['enabled'] = False
    return replace(UGParentGoal.default(), scenario='larc',
        larc_policy={'variant': 'baseline', 'revision': CURRENT_REVISION,
            'decision_preferences': preferences})


def _evaluate(state, goal, preview):
    local = LarcOpeningStrategy(goal).evaluate(state)
    compared = choose_larc_ss(state, goal, preview, ss_executions=[])
    rows = deepcopy(compared.get('training_candidates') or local.scored_candidates)
    if type(compared.get('score')) in (int, float):
        rows.append({**deepcopy(compared), 'action_id': 'ss', 'action': 'ss',
                     'rejected': False, 'risk_flags': []})
    action = compared.get('local_action', local.action)
    target = compared.get('local_target', local.target)
    selected = ('ss' if compared.get('selected') else
        'train:' + target if action == 'train' else
        'outing.' + target.replace(':', '.') if action == 'outing' else
        'rest.normal' if action == 'rest' else None)
    return {r['action_id']: r for r in rows}, selected, compared


def _label(row):
    if row.get('training_type') in TRAINING_LABELS:
        return TRAINING_LABELS[row['training_type']]
    return 'SSS / SS' if row['action_id'] == 'ss' else '黄帽外出' if row['action_id'].startswith('outing.satake_mei') else '普通外出' if row['action_id'] == 'outing.normal' else '休息'


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('示例评分包含无效数字。')
    return float(value)


def _explain(base, changed):
    trace = deepcopy(changed.get('score_workflow') or {})
    applied = trace.get('status') == 'applied'
    effects = trace.get('effects', {}) if applied else {}
    factors = effects.get('component_factors', {})
    multiplier = trace.get('training_multiplier', 1.) if applied else 1.
    base_parts = base.get('components') or {}
    raw_parts = changed.get('components') or {}
    keys = list(dict.fromkeys([*base_parts, *raw_parts, *factors]))
    breakdown = []
    for key in keys:
        if key in {'custom_workflow_adjustment', 'custom_tuning_adjustment'}:
            continue
        before, raw = _number(base_parts.get(key, 0.)), _number(raw_parts.get(key, 0.))
        if not before and not raw and key not in factors:
            continue
        catalog = tuning.CATALOG.get(key, {})
        factor = factors.get(key, 1.)
        effective = raw * factor * multiplier
        breakdown.append({'key': key, 'label': catalog.get('label', '其他贡献：' + key),
            'group': catalog.get('group', 'unclassified'), 'catalogued': bool(catalog),
            'base': before, 'raw': raw, 'factor': factor, 'effective': effective})
    baseline_total, modified_total = _number(base['score']), _number(changed['score'])
    baseline_residual = baseline_total - sum(item['base'] for item in breakdown)
    residual = modified_total - sum(item['effective'] for item in breakdown)
    return {'id': changed['action_id'], 'label': _label(changed), 'action': action_kind(changed),
        'training_type': changed.get('training_type'), 'baseline_total': baseline_total,
        'modified_total': modified_total, 'delta': modified_total - baseline_total,
        'rejected': bool(changed.get('rejected')), 'risk_flags': changed.get('risk_flags', []),
        'priority_rank': trace.get('priority_rank', 0),
        'priority_active': trace.get('priority_active', False), 'training_multiplier': multiplier,
        'breakdown': breakdown, 'baseline_residual': baseline_residual, 'residual': residual,
        'residual_note': '四舍五入、旧高级模式／未列入分项的差额；已保留以与真实总分对齐。',
        'trace': trace, 'baseline_tuning': deepcopy(base.get('score_tuning') or {}),
        'modified_tuning': deepcopy(changed.get('score_tuning') or {}),
        'unknown_fields': changed.get('unknown_fields', [])}


def preview(request):
    if not isinstance(request, dict):
        raise ValueError('预览请求应包含方案与示例场景。')
    profile = checked_profile(request.get('profile'))
    key = request.get('scenario', 'classic_domestic')
    state, ss, description = sample_state(key)
    modified_goal = _goal(profile)
    flow_enabled = (profile['preferences'].get('workflow') or {}).get('enabled') is True
    # Disabled workflow means the complete existing legacy behavior remains.
    base_goal = _goal(profile, workflow_disabled=True) if flow_enabled else modified_goal
    base_rows, base_id, _ = _evaluate(state, base_goal, ss)
    changed_rows, changed_id, compared = _evaluate(state, modified_goal, ss)
    rows = [_explain(base_rows[identifier], changed)
        for identifier, changed in changed_rows.items() if identifier in base_rows]
    rows.sort(key=lambda row: (row['rejected'], row['priority_rank'], -row['modified_total'], row['id']))
    phase_key = phase(state)
    workflow = resolve(state, modified_goal)
    workflow.update(enabled=flow_enabled, matched=workflow.get('status') == 'matched',
        effects_applied=any((r.get('score_workflow') or {}).get('status') == 'applied' for r in changed_rows.values()),
        advanced_suspended=flow_enabled)
    return {'ok': True, 'schema_version': SCHEMA, 'sample': True, 'sample_note': SAMPLE_NOTE,
        'device_actions': 0, 'network_requests': 0, 'real_user_data_read': False,
        'scenario': description, 'scenarios': [{'key': key, 'label': value[0]} for key, value in SCENARIOS.items()],
        'workflow': workflow, 'catalog_count': len(tuning.CATALOG),
        'phase': {'key': phase_key, 'label': PHASE_LABELS[phase_key],
            'coefficients': PHASE_WEIGHTS[phase_key], 'effective_coefficients': phase_weights(state, modified_goal)},
        'base_preferences': values(modified_goal), 'candidates': rows,
        'selection': {'baseline_id': base_id, 'modified_id': changed_id,
            'ss_reason': compared.get('reason')},
        'notes': ['基础偏好保持；基线只关闭工作流，保留其暂存的旧高级模式后独立重算。启用工作流时旧高级模式暂停。',
            'base 是基线贡献；raw 已包含本方案属性效用层，之后只应用一次最终分项倍率和训练倍率。',
            '优先行动先比较类别层级，再在同一层级比较分数；优先级不算作加分。',
            '远征没有 SS 或黄帽外出时不伪造候选；示例的未覆盖资料不代表实局覆盖率。']}


def main():
    if '--schema' in sys.argv:
        result = tuning.schema()
        result['preview_scenarios'] = [{'key': key, 'label': value[0]} for key, value in SCENARIOS.items()]
        result['phase_coefficients'] = [{ 'key': key, 'label': PHASE_LABELS[key], 'coefficients': value}
            for key, value in PHASE_WEIGHTS.items()]
    else:
        try:
            result = preview(json.loads(sys.stdin.read()))
        except (ValueError, TypeError, KeyError) as error:
            result = {'ok': False, 'schema_version': SCHEMA, 'sample': True,
                'error': str(error), 'candidates': []}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
