"""Purely local route and actual-win editing behind the planning API envelope."""
from copy import deepcopy
import json
from pathlib import Path
import unicodedata

from core import parent_library as store
from core import parent_planning_state as planning
from core.parent_compatibility import CompatibilityCatalog, characters_from_roster
from core.parent_local_route import RED_LABELS, evaluate_local_route
from core.parent_aptitude import AptitudeCatalog, RANKS, START_RANKS
from core.parent_race_records import RaceCatalog, records_summary


ROOT = Path(__file__).resolve().parents[1]
_ROUTE_FIELDS = {'target_character_id', 'library_id', 'account_id', 'include_unassigned', 'local_route'}
_EXTERNAL_FIELDS = _ROUTE_FIELDS | {'slot_index', 'targets', 'raw_targets'}
_AUTO_PAIR_FIELDS = {'targets', 'raw_targets', 'include_mali_pool', 'respect_locked_slots', 'limit'}
_SCHEDULE_FIELDS = {'slot_index', 'max_races'}
_POOL_MODES = ('pool_list', 'pool_save', 'pool_remove', 'pool_clear')
FIELDS = {
    'parent_planning_route_catalog': set(),
    'parent_planning_route_evaluate': _ROUTE_FIELDS | _AUTO_PAIR_FIELDS | _SCHEDULE_FIELDS,
    'parent_planning_external_query': _EXTERNAL_FIELDS,
    'parent_planning_external_compare': _EXTERNAL_FIELDS | {'candidates', 'limit', 'entry_id', 'save_to_pool', 'use_pool'},
    'parent_planning_race_catalog': {'query', 'limit'},
    'parent_planning_races_get': {'entry_id', 'node'},
    'parent_planning_races_save': {'entry_id', 'node', 'race_records', 'expected_revision'},
}
VENUES = {'tokyo': '东京', 'nakayama': '中山', 'kyoto': '京都', 'hanshin': '阪神',
    'chukyo': '中京', 'sapporo': '札幌', 'hakodate': '函馆', 'fukushima': '福岛',
    'niigata': '新潟', 'kokura': '小仓', 'oi': '大井', 'ooi': '大井',
    'kawasaki': '川崎', 'funabashi': '船桥', 'morioka': '盛冈', 'longchamp': '隆尚'}


def _success(value):
    return {'ok': True, 'results': [value]}


def _error(code, message):
    return {'ok': False, 'code': code, 'message': message,
        'results': [{'code': code, 'message': message}]}


def _node(entry, name):
    if name == 'self':
        return entry['self']
    if name in ('parent1', 'parent2'):
        return entry['parents'][int(name[-1]) - 1]
    raise ValueError('胜场节点请选择本体、亲代 1 或亲代 2。')


def _race_result(library, entry_id, name, catalog):
    entry = store.get_entry(library, entry_id)
    node = _node(entry, name)
    identifier = entry_id if name == 'self' else (node or {}).get('entry_id')
    records = deepcopy((node or {}).get('race_records'))
    summary = (records_summary(records, identifier, catalog) if identifier is not None else
        {'entry_id': None, 'coverage': 'unknown', 'status': 'unknown',
         'known_win_count': None, 'wins': [], 'complete': False})
    return {'entry_id': entry_id, 'node': name, 'race_records': records,
        'library_revision': library['revision'], 'library_id': library['library_id'],
        'node_identity': {'entry_id': identifier,
            **{key: ((node or {}).get(key) or (entry.get(key) if name == 'self' else None))
               for key in ('character_id', 'character_name', 'variant_id', 'variant_name')}},
        'race_summary': summary, 'source': deepcopy(catalog.source)}


