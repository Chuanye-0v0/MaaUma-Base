"""Deterministic starting aptitude gaps from known red-factor slots, never odds."""
from copy import deepcopy
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / 'data/parent_library/aptitude_catalog.json'
SCHEMA = 'uma.parent_aptitude_catalog.v1'
START_RANKS = ('G', 'F', 'E', 'D', 'C', 'B', 'A')
RANKS = (*START_RANKS, 'S')
STAR_THRESHOLDS = (0, 1, 4, 7, 10)
_INHERITANCE_SLOTS = ('parent_a', 'gp_a1', 'gp_a2', 'parent_b', 'gp_b1', 'gp_b2')
FACTOR_KEYS = {
    'red:草地': 'turf', 'red:泥地': 'dirt', 'red:短距离': 'short',
    'red:英里': 'mile', 'red:中距离': 'middle', 'red:长距离': 'long',
    'red:领跑': 'nige', 'red:跟前': 'senko', 'red:居中': 'sashi', 'red:后追': 'oikomi',
}


def _factor_key(value):
    if isinstance(value, str) and value.startswith('pink:'):
        value = 'red:' + value[5:]
    if not isinstance(value, str) or value not in FACTOR_KEYS:
        raise ValueError('请选择目录中的红因子适性类型。')
    return value


RULE_REFERENCE = {
    'rule_id': 'starting_red_stars_reference_v1', 'source_status': 'reference',
    'url': 'https://github.com/mikumifa/UmaShow/blob/324e8859fb179c15b9593b94eda02ccba5331b8b/src/renderer/ui/SuccessionPlanner.tsx#L633',
    'local_reference': 'integrations/parent-planner-source-review-20261021/umashow-evidence/src/renderer/ui/SuccessionPlanner.tsx',
    'thresholds': list(STAR_THRESHOLDS), 'maximum_grade_raises': 4,
    'starting_rank_cap': 'A', 'cn_live_calibrated': False,
    'note': '本地缓存工具的开局确定性阈值参考，尚未逐项实机校准；不使用其继承概率模型。',
}


def _rank(value, *, baseline=False):
    if not isinstance(value, str) or value not in RANKS:
        raise ValueError('适性等级应为 G、F、E、D、C、B、A 或 S。')
    if baseline and value == 'S':
        raise ValueError('先天适性请选择 G～A；育成中的 S 不能当作先天适性。')
    return RANKS.index(value)


def minimum_stars_for_rank(baseline_rank, target_rank):
    target = _rank(target_rank)
    if baseline_rank is None:
        return None
    base = _rank(baseline_rank, baseline=True)
    if target <= base:
        return 0
    raises = target - base
    if target > RANKS.index('A') or raises >= len(STAR_THRESHOLDS):
        return None
    return STAR_THRESHOLDS[raises]


def _confirmed_start_rank(baseline_rank, known_stars):
    if baseline_rank is None or known_stars is None:
        return baseline_rank
    raises = max(i for i, threshold in enumerate(STAR_THRESHOLDS) if known_stars >= threshold)
    return RANKS[min(RANKS.index('A'), _rank(baseline_rank, baseline=True) + raises)]


