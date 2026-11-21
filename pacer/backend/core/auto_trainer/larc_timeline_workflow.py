"""Calendar rules over existing eligible scores. No observation, files or input."""
from core.auto_trainer.rejection_policy import is_hard_rejected
from copy import deepcopy
import math
import re

SCHEMA = 'uma.timeline_workflow.v1'
SEMANTICS = {'model': 'uma.larc.calendar_workflow', 'version': 1}
FIELDS = {'vitality': '体力', 'speed': '速度', 'stamina': '耐力', 'power': '力量',
          'guts': '毅力', 'wisdom': '智力', 'skill_pt': '技能 Pt',
          'calendar_year': '育成年份', 'calendar_month': '育成月份',
          'calendar_turn': '日历回合（1～72）', 'remaining_turns': '距当前目标剩余回合'}
TRAINING_TYPES = ('speed', 'stamina', 'power', 'guts', 'wisdom')
ACTIONS = ('train', 'rest', 'outing', 'ss')
OPERATORS = ('lt', 'lte', 'eq', 'gte', 'gt')
ADAPTATION_PHASES = ('guts_expedition', 'stamina_expedition', 'power_expedition',
    'speed_expedition', 'french_three', 'expedition_skill_pt', 'courage_three', 'willpower_three')
DEFAULT = {'schema_version': SCHEMA, 'enabled': False,
           'evaluation': 'calendar_recheck', 'stages': []}
LIMITS = {'stages': 16, 'rules': 16, 'conditions': 12, 'factor_min': 0, 'factor_max': 10}


def schema():
    return {'schema_version': SCHEMA, 'semantics': deepcopy(SEMANTICS),
            'defaults': deepcopy(DEFAULT), 'fields': [{'key': k, 'label': v} for k, v in FIELDS.items()],
            'operators': list(OPERATORS), 'actions': list(ACTIONS),
            'training_types': list(TRAINING_TYPES), 'window_phases': ['all', 'pre_debut', 'dated', 'year'],
            'condition_groups': ['all', 'any'], 'limits': dict(LIMITS),
            'evaluation': 'calendar_recheck', 'matching': 'first_matching_window_and_true_rule',
            'unknown': 'skip_rule_and_continue', 'empty_conditions': 'always',
            'editor_graph': {'schema_version': 'uma.weight_graph.v1',
                'local_profile_max_utf8_bytes': 1024 * 1024,
                'profile_without_graph_max_utf8_bytes': 64 * 1024,
                'sharing': 'local_file_only', 'layout_changes_scoring': False},
            'adaptation_phases': list(ADAPTATION_PHASES),
            'attribute_weight_bounds': {'exclusive_min': 0, 'max': 10},
            'legacy_tuning': 'preserved_but_suspended_while_workflow_enabled'}


def _object(value, allowed, message):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(message)


def _text(value, label, *, identifier=False):
    if not isinstance(value, str) or not value.strip() or len(value) > (64 if identifier else 100):
        raise ValueError(label + '不能为空且不能过长。')
    if any(ord(c) < 32 for c in value) or identifier and not re.fullmatch(r'[A-Za-z0-9_.-]+', value):
        raise ValueError(label + '格式不正确。')
    return value.strip()


def _flag(value, label):
    if type(value) is not bool:
        raise ValueError(label + '必须为启用或关闭。')
    return value


def _date(value):
    if value is None:
        return None
    _object(value, ('year', 'month', 'half'), '工作流日期格式不正确。')
    if (set(value) != {'year', 'month', 'half'} or type(value['year']) is not int
            or value['year'] not in (1, 2, 3) or type(value['month']) is not int
            or not 1 <= value['month'] <= 12 or value['half'] not in ('first', 'second')):
        raise ValueError('工作流日期请选择年份、月份和上旬或下旬。')
    return dict(value)


def _ordinal(value):
    return (value['year'] - 1) * 24 + (value['month'] - 1) * 2 + (value['half'] == 'second')


def _factors(value, allowed):
    _object(value, allowed, '工作流倍率包含未知项目。')
    result = {}
    for key, amount in value.items():
        if type(amount) not in (int, float) or not math.isfinite(amount) or not 0 <= amount <= 10:
            raise ValueError('工作流倍率应在0～10之间。')
        result[key] = float(amount)
    return result


def _positive_factors(value):
    checked = _factors(value, TRAINING_TYPES)
    if any(amount <= 0 for amount in checked.values()):
        raise ValueError('属性收益倍率应大于0且不超过10。')
    return checked


