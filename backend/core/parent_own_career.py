"""Local own-breeding plan for the Kawasaki 2100 m front-runner template."""
import json
import math
from heapq import nsmallest
from itertools import product
from pathlib import Path

from core.parent_aptitude import (
    AptitudeCatalog, FACTOR_KEYS, RANKS, detected_red_summary, minimum_stars_for_rank, red_node_summary,
)
from core.parent_compatibility import COMPONENTS, CompatibilityCatalog, evaluate_lineage, resolve_node_identity
from core.parent_local_route import NODE_LABELS, normalize_local_route
from core.parent_race_records import RaceCatalog, common_wins, records_summary


TEMPLATE_ID = 'kawasaki_2100_front'
MIN_WINS = 26
TARGET_WINS = 27
SCENARIO_FACTOR = 'white:大师杯剧本'
MIDDLE = 'red:中距离'
DIRT = 'red:泥地'
TARGET_VARIANT = '109801'
TARGET_CHARACTER = 1098
CORE_WHITE = ('white:位置感', 'white:危险回避', 'white:点燃青春·智', 'white:拔得头筹')
GREEN_ACTIVE_TARGET = 6
GREEN_SKILLS = (
    {'name': '川崎赛场', 'factor_key': 'white:川崎赛场', 'source': 'dye', 'condition': '比赛在川崎'},
    {'name': '急弯', 'factor_key': 'white:急弯', 'source': 'dye', 'condition': '适用赛场含川崎'},
    {'name': '逆时针', 'factor_key': 'white:逆时针', 'source': 'dye', 'condition': '赛道左转'},
    {'name': '冬季优俊少女', 'factor_key': 'white:冬季优俊少女', 'source': 'conditional', 'condition': '比赛为冬季'},
    {'name': '交流重赏', 'factor_key': 'white:交流重赏', 'source': 'native', 'awakening_level': 4,
     'condition': '本期比赛带交流重赏标记'},
    {'name': '擅长迈步', 'factor_key': 'white:擅长迈步', 'source': 'native', 'awakening_level': 2,
     'condition': '泥地且基础力量至少1000，达到1200时效果更强'},
    {'name': '自信十足', 'factor_key': 'white:自信十足', 'source': 'conditional',
     'condition': '英里或中距离且基础速度、力量各至少1000'},
)
GREEN_PASSIVES = tuple(skill['factor_key'] for skill in GREEN_SKILLS)
GREEN_DYE_FACTORS = frozenset(skill['factor_key'] for skill in GREEN_SKILLS if skill['source'] == 'dye')
GREEN_FACTOR_ALIASES = {skill['name'] + suffix: skill['factor_key']
                        for skill in GREEN_SKILLS for suffix in ('', '○', '◎')}
VARIANT_NAMES = {
    '106801': '原皮北部玄驹', '102002': '舞会青云天空', '100402': '泳装丸善斯基',
    '100601': '原皮小栗帽', '101401': '原皮神鹰', '103401': '原皮稻荷一',
    '102301': '原皮琵琶晨光', '101901': '原皮爱丽数码', '102901': '原皮雪之美人',
    '100602': '圣诞小栗帽', '103202': '泳装爱丽速子',
}
DIRT_PALETTE = ('100402', '101401', '101901', '102901')
MIDDLE_PALETTE = ('100601', '103401', '102301')
SLOT_KEYS = ('parent_a', 'gp_a1', 'gp_a2', 'parent_b', 'gp_b1', 'gp_b2')
SLOT_LABELS = ('亲代 A', '北黑侧祖辈 1', '北黑侧祖辈 2', '亲代 B', '舞云侧祖辈 1', '舞云侧祖辈 2')
SLOT_FACTORS = (MIDDLE, DIRT, MIDDLE, MIDDLE, DIRT, MIDDLE)
REFERENCE = ('106801', '100402', '100601', '102002', '101401', '103401')
ANCESTOR_SLOTS = (1, 2, 4, 5)
ANCESTOR_PALETTES = (DIRT_PALETTE, MIDDLE_PALETTE, DIRT_PALETTE, MIDDLE_PALETTE)
ROSTER = Path(__file__).resolve().parent / 'autonomous_training' / 'plant_roster.json'
EDGES = ((0, 1), (0, 2), (3, 4), (3, 5), (0, 3))
RACE_DISTANCE = {'sprint': 'short', 'mile': 'mile', 'medium': 'middle', 'long': 'long'}
RACE_APTITUDE_TARGETS = {'turf': 'A', 'dirt': 'A', 'mile': 'A', 'middle': 'A', 'long': 'B'}
OPENING_REFERENCE_RANKS = {'103202': {'dirt': 'D', 'mile': 'B'}}


