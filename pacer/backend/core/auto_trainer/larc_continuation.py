"""User-configured continuation gates. Pure decisions, no device or purchases."""
from copy import deepcopy
import math
import json
from pathlib import Path
import re

_INT_STR_RE = re.compile(r'^[+-]?\d+$')

VERSION = 'larc_continuation.v1'
NODES = ('junior', 'inheritance', 'pre_expedition', 'inheritance2')
STATS = ('speed', 'stamina', 'power', 'guts', 'wisdom')
DISTANCES = ('short', 'mile', 'medium', 'long')
RANKS = 'GFEDCBAS'
KINDS = {
    'stat_min', 'stat_sum_min', 'adaptation_points_min', 'supporter_points_min',
    'bond_ready_min', 'bond_card_min', 'friend_outing_unlocked',
    'chain_stage_min', 'chain_complete', 'cut_event_completed', 'cut_present',
    'realtime_luck_min',
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
    return {
        'schema_version': VERSION,
        'enabled': False,
        'junior': {
            'mode': 'filter', 'combine': 'all', 'min_pass': 1,
            'conditions': [], 'on_fail': 'pause',
        },
        'pre_expedition': {
            'mode': 'observe', 'combine': 'all', 'min_pass': 1,
            'conditions': [], 'on_fail': 'pause',
        },
        'inheritance': {
            'mode': 'filter', 'skills': [], 'min_skills': 1,
            'distances': {}, 'on_fail': 'pause',
        },
        'inheritance2': {
            'mode': 'observe', 'combine': 'all', 'min_pass': 1,
            'conditions': [], 'skills': [], 'min_skills': 1,
            'distances': {}, 'on_fail': 'pause',
        },
        'custom_dates': [],
        'notify': True,
    }


def _coerce_int_opt(value):
    if type(value) is int:
        return value
    if isinstance(value, str):
        text = value.strip()
        if _INT_STR_RE.match(text):
            try:
                return int(text)
            except ValueError:
                return None
    return None


def _coerce_float_opt(value):
    if isinstance(value, bool):
        return None
    if type(value) is int:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if _INT_STR_RE.match(text):
            try:
                return int(text)
            except ValueError:
                return None
        try:
            parsed = float(text)
            return parsed if math.isfinite(parsed) else None
        except ValueError:
            return None
    return None


def _coerce_section_numbers(section):
    if not isinstance(section, dict):
        return
    for key in ('min_pass', 'min_skills'):
        if key in section:
            coerced = _coerce_int_opt(section[key])
            if coerced is not None:
                section[key] = coerced
    for key in ('year', 'month'):
        if key in section:
            coerced = _coerce_int_opt(section[key])
            if coerced is not None:
                section[key] = coerced
    conditions = section.get('conditions')
    if isinstance(conditions, list):
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            if condition.get('kind') == 'realtime_luck_min':
                coerced = _coerce_float_opt(condition.get('minimum'))
            else:
                coerced = _coerce_int_opt(condition.get('minimum'))
            if coerced is not None:
                condition['minimum'] = coerced
            if 'bond_threshold' in condition:
                coerced_bond = _coerce_int_opt(condition['bond_threshold'])
                if coerced_bond is not None:
                    condition['bond_threshold'] = coerced_bond


def coerce_continuation_numbers(value):
    """Best-effort in-place-safe conversion of legacy numeric strings in continuation config."""
    if not isinstance(value, dict):
        return value
    for name in NODES:
        _coerce_section_numbers(value.get(name))
    custom_dates = value.get('custom_dates')
    if isinstance(custom_dates, list):
        for entry in custom_dates:
            _coerce_section_numbers(entry)
    return value


def _integer(value, label, low=0, high=1000000000):
    coerced = _coerce_int_opt(value)
    if coerced is None or not low <= coerced <= high:
        raise ValueError(f'续养配置：{label}应为{low}～{high}的整数。')
    return coerced


def _validate_conditions_section(section, label, *, require_non_empty, enabled=True):
    if section['combine'] not in {'all', 'at_least'}:
        raise ValueError(f'{label}组合请选择全部达标或至少N项达标。')
    if enabled:
        section['min_pass'] = _integer(section['min_pass'], f'{label}·最低达标项数', 1, 100)
    else:
        coerced_pass = _coerce_int_opt(section.get('min_pass'))
        if coerced_pass is not None:
            section['min_pass'] = coerced_pass
    if not isinstance(section['conditions'], list) or len(section['conditions']) > 100:
        raise ValueError(f'{label}筛选条件应为最多100项的列表。')
    for condition in section['conditions']:
        if not isinstance(condition, dict) or condition.get('kind') not in KINDS:
            raise ValueError(f'{label}筛选条件类型不受支持。')
        allowed = {'kind', 'minimum', 'stat', 'card_id', 'chain_id', 'bond_threshold', 'event_kind'}
        if set(condition) - allowed:
            raise ValueError(f'{label}筛选条件包含未知参数。')
        kind = condition['kind']
        if kind == 'stat_min' and condition.get('stat') not in STATS:
            raise ValueError('请选择正确的五维属性。')
        if kind in {'bond_card_min', 'chain_stage_min', 'chain_complete'}:
            if not isinstance(condition.get('card_id'), str) or not condition['card_id'].isdigit():
                raise ValueError('关键卡条件需要本局支援卡ID。')
        if kind == 'cut_event_completed' and condition.get('event_kind') not in {'big', 'small'}:
            raise ValueError('切事件请选择大切或小切。')
        if not enabled:
            if kind == 'realtime_luck_min':
                coerced_min = _coerce_float_opt(condition.get('minimum'))
                if coerced_min is not None:
                    condition['minimum'] = coerced_min
            elif 'minimum' in condition:
                coerced_min = _coerce_int_opt(condition.get('minimum'))
                if coerced_min is not None:
                    condition['minimum'] = coerced_min
            if kind == 'bond_ready_min' and 'bond_threshold' in condition:
                coerced_bond = _coerce_int_opt(condition['bond_threshold'])
                if coerced_bond is not None:
                    condition['bond_threshold'] = coerced_bond
            continue
        if kind == 'realtime_luck_min':
            val = _coerce_float_opt(condition.get('minimum'))
            if val is None or not -30.0 <= float(val) <= 30.0:
                raise ValueError(f'续养配置：{label}·胡度值最低门槛应为-30～30的数值。')
            condition['minimum'] = val
        elif kind == 'bond_ready_min':
            condition['minimum'] = _integer(condition.get('minimum'), f'{label}·达线支援张数', 1, 6)
            condition['bond_threshold'] = _integer(condition.get('bond_threshold', 80), f'{label}·羁绊线', 0, 100)
        elif kind == 'bond_card_min':
            condition['minimum'] = _integer(condition.get('minimum'), f'{label}·关键卡羁绊线', 0, 100)
        elif kind == 'chain_stage_min':
            condition['minimum'] = _integer(condition.get('minimum'), f'{label}·事件段数', 1, 20)
        elif kind in {'stat_min', 'stat_sum_min', 'adaptation_points_min', 'supporter_points_min'}:
            condition['minimum'] = _integer(condition.get('minimum'), f'{label}·条件最低值')
    count = len(section['conditions'])
    if enabled:
        if require_non_empty and not count:
            raise ValueError(f'{label}筛选至少选择一项条件，或改为仅记录。')
        if count and section['combine'] == 'at_least' and section['min_pass'] > count:
            raise ValueError(f'续养配置：{label}·最低达标项数不能超过已启用条件数。')


def _validate_inheritance_targets(section, label, *, require_non_empty, enabled=True):
    skills = section['skills']
    if not isinstance(skills, list) or len(skills) > 200 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 150 for item in skills):
        raise ValueError('目标技能应为名称或ID列表。')
    section['skills'] = list(dict.fromkeys(item.strip() for item in skills))
    distances = section['distances']
    if not isinstance(distances, dict) or any(
            key not in DISTANCES or not isinstance(rank, str) or len(rank) != 1 or rank not in RANKS
            for key, rank in distances.items()):
        raise ValueError('目标距离适性需要距离类型及G～S等级。')
    if not enabled:
        coerced_skills = _coerce_int_opt(section.get('min_skills'))
        if coerced_skills is not None:
            section['min_skills'] = coerced_skills
        return
    section['min_skills'] = _integer(section['min_skills'], f'{label}·技能最低命中数', 1, 200)
    if skills and section['min_skills'] > len(section['skills']):
        raise ValueError(f'续养配置：{label}·技能最低命中数不能超过去重后的名单项数。')
    if require_non_empty and not skills and not distances:
        raise ValueError(f'{label}至少配置技能或距离路线，或改为仅记录。')


