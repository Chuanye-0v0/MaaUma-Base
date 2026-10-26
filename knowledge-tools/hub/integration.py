"""Explicit, local-only copies exchanged with the MaaUma configuration UI.

This module has no dependency on MaaUma account/configuration stores.  Every
import creates a new toolbox inventory ID; source IDs remain provenance only.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
from threading import RLock
import uuid

CONTEXT_SCHEMA = 'uma.toolbox.context.v1'
DRAFT_SCHEMA = 'uma.toolbox.draft.v1'
LOCK = RLock()
NODE_KEYS = ('target', 'parent_a', 'gp_a1', 'gp_a2', 'parent_b', 'gp_b1', 'gp_b2')
STATS = ('speed', 'stamina', 'power', 'guts', 'wisdom')
SOURCE_FIELDS = ('kind', 'status', 'id', 'revision', 'schema_version', 'context_id',
                 'source_library_id', 'account_id', 'server', 'observed', 'user_declared')


def default_storage_root():
    """The toolbox's own user store, never the MaaUma configuration folder."""
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local' / 'share')
    return base / 'MaaUmaToolbox' / 'local-store'


def storage_path(storage_root=None):
    return Path(storage_root) if storage_root is not None else default_storage_root()


def _scalar(value):
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not (-float('inf') < value < float('inf')):
            raise ValueError('数值必须是有限值。')
        return value
    if isinstance(value, str) and len(value) <= 1000:
        # Declared labels/IDs may not be used to smuggle local files or images.
        if re.search(r'(?i)(?:[A-Z]:[\\/]|\\\\|file://|data:image/)', value) or value.startswith('/'):
            raise ValueError('上下文和草稿不能携带本地路径或截图。')
        return value
    raise ValueError('声明字段应为简短文字、数值或空值。')


def _fields(value, names):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('声明字段应为对象。')
    return {key: _scalar(value[key]) for key in names if key in value}


def _source(value):
    return _fields(value, SOURCE_FIELDS) if isinstance(value, dict) else _scalar(value)


def _list(value, projector=_scalar, limit=3000):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError('声明列表无效或过长。')
    return [projector(item) for item in value]


def _revisions(value):
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > 50:
        raise ValueError('来源版本应为对象。')
    result = {}
    for key, item in value.items():
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', key):
            continue
        if re.search(r'(?i)(password|secret|token|credential|activation|path|screenshot)', key):
            continue
        result[key] = _fields(item, ('id', 'revision', 'version', 'schema_version', 'server')) if isinstance(item, dict) else _scalar(item)
    return result


def _targets(value):
    return _fields(value, STATS)


def _card(value):
    return _fields(value, ('slot', 'card_id', 'id', 'name', 'type', 'level', 'limit_breaks',
                           'limit_break', 'rarity', 'borrowed', 'is_friend', 'friend', 'status'))


def _support(value):
    result = _fields(value, ('schema_version', 'status', 'deck_id', 'friend_card_id', 'auto_select'))
    if result is None:
        return None
    for key in ('cards', 'card_ids'):
        if key in value:
            result[key] = _list(value[key], _card if key == 'cards' else _scalar, 6)
    if 'source' in value:
        result['source'] = _source(value['source'])
    if 'reference_snapshot' in value:
        result['reference_snapshot'] = _fields(value['reference_snapshot'],
            ('context_id', 'source_kind', 'source_status', 'source_revision'))
    return result


def _skill(value):
    if not isinstance(value, dict):
        return _scalar(value)
    result = _fields(value, ('skill_id', 'db_id', 'id', 'name', 'raw_name', 'hint_level',
                             'learned', 'selected', 'status', 'priority', 'name_status'))
    if 'source' in value:
        result['source'] = _source(value['source'])
    return result


def _requirements(value):
    result = _fields(value, ('schema_version', 'server', 'event_mode', 'event_name', 'venue',
        'surface', 'distance_m', 'distance_category', 'direction', 'season', 'weather',
        'track_condition', 'team_size', 'course_id', 'track_id', 'running_style'))
    if result is None:
        return None
    if 'edition' in value:
        result['edition'] = _fields(value['edition'], ('year', 'month', 'index'))
    for key in ('running_styles', 'race_targets'):
        if key in value:
            result[key] = _list(value[key], _race if key == 'race_targets' else _scalar)
    if isinstance(value.get('styles'), dict):
        styles = value['styles']; result['styles'] = {}
        for key in ('allowed', 'preferred', 'excluded'):
            if key in styles:
                result['styles'][key] = _list(styles[key])
        if 'counts' in styles:
            result['styles']['counts'] = _fields(styles['counts'], ('nige', 'senko', 'sashi', 'oikomi'))
    return result