def plan_aptitude(baseline_rank, target_rank, known_stars=0, unknown_nodes=0):
    _rank(target_rank)
    if known_stars is not None and (type(known_stars) is not int or not 0 <= known_stars <= 18):
        raise ValueError('六个继承槽位的已确认红星应为 0～18 或未知。')
    if type(unknown_nodes) is not int or not 0 <= unknown_nodes <= 6:
        raise ValueError('未确认继承槽位数量应为 0～6。')
    required = minimum_stars_for_rank(baseline_rank, target_rank)
    maximum = RANKS[min(RANKS.index('A'), _rank(baseline_rank, baseline=True) + 4)] if baseline_rank is not None else None
    gap = max(0, required - known_stars) if required is not None and known_stars is not None else None
    if baseline_rank is None:
        status = 'unknown_baseline'
    elif required is None:
        status = 'unreachable_at_start'
    elif required == 0 or gap == 0:
        status = 'satisfied'
    elif known_stars is None or unknown_nodes:
        status = 'unconfirmed'
    else:
        status = 'missing'
    return {'status': status, 'baseline_rank': baseline_rank, 'target_rank': target_rank,
        'required_stars': required, 'known_stars': known_stars, 'unknown_nodes': unknown_nodes,
        'gap_from_confirmed': gap, 'additional_stars': 0 if status == 'satisfied' else gap if status == 'missing' else None,
        'max_start_rank': maximum,
        'confirmed_start_rank_lower_bound': _confirmed_start_rank(baseline_rank, known_stars),
        'reference': deepcopy(RULE_REFERENCE), 'inheritance_probability': None,
        'message': ('先天适性未知，请手动填写或明确选择有来源的衣装资料。' if status == 'unknown_baseline' else
            '仅靠开局红因子达不到该等级，不能显示为补齐若干星即可保证。' if status == 'unreachable_at_start' else
            '按六个继承槽位的已确认红星核对；未知槽位不当作零星，后续继承不作概率承诺。')}


class AptitudeCatalog:
    def __init__(self, payload):
        if not isinstance(payload, dict) or payload.get('schema_version') != SCHEMA:
            raise ValueError('先天适性参考目录版本无效。')
        self.source = deepcopy(payload.get('source', {}))
        self.rows = []
        seen = set()
        for row in payload.get('records', []):
            if (not isinstance(row, dict) or type(row.get('character_id')) is not int
                    or not isinstance(row.get('variant_id'), str) or row['variant_id'] in seen):
                raise ValueError('先天适性目录的角色或衣装编号无效。')
            seen.add(row['variant_id'])
            if set(row.get('aptitudes', {})) != set(FACTOR_KEYS.values()):
                raise ValueError('先天适性目录缺少明确的适性类型。')
            for rank in row['aptitudes'].values():
                _rank(rank, baseline=True)
            self.rows.append(deepcopy(row))

    @classmethod
    def load(cls, path=None):
        return cls(json.loads(Path(path or DEFAULT_CATALOG).read_text('utf-8-sig')))

    def baseline(self, character_id, variant_id=None, factor_key='red:泥地', manual_rank=None):
        factor_key = _factor_key(factor_key)
        if character_id is not None:
            if isinstance(character_id, str) and character_id.isascii() and character_id.isdigit():
                character_id = int(character_id)
            if type(character_id) is not int or character_id < 1:
                raise ValueError('先天适性需要明确角色编号，未知时可手填等级。')
        if variant_id is not None:
            if type(variant_id) is int:
                variant_id = str(variant_id)
            if not isinstance(variant_id, str) or not variant_id.isascii() or not variant_id.isdigit():
                raise ValueError('衣装编号无效。')
        result = {'character_id': character_id, 'variant_id': variant_id, 'factor_key': factor_key,
            'rank': None, 'status': 'unknown', 'source_status': 'reference',
            'cn_live_calibrated': False, 'source': deepcopy(self.source), 'candidates': []}
        if manual_rank is not None:
            _rank(manual_rank, baseline=True)
            result.update(rank=manual_rank, status='manual', source_status='manual')
            return result
        rows = [row for row in self.rows if character_id is None or row['character_id'] == character_id]
        if variant_id is not None:
            rows = [row for row in rows if row['variant_id'] == variant_id]
        elif character_id is None:
            rows = []
        name = FACTOR_KEYS[factor_key]
        result['candidates'] = [{'variant_id': row['variant_id'], 'character_id': row['character_id'],
            'rank': row['aptitudes'][name]} for row in rows]
        values = {row['rank'] for row in result['candidates']}
        if len(values) == 1:
            result.update(rank=next(iter(values)), status='reference')
        elif len(values) > 1:
            result['status'] = 'ambiguous_variant'
        return result


