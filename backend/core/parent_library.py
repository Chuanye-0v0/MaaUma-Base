"""Shared, local finished-horse library. Pure data; no game or network access.

An entry identifies one finished horse, never a character/portrait. Observed
factors belong to exactly one node; lineage totals never alter self factors.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import uuid
SCHEMA = 'uma.parent_library.v1'
SNAPSHOT_SCHEMA = 'uma.parent_library.planner.v1'
KINDS = ('blue', 'red', 'white', 'green')
LABELS = {'known_satisfied': '已知满足', 'partial_material': '部分素材', 'insufficient_data': '资料不足'}
IDENTITY = ('character_id', 'character_name', 'variant_id', 'variant_name', 'nickname', 'trained_on')
FACTOR_FIELDS = ('kind', 'factor_key', 'raw_name', 'stars')

class LibraryConflict(ValueError):
    """The caller must reload rather than overwrite another panel's edits."""

def _text(value, *, optional=False, limit=240):
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > limit or (not optional and (not value.strip())):
        raise ValueError('种马库文字字段无效。')
    return value.strip() or None

def _id(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]{1,96}', value):
        raise ValueError('种马库或成品编号无效。')
    return value

def _fields(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(label + '字段无效。')

def _kind(value):
    value = 'red' if value == 'pink' else value
    if value not in KINDS:
        raise ValueError('因子颜色请选择蓝、红、白或绿。')
    return value

def _key(value, kind=None):
    if value is None or value == '':
        return None
    value = _text(value)
    prefix, separator, name = value.partition(':')
    prefix = _kind(prefix)
    if not separator or not name or (kind is not None and prefix != kind):
        raise ValueError('标准因子编号与颜色不一致。')
    return prefix + ':' + name

def catalog_choices(path):
    """Optional caller-owned vocabulary; unknown OCR names remain valid data."""
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    result = {}
    for row in data.get('factors', []):
        key = _key(row['key'])
        result[key] = {'factor_key': key, 'kind': key.split(':')[0], 'name': row['name']}
        if row.get('category') == 'scenario' and row.get('is_scenario') is True:
            result[key].update(category='scenario', is_scenario=True)
    return list(result.values())

def catalog_scenario_names(path):
    """Exact display classifications, including caller-provided reference names.

    A display name never creates a canonical factor key for an OCR observation.
    """
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    names = data.get('scenario_names', [])
    if not isinstance(names, list):
        raise ValueError('剧本因子展示目录无效。')
    return list(dict.fromkeys((_text(name) for name in names)))

def _screenshot_path(value, *, optional=True):
    screenshot = _text(value, optional=optional, limit=512)
    if screenshot:
        posix = PurePosixPath(screenshot.replace('\\', '/'))
        windows = PureWindowsPath(screenshot)
        if windows.drive or windows.root or posix.is_absolute() or ('..' in posix.parts) or (':' in screenshot):
            raise ValueError('截图来源必须使用库内相对路径。')
        screenshot = str(posix)
    return screenshot

def _source(value):
    _fields(value, ('page', 'screenshot'), '来源')
    page = value.get('page')
    if type(page) is not int or page < 1:
        raise ValueError('来源页码应为正整数。')
    screenshot = _screenshot_path(value.get('screenshot'))
    return {'page': page, 'screenshot': screenshot}

def _portraits(value):
    _fields(value, ('self', 'parent_1', 'parent_2'), '头像')
    result = {}
    for name, portrait in value.items():
        _fields(portrait, ('screenshot', 'bbox'), '头像来源')
        screenshot = _screenshot_path(portrait.get('screenshot'), optional=False)
        bbox = portrait.get('bbox')
        if not isinstance(bbox, list) or len(bbox) != 4 or any((type(n) is not int or n < 0 for n in bbox)) or (bbox[2] <= bbox[0]) or (bbox[3] <= bbox[1]):
            raise ValueError('头像区域应为原始截图中的有效整数矩形。')
        result[name] = {'screenshot': screenshot, 'bbox': list(bbox)}
    return result

def _factor_values(value):
    kind = _kind(value.get('kind'))
    stars = value.get('stars')
    if stars is not None and (type(stars) is not int or not 1 <= stars <= 3):
        raise ValueError('因子星数应为 1～3 或未知。')
    return {'kind': kind, 'factor_key': _key(value.get('factor_key'), kind), 'raw_name': _text(value.get('raw_name', ''), optional=True) or '', 'stars': stars}

def _factor(value):
    _fields(value, (*FACTOR_FIELDS, 'sources', 'corrections'), '因子')
    result = _factor_values(value)
    sources = value.get('sources', [])
    corrections = value.get('corrections', [])
    if not isinstance(sources, list) or not isinstance(corrections, list):
        raise ValueError('因子来源或修正记录应为列表。')
    result['sources'] = [_source(row) for row in sources]
    result['corrections'] = []
    for correction in corrections:
        _fields(correction, ('before', 'after', 'at', 'note'), '人工修正')
        result['corrections'].append({'before': _factor_values(correction['before']), 'after': _factor_values(correction['after']), 'at': _text(correction['at']), 'note': _text(correction.get('note'), optional=True)})
    if result['corrections'] and result['corrections'][-1]['after'] != {k: result[k] for k in FACTOR_FIELDS}:
        raise ValueError('最后一次修正与当前因子不一致。')
    return result

def _node(value):
    if value is None:
        return None
    _fields(value, ('entry_id', *IDENTITY, 'status', 'scope', 'missing', 'factors'), '谱系节点')
    scope = value.get('scope', [])
    missing = value.get('missing', [])
    factors = value.get('factors', [])
    status = value.get('status', 'partial')
    if status not in {'partial', 'complete'} or not isinstance(scope, list) or (not isinstance(missing, list)) or (not isinstance(factors, list)):
        raise ValueError('节点完整性或因子列表无效。')
    scope = list(dict.fromkeys((_kind(kind) for kind in scope)))
    missing = list(dict.fromkeys((_text(item) for item in missing)))
    rows = [_factor(row) for row in factors]
    if any((row['kind'] not in scope for row in rows)):
        raise ValueError('已观察因子的颜色必须包含在节点扫描范围中。')
    derived = []
    keys = [row['factor_key'] for row in rows if row['factor_key']]
    if len(set(keys)) != len(keys):
        derived.append('duplicate_factor_rows')
    if any((row['factor_key'] is None for row in rows)):
        derived.append('unresolved_factor_name')
    if any((row['stars'] is None for row in rows)):
        derived.append('unknown_factor_stars')
    if not scope:
        derived.append('not_scanned')
    missing = [item for item in missing if item not in {'duplicate_factor_rows', 'unresolved_factor_name', 'unknown_factor_stars', 'not_scanned'}]
    missing = list(dict.fromkeys(missing + derived))
    if missing:
        status = 'partial'
    if status == 'partial' and (not missing):
        missing = ['scan_incomplete']
    return {'entry_id': _id(value['entry_id']) if value.get('entry_id') is not None else None, **{key: _text(value.get(key), optional=True) for key in IDENTITY}, 'status': status, 'scope': scope, 'missing': missing, 'factors': rows}

def _entry(value):
    _fields(value, ('entry_id', *IDENTITY, 'self', 'parents', 'portraits'), '成品')
    parents = value.get('parents', [None, None])
    if not isinstance(parents, list) or len(parents) != 2:
        raise ValueError('父母节点必须恰好两个，未扫描节点填空值。')
    own = _node(value.get('self') or {})
    if own['entry_id'] not in (None, value['entry_id']):
        raise ValueError('本体节点与成品编号不一致。')
    return {'entry_id': _id(value['entry_id']), **{key: _text(value.get(key), optional=True) for key in IDENTITY}, 'self': own, 'parents': [_node(row) for row in parents], **({'portraits': _portraits(value['portraits'])} if 'portraits' in value else {})}

def new_library(name='我的种马库', library_id=None):
    return {'schema_version': SCHEMA, 'library_id': _id(library_id or uuid.uuid4().hex), 'name': _text(name), 'revision': 0, 'entries': []}

def normalize_library(value, *, catalog_path=None):
    _fields(value, ('schema_version', 'library_id', 'name', 'revision', 'entries'), '种马库')
    if value.get('schema_version') != SCHEMA:
        raise ValueError('种马库格式版本不受支持。')
    revision = value.get('revision', 0)
    if type(revision) is not int or revision < 0 or (not isinstance(value.get('entries'), list)):
        raise ValueError('种马库版本或条目列表无效。')
    if catalog_path is not None:
        catalog_choices(catalog_path)
    rows = [_entry(row) for row in value['entries']]
    if len({row['entry_id'] for row in rows}) != len(rows):
        raise ValueError('成品编号重复；请明确更新原编号或保留为另一只成品。')
    return {'schema_version': SCHEMA, 'library_id': _id(value['library_id']), 'name': _text(value['name']), 'revision': revision, 'entries': rows}

def store_path(root=None):
    directory = Path(root) if root is not None else Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local') / 'MaaUma/parent-library'
    return directory / 'library.json'

def load_library(*, root=None, catalog_path=None):
    path = store_path(root)
    if not path.exists():
        return new_library(library_id='local')
    return normalize_library(json.loads(path.read_text(encoding='utf-8-sig')), catalog_path=catalog_path)

def save_library(value, *, root=None, expected_revision, catalog_path=None):
    """One shared library; a revision check and short lock prevent lost edits."""
    value = normalize_library(value, catalog_path=catalog_path)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('保存时必须提供已读取的种马库版本。')
    path = store_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix('.lock')
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 384)
    except FileExistsError:
        raise LibraryConflict('种马库正在保存，请稍后重新读取。') from None
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        os.close(descriptor)
        current = load_library(root=root)
        if current['revision'] != expected_revision:
            raise LibraryConflict('种马库已被另一面板修改，请重新读取后再保存。')
        value['revision'] = current['revision'] + 1
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(path)
        return value
    finally:
        if temporary.exists():
            temporary.unlink()
        lock.unlink()