class AncestorLockError(ValueError):
    """The requested formal ancestor locks are invalid."""


def _roster_labels(aptitude):
    roster = json.loads(ROSTER.read_text('utf-8-sig'))
    labels = {}
    for record in roster['records']:
        variant = record['id']
        if variant not in aptitude or record['character_id'] != aptitude[variant]['character_id']:
            continue
        outfit = record.get('outfit')
        label = f"{record['name']} · {outfit}" if outfit else record['name']
        labels[variant] = VARIANT_NAMES.get(variant, label)
    return labels


def _reserved_characters(aptitude):
    return {aptitude[variant]['character_id'] for variant in (REFERENCE[0], REFERENCE[3], TARGET_VARIANT)}


def own_career_lock_options():
    aptitude = {row['variant_id']: row for row in AptitudeCatalog.load().rows}
    reserved = _reserved_characters(aptitude)
    return {'slots': [{'slot': SLOT_KEYS[i], 'label': SLOT_LABELS[i], 'factor_key': SLOT_FACTORS[i]}
                      for i in ANCESTOR_SLOTS],
            'variants': [{'variant_id': variant, 'label': label}
                         for variant, label in sorted(_roster_labels(aptitude).items())
                         if aptitude[variant]['character_id'] not in reserved]}


def _normalize_locks(value, aptitude, labels):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {SLOT_KEYS[i] for i in ANCESTOR_SLOTS}:
        raise AncestorLockError('只能锁定北黑侧和舞云侧的四个祖辈槽位。')
    reserved = _reserved_characters(aptitude)
    locked = {}
    by_character = {}
    for i in ANCESTOR_SLOTS:
        slot, label = SLOT_KEYS[i], SLOT_LABELS[i]
        if slot not in value:
            continue
        variant = value[slot]
        if not isinstance(variant, str) or variant not in labels:
            raise AncestorLockError(f'{label}请选择本地目录中的确切衣装。')
        character = aptitude[variant]['character_id']
        if character in reserved:
            raise AncestorLockError(f'{label}不能与固定亲代或本次战马使用同一角色。')
        if character in by_character:
            raise AncestorLockError(f'{by_character[character]}与{label}不能重复使用同一角色。')
        locked[slot] = variant
        by_character[character] = label
    return locked


def _positive_factors(node):
    return {row['factor_key'] for row in node.get('factors', [])
            if row.get('factor_key') and (row.get('stars') or 0) > 0}


def body_green_stars(node):
    """Read observed target greens, including exact names left unbound by scanning."""
    observed = {}
    for row in node.get('factors', []):
        stars = row.get('stars') or 0
        if row.get('kind') != 'white' or stars <= 0:
            continue
        key = row.get('factor_key')
        if key is None:
            key = GREEN_FACTOR_ALIASES.get(row.get('raw_name'))
        if key in GREEN_PASSIVES:
            observed[key] = max(stars, observed.get(key, 0))
    return observed


def body_scenario(node):
    """A known, observed own-body factor is positive evidence for this scenario."""
    return any((row.get('stars') or 0) > 0 and row.get('kind') == 'white'
               and (row.get('factor_key') == SCENARIO_FACTOR or
                    (row.get('factor_key') is None and row.get('raw_name') == '荣耀女神杯剧本'))
               for row in (node or {}).get('factors', []))


def _observed_entries(library, projected_entries, race_catalog):
    projected = {row['entry_id']: row for row in projected_entries}
    evidence = []
    for entry in library['entries']:
        node = entry.get('self') or {}
        identifier = entry['entry_id']
        summary = records_summary(node.get('race_records'), identifier, race_catalog)
        factors = _positive_factors(node)
        greens = body_green_stars(node)
        variants = {str(value) for value in (node.get('variant_id'), entry.get('variant_id')) if value}
        variant = next(iter(variants)) if len(variants) == 1 else ''
        if any(str(value) != variant[:-2] for value in (node.get('character_id'), entry.get('character_id')) if value):
            variant = ''
        confirmed_turns = set()
        undated_wins = 0
        for row in summary['wins']:
            if row['status'] != 'confirmed' or row['race'] is None:
                continue
            turn = (row['race'].get('career_time') or {}).get('global_turn')
            if turn is None:
                undated_wins += 1
            else:
                confirmed_turns.add(turn)
        evidence.append({
            'entry': entry, 'entry_id': identifier,
            'variant_id': variant,
            'availability': projected.get(identifier, {}).get('availability', 'unassigned'),
            'wins': len(confirmed_turns) if summary['known_win_count'] is not None else None,
            'unclassified_wins': summary['unclassified_count'], 'undated_wins': undated_wins,
            'win_coverage': summary['status'],
            'scenario_feature_confirmed': body_scenario(node),
            'core_white': [key for key in CORE_WHITE if key in factors],
            'green_passives': [key for key in GREEN_PASSIVES if key in greens],
            'red_stars': {key: red_node_summary(node, key)['known_stars'] for key in (MIDDLE, DIRT)},
        })
    return evidence