def _normalize_custom_date(entry, seen_ids, enabled):
    if not isinstance(entry, dict):
        raise ValueError('自定义筛选节点应为对象。')
    allowed = {'id', 'label', 'year', 'month', 'half', 'mode', 'combine', 'min_pass', 'conditions', 'on_fail'}
    if set(entry) - allowed:
        raise ValueError('自定义筛选节点包含未知参数。')
    year = _integer(entry.get('year'), '自定义筛选节点·年份', 1, 3)
    month = _integer(entry.get('month'), '自定义筛选节点·月份', 1, 12)
    half = entry.get('half', 'second')
    if half not in {'first', 'second'}:
        raise ValueError('自定义筛选节点上下半月请选择 first 或 second。')
    default_id = f'date_{year}_{month}_{half}'
    identifier = str(entry.get('id') or default_id).strip()
    if not identifier or identifier in NODES or identifier == 'handoff' or identifier in seen_ids:
        raise ValueError('自定义筛选节点标识为空、重复或与固定节点冲突。')
    seen_ids.add(identifier)
    year_label = ('初级', '经典', '高级')[year - 1]
    default_label = f"{year_label}{month}月{'下' if half == 'second' else '上'}"
    label = str(entry.get('label') or default_label).strip() or default_label
    normalized = {
        'id': identifier,
        'label': label[:64],
        'year': year,
        'month': month,
        'half': half,
        'mode': entry.get('mode', 'filter'),
        'combine': entry.get('combine', 'all'),
        'min_pass': entry.get('min_pass', 1),
        'conditions': deepcopy(entry.get('conditions', [])),
        'on_fail': entry.get('on_fail', 'pause'),
    }
    if normalized['mode'] not in {'filter', 'observe'}:
        raise ValueError('节点模式请选择筛选或仅记录。')
    if normalized['on_fail'] not in {'pause', 'restart'}:
        raise ValueError('未通过处理请选择暂停保留或回刷切。')
    _validate_conditions_section(
        normalized, label,
        require_non_empty=(enabled and normalized['mode'] == 'filter'),
        enabled=enabled,
    )
    return normalized


