"""Typed editor graph compiled to the existing calendar workflow. No game input."""
from copy import deepcopy
import math
import re

SCHEMA = 'uma.weight_graph.v1'
WEIGHT_GROUPS = {
    'score_attributes': ('stats', 'dynamic_attribute_gap', 'skill_pt', 'first_arc_speed_preparation', 'high_stat_target_scarcity'),
    'score_bond': ('effective_bond', 'training_hint_bond', 'friend_startup', 'friend_coverage_preparation', 'mei_initial_contact_preference', 'user_gold_ship_bonus', 'user_mei_bonus'),
    'score_charge': ('net_charge', 'newly_full', 'ss_preparation', 'cycle_reward_preparation', 'charming_charge_progress', 'future_recovery_cycle_progress', 'mei_expected_charge', 'full_group_preference', 'battery_release_opportunity', 'first_charming_preference'),
    'score_recovery': ('vitality', 'recovery', 'known_recovery', 'rule_recovery', 'energy_continuity', 'recovery_timing_preference', 'opening_preparation_preference', 'healing_charge_tie_preference', 'mood_recovery', 'outing_chain_progress', 'outing_completion_increment', 'outing_skill_hint_option'),
    'score_stage': ('conditional_fan_route', 'expectation_training_progress', 'adaptation_early_use', 'phase_strong_training_opportunity', 'supporter_deadline_preference', 'baseline_support'),
    'score_risk': ('failure_penalty', 'failure_expected_loss', 'risk_aversion_preference'),
}
PORTS = {
    'start': ({}, {'next': 'stage_flow'}),
    'stage': ({'flow': 'stage_flow'}, {'next': 'stage_flow', 'rules': 'rule_flow'}),
    'condition': ({}, {'value': 'condition'}),
    'priority': ({}, {'value': 'priority'}),
    'weights': ({'base': 'weights', **{key: key for key in WEIGHT_GROUPS}}, {'value': 'weights'}),
    'attributes': ({}, {'value': 'attributes'}),
    'adaptation': ({}, {'value': 'adaptation'}),
    'rule': ({'flow': 'rule_flow', 'condition': 'condition', 'priority': 'priority',
              'weights': 'weights', 'attributes': 'attributes', 'adaptation': 'adaptation'}, {'next': 'rule_flow'}),
}
PORTS.update({key: ({}, {'value': key}) for key in WEIGHT_GROUPS})
LABELS = {'start': '每回合开始', 'stage': '育成阶段', 'condition': '体力低于40',
          'priority': '优先行动', 'weights': '训练权重', 'rule': '安排输出',
          'attributes': '属性收益权重', 'adaptation': '适应性购买安排'}
LABELS.update({'score_attributes': '属性与技能点', 'score_bond': '羁绊启动', 'score_charge': '充电与 SS', 'score_recovery': '体力与心情', 'score_stage': '阶段目标', 'score_risk': '风险偏好'})


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) - set(keys):
        raise ValueError(label + '格式不正确。')


def _text(value, label, identifier=False):
    if (not isinstance(value, str) or not value.strip() or len(value) > (64 if identifier else 100)
            or any(ord(c) < 32 for c in value)
            or identifier and not re.fullmatch(r'[A-Za-z0-9_.-]+', value)):
        raise ValueError(label + '格式不正确。')
    return value.strip()


def _plain(value):
    from core.auto_trainer.larc_timeline_workflow import _normalize_plain
    return _normalize_plain(value)


