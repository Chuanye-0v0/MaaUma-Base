"""Offline base compatibility for explicit seven-node pedigrees.

This independently implemented group-intersection calculation reads no device,
network or user store. The bundled relation facts are a local candidate snapshot;
the data source and distribution boundary remain separate from this code.
"""
from copy import deepcopy
from functools import lru_cache
from heapq import nsmallest
import json
from pathlib import Path
import unicodedata
SCHEMA = 'uma.parent_compatibility_relations.v1'
DEFAULT_RELATIONS = Path(__file__).resolve().parents[1] / 'data/parent_library/compatibility_relations.json'
NODE_KEYS = ('target', 'parent_a', 'gp_a1', 'gp_a2', 'parent_b', 'gp_b1', 'gp_b2')
NODE_LABELS = ('目标马娘', '亲代 A', '亲代 A 的亲代 1', '亲代 A 的亲代 2', '亲代 B', '亲代 B 的亲代 1', '亲代 B 的亲代 2')
COMPONENTS = (('target_parent_a', '目标与亲代 A', (0, 1)), ('target_parent_b', '目标与亲代 B', (0, 4)), ('parents', '两亲代之间', (1, 4)), ('ancestor_a1', '目标、亲代 A 与祖辈 1', (0, 1, 2)), ('ancestor_a2', '目标、亲代 A 与祖辈 2', (0, 1, 3)), ('ancestor_b1', '目标、亲代 B 与祖辈 1', (0, 4, 5)), ('ancestor_b2', '目标、亲代 B 与祖辈 2', (0, 4, 6)))
BASE_LIMITATIONS = ['仅计算已选七节点的基础相性，不包含共同重赏或胜鞍加成。', '相性分不代表继承成功率，也不保证未来因子生成。', '关系事实来自固定网页修订，尚未逐项核对国服当前版本。', '缺少身份或未收录角色时只显示已知分项，不把未知当作零分。']

def character_id(value):
    if value is None or value == '':
        return None
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and (int(value) > 0):
        return int(value)
    raise ValueError('角色请选择明确的角色编号，未知节点可留空。')

def _normal(value):
    if not isinstance(value, str):
        return ''
    return ''.join(unicodedata.normalize('NFKC', value).split()).casefold()

class CompatibilityCatalog:

    def __init__(self, payload):
        if not isinstance(payload, dict) or payload.get('schema_version') != SCHEMA:
            raise ValueError('相性关系目录版本无效。')
        version = payload.get('dataset_version')
        if not isinstance(version, str) or not version:
            raise ValueError('相性关系目录缺少资料版本。')
        try:
            ids = payload['character_ids']
            groups = payload['groups']
            if not isinstance(ids, list) or not isinstance(groups, list):
                raise ValueError()
            self.character_ids = frozenset((character_id(value) for value in ids))
            if None in self.character_ids or len(self.character_ids) != len(ids):
                raise ValueError()
            parsed = []
            seen = set()
            for row in groups:
                identifier, points, members = (row['relation_type'], row['points'], row['member_ids'])
                if type(identifier) is not int or identifier in seen or type(points) is not int or (points < 0) or (not isinstance(members, list)):
                    raise ValueError()
                members = frozenset((character_id(value) for value in members))
                if None in members or not members <= self.character_ids:
                    raise ValueError()
                parsed.append((identifier, points, members))
                seen.add(identifier)
        except (KeyError, TypeError, ValueError):
            raise ValueError('相性关系目录内容无效。') from None
        self.groups = tuple(parsed)
        self.data_version = version
        self.source = deepcopy(payload.get('source', {}))
        self.distribution_review = deepcopy(payload.get('distribution_review', {}))

        @lru_cache(maxsize=8192)
        def score(key):
            selected = frozenset(key)
            return sum((points for _, points, members in self.groups if selected <= members))
        self._score = score

    @classmethod
    def load(cls, path=DEFAULT_RELATIONS):
        return cls(json.loads(Path(path).read_text(encoding='utf-8-sig')))

    def score(self, ids):
        values = tuple(sorted(set(ids)))
        if not values or any((value not in self.character_ids for value in values)):
            raise ValueError('相性分项含未收录角色。')
        return self._score(values)

    def metadata(self):
        return {'data_version': self.data_version, 'limitations': list(BASE_LIMITATIONS), 'source': deepcopy(self.source), 'distribution_review': deepcopy(self.distribution_review)}

