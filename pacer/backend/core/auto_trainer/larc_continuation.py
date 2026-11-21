"""User-configured continuation gates. Pure decisions, no device or purchases."""
from copy import deepcopy
import math
import json
from pathlib import Path

VERSION = 'larc_continuation.v1'
STATS = ('speed', 'stamina', 'power', 'guts', 'wisdom')
DISTANCES = ('short', 'mile', 'medium', 'long')
RANKS = 'GFEDCBAS'
KINDS = {
    'stat_min', 'stat_sum_min', 'adaptation_points_min', 'supporter_points_min',
    'bond_ready_min', 'bond_card_min', 'friend_outing_unlocked',
    'chain_stage_min', 'chain_complete', 'cut_event_completed', 'cut_present',
}


def feature_available(root):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    for candidate in (root, root / 'backend', root / 'pacer/backend'):
        if ((candidate / 'scripts/larc_continuation_collect.py').is_file()
                and (candidate / 'scripts/larc_continuation_inspection.py').is_file()):
            return True
    package = root.parent if root.name.casefold() == 'backend' else root
    path = package / 'product-package-manifest.json'
    if not path.is_file():
        return root.name.casefold() != 'backend'  # Explicit source-tree development.
    try:
        manifest = json.loads(path.read_text(encoding='utf-8-sig'))
        declared = manifest.get('release_channel')
        channel = (declared if declared in ('stable', 'preview') else None) if declared is not None else {
            'private-preview': 'preview', 'private-stable': 'stable',
        }.get(manifest.get('distribution'))
        from desktop.cut_package_profile import continuation_dependencies_present
        return (channel == 'preview' and manifest.get('features', {}).get('continuation_gates') is True
                and continuation_dependencies_present(package, manifest))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def defaults():
    return {'schema_version': VERSION, 'enabled': False,
        'junior': {'mode': 'filter', 'combine': 'all', 'min_pass': 1,
                   'conditions': [], 'on_fail': 'pause'},
        'inheritance': {'mode': 'filter', 'skills': [], 'min_skills': 1,
                        'distances': {}, 'on_fail': 'pause'},
        'notify': True}


def _integer(value, label, low=0, high=1000000000):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'续养配置：{label}应为{low}～{high}的整数。')
    return value