def _eligible(observed, variant_id, factor_key):
    return sorted((row for row in observed if row['variant_id'] == variant_id
                   and row['availability'] == 'owned' and row['scenario_feature_confirmed']
                   and row['wins'] is not None and row['wins'] >= MIN_WINS
                   and row['red_stars'][factor_key] == 3),
                  key=lambda row: (-len(row['core_white']), -green_dye_count(row['green_passives']),
                                   -row['red_stars'][factor_key], -row['wins'], row['entry_id']))


def green_dye_count(observed_factors):
    return len(GREEN_DYE_FACTORS.intersection(observed_factors))


def _bind_existing(choices):
    selected = [options[0] if options else None for options in choices]
    for parent in (0, 3):
        # A finished parent already has a fixed family. Only adopt it if both
        # of those grandparents are separately verified on this account.
        selected[parent] = None
        left, right = ({row['entry_id']: row for row in choices[index]}
                       for index in (parent + 1, parent + 2))
        for candidate in choices[parent]:
            snapshots = candidate['entry'].get('parents', [])
            for first, second in (snapshots, snapshots[::-1]) if len(snapshots) == 2 else ():
                a = _matching_snapshot(first, left)
                b = _matching_snapshot(second, right)
                if a and b and a['entry_id'] != b['entry_id']:
                    selected[parent:parent + 3] = [candidate, a, b]
                    break
            if selected[parent] is not None:
                break
    return selected


def _matching_snapshot(snapshot, available):
    snapshot = snapshot or {}
    selected = available.get(snapshot.get('entry_id'))
    if selected is None:
        return None
    variant = selected['variant_id']
    if (snapshot.get('variant_id') and str(snapshot['variant_id']) != variant or
            snapshot.get('character_id') and str(snapshot['character_id']) != variant[:-2]):
        return None
    return selected


def _race_fit(aptitudes, race_catalog, opening_reference):
    thresholds = {}
    adjusted = dict(aptitudes)
    for key, goal in {**RACE_APTITUDE_TARGETS, **opening_reference}.items():
        baseline = RANKS.index(aptitudes[key])
        maximum = min(RANKS.index('A'), baseline + 4)
        adjusted[key] = RANKS[max(baseline, min(maximum, RANKS.index(goal)))]
        thresholds[key] = minimum_stars_for_rank(aptitudes[key], adjusted[key])
    # One race per career turn. These are catalog opportunities, before
    # mandatory objectives, entry conditions, or the result of any race.
    native, prepared = set(), set()
    for race in race_catalog.by_id.values():
        distance = RACE_DISTANCE.get(race.get('distance_category'))
        surface = race.get('surface')
        turn = (race.get('career_time') or {}).get('global_turn')
        if race['grade'] != 'G1' or turn is None or distance is None or surface not in ('turf', 'dirt'):
            continue
        if min(RANKS.index(aptitudes[surface]), RANKS.index(aptitudes[distance])) >= RANKS.index('B'):
            native.add(turn)
        if min(RANKS.index(adjusted[surface]), RANKS.index(adjusted[distance])) >= RANKS.index('B'):
            prepared.add(turn)
    return {'native_g1_opportunity_turns': len(native), 'prepared_g1_opportunity_turns': len(prepared),
            'starting_red_stars': thresholds, 'starting_red_star_total': sum(thresholds.values()),
            'minimum_red_slots': sum(math.ceil(stars / 3) for stars in thresholds.values()),
            'starting_ranks_after_reference_red': {key: adjusted[key] for key in RACE_APTITUDE_TARGETS},
            'needs_later_inheritance': [key for key, goal in RACE_APTITUDE_TARGETS.items()
                                        if RANKS.index(adjusted[key]) < RANKS.index(goal)],
            'status': 'catalog_estimate_before_character_objectives'}