def _adaptation(value):
    _object(value, ('mode', 'priorities', 'reserve_points'), '适应性安排格式不正确。')
    mode = value.get('mode', 'auto'); priorities = value.get('priorities', [])
    reserve = value.get('reserve_points', 0)
    if mode not in ('auto', 'prefer', 'defer'):
        raise ValueError('适应性安排请选择自动、优先或暂缓。')
    if (not isinstance(priorities, list) or any(not isinstance(p, str) or p not in ADAPTATION_PHASES for p in priorities)
            or len(priorities) != len(set(priorities))):
        raise ValueError('适应性优先项应为现有项目的不重复顺序。')
    if type(reserve) is not int or reserve < 0:
        raise ValueError('适应性保留Pt应为非负整数。')
    return {'mode': mode, 'priorities': list(priorities), 'reserve_points': reserve}


def _normalize_plain(value=None):
    from core.auto_trainer.larc_score_tuning import CATALOG
    if value is None:
        return deepcopy(DEFAULT)
    _object(value, DEFAULT, '时间轴工作流包含未知字段。')
    result = {**deepcopy(DEFAULT), **value}
    if result['schema_version'] != SCHEMA or result['evaluation'] != 'calendar_recheck':
        raise ValueError('时间轴工作流版本或运行语义不兼容。')
    result['enabled'] = _flag(result['enabled'], '工作流启停')
    if not isinstance(result['stages'], list) or len(result['stages']) > LIMITS['stages']:
        raise ValueError('工作流最多16个阶段。')
    stages, stage_ids = [], set()
    for stage in result['stages']:
        _object(stage, ('id', 'label', 'enabled', 'window', 'rules'), '工作流阶段格式不正确。')
        identifier = _text(stage.get('id'), '阶段编号', identifier=True)
        if identifier in stage_ids:
            raise ValueError('阶段编号不能重复。')
        stage_ids.add(identifier)
        window = stage.get('window', {'phase': 'all', 'from': None, 'to': None})
        _object(window, ('phase', 'year', 'from', 'to'), '阶段时间范围格式不正确。')
        window = {'phase': 'all', 'from': None, 'to': None, **window}
        if window['phase'] not in ('all', 'pre_debut', 'dated', 'year'):
            raise ValueError('阶段时间类型不正确。')
        window['from'], window['to'] = _date(window['from']), _date(window['to'])
        if window['phase'] not in ('dated', 'year') and (window['from'] is not None or window['to'] is not None):
            raise ValueError('只有日期阶段可以设置日期边界。')
        if window['from'] is not None and window['to'] is not None and _ordinal(window['from']) > _ordinal(window['to']):
            raise ValueError('阶段结束日期不能早于开始日期。')
        if window['phase'] == 'year':
            if type(window.get('year')) is not int or window['year'] not in (1, 2, 3):
                raise ValueError('全年阶段请选择第一年、第二年或第三年。')
            if any(bound is not None and bound['year'] != window['year'] for bound in (window['from'], window['to'])):
                raise ValueError('全年阶段的日期边界必须在所选年份。')
        elif 'year' in window:
            raise ValueError('只有全年阶段可以设置年份。')
        rules = stage.get('rules', [])
        if not isinstance(rules, list) or len(rules) > LIMITS['rules']:
            raise ValueError('每个阶段最多16条规则。')
        checked_rules, rule_ids = [], set()
        for rule in rules:
            _object(rule, ('id', 'label', 'enabled', 'when', 'effects'), '工作流规则格式不正确。')
            rule_id = _text(rule.get('id'), '规则编号', identifier=True)
            if rule_id in rule_ids:
                raise ValueError('同一阶段的规则编号不能重复。')
            rule_ids.add(rule_id)
            when = rule.get('when', {'all': []})
            if not isinstance(when, dict) or len(when) != 1 or next(iter(when)) not in ('all', 'any'):
                raise ValueError('规则条件请选择全部满足或任一满足。')
            group = next(iter(when)); conditions = when[group]
            if not isinstance(conditions, list) or len(conditions) > LIMITS['conditions']:
                raise ValueError('每条规则最多12个条件。')
            checked_conditions = []
            for condition in conditions:
                _object(condition, ('field', 'op', 'value'), '工作流条件格式不正确。')
                amount = condition.get('value')
                if (set(condition) != {'field', 'op', 'value'} or not isinstance(condition.get('field'), str)
                        or condition['field'] not in FIELDS or condition.get('op') not in OPERATORS or type(amount) not in (int, float)
                        or not math.isfinite(amount) or not 0 <= amount <= 100000
                        or condition['field'] == 'vitality' and amount > 100
                        or condition['field'] in ('calendar_year', 'calendar_month', 'calendar_turn', 'remaining_turns') and type(amount) is not int
                        or condition['field'] == 'calendar_year' and not 1 <= amount <= 3
                        or condition['field'] == 'calendar_month' and not 1 <= amount <= 12
                        or condition['field'] == 'calendar_turn' and not 1 <= amount <= 72):
                    raise ValueError('工作流条件字段、比较符或数值不正确。')
                checked_conditions.append(dict(condition))
            effects = rule.get('effects', {})
            _object(effects, ('preferred_actions', 'training_multipliers', 'component_factors',
                              'attribute_weights', 'after_target_credit', 'adaptation'), '工作流安排包含未知字段。')
            order = effects.get('preferred_actions', [])
            if (not isinstance(order, list) or any(action not in ACTIONS for action in order)
                    or len(order) != len(set(order))):
                raise ValueError('优先行动必须是训练、休息、外出、SS的不重复顺序。')
            optional_effects = {}
            if 'attribute_weights' in effects:
                optional_effects['attribute_weights'] = _positive_factors(effects['attribute_weights'])
            if 'after_target_credit' in effects:
                amount = effects['after_target_credit']
                if type(amount) not in (int, float) or not math.isfinite(amount) or not 0 < amount <= 1:
                    raise ValueError('达标后收益折扣应大于0且不超过1。')
                optional_effects['after_target_credit'] = float(amount)
            if 'adaptation' in effects:
                optional_effects['adaptation'] = _adaptation(effects['adaptation'])
            checked_rules.append({'id': rule_id, 'label': _text(rule.get('label'), '规则名称'),
                'enabled': _flag(rule.get('enabled', True), '规则启停'), 'when': {group: checked_conditions},
                'effects': {'preferred_actions': list(order),
                    'training_multipliers': _factors(effects.get('training_multipliers', {}), TRAINING_TYPES),
                    'component_factors': _factors(effects.get('component_factors', {}), CATALOG), **optional_effects}})
        stages.append({'id': identifier, 'label': _text(stage.get('label'), '阶段名称'),
            'enabled': _flag(stage.get('enabled', True), '阶段启停'), 'window': window, 'rules': checked_rules})
    result['stages'] = stages
    return result