def red_node_summary(node, factor_key='red:泥地'):
    """One observed slot; non-red gaps never invalidate a clear red read."""
    from core import parent_library as store
    factor_key = _factor_key(factor_key)
    node = store._node(node)
    stars, unknown, conflict = None, True, None
    if node is not None:
        red_rows = [row for row in node['factors'] if row['kind'] == 'red']
        keys = {row['factor_key'] for row in red_rows if row['factor_key'] is not None}
        rows = [row for row in red_rows if row['factor_key'] == factor_key]
        values = {row['stars'] for row in rows if row['stars'] is not None}
        all_stars = {row['stars'] for row in red_rows if row['stars'] is not None}
        if len(keys) > 1:
            conflict = 'multiple_red_factor_types'
        elif len(all_stars) > 1:
            conflict = 'conflicting_red_stars'
        elif len(values) == 1:
            stars, unknown = next(iter(values)), False
        elif len(keys) == 1 and factor_key not in keys and all(row['factor_key'] is not None for row in red_rows):
            stars, unknown = 0, False
        elif not rows and node['status'] == 'complete' and 'red' in node['scope']:
            stars, unknown = 0, False
    return {'factor_key': factor_key, 'entry_id': node.get('entry_id') if node else None,
        'known_stars': stars, 'unknown': unknown, 'conflict': conflict}


def detected_red_summary(node):
    """Describe the red factor seen in this slot, regardless of the goal."""
    from core import parent_library as store

    node = store._node(node)
    result = {'entry_id': node.get('entry_id') if node else None, 'factor_key': None,
              'label': None, 'stars': None, 'unknown': True, 'conflict': None}
    if node is None:
        return result

    rows = [row for row in node['factors'] if row['kind'] == 'red']
    keys = {row['factor_key'] for row in rows if row['factor_key'] is not None}
    stars = {row['stars'] for row in rows if row['stars'] is not None}
    if len(keys) > 1:
        result['conflict'] = 'multiple_red_factor_types'
    elif len(stars) > 1:
        result['conflict'] = 'conflicting_red_stars'
    elif keys:
        factor_key = next(iter(keys))
        result.update(factor_key=factor_key, label=factor_key.split(':', 1)[1])
        if stars:
            result.update(stars=next(iter(stars)), unknown=False)
    elif not rows and node['status'] == 'complete' and 'red' in node['scope']:
        result.update(stars=0, unknown=False)
    return result


def detected_red_from_nodes(nodes):
    """Keep all six positions and flag inconsistent readings of one entry."""
    if not isinstance(nodes, list) or len(nodes) != len(_INHERITANCE_SLOTS):
        raise ValueError('红星覆盖需要保留六个继承槽位，未观察或待培养节点填空值。')
    slots = [{'slot': name, **detected_red_summary(node)} for name, node in zip(_INHERITANCE_SLOTS, nodes)]
    entry_slots = {}
    for slot in slots:
        if slot['entry_id']:
            entry_slots.setdefault(slot['entry_id'], []).append(slot)
    for observations in entry_slots.values():
        confirmed = {(row['factor_key'], row['stars']) for row in observations if not row['unknown']}
        if len(confirmed) > 1:
            for row in observations:
                row.update(factor_key=None, label=None, stars=None, unknown=True,
                           conflict='conflicting_entry_observations')
    return slots


def red_star_coverage(library, parent_entry_ids, factor_key='red:泥地'):
    """Resolve two current finished branches, then count six actual positions."""
    from core import parent_library as store
    factor_key = _factor_key(factor_key)
    if not isinstance(parent_entry_ids, list) or len(parent_entry_ids) != 2:
        raise ValueError('请保留两个亲代槽位，待培养或未选择的一侧可留空。')
    if parent_entry_ids[0] is not None and parent_entry_ids[0] == parent_entry_ids[1]:
        raise ValueError('两位亲代不能选择同一条成品记录。')
    entries = {row['entry_id']: row for row in store.normalize_library(library)['entries']}
    if any(identifier is not None and identifier not in entries for identifier in parent_entry_ids):
        raise ValueError('所选成品已不在当前种马库，请刷新后重选。')
    nodes = []
    for identifier in parent_entry_ids:
        entry = entries.get(identifier)
        if entry:
            own = deepcopy(entry['self']); own['entry_id'] = identifier
            nodes.extend([own, *entry['parents']])
        else:
            nodes.extend([None, None, None])
    return red_star_coverage_from_nodes(nodes, factor_key)


