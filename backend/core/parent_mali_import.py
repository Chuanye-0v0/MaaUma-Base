"""Imports and a local sidecar for user-selected 吗哩吗哩 candidates.

Search results give a self rarity and a family total for each factor. Only the
self rarity becomes a node observation. An exact total stays separate, even
when subtracting the self stars would determine the sum of the two parents.
"""

from contextlib import contextmanager
from copy import deepcopy
from functools import cache
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import unicodedata
from uuid import uuid4

from core import parent_library as library
from core.parent_planner_catalog import ParentPlannerCatalog


DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / 'data/parent_library/factor_catalog.json'
PROVIDER_TYPES = {1: 'blue', 2: 'red', 3: 'green', 4: 'white', 5: 'white', 6: 'white'}
CARD_COLORS = {'蓝': 'blue', '红': 'red', '绿': 'green', '白': 'white'}
CARD_ID = re.compile(r'^ID\s*[:：]?\s*([0-9]{1,20})(?=\s|[·，,]|$)', re.IGNORECASE)
CARD_FACTOR = re.compile(
    r'·\s*家系\s*(?P<family>[0-9])\s*★'
    r'\s*·\s*本体\s*(?P<own>[0-3])\s*★')
SOURCE_ID = re.compile(r'mali-(?:[0-9]{1,20}|card)-[0-9a-f]{20}\Z')
POOL_SCHEMA = 'uma.parent_mali_pool.v1'
_CARD_ERROR = '请粘贴一张种马卡片的角色名、ID 和星数行，或导入搜索结果 JSON。'


def _name_key(value):
    return unicodedata.normalize('NFKC', value).strip().casefold()


@cache
def _positive_circle_aliases():
    """Use the planner's explicit white-factor mappings, not suffix stripping."""
    try:
        planner = ParentPlannerCatalog.load()
    except FileNotFoundError:
        return ()
    return tuple(
        (_name_key(row['name']), row['factor_key'])
        for row in planner.targets()
        if row['kind'] == 'skill' and row['name'].endswith(('○', '◎'))
        and row['mapping'] in ('confirmed_reference', 'lower_relation')
        and row['factor_key'].startswith('white:')
    )


def _catalog(path):
    matches = {}
    factors = library.catalog_choices(path or DEFAULT_CATALOG)
    for row in factors:
        key = row['kind'], _name_key(row['name'])
        matches.setdefault(key, set()).add(row['factor_key'])
    if path is None:
        available = {row['factor_key'] for row in factors}
        if 'white:大师杯剧本' in available:
            matches.setdefault(('white', _name_key('荣耀女神杯剧本')), set()).add('white:大师杯剧本')
        for name, factor_key in _positive_circle_aliases():
            if factor_key in available:
                matches.setdefault(('white', name), set()).add(factor_key)
    return {key: next(iter(values)) for key, values in matches.items() if len(values) == 1}


def _role_id(value):
    if type(value) is int:
        value = str(value)
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,20}', value) and int(value) > 0:
        return value
    return None


def _stars(value, maximum):
    if isinstance(value, str) and re.fullmatch(r'[0-9]', value):
        value = int(value)
    return value if type(value) is int and 0 <= value <= maximum else None


def _text(value):
    return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= 240 else None


