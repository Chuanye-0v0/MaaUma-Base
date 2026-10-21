"""Deterministic starting aptitude gaps from known red-factor slots, never odds."""
from copy import deepcopy
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / 'data/parent_library/aptitude_catalog.json'
SCHEMA = 'uma.parent_aptitude_catalog.v1'
RANKS = ('G', 'F', 'E', 'D', 'C', 'B', 'A', 'S')
STAR_THRESHOLDS = (0, 1, 4, 7, 10)
FACTOR_KEYS = {'red:草地': 'turf', 'red:泥地': 'dirt', 'red:短距离': 'short', 'red:英里': 'mile', 'red:中距离': 'middle', 'red:长距离': 'long', 'red:领跑': 'nige', 'red:跟前': 'senko', 'red:居中': 'sashi', 'red:后追': 'oikomi'}

def _factor_key(value):
    if isinstance(value, str) and value.startswith('pink:'):
        value = 'red:' + value[5:]
    if not isinstance(value, str) or value not in FACTOR_KEYS:
        raise ValueError('请选择目录中的红因子适性类型。')
    return value
RULE_REFERENCE = {'rule_id': 'starting_red_stars_reference_v1', 'source_status': 'reference', 'url': 'https://github.com/mikumifa/UmaShow/blob/324e8859fb179c15b9593b94eda02ccba5331b8b/src/renderer/ui/SuccessionPlanner.tsx#L633', 'local_reference': 'integrations/parent-planner-source-review-20261021/umashow-evidence/src/renderer/ui/SuccessionPlanner.tsx', 'thresholds': list(STAR_THRESHOLDS), 'maximum_grade_raises': 4, 'starting_rank_cap': 'A', 'cn_live_calibrated': False, 'note': '本地缓存工具的开局确定性阈值参考，尚未逐项实机校准；不使用其继承概率模型。'}

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
    predicted = baseline_rank
    if baseline_rank is not None and known_stars is not None:
        raises = max((i for i, threshold in enumerate(STAR_THRESHOLDS) if known_stars >= threshold))
        predicted = RANKS[min(RANKS.index('A'), _rank(baseline_rank, baseline=True) + raises)]
    return {'status': status, 'baseline_rank': baseline_rank, 'target_rank': target_rank, 'required_stars': required, 'known_stars': known_stars, 'unknown_nodes': unknown_nodes, 'gap_from_confirmed': gap, 'additional_stars': 0 if status == 'satisfied' else gap if status == 'missing' else None, 'max_start_rank': maximum, 'confirmed_start_rank_lower_bound': predicted, 'reference': deepcopy(RULE_REFERENCE), 'inheritance_probability': None, 'message': '先天适性未知，请手动填写或明确选择有来源的衣装资料。' if status == 'unknown_baseline' else '仅靠开局红因子达不到该等级，不能显示为补齐若干星即可保证。' if status == 'unreachable_at_start' else '按六个继承槽位的已确认红星核对；未知槽位不当作零星，后续继承不作概率承诺。'}

class AptitudeCatalog:

    def __init__(self, payload):
        if not isinstance(payload, dict) or payload.get('schema_version') != SCHEMA:
            raise ValueError('先天适性参考目录版本无效。')
        self.source = deepcopy(payload.get('source', {}))
        self.rows = []
        seen = set()
        for row in payload.get('records', []):
            if not isinstance(row, dict) or type(row.get('character_id')) is not int or (not isinstance(row.get('variant_id'), str)) or (row['variant_id'] in seen):
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
            if not isinstance(variant_id, str) or not variant_id.isascii() or (not variant_id.isdigit()):
                raise ValueError('衣装编号无效。')
        result = {'character_id': character_id, 'variant_id': variant_id, 'factor_key': factor_key, 'rank': None, 'status': 'unknown', 'source_status': 'reference', 'cn_live_calibrated': False, 'source': deepcopy(self.source), 'candidates': []}
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
        result['candidates'] = [{'variant_id': row['variant_id'], 'character_id': row['character_id'], 'rank': row['aptitudes'][name]} for row in rows]
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
    stars, unknown, conflict = (None, True, None)
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
            stars, unknown = (next(iter(values)), False)
        elif len(keys) == 1 and factor_key not in keys and all((row['factor_key'] is not None for row in red_rows)):
            stars, unknown = (0, False)
        elif not rows and node['status'] == 'complete' and ('red' in node['scope']):
            stars, unknown = (0, False)
    return {'factor_key': factor_key, 'entry_id': node.get('entry_id') if node else None, 'known_stars': stars, 'unknown': unknown, 'conflict': conflict}

def red_star_coverage(library, parent_entry_ids, factor_key='red:泥地'):
    """Resolve two current finished branches, then count six actual positions."""
    from core import parent_library as store
    factor_key = _factor_key(factor_key)
    if not isinstance(parent_entry_ids, list) or len(parent_entry_ids) != 2:
        raise ValueError('请保留两个亲代槽位，待培养或未选择的一侧可留空。')
    if parent_entry_ids[0] is not None and parent_entry_ids[0] == parent_entry_ids[1]:
        raise ValueError('两位亲代不能选择同一条成品记录。')
    entries = {row['entry_id']: row for row in store.normalize_library(library)['entries']}
    if any((identifier is not None and identifier not in entries for identifier in parent_entry_ids)):
        raise ValueError('所选成品已不在当前种马库，请刷新后重选。')
    nodes = []
    for identifier in parent_entry_ids:
        entry = entries.get(identifier)
        if entry:
            own = deepcopy(entry['self'])
            own['entry_id'] = identifier
            nodes.extend([own, *entry['parents']])
        else:
            nodes.extend([None, None, None])
    return red_star_coverage_from_nodes(nodes, factor_key)

def red_star_coverage_from_nodes(nodes, factor_key='red:泥地'):
    """Hybrid routes pass six observed snapshots; future/planned nodes stay None."""
    factor_key = _factor_key(factor_key)
    if not isinstance(nodes, list) or len(nodes) != 6:
        raise ValueError('红星覆盖需要保留六个继承槽位，未观察或待培养节点填空值。')
    names = ('parent_a', 'gp_a1', 'gp_a2', 'parent_b', 'gp_b1', 'gp_b2')
    slots = [{'slot': name, **red_node_summary(node, factor_key)} for name, node in zip(names, nodes)]
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
                row['known_stars'], row['unknown'] = (None, True)
                row['conflict'] = 'conflicting_entry_observations'
    known = [slot['known_stars'] for slot in slots if slot['known_stars'] is not None]
    return {'factor_key': factor_key, 'known_stars': sum(known) if known else None, 'unknown_nodes': sum((slot['unknown'] for slot in slots)), 'slot_count': 6, 'slots': slots, 'repeated_entry_ids': [{'entry_id': identifier, 'slots': [row['slot'] for row in rows], 'counted_per_slot': identifier not in conflicts} for identifier, rows in repeated.items() if len(rows) > 1], 'conflicting_entry_ids': conflicts, 'inheritance_basis': 'six_position_slots', 'message': '六个继承位置分别计星，同节点重复滚屏行只计一次；跨位置相同成品明确列出，不按同名合并。'}
