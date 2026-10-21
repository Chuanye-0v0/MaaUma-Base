"""Known factor coverage of two actual finished horses, independent of affinity."""
from core import parent_library as store

def _int(value, default, lower, upper, message):
    value = default if value is None else value
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(message)
    return value

def goal_groups(request, catalog):
    chosen = request.get('targets', [])
    raw = request.get('raw_targets', [])
    if not isinstance(chosen, list) or not isinstance(raw, list) or len(chosen) + len(raw) > 200:
        raise ValueError('请检查目标清单，一次最多规划 200 个目标。')
    groups = {}

    def add(requirement, label, input_id, display_name):
        identity = ('key', requirement['factor_key']) if 'factor_key' in requirement else ('raw', requirement['raw_name'])
        if identity not in groups:
            groups[identity] = {'requirement': requirement, 'names': [], 'display_names': [], 'input_ids': []}
        group = groups[identity]
        for field, value in (('names', label), ('display_names', display_name), ('input_ids', input_id)):
            if value not in group[field]:
                group[field].append(value)
        for field in ('min_stars', 'priority'):
            group['requirement'][field] = max(group['requirement'][field], requirement[field])
    for row in chosen:
        store._fields(row, ('target_id', 'min_stars', 'priority'), '规划目标')
        identifier = store._text(row.get('target_id'))
        target = catalog.get(identifier)
        requirement = {'factor_key': target['factor_key'], 'min_stars': _int(row.get('min_stars'), 1, 1, 9, '最低星数应为 1～9。'), 'priority': _int(row.get('priority'), 3, 1, 5, '优先级应为 1～5。')}
        add(requirement, target['name'], identifier, target.get('display_name') or target['name'])
    for row in raw:
        store._fields(row, ('name', 'min_stars', 'priority'), '原文目标')
        name = store._text(row.get('name'))
        requirement = {'raw_name': name, 'min_stars': _int(row.get('min_stars'), 1, 1, 9, '最低星数应为 1～9。'), 'priority': _int(row.get('priority'), 3, 1, 5, '优先级应为 1～5。')}
        add(requirement, name, 'raw:' + name, '原文：' + name)
    if not groups:
        raise ValueError('请先确认至少一个目标技能或因子。')
    return list(groups.values())

def pair_coverage(library, parent_entry_ids, targets, raw_targets, catalog):
    if not isinstance(parent_entry_ids, list) or len(parent_entry_ids) != 2 or parent_entry_ids[0] == parent_entry_ids[1]:
        raise ValueError('请选择两条不同的种马成品记录。')
    groups = goal_groups({'targets': targets, 'raw_targets': raw_targets}, catalog)
    entries = {row['entry_id']: row for row in store.normalize_library(library)['entries']}
    if any((identifier not in entries for identifier in parent_entry_ids)):
        raise ValueError('所选成品记录已变化，请刷新种马库后重选。')
    plain = [{k: v for k, v in g['requirement'].items() if k != 'priority'} for g in groups]
    assessments = [store.assess_entry(entries[identifier], plain, 'pedigree')['checks'] for identifier in parent_entry_ids]
    return _combine_coverage(groups, assessments)

def pair_coverage_resolver(library, targets, raw_targets, catalog):
    """Freeze one comparison's inputs and assess each finished entry once."""
    groups = goal_groups({'targets': targets, 'raw_targets': raw_targets}, catalog)
    entries = {row['entry_id']: row for row in store.normalize_library(library)['entries']}
    plain = [{k: v for k, v in g['requirement'].items() if k != 'priority'} for g in groups]
    cache = {}

    def resolve(left, right):
        if left == right or left not in entries or right not in entries:
            raise ValueError('请选择两条仍在当前库中的不同成品记录。')
        for identifier in (left, right):
            if identifier not in cache:
                cache[identifier] = store.assess_entry(entries[identifier], plain, 'pedigree')['checks']
        return _combine_coverage(groups, [cache[left], cache[right]])
    return resolve

def _combine_coverage(groups, assessments):
    checks = []
    for index, group in enumerate(groups):
        sides = [rows[index] for rows in assessments]
        known = [0 if row['known_absent'] else row['known_stars'] for row in sides]
        total = sum((value for value in known if value is not None)) if any((value is not None for value in known)) else None
        required = group['requirement']['min_stars']
        checks.append({'name': ' / '.join(group['display_names']), 'known_stars': total, 'required_stars': required, 'satisfied': total is not None and total >= required, 'uncertain': any((row['uncertain'] for row in sides)), 'priority': group['requirement']['priority'], 'parent_known_stars': known})
    return {'matched': [r['name'] for r in checks if r['satisfied']], 'missing': [r['name'] for r in checks if not r['satisfied'] and (not r['uncertain'])], 'unknown': [r['name'] for r in checks if not r['satisfied'] and r['uncertain']], 'checks': checks, 'satisfied_priority': sum((r['priority'] for r in checks if r['satisfied'])), 'confirmed_target_stars': sum((min(r['known_stars'], r['required_stars']) for r in checks if r['known_stars'] is not None)), 'message': '汇总所选两匹成品及其祖辈的已记录因子；未读全单独标记，不代表继承概率。'}