def _data(kind, value, identifier):
    """Use the execution schema for every data node, including detached drafts."""
    keys = {'start': (), 'stage': ('enabled', 'window', 'workflow_id'),
            'condition': ('when',), 'priority': ('preferred_actions',),
            'weights': ('training_multipliers', 'component_factors'),
            'attributes': ('attribute_weights', 'after_target_credit'),
            'adaptation': ('mode', 'priorities', 'reserve_points'),
            'rule': ('enabled', 'workflow_id'),
            **{key: ('component_factors',) for key in WEIGHT_GROUPS}}[kind]
    _object(value, keys, '节点 ' + identifier + ' 的参数')
    if kind in WEIGHT_GROUPS:
        _object(value.get('component_factors', {}), WEIGHT_GROUPS[kind], '节点 ' + identifier + ' 的分组倍率')
    stage = {'id': 's', 'label': 's', 'rules': []}
    rule = {'id': 'r', 'label': 'r'}
    if kind == 'stage':
        stage.update({k: deepcopy(v) for k, v in value.items() if k != 'workflow_id'})
    elif kind == 'rule':
        rule['enabled'] = value.get('enabled', True)
    elif kind == 'condition':
        rule['when'] = deepcopy(value.get('when', {'all': []}))
    elif kind == 'attributes':
        rule['effects'] = deepcopy(value)
    elif kind == 'adaptation':
        rule['effects'] = {'adaptation': deepcopy(value)}
    elif kind in ('priority', 'weights') or kind in WEIGHT_GROUPS:
        rule['effects'] = deepcopy(value)
    if kind != 'stage':
        stage['rules'] = [rule]
    try:
        checked = _plain({'stages': [stage]})['stages'][0]
    except ValueError as error:
        raise ValueError('节点 ' + identifier + '：' + str(error)) from error
    if kind == 'start':
        result = {}
    elif kind == 'stage':
        result = {k: checked[k] for k in ('enabled', 'window')}
    elif kind == 'rule':
        result = {'enabled': checked['rules'][0]['enabled']}
    elif kind == 'condition':
        result = {'when': checked['rules'][0]['when']}
    elif kind == 'attributes':
        effects = checked['rules'][0]['effects']
        result = {key: effects[key] for key in keys if key in effects}
    elif kind == 'adaptation':
        result = checked['rules'][0]['effects']['adaptation']
    else:
        result = {key: checked['rules'][0]['effects'][key] for key in keys}
    if 'workflow_id' in value:
        result['workflow_id'] = _text(value['workflow_id'], '节点 ' + identifier + ' 的原编号', True)
    return result


def normalize_graph(value):
    _object(value, ('schema_version', 'enabled', 'entry', 'nodes', 'edges'), '节点画布')
    if value.get('schema_version') != SCHEMA or type(value.get('enabled')) is not bool:
        raise ValueError('节点画布版本或启停格式不正确。')
    if not isinstance(value.get('nodes'), list) or not isinstance(value.get('edges'), list):
        raise ValueError('节点画布需要节点和连线列表。')
    nodes, by_id = [], {}
    for raw in value['nodes']:
        _object(raw, ('id', 'type', 'label', 'x', 'y', 'data'), '画布节点')
        identifier = _text(raw.get('id'), '节点编号', True)
        kind = raw.get('type')
        if not isinstance(kind, str) or kind not in PORTS or identifier in by_id:
            raise ValueError('节点 ' + identifier + '：类型未知或编号重复。')
        if any(type(raw.get(axis)) not in (int, float) or not math.isfinite(raw[axis]) for axis in ('x', 'y')):
            raise ValueError('节点 ' + identifier + '：位置必须是有限数字。')
        node = {'id': identifier, 'type': kind,
                'label': _text(raw.get('label', LABELS[kind]), '节点 ' + identifier + ' 名称'),
                'x': raw['x'], 'y': raw['y'], 'data': _data(kind, raw.get('data', {}), identifier)}
        by_id[identifier] = node
        nodes.append(node)
    entry = _text(value.get('entry'), '起点编号', True)
    if entry not in by_id or by_id[entry]['type'] != 'start':
        raise ValueError('画布起点必须指向一个每回合开始节点。')
    inputs, outputs, edge_ids, edges = {}, {}, set(), []
    for raw in value['edges']:
        _object(raw, ('id', 'from', 'port', 'to', 'input'), '画布连线')
        edge = {k: _text(raw.get(k), '连线 ' + k, True) for k in ('id', 'from', 'port', 'to', 'input')}
        if edge['id'] in edge_ids:
            raise ValueError('连线编号重复：' + edge['id'])
        edge_ids.add(edge['id'])
        if edge['from'] not in by_id or edge['to'] not in by_id:
            raise ValueError('连线 ' + edge['id'] + '：找不到对应节点。')
        source, target = by_id[edge['from']], by_id[edge['to']]
        output_type = PORTS[source['type']][1].get(edge['port'])
        input_type = PORTS[target['type']][0].get(edge['input'])
        if output_type is None or output_type != input_type:
            raise ValueError('连线 ' + edge['id'] + '：节点 ' + source['id'] + ' 与 ' + target['id'] + ' 的端口类型不匹配。')
        in_key, out_key = (target['id'], edge['input']), (source['id'], edge['port'])
        if in_key in inputs or output_type.endswith('_flow') and out_key in outputs:
            raise ValueError('连线 ' + edge['id'] + '：节点输入或顺序输出只能连接一次。')
        inputs[in_key] = edge
        outputs[out_key] = edge
        edges.append(edge)
    # Include detached data chains: a base-weights cycle is an editor configuration error.
    adjacency = {identifier: [] for identifier in by_id}
    for edge in edges:
        adjacency[edge['from']].append(edge['to'])
    marks = {}
    def visit(identifier):
        if marks.get(identifier) == 1:
            raise ValueError('节点 ' + identifier + '：阶段、安排或基础权重连线不能形成循环。')
        if marks.get(identifier) == 2:
            return
        marks[identifier] = 1
        for target in adjacency[identifier]:
            visit(target)
        marks[identifier] = 2
    for identifier in by_id:
        visit(identifier)
    return {'schema_version': SCHEMA, 'enabled': value['enabled'], 'entry': entry,
            'nodes': nodes, 'edges': edges}