def _slot(index, variant, label, aptitude, fit, selected, locked):
    key = SLOT_FACTORS[index]
    return {'slot': SLOT_KEYS[index], 'slot_label': SLOT_LABELS[index], 'label': label, 'locked': locked,
            'character_id': aptitude['character_id'], 'variant_id': variant,
            'red_target': {'factor_key': key, 'stars': 3},
            'status': 'qualified' if selected else 'planned', 'entry_id': selected['entry_id'] if selected else None,
            'confirmed_wins': selected['wins'] if selected else None,
            'win_coverage': selected['win_coverage'] if selected else 'unknown',
            'scenario_feature_confirmed': bool(selected), 'observed_red_stars': selected['red_stars'][key] if selected else None,
            'core_white': selected['core_white'] if selected else [],
            'green_passives': selected['green_passives'] if selected else [],
            'native_aptitudes': aptitude['aptitudes'], 'race_fit': fit}


def _race_overlap(selected, race_catalog):
    rows = []
    for left, right in EDGES:
        a, b = selected[left], selected[right]
        if a and b:
            shared = common_wins(a['entry']['self']['race_records'], b['entry']['self']['race_records'],
                                 race_catalog, grades=['G1'], left_entry_id=a['entry_id'], right_entry_id=b['entry_id'])
            count = shared['confirmed_common_count']
            status = 'complete' if shared['complete'] else 'partial'
        else:
            count, status = None, 'unknown'
        rows.append({'left': SLOT_KEYS[left], 'right': SLOT_KEYS[right],
                     'confirmed_count': count, 'status': status})
    return rows


def _route(variants, observed, aptitude, labels, race_fit, compatibility, race_catalog, locked):
    choices = [_eligible(observed, variant, factor) for variant, factor in zip(variants, SLOT_FACTORS)]
    selected = _bind_existing(choices)
    slots = [_slot(i, variant, labels[variant], aptitude[variant], race_fit[variant], selected[i], SLOT_KEYS[i] in locked)
             for i, variant in enumerate(variants)]
    affinity = evaluate_lineage(TARGET_CHARACTER, [row['character_id'] for row in slots], compatibility)
    return {'route_id': 'kawasaki-' + '-'.join(variants[1:3] + variants[4:6]), 'slots': slots,
            'affinity': {key: affinity[key] for key in ('status', 'total', 'grade', 'detail')},
            'transformation_cost': {'starting_red_star_total': sum(race_fit[v]['starting_red_star_total'] for v in variants),
                                    'minimum_red_slots_total': sum(race_fit[v]['minimum_red_slots'] for v in variants)},
            'g1_opportunity_shortfall': sum(max(0, MIN_WINS - race_fit[v]['prepared_g1_opportunity_turns'])
                                            for v in variants),
            'common_g1': _race_overlap(selected, race_catalog), 'qualified_count': sum(row is not None for row in selected),
            'ready_to_start': all(row is not None for row in selected)}


def _rank(route):
    return (-route['qualified_count'], -sum(edge['confirmed_count'] or 0 for edge in route['common_g1']),
            -sum(len(slot['core_white']) for slot in route['slots']),
            -sum(green_dye_count(slot['green_passives']) for slot in route['slots']),
            route['g1_opportunity_shortfall'], route['transformation_cost']['starting_red_star_total'],
            -(route['affinity']['total'] or 0), route['route_id'])


