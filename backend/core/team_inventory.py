"""Account-bound playable-character/card facts, independent of breeding drafts."""
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import uuid
from core.parent_library import store_path
SCHEMA = 'uma.team_inventory.v1'
COVERAGES = {'unknown', 'partial', 'complete'}
STATUSES = {'owned', 'borrowable', 'not_owned', 'unknown'}

class InventoryConflict(ValueError):
    pass

def _id(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]{1,96}', value):
        raise ValueError('请选择明确的规划账号。')
    return value

def _number(value, low, high, label):
    if value is None:
        return None
    if type(value) is not int or not low <= value <= high:
        raise ValueError(label + '无效，未知时应留空。')
    return value

def _text(value, limit=240):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError('持有资料文字无效。')
    return value.strip()

def _numeric_id(value):
    if value is None:
        return None
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch('[1-9][0-9]{0,15}', value):
        raise ValueError('持有资料编号无效。')
    return value

def normalize_section(value, kind):
    if not isinstance(value, dict) or set(value) - {'coverage', 'items'}:
        raise ValueError('角色或支援卡资料应包含完整性与清单。')
    coverage, items = (value.get('coverage', 'unknown'), value.get('items', []))
    if not isinstance(coverage, str) or coverage not in COVERAGES or (not isinstance(items, list)) or (len(items) > 2000):
        raise ValueError('持有清单完整性或数量无效。')
    rows, seen = ([], set())
    for item in items:
        allowed = {'character_id', 'variant_id'} if kind == 'characters' else {'card_id', 'level', 'limit_break'}
        if not isinstance(item, dict) or set(item) - allowed - {'raw_name', 'status', 'source'}:
            raise ValueError('持有资料条目字段无效。')
        status = item.get('status', 'unknown')
        if not isinstance(status, str) or status not in STATUSES or (kind == 'characters' and status == 'borrowable'):
            raise ValueError('可育成角色不能作为借用角色；支援卡可明确标记可借。')
        raw = _text(item.get('raw_name', ''))
        row = {'raw_name': raw, 'status': status, 'source': _text(item.get('source', 'manual'))}
        if kind == 'characters':
            cid = _numeric_id(item.get('character_id'))
            vid = _numeric_id(item.get('variant_id'))
            row.update(character_id=int(cid) if cid else None, variant_id=vid)
            identity = (cid, vid) if cid or vid else ('raw', raw)
        else:
            card = _numeric_id(item.get('card_id'))
            row.update(card_id=card, level=_number(item.get('level'), 1, 50, '支援卡等级'), limit_break=_number(item.get('limit_break'), 0, 4, '支援卡突破'))
            identity = card or ('raw', raw)
        if not raw and all((row.get(key) is None for key in ('character_id', 'variant_id', 'card_id'))):
            raise ValueError('未收录资料至少应保留原名。')
        if identity in seen:
            raise ValueError('同一角色衣装或支援卡重复，请合并后再保存。')
        seen.add(identity)
        rows.append(row)
    return {'coverage': coverage, 'items': rows}

def empty_account(account_id):
    return {'account_id': _id(account_id), 'characters': {'coverage': 'unknown', 'items': []}, 'support_cards': {'coverage': 'unknown', 'items': []}}

def new_inventory():
    return {'schema_version': SCHEMA, 'revision': 0, 'accounts': []}

def normalize_inventory(value):
    if not isinstance(value, dict) or set(value) - {'schema_version', 'revision', 'accounts'}:
        raise ValueError('账号资源资料字段无效。')
    if value.get('schema_version') != SCHEMA or type(value.get('revision')) is not int or value['revision'] < 0:
        raise ValueError('账号资源资料版本无效。')
    accounts = value.get('accounts')
    if not isinstance(accounts, list) or len(accounts) > 1000:
        raise ValueError('账号资源资料列表无效。')
    rows, seen = ([], set())
    for account in accounts:
        if not isinstance(account, dict) or set(account) != {'account_id', 'characters', 'support_cards'}:
            raise ValueError('账号资源记录格式无效。')
        identifier = _id(account['account_id'])
        if identifier in seen:
            raise ValueError('账号资源记录重复。')
        seen.add(identifier)
        rows.append({'account_id': identifier, **{kind: normalize_section(account[kind], kind) for kind in ('characters', 'support_cards')}})
    return {'schema_version': SCHEMA, 'revision': value['revision'], 'accounts': rows}