def characters_from_roster(roster, catalog):
    if not isinstance(roster, dict) or not isinstance(roster.get('records'), list):
        raise ValueError('本地角色目录格式无效。')
    result = {}
    for record in roster['records']:
        identifier = character_id(record.get('character_id'))
        name = record.get('name')
        if identifier is None or not isinstance(name, str) or (not name.strip()):
            raise ValueError('本地角色目录缺少明确编号或名字。')
        row = result.setdefault(identifier, {'character_id': identifier, 'name': name, 'aliases': [], 'outfits': [], 'supported': identifier in catalog.character_ids})
        for alias in [name, *record.get('aliases', [])]:
            if isinstance(alias, str) and alias != row['name'] and (alias not in row['aliases']):
                row['aliases'].append(alias)
        outfit = {'variant_id': str(record.get('id', '')), 'name': record.get('outfit') or ''}
        if isinstance(record.get('release'), str):
            outfit['release'] = record['release']
        if outfit not in row['outfits']:
            row['outfits'].append(outfit)
    return [result[key] for key in sorted(result)]

def _identity(entry):
    own = entry.get('self') or {}
    return {key: entry.get(key) or own.get(key) for key in ('character_id', 'character_name', 'variant_name')}

def _resolve_node(node, characters, catalog):
    node = node or {}
    supplied = node.get('character_id')
    if supplied not in (None, ''):
        try:
            identifier = character_id(supplied)
        except ValueError:
            return (None, 'invalid_character_id')
    else:
        name = _normal(node.get('character_name'))
        matches = {row['character_id'] for row in characters if name and name in {_normal(item) for item in [row['name'], *row['aliases']]}}
        if len(matches) != 1:
            return (None, 'ambiguous_character_name' if matches else 'missing_character_id')
        identifier = next(iter(matches))
    return (identifier, None if identifier in catalog.character_ids else 'character_not_in_dataset')

def entry_summaries(library, characters, catalog):
    summaries = []
    for entry in library['entries']:
        node = _identity(entry)
        identifier, reason = _resolve_node(node, characters, catalog)
        unresolved = []
        if reason:
            unresolved.append({'node': 'self', 'reason': reason, 'character_id': identifier})
        parents = []
        for index, parent in enumerate(entry.get('parents') or [None, None], 1):
            parent_id, parent_reason = _resolve_node(parent, characters, catalog)
            parents.append({'character_id': parent_id, 'character_name': (parent or {}).get('character_name'), 'variant_name': (parent or {}).get('variant_name')})
            if parent_reason:
                unresolved.append({'node': 'parent' + str(index), 'reason': parent_reason, 'character_id': parent_id})
        summaries.append({'entry_id': entry['entry_id'], 'character_id': identifier, 'character_name': node['character_name'], 'variant_name': node['variant_name'], 'parents': parents, 'unresolved': unresolved})
    return summaries

def grade_for_score(score):
    if type(score) is not int or score < 0:
        raise ValueError('基础相性分应为非负整数。')
    return '△' if score < 51 else '〇' if score < 151 else '◎'

