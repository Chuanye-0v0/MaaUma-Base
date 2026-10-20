"""Read-only finished-horse material planning, with no inheritance model.

Confirmed stars describe local evidence in the selected scope. They never
predict the factors of a newly trained horse or a future inheritance result.
"""
from copy import deepcopy

from core import parent_library as store


SCHEMA = 'uma.parent_material_plan.v1'
LABELS = {'complete': '本地素材可用', 'partial': '部分素材可用，待补因子',
          'insufficient_data': '资料不足', 'no_material': '待补因子'}


def normalize_requirements(requirements):
    """Reuse the library's exact key/raw-name identities and star validation."""
    if not isinstance(requirements, list) or not requirements:
        raise ValueError('请至少设置一个目标因子。')
    stripped = []; priorities = []
    for row in requirements:
        store._fields(row, ('factor_key', 'raw_name', 'min_stars', 'priority'), '规划目标')
        priority = row.get('priority', 3)
        if type(priority) is not int or not 1 <= priority <= 5:
            raise ValueError('目标优先级应为 1～5。')
        stripped.append({key: value for key, value in row.items() if key != 'priority'})
        priorities.append(priority)
    rows = store._requirements(stripped)
    return [{**row, 'priority': priority, 'target_id':
             ('key:' + row['factor_key'] if 'factor_key' in row else 'raw:' + row['raw_name'])}
            for row, priority in zip(rows, priorities)]


def _check(target, evidence):
    known = evidence['known_stars']
    if evidence['known_absent']:
        known = 0  # Zero is used only after complete evidence proves absence.
    state = ('available' if evidence['satisfied'] else 'unknown' if known is None
             else 'partial' if known > 0 else 'absent')
    return {**{key: value for key, value in target.items() if key != 'sources'},
            'required_stars': target['min_stars'], 'known_stars': known,
            'gap_stars': None if known is None else max(0, target['min_stars'] - known),
            'gap_is_confirmed': not evidence['uncertain'] or evidence['satisfied'],
            'uncertain': evidence['uncertain'], 'known_absent': evidence['known_absent'],
            'satisfied': evidence['satisfied'], 'state': state}


def _candidate(entry, entries, targets, requirements, scope):
    assessment = store.assess_entry(entry, requirements, scope)
    checks = [_check(target, evidence) for target, evidence in zip(targets, assessment['checks'])]
    covered = sum(row['satisfied'] for row in checks)
    material = sum(row['known_stars'] is not None and row['known_stars'] > 0 for row in checks)
    unknown = sum(not row['satisfied'] and row['uncertain'] for row in checks)
    status = ('complete' if covered == len(targets) else 'partial' if material
              else 'insufficient_data' if unknown else 'no_material')
    priority = sum(row['priority'] for row in checks if row['satisfied'])
    # Capped per target: this is a sorting measure, never a pooled star total.
    stars = sum(min(row['known_stars'], row['required_stars']) for row in checks
                if row['known_stars'] is not None)
    return {'entry_id': entry['entry_id'], **{key: entry[key] for key in store.IDENTITY},
            'scope': scope, 'summary': assessment['summary'], 'checks': checks,
            'status': status, 'label': LABELS[status], 'covered_target_count': covered,
            'material_target_count': material, 'unknown_target_count': unknown,
            'possible_duplicate_ids': store._duplicates(entries, entry),
            'rank_explanation': {'satisfied_priority': priority, 'confirmed_target_stars': stars,
                'unknown_target_count': unknown,
                'text': f'已满足目标优先级合计 {priority}；已确认目标星数 {stars}；待确认目标 {unknown}。'}}


def _availability(target, candidates):
    rows = [(candidate, next(row for row in candidate['checks'] if row['target_id'] == target['target_id']))
            for candidate in candidates]
    best = max((row['known_stars'] for _, row in rows if row['known_stars'] is not None), default=None)
    available = [candidate['entry_id'] for candidate, row in rows if row['satisfied']]
    partial = [candidate['entry_id'] for candidate, row in rows if row['state'] == 'partial']
    unknown = [candidate['entry_id'] for candidate, row in rows if not row['satisfied'] and row['uncertain']]
    return {'target_id': target['target_id'], 'available_entry_ids': available,
            'partial_entry_ids': partial, 'unknown_entry_ids': unknown,
            'best_known_stars': best,
            'gap_stars': None if best is None else max(0, target['min_stars'] - best),
            'gap_is_confirmed': bool(available) or best is not None and not unknown,
            'state': 'available' if available else 'partial' if partial else 'unknown' if unknown or best is None else 'absent',
            'has_unknown_candidates': bool(unknown)}


def plan_materials(library, requirements, scope='pedigree', *, source_resolver=None):
    """Compare every local record; never combine different candidates' stars.

    An optional caller-owned resolver receives one normalized target and
    supplies static source information. This module does not read source
    catalogs, resolve approximate skill names, or access account inventories.
    """
    if scope not in {'self', 'pedigree'}:
        raise ValueError('规划范围请选择本体或谱系。')
    if source_resolver is not None and not callable(source_resolver):
        raise ValueError('静态来源查询接口无效。')
    targets = normalize_requirements(requirements)
    value = store.normalize_library(library)
    for target in targets:
        target['sources'] = deepcopy(source_resolver(deepcopy(target))) if source_resolver else None
    plain = [{key: value for key, value in target.items() if key in {'factor_key', 'raw_name', 'min_stars'}}
             for target in targets]
    candidates = [_candidate(entry, value['entries'], targets, plain, scope) for entry in value['entries']]
    candidates.sort(key=lambda row: (-row['rank_explanation']['satisfied_priority'],
        -row['rank_explanation']['confirmed_target_stars'], row['unknown_target_count'], row['entry_id']))
    for rank, row in enumerate(candidates, 1):
        row['rank'] = rank
    return {'schema_version': SCHEMA, 'library_id': value['library_id'], 'revision': value['revision'],
            'scope': scope, 'identity_basis': 'local_record_id', 'targets': targets,
            'candidates': candidates, 'candidate_count': len(candidates),
            'target_availability': [_availability(target, candidates) for target in targets],
            'ranking': ['satisfied_priority_desc', 'confirmed_target_stars_desc', 'unknown_target_count_asc', 'entry_id_asc'],
            'limitations': ['仅比较本地素材；谱系星数与本体星数分别记录。',
                '排序依据已确认目标资料，不代表绝对最优家系。',
                '待补因子不表示未来继承或成品生成保证；本版不计算相性、祖辈或成功率。',
                '静态技能来源不表示账号已拥有卡片或一定获得技能。'],
            'inheritance_probability': None, 'generation_probability': None,
            'execution_authorized': False}