def plan_own_career(library, *, projected_entries=(), locked_ancestors=None):
    """Plan formal own ancestry and separately list available upstream dye references."""
    aptitude = {row['variant_id']: row for row in AptitudeCatalog.load().rows}
    labels = _roster_labels(aptitude)
    locked = _normalize_locks(locked_ancestors, aptitude, labels)
    palettes = [(locked[SLOT_KEYS[i]],) if SLOT_KEYS[i] in locked else palette
                for i, palette in zip(ANCESTOR_SLOTS, ANCESTOR_PALETTES)]
    race_catalog = RaceCatalog.load()
    compatibility = CompatibilityCatalog.load()
    variants = {REFERENCE[0], REFERENCE[3]}
    variants.update(variant for palette in palettes for variant in palette)
    race_fit = {variant: _race_fit(aptitude[variant]['aptitudes'], race_catalog,
                                  OPENING_REFERENCE_RANKS.get(variant, {})) for variant in variants}
    observed = _observed_entries(library, projected_entries, race_catalog)
    routes = [_route((REFERENCE[0], a1, a2, REFERENCE[3], b1, b2), observed, aptitude, labels,
                     race_fit, compatibility, race_catalog, locked)
              for a1, a2, b1, b2 in product(*palettes)
              if len({aptitude[variant]['character_id'] for variant in (a1, a2, b1, b2)}) == 4]
    routes.sort(key=_rank)
    reference = max(routes, default=None, key=lambda route: sum(
        slot['variant_id'] == expected for slot, expected in zip(route['slots'], REFERENCE)))
    reference_variants = [slot['variant_id'] for slot in reference['slots']] if reference else []
    best_by_palette = {}
    for route in routes:
        key = frozenset(row['variant_id'] for row in route['slots'])
        best_by_palette.setdefault(key, route)
    proposed = list(best_by_palette.values())[:3]
    for route in proposed:
        route['reference_differences'] = [
            {'slot': slot['slot'], 'local_variant_id': slot['variant_id'], 'reference_variant_id': expected}
            for slot, expected in zip(route['slots'], reference_variants) if slot['variant_id'] != expected
        ]
        route['reference_slot_matches'] = len(SLOT_KEYS) - len(route['reference_differences'])
    dyes = [row for row in observed if row['availability'] == 'borrowed'
            and (row['core_white'] or row['green_passives'] or any(row['red_stars'].values()))]
    dyes.sort(key=lambda row: (-len(row['core_white']), -green_dye_count(row['green_passives']), row['entry_id']))
    target = aptitude[TARGET_VARIANT]
    return {'template_id': TEMPLATE_ID, 'label': '川崎 2100 米 LoH · 领跑 · 女神杯种马',
            'locked_ancestors': locked, 'status': 'ok' if proposed else 'no_compatible_route',
            'message': None if proposed else '按当前锁定条件，默认候选无法补齐四个不同祖辈。请再指定一个祖辈或调整锁定。',
            'target': {'character_id': TARGET_CHARACTER, 'variant_id': TARGET_VARIANT, 'label': '小林历奇',
                       'dirt_rank': target['aptitudes']['dirt'], 'middle_rank': target['aptitudes']['middle'],
                       'middle_s_requires_inheritance': True},
            'qualification': {'observed_entries': len(observed),
                              'confirmed_win_records': sum(row['wins'] is not None for row in observed),
                              'unclassified_wins': sum(row['unclassified_wins'] for row in observed),
                              'undated_wins': sum(row['undated_wins'] for row in observed),
                              'minimum_confirmed_wins': MIN_WINS, 'target_wins': TARGET_WINS,
                              'scenario_factor': SCENARIO_FACTOR, 'formal_ownership': 'owned_only',
                              'required_red_stars': 3, 'borrow_use': 'upstream_dye_only'},
            'factor_targets': {'core_white': list(CORE_WHITE), 'green_passives': list(GREEN_PASSIVES),
                               'green_active_target': GREEN_ACTIVE_TARGET,
                               'green_skills': [dict(skill) for skill in GREEN_SKILLS],
                               'medium_slots': SLOT_FACTORS.count(MIDDLE), 'dirt_slots': SLOT_FACTORS.count(DIRT)},
            'training_order': ['用自有材料或可借素材染色', '培养并验收四匹自有祖辈',
                               '用对应祖辈培养并验收北黑和舞云', '正式战马使用两匹已验收亲代'],
            'routes': proposed, 'ready_to_start': any(row['ready_to_start'] for row in proposed),
            'reference': {'label': '按锁定条件调整的范本' if locked else '人工范本',
                          'route_id': reference['route_id'] if reference else None,
                          'slots': reference['slots'] if reference else [],
                          'affinity': reference['affinity'] if reference else None,
                          'transformation_cost': reference['transformation_cost'] if reference else None,
                          'top_route_slot_matches': proposed[0]['reference_slot_matches'] if proposed else None},
            'dye_references': [{key: row[key] for key in ('entry_id', 'variant_id', 'core_white',
                                'green_passives', 'red_stars')} for row in dyes[:12]],
            'limits': ['正式槽位只使用已核验自有种马；待培养角色的持有情况和未来因子需在游戏内确认。',
                       '赛程适性来自本地参考目录；同回合只计一次机会，角色目标与实际胜场另核。胜场资格只计可对上赛事窗口的本体记录。',
                       '基础相性与各边已确认 G1 交集分别展示。相同赛事跨年胜场可累计，但共同 G1 按赛事键去重。',
                       '有大师杯剧本本体因子是正向证据；没扫到时按来源待核，不判断成其他剧本。']}


_GENERAL_BLOCKED_APTITUDES = ('unreachable_at_start', 'slot_capacity_exceeded', 'unknown_baseline')
_GENERAL_ANTI_A_CONFLICTS = ('polluted', 'planned_polluted', 'already_a_at_baseline', 'unknown_baseline')
_GENERAL_PLANNING_ORDER = (0, 3, *ANCESTOR_SLOTS)