def evaluate_lineage(target_character_id, lineage, catalog, characters=()):
    if not isinstance(lineage, list) or len(lineage) != 6:
        raise ValueError('请提供两亲代及四祖辈共六个节点；未知节点可留空。')
    ids = [character_id(value) for value in [target_character_id, *lineage]]
    by_id = {row['character_id']: row['name'] for row in characters}
    nodes = []
    unresolved = []
    for key, label, identifier in zip(NODE_KEYS, NODE_LABELS, ids):
        supported = identifier is not None and identifier in catalog.character_ids
        nodes.append({'key': key, 'label': label, 'character_id': identifier, 'character_name': by_id.get(identifier), 'supported': supported})
        if not supported:
            unresolved.append({'node': key, 'reason': 'missing_character_id' if identifier is None else 'character_not_in_dataset', 'character_id': identifier})
    detail = []
    for key, label, indexes in COMPONENTS:
        selected = [ids[index] for index in indexes]
        same_target_ancestor = len(indexes) == 3 and selected[0] is not None and (selected[2] == selected[0])
        if same_target_ancestor:
            score, reason = (0, 'ancestor_is_target')
        elif all((value is not None and value in catalog.character_ids for value in selected)):
            score, reason = (catalog.score(selected), None)
        else:
            score, reason = (None, 'unresolved_node')
        detail.append({'key': key, 'label': label, 'character_ids': selected, 'score': score, 'status': 'complete' if score is not None else 'partial', 'reason': reason})
    subtotal = sum((row['score'] for row in detail if row['score'] is not None))
    complete = not unresolved
    selection_issues = []
    if ids[1] is not None and ids[1] == ids[4]:
        selection_issues.append('两位亲代是同一角色，游戏中不能同时选用；这里仍显示基础分供比较。')
    if ids[0] is not None and ids[0] in (ids[1], ids[4]):
        selection_issues.append('亲代与育成目标是同一角色，游戏中不能选用；可调整亲代后重新比较。')
    return {'status': 'complete' if complete else 'partial', 'total': subtotal if complete else None, 'known_subtotal': subtotal, 'grade': grade_for_score(subtotal) if complete else None, 'detail': detail, 'nodes': nodes, 'unresolved': unresolved, 'selection_issues': selection_issues, **catalog.metadata()}

def evaluate_entries(target_character_id, parent_entry_ids, entries, catalog, characters=()):
    if not isinstance(parent_entry_ids, list) or len(parent_entry_ids) != 2 or any((not isinstance(value, str) for value in parent_entry_ids)) or (parent_entry_ids[0] == parent_entry_ids[1]):
        raise ValueError('请从种马库选择两条不同的成品记录。')
    by_id = {entry['entry_id']: entry for entry in entries}
    if any((identifier not in by_id for identifier in parent_entry_ids)):
        raise ValueError('选中的种马记录已不存在，请刷新本地种马库。')
    lineage = []
    for identifier in parent_entry_ids:
        entry = by_id[identifier]
        lineage.extend([entry['character_id'], *[row['character_id'] for row in entry['parents']]])
    result = evaluate_lineage(target_character_id, lineage, catalog, characters)
    reason_map = {}
    for side, identifier in zip(('a', 'b'), parent_entry_ids):
        mapping = {'self': 'parent_' + side, 'parent1': 'gp_' + side + '1', 'parent2': 'gp_' + side + '2'}
        for issue in by_id[identifier].get('unresolved', []):
            if issue['node'] in mapping:
                reason_map[mapping[issue['node']]] = issue['reason']
    for issue in result['unresolved']:
        issue['reason'] = reason_map.get(issue['node'], issue['reason'])
    result['parent_entry_ids'] = list(parent_entry_ids)
    return result

def suggest_entries(target_character_id, entries, catalog, characters=(), *, candidate_entry_ids=None, limit=20):
    target = character_id(target_character_id)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('组合建议的显示数量应为 1～20。')
    by_id = {entry['entry_id']: entry for entry in entries}
    if candidate_entry_ids is None:
        candidates = sorted(by_id)
    elif isinstance(candidate_entry_ids, list) and all((isinstance(value, str) for value in candidate_entry_ids)):
        candidates = sorted(set(candidate_entry_ids))
        if any((identifier not in by_id for identifier in candidates)):
            raise ValueError('候选记录已不存在，请刷新本地种马库。')
    else:
        raise ValueError('候选范围应为本地成品记录编号列表。')
    excluded_pair_count = 0
    evaluated_pair_count = 0

    def values():
        nonlocal excluded_pair_count, evaluated_pair_count
        for i, left in enumerate(candidates):
            for right in candidates[i + 1:]:
                a, b = (by_id[left]['character_id'], by_id[right]['character_id'])
                if a is not None and a == b or (target is not None and (a == target or b == target)):
                    excluded_pair_count += 1
                    continue
                evaluated_pair_count += 1
                yield evaluate_entries(target, [left, right], [by_id[left], by_id[right]], catalog, characters)

    def ranking(result):
        return (result['status'] != 'complete', -(result['total'] if result['total'] is not None else result['known_subtotal']), len(result['unresolved']), *result['parent_entry_ids'])
    suggestions = nsmallest(limit, values(), key=ranking)
    return {'suggestions': suggestions, 'candidate_entry_count': len(candidates), 'pair_count': evaluated_pair_count, 'excluded_pair_count': excluded_pair_count, 'ranking_scope': 'selected_local_finished_entries', 'ranking_description': '仅对当前候选成品及其已记录家系排序，不是全局最优配种。', **catalog.metadata()}