def red_star_coverage_from_nodes(nodes, factor_key='red:泥地'):
    """Hybrid routes pass six observed snapshots; future/planned nodes stay None."""
    factor_key = _factor_key(factor_key)
    if not isinstance(nodes, list) or len(nodes) != len(_INHERITANCE_SLOTS):
        raise ValueError('红星覆盖需要保留六个继承槽位，未观察或待培养节点填空值。')
    slots = [{'slot': name, **red_node_summary(node, factor_key)} for name, node in zip(_INHERITANCE_SLOTS, nodes)]
    repeated = {}
    for slot in slots:
        if slot['entry_id'] is not None:
            repeated.setdefault(slot['entry_id'], []).append(slot)
    conflicts = []
    for identifier, rows in repeated.items():
        known = {row['known_stars'] for row in rows if row['known_stars'] is not None}
        if len(known) > 1:
            conflicts.append(identifier)
            for row in rows:
                row['known_stars'], row['unknown'] = None, True
                row['conflict'] = 'conflicting_entry_observations'
    known = [slot['known_stars'] for slot in slots if slot['known_stars'] is not None]
    return {'factor_key': factor_key, 'known_stars': sum(known) if known else None,
        'unknown_nodes': sum(slot['unknown'] for slot in slots), 'slot_count': 6, 'slots': slots,
        'repeated_entry_ids': [{'entry_id': identifier, 'slots': [row['slot'] for row in rows],
            'counted_per_slot': identifier not in conflicts} for identifier, rows in repeated.items() if len(rows) > 1],
        'conflicting_entry_ids': conflicts, 'inheritance_basis': 'six_position_slots',
        'message': '六个继承位置分别计星，同节点重复滚屏行只计一次；跨位置相同成品明确列出，不按同名合并。'}


