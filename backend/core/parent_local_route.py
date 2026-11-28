"""Local breeding drafts made from actual horses and explicit future targets.

Plans never become observed library entries. A finished parent fixes its real
ancestors; a different ancestry is a request to breed a new parent instead.
"""
from copy import deepcopy
from collections import defaultdict
from functools import lru_cache
import re

from core.parent_aptitude import FACTOR_KEYS, RANKS, START_RANKS, _confirmed_start_rank
from core.parent_compatibility import character_id, evaluate_lineage, grade_for_score, resolve_node_identity
from core.parent_race_records import RACE_AFFINITY_G1_POINTS, RACE_AFFINITY_PAIRS, evaluate_race_affinity


NODE_KEYS = ('parent_a', 'gp_a1', 'gp_a2', 'parent_b', 'gp_b1', 'gp_b2')
NODE_LABELS = ('亲代 A', 'A 的亲代 1', 'A 的亲代 2', '亲代 B', 'B 的亲代 1', 'B 的亲代 2')
RED_LABELS = ('草地', '泥地', '短距离', '英里', '中距离', '长距离', '领跑', '跟前', '居中', '后追')
CANONICAL_RED_KEYS = tuple('red:' + label for label in RED_LABELS)
LARC_FIXED_ORDINALS = frozenset({33, 40, 42, 59, 64, 66})
LARC_OVERSEAS_LOCKED_ORDINALS = frozenset({36, 37, 38, 39, 41, 60, 61, 62, 63, 65})
_LARC_STAGE_YEARS = {'junior': 1, 'classic': 2, 'senior': 3}
_LARC_STAGE_LABELS = ('初级年', '经典年', '资深年')
_RACE_DISTANCES = {'short': 'sprint', 'mile': 'mile', 'middle': 'medium', 'long': 'long'}
_LARC_STYLES = {'nige': 'leader', 'senko': 'front', 'sashi': 'middle', 'oikomi': 'back'}
BORROW_PAIR_REFERENCE = {
    'scope': 'two_direct_parents_for_one_breeding_operation', 'status': 'local_reference',
    'local_source': 'integrations/parent-planner-source-review-20261021/umashow-evidence/src/renderer/ui/SuccessionPlanner.tsx',
    'source_lines': [391, 416],
    'note': '同一次配种的两位直接亲代不能同时借用；不对整条家系或不同未来分支累计借用次数。',
}