def _compiled(graph):
    by_id = {n['id']: n for n in graph['nodes']}
    output = {(e['from'], e['port']): e['to'] for e in graph['edges']}
    inputs = {(e['to'], e['input']): e['from'] for e in graph['edges']}
    used = {graph['entry']}
    weight_cache = {}
    def resolve_weights(identifier):
        used.add(identifier)
        if identifier in weight_cache:
            return deepcopy(weight_cache[identifier])
        base_id = inputs.get((identifier, 'base'))
        result = resolve_weights(base_id) if base_id else {'training_multipliers': {}, 'component_factors': {}}
        source = by_id[identifier]['data']
        for key in ('training_multipliers', 'component_factors'):
            result[key].update(deepcopy(source[key]))
        for group in WEIGHT_GROUPS:
            group_id = inputs.get((identifier, group))
            if group_id:
                used.add(group_id)
                result['component_factors'].update(deepcopy(by_id[group_id]['data']['component_factors']))
        weight_cache[identifier] = deepcopy(result)
        return result
    stages, stage_ids = [], set()
    stage_id = output.get((graph['entry'], 'next'))
    while stage_id:
        stage = by_id[stage_id]; used.add(stage_id)
        if len(stages) == 16:
            raise ValueError('节点 ' + stage_id + '：工作流最多16个阶段。')
        workflow_id = stage['data'].get('workflow_id', stage_id)
        if workflow_id in stage_ids:
            raise ValueError('节点 ' + stage_id + '：阶段执行编号重复。')
        stage_ids.add(workflow_id)
        rules, rule_ids, rule_id = [], set(), output.get((stage_id, 'rules'))
        while rule_id:
            node = by_id[rule_id]; used.add(rule_id)
            if len(rules) == 16:
                raise ValueError('节点 ' + rule_id + '：每个阶段最多16条安排。')
            workflow_rule_id = node['data'].get('workflow_id', rule_id)
            if workflow_rule_id in rule_ids:
                raise ValueError('节点 ' + rule_id + '：同一阶段的安排执行编号重复。')
            rule_ids.add(workflow_rule_id)
            when, effects = {'all': []}, {}
            for port in ('condition', 'priority', 'weights', 'attributes', 'adaptation'):
                source_id = inputs.get((rule_id, port))
                if source_id:
                    used.add(source_id)
                    data = resolve_weights(source_id) if port == 'weights' else by_id[source_id]['data']
                    if port == 'condition':
                        when = deepcopy(data['when'])
                    elif port == 'adaptation':
                        effects['adaptation'] = deepcopy(data)
                    else:
                        effects.update(deepcopy(data))
            rules.append({'id': workflow_rule_id, 'label': node['label'],
                          'enabled': node['data']['enabled'], 'when': when, 'effects': effects})
            rule_id = output.get((rule_id, 'next'))
        stages.append({'id': workflow_id, 'label': stage['label'],
                       'enabled': stage['data']['enabled'], 'window': deepcopy(stage['data']['window']),
                       'rules': rules})
        stage_id = output.get((stage_id, 'next'))
    try:
        workflow = _plain({'enabled': graph['enabled'], 'stages': stages})
    except ValueError as error:
        raise ValueError('画布编译：' + str(error)) from error
    return workflow, used


def compile_graph(value, retain_graph=True):
    graph = normalize_graph(value)
    workflow, _ = _compiled(graph)
    if retain_graph:
        workflow['editor_graph'] = graph
    return workflow


def warnings(value):
    graph = normalize_graph(value)
    _, used = _compiled(graph)
    return ['节点 ' + node['id'] + '（' + node['label'] + '）尚未连接到有效安排，仅保留草稿。'
            for node in graph['nodes'] if node['id'] not in used]