def _display_race(row):
    row = deepcopy(row)
    row['raw_name'] = row.get('source_name') or ''
    if row.get('name_cn'):
        row['display_name'] = row['name_cn']
    else:
        place = VENUES.get(row.get('venue'), row.get('venue') or '场地未确认')
        surface = {'turf': '草地', 'dirt': '泥地'}.get(row.get('surface'), '场地类型未确认')
        date = row.get('career_time') or {}
        stage = {'junior': '初级', 'classic': '经典', 'senior': '资深', 'final': '决赛'}.get(date.get('stage'), '')
        month = stage + str(date.get('month', ''))
        half = {'first': '上半月', 'second': '下半月'}.get(date.get('half'), '')
        # Descriptive known fields, never an invented translation of a race.
        row['display_name'] = (place + ' · ' + surface + ' ' + str(row.get('distance_m') or '') +
            'm · ' + str(row.get('grade') or '') + ' · ' + month + '月' + half)
        row['name_status'] = 'cn_race_name_unverified'
    return row


def _search_races(catalog, query, limit, library):
    if not isinstance(query, str):
        raise ValueError('赛事查询应为文字。')
    aliases = {**{value: key for key, value in VENUES.items()},
        '泥地': 'dirt', '草地': 'turf', '英里': 'mile', '短距离': 'short',
        '中距离': 'middle', '长距离': 'long'}
    translated = query
    for text, token in aliases.items():
        translated = translated.replace(text, token)
    # Search descriptors as well as source names. The old calendar helper has
    # no distance_category in its text index, so e.g. 英里 must not disappear.
    result = catalog.search('', limit)  # Keeps the existing query/limit guards.
    if len(query) > 240:
        raise ValueError('赛事查询文字过长。')
    def normal(value):
        return ''.join(unicodedata.normalize('NFKC', str(value)).split()).casefold()
    terms = [normal(term) for term in translated.split()]
    matches = []
    for original in catalog.search('', 500)['races']:
        row = _display_race(original)
        haystack = normal(' '.join(str(row.get(key) or '') for key in
            ('race_id', 'event_key', 'name_cn', 'source_name', 'grade', 'surface',
             'distance_m', 'distance_category', 'venue', 'display_name')))
        if all(term in haystack for term in terms):
            matches.append(row)
    result.update(races=matches[:limit], total=len(matches))
    # An explicitly typed local name can find a known ID in the user's own
    # observations. It does not rename the shared calendar or another horse.
    matching_ids = set()
    if query.strip():
        for entry in library['entries']:
            for node in (entry['self'], *entry['parents']):
                for win in ((node or {}).get('race_records') or {}).get('wins', []):
                    if query.casefold() in win['raw_name'].casefold():
                        matching_ids.add(win['race_id'])
    known = {row['race_id'] for row in result['races']}
    extra = [row for identifier in sorted(matching_ids - known)
             if (row := catalog.get(identifier)) is not None]
    result['races'] = [_display_race(row) for row in [*result['races'], *extra][:limit]]
    result['matched_local_observation_ids'] = sorted(matching_ids)
    return result


def _route_groups(request, planner_catalog_path):
    from core.parent_pair_coverage import goal_groups
    from core.parent_planner_catalog import ParentPlannerCatalog

    targets, raw = request.get('targets', []), request.get('raw_targets', [])
    if not isinstance(targets, list) or not isinstance(raw, list):
        raise ValueError('因子目标清单格式无效。')
    if not (targets or raw):
        return []
    catalog = ParentPlannerCatalog.load(planner_catalog_path or ROOT/'data/parent_library/planner_catalog.json')
    return goal_groups({'targets': targets, 'raw_targets': raw}, catalog)


def _pool_operation(request, root, library):
    from core.parent_mali_import import clear_mali_pool, load_mali_pool, remove_mali_pool_item, save_mali_pool_items

    result = {'library_id': library['library_id'] if store.store_path(root).is_file() else None,
              'library_revision': library['revision']}
    match request.get('mode'):
        case 'pool_save':
            pool, result['added_count'] = save_mali_pool_items(request.get('candidates'), root=root)
        case 'pool_remove':
            pool, result['removed'] = remove_mali_pool_item(request.get('entry_id'), root=root)
        case 'pool_clear':
            pool = clear_mali_pool(root=root)
        case _:
            pool = load_mali_pool(root)
    return _success({**result, 'pool': pool, 'pool_count': len(pool)})