def normalize(value):
    if value is None:
        return defaults()
    if not isinstance(value, dict):
        raise ValueError('续养配置应为对象。')
    if set(value) - {'schema_version', 'enabled', 'notify', 'custom_dates', *NODES}:
        raise ValueError('续养配置包含未知参数。')
    if value.get('schema_version', VERSION) != VERSION:
        raise ValueError('续养配置版本不受支持。')
    result = defaults()
    for key in ('enabled', 'notify'):
        if key in value:
            if type(value[key]) is not bool:
                raise ValueError(f'续养配置：{key}必须是开关。')
            result[key] = value[key]
    for name in NODES:
        section = value.get(name, {})
        if not isinstance(section, dict) or set(section) - set(result[name]):
            raise ValueError(f'续养配置：{name}包含未知参数。')
        result[name].update(deepcopy(section))
        if result[name]['mode'] not in {'filter', 'observe'}:
            raise ValueError('节点模式请选择筛选或仅记录。')
        if result[name]['on_fail'] not in {'pause', 'restart'}:
            raise ValueError('未通过处理请选择暂停保留或回刷切。')
    _validate_conditions_section(
        result['junior'], '首年',
        require_non_empty=(result['enabled'] and result['junior']['mode'] == 'filter'),
        enabled=result['enabled'],
    )
    _validate_conditions_section(
        result['pre_expedition'], '首次远征前',
        require_non_empty=(result['enabled'] and result['pre_expedition']['mode'] == 'filter'),
        enabled=result['enabled'],
    )
    _validate_conditions_section(
        result['inheritance2'], '第二次继承后',
        require_non_empty=False,
        enabled=result['enabled'],
    )
    _validate_inheritance_targets(
        result['inheritance'], '继承后',
        require_non_empty=(result['enabled'] and result['inheritance']['mode'] == 'filter'),
        enabled=result['enabled'],
    )
    _validate_inheritance_targets(
        result['inheritance2'], '第二次继承后',
        require_non_empty=False,
        enabled=result['enabled'],
    )
    inheritance2 = result['inheritance2']
    if (result['enabled'] and inheritance2['mode'] == 'filter'
            and not inheritance2['conditions'] and not inheritance2['skills'] and not inheritance2['distances']):
        raise ValueError('第二次继承后至少配置条件、技能或距离路线，或改为仅记录。')
    custom_raw = value.get('custom_dates', [])
    if not isinstance(custom_raw, list) or len(custom_raw) > 24:
        raise ValueError('自定义筛选节点最多配置 24 个日期节点。')
    seen_ids = set()
    result['custom_dates'] = [_normalize_custom_date(entry, seen_ids, result['enabled']) for entry in custom_raw]
    return result