def _fields(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(label + '字段无效。')


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', value):
        raise ValueError('成品记录编号无效。')
    return value


def _variant(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,16}', value):
        raise ValueError('衣装请选择明确编号，未确认时可留空。')
    return value


def factor_key(value):
    if not isinstance(value, str):
        raise ValueError('请选择一种红因子。')
    value = value.replace('pink:', 'red:', 1) if value.startswith('pink:') else value
    if value not in CANONICAL_RED_KEYS:
        raise ValueError('请选择已收录的场地、距离或跑法红因子。')
    return value


def _external_slot(value):
    from core import parent_library as store
    _fields(value, ('kind', 'entry', 'borrow_confirmed', 'reference_totals'), '吗哩吗哩候选槽位')
    if value.get('borrow_confirmed', False) is not False:
        raise ValueError('外部候选仅作为待确认借用素材。')
    entry = store._entry(value.get('entry'))
    entry.pop('portraits', None)
    for node in (entry['self'], *entry['parents']):
        if node is not None:
            node.pop('race_records', None)
    totals = value.get('reference_totals', [])
    if not isinstance(totals, list) or len(totals) > 200:
        raise ValueError('外部家系星数格式无效。')
    normalized = {}
    for total in totals:
        _fields(total, ('factor_key', 'family_stars'), '外部家系星数')
        key = store._key(total.get('factor_key'))
        stars = total.get('family_stars')
        if key is None or type(stars) is not int or not 1 <= stars <= 9 or key in normalized:
            raise ValueError('外部家系星数应为每个因子一条 1～9 星记录。')
        normalized[key] = stars
    return {'kind': 'external', 'entry': entry, 'borrow_confirmed': False,
            'reference_totals': [{'factor_key': key, 'family_stars': stars}
                                 for key, stars in normalized.items()]}


def _aptitude_goal(goal, *, legacy=False):
    _fields(goal, ('factor_key', 'baseline_rank', 'target_rank', 'target_variant_id'), '适性目标')
    baseline, target = goal.get('baseline_rank'), goal.get('target_rank', 'A' if legacy else None)
    if (baseline is not None and baseline not in START_RANKS) or target not in RANKS:
        raise ValueError('基础适性应为 G～A，目标可选到 S；未确认的基础适性可留空。')
    return {'factor_key': factor_key(goal.get('factor_key', 'red:泥地' if legacy else None)),
            'baseline_rank': baseline, 'target_rank': target}


def normalize_avoid_a_factors(value):
    if not isinstance(value, list) or len(value) > len(CANONICAL_RED_KEYS):
        raise ValueError('禁升 A 保护项应为至多十项的列表。')
    by_key = {}
    for row in value:
        _fields(row, ('factor_key', 'baseline_rank'), '禁升 A 保护项')
        key, baseline = factor_key(row.get('factor_key')), row.get('baseline_rank')
        if key in by_key:
            raise ValueError('同一种红因子只能设置一个禁升 A 保护项。')
        if baseline is not None and baseline not in START_RANKS:
            raise ValueError('禁升 A 保护项的先天适性应为 G～A，未确认时可留空。')
        by_key[key] = {'factor_key': key, 'baseline_rank': baseline}
    return [by_key[key] for key in CANONICAL_RED_KEYS if key in by_key]


def normalize_local_route(value):
    if value is None:
        return None
    _fields(value, ('aptitude_goal', 'aptitude_goals', 'avoid_a_factors', 'target_variant_id', 'nodes'), '本地路线')
    variant = _variant(value.get('target_variant_id'))
    legacy = value.get('aptitude_goal', {})
    has_multiple_field = 'aptitude_goals' in value
    if has_multiple_field and legacy is None:
        legacy = {}
    primary = _aptitude_goal(legacy, legacy=True)
    legacy_variant = _variant(legacy.get('target_variant_id'))
    if variant is not None and legacy_variant is not None and variant != legacy_variant:
        raise ValueError('育成目标衣装编号不一致。')
    variant = variant if variant is not None else legacy_variant
    if has_multiple_field:
        raw_goals = value['aptitude_goals']
        if not isinstance(raw_goals, list) or len(raw_goals) > len(CANONICAL_RED_KEYS):
            raise ValueError('多维适性目标应为至多十项的列表。')
        by_key = {}
        for raw_goal in raw_goals:
            goal = _aptitude_goal(raw_goal)
            goal_variant = _variant(raw_goal.get('target_variant_id'))
            if goal_variant is not None and goal_variant != variant:
                raise ValueError('育成目标衣装编号不一致。')
            key = goal['factor_key']
            if key in by_key:
                raise ValueError('同一种红因子只能设置一个适性目标。')
            by_key[key] = goal
        goals = [by_key[key] for key in CANONICAL_RED_KEYS if key in by_key]
        primary = goals[0] if goals else {'factor_key': 'red:泥地', 'baseline_rank': None, 'target_rank': 'G'}
    elif 'avoid_a_factors' in value and 'aptitude_goal' not in value:
        goals = []
        primary = {'factor_key': 'red:泥地', 'baseline_rank': None, 'target_rank': 'G'}
    else:
        goals = [primary]

    avoid_a = normalize_avoid_a_factors(value.get('avoid_a_factors', []))
    if {row['factor_key'] for row in avoid_a} & {goal['factor_key'] for goal in goals}:
        raise ValueError('同一适性不能同时设置改适性目标与禁升 A 保护。')

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
        elif row.get('kind') == 'external':
            nodes.append(_external_slot(row))
        elif row.get('kind') == 'planned':
            _fields(row, ('kind', 'character_id', 'variant_id', 'red_target'), '待培养槽位')
            red = row.get('red_target')
            if red is not None:
                _fields(red, ('factor_key', 'stars'), '待培养红因子目标')
                if type(red.get('stars')) is not int or not 1 <= red['stars'] <= 3:
                    raise ValueError('单匹待培养成品的红因子目标应为 1～3 星。')
                red = {'factor_key': factor_key(red.get('factor_key')), 'stars': red['stars']}
            nodes.append({'kind': 'planned', 'character_id': character_id(row.get('character_id')),
                'variant_id': _variant(row.get('variant_id')), 'red_target': red})
        else:
            raise ValueError('槽位请选择已有成品或待培养角色。')
    return {'target_variant_id': variant, 'aptitude_goals': goals, 'avoid_a_factors': avoid_a,
            'aptitude_goal': {**primary, 'target_variant_id': variant}, 'nodes': nodes}


def route_entry_ids(route):
    route = normalize_local_route(route)
    return [] if route is None else list(dict.fromkeys(
        row['entry_id'] for row in route['nodes'] if row and row['kind'] == 'entry'))


def _own_node(entry):
    node = deepcopy(entry.get('self') or {})
    for key in ('character_id', 'character_name', 'variant_id', 'variant_name', 'nickname'):
        if not node.get(key):
            node[key] = entry.get(key)
    if not node.get('character_id') and entry.get('character_name'):
        node['character_name'] = entry['character_name']
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


def _schedule_aptitudes(character, variant, aptitude_catalog, child_nodes, manual_baselines):
    stars = defaultdict(int)
    for node in child_nodes:
        if not node or ('usable' in node and node['usable'] is not True):
            continue
        for row in (node.get('_fact') or node).get('factors', ()):
            if row.get('kind') == 'red' and type(row.get('stars')) is int and 1 <= row['stars'] <= 3:
                stars[row.get('factor_key')] += row['stars']
    manual_baselines = manual_baselines or {}
    profile = {}
    for key, attribute in FACTOR_KEYS.items():
        manual = manual_baselines.get(key, manual_baselines.get(attribute))
        rank = manual
        if aptitude_catalog is not None:
            baseline = aptitude_catalog.baseline(character, variant_id=variant, factor_key=key, manual_rank=manual)
            rank = baseline['rank']
            if baseline['status'] == 'ambiguous_variant' and variant is None:
                rank = min((row['rank'] for row in baseline['candidates']), key=START_RANKS.index)
        profile[attribute] = _confirmed_start_rank(rank, stars[key])
    return profile


def _race_turn(race):
    date = race.get('career_time') or {}
    turn = date.get('global_turn')
    if turn is not None:
        return turn if type(turn) is int else None
    year = date.get('year_index') or _LARC_STAGE_YEARS.get(date.get('stage'))
    month, half = date.get('month'), date.get('half')
    if type(year) is int and year > 0 and type(month) is int and 1 <= month <= 12 and half in ('first', 'second'):
        return (year - 1) * 24 + (month - 1) * 2 + (half == 'second') + 1
    return None


def _partner_g1_events(slot_index, partner_nodes, race_catalog):
    partners = []
    for _, left, right, li, ri in RACE_AFFINITY_PAIRS:
        if slot_index == li:
            partners.append(right)
        elif slot_index == ri:
            partners.append(left)
    shared = defaultdict(list)
    for partner in partners:
        node = partner_nodes.get(partner) or {}
        records = (node.get('_fact') or node).get('race_records') or {}
        events = set()
        for win in records.get('wins', ()):
            race = race_catalog.by_id.get(win.get('race_id'))
            if win.get('status') == 'confirmed' and race is not None and race.get('grade') == 'G1':
                events.add(race['event_key'])
        for event in events:
            shared[event].append(partner)
    return shared


@lru_cache(maxsize=128)
def _choose_g1_schedule(options, max_races):
    """Optimize shared partner events, then starts, while keeping fixed starts."""
    by_turn = defaultdict(list)
    event_turns = defaultdict(set)
    for turn, race_id, event, shared_count in options:
        by_turn[turn].append((race_id, event, shared_count))
        if shared_count:
            event_turns[event].add(turn)
    # Only repeats need a bit. Forget each bit after the last turn when it can
    # affect an award, so closed event groups do not increase the state space.
    repeated = [event for event, turns in event_turns.items() if len(turns) > 1]
    bits = {event: 1 << index for index, event in enumerate(repeated)}
    closing = defaultdict(int)
    for event, bit in bits.items():
        closing[max(event_turns[event])] |= bit

    states = {(0, 0, 0): (0, ())}
    previous_turn = -1
    for turn in sorted(by_turn.keys() | LARC_FIXED_ORDINALS):
        fixed = turn in LARC_FIXED_ORDINALS
        clear = ~closing[turn]
        updated = {}

        def retain(state, score, selected):
            prior = updated.get(state)
            if prior is None or score > prior[0] or (score == prior[0] and selected < prior[1]):
                updated[state] = (score, selected)

        for (count, streak, mask), (score, selected) in states.items():
            if previous_turn != turn - 1:
                streak = 0
            if not fixed:
                retain((count, 0, mask & clear), score, selected)
            if streak >= 2:
                continue
            if fixed:
                retain((count, streak + 1, mask & clear), score, selected)
            elif count < max_races:
                for race_id, event, shared_count in by_turn[turn]:
                    bit = bits.get(event, 0)
                    gain = shared_count if not mask & bit else 0
                    retain((count + 1, streak + 1, (mask | bit) & clear), score + gain, (*selected, race_id))
        states = updated
        previous_turn = turn
    return min(states.values(), key=lambda row: (-row[0], -len(row[1]), row[1]))[1]


def plan_larc_g1_schedule(
    *, slot_index, character_id, variant_id=None, aptitude_catalog, race_catalog, child_nodes=(),
    partner_nodes=None, manual_baselines=None, max_races=14,
):
    """Return a read-only L'Arc G1 plan and Pacer import payload."""
    if type(slot_index) is not int or not 0 <= slot_index < len(NODE_KEYS):
        raise ValueError('请选择有效的继承槽位。')
    if type(max_races) is not int or not 1 <= max_races <= 24:
        raise ValueError('最大参赛数须为 1～24 的整数。')
    profile = _schedule_aptitudes(character_id, variant_id, aptitude_catalog, child_nodes, manual_baselines)
    runnable = {name for name, rank in profile.items() if rank in ('B', 'A')}
    surfaces = sorted(runnable & {'turf', 'dirt'})
    distances = sorted(_RACE_DISTANCES[key] for key in runnable & _RACE_DISTANCES.keys())
    best_style = max((key for key in _LARC_STYLES if key in runnable), default=None,
                     key=lambda key: START_RANKS.index(profile[key]))
    style = _LARC_STYLES.get(best_style, 'keep')
    shared = _partner_g1_events(slot_index, partner_nodes or {}, race_catalog)
    eligible = {}
    for race_id, race in race_catalog.by_id.items():
        if (race.get('grade') != 'G1' or race.get('schedule_status') == 'unavailable'
                or race.get('surface') not in surfaces or race.get('distance_category') not in distances):
            continue
        turn = _race_turn(race)
        if turn is None:
            continue
        ordinal = turn - 1
        if (12 <= ordinal <= 65 and ordinal not in LARC_FIXED_ORDINALS
                and ordinal not in LARC_OVERSEAS_LOCKED_ORDINALS):
            eligible[race_id] = ordinal, race
    options = tuple(sorted((ordinal, race_id, race['event_key'], len(shared[race['event_key']]))
                           for race_id, (ordinal, race) in eligible.items()))
    chosen = _choose_g1_schedule(options, max_races)
    races = []
    counted = set()
    for race_id in chosen:
        ordinal, race = eligible[race_id]
        event = race['event_key']
        partners = shared[event]
        new_pairs = {(event, partner) for partner in partners} - counted
        counted.update(new_pairs)
        year, within_year = divmod(ordinal, 24)
        month, half = divmod(within_year, 2)
        race_name = race.get('name_cn') or race.get('source_name') or race_id
        races.append({'race_id': race_id, 'event_key': event, 'display_name': race_name,
                      **{key: race.get(key) for key in ('source_name', 'grade', 'surface', 'distance_m', 'distance_category')},
                      'larc_ordinal': ordinal, 'global_turn': ordinal + 1,
                      'date_label': f"{_LARC_STAGE_LABELS[year]}{month + 1}月{'下半月' if half else '上半月'}",
                      'shared_with': list(partners), 'confirmed_shared_with': list(partners),
                      'projected_bonus_points': RACE_AFFINITY_G1_POINTS * len(new_pairs)})
    return {'slot_index': slot_index, 'node_key': NODE_KEYS[slot_index], 'character_id': character_id,
            'variant_id': variant_id, 'max_races': max_races, 'aptitude_profile': profile,
            'runnable_surfaces': surfaces, 'runnable_distances': distances, 'recommended_running_style': style,
            'race_count': len(races), 'zero_three_streak': True, 'projected_shared_g1_count': len(counted),
            'projected_shared_g1_bonus': RACE_AFFINITY_G1_POINTS * len(counted), 'races': races,
            'pacer_preset': {
                'schema_version': 'uma.pacer_race_preset.v1', 'scenario': 'larc',
                'title': NODE_LABELS[slot_index] + ' · 凯旋门 G1 历战',
                'running_style': style, 'custom_races': list(chosen), 'race_running_styles': {},
                'notes': ['按已知开局适性筛选，避开固定赛和远征冲突，保留零三连战。',
                          '共同胜鞍加分以本次计划赛事实际获胜为前提。'],
            }}


def evaluate_local_route(library, projected_entries, target_character_id, local_route,
                         *, race_catalog, aptitude_catalog, characters=(), assume_external_slot=None,
                         compatibility=None, max_races=14):
    from core.parent_aptitude import (
        detected_red_from_nodes, plan_anti_a_pollution, plan_aptitude, plan_multi_aptitude,
        red_node_summary, red_star_coverage_from_nodes,
    )
    from core.parent_race_records import RaceCatalog, records_summary

    race_catalog = race_catalog or RaceCatalog.load()
    target = character_id(target_character_id)
    route = normalize_local_route(local_route)
    if route is None:
        route = normalize_local_route({})
    goal, specs = route['aptitude_goal'], route['nodes']
    if assume_external_slot is None:
        assumed_slots = ()
    elif type(assume_external_slot) is int:
        assumed_slots = (assume_external_slot,)
    elif isinstance(assume_external_slot, (set, frozenset, list, tuple)):
        assumed_slots = assume_external_slot
    else:
        raise ValueError('吗哩吗哩借用推演需要指定一个或多个继承槽位。')
    if any(type(index) is not int or not 0 <= index < len(NODE_KEYS) for index in assumed_slots):
        raise ValueError('吗哩吗哩借用推演的槽位编号无效。')
    assumed_slots = frozenset(assumed_slots)
    by_id = {entry['entry_id']: entry for entry in library['entries']}
    resources = {entry['entry_id']: entry for entry in projected_entries}
    names = {row['character_id']: row for row in characters}
    slots = []
    issues = []

    def resolve_fact(fact):
        identifier, reason = resolve_node_identity(fact, characters)
        if identifier is not None:
            fact['character_id'] = str(identifier)
        return reason

    def slot(index, spec):
        row = {'key': NODE_KEYS[index], 'index': index, 'label': NODE_LABELS[index],
            'kind': 'unknown', 'status': 'unknown', 'status_label': '未选择',
            'character_id': None, 'variant_id': None, 'entry_id': None,
            'locked': False, 'usable': False, 'record_binding': None,
            'display_name': '未选择成品或待培养角色', 'notes': [], '_fact': None}
        if spec is None:
            return row
        if spec['kind'] == 'planned':
            fact = {'character_id': spec['character_id'], 'variant_id': spec['variant_id']}
            row.update(kind='planned', status='planned', status_label='待培养',
                character_id=spec['character_id'], variant_id=spec['variant_id'],
                display_name=_name(fact, names), red_target=deepcopy(spec['red_target']))
            return row
        external = spec['kind'] == 'external'
        entry = spec['entry'] if external else by_id.get(spec['entry_id'])
        row.update(kind=spec['kind'], entry_id=entry['entry_id'] if external else spec['entry_id'])
        if entry is None:
            row.update(status='missing', status_label='原成品已不在当前库', display_name='成品记录缺失')
            return row
        identifier = entry['entry_id']
        fact = _own_node(entry)
        identity_reason = resolve_fact(fact)
        if identity_reason is not None:
            message = ('角色名称对应多个角色，请先核对成品身份。' if identity_reason == 'ambiguous_character_name'
                       else '角色身份待确认，请先核对这条成品记录。')
            row['notes'].append(message)
            if index in (0, 3):
                issues.append(row['label'] + '：' + message)
        available = resources.get(identifier, {})
        availability = available.get('availability', 'reference')
        assumed_ref = (not external and index in assumed_slots
                       and available.get('eligible') is True
                       and availability in ('reference', 'unassigned_reference'))
        row.update(status=availability,
            status_label=available.get('availability_label', '仅作参考'),
            character_id=_identity(fact), variant_id=fact.get('variant_id'),
            display_name=_name(fact, names),
            usable=(available.get('available') is True) or assumed_ref,
            record_binding={'entry_id': identifier, 'node': 'self'}, _fact=fact)
        if external:
            assumed = index in assumed_slots
            trainer = re.fullmatch(r'mali-([0-9]{1,20})-[0-9a-f]{20}', identifier)
            row.update(status='borrowed' if assumed else 'external_reference',
                status_label='吗哩吗哩 · 假设可借' if assumed else '吗哩吗哩 · 待确认可借',
                usable=assumed, record_binding=None,
                trainer_id=trainer.group(1) if trainer else None,
                reference_totals=deepcopy(spec['reference_totals']))
        return row

    def needs_borrow(row):
        return row['kind'] == 'external' or row['status'] == 'borrowed'

    for index, spec in enumerate(specs):
        slots.append(slot(index, spec))
    blocked_training_pairs = set()
    if all(needs_borrow(slots[i]) for i in (0, 3)):
        issues.append('两位最终亲代都为可借成品，同一次配种不能同时借用；请先持有或培养其中一位。')
    for parent_index in (0, 3):
        parent = slots[parent_index]
        external = parent['kind'] == 'external'
        if external:
            entry = specs[parent_index]['entry']
        elif parent['kind'] == 'entry':
            entry = by_id.get(parent['entry_id'])
        else:
            entry = None
        if entry is None:
            continue
        for offset, snapshot in enumerate(entry.get('parents', [None, None]), 1):
            index = parent_index + offset
            if specs[index] is not None:
                issues.append(NODE_LABELS[index] + '已由成品亲代固定；调整祖辈前请将该亲代改为待培养。')
            fact = deepcopy(snapshot)
            if fact is not None:
                resolve_fact(fact)
            binding = None if external else {'entry_id': parent['entry_id'], 'node': 'parent' + str(offset)}
            note = []
            identity_conflict = False
            reference = (snapshot or {}).get('entry_id')
            if not external and reference in by_id:
                linked = _own_node(by_id[reference])
                resolve_fact(linked)
                observed_id, linked_id = _identity(fact), _identity(linked)
                observed_variant = (snapshot or {}).get('variant_id')
                linked_variant = linked.get('variant_id')
                conflict = (observed_id is not None and linked_id is not None and observed_id != linked_id
                    or observed_variant and linked_variant and observed_variant != linked_variant)
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
            row.update(kind='snapshot', status='identity_conflict' if identity_conflict else 'via_parent' if parent['usable'] else 'reference',
                status_label='身份冲突待核对' if identity_conflict else '随可用亲代继承' if parent['usable'] else '亲代参考快照',
                entry_id=(fact or {}).get('entry_id'), character_id=_identity(fact),
                variant_id=(fact or {}).get('variant_id'), locked=True,
                usable=parent['usable'] and not identity_conflict, display_name=_name(fact, names),
                record_binding=binding, notes=note, _fact=fact)
    for parent_index in (0, 3):
        parent = slots[parent_index]
        for child_index in (parent_index + 1, parent_index + 2):
            child = slots[child_index]
            if parent['character_id'] is not None and parent['character_id'] == child['character_id']:
                issues.append(parent['label'] + '与其亲代是同一角色，需要调整培养材料。')
        left, right = slots[parent_index + 1], slots[parent_index + 2]
        if parent['kind'] == 'planned' and all(needs_borrow(child) for child in (left, right)):
            blocked_training_pairs.add(parent_index)
            issues.append(parent['label'] + '的两位直接材料都为可借成品，同一次培养不能同时借用；请先持有或培养其中一位材料。')
        if left['character_id'] is not None and left['character_id'] == right['character_id']:
            issues.append(parent['label'] + '的两位亲代是同一角色，游戏中不能同时选用。')
    if slots[0]['character_id'] is not None and slots[0]['character_id'] == slots[3]['character_id']:
        issues.append('两位最终亲代是同一角色，游戏中不能同时选用。')
    if target is not None and target in (slots[0]['character_id'], slots[3]['character_id']):
        issues.append('最终亲代与育成目标是同一角色，需要调整。')

    usable = [row['_fact'] if row['usable'] else None for row in slots]
    aptitude = plan_multi_aptitude(route['aptitude_goals'], target_character_id=target,
        target_variant_id=route['target_variant_id'], aptitude_catalog=aptitude_catalog, usable_nodes=usable)
    items, budget = aptitude['items'], aptitude['slot_budget']
    if items:
        baseline = deepcopy(items[0]['baseline'])
        red_coverage = deepcopy(items[0]['red_coverage'])
        plan = deepcopy(items[0]['plan'])
    else:
        baseline = (aptitude_catalog.baseline(target, variant_id=route['target_variant_id'],
                    factor_key=goal['factor_key']) if aptitude_catalog is not None else
                    {'status': 'unknown', 'rank': None})
        red_coverage = red_star_coverage_from_nodes(usable, goal['factor_key'])
        plan = plan_aptitude(baseline.get('rank'), goal['target_rank'],
            known_stars=red_coverage['known_stars'], unknown_nodes=red_coverage['unknown_nodes'])
        plan.update(status='satisfied', required_stars=0, gap_from_confirmed=0, additional_stars=0,
                    message='本路线未设置开局适性目标。')
    if len(items) > 1 or budget['over_capacity']:
        status = aptitude['status']
        gap = aptitude['total_gap_from_confirmed']
        plan.update(status=status, required_stars=aptitude['total_required_stars'],
            known_stars=aptitude['total_known_goal_stars'], gap_from_confirmed=gap,
            additional_stars=gap if status == 'missing' else 0 if status == 'satisfied' else None)
    if budget['over_capacity']:
        minimum = budget['minimum_slots_for_targets']
        if minimum > budget['total_slots']:
            issues.append(f'多维适性目标至少需要 {minimum} 个 3 星红因子槽位，超出 6 个家系继承槽上限。')
        else:
            issues.append(f"剩余可调整槽位仅 {budget['open_slots']} 个，但当前多维适性缺口"
                          f"至少需要 {budget['minimum_additional_slots_needed']} 个红因子槽位。")
    detected = detected_red_from_nodes([row['_fact'] for row in slots])
    goal_keys = {item['factor_key'] for item in items}
    branch_reference_totals = {
        index: {row['factor_key']: row['family_stars'] for row in slots[index]['reference_totals']}
        for index in (0, 3) if slots[index]['usable'] and slots[index]['kind'] == 'external'
    }
    anti_a = plan_anti_a_pollution(route['avoid_a_factors'], target_character_id=target,
        target_variant_id=route['target_variant_id'], aptitude_catalog=aptitude_catalog, usable_nodes=usable,
        planned_red_targets=[row.get('red_target') if row['kind'] == 'planned' else None for row in slots],
        branch_reference_totals=branch_reference_totals, active_goal_keys=goal_keys,
        manual_baselines={row['factor_key']: row['baseline_rank'] for row in route['aptitude_goals']})
    avoid_keys = {item['factor_key'] for item in anti_a['items']}
    polluted_keys = set()
    for item in anti_a['items']:
        label = item['label']
        known, planned, threshold = item['known_stars'], item['planned_stars'], item['a_threshold_stars']
        match item['status']:
            case 'polluted':
                polluted_keys.add(item['factor_key'])
                issues.append(f'防污染告警：{label} 已确认 {known}★（≥{threshold}★ 升 A 污染红池）。')
            case 'planned_polluted':
                issues.append(f'防污染告警：{label} 已有 {known}★ 叠加待培养计划 +{planned}★ 后将触及 {threshold}★ 升 A 污染红池。')
            case 'already_a_at_baseline':
                issues.append(f'防污染设置冲突：{label} 先天已为 A，不能作为禁升 A 保护项。')
            case 'unknown_baseline':
                issues.append(f'防污染保护项「{label}」先天适性未知，请指定衣装或在十格表中手动填写。')
    reference_stars = 0
    for index, row in enumerate(slots):
        fact = row['_fact']
        red = (deepcopy(red_coverage['slots'][index]) if row['usable'] else
               red_node_summary(fact, goal['factor_key']))
        row['red_summary'] = red
        observed = detected[index].copy()
        observed.pop('slot')
        key = observed['factor_key']
        observed['matches_goal'] = key in goal_keys
        # Keep the established observation shape for routes without a lock.
        if avoid_keys:
            observed.update(in_avoid_a=key in avoid_keys,
                            triggers_anti_a=key in polluted_keys and (observed['stars'] or 0) > 0)
        row['detected_red'] = observed
        if not row['usable']:
            reference_stars += red.get('known_stars') or 0
        records = (fact or {}).get('race_records')
        identifier = (fact or {}).get('entry_id')
        row['race_summary'] = (records_summary(records, entry_id=identifier, catalog=race_catalog)
            if identifier is not None else {'entry_id': None, 'status': 'unknown',
                'coverage': 'unknown', 'known_win_count': None, 'complete': False, 'wins': []})
    aptitude.update(baseline=baseline, plan=plan, red_coverage=red_coverage, anti_a=anti_a,
                    selected_reference_stars=reference_stars)
    affinity_base = (evaluate_lineage(target, [row['character_id'] for row in slots], compatibility, characters)
                     if compatibility is not None else {})
    race_affinity = evaluate_race_affinity([(row['_fact'] or {}).get('race_records') for row in slots],
        catalog=race_catalog, base_total=affinity_base.get('total'),
        base_known_subtotal=affinity_base.get('known_subtotal', 0), grade_fn=grade_for_score)
    race_links = [{**link, 'known_common_count': link['confirmed_common_count'],
        'common_g1_count': link['confirmed_common_count'] if link['complete'] else None}
        for link in race_affinity['links']]
    if compatibility is not None:
        affinity_base['race_affinity'] = race_affinity
    partner_nodes = dict(zip(NODE_KEYS, slots))
    tasks = []
    # Ancestors precede the parent that depends on them. This is a local to-do
    # list, not an instruction to start a game or a claim of future factors.
    for index in (1, 2, 4, 5, 0, 3):
        row = slots[index]
        if row['kind'] != 'planned':
            continue
        children = [index + 1, index + 2] if index in (0, 3) else []
        dependencies = [slots[i]['key'] for i in children if slots[i]['kind'] == 'planned']
        identities = [row['character_id']] + [slots[i]['character_id'] for i in children]
        inputs_ready = (bool(children) and None not in identities and len(set(identities)) == 3
            and index not in blocked_training_pairs
            and all(slots[i]['kind'] in ('entry', 'external') and slots[i]['usable'] for i in children))
        schedule = plan_larc_g1_schedule(slot_index=index, character_id=row['character_id'],
            variant_id=row['variant_id'], aptitude_catalog=aptitude_catalog, race_catalog=race_catalog,
            child_nodes=[slots[i] for i in children], partner_nodes=partner_nodes, max_races=max_races)
        tasks.append({'node_key': row['key'], 'slot_index': index, 'character_id': row['character_id'],
            'variant_id': row['variant_id'], 'label': row['label'] + ' · ' + row['display_name'],
            'red_target': deepcopy(row.get('red_target')), 'depends_on': dependencies,
            'inputs_ready': inputs_ready, 'recommended_schedule': schedule,
            'reason': '两位直接材料都需要借用，请先持有或培养其中一位，再准备这次培养。' if index in blocked_training_pairs else
                '材料已在当前账号范围内，可准备培养该亲代。' if inputs_ready else
                '先补齐其材料；目标因子仅为计划，未写入成品库。' if children else
                '该祖辈待培养；其父母可另存一份培养方案。'})
    ready = (target is not None and not issues and all(slots[i]['kind'] in ('entry', 'external')
        and slots[i]['usable'] and slots[i]['status'] not in ('reference', 'unassigned_reference')
        and slots[i]['character_id'] is not None for i in (0, 3))
        and aptitude.get('status') in {'satisfied', 'ready'} and anti_a['status'] in {'inactive', 'safe'})
    for row in slots:
        row.pop('_fact')
    return {'local_route': route, 'target_character_id': target, 'route_nodes': slots,
        'aptitude': aptitude, 'race_affinity': race_affinity,
        **({'compatibility': affinity_base} if compatibility is not None else {}),
        'race_links': race_links, 'tasks': tasks, 'selection_issues': list(dict.fromkeys(issues)),
        'resource_rule_reference': deepcopy(BORROW_PAIR_REFERENCE),
        'ready_to_start': ready, 'notes': [
            '只有实际成品和明确可借材料计入可用红星；待培养目标不会变成已观测因子。',
            '随成品亲代继承的祖辈不要求另行持有；未知因子与确认零星分开。',
            '五组配种关系各自按已确认的同一 G1 赛事去重，每场加 3 点相性；胜场资料不完整时为已知下限。',
            '此处是本地培养计划，不会连接模拟器、开局、借种或替换游戏内设置。']}
