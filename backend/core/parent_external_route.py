"""Compare a pedigree slot or search the account's available breeding routes."""

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from heapq import nsmallest
from itertools import combinations, product
import json

from core import parent_library as store
from core.parent_aptitude import FACTOR_KEYS, RANKS, minimum_stars_for_rank, red_star_coverage_from_nodes
from core.parent_compatibility import COMPONENTS, affinity_ordering_score, evaluate_lineage, resolve_node_identity
from core.parent_local_route import NODE_LABELS, normalize_local_route
from core.parent_mali_import import mali_pool_item, mali_search_records
from core.parent_own_career import _general_candidate_outfits, _general_filler, plan_general_lineage, start_capped_route
from core.parent_race_records import RACE_AFFINITY_G1_POINTS, RACE_AFFINITY_PAIRS, RaceCatalog, evaluate_race_affinity


MALI_URL = 'https://game.bilibili.com/tool/pd/'
DIRECT = (0, 3)


def prepare_external_route(local_route, slot_index):
    if type(slot_index) is not int or not 0 <= slot_index < 6:
        raise ValueError('请选择一个可编辑的继承槽位。')
    route = normalize_local_route(local_route) or normalize_local_route({})
    nodes = route['nodes']
    if slot_index in DIRECT:
        if any(nodes[slot_index + offset] is not None for offset in (1, 2)):
            raise ValueError('这一侧已选择祖辈，请先清除祖辈草稿，再换成吗哩吗哩的成品。')
    elif (nodes[slot_index // 3 * 3] or {}).get('kind') in ('entry', 'external'):
        raise ValueError('成品亲代的祖辈已固定，请选择另一槽位。')
    nodes[slot_index] = None
    return route


def _node_indexes(slot_index):
    return range(slot_index, slot_index + 3) if slot_index in DIRECT else (slot_index,)


def _known_stars(review, indexes, factor_key):
    known = []
    for index in indexes:
        node = review['route_nodes'][index]
        summary = node['red_summary']
        if summary['factor_key'] == factor_key:
            known.append(summary['known_stars'])
        else:
            detected = node['detected_red']
            if not detected['unknown']:
                known.append(detected['stars'] if detected['factor_key'] == factor_key else 0)
    return _sum_known(known)


def _sum_known(values):
    values = [value for value in values if value is not None]
    return sum(values) if values else None


def _factor_material(spec, entries, requirements, scope):
    if spec is None or spec['kind'] == 'planned':
        return [None] * len(requirements)
    entry = spec['entry'] if spec['kind'] == 'external' else entries.get(spec['entry_id'])
    if entry is None:
        return [None] * len(requirements)
    observed = store.assess_entry(entry, requirements, scope=scope)['checks']
    totals = ({row['factor_key']: row['family_stars'] for row in spec['reference_totals']}
              if scope == 'pedigree' and spec['kind'] == 'external' else {})
    known = []
    for requirement, check in zip(requirements, observed):
        recorded = 0 if check['known_absent'] else check['known_stars']
        reported = totals.get(requirement.get('factor_key'))
        known.append(max(recorded or 0, reported) if reported is not None else recorded)
    return known


def _retained_factors(route, slot_index, entries, groups):
    requirements = [{key: value for key, value in group['requirement'].items() if key != 'priority'}
                    for group in groups]
    materials = []
    for index in DIRECT:
        if index == slot_index:
            continue
        spec = route['nodes'][index]
        if (spec or {}).get('kind') in ('entry', 'external'):
            materials.append(_factor_material(spec, entries, requirements, 'pedigree'))
        else:
            for child in (index + 1, index + 2):
                if child != slot_index:
                    materials.append(_factor_material(route['nodes'][child], entries, requirements, 'self'))
    retained = [_sum_known(values) for values in zip(*materials)] if materials else [None] * len(groups)
    return requirements, retained


def _search_term(label, stars, slot_index):
    if slot_index in DIRECT:
        return f'{label}{min(9, stars)}星'
    return f'{label}{min(3, stars)}星本体{min(3, stars)}星'


def _aptitude_items(route, review):
    aptitude = review['aptitude']
    items = aptitude.get('items')
    if items is None:
        factor_key = route['aptitude_goal']['factor_key']
        return [{'factor_key': factor_key, 'label': factor_key.split(':', 1)[1],
                 'plan': aptitude['plan']}]
    return items


def _anti_a_items(review):
    return review['aptitude'].get('anti_a', {}).get('items', [])


def external_query(route, slot_index, local_review, library, groups):
    red_gaps = []
    terms = {}
    for item in _aptitude_items(route, local_review):
        factor_key, label = item['factor_key'], item['label']
        required = item['plan']['required_stars']
        known = _known_stars(local_review, range(6), factor_key)
        remaining = max(0, required - (known or 0)) if required is not None else None
        red_gaps.append({'factor_key': factor_key, 'label': label, 'required_stars': required,
                         'retained_recorded_stars': known, 'remaining_stars': remaining})
        if remaining is None or remaining > 0:
            terms[label] = remaining
    note = '只按已记录素材计算缺口。亲代使用家系星数，填祖辈只使用本体星数。'
    if slot_index not in DIRECT and len(terms) > 1:
        note += '单匹祖辈只能携带一种红因子，多维缺星需分槽补齐。'
    entries = {entry['entry_id']: entry for entry in library['entries']}
    _, retained = _retained_factors(route, slot_index, entries, groups)
    for group, known in zip(groups, retained):
        name = group['names'][0]
        gap = group['requirement']['min_stars'] - (known or 0)
        if gap > 0:
            terms[name] = max(terms.get(name) or 0, gap)
    avoid_a_limits = []
    other_indexes = [index for index in range(6) if index not in _node_indexes(slot_index)]
    for item in _anti_a_items(local_review):
        factor_key = item['factor_key']
        known = _known_stars(local_review, other_indexes, factor_key) or 0
        maximum = item['max_safe_stars']
        avoid_a_limits.append({
            'factor_key': factor_key, 'label': item['label'],
            'baseline_rank': (item.get('baseline') or {}).get('rank'),
            'a_threshold_stars': item['a_threshold_stars'], 'max_safe_stars': maximum,
            'retained_recorded_stars': known,
            'remaining_safe_stars': max(0, maximum - known) if maximum is not None else None,
        })
    if avoid_a_limits:
        limits = '、'.join(
            f"{item['label']}(安全上限{item['max_safe_stars'] if item['max_safe_stars'] is not None else '?'}★"
            f"/剩余{item['remaining_safe_stars'] if item['remaining_safe_stars'] is not None else '?'}★)"
            for item in avoid_a_limits)
        note += '防升 A 禁区：' + limits + '。'
    primary = red_gaps[0] if red_gaps else {
        'factor_key': route['aptitude_goal']['factor_key'], 'required_stars': 0,
        'retained_recorded_stars': 0, 'remaining_stars': 0,
    }
    return {'slot_index': slot_index, 'slot_label': NODE_LABELS[slot_index],
            **{key: value for key, value in primary.items() if key != 'label'},
            'red_gaps': red_gaps, 'avoid_a_limits': avoid_a_limits, 'url': MALI_URL,
            'search_text': '、'.join(_search_term(name, stars, slot_index) if stars else name
                                    for name, stars in terms.items()),
            'note': note}


def _conditional_stars(local, selected_indexes, other_indexes, factor_key, reported, scope):
    branch = _known_stars(local, selected_indexes, factor_key)
    other = _known_stars(local, other_indexes, factor_key)
    # Provider totals include the entire branch. For an ancestor only its
    # individual factors count, because its pedigree lies outside these slots.
    if scope == 'pedigree' and reported is not None:
        branch = max(branch or 0, reported)
    return _sum_known((other, branch))


def _external_spec(item):
    return {'kind': 'external', 'entry': item['entry'], 'borrow_confirmed': False,
            'reference_totals': item['reference_totals']}


def external_compare(route, slot_index, candidates, limit, evaluate, *, target, compatibility,
                     characters, library, groups, pool_items=(), use_pool=False):
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError('最多显示 1～10 个吗哩吗哩候选。')
    records = [] if use_pool and (candidates is None or
        isinstance(candidates, str) and not candidates.strip()) else mali_search_records(candidates)
    if len(records) > 50 or (not records and not use_pool):
        raise ValueError('请一次导入 1～50 条吗哩吗哩候选。')
    if use_pool:
        records = [*records, *pool_items]
    entries = {entry['entry_id']: entry for entry in library['entries']}
    requirements, retained = _retained_factors(route, slot_index, entries, groups)
    scope = 'pedigree' if slot_index in DIRECT else 'self'
    selected_indexes = tuple(_node_indexes(slot_index))
    other_indexes = tuple(index for index in range(6) if index not in selected_indexes)
    suggestions, seen, skipped = [], set(), 0
    for candidate in records:
        try:
            item = mali_pool_item(candidate)
        except ValueError:
            skipped += 1
            continue
        entry_id = item['entry_id']
        if entry_id in seen:
            continue
        seen.add(entry_id)
        proposal = deepcopy(route)
        spec = _external_spec(item)
        proposal['nodes'][slot_index] = spec
        local = evaluate(proposal)
        conditional = evaluate(proposal, assume_external_slot=slot_index)
        affinity = evaluate_lineage(target, [node['character_id'] for node in local['route_nodes']],
                                    compatibility, characters)
        family_totals = {row['factor_key']: row['family_stars'] for row in spec['reference_totals']}
        aptitude_checks = []
        for item in _aptitude_items(route, local):
            factor_key = item['factor_key']
            required = item['plan']['required_stars']
            reported = family_totals.get(factor_key)
            total = _conditional_stars(local, selected_indexes, other_indexes, factor_key, reported, scope)
            aptitude_checks.append({'factor_key': factor_key, 'label': item['label'],
                                    'required_stars': required, 'conditional_stars': total,
                                    'reported_family_stars': reported,
                                    'remaining_gap': max(0, required - (total or 0)) if required is not None else 0})
        primary = aptitude_checks[0] if aptitude_checks else {}
        anti_a_checks = []
        for item in _anti_a_items(local):
            factor_key = item['factor_key']
            baseline = (item.get('baseline') or {}).get('rank')
            threshold, maximum = item['a_threshold_stars'], item['max_safe_stars']
            reported = family_totals.get(factor_key)
            total = _conditional_stars(local, selected_indexes, other_indexes, factor_key, reported, scope)
            polluted = (item['status'] == 'polluted' or baseline == 'A'
                        or (threshold is not None and total is not None and total >= threshold))
            anti_a_checks.append({
                'factor_key': factor_key, 'label': item['label'], 'baseline_rank': baseline,
                'a_threshold_stars': threshold, 'max_safe_stars': maximum,
                'conditional_stars': total, 'reported_family_stars': reported,
                'remaining_safe_stars': max(0, maximum - (total or 0))
                if maximum is not None and maximum >= 0 else None,
                'polluted': polluted,
            })
        known = _factor_material(spec, entries, requirements, scope)
        factor_checks = []
        for group, earlier, imported in zip(groups, retained, known):
            stars = _sum_known((earlier, imported))
            needed = group['requirement']['min_stars']
            factor_checks.append({'name': ' / '.join(group['display_names']), 'required_stars': needed,
                                  'reference_stars': stars, 'met_on_reference': stars is not None and stars >= needed,
                                  'priority': group['requirement']['priority']})
        external_node = local['route_nodes'][slot_index]
        trainer_id = external_node['trainer_id']
        name = external_node['display_name']
        label = f'{name} · 训练员 {trainer_id}' if trainer_id else name
        suggestions.append({'label': label, 'trainer_id': trainer_id,
            'entry_id': entry_id, 'local_route': local.pop('local_route'),
            'local_review': local, 'conditional_review': conditional,
            'reported_family_stars': primary.get('reported_family_stars'),
            'conditional_total_stars': primary.get('conditional_stars'),
            'aptitude_checks': aptitude_checks, 'anti_a_checks': anti_a_checks,
            'compatibility': affinity, 'factor_checks': factor_checks})
        conditional.pop('local_route')
    if not suggestions:
        raise ValueError('没有识别到可比较的吗哩吗哩种马，请检查复制的候选内容。')

    def ranking(row):
        review = row['conditional_review']
        gap = sum(check['remaining_gap'] for check in row['aptitude_checks'])
        total = sum(check['conditional_stars'] or 0 for check in row['aptitude_checks'])
        priority = sum(check['priority'] for check in row['factor_checks'] if check['met_on_reference'])
        has_issue = bool(review['selection_issues']) or any(check['polluted'] for check in row['anti_a_checks'])
        return (has_issue, gap, -priority, -total,
                -row['compatibility']['known_subtotal'], row['entry_id'])

    return {'slot_index': slot_index, 'slot_label': NODE_LABELS[slot_index],
            'suggestions': sorted(suggestions, key=ranking)[:limit], 'skipped_candidates': skipped,
            'note': '比较分和家系星数仅作参考。导入候选的借用状态仍需确认。'}


_APTITUDE_BLOCKERS = {'unreachable_at_start', 'slot_capacity_exceeded', 'unknown_baseline'}
_POLLUTION = {'polluted', 'planned_polluted', 'already_a_at_baseline'}
_TIER_LABELS = ('0代直出', '1代现养', '2代精养染色')


def _planned_red(nodes):
    stars = Counter()
    for node in nodes:
        if node and node['kind'] == 'planned' and (red := node['red_target']):
            stars[red['factor_key']] += red['stars']
    return stars


def _reference_stars(review, item):
    slots = item['red_coverage']['slots']
    key = item['factor_key']
    total = 0
    for index in DIRECT:
        branch = sum(slot['known_stars'] or 0 for slot in slots[index:index + 3])
        parent = review['route_nodes'][index]
        if parent['usable'] and parent['kind'] == 'external':
            reports = {row['factor_key']: row['family_stars'] for row in parent['reference_totals']}
            branch = max(branch, reports.get(key, 0))
        total += branch
    return total


def _aptitude_summary(proposal, review):
    aptitude = review['aptitude']
    planned = _planned_red(proposal['nodes'])
    required_stars = known_stars = planned_stars = gap = s_goal_stars = 0
    has_s_target = blocker = False
    for item in aptitude['items']:
        key = item['factor_key']
        known = _reference_stars(review, item)
        known_stars += known
        if item.get('target_rank') == 'S':
            has_s_target = True
            s_goal_stars += known + planned[key]
            required = minimum_stars_for_rank(item['baseline']['rank'], 'A')
            if required is None:
                blocker = True
        else:
            if item['plan']['status'] in _APTITUDE_BLOCKERS:
                blocker = True
            required = item['plan']['required_stars']
        if required is not None:
            required_stars += required
            missing_now = max(0, required - known)
            covered_by_plan = min(missing_now, planned[key])
            planned_stars += covered_by_plan
            gap += missing_now - covered_by_plan
    status = aptitude['status']
    if not has_s_target and status in _APTITUDE_BLOCKERS:
        blocker = True
    satisfied_with_plan = not blocker and gap == 0
    satisfied_now = satisfied_with_plan and planned_stars == 0
    if satisfied_with_plan:
        status = 'satisfied' if satisfied_now else 'planned_satisfied'
    return {'status': status, 'satisfied_now': satisfied_now,
            'satisfied_with_plan': satisfied_with_plan, 'known_goal_stars': known_stars,
            'planned_red_stars': planned_stars, 'required_goal_stars': required_stars,
            'remaining_gap': gap, 'has_s_target': has_s_target, 's_goal_stars': s_goal_stars}


def _factor_summary(proposal, review, entries, groups, material_factors):
    if not groups:
        return {'total_targets': 0, 'satisfied_count': 0, 'satisfied_priority': 0,
                'confirmed_stars': 0, 'checks': []}
    materials = []
    for index, (spec, row) in enumerate(zip(proposal['nodes'], review['route_nodes'])):
        if spec and row['usable'] and row['kind'] in ('entry', 'external'):
            scope = 'pedigree' if index in DIRECT else 'self'
            materials.append(material_factors(spec, entries, scope))
    checks = []
    for index, group in enumerate(groups):
        stars = _sum_known(row[index] for row in materials)
        requirement = group['requirement']
        needed = requirement['min_stars']
        checks.append({'name': ' / '.join(group['display_names']), 'required_stars': needed,
                       'known_stars': stars, 'priority': requirement['priority'],
                       'satisfied': stars is not None and stars >= needed})
    satisfied = [check for check in checks if check['satisfied']]
    return {'total_targets': len(checks), 'satisfied_count': len(satisfied),
            'satisfied_priority': sum(check['priority'] for check in satisfied),
            'confirmed_stars': sum(min(check['known_stars'] or 0, check['required_stars'])
                                   for check in checks), 'checks': checks}


def _assume_ready_for_pair(review, aptitude):
    if not aptitude['satisfied_now'] or review['selection_issues']:
        return
    slots = review['route_nodes']
    if not all(slots[i]['usable'] and slots[i]['character_id'] is not None for i in DIRECT):
        return
    anti_a = review['aptitude']['anti_a']
    reference_parents = [i for i in DIRECT
                         if slots[i]['kind'] == 'external' and slots[i]['reference_totals']]
    if not reference_parents or anti_a['status'] not in {'inactive', 'safe', 'unconfirmed_risk'}:
        return
    reference_children = {i + offset for i in reference_parents for offset in (1, 2)
                          if slots[i + offset]['character_id'] is None}
    for item in anti_a['items']:
        if item['status'] == 'unconfirmed_risk' and any(
            slot['unknown'] and i not in reference_children
            for i, slot in enumerate(item['red_coverage']['slots'])
        ):
            return
    if review['ready_to_start']:
        return
    review['ready_to_start'] = True
    review['ready_basis'] = 'mali_branch_reference_assumption'
    review['notes'].append(
        '条件预估采用吗哩吗哩家系总星数，将外部未知祖辈未报告的受保护红因子暂按 0 星模拟。'
        '借用资格、祖辈和红因子均须确认后再开局。')


def _slot_race_records(slot):
    records = slot.get('race_records')
    if records is not None:
        return records
    summary = slot.get('race_summary') or {}
    if not summary.get('entry_id') or not summary.get('source'):
        return None
    return {'entry_id': summary['entry_id'], 'coverage': summary['coverage'],
            'source': summary['source'], 'wins': [
                {key: win[key] for key in ('race_id', 'raw_name', 'status', 'source')}
                for win in summary['wins']
            ]}


def _format_pair(tier, proposal, evaluate, target, compatibility, characters, entries,
                 groups, material_factors, *, label=None, steps=None, race_catalog=None,
                 reference_ids=()):
    local = evaluate(proposal)
    proposal = local.pop('local_route')
    assumed = {
        i for i, node in enumerate(proposal['nodes'])
        if node and (node['kind'] == 'external'
                     or (node['kind'] == 'entry' and node['entry_id'] in reference_ids))
    }
    external = {i for i, node in enumerate(proposal['nodes']) if node and node['kind'] == 'external'}
    conditional = evaluate(proposal, assume_external_slot=assumed) if assumed else deepcopy(local)
    conditional.pop('local_route', None)
    slots = conditional['route_nodes']
    aptitude = _aptitude_summary(proposal, conditional)
    anti_a = conditional['aptitude']['anti_a']
    violated = [item['label'] for item in anti_a['items'] if item['status'] in _POLLUTION]
    if tier == 0:
        _assume_ready_for_pair(conditional, aptitude)
    names = [slots[index]['display_name'] for index in DIRECT]
    affinity = conditional.get('compatibility')
    if affinity is None:
        affinity = evaluate_lineage(target, [row['character_id'] for row in slots], compatibility, characters)
    if 'race_affinity' not in affinity:
        affinity = {**affinity, 'race_affinity': evaluate_race_affinity(
            [_slot_race_records(row) for row in slots], catalog=race_catalog,
            base_total=affinity['total'], base_known_subtotal=affinity['known_subtotal'])}
    route_id = sha256(json.dumps(proposal['nodes'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    uses_reference = any(row['status'] in ('reference', 'unassigned_reference') for row in slots)
    if steps is None:
        steps = [f"{task['label']}。{task['reason']}" for task in conditional['tasks']]
        steps.append('核对两位亲代的成品与借用状态后，准备最终育成。')
    else:
        steps = list(steps)
    if aptitude['has_s_target']:
        steps.append('育成中通过三次继承事件冲刺 S 适性：已优先保开局 A 并叠加 S 目标红星。')
    if uses_reference:
        steps.append('方案含未归属或全部资料参考成品，正式开局前请确认实际持有或可借账号。')
    return {'tier': tier, 'tier_label': _TIER_LABELS[tier], 'route_id': route_id,
            'label': label or f'{names[0]} × {names[1]}',
            'summary': ('按已记录成品和借用条件核对最终亲代。' if tier == 0 else
                        '先培养计划槽位，再用实测成品核对最终育成条件。'),
            'local_route': proposal, 'local_review': local, 'conditional_review': conditional,
            'aptitude_summary': aptitude,
            'anti_a_summary': {'status': anti_a['status'],
                               'polluted': bool(violated) or anti_a['status'] in _POLLUTION,
                               'violated_labels': violated},
            'factor_summary': _factor_summary(proposal, conditional, entries, groups, material_factors),
            'compatibility': affinity, 'breeding_steps': steps, 'uses_external': bool(external),
            'uses_borrow': bool(external) or any(row['status'] == 'borrowed' for row in slots),
            'uses_reference': uses_reference,
            'planned_count': sum(row['kind'] == 'planned' for row in slots)}


@dataclass
class _Material:
    spec: dict
    entry_id: str
    ids: tuple
    borrow: bool
    family_red: Counter
    self_red: Counter
    g1_events: tuple

    @property
    def character(self):
        return self.ids[0]


def _g1_events(node, catalog):
    records = (node or {}).get('race_records') or {}
    return frozenset(race['event_key'] for win in records.get('wins', ())
                     if win['status'] == 'confirmed' and
                     (race := catalog.by_id.get(win['race_id'])) is not None and race.get('grade') == 'G1')


def _shared_g1_points(node_events):
    return RACE_AFFINITY_G1_POINTS * sum(
        len(node_events[left] & node_events[right]) for _, _, _, left, right in RACE_AFFINITY_PAIRS)


_UNKNOWN_BRANCH_EVENTS = (frozenset(),) * 3


def _material(spec, entries, borrowed, characters, relevant_keys, race_catalog):
    external = spec['kind'] == 'external'
    entry = spec['entry'] if external else entries.get(spec['entry_id'])
    if entry is None:
        return None
    own = dict(entry['self'], entry_id=entry['entry_id'])
    for key in ('character_id', 'character_name', 'variant_id'):
        own[key] = own.get(key) or entry.get(key)
    facts = [own, *entry['parents']]
    ids = [resolve_node_identity(fact, characters)[0] for fact in facts]
    if not external:
        for index in (1, 2):
            linked = entries.get((facts[index] or {}).get('entry_id'))
            if linked is None:
                continue
            node = linked['self']
            identifier, _ = resolve_node_identity(
                {'character_id': node.get('character_id') or linked.get('character_id'),
                 'character_name': node.get('character_name') or linked.get('character_name')}, characters)
            snapshot_variant = facts[index].get('variant_id')
            variant = node.get('variant_id') or linked.get('variant_id')
            if ((ids[index] is None or identifier is None or ids[index] == identifier)
                    and (not snapshot_variant or not variant or snapshot_variant == variant)):
                facts[index], ids[index] = node, identifier
    reported = {row['factor_key']: row['family_stars'] for row in spec.get('reference_totals', ())}
    family_red, self_red = Counter(), Counter()
    for key in relevant_keys:
        coverage = red_star_coverage_from_nodes([*facts, None, None, None], key)
        family_red[key] = max(coverage['known_stars'] or 0, reported.get(key, 0))
        self_red[key] = coverage['slots'][0]['known_stars'] or 0
    g1_events = (_UNKNOWN_BRANCH_EVENTS if external else
                 tuple(_g1_events(fact, race_catalog) for fact in facts))
    return _Material(spec, entry['entry_id'], tuple(ids), external or entry['entry_id'] in borrowed,
                     family_red, self_red, g1_events)


def _placement_candidates(left, right, unconstrained):
    if unconstrained:
        for first, second in combinations(left, 2):
            if first.borrow and not second.borrow:
                first, second = second, first
            yield first, second
    else:
        yield from product(left, right)


def _affinity_points(target, lineage, catalog):
    ids = (target, *lineage)
    total = 0
    for _, _, indexes in COMPONENTS:
        values = [ids[i] for i in indexes]
        if all(value in catalog.character_ids for value in values) and (
            len(values) == 2 or values[0] != values[2]
        ):
            total += catalog.score(values)
    return total


def _parent_outfits(characters, aptitude_catalog, key):
    attribute = FACTOR_KEYS[key]
    return sorted(((identifier, variant, aptitude.get(attribute), sum(rank == 'A' for rank in aptitude.values()))
                   for identifier, variant, aptitude in _general_candidate_outfits(characters, aptitude_catalog)),
                  key=lambda row: (-(RANKS.index(row[2]) if row[2] else -1), row[3], row[0], row[1] or ''))


def _best_parent(outfits, locked, key, gp_stars, forbidden, target, fixed, children, catalog):
    if locked:
        outfits = [row for row in outfits if row[0] == locked['character_id'] and
                   (locked['variant_id'] is None or row[1] == locked['variant_id'])]
        if not outfits:
            outfits = [(locked['character_id'], locked['variant_id'], None, 0)]
    best = None
    for identifier, variant, rank, a_count in outfits:
        if identifier in forbidden:
            continue
        required = minimum_stars_for_rank(rank, 'A') if rank else None
        opens_at_a = required is not None and gp_stars >= required
        affinity = _affinity_points(target, (*fixed.ids, identifier, children[0], children[1]), catalog)
        score = (not opens_at_a, -(affinity - 2 * a_count), -(RANKS.index(rank) if rank else -1),
                 identifier, variant or '')
        if best is None or score < best[0]:
            best = score, locked or {'kind': 'planned', 'character_id': identifier, 'variant_id': variant,
                                     'red_target': {'factor_key': key, 'stars': 3}}, opens_at_a
    return best


def _tier_block(index, title, suggestions, *, status=None, note=None):
    return {'tier': index, 'title': title, 'status': status or ('ok' if suggestions else 'no_candidates'),
            'note': note or ('按当前材料和目标排序。' if suggestions else '当前材料不足以形成这一档方案。'),
            'suggestions': suggestions}


def _pair_rank(row, opens_at_a=None):
    aptitude, factors = row['aptitude_summary'], row['factor_summary']
    issue = bool(row['conditional_review']['selection_issues']) or row['anti_a_summary']['polluted']
    points = affinity_ordering_score(row['compatibility'])
    lead = (issue, aptitude['remaining_gap'])
    suffix = (-points, row['uses_external'], row['uses_borrow'],
              row.get('uses_reference', False), row['route_id'])
    completed = (-factors['satisfied_priority'], -factors['confirmed_stars'])
    s_stars = -aptitude.get('s_goal_stars', 0)
    if opens_at_a is None:
        return (*lead, *completed, s_stars, -aptitude['known_goal_stars'], *suffix)
    return (*lead, not opens_at_a, *completed, s_stars, *suffix)


def auto_pair_routes(
    target_character_id, route, evaluate, *, library, projected, pool_items=(),
    compatibility, characters, aptitude_catalog, groups=(), include_mali_pool=True,
    respect_locked_slots=False, limit=3, race_catalog=None,
):
    """Search ready pairs, one immediate breeding step and a six-slot plan."""
    if type(target_character_id) is not int or target_character_id < 1:
        raise ValueError('请先选择本次育成的目标马娘。')
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError('自动配对每档显示数量应为 1～10。')
    if type(include_mali_pool) is not bool or type(respect_locked_slots) is not bool:
        raise ValueError('候选池与锁定槽位选项应为开关。')
    race_catalog = race_catalog or RaceCatalog.load()
    target = target_character_id
    route = normalize_local_route(route) or normalize_local_route({})
    start_route, s_goal_keys = start_capped_route(route)
    locked = route['nodes'] if respect_locked_slots else [None] * 6
    entries = {entry['entry_id']: entry for entry in library['entries']}
    owned = [entry for entry in projected if entry['availability'] == 'owned']
    borrowed = [entry for entry in projected if entry['availability'] == 'borrowed']
    references = [entry for entry in projected
                  if entry['eligible'] and entry['availability'] in ('reference', 'unassigned_reference')]
    borrowed_ids = {entry['entry_id'] for entry in borrowed}
    reference_ids = {entry['entry_id'] for entry in references}

    def pair_evaluate(proposal_route, *, assume_external_slot=None):
        ref_slots = {
            i for i, node in enumerate((proposal_route or {}).get('nodes') or ())
            if node and node.get('kind') == 'entry' and node.get('entry_id') in reference_ids
        }
        merged = frozenset(assume_external_slot or ()) | ref_slots
        return evaluate(proposal_route, assume_external_slot=merged) if merged else evaluate(proposal_route)

    empty = pair_evaluate({**start_route, 'nodes': [None] * 6})['aptitude']
    required = {item['factor_key']: item['plan']['required_stars'] or 0 for item in empty['items']}
    protected = {item['factor_key']: item['a_threshold_stars'] for item in empty['anti_a']['items']}
    keys = required.keys() | protected.keys()
    specs = [{'kind': 'entry', 'entry_id': entry['entry_id']} for entry in (*owned, *borrowed, *references)]
    if include_mali_pool:
        specs.extend(_external_spec(item) for item in pool_items)
    materials = [value for spec in specs
                 if (value := _material(spec, entries, borrowed_ids, characters, keys, race_catalog)) is not None]
    requirements = [{key: value for key, value in group['requirement'].items() if key != 'priority'}
                    for group in groups]
    factor_cache = {}

    def material_factors(spec, entries, scope):
        if not requirements:
            return []
        entry_id = spec['entry']['entry_id'] if spec['kind'] == 'external' else spec['entry_id']
        key = (spec['kind'], entry_id, scope)
        if key not in factor_cache:
            factor_cache[key] = _factor_material(spec, entries, requirements, scope)
        return factor_cache[key]

    def confirmed_rank(parts):
        stars = [_sum_known(values) or 0 for values in zip(*[
            material_factors(material.spec, entries, scope) for material, scope in parts])]
        priority = sum(group['requirement']['priority'] for group, n in zip(groups, stars)
                       if n >= group['requirement']['min_stars'])
        capped = sum(min(n, group['requirement']['min_stars']) for group, n in zip(groups, stars))
        return -priority, -capped

    def pre_rank(stars, parts, red_target=None):
        totals = stars.copy()
        if red_target:
            totals[red_target['factor_key']] += red_target['stars']
        pollution = any(threshold is not None and totals[key] >= threshold
                        for key, threshold in protected.items())
        gap = sum(max(0, needed - totals[key]) for key, needed in required.items())
        return pollution, gap, *confirmed_rank(parts)

    def shortlist(scope, cap):
        if len(materials) <= cap:
            return materials
        def priority(material):
            stars = material.family_red if scope == 'pedigree' else material.self_red
            race_evidence = sum(map(len, material.g1_events)) if scope == 'pedigree' else 0
            return (*pre_rank(stars, [(material, scope)]), -race_evidence,
                    material.entry_id in reference_ids, material.borrow, material.entry_id)
        # Keep at least one candidate per role/borrow combination before taking
        # duplicate entries for especially strong characters.
        unique, repeats = {}, []
        for material in sorted(materials, key=priority):
            key = (material.character, material.borrow)
            if key in unique:
                repeats.append(material)
            else:
                unique[key] = material
        return [*unique.values(), *repeats][:cap]

    def choices(index, candidates):
        if locked[index] is None:
            return candidates
        if locked[index]['kind'] == 'planned':
            return []
        value = _material(locked[index], entries, borrowed_ids, characters, keys, race_catalog)
        return [value] if value is not None else []

    def format_tier(tier, nodes, **detail):
        return _format_pair(tier, {**route, 'nodes': nodes}, evaluate, target, compatibility,
                            characters, entries, groups, material_factors, race_catalog=race_catalog,
                            reference_ids=reference_ids, **detail)

    parents = shortlist('pedigree', 32)
    ancestors = shortlist('self', 24)
    # Fully evaluate all small Tier 0 inventories. Larger searches evaluate a
    # bounded shortlist after scoring every eligible pairing of retained roles.
    full_rank_limit = max(32, 8 * limit)

    def direct_options():
        if any(locked[i] for i in (1, 2, 4, 5)):
            return
        both_locked = locked[0] is not None and locked[3] is not None
        for a, b in _placement_candidates(choices(0, parents), choices(3, parents),
                                          not (locked[0] or locked[3])):
            if ((a.borrow and b.borrow and not both_locked) or a.entry_id == b.entry_id
                    or (a.character is not None and a.character == b.character)
                    or target in (a.character, b.character)):
                continue
            parts = [(a, 'pedigree'), (b, 'pedigree')]
            stars = a.family_red + b.family_red
            points = (_affinity_points(target, (*a.ids, *b.ids), compatibility)
                      + _shared_g1_points((*a.g1_events, *b.g1_events)))
            rank = (*pre_rank(stars, parts),
                    -sum(stars[key] for key in s_goal_keys),
                    -sum(stars[key] for key in required),
                    -points,
                    a.spec['kind'] == 'external' or b.spec['kind'] == 'external',
                    a.borrow or b.borrow,
                    a.entry_id in reference_ids or b.entry_id in reference_ids,
                    a.entry_id, b.entry_id)
            yield rank, [a.spec, None, None, b.spec, None, None]

    direct_rows = direct_options()
    if len(materials) > 12:
        direct_rows = nsmallest(full_rank_limit, direct_rows, key=lambda row: row[0])
    direct = [format_tier(0, nodes) for _, nodes in direct_rows]
    direct.sort(key=_pair_rank)

    def one_generation_materials():
        branch_a_planned = locked[0] and locked[0]['kind'] == 'planned'
        branch_b_finished = locked[3] and locked[3]['kind'] != 'planned'
        fixed_index = 3 if branch_a_planned or branch_b_finished or any(locked[i] for i in (1, 2)) else 0
        planned_index = 3 - fixed_index
        planned = locked[planned_index]
        if (planned and planned['kind'] != 'planned') or any(locked[fixed_index + i] for i in (1, 2)):
            return
        indexes = planned_index + 1, planned_index + 2
        left_options, right_options = (choices(index, ancestors) for index in indexes)
        unlabeled_ancestors = not any(locked[i] for i in indexes)
        for fixed in choices(fixed_index, parents):
            if fixed.character is None or fixed.character == target:
                continue
            race_points = _shared_g1_points((*fixed.g1_events, *_UNKNOWN_BRANCH_EVENTS))
            for left, right in _placement_candidates(left_options, right_options, unlabeled_ancestors):
                if left.borrow and right.borrow or None in (left.character, right.character):
                    continue
                if len({fixed.character, left.character, right.character}) < 3:
                    continue
                stars = fixed.family_red + left.self_red + right.self_red
                gap = {key: max(0, need - stars[key]) for key, need in required.items()}
                if planned:
                    red = planned['red_target']
                    key = (red or {}).get('factor_key')
                    if key in protected or planned['character_id'] in {target, fixed.character, left.character, right.character}:
                        continue
                else:
                    key = max((key for key in gap if gap[key] > 0 and key not in protected),
                              key=lambda key: min(3, gap[key]), default=None)
                    if key is None:
                        key = _general_filler(empty['items'], stars, protected, groups, target,
                                              route['target_variant_id'], aptitude_catalog,
                                              s_goal_keys=s_goal_keys)
                    if key is None:
                        continue
                    red = {'factor_key': key, 'stars': 3}
                parts = [(fixed, 'pedigree'), (left, 'self'), (right, 'self')]
                rank = (*pre_rank(stars, parts, red), fixed.entry_id, left.entry_id, right.entry_id)
                yield rank, fixed_index, fixed, planned, left, right, key, race_points

    outfit_cache = {}
    one_gen_options = []
    one_gen_materials = one_generation_materials()
    complete_search = len(materials) <= 8
    if not complete_search:
        one_gen_materials = nsmallest(max(160, 20 * limit), one_gen_materials,
                                     key=lambda row: (*row[0][:4], -row[-1], *row[0][4:]))
    for rank, fixed_index, fixed, planned, left, right, key, race_points in one_gen_materials:
        if key is None:
            parent, opens_at_a = planned, False
        else:
            if key not in outfit_cache:
                outfit_cache[key] = _parent_outfits(characters, aptitude_catalog, key)
            choice = _best_parent(outfit_cache[key], planned, key, left.self_red[key] + right.self_red[key],
                                  {target, fixed.character, left.character, right.character},
                                  target, fixed, (left.character, right.character), compatibility)
            if choice is None:
                continue
            _, parent, opens_at_a = choice
        first = [fixed.spec, None, None]
        second = [parent, left.spec, right.spec]
        nodes = first + second if fixed_index == 0 else second + first
        lineage = (*fixed.ids, parent['character_id'], left.character, right.character)
        affinity = _affinity_points(target, lineage, compatibility) + race_points
        external = any(material.spec['kind'] == 'external' for material in (fixed, left, right))
        borrow = fixed.borrow or left.borrow or right.borrow
        one_gen_options.append(((*rank[:2], not opens_at_a, *rank[2:4], -affinity,
                                 external, borrow, *rank[4:]), nodes, opens_at_a))
    one_gen = []
    if not complete_search:
        one_gen_options = nsmallest(full_rank_limit, one_gen_options, key=lambda row: row[0])
    for _, nodes, opens_at_a in one_gen_options:
        row = format_tier(1, nodes)
        row['parent_opens_at_a'] = opens_at_a
        one_gen.append(row)
    one_gen.sort(key=lambda row: _pair_rank(row, row['parent_opens_at_a']))

    planned = plan_general_lineage(target, route, pair_evaluate, library=library, projected=projected,
        pool_candidates=pool_items if include_mali_pool else (), compatibility=compatibility,
        characters=characters, aptitude_catalog=aptitude_catalog, groups=groups,
        respect_locked_slots=respect_locked_slots, limit=limit)
    two_gen = [format_tier(2, blueprint['nodes'], label=blueprint.get('label'), steps=blueprint.get('steps'))
               for blueprint in planned['blueprints']]
    two_gen.sort(key=_pair_rank)
    return {'mode': 'auto_pair', 'target_character_id': target,
            'inventory_summary': {'owned_count': len(owned), 'borrowed_count': len(borrowed),
                                  'reference_count': len(references),
                                  'mali_pool_count': len(pool_items), 'include_mali_pool': include_mali_pool,
                                  'respect_locked_slots': respect_locked_slots},
            'tier0_direct': _tier_block(0, '0 代直出（直接开局）', direct[:limit]),
            'tier1_one_gen': _tier_block(1, '1 代现养（固定一侧成品 + 现养一匹自有亲代）', one_gen[:limit]),
            'tier2_two_gen': _tier_block(2, '2 代精养染色（通用六槽家系分配）', two_gen,
                                         status=planned['status'], note=planned['note'])}