def normalize(value):
    if value is None:
        return defaults()
    if not isinstance(value, dict):
        raise ValueError('续养配置应为对象。')
    if set(value) - {'schema_version', 'enabled', 'junior', 'inheritance', 'notify'}:
        raise ValueError('续养配置包含未知参数。')
    if value.get('schema_version', VERSION) != VERSION:
        raise ValueError('续养配置版本不受支持。')
    result = defaults()
    for key in ('enabled', 'notify'):
        if key in value:
            if type(value[key]) is not bool:
                raise ValueError(f'续养配置：{key}必须是开关。')
            result[key] = value[key]
    for name in ('junior', 'inheritance'):
        section = value.get(name, {})
        if not isinstance(section, dict) or set(section) - set(result[name]):
            raise ValueError(f'续养配置：{name}包含未知参数。')
        result[name].update(deepcopy(section))
        if result[name]['mode'] not in {'filter', 'observe'}:
            raise ValueError('节点模式请选择筛选或仅记录。')
        if result[name]['on_fail'] not in {'pause', 'restart'}:
            raise ValueError('未通过处理请选择暂停保留或回刷切。')
    junior = result['junior']
    if junior['combine'] not in {'all', 'at_least'}:
        raise ValueError('首年组合请选择全部达标或至少N项达标。')
    _integer(junior['min_pass'], '最低达标项数', 1, 100)
    if not isinstance(junior['conditions'], list) or len(junior['conditions']) > 100:
        raise ValueError('首年筛选条件应为最多100项的列表。')
    for condition in junior['conditions']:
        if not isinstance(condition, dict) or condition.get('kind') not in KINDS:
            raise ValueError('首年筛选条件类型不受支持。')
        allowed = {'kind', 'minimum', 'stat', 'card_id', 'chain_id', 'bond_threshold', 'event_kind'}
        if set(condition) - allowed:
            raise ValueError('首年筛选条件包含未知参数。')
        kind = condition['kind']
        if kind in {'stat_min', 'stat_sum_min', 'adaptation_points_min', 'supporter_points_min',
                    'bond_ready_min', 'bond_card_min', 'chain_stage_min'}:
            _integer(condition.get('minimum'), '条件最低值')
        if kind == 'stat_min' and condition.get('stat') not in STATS:
            raise ValueError('请选择正确的五维属性。')
        if kind == 'bond_ready_min':
            _integer(condition['minimum'], '达线支援张数', 1, 6)
            _integer(condition.get('bond_threshold', 80), '羁绊线', 0, 100)
        if kind in {'bond_card_min', 'chain_stage_min', 'chain_complete'}:
            if not isinstance(condition.get('card_id'), str) or not condition['card_id'].isdigit():
                raise ValueError('关键卡条件需要本局支援卡ID。')
        if kind == 'bond_card_min':
            _integer(condition['minimum'], '关键卡羁绊线', 0, 100)
        if kind == 'chain_stage_min':
            _integer(condition['minimum'], '事件段数', 1, 20)
        if kind == 'cut_event_completed' and condition.get('event_kind') not in {'big', 'small'}:
            raise ValueError('切事件请选择大切或小切。')
    count = len(junior['conditions'])
    if result['enabled'] and junior['mode'] == 'filter' and not count:
        raise ValueError('首年筛选至少选择一项条件，或改为仅记录。')
    if count and junior['combine'] == 'at_least' and junior['min_pass'] > count:
        raise ValueError('最低达标项数不能超过已启用条件数。')
    inheritance = result['inheritance']
    skills = inheritance['skills']
    if not isinstance(skills, list) or len(skills) > 200 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 150 for item in skills):
        raise ValueError('目标技能应为名称或ID列表。')
    inheritance['skills'] = list(dict.fromkeys(item.strip() for item in skills))
    _integer(inheritance['min_skills'], '技能最低命中数', 1, 200)
    if skills and inheritance['min_skills'] > len(inheritance['skills']):
        raise ValueError('技能最低命中数不能超过去重后的名单项数。')
    distances = inheritance['distances']
    if not isinstance(distances, dict) or any(
            key not in DISTANCES or not isinstance(rank, str) or len(rank) != 1 or rank not in RANKS
            for key, rank in distances.items()):
        raise ValueError('目标距离适性需要距离类型及G～S等级。')
    if result['enabled'] and inheritance['mode'] == 'filter' and not skills and not distances:
        raise ValueError('继承后至少配置技能或距离路线，或改为仅记录。')
    return result


def _number(value):
    return value if type(value) in {int, float} and math.isfinite(value) else None


def _minimum(value, target):
    value = _number(value)
    return None if value is None else value >= target


def _card_bond(card, target):
    # A bar estimate is a range, never an invented exact integer.
    low = _number(card.get('bond_min'))
    high = _number(card.get('bond_max'))
    exact = _number(card.get('bond_exact'))
    if exact is not None:
        return exact >= target
    if low is not None and low >= target:
        return True
    if high is not None and high < target:
        return False
    return None