def plan_multi_aptitude(goals, *, target_character_id=None, target_variant_id=None,
                        aptitude_catalog=None, usable_nodes=None):
    """Evaluate independent start ranks and the shared six-position limit."""
    total_slots = len(_INHERITANCE_SLOTS)
    max_stars = 3
    nodes = [None] * total_slots if usable_nodes is None else usable_nodes
    detected = detected_red_from_nodes(nodes)
    items = []
    for goal in goals:
        factor_key = goal['factor_key']
        manual_rank = goal['baseline_rank']
        if aptitude_catalog is None:
            baseline = {'character_id': target_character_id, 'variant_id': target_variant_id,
                        'factor_key': factor_key, 'rank': manual_rank,
                        'status': 'manual' if manual_rank is not None else 'unknown',
                        'source_status': 'manual' if manual_rank is not None else 'reference',
                        'cn_live_calibrated': False, 'source': {}, 'candidates': []}
        else:
            baseline = aptitude_catalog.baseline(target_character_id, variant_id=target_variant_id,
                                                 factor_key=factor_key, manual_rank=manual_rank)
        coverage = red_star_coverage_from_nodes(nodes, factor_key)
        plan = plan_aptitude(baseline.get('rank'), goal['target_rank'],
                             known_stars=coverage['known_stars'], unknown_nodes=coverage['unknown_nodes'])
        required = plan['required_stars']
        gap = plan['gap_from_confirmed']
        items.append({'factor_key': factor_key, 'label': factor_key.split(':', 1)[1],
                      'target_rank': goal['target_rank'], 'baseline': baseline, 'plan': plan,
                      'red_coverage': coverage,
                      'minimum_slots_for_target': (required + max_stars - 1) // max_stars if required else 0,
                      'minimum_additional_slots': (gap + max_stars - 1) // max_stars if gap else 0})

    active_goal_keys = {item['factor_key'] for item in items if item['plan']['required_stars'] != 0}
    occupied = [slot for slot in detected if not slot['unknown'] and slot['conflict'] is None]
    occupied_goal_slots = sum(slot['factor_key'] in active_goal_keys and (slot['stars'] or 0) > 0
                              for slot in occupied)
    open_slots = total_slots - len(occupied)
    target_slots = sum(item['minimum_slots_for_target'] for item in items)
    additional_slots = sum(item['minimum_additional_slots'] for item in items)
    over_capacity = target_slots > total_slots or additional_slots > open_slots
    slot_budget = {'total_slots': total_slots, 'max_stars_per_slot': max_stars,
                   'occupied_goal_slots': occupied_goal_slots,
                   'occupied_non_goal_slots': len(occupied) - occupied_goal_slots,
                   'open_slots': open_slots, 'minimum_slots_for_targets': target_slots,
                   'minimum_additional_slots_needed': additional_slots, 'over_capacity': over_capacity}

    plans = [item['plan'] for item in items]
    statuses = [plan['status'] for plan in plans]
    satisfied_count = statuses.count('satisfied')
    if 'unreachable_at_start' in statuses:
        status = 'unreachable_at_start'
    elif 'unknown_baseline' in statuses:
        status = 'unknown_baseline'
    elif over_capacity:
        status = 'slot_capacity_exceeded'
    elif satisfied_count == len(items):
        status = 'satisfied'
    elif 'unconfirmed' in statuses:
        status = 'unconfirmed'
    else:
        status = 'missing'
    return {'status': status, 'goal_count': len(items), 'satisfied_count': satisfied_count,
            'total_required_stars': sum(plan['required_stars'] or 0 for plan in plans),
            'total_known_goal_stars': sum(plan['known_stars'] or 0 for plan in plans),
            'total_gap_from_confirmed': sum(plan['gap_from_confirmed'] or 0 for plan in plans),
            'items': items, 'slot_budget': slot_budget}


def anti_a_star_budget(baseline_rank):
    """Return the first star total that reaches A and the safe limit below it."""
    threshold = minimum_stars_for_rank(baseline_rank, 'A')
    if threshold is not None:
        max_safe = threshold - 1
    else:
        max_safe = None if baseline_rank is None else 18
    return {'a_threshold_stars': threshold, 'max_safe_stars': max_safe}


_ANTI_A_WARNINGS = {
    'polluted': '已确认红星达到升 A 的门槛。',
    'planned_polluted': '已确认红星加上待培养红星将达到升 A 的门槛。',
    'already_a_at_baseline': '先天已为 A，不能设为禁升 A 保护项。',
    'unknown_baseline': '先天适性未知，请指定衣装或手动填写。',
    'unconfirmed_risk': '未确认槽位可能使该项开局升 A，请核对红因子。',
}


def plan_anti_a_pollution(
    avoid_a_factors, *, target_character_id=None, target_variant_id=None,
    aptitude_catalog=None, usable_nodes=None, planned_red_targets=None,
    branch_reference_totals=None, active_goal_keys=(), manual_baselines=None,
):
    """Check protected factors and summarize the A-rank starting red pool."""
    nodes = [None] * len(_INHERITANCE_SLOTS) if usable_nodes is None else usable_nodes
    branch_totals = branch_reference_totals or {}
    manual_ranks = dict(manual_baselines or {})
    star_evidence = {}

    def evidence(factor_key):
        if factor_key not in star_evidence:
            coverage = red_star_coverage_from_nodes(nodes, factor_key)
            known_stars = 0
            for start in (0, 3):
                recorded = sum(slot['known_stars'] or 0 for slot in coverage['slots'][start:start + 3])
                reported = branch_totals.get(start, {}).get(factor_key)
                known_stars += max(recorded, reported) if reported is not None else recorded
            star_evidence[factor_key] = coverage, known_stars
        return star_evidence[factor_key]

    items = []
    for entry in avoid_a_factors:
        factor_key = _factor_key(entry['factor_key'])
        manual_rank = entry.get('baseline_rank')
        baseline = (aptitude_catalog.baseline(target_character_id, variant_id=target_variant_id,
                    factor_key=factor_key, manual_rank=manual_rank) if aptitude_catalog is not None
                    else {'status': 'manual' if manual_rank is not None else 'unknown', 'rank': manual_rank})
        if manual_rank is not None:
            manual_ranks[factor_key] = manual_rank
        rank = baseline.get('rank')
        budget = anti_a_star_budget(rank)
        threshold = budget['a_threshold_stars']
        max_safe = budget['max_safe_stars']
        coverage, known_stars = evidence(factor_key)
        slots_cov = coverage['slots']
        if planned_red_targets is not None and len(planned_red_targets) == len(slots_cov):
            unknown_nodes = sum(
                1 for slot_cov, target in zip(slots_cov, planned_red_targets)
                if slot_cov['unknown'] and not (target and target.get('factor_key'))
            )
        else:
            unknown_nodes = coverage['unknown_nodes']
        planned_stars = sum(target['stars'] for target in planned_red_targets or ()
                            if target and target.get('factor_key') == factor_key)

        if rank is None:
            status = 'unknown_baseline'
        elif rank == 'A':
            status = 'already_a_at_baseline'
        elif threshold is None:
            status = 'immune_at_start'
        elif known_stars >= threshold:
            status = 'polluted'
        elif known_stars + planned_stars >= threshold:
            status = 'planned_polluted'
        elif unknown_nodes and known_stars + 3 * unknown_nodes >= threshold:
            status = 'unconfirmed_risk'
        else:
            status = 'safe'

        items.append({'factor_key': factor_key, 'label': factor_key.split(':', 1)[1],
                      'baseline': baseline, 'status': status, **budget,
                      'known_stars': known_stars, 'planned_stars': planned_stars,
                      'unknown_nodes': unknown_nodes,
                      'remaining_safe_stars': max(0, max_safe - known_stars)
                      if max_safe is not None and max_safe >= 0 else None,
                      'confirmed_start_rank_lower_bound': _confirmed_start_rank(rank, known_stars),
                      'red_coverage': coverage,
                      'message': _ANTI_A_WARNINGS.get(status, '开局最多提升四阶，先天适性无法升至 A。'
                                                 if status == 'immune_at_start' else '当前红星在安全范围内。')})

    pool_ranks = {}
    for factor_key in FACTOR_KEYS:
        rank = (aptitude_catalog.baseline(target_character_id, variant_id=target_variant_id,
                factor_key=factor_key).get('rank') if aptitude_catalog is not None else None)
        pool_ranks[factor_key] = rank if rank is not None else manual_ranks.get(factor_key)
    complete_profile = all(rank is not None for rank in pool_ranks.values())
    baseline_a_labels, confirmed_a_labels, unintended_a_labels = [], [], []
    if complete_profile:
        goal_keys = set(active_goal_keys)
        for factor_key, rank in pool_ranks.items():
            label = factor_key.split(':', 1)[1]
            if rank == 'A':
                baseline_a_labels.append(label)
                confirmed_a_labels.append(label)
            elif _confirmed_start_rank(rank, evidence(factor_key)[1]) == 'A':
                confirmed_a_labels.append(label)
                if factor_key not in goal_keys:
                    unintended_a_labels.append(label)
    red_pool = {'complete_profile': complete_profile,
                'baseline_a_count': len(baseline_a_labels) if complete_profile else None,
                'confirmed_a_count': len(confirmed_a_labels) if complete_profile else None,
                'baseline_a_labels': baseline_a_labels, 'confirmed_a_labels': confirmed_a_labels,
                'unintended_a_labels': unintended_a_labels}
    statuses = {item['status'] for item in items}
    status = next((warning for warning in _ANTI_A_WARNINGS if warning in statuses),
                  'safe' if items else 'inactive')
    return {'status': status, 'count': len(items), 'items': items, 'red_pool': red_pool}
