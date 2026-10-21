"""Local breeding drafts made from actual horses and explicit future targets.

Plans never become observed library entries. A finished parent fixes its real
ancestors; a different ancestry is a request to breed a new parent instead.
"""
from copy import deepcopy
import re
from core.parent_compatibility import character_id
NODE_KEYS = ('parent_a', 'gp_a1', 'gp_a2', 'parent_b', 'gp_b1', 'gp_b2')
NODE_LABELS = ('亲代 A', 'A 的亲代 1', 'A 的亲代 2', '亲代 B', 'B 的亲代 1', 'B 的亲代 2')
RED_LABELS = ('草地', '泥地', '短距离', '英里', '中距离', '长距离', '领跑', '跟前', '居中', '后追')
RANKS = ('G', 'F', 'E', 'D', 'C', 'B', 'A', 'S')
BORROW_PAIR_REFERENCE = {'scope': 'two_direct_parents_for_one_breeding_operation', 'status': 'local_reference', 'local_source': 'integrations/parent-planner-source-review-20261021/umashow-evidence/src/renderer/ui/SuccessionPlanner.tsx', 'source_lines': [391, 416], 'note': '同一次配种的两位直接亲代不能同时借用；不对整条家系或不同未来分支累计借用次数。'}

def _fields(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(label + '字段无效。')

def _id(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]{1,96}', value):
        raise ValueError('成品记录编号无效。')
    return value

def _variant(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch('[0-9]{1,16}', value):
        raise ValueError('衣装请选择明确编号，未确认时可留空。')
    return value

def factor_key(value):
    if not isinstance(value, str):
        raise ValueError('请选择一种红因子。')
    value = value.replace('pink:', 'red:', 1) if value.startswith('pink:') else value
    if value not in {'red:' + name for name in RED_LABELS}:
        raise ValueError('请选择已收录的场地、距离或跑法红因子。')
    return value

def normalize_local_route(value):
    if value is None:
        return None
    _fields(value, ('aptitude_goal', 'nodes'), '本地路线')
    goal = value.get('aptitude_goal', {})
    _fields(goal, ('factor_key', 'baseline_rank', 'target_rank', 'target_variant_id'), '适性目标')
    baseline, target = (goal.get('baseline_rank'), goal.get('target_rank', 'A'))
    if baseline is not None and baseline not in RANKS[:-1] or target not in RANKS:
        raise ValueError('基础适性应为 G～A，目标可选到 S；未确认的基础适性可留空。')
    values = value.get('nodes', [None] * 6)
    if not isinstance(values, list) or len(values) != 6:
        raise ValueError('本地路线应保留六个继承槽，未选择的槽位可留空。')
    nodes = []
    for row in values:
        if row is None:
            nodes.append(None)
            continue
        if not isinstance(row, dict):
            raise ValueError('系谱槽位格式无效。')
        if row.get('kind') == 'entry':
            _fields(row, ('kind', 'entry_id'), '已有成品槽位')
            nodes.append({'kind': 'entry', 'entry_id': _id(row.get('entry_id'))})
        elif row.get('kind') == 'planned':
            _fields(row, ('kind', 'character_id', 'variant_id', 'red_target'), '待培养槽位')
            red = row.get('red_target')
            if red is not None:
                _fields(red, ('factor_key', 'stars'), '待培养红因子目标')
                if type(red.get('stars')) is not int or not 1 <= red['stars'] <= 3:
                    raise ValueError('单匹待培养成品的红因子目标应为 1～3 星。')
                red = {'factor_key': factor_key(red.get('factor_key')), 'stars': red['stars']}
            nodes.append({'kind': 'planned', 'character_id': character_id(row.get('character_id')), 'variant_id': _variant(row.get('variant_id')), 'red_target': red})
        else:
            raise ValueError('槽位请选择已有成品或待培养角色。')
    return {'aptitude_goal': {'factor_key': factor_key(goal.get('factor_key', 'red:泥地')), 'baseline_rank': baseline, 'target_rank': target, 'target_variant_id': _variant(goal.get('target_variant_id'))}, 'nodes': nodes}

def route_entry_ids(route):
    route = normalize_local_route(route)
    return [] if route is None else list(dict.fromkeys((row['entry_id'] for row in route['nodes'] if row and row['kind'] == 'entry')))

def _own_node(entry):
    node = deepcopy(entry.get('self') or {})
    for key in ('character_id', 'character_name', 'variant_id', 'variant_name', 'nickname'):
        if not node.get(key):
            node[key] = entry.get(key)
    node['entry_id'] = entry['entry_id']
    return node

def _identity(node):
    node = node or {}
    try:
        return character_id(node.get('character_id'))
    except ValueError:
        return None

def _name(node, characters):
    identifier = _identity(node)
    return characters.get(identifier, {}).get('name') or (node or {}).get('character_name') or '角色待确认'

def evaluate_local_route(library, projected_entries, target_character_id, local_route, *, race_catalog, aptitude_catalog, characters=()):
    from core.parent_aptitude import plan_aptitude, red_node_summary, red_star_coverage_from_nodes
    from core.parent_race_records import records_summary, common_wins
    target = character_id(target_character_id)
    route = normalize_local_route(local_route)
    if route is None:
        route = normalize_local_route({})
    goal, specs = (route['aptitude_goal'], route['nodes'])
    by_id = {entry['entry_id']: entry for entry in library['entries']}
    resources = {entry['entry_id']: entry for entry in projected_entries}
    names = {row['character_id']: row for row in characters}
    slots = []
    issues = []

    def slot(index, spec):
        row = {'key': NODE_KEYS[index], 'index': index, 'label': NODE_LABELS[index], 'kind': 'unknown', 'status': 'unknown', 'status_label': '未选择', 'character_id': None, 'variant_id': None, 'entry_id': None, 'locked': False, 'usable': False, 'record_binding': None, 'display_name': '未选择成品或待培养角色', 'notes': [], '_fact': None}
        if spec is None:
            return row
        if spec['kind'] == 'planned':
            fact = {'character_id': spec['character_id'], 'variant_id': spec['variant_id']}
            row.update(kind='planned', status='planned', status_label='待培养', character_id=spec['character_id'], variant_id=spec['variant_id'], display_name=_name(fact, names), red_target=deepcopy(spec['red_target']))
            return row
        identifier = spec['entry_id']
        row.update(kind='entry', entry_id=identifier)
        if identifier not in by_id:
            row.update(status='missing', status_label='原成品已不在当前库', display_name='成品记录缺失')
            return row
        fact = _own_node(by_id[identifier])
        available = resources.get(identifier, {})
        row.update(status=available.get('availability', 'reference'), status_label=available.get('availability_label', '仅作参考'), character_id=_identity(fact), variant_id=fact.get('variant_id'), display_name=_name(fact, names), usable=available.get('available') is True, record_binding={'entry_id': identifier, 'node': 'self'}, _fact=fact)
        return row
    for index, spec in enumerate(specs):
        slots.append(slot(index, spec))
    blocked_training_pairs = set()
    if all((slots[i]['kind'] == 'entry' and slots[i]['status'] == 'borrowed' for i in (0, 3))):
        issues.append('两位最终亲代都为可借成品，同一次配种不能同时借用；请先持有或培养其中一位。')
    for parent_index in (0, 3):
        parent = slots[parent_index]
        if parent['kind'] != 'entry' or parent['entry_id'] not in by_id:
            continue
        entry = by_id[parent['entry_id']]
        for offset, snapshot in enumerate(entry.get('parents', [None, None]), 1):
            index = parent_index + offset
            if specs[index] is not None:
                issues.append(NODE_LABELS[index] + '已由成品亲代固定；调整祖辈前请将该亲代改为待培养。')
            fact = deepcopy(snapshot)
            binding = {'entry_id': parent['entry_id'], 'node': 'parent' + str(offset)}
            note = []
            identity_conflict = False
            reference = (snapshot or {}).get('entry_id')
            if reference in by_id:
                linked = _own_node(by_id[reference])
                observed_id, linked_id = (_identity(snapshot), _identity(linked))
                observed_variant = (snapshot or {}).get('variant_id')
                linked_variant = linked.get('variant_id')
                conflict = observed_id is not None and linked_id is not None and (observed_id != linked_id) or (observed_variant and linked_variant and (observed_variant != linked_variant))
                if conflict:
                    identity_conflict = True
                    note.append('祖辈引用编号与库内身份冲突，保留原快照，未套用另一匹成品。')
                    issues.append(NODE_LABELS[index] + '的成品编号与角色身份冲突，请先核对该快照。')
                else:
                    fact = linked
                    binding = {'entry_id': reference, 'node': 'self'}
                    note.append('已按明确成品编号关联库内记录。')
            row = slots[index]
            row.pop('red_target', None)
            row.update(kind='snapshot', status='identity_conflict' if identity_conflict else 'via_parent' if parent['usable'] else 'reference', status_label='身份冲突待核对' if identity_conflict else '随可用亲代继承' if parent['usable'] else '亲代参考快照', entry_id=(fact or {}).get('entry_id'), character_id=_identity(fact), variant_id=(fact or {}).get('variant_id'), locked=True, usable=parent['usable'] and (not identity_conflict), display_name=_name(fact, names), record_binding=binding, notes=note, _fact=fact)
    for parent_index in (0, 3):
        parent = slots[parent_index]
        for child_index in (parent_index + 1, parent_index + 2):
            child = slots[child_index]
            if parent['character_id'] is not None and parent['character_id'] == child['character_id']:
                issues.append(parent['label'] + '与其亲代是同一角色，需要调整培养材料。')
        left, right = (slots[parent_index + 1], slots[parent_index + 2])
        if parent['kind'] == 'planned' and all((child['kind'] == 'entry' and child['status'] == 'borrowed' for child in (left, right))):
            blocked_training_pairs.add(parent_index)
            issues.append(parent['label'] + '的两位直接材料都为可借成品，同一次培养不能同时借用；请先持有或培养其中一位材料。')
        if left['character_id'] is not None and left['character_id'] == right['character_id']:
            issues.append(parent['label'] + '的两位亲代是同一角色，游戏中不能同时选用。')
    if slots[0]['character_id'] is not None and slots[0]['character_id'] == slots[3]['character_id']:
        issues.append('两位最终亲代是同一角色，游戏中不能同时选用。')
    if target is not None and target in (slots[0]['character_id'], slots[3]['character_id']):
        issues.append('最终亲代与育成目标是同一角色，需要调整。')
    red_coverage = red_star_coverage_from_nodes([row['_fact'] if row['usable'] else None for row in slots], goal['factor_key'])
    known_stars = red_coverage['known_stars']
    unknown_nodes = red_coverage['unknown_nodes']
    reference_stars = 0
    for index, row in enumerate(slots):
        fact = row['_fact']
        red = deepcopy(red_coverage['slots'][index]) if row['usable'] else red_node_summary(fact, goal['factor_key'])
        row['red_summary'] = red
        if not row['usable']:
            reference_stars += red.get('known_stars') or 0
        records = (fact or {}).get('race_records')
        identifier = (fact or {}).get('entry_id')
        row['race_summary'] = records_summary(records, entry_id=identifier, catalog=race_catalog) if identifier is not None else {'entry_id': None, 'status': 'unknown', 'coverage': 'unknown', 'known_win_count': None, 'complete': False, 'wins': []}
    baseline = aptitude_catalog.baseline(target, variant_id=goal['target_variant_id'], factor_key=goal['factor_key'], manual_rank=goal['baseline_rank'])
    rank = baseline.get('rank', baseline.get('baseline_rank'))
    aptitude = plan_aptitude(rank, goal['target_rank'], known_stars=known_stars, unknown_nodes=unknown_nodes)
    race_links = []
    for left_index, right_index in ((0, 1), (0, 2), (3, 4), (3, 5), (0, 3)):
        left, right = (slots[left_index], slots[right_index])
        common = common_wins((left['_fact'] or {}).get('race_records'), (right['_fact'] or {}).get('race_records'), catalog=race_catalog, grades=['G1'])
        race_links.append({'left': left['key'], 'right': right['key'], **common, 'known_common_count': common['confirmed_common_count'], 'common_g1_count': common['confirmed_common_count'] if common['complete'] else None})
    tasks = []
    for index in (1, 2, 4, 5, 0, 3):
        row = slots[index]
        if row['kind'] != 'planned':
            continue
        children = [index + 1, index + 2] if index in (0, 3) else []
        dependencies = [slots[i]['key'] for i in children if slots[i]['kind'] == 'planned']
        identities = [row['character_id']] + [slots[i]['character_id'] for i in children]
        inputs_ready = bool(children) and None not in identities and (len(set(identities)) == 3) and (index not in blocked_training_pairs) and all((slots[i]['kind'] == 'entry' and slots[i]['usable'] for i in children))
        tasks.append({'node_key': row['key'], 'character_id': row['character_id'], 'variant_id': row['variant_id'], 'label': row['label'] + ' · ' + row['display_name'], 'red_target': deepcopy(row.get('red_target')), 'depends_on': dependencies, 'inputs_ready': inputs_ready, 'reason': '两位直接材料都需要借用，请先持有或培养其中一位，再准备这次培养。' if index in blocked_training_pairs else '材料已在当前账号范围内，可准备培养该亲代。' if inputs_ready else '先补齐其材料；目标因子仅为计划，未写入成品库。' if children else '该祖辈待培养；其父母可另存一份培养方案。'})
    ready = target is not None and (not issues) and all((slots[i]['kind'] == 'entry' and slots[i]['usable'] and (slots[i]['character_id'] is not None) for i in (0, 3))) and (aptitude.get('status') in {'satisfied', 'ready'})
    for row in slots:
        row.pop('_fact')
    return {'local_route': route, 'target_character_id': target, 'route_nodes': slots, 'aptitude': {'baseline': baseline, 'plan': aptitude, 'selected_reference_stars': reference_stars, 'red_coverage': red_coverage}, 'race_links': race_links, 'tasks': tasks, 'selection_issues': list(dict.fromkeys(issues)), 'resource_rule_reference': deepcopy(BORROW_PAIR_REFERENCE), 'ready_to_start': ready, 'notes': ['只有实际成品和明确可借材料计入可用红星；待培养目标不会变成已观测因子。', '随成品亲代继承的祖辈不要求另行持有；未知因子与确认零星分开。', '共同胜场仅列实际记录的交集；本轮不把胜场折算成未经国服版本核对的相性加分。', '此处是本地培养计划，不会连接模拟器、开局、借种或替换游戏内设置。']}