def dispatch_route(request, *, root=None, account_book_path=None, relations_path=None,
                   roster_path=None, race_catalog_path=None, aptitude_catalog_path=None,
                   planner_catalog_path=None):
    if (not isinstance(request, dict) or request.get('action') not in FIELDS or
            set(request) - FIELDS[request['action']] - {'action', 'request_id', 'mode', 'endpoint'}):
        return _error('parent_planning_invalid_request', '本地路线请求字段不正确，不接受文件路径。')
    action = request['action']
    mode = request.get('mode')
    auto_pair = action == 'parent_planning_route_evaluate' and mode == 'auto_pair'
    race_schedule = action == 'parent_planning_route_evaluate' and mode == 'race_schedule'
    if action == 'parent_planning_route_evaluate':
        if not auto_pair and _AUTO_PAIR_FIELDS.intersection(request):
            return _error('parent_planning_invalid_request', '自动配对参数只能用于自动配对模式。')
        if not race_schedule and _SCHEDULE_FIELDS.intersection(request):
            return _error('parent_planning_invalid_request', '赛程参数只能用于赛程规划模式。')
    try:
        library = store.load_library(root=root)
        if action == 'parent_planning_external_compare' and mode in _POOL_MODES:
            return _pool_operation(request, root, library)
        race_catalog = RaceCatalog.load(race_catalog_path)
        if action == 'parent_planning_race_catalog':
            return _success(_search_races(race_catalog, request.get('query', ''),
                request.get('limit', 100), library))
        if action in {'parent_planning_races_get', 'parent_planning_races_save'}:
            entry_id, node = request.get('entry_id'), request.get('node')
            if action == 'parent_planning_races_save':
                revision = request.get('expected_revision')
                if type(revision) is not int or revision < 0:
                    raise ValueError('保存实际胜场前请先读取当前种马库版本。')
                if 'race_records' not in request:
                    raise ValueError('请提供胜场清单；清除记录需明确提交空值。')
                updated = store.set_race_records(library, entry_id, node, request['race_records'])
                library = store.save_library(updated, root=root, expected_revision=revision)
            return _success(_race_result(library, entry_id, node, race_catalog))

        compatibility = CompatibilityCatalog.load(relations_path or ROOT/'data/parent_library/compatibility_relations.json')
        roster = json.loads(Path(roster_path or ROOT/'core/autonomous_training/plant_roster.json').read_text('utf-8-sig'))
        characters = characters_from_roster(roster, compatibility)
        aptitude_catalog = AptitudeCatalog.load(aptitude_catalog_path)
        if action == 'parent_planning_route_catalog':
            return _success({'characters': characters,
                'aptitude_factors': [{'factor_key': 'red:' + name, 'label': name} for name in RED_LABELS],
                'ranks': list(RANKS), 'start_ranks': list(START_RANKS),
                'aptitude_profiles': deepcopy(aptitude_catalog.rows),
                'race_catalog': {'source': deepcopy(race_catalog.source), 'status': 'reference'},
                'aptitude_source': deepcopy(aptitude_catalog.source),
                'library_revision': library['revision'],
                'library_id': library['library_id'] if store.store_path(root).is_file() else None})
        selected_library = request.get('library_id')
        if selected_library is not None and selected_library != library['library_id']:
            raise ValueError('这份方案来自另一份种马库；请重新关联当前成品，未按相同名字替换。')
        from desktop.parent_planning_api import _account_catalog
        state = planning.load_state(root)
        accounts = _account_catalog(state, account_book_path or ROOT/'runs/mfa-accounts.json')
        account_id = request.get('account_id')
        if account_id is not None and account_id not in {row['id'] for row in accounts}:
            raise ValueError('当前规划账号已不存在，请刷新账号列表。')
        include = request.get('include_unassigned', False)
        if type(include) is not bool:
            raise ValueError('是否包含未关联参考应为开关。')
        projected = planning.project_entries(state, library['library_id'], library['entries'],
            account_id=account_id, include_unassigned=include)
        target = request.get('target_character_id')

        def evaluate(route, *, assume_external_slot=None, max_races=14):
            return evaluate_local_route(library, projected, target, route,
                race_catalog=race_catalog, aptitude_catalog=aptitude_catalog,
                characters=characters, assume_external_slot=assume_external_slot,
                compatibility=compatibility, max_races=max_races)

        if race_schedule:
            max_races = request.get('max_races', 14)
            if type(max_races) is not int or not 1 <= max_races <= 24:
                raise ValueError('赛程场数上限应为 1～24 的整数。')
            slot = request.get('slot_index')
            if 'slot_index' in request and (type(slot) is not int or not 0 <= slot <= 5):
                raise ValueError('请选择六个继承槽位中的一个。')
            review = evaluate(request.get('local_route'), max_races=max_races)
            if 'slot_index' not in request:
                result = {'mode': 'race_schedule',
                          'schedules': [task['recommended_schedule'] for task in review['tasks']]}
            else:
                task = next((task for task in review['tasks'] if task['slot_index'] == slot), None)
                if task is None:
                    raise ValueError('仅可为待培养槽位生成赛程。')
                result = {'mode': 'race_schedule', 'slot_index': slot,
                          'node_key': task['node_key'], 'recommended_schedule': task['recommended_schedule']}
        elif auto_pair:
            from core.parent_external_route import auto_pair_routes
            from core.parent_mali_import import load_mali_pool

            include_pool = request.get('include_mali_pool', True)
            respect_locked = request.get('respect_locked_slots', False)
            if type(include_pool) is not bool or type(respect_locked) is not bool:
                raise ValueError('是否纳入吗哩吗哩候选池和保留锁定槽位应为开关。')
            groups = _route_groups(request, planner_catalog_path)
            result = auto_pair_routes(target, request.get('local_route'), evaluate,
                library=library, projected=projected, compatibility=compatibility, characters=characters,
                aptitude_catalog=aptitude_catalog, race_catalog=race_catalog, groups=groups,
                pool_items=load_mali_pool(root) if include_pool else (),
                include_mali_pool=include_pool, respect_locked_slots=respect_locked, limit=request.get('limit', 3))
        elif action in ('parent_planning_external_query', 'parent_planning_external_compare'):
            from core.parent_external_route import prepare_external_route, external_compare, external_query

            slot_index = request.get('slot_index')
            route = prepare_external_route(request.get('local_route'), slot_index)
            groups = _route_groups(request, planner_catalog_path)
            if action == 'parent_planning_external_query':
                result = external_query(route, slot_index, evaluate(route), library, groups)
            else:
                from core.parent_mali_import import load_mali_pool, save_mali_pool_items

                save, use = request.get('save_to_pool', False), request.get('use_pool', False)
                if type(save) is not bool or type(use) is not bool:
                    raise ValueError('是否保存或使用吗哩吗哩候选池应为开关。')
                candidates = request.get('candidates')
                has_candidates = candidates.strip() if isinstance(candidates, str) else candidates
                if save and has_candidates:
                    pool, _ = save_mali_pool_items(candidates, root=root)
                else:
                    pool = load_mali_pool(root)
                result = external_compare(route, slot_index, candidates, request.get('limit', 5), evaluate,
                    target=target, compatibility=compatibility, characters=characters, library=library,
                    groups=groups, pool_items=pool, use_pool=use)
                result['pool_count'] = len(pool)
        else:
            result = evaluate(request.get('local_route'))
        result.update(library_id=library['library_id'] if store.store_path(root).is_file() else None,
            library_revision=library['revision'], account_id=account_id)
        return _success(result)
    except store.LibraryConflict:
        return _error('parent_library_revision_conflict', '种马库已被另一窗口修改；请重新读取，未自动覆盖胜场。')
    except OSError:
        return _error('parent_planning_storage_unavailable', '本地种马资料暂不可读写，请检查目录。')
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        message = str(error) if isinstance(error, ValueError) and any('\u4e00' <= c <= '\u9fff' for c in str(error)) else '本地路线资料或参数不正确，请刷新后检查。'
        return _error('parent_planning_invalid_data', message)