def account_inventory(value, account_id):
    value = normalize_inventory(value)
    if account_id is None:
        return {'account_id': None, 'characters': {'coverage': 'unknown', 'items': []}, 'support_cards': {'coverage': 'unknown', 'items': []}}
    return deepcopy(next((row for row in value['accounts'] if row['account_id'] == _id(account_id)), empty_account(account_id)))

def upsert_account_inventory(value, account_id, *, characters=None, support_cards=None):
    result = normalize_inventory(value)
    row = account_inventory(result, account_id)
    for kind, section in (('characters', characters), ('support_cards', support_cards)):
        if section is not None:
            row[kind] = normalize_section(section, kind)
    result['accounts'] = [old for old in result['accounts'] if old['account_id'] != row['account_id']]
    result['accounts'].append(row)
    return normalize_inventory(result)

def inventory_path(root=None):
    return store_path(root).with_name('team-inventory.json')

def load_inventory(root=None):
    path = inventory_path(root)
    if not path.exists():
        return new_inventory()
    if path.stat().st_size > 8 * 1024 ** 2:
        raise ValueError('账号资源资料过大，请先检查本地文件。')
    try:
        return normalize_inventory(json.loads(path.read_text('utf-8-sig')))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError('账号资源资料损坏，未自动覆盖。') from None

def save_inventory(value, root=None, *, expected_revision):
    result = normalize_inventory(value)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('保存前请读取账号资源版本。')
    path = inventory_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix('.lock')
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 384)
    except FileExistsError:
        raise InventoryConflict('账号资源正在另一窗口保存。') from None
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        os.close(descriptor)
        current = load_inventory(root)
        if current['revision'] != expected_revision:
            raise InventoryConflict('账号资源已变化，请刷新后再保存。')
        result['revision'] = current['revision'] + 1
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        temporary.replace(path)
        return result
    finally:
        if temporary.exists():
            temporary.unlink()
        lock.unlink()

def merge_explicit_snapshot(account, snapshot):
    """Consume only an explicitly account-bound existing report, never discover files.

    Existing support_library_snapshot reports carry stable IDs and an explicit
    account_id but no full-coverage guarantee. SSR-screen reports explicitly do
    not verify account identity and are deliberately not accepted here.
    """
    result = deepcopy(account)
    if not isinstance(snapshot, dict) or snapshot.get('schema_version') != 'uma.support_library_snapshot.v1':
        return result
    if snapshot.get('account_id') != result.get('account_id'):
        raise ValueError('已有扫描资料不属于当前明确账号，未按相似账号名合并。')
    for kind, incoming in (('characters', snapshot.get('umas', [])), ('support_cards', snapshot.get('support_cards', []))):
        rows = list(result[kind]['items'])
        for item in incoming if isinstance(incoming, list) else []:
            if not isinstance(item, dict):
                continue
            confidence = item.get('confidence')
            if type(confidence) not in (int, float) or not math.isfinite(confidence) or confidence < 0.9:
                continue
            if kind == 'characters':
                identifier = str(item.get('uma_id', ''))
                row = {'character_id': int(identifier) if identifier.isdigit() and len(identifier) <= 4 else None, 'variant_id': identifier if identifier.isdigit() and len(identifier) > 4 else None, 'raw_name': item.get('display_name', ''), 'status': 'owned', 'source': 'explicit_account_snapshot'}
                duplicate = any((old.get('variant_id') == row['variant_id'] and old.get('character_id') == row['character_id'] for old in rows))
            else:
                row = {'card_id': str(item.get('card_id', '')), 'raw_name': item.get('display_name', ''), 'status': 'owned', 'level': item.get('level'), 'limit_break': item.get('breakthrough_stage'), 'source': 'explicit_account_snapshot'}
                duplicate = any((old.get('card_id') == row['card_id'] for old in rows))
            if not duplicate:
                try:
                    rows.extend(normalize_section({'coverage': 'partial', 'items': [row]}, kind)['items'])
                except ValueError:
                    continue
        result[kind] = {'coverage': result[kind]['coverage'] if result[kind]['coverage'] == 'complete' else 'partial' if rows else 'unknown', 'items': rows}
    return result