def enforce_pause_only(value):
    """Force every continuation/screening checkpoint on_fail to pause (free base guard)."""
    config = normalize(value)
    for name in NODES:
        config[name]['on_fail'] = 'pause'
    for entry in config.get('custom_dates', []):
        entry['on_fail'] = 'pause'
    return config


def any_restart_requested(value) -> bool:
    config = normalize(value)
    if any(config[name]['on_fail'] == 'restart' for name in NODES):
        return True
    return any(entry['on_fail'] == 'restart' for entry in config.get('custom_dates', []))


def custom_date_entry(config, node_id):
    return next((entry for entry in config.get('custom_dates', []) if entry.get('id') == node_id), None)


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
    elif kind == 'realtime_luck_min':
        value = facts.get('realtime_luck')
    return {'condition': deepcopy(condition), 'actual': value, 'passed': _minimum(value, target)}


def _evaluate_condition_section(node_name, section, facts):
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
    return {'node': node_name, 'status': status, 'conditions': rows, 'on_fail': section['on_fail']}


def evaluate_junior(config, facts):
    return _evaluate_condition_section('junior', config['junior'], facts)


def evaluate_pre_expedition(config, facts):
    return _evaluate_condition_section('pre_expedition', config['pre_expedition'], facts)


def evaluate_custom_date(section, facts):
    return _evaluate_condition_section(section['id'], section, facts)


def _evaluate_inheritance_routes(section, facts):
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
    return matches, skill_pass, distances, distance_pass, routes


def evaluate_inheritance(config, facts):
    section = config['inheritance']
    matches, skill_pass, distances, distance_pass, routes = _evaluate_inheritance_routes(section, facts)
    status = ('observed' if section['mode'] == 'observe' else 'not_configured' if not routes else
              'passed' if True in routes else 'failed' if all(v is False for v in routes) else 'unknown')
    return {'node': 'inheritance', 'status': status, 'skill_matches': matches,
            'skill_route': skill_pass, 'distances': distances, 'distance_route': distance_pass,
            'on_fail': section['on_fail']}


def evaluate_inheritance2(config, facts):
    section = config['inheritance2']
    matches, skill_pass, distances, distance_pass, routes = _evaluate_inheritance_routes(section, facts)
    cond_eval = _evaluate_condition_section('inheritance2', section, facts)
    if section['mode'] == 'observe':
        status = 'observed'
    else:
        verdicts = []
        if routes:
            apt_verdict = (True if True in routes else
                           False if all(v is False for v in routes) else None)
            verdicts.append(apt_verdict)
        if section['conditions']:
            verdicts.append({'passed': True, 'failed': False}.get(cond_eval['status']))
        if not verdicts:
            status = 'not_configured'
        elif False in verdicts:
            status = 'failed'
        elif None in verdicts:
            status = 'unknown'
        else:
            status = 'passed'
    return {'node': 'inheritance2', 'status': status, 'conditions': cond_eval['conditions'],
            'skill_matches': matches, 'skill_route': skill_pass,
            'distances': distances, 'distance_route': distance_pass,
            'on_fail': section['on_fail']}