def _card_from_text(value, catalog_path):
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    ids = [(index, match.group(1)) for index, line in enumerate(lines)
           if (match := CARD_ID.match(line))]
    if len(ids) != 1 or ids[0][0] not in (0, 1):
        raise ValueError(_CARD_ERROR)
    index, role = ids[0]
    name = lines[0] if index else None
    if name and re.match(r'^角色\s*[:：]', name):
        name = re.split(r'[:：]', name, maxsplit=1)[1].strip()

    catalog = _catalog(catalog_path)
    factors = []
    previous_end = 0
    for match in CARD_FACTOR.finditer(value):
        line_start = value.rfind('\n', previous_end, match.start()) + 1
        factor_name = value[max(previous_end, line_start):match.start()]
        previous_end = match.end()
        # The community result card can display a gold skill followed by the
        # actual lower white factor. Keep the latter as the observed name.
        label = factor_name.rsplit('→', 1)[-1].strip()
        # Try the whole name first. A dot can be part of a factor name, or
        # separate a card annotation such as 必需 from the actual factor.
        while label:
            prefix = re.match(r'^([蓝红绿白])(?:因子)?\s*[:：]\s*(.+)$', label)
            if prefix:
                kind, label = CARD_COLORS[prefix.group(1)], prefix.group(2).strip()
                break
            kinds = {kind for kind, key in catalog if key == _name_key(label)}
            if len(kinds) == 1:
                kind = next(iter(kinds))
                break
            label = label.partition('·')[2].strip()
        if not label:
            continue
        factors.append({'kind': kind, 'name': label, 'rarity': int(match.group('own')),
                        'total_rarity': int(match.group('family'))})
    if not factors:
        raise ValueError('这张卡片没有可识别的因子星数，请复制完整卡片或粘贴搜索结果 JSON。')
    return {'role_id': role, 'hero_card': {'name': name, 'factors': factors}}


def mali_search_records(payload, *, catalog_path=None, allow_invalid_records=False):
    """Unwrap the public search response, a candidate array, or one copied card."""
    if isinstance(payload, str):
        if len(payload) > 512_000:
            raise ValueError('吗哩吗哩候选内容过长，请分批导入。')
        payload = payload.lstrip('\ufeff \r\n\t')
        if not payload.startswith(('{', '[')):
            return [_card_from_text(payload, catalog_path)]
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError('吗哩吗哩搜索结果 JSON 格式有误。') from exc
    if isinstance(payload, dict) and 'data' in payload:
        data = payload['data']
        if not isinstance(data, dict) or not isinstance(data.get('records'), list):
            raise ValueError('搜索结果里没有候选记录，请检查是否复制了搜索响应。')
        payload = data['records']
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or (
            not allow_invalid_records and any(not isinstance(row, dict) for row in payload)):
        raise ValueError(_CARD_ERROR)
    return payload


def _provider_factors(card, catalog_path):
    factors = card.get('factors', [])
    if not isinstance(factors, list) or len(factors) > 500:
        raise ValueError('吗哩吗哩因子列表无效。')
    catalog = _catalog(catalog_path)
    for factor in factors:
        if not isinstance(factor, dict):
            raise ValueError('吗哩吗哩因子条目无效。')
        kind = PROVIDER_TYPES.get(_stars(factor.get('type'), 6))
        if kind is None and factor.get('kind') in ('blue', 'red', 'green', 'white'):
            kind = factor['kind']
        name = _text(factor.get('name')) or _text(factor.get('factor_name'))
        if not kind or not name:
            continue
        own, family = _stars(factor.get('rarity'), 3), _stars(factor.get('total_rarity'), 9)
        if own is not None and family is not None and own > family:
            family = None
        yield kind, name, catalog.get((kind, _name_key(name))), own, family


def mali_candidate_totals(candidate, *, catalog_path=None):
    """Return unambiguous official totals for recognized factors, as references."""
    card = candidate.get('hero_card') if isinstance(candidate, dict) else None
    if not isinstance(card, dict):
        return []
    totals = {}
    for _, _, key, _, family in _provider_factors(card, catalog_path):
        if key is not None and family is not None:
            totals.setdefault(key, set()).add(family)
    return [{'factor_key': key, 'family_stars': next(iter(stars))}
            for key, stars in sorted(totals.items()) if len(stars) == 1 and 0 not in stars]