def upsert_entry(library, entry, *, catalog_path=None):
    result = normalize_library(library, catalog_path=catalog_path)
    value = deepcopy(entry)
    if not value.get('entry_id'):
        value['entry_id'] = uuid.uuid4().hex
    index = next((i for i, row in enumerate(result['entries']) if row['entry_id'] == value['entry_id']), None)
    if index is None:
        result['entries'].append(_entry(value))
    else:
        result['entries'][index] = _entry({**result['entries'][index], **value})
    return result

def get_entry(library, entry_id):
    value = normalize_library(library)
    for row in value['entries']:
        if row['entry_id'] == entry_id:
            return row
    raise ValueError('未找到这只成品，请刷新种马库。')

def correct_factor(library, entry_id, node, factor_index, changes, *, note=None):
    result = normalize_library(library)
    entry = get_entry(result, entry_id)
    locations = {'self': entry['self'], 'parent1': entry['parents'][0], 'parent2': entry['parents'][1]}
    target = locations.get(node)
    if target is None or type(factor_index) is not int or (not 0 <= factor_index < len(target['factors'])) or (not isinstance(changes, dict)) or (not changes) or set(changes) - set(FACTOR_FIELDS):
        raise ValueError('请选择已有因子及需要明确修正的字段。')
    row = target['factors'][factor_index]
    before = {key: row[key] for key in FACTOR_FIELDS}
    after = _factor_values({**before, **changes})
    if before != after:
        row.update(after)
        row['corrections'].append({'before': before, 'after': after, 'at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'note': _text(note, optional=True)})
    return upsert_entry(result, entry)

def duplicate_candidates(library, entry_id):
    rows = normalize_library(library)['entries']
    entry = next((row for row in rows if row['entry_id'] == entry_id), None)
    if entry is None:
        raise ValueError('未找到这只成品。')
    return _duplicates(rows, entry)

def _duplicates(rows, entry):

    def identity(row):
        return (row['character_id'] or row['character_name'], row['variant_id'] or row['variant_name'])

    def signature(row):
        return sorted(((f['factor_key'], f['stars']) for f in row['self']['factors'] if f['factor_key'] and f['stars'] is not None))
    own = signature(entry)
    return [row['entry_id'] for row in rows if row['entry_id'] != entry['entry_id'] and identity(entry)[0] is not None and (identity(entry) == identity(row)) and (entry['trained_on'] is not None and entry['trained_on'] == row['trained_on'] or (bool(own) and own == signature(row)))]

def _node_summary(node):
    if node is None:
        return {'known_stars': {}, 'known_raw_stars': {}, 'raw_names_complete': False, 'complete_kinds': [], 'missing': ['not_scanned']}
    by_key = {}
    by_raw = {}
    for row in node['factors']:
        if row['raw_name']:
            by_raw.setdefault(row['raw_name'], set()).add(row['stars'])
        if row['factor_key'] and row['stars'] is not None:
            by_key.setdefault(row['factor_key'], set()).add(row['stars'])
    return {'known_stars': {key: next(iter(stars)) for key, stars in by_key.items() if len(stars) == 1}, 'known_raw_stars': {name: next(iter(stars)) for name, stars in by_raw.items() if len(stars) == 1 and None not in stars}, 'raw_names_complete': node['status'] == 'complete' and set(node['scope']) == set(KINDS) and all((row['raw_name'] for row in node['factors'])) and all((len(stars) == 1 and None not in stars for stars in by_raw.values())), 'complete_kinds': node['scope'] if node['status'] == 'complete' else [], 'missing': list(node['missing'])}

def summarize_entry(entry):
    entry = _entry(entry)
    nodes = {'self': _node_summary(entry['self']), 'parent1': _node_summary(entry['parents'][0]), 'parent2': _node_summary(entry['parents'][1])}
    totals = {}
    raw_totals = {}
    duplicate_nodes = []
    seen = {entry['entry_id']: 'self'}
    included = ['self']
    conflicts = set()
    raw_conflicts = set()
    for i, parent in enumerate(entry['parents'], 1):
        name = f'parent{i}'
        reference = parent and parent.get('entry_id')
        if reference and reference in seen:
            duplicate_nodes.append(name)
            prior = seen[reference]
            if prior != 'self':
                if any((nodes[prior][key] != nodes[name][key] for key in ('known_stars', 'complete_kinds', 'missing'))):
                    conflicts.add(prior)
                if any((nodes[prior][key] != nodes[name][key] for key in ('known_raw_stars', 'raw_names_complete'))):
                    raw_conflicts.add(prior)
            continue
        if reference:
            seen[reference] = name
        included.append(name)
    for name in included:
        if name in conflicts:
            continue
        for key, stars in nodes[name]['known_stars'].items():
            totals[key] = totals.get(key, 0) + stars
        if name not in raw_conflicts:
            for raw, stars in nodes[name]['known_raw_stars'].items():
                raw_totals[raw] = raw_totals.get(raw, 0) + stars
    complete = [] if conflicts else [kind for kind in KINDS if all((kind in nodes[n]['complete_kinds'] for n in included))]
    return {'self': nodes['self'], 'parents': [nodes['parent1'], nodes['parent2']], 'pedigree': {'known_stars': totals, 'known_raw_stars': raw_totals, 'raw_names_complete': not conflicts and (not raw_conflicts) and all((nodes[n]['raw_names_complete'] for n in included)), 'complete_kinds': complete, 'included_nodes': included, 'duplicate_nodes': duplicate_nodes, 'missing': [f'{name}:{gap}' for name in included for gap in nodes[name]['missing']] + [f'{name}:conflicting_parent_observations' for name in sorted(conflicts)] + [f'{name}:conflicting_parent_raw_observations' for name in sorted(raw_conflicts)]}}

def _requirements(rows):
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError('筛选条件应为因子列表。')
    result = []
    seen = set()
    for row in rows:
        _fields(row, ('factor_key', 'raw_name', 'min_stars'), '筛选条件')
        if ('factor_key' in row) == ('raw_name' in row):
            raise ValueError('筛选因子请选择标准编号或明确原文之一。')
        field = 'factor_key' if 'factor_key' in row else 'raw_name'
        key = _key(row['factor_key']) if field == 'factor_key' else _text(row['raw_name'])
        stars = row.get('min_stars')
        identity = (field, key)
        if key is None or identity in seen or type(stars) is not int or (not 1 <= stars <= 9):
            raise ValueError('筛选因子或最低星数无效。')
        result.append({field: key, 'min_stars': stars})
        seen.add(identity)
    return result

def _match_mode(value):
    if value not in {'all', 'any'}:
        raise ValueError('因子匹配方式请选择全部或任一。')
    return value

def _assessment(selected, requirements, match_mode):
    checks = []
    for row in requirements:
        if 'factor_key' in row:
            key = row['factor_key']
            known = selected['known_stars'].get(key)
            complete = key.split(':')[0] in selected['complete_kinds']
        else:
            known = selected['known_raw_stars'].get(row['raw_name'])
            complete = selected['raw_names_complete']
        checks.append({**row, 'known_stars': known, 'satisfied': known is not None and known >= row['min_stars'], 'uncertain': not complete, 'known_absent': complete and known is None})
    if checks:
        satisfied = all((row['satisfied'] for row in checks)) if match_mode == 'all' else any((row['satisfied'] for row in checks))
        if satisfied:
            status = 'known_satisfied'
        elif any((row['known_stars'] is not None for row in checks)):
            status = 'partial_material'
        elif any((row['uncertain'] for row in checks)):
            status = 'insufficient_data'
        else:
            status = None
    else:
        status = 'known_satisfied' if set(selected['complete_kinds']) == set(KINDS) else 'partial_material' if selected['known_stars'] else 'insufficient_data'
    return {'status': status, 'label': LABELS.get(status), 'matches': status is not None, 'checks': checks}

def assess_entry(entry, requirements=None, scope='self', match_mode='all'):
    if scope not in {'self', 'pedigree'}:
        raise ValueError('筛选范围请选择本体或谱系。')
    match_mode = _match_mode(match_mode)
    summary = summarize_entry(entry)
    requirements = _requirements(requirements)
    views = {name: _assessment(summary[name], requirements, match_mode) for name in ('self', 'pedigree')}
    return {**views[scope], 'scope': scope, 'match_mode': match_mode, 'summary': summary, **views}

def list_entries(library, query='', requirements=None, scope='self', match_mode='all'):
    if scope not in {'self', 'pedigree'}:
        raise ValueError('筛选范围请选择本体或谱系。')
    match_mode = _match_mode(match_mode)
    requirements = _requirements(requirements)
    value = normalize_library(library)
    query = _text(query, optional=True) or ''
    result = []
    for entry in value['entries']:
        text = ' '.join((str(entry.get(key) or '') for key in IDENTITY))
        text += ' ' + ' '.join((f['raw_name'] + ' ' + (f['factor_key'] or '') for n in [entry['self'], *entry['parents']] if n for f in n['factors']))
        if query.casefold() not in text.casefold():
            continue
        assessment = assess_entry(entry, requirements, scope, match_mode)
        if not assessment['matches']:
            continue
        result.append({'entry_id': entry['entry_id'], **{key: entry[key] for key in IDENTITY}, 'assessment': assessment, 'duplicate_candidates': _duplicates(value['entries'], entry), **({'portraits': deepcopy(entry['portraits'])} if 'portraits' in entry else {})})
    return result

def export_library(library):
    return normalize_library(library)

def import_library(value, *, catalog_path=None):
    if isinstance(value, str):
        value = json.loads(value)
    return normalize_library(value, catalog_path=catalog_path)

def planner_snapshot(library, requirements=None, scope='self', match_mode='all'):
    value = normalize_library(library)
    requirements = _requirements(requirements)
    selected = {row['entry_id']: row for row in list_entries(value, requirements=requirements, scope=scope, match_mode=match_mode)}

    def node_projection(node):
        if node is None:
            return None
        return {key: deepcopy(node[key]) for key in ('entry_id', *IDENTITY, 'status', 'scope', 'missing')} | {'factors': [{**{key: factor[key] for key in FACTOR_FIELDS}, 'manually_corrected': bool(factor['corrections'])} for factor in node['factors']]}
    return {'schema_version': SNAPSHOT_SCHEMA, 'library_id': value['library_id'], 'name': value['name'], 'revision': value['revision'], 'scope': scope, 'requirements': requirements, 'match_mode': match_mode, 'identity_basis': 'local_record_id', 'entries': [{**{key: entry[key] for key in ('entry_id', *IDENTITY)}, 'self': node_projection(entry['self']), 'parents': [node_projection(n) for n in entry['parents']], 'assessment': selected[entry['entry_id']]['assessment'], 'duplicate_candidates': selected[entry['entry_id']]['duplicate_candidates']} for entry in value['entries'] if entry['entry_id'] in selected]}