def _general_result(status, note, blueprints=()):
    return {'status': status, 'note': note, 'blueprints': list(blueprints)}


def _general_candidate_outfits(characters, aptitude_catalog):
    profiles = {row['variant_id']: row for row in aptitude_catalog.rows} if aptitude_catalog is not None else {}
    for character in characters:
        identifier = character['character_id']
        variants = [outfit['variant_id'] for outfit in character.get('outfits', [])
                    if str(outfit['variant_id']).isascii() and str(outfit['variant_id']).isdigit()]
        for variant in variants or (None,):
            profile = profiles.get(variant)
            aptitudes = profile['aptitudes'] if profile and profile['character_id'] == identifier else {}
            yield identifier, variant, aptitudes


def start_capped_route(route):
    """Return (start_route, s_goal_keys) capping 'S' targets at opening 'A'."""
    normalized = normalize_local_route(route) or normalize_local_route({})
    goals = normalized.get('aptitude_goals') or []
    s_keys = [goal['factor_key'] for goal in goals if goal.get('target_rank') == 'S']
    if not s_keys and normalized['aptitude_goal'].get('target_rank') != 'S':
        return normalized, ()
    capped_goals = [{**goal, 'target_rank': 'A'} if goal.get('target_rank') == 'S' else dict(goal)
                    for goal in goals]
    capped_primary = ({**normalized['aptitude_goal'], 'target_rank': 'A'}
                      if normalized['aptitude_goal'].get('target_rank') == 'S'
                      else dict(normalized['aptitude_goal']))
    return ({**normalized, 'aptitude_goal': capped_primary, 'aptitude_goals': capped_goals},
            tuple(dict.fromkeys(s_keys)))


def _general_filler(items, totals, avoid_keys, groups, target, variant, aptitude_catalog,
                    s_goal_keys=(), reuse_keys=()):
    for item in items:
        key = item['factor_key']
        threshold = minimum_stars_for_rank(item['baseline']['rank'], 'A')
        if key not in avoid_keys and totals.get(key, 0) < (18 if threshold is None else threshold):
            return key
    s_candidates = [key for key in s_goal_keys
                    if key in FACTOR_KEYS and key not in avoid_keys and totals.get(key, 0) < 18]
    if s_candidates:
        return min(s_candidates, key=lambda key: totals.get(key, 0))
    for group in groups:
        key = group['requirement'].get('factor_key')
        if key in FACTOR_KEYS and key not in avoid_keys:
            return key
    available = [key for key in FACTOR_KEYS if key not in avoid_keys]
    reuse_candidates = [key for key in available if key in reuse_keys and totals.get(key, 0) < 18]
    if reuse_candidates:
        if aptitude_catalog is not None:
            safe_a = [key for key in reuse_candidates
                      if aptitude_catalog.baseline(target, variant_id=variant, factor_key=key)['rank'] == 'A']
            if safe_a:
                return min(safe_a, key=lambda key: totals.get(key, 0))
        return min(reuse_candidates, key=lambda key: totals.get(key, 0))
    if aptitude_catalog is not None:
        for key in available:
            if aptitude_catalog.baseline(target, variant_id=variant, factor_key=key)['rank'] == 'A':
                return key
    return available[0] if available else None


def _general_owned_ancestors(library, projected, characters, avoid_keys):
    eligible = {
        row['entry_id']: row['availability']
        for row in projected
        if row['eligible'] and row['availability'] in ('owned', 'reference', 'unassigned_reference')
    }
    rank_order = {'owned': 0, 'unassigned_reference': 1, 'reference': 2}
    entries_by_id = {entry['entry_id']: entry for entry in library['entries']}
    reusable = {}
    for entry_id in sorted(eligible, key=lambda eid: (rank_order.get(eligible[eid], 3), eid)):
        entry = entries_by_id.get(entry_id)
        if entry is None:
            continue
        own = entry['self']
        red = detected_red_summary(own)
        key = red['factor_key']
        if red['stars'] != 3 or key not in FACTOR_KEYS or key in avoid_keys:
            continue
        if avoid_keys and any(red['unknown'] or red['factor_key'] in avoid_keys
                              for red in map(detected_red_summary, entry['parents'])):
            continue
        identity = {field: own.get(field) or entry.get(field) for field in ('character_id', 'character_name')}
        identifier, _ = resolve_node_identity(identity, characters)
        if identifier is not None:
            reusable.setdefault((key, identifier), []).append(entry_id)
    return reusable