def _race(value):
    if not isinstance(value, dict):
        return _scalar(value)
    result = _fields(value, ('race_id', 'id', 'event_key', 'name', 'raw_name', 'grade',
                             'status', 'confirmed', 'venue', 'surface', 'distance_m'))
    if 'source' in value:
        result['source'] = _source(value['source'])
    if 'career_time' in value:
        result['career_time'] = _fields(value['career_time'], ('year', 'month', 'half', 'turn'))
    return result


def _factor(value):
    return _fields(value, ('kind', 'type', 'factor_key', 'key', 'name', 'raw_name', 'stars',
                           'status', 'level', 'id', 'skill_id', 'name_status'))


def _node(value):
    if value is None or not isinstance(value, dict):
        return _scalar(value)
    result = _fields(value, ('entry_id', 'trained_chara_id', 'entry_character_id', 'character_id',
        'chara_id', 'character_name', 'name', 'outfit_id', 'outfit_name', 'variant_id',
        'variant_name', 'card_id', 'status', 'ownership', 'availability', 'availability_label',
        'eligible', 'available', 'reference_only', 'owner_account_id', 'source_kind',
        'locked', 'red_observed', 'wins_coverage', 'ancestry_bound_to_entry',
        'recipe_from_entry_id', 'identity_status', 'source_server', 'cross_server_reference'))
    for key in ('scope', 'missing', 'borrowable_by'):
        if key in value:
            result[key] = _list(value[key])
    for key in ('source',):
        if key in value:
            result[key] = _source(value[key])
    for key in ('red_target',):
        if key in value:
            result[key] = _fields(value[key], ('factor_key', 'factor', 'stars'))
    for key in ('red_factors',):
        if key in value:
            result[key] = _fields(value[key], ('turf', 'dirt', 'short', 'sprint', 'mile', 'medium', 'middle', 'long', 'nige', 'senko', 'sashi', 'oikomi'))
    if 'stats' in value:
        result['stats'] = _targets(value['stats'])
    if 'factors' in value:
        result['factors'] = _list(value['factors'], _factor)
    for key in ('wins', 'g1_wins', 'planned_wins', 'planned_races'):
        if key in value:
            result[key] = _list(value[key], _race)
    if 'race_records' in value:
        records = value['race_records']
        result['race_records'] = _fields(records, ('entry_id', 'coverage'))
        if isinstance(records, dict):
            if 'source' in records:
                result['race_records']['source'] = _source(records['source'])
            if 'wins' in records:
                result['race_records']['wins'] = _list(records['wins'], _race)
    return result


def _entry(value, account_id):
    if not isinstance(value, dict):
        raise ValueError('马库条目应为对象。')
    result = _node(value)
    if 'self' in value:
        result['self'] = _node(value['self'])
    if 'parents' in value:
        result['parents'] = _list(value['parents'], _node, 2)
    availability = value.get('availability')
    borrowed = value.get('borrowable_by') or []
    if account_id and availability == 'other_account' and account_id not in borrowed:
        return None
    # Completeness ('partial'/'complete') does not express ownership.
    status = 'unknown'
    if account_id:
        if value.get('status') in ('planned', 'unknown'):
            status = value['status']
        elif availability in ('planned', 'unknown'):
            status = availability
        elif account_id in borrowed or availability in ('borrowed', 'borrowable'):
            status = 'borrowable'
        elif availability == 'owned' or value.get('owner_account_id') == account_id:
            status = 'owned'
    else:
        result.update(availability='reference', reference_only=True, available=False)
    result['status'] = status
    result['ancestry_bound_to_entry'] = bool(result.get('entry_id') and
        status != 'planned' and value.get('status') != 'planned' and availability != 'planned')
    return result


def _library(value, account_id):
    result = _fields(value, ('schema_version', 'library_id', 'name', 'revision', 'server'))
    if result is None:
        return None
    rows = value.get('project_entries', value.get('entries', []))
    entries = [_entry(row, account_id) for row in _list(rows, lambda row: row)]
    result['entries'] = [row for row in entries if row is not None]
    identifiers = [row.get('entry_id') for row in result['entries'] if row.get('entry_id')]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError('来源成品编号重复；请先在面板选择明确的条目。')
    result['excluded_entry_count'] = len(entries) - len(result['entries'])
    if 'source' in value:
        result['source'] = _source(value['source'])
    return result