def condition_result(condition, facts):
    kind, target = condition['kind'], condition.get('minimum')
    value = None
    if kind == 'stat_min':
        value = facts.get('stats', {}).get(condition['stat'])
    elif kind == 'stat_sum_min':
        values = [facts.get('stats', {}).get(key) for key in STATS]
        value = sum(values) if all(_number(v) is not None for v in values) else None
    elif kind in {'adaptation_points_min', 'supporter_points_min'}:
        value = facts.get(kind.removesuffix('_min'))
    elif kind == 'bond_ready_min':
        cards = [card for card in facts.get('supports', []) if card.get('role') == 'training']
        results = [_card_bond(card, condition.get('bond_threshold', 80)) for card in cards]
        yes, unknown = results.count(True), results.count(None)
        value = {'ready': yes, 'known_not_ready': results.count(False), 'unknown': unknown,
                 'profile_complete': facts.get('support_profile_complete') is True}
        passed = True if yes >= target else (False if value['profile_complete'] and yes + unknown < target else None)
        return {'condition': deepcopy(condition), 'actual': value, 'passed': passed}
    elif kind == 'bond_card_min':
        card = next((c for c in facts.get('supports', []) if c.get('card_id') == condition['card_id']), None)
        return {'condition': deepcopy(condition), 'actual': deepcopy(card),
                'passed': _card_bond(card, target) if card else None}
    elif kind == 'friend_outing_unlocked':
        value = facts.get('friend_outing_unlocked')
        return {'condition': deepcopy(condition), 'actual': value, 'passed': value if type(value) is bool else None}
    elif kind in {'chain_stage_min', 'chain_complete'}:
        rows = [c for c in facts.get('chains', []) if c.get('card_id') == condition['card_id']
                and (not condition.get('chain_id') or c.get('chain_id') == condition['chain_id'])]
        if len(rows) == 1:
            value = rows[0].get('settled_stage') if kind == 'chain_stage_min' else rows[0].get('completed')
        if kind == 'chain_complete':
            return {'condition': deepcopy(condition), 'actual': value, 'passed': value if type(value) is bool else None}
    elif kind == 'cut_event_completed':
        rows = [row for row in facts.get('cut_events', []) if row.get('kind') == condition['event_kind']]
        value = any(row.get('completed') is True for row in rows)
        return {'condition': deepcopy(condition), 'actual': rows,
                'passed': True if value else (False if facts.get('event_history_complete') is True else None)}
    elif kind == 'cut_present':
        value = facts.get('cut_state')
        return {'condition': deepcopy(condition), 'actual': value,
                'passed': {'present': True, 'absent': False}.get(value)}
    return {'condition': deepcopy(condition), 'actual': value, 'passed': _minimum(value, target)}


def evaluate_junior(config, facts):
    section = config['junior']
    rows = [condition_result(condition, facts) for condition in section['conditions']]
    if section['mode'] == 'observe':
        status = 'observed'
    elif not rows:
        status = 'not_configured'
    else:
        required = len(rows) if section['combine'] == 'all' else section['min_pass']
        yes = sum(row['passed'] is True for row in rows)
        unknown = sum(row['passed'] is None for row in rows)
        status = 'passed' if yes >= required else ('failed' if yes + unknown < required else 'unknown')
    return {'node': 'junior', 'status': status, 'conditions': rows, 'on_fail': section['on_fail']}


def evaluate_inheritance(config, facts):
    section = config['inheritance']
    available = {}
    for skill in facts.get('skills', []):
        if skill.get('state') in {'learned', 'available'}:
            identity = str(skill.get('id') or skill.get('name') or '')
            for item in (skill.get('id'), skill.get('name'), *skill.get('aliases', [])):
                if item:
                    available[str(item).strip()] = identity
    matches, counted = [], set()
    for item in section['skills']:
        identity = available.get(item)
        if identity and identity not in counted:
            matches.append(item)
            counted.add(identity)
    skill_pass = (True if len(matches) >= section['min_skills'] else
        False if facts.get('skills_complete') is True else None) if section['skills'] else None
    distances = []
    for key, rank in section['distances'].items():
        actual = facts.get('distances', {}).get(key)
        passed = RANKS.index(actual) >= RANKS.index(rank) if actual in tuple(RANKS) else None
        distances.append({'distance': key, 'required': rank, 'actual': actual, 'passed': passed})
    distance_pass = (True if any(row['passed'] is True for row in distances) else
        False if all(row['passed'] is False for row in distances) else None) if distances else None
    routes = ([skill_pass] if section['skills'] else []) + ([distance_pass] if distances else [])
    status = ('observed' if section['mode'] == 'observe' else 'not_configured' if not routes else
              'passed' if True in routes else 'failed' if all(v is False for v in routes) else 'unknown')
    return {'node': 'inheritance', 'status': status, 'skill_matches': matches,
            'skill_route': skill_pass, 'distances': distances, 'distance_route': distance_pass,
            'on_fail': section['on_fail']}