def _manual_node(node):
    if node is None:
        return None
    if not isinstance(node, dict):
        raise ValueError('吗哩吗哩系谱节点应为对象。')
    factors = node.get('factors', [])
    if not isinstance(factors, list) or len(factors) > 500 or any(not isinstance(row, dict) for row in factors):
        raise ValueError('吗哩吗哩系谱因子列表无效。')
    return {**_manual_identity(node),
            'factors': [{key: row[key] for key in library.FACTOR_FIELDS if key in row} for row in factors],
            'scope': node.get('scope', list(dict.fromkeys(row.get('kind') for row in factors))),
            **{key: node[key] for key in ('status', 'missing') if key in node}}


def _manual_identity(node):
    result = {key: node[key] for key in library.IDENTITY if key in node}
    for key in ('character_id', 'variant_id'):
        if type(result.get(key)) is int:
            result[key] = str(result[key])
    return result


def normalize_mali_candidate(candidate, *, catalog_path=None):
    """Make an external entry snapshot without local file or entry references."""
    if not isinstance(candidate, dict):
        raise ValueError('吗哩吗哩候选应为对象。')
    role = _role_id(candidate.get('role_id'))
    card = candidate.get('hero_card')
    observed = []
    if isinstance(card, dict):
        name = _text(card.get('name')) or _text(card.get('card_name'))
        if not role and not name:
            raise ValueError('吗哩吗哩候选缺少角色名和训练员 ID。')
        observed = sorted(set(_provider_factors(card, catalog_path)), key=repr)
        own_observations = dict.fromkeys(row[:4] for row in observed if row[3])
        factors = [{'kind': kind, 'factor_key': key, 'raw_name': label, 'stars': own}
                   for kind, label, key, own in own_observations]
        identity = {'character_name': name}
        entry = {**identity, 'self': {**identity, 'status': 'partial',
                'scope': list(dict.fromkeys(row['kind'] for row in factors)),
                'missing': ['mali_search_snapshot_only'], 'factors': factors}, 'parents': [None, None]}
    elif 'self' in candidate or 'parents' in candidate:
        parents = candidate.get('parents', [None, None])
        if not isinstance(parents, list) or len(parents) != 2:
            raise ValueError('吗哩吗哩手填系谱应有两个祖辈槽，未知可填空值。')
        entry = {**_manual_identity(candidate),
                 'self': _manual_node(candidate.get('self')), 'parents': [_manual_node(node) for node in parents]}
    else:
        raise ValueError('找不到吗哩吗哩种马卡片或手填系谱。')

    # Key on the imported observations, not display order or ignored response
    # fields. A player's currently shared horse can change under the same ID.
    normalized = library._entry({'entry_id': 'mali-preview', **entry})
    existing = candidate.get('entry_id')
    if card is None and not role and isinstance(existing, str) and SOURCE_ID.fullmatch(existing):
        normalized['entry_id'] = existing
    else:
        normalized.pop('entry_id')
        content = json.dumps([normalized, observed], ensure_ascii=False, sort_keys=True)
        digest = sha256(content.encode('utf-8')).hexdigest()[:20]
        normalized['entry_id'] = f"mali-{role or 'card'}-{digest}"
    return normalized


def mali_pool_path(root=None):
    return library.store_path(root).with_name('mali_pool.json')