def suggest_entries_joint(target_character_id, entries, catalog, characters=(), *, candidate_entry_ids=None, limit=20, coverage_resolver=None):
    """Rank every available real pair by identity, factor goals, then affinity.

    ``coverage_resolver(left_entry_id, right_entry_id)`` supplies the existing
    pair-coverage projection. It is called before the result limit is applied,
    so a useful factor pair cannot disappear in an affinity-only shortlist.
    Without a resolver the old interface's result is preserved exactly.
    """
    if coverage_resolver is None:
        return suggest_entries(target_character_id, entries, catalog, characters, candidate_entry_ids=candidate_entry_ids, limit=limit)
    if not callable(coverage_resolver):
        raise ValueError('目标覆盖计算器应为可调用函数。')
    target = character_id(target_character_id)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('组合建议的显示数量应为 1～20。')
    by_id = {entry['entry_id']: entry for entry in entries}
    if candidate_entry_ids is None:
        candidates = sorted(by_id)
    elif isinstance(candidate_entry_ids, list) and all((isinstance(value, str) for value in candidate_entry_ids)):
        candidates = sorted(set(candidate_entry_ids))
        if any((identifier not in by_id for identifier in candidates)):
            raise ValueError('候选记录已不存在，请刷新本地种马库。')
    else:
        raise ValueError('候选范围应为本地成品记录编号列表。')
    excluded_pair_count = 0
    evaluated_pair_count = 0
    has_targets = False

    def values():
        nonlocal excluded_pair_count, evaluated_pair_count, has_targets
        for index, left in enumerate(candidates):
            for right in candidates[index + 1:]:
                a, b = (by_id[left]['character_id'], by_id[right]['character_id'])
                if a is not None and a == b or (target is not None and (a == target or b == target)):
                    excluded_pair_count += 1
                    continue
                result = evaluate_entries(target, [left, right], [by_id[left], by_id[right]], catalog, characters)
                coverage = coverage_resolver(left, right)
                if not isinstance(coverage, dict) or not isinstance(coverage.get('checks', []), list):
                    raise ValueError('目标覆盖计算结果无效。')
                has_targets = has_targets or bool(coverage.get('checks'))
                result['factor_coverage'] = deepcopy(coverage)
                evaluated_pair_count += 1
                yield result

    def ranking(result):
        coverage = result['factor_coverage']
        if coverage.get('checks'):
            priority = coverage['satisfied_priority']
            stars = coverage['confirmed_target_stars']
            unknown = sum((not row['satisfied'] and row['uncertain'] for row in coverage['checks']))
        else:
            priority = stars = unknown = 0
        affinity = result['total'] if result['total'] is not None else result['known_subtotal']
        return (result['status'] != 'complete', -priority, -stars, unknown, -affinity, len(result['unresolved']), *result['parent_entry_ids'])
    suggestions = nsmallest(limit, values(), key=ranking)
    description = '在当前候选成品的全部可用组合中，先按家系身份完整性，再按已满足目标优先级、确认目标星数、未满足且未知目标数、基础相性和缺失节点数排序；未知因子不会当作零星或已达标，不代表全局最优或继承概率。' if has_targets else '仅对当前候选成品及其已记录家系排序，不是全局最优配种。'
    return {'suggestions': suggestions, 'candidate_entry_count': len(candidates), 'pair_count': evaluated_pair_count, 'excluded_pair_count': excluded_pair_count, 'ranking_scope': 'selected_local_finished_entries', 'ranking_description': description, **catalog.metadata()}