def _general_steps(nodes, identifiers, characters):
    names = {row['character_id']: row['name'] for row in characters}
    return [
        f"{'培养' if spec['kind'] == 'planned' else '复用'}{NODE_LABELS[index]} · {names.get(identifiers[index], identifiers[index])}"
        + (f" · {spec['red_target']['factor_key'].split(':', 1)[1]}3★" if spec.get('red_target') else '')
        for index in (*ANCESTOR_SLOTS, 0, 3)
        if (spec := nodes[index]) is not None
    ]


def plan_general_lineage(
    target_character_id, route, evaluate, *, library, projected, pool_candidates=(),
    compatibility, characters, aptitude_catalog, groups=(), respect_locked_slots=False, limit=3,
):
    """Assign a six-slot red plan and choose distinct characters by bounded beam search."""
    route, s_goal_keys = start_capped_route(route)
    preflight = evaluate({**route, 'nodes': [None] * 6})['aptitude']
    if preflight['status'] in _GENERAL_BLOCKED_APTITUDES:
        return _general_result(preflight['status'], preflight['plan']['message'])
    if preflight['anti_a']['status'] in _GENERAL_ANTI_A_CONFLICTS:
        return _general_result(preflight['anti_a']['status'], '请先核对禁升 A 项及其先天适性。')

    nodes = route['nodes'] if respect_locked_slots else [None] * 6
    locked = {
        index for index, spec in enumerate(nodes)
        if spec and (spec['kind'] in ('entry', 'external') or spec.get('character_id') is not None)
    }
    for index in (0, 3):
        if index in locked and nodes[index]['kind'] in ('entry', 'external'):
            locked.update((index + 1, index + 2))
    nodes = [spec if index in locked else None for index, spec in enumerate(nodes)]
    open_slots = [index for index in range(6) if index not in locked]
    red_open_slots = [
        index for index in range(6)
        if index in open_slots or (nodes[index] and nodes[index]['kind'] == 'planned'
                                   and not (nodes[index].get('red_target') or {}).get('factor_key'))
    ]
    locked_route = {**route, 'nodes': nodes}
    assumed = {index for index, spec in enumerate(nodes) if spec and spec['kind'] == 'external'}
    locked_review = evaluate(locked_route, assume_external_slot=assumed) if assumed else evaluate(locked_route)
    avoid_keys = {item['factor_key'] for item in route['avoid_a_factors']}
    if any(spec and (spec.get('red_target') or {}).get('factor_key') in avoid_keys for spec in nodes):
        return _general_result('planned_polluted', '已锁定的待培养槽使用了禁升 A 因子，请调整该槽。')
    locked_aptitude = locked_review['aptitude']
    if locked_aptitude['anti_a']['status'] in _GENERAL_ANTI_A_CONFLICTS:
        return _general_result(locked_aptitude['anti_a']['status'], '已锁定的素材与禁升 A 保护冲突。')
    if locked_review['selection_issues']:
        status = locked_aptitude['status']
        return _general_result(status if status in _GENERAL_BLOCKED_APTITUDES else 'locked_slot_conflict',
                               ' '.join(locked_review['selection_issues']))

    identifiers = [slot['character_id'] for slot in locked_review['route_nodes']]
    known_ids = [identifiers[index] for index in locked]
    if (None in known_ids or target_character_id in known_ids or len(set(known_ids)) != len(known_ids)
            or any(nodes[index] and nodes[index]['kind'] != 'planned'
                   and not locked_review['route_nodes'][index]['usable'] for index in locked)):
        return _general_result('locked_slot_conflict', '请核对锁定槽位的角色、祖辈及可用状态。')

    totals = {}
    red_assignments = []
    items = locked_aptitude['items']
    for item in items:
        key, plan = item['factor_key'], item['plan']
        planned = sum(target['stars'] for spec in nodes if spec and (target := spec.get('red_target'))
                      and target['factor_key'] == key)
        totals[key] = (plan['known_stars'] or 0) + planned
        needed = math.ceil(max(0, plan['required_stars'] - totals[key]) / 3)
        red_assignments.extend([key] * needed)
        totals[key] += 3 * needed
    reuse = _general_owned_ancestors(library, projected, characters, avoid_keys)
    reuse_keys = {key for key, _ in reuse}
    if len(red_assignments) > len(red_open_slots):
        return _general_result('slot_capacity_exceeded', '锁定槽位保留后，剩余空槽不足以分配各项目标红星。')
    while len(red_assignments) < len(red_open_slots):
        key = _general_filler(items, totals, avoid_keys, groups, target_character_id,
                              route['target_variant_id'], aptitude_catalog,
                              s_goal_keys=s_goal_keys, reuse_keys=reuse_keys)
        if key is None:
            return _general_result('no_safe_red_factor', '所有红因子均被保护，无法为剩余槽位选择安全因子。')
        red_assignments.append(key)
        totals[key] = totals.get(key, 0) + 3

    assignments = dict(zip(red_open_slots, red_assignments))
    nodes = [
        {**spec, 'red_target': {'factor_key': assignments[index], 'stars': 3}}
        if index in locked and spec and spec['kind'] == 'planned' and index in assignments else spec
        for index, spec in enumerate(nodes)
    ]
    outfits = tuple(_general_candidate_outfits(characters, aptitude_catalog))
    by_factor = {}
    for key in set(red_assignments):
        attribute = FACTOR_KEYS[key]
        best = {}
        for identifier, variant, aptitudes in outfits:
            if identifier == target_character_id or identifier in known_ids:
                continue
            rank = RANKS.index(aptitudes[attribute]) if attribute in aptitudes else -1
            suitability = 32 * rank - 2 * sum(value == 'A' for value in aptitudes.values())
            current = best.get(identifier)
            if current is None or (-suitability, variant or '') < (-current[1], current[0] or ''):
                best[identifier] = (variant, suitability)
        by_factor[key] = [(identifier, variant, value) for identifier, (variant, value) in best.items()]

    def score(assignment):
        # Use the same seven completed components as evaluate_lineage while
        # scoring partial rows. Metadata and labels are built only for delivery.
        lineage = [target_character_id, *assignment]
        total = 0
        for _, _, indexes in COMPONENTS:
            values = [lineage[index] for index in indexes]
            if all(value in compatibility.character_ids for value in values):
                total += compatibility.score(values)
        return total

    beam = [(identifiers, nodes, 0, 0)]
    width = max(36, 6 * limit)
    for index in _GENERAL_PLANNING_ORDER:
        if index not in open_slots:
            continue
        key = assignments[index]

        def continuations():
            for current_ids, current_nodes, strength, reused in beam:
                for identifier, variant, suitability in by_factor[key]:
                    if identifier in current_ids:
                        continue
                    next_ids = current_ids.copy()
                    next_ids[index] = identifier
                    spec = {'kind': 'planned', 'character_id': identifier, 'variant_id': variant,
                            'red_target': {'factor_key': key, 'stars': 3}}
                    matches = reuse.get((key, identifier), ()) if index in ANCESTOR_SLOTS else ()
                    yield (next_ids, [*current_nodes[:index], spec, *current_nodes[index + 1:]],
                           strength + suitability, reused)
                    if matches:
                        existing = {'kind': 'entry', 'entry_id': matches[0]}
                        yield (next_ids, [*current_nodes[:index], existing, *current_nodes[index + 1:]],
                               strength + suitability, reused + 1)

        beam = nsmallest(width, continuations(),
                         key=lambda row: (-row[3], -(row[2] + score(row[0])), tuple(value or 0 for value in row[0])))

    blueprints = []
    for identifiers, specs, _, reused in beam:
        draft = {**route, 'nodes': specs}
        local = evaluate(draft)
        conditional = evaluate(draft, assume_external_slot=assumed) if assumed else local
        aptitude = conditional['aptitude']
        if (local['selection_issues'] or conditional['selection_issues']
                or aptitude['status'] in _GENERAL_BLOCKED_APTITUDES
                or aptitude['anti_a']['status'] in _GENERAL_ANTI_A_CONFLICTS):
            continue
        if any(item['plan']['required_stars'] > (item['plan']['known_stars'] or 0) +
               sum(red['stars'] for spec in specs if spec and (red := spec.get('red_target'))
                   and red['factor_key'] == item['factor_key']) for item in aptitude['items']):
            continue
        blueprints.append({'nodes': local['local_route']['nodes'],
                           'label': f'六槽红因子规划 {len(blueprints) + 1}',
                           'steps': _general_steps(specs, identifiers, characters),
                           'reused_ancestor_count': reused})
        if len(blueprints) == limit:
            break
    if not blueprints:
        return _general_result('no_compatible_route', '未找到身份互异且满足当前红因子目标的六槽方案。')
    note = ('已按开局 A 门槛并优先叠加 S 目标红星分配六槽；育成中需通过继承事件突破 S。' if s_goal_keys
            else '按目标星数分配六槽，先准备祖辈，再培养两位亲代。')
    return _general_result('ok', note, blueprints)