def _context(value):
    if not isinstance(value, dict) or value.get('schema_version') != CONTEXT_SCHEMA:
        raise ValueError('工具箱上下文格式版本无效。')
    server = value.get('server') or 'cn'
    if server not in ('cn', 'tw', 'jp'):
        raise ValueError('服务器请选择 cn、tw 或 jp。')
    result = {'schema_version': CONTEXT_SCHEMA, 'server': server}
    if 'account' in value:
        result['account'] = _fields(value['account'], ('id', 'label'))
    account_id = (result.get('account') or {}).get('id')
    if 'library' in value:
        result['library'] = _library(value['library'], account_id)
    if 'target_character_id' in value:
        result['target_character_id'] = _scalar(value['target_character_id'])
    if 'include_unassigned' in value:
        result['include_unassigned'] = value['include_unassigned'] is True
    for key, projector in (('requirements', _requirements), ('targets', _targets),
        ('support_profile', _support), ('source_revisions', _revisions)):
        if key in value:
            result[key] = projector(value[key])
    if 'skill_targets' in value:
        result['skill_targets'] = _list(value['skill_targets'], _skill)
    return result


def _draft(value):
    if not isinstance(value, dict) or value.get('schema_version') != DRAFT_SCHEMA:
        raise ValueError('工具箱草稿格式版本无效。')
    result = {'schema_version': DRAFT_SCHEMA}
    for key, projector in (('targets', _targets), ('support_profile', _support), ('source_revisions', _revisions)):
        if key in value:
            result[key] = projector(value[key])
    if 'skill_targets' in value:
        result['skill_targets'] = _list(value['skill_targets'], _skill)
    if 'lineage' in value:
        lineage = value['lineage']
        if not isinstance(lineage, dict):
            raise ValueError('系谱草稿应为对象。')
        nodes = lineage.get('nodes', lineage)
        if not isinstance(nodes, dict):
            raise ValueError('系谱节点应为对象。')
        result['lineage'] = {'nodes': {key: _node(nodes[key]) for key in NODE_KEYS if key in nodes}}
    return result


def _path(context_id, storage_root, category):
    if not isinstance(context_id, str) or not re.fullmatch(r'ctx-[a-f0-9]{32}', context_id):
        raise ValueError('工具箱上下文编号无效。')
    folder = storage_path(storage_root) / '_integration' / category
    if folder.is_symlink():
        raise ValueError('工具箱存储目录应为本地普通目录。')
    path = folder / (context_id + '.json')
    if path.is_symlink():
        raise ValueError('工具箱存储应为本地普通文件。')
    return path


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n', 'utf-8')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read(context_id, storage_root, category):
    path = _path(context_id, storage_root, category)
    return json.loads(path.read_text('utf-8')) if path.is_file() else None


def _packet(context_id, context):
    library = context.get('library') or {}
    cards = (context.get('support_profile') or {}).get('cards') or []
    return {'context_id': context_id, 'context': deepcopy(context),
        'library_id': context.get('library_id'), 'library_is_local_copy': True,
        'summary': {'account': deepcopy(context.get('account')), 'server': context['server'],
            'entry_count': len(library.get('entries', [])), 'excluded_entry_count': library.get('excluded_entry_count', 0),
            'card_count': len(cards), 'skill_count': len(context.get('skill_targets') or []),
            'library_label': '工具箱本地副本', 'advisory_only': True}, 'advisory_only': True}


def dispatch(request, *, storage_root=None):
    action = request.get('action')
    with LOCK:
        if action == 'integration.context.import':
            context = _context(request.get('context'))
            context_id = 'ctx-' + uuid.uuid4().hex
            library = context.get('library')
            if library is not None:
                from hub.parents import dispatch as inventory
                source_id = library.pop('library_id', None)
                local_id = 'context-' + context_id[4:]
                imported = inventory({'action': 'inventory.import', 'server': context['server'],
                    'library_id': local_id, 'inventory': {**library, 'library_id': source_id,
                        'server': library.get('server') or context['server'], 'source': {'kind': 'maauma_context_copy',
                            'context_id': context_id, 'source_library_id': source_id,
                            'account_id': (context.get('account') or {}).get('id')}},
                    'import_default_status': 'unknown'}, storage_root=storage_path(storage_root))
                if imported.get('ok') is False:
                    return imported
                library.update(library_id=local_id, source_library_id=source_id, is_local_copy=True)
                context['library_id'] = local_id
            _write(_path(context_id, storage_root, 'contexts'), context)
            return _packet(context_id, context)
        context_id = request.get('context_id')
        context = _read(context_id, storage_root, 'contexts')
        if context is None:
            return {'ok': False, 'error': 'context_not_found', 'message': '本地上下文不存在，请从面板重新带入。'}
        if action == 'integration.context.get':
            return _packet(context_id, context)
        if action == 'integration.draft.save':
            draft = _draft(request.get('draft'))
            _write(_path(context_id, storage_root, 'drafts'), draft)
        elif action == 'integration.draft.get':
            draft = _read(context_id, storage_root, 'drafts')
        else:
            raise ValueError('未识别的工具箱接入操作。')
        return {'context_id': context_id, 'draft': draft, 'advisory_only': True,
                'saved_to_main_library': False, 'execution_authorized': False}