def mali_pool_item(candidate, *, catalog_path=None):
    """Keep the imported material and its separate family totals."""
    if not isinstance(candidate, dict):
        raise ValueError('吗哩吗哩候选应为对象。')
    if 'entry' not in candidate:
        entry = normalize_mali_candidate(candidate, catalog_path=catalog_path)
        totals = mali_candidate_totals(candidate, catalog_path=catalog_path)
    else:
        entry = library._entry(candidate['entry'])
        raw_totals = candidate.get('reference_totals', [])
        if not isinstance(raw_totals, list) or len(raw_totals) > 200:
            raise ValueError('吗哩吗哩候选家系星数格式无效。')
        totals = []
        seen = set()
        for row in raw_totals:
            library._fields(row, ('factor_key', 'family_stars'), '吗哩吗哩候选家系星数')
            key, stars = library._key(row.get('factor_key')), row.get('family_stars')
            if key is None or key in seen or type(stars) is not int or not 1 <= stars <= 9:
                raise ValueError('吗哩吗哩候选家系星数应为每个因子一条 1～9 星记录。')
            seen.add(key)
            totals.append({'factor_key': key, 'family_stars': stars})
    identifier = entry['entry_id']
    match = re.fullmatch(r'mali-([0-9]{1,20})-[0-9a-f]{20}', identifier)
    trainer_id = match.group(1) if match else _role_id(candidate.get('role_id') or candidate.get('trainer_id'))
    name = entry.get('character_name') or entry['self'].get('character_name')
    label = f'{name} · 训练员 {trainer_id}' if name and trainer_id else (
        name or (f'训练员 {trainer_id}' if trainer_id else identifier))
    return {'entry_id': identifier, 'trainer_id': trainer_id, 'character_name': name, 'label': label,
            'self_factors': deepcopy(entry['self']['factors']), 'reference_totals': deepcopy(totals),
            'entry': entry}


def load_mali_pool(root=None):
    path = mali_pool_path(root)
    try:
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as exc:
        raise ValueError('本地吗哩吗哩候选池 JSON 格式有误。') from exc
    if (not isinstance(payload, dict) or payload.get('schema_version') != POOL_SCHEMA
            or not isinstance(payload.get('items'), list)):
        raise ValueError('本地吗哩吗哩候选池版本或条目列表无效。')
    return [mali_pool_item(item) for item in payload['items']]


@contextmanager
def _mali_pool_transaction(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix('.lock')
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise library.LibraryConflict('吗哩吗哩候选池正在保存，请稍后重新读取。') from None
    try:
        os.close(descriptor)
        yield
    finally:
        lock.unlink()


def _write_mali_pool(path, items):
    temporary = path.with_name(f'{path.name}.{uuid4().hex}.tmp')
    try:
        temporary.write_text(json.dumps({'schema_version': POOL_SCHEMA, 'items': items},
            ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_mali_pool_items(candidates, *, root=None, catalog_path=None, max_items=200):
    if type(max_items) is not int or max_items < 1:
        raise ValueError('吗哩吗哩候选池容量应为正整数。')
    if candidates is None or (isinstance(candidates, str) and not candidates.strip()):
        raise ValueError('没有识别到可存入的吗哩吗哩候选。')
    records = mali_search_records(candidates, catalog_path=catalog_path, allow_invalid_records=True)
    if not records:
        raise ValueError('没有识别到可存入的吗哩吗哩候选。')
    updates = {}
    for record in records:
        try:
            item = mali_pool_item(record, catalog_path=catalog_path)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        updates[item['entry_id']] = item
    if not updates:
        raise ValueError('没有识别到有效的吗哩吗哩种马记录，请检查粘贴内容。')
    path = mali_pool_path(root)
    with _mali_pool_transaction(path):
        entries = {item['entry_id']: item for item in load_mali_pool(root)}
        added = len(updates.keys() - entries.keys())
        entries.update(updates)
        if len(entries) > max_items:
            raise ValueError(f'本地吗哩吗哩候选池最多保留 {max_items} 匹，请先移除部分候选。')
        pool = list(entries.values())
        _write_mali_pool(path, pool)
    return pool, added


def remove_mali_pool_item(entry_id, *, root=None):
    if not isinstance(entry_id, str) or not entry_id.strip():
        raise ValueError('请指定要移除的候选记录编号。')
    path = mali_pool_path(root)
    if not path.is_file():
        return [], False
    with _mali_pool_transaction(path):
        old = load_mali_pool(root)
        pool = [item for item in old if item['entry_id'] != entry_id]
        removed = len(pool) != len(old)
        if removed:
            _write_mali_pool(path, pool)
    return pool, removed


def clear_mali_pool(*, root=None):
    path = mali_pool_path(root)
    with _mali_pool_transaction(path):
        _write_mali_pool(path, [])
    return []