def normalize(value=None):
    if value is None or not isinstance(value, dict) or 'editor_graph' not in value:
        return _normalize_plain(value)
    from core.auto_trainer.larc_weight_graph import compile_graph
    plain = _normalize_plain({key: item for key, item in value.items() if key != 'editor_graph'})
    compiled = compile_graph(value['editor_graph'])
    if plain != {key: item for key, item in compiled.items() if key != 'editor_graph'}:
        raise ValueError('节点画布与实际执行安排不一致，请重新生成权重代码。')
    return compiled


def enabled(goal):
    from core.auto_trainer.larc_user_preferences import values
    return (values(goal).get('workflow') or {}).get('enabled') is True


def _window(state, window):
    from core.auto_trainer.larc_recovery import period_value
    period = period_value(state)
    if window['phase'] == 'all':
        return True, []
    pre_debut = period.get('pre_debut') is True or period.get('stage') == 'pre_debut'
    if window['phase'] == 'pre_debut':
        known_later = period.get('stage') in ('junior', 'classic', 'senior') or (
            period.get('year_index') in (2, 3) or type(period.get('month')) is int)
        return pre_debut, [] if pre_debut or known_later else ['pre_debut']
    year, month, half = period.get('year_index'), period.get('month'), period.get('half')
    if window['phase'] == 'year':
        if type(year) is not int or year not in (1, 2, 3):
            return False, ['calendar_year']
        if year != window['year']:
            return False, []
        if window['from'] is None and window['to'] is None:
            return True, []
        if pre_debut:
            return window['from'] is None, []
    if (pre_debut or type(year) is not int or year not in (1, 2, 3)
            or type(month) is not int or not 1 <= month <= 12 or half not in ('first', 'second')):
        return False, ['date']
    day = _ordinal({'year': year, 'month': month, 'half': half})
    return ((window['from'] is None or day >= _ordinal(window['from']))
            and (window['to'] is None or day <= _ordinal(window['to']))), []


def _condition_value(state, field):
    if field == 'vitality':
        return state.vitality
    if field == 'remaining_turns':
        value = state.scenario_state.get('larc_remaining_turns')
        return value if type(value) is int and value >= 0 else None
    if field.startswith('calendar_'):
        from core.auto_trainer.larc_recovery import period_value
        period = period_value(state)
        year, month, half = period.get('year_index'), period.get('month'), period.get('half')
        if type(year) is not int or year not in (1, 2, 3):
            return None
        if field == 'calendar_year':
            return year
        if period.get('pre_debut') is True or period.get('stage') == 'pre_debut':
            return None
        if type(month) is not int or not 1 <= month <= 12:
            return None
        if field == 'calendar_month':
            return month
        if half not in ('first', 'second'):
            return None
        return _ordinal({'year': year, 'month': month, 'half': half}) + 1
    return state.current_stats.get(field)


def _conditions(state, when):
    group = next(iter(when)); observations = []
    for condition in when[group]:
        field, op, expected = condition['field'], condition['op'], condition['value']
        actual = _condition_value(state, field)
        if type(actual) not in (int, float) or not math.isfinite(actual) or actual < 0:
            actual, outcome = None, 'unknown'
        else:
            outcome = {'lt': actual < expected, 'lte': actual <= expected, 'eq': actual == expected,
                       'gte': actual >= expected, 'gt': actual > expected}[op]
        observations.append({**condition, 'observed': actual, 'result': outcome})
    outcomes = [row['result'] for row in observations]
    if not outcomes:
        outcome = True
    elif group == 'all':
        outcome = False if False in outcomes else 'unknown' if 'unknown' in outcomes else True
    else:
        outcome = True if True in outcomes else 'unknown' if 'unknown' in outcomes else False
    return outcome, observations


def resolve(state, goal):
    from core.auto_trainer.larc_user_preferences import values
    config = values(goal).get('workflow') or DEFAULT
    if not config['enabled']:
        return {'active': False, 'status': 'disabled', 'changed': False}
    checked, unknown = [], []
    for stage in config['stages']:
        if not stage['enabled']:
            continue
        matches, missing = _window(state, stage['window'])
        unknown.extend(missing)
        if not matches:
            continue
        for rule in stage['rules']:
            if not rule['enabled']:
                continue
            outcome, observations = _conditions(state, rule['when'])
            unknown.extend(row['field'] for row in observations if row['result'] == 'unknown')
            checked.append({'stage_id': stage['id'], 'rule_id': rule['id'],
                            'result': outcome, 'conditions': observations})
            if outcome is True:
                return {'active': True, 'status': 'matched', 'changed': False,
                    'evaluation': 'calendar_recheck', 'stage_id': stage['id'], 'stage_label': stage['label'],
                    'rule_id': rule['id'], 'rule_label': rule['label'], 'conditions': observations,
                    'effects': deepcopy(rule['effects']), 'checked_rules': checked,
                    'unknown_fields': list(dict.fromkeys(unknown)), 'legacy_tuning_suspended': True}
    return {'active': True, 'status': 'unmatched', 'changed': False, 'evaluation': 'calendar_recheck',
            'checked_rules': checked, 'unknown_fields': list(dict.fromkeys(unknown)),
            'legacy_tuning_suspended': True}


def action_kind(row):
    if row.get('action') in ACTIONS:
        return row['action']
    identifier = row.get('action_id', '')
    return ('train' if row.get('training_type') or identifier.startswith('train:') else
            'ss' if identifier == 'ss' else 'rest' if identifier.startswith('rest.') else
            'outing' if identifier.startswith('outing.') else '')


def selection_key(row):
    detail = row.get('score_workflow') or {}
    return (detail.get('priority_rank', 0), -row['score'], row.get('action_id', ''))


def prefers_ss(ss_row, selected_row):
    # Preserve the existing SS-on-equal-score tie break within one action tier.
    return selection_key(ss_row)[:2] <= selection_key(selected_row)[:2]


def apply(state, goal, rows):
    trace = resolve(state, goal)
    if not trace['active']:
        return trace
    plans = []
    for row in rows:
        if is_hard_rejected(row):
            continue
        detail = deepcopy(trace)
        if trace['status'] != 'matched':
            plans.append((row, row['score'], detail))
            continue
        effects = trace['effects']; parts = row.get('components') or {}; base = row['score']
        changes = [{'key': key, 'base': parts.get(key, 0), 'factor': factor,
                    'adjustment': parts.get(key, 0) * (factor - 1)}
                   for key, factor in effects['component_factors'].items() if parts.get(key)]
        subtotal = base + sum(change['adjustment'] for change in changes)
        multiplier = effects['training_multipliers'].get(row.get('training_type'), 1.)
        score = subtotal * multiplier
        if not math.isfinite(score) or abs(score) > 1000000:
            raise ValueError('工作流评分超出允许范围，本次使用原策略。')
        order = effects['preferred_actions']; action = action_kind(row)
        rank = order.index(action) if action in order else len(order)
        detail.update(status='applied', base_score=base, base_rejected=row.get('rejected', False),
            component_changes=changes, component_subtotal=round(subtotal, 4),
            training_multiplier=multiplier, priority_rank=rank, priority_active=bool(order),
            adjustment=round(score - base, 4))
        plans.append((row, round(score, 4), detail))
    changed = False
    for row, score, detail in plans:
        if detail['status'] == 'applied':
            changed |= bool(detail['adjustment']) or detail['priority_active']
            row.setdefault('components', {})['custom_workflow_adjustment'] = detail['adjustment']
        row['score'], row['score_workflow'] = score, detail
    return {**trace, 'changed': changed}
