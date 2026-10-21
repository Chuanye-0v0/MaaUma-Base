"""Local planning sidecar, separate from observed horses and credentials.

Reads never create a store. Pure updates return a normalized copy; only
save_state performs a revision-checked atomic replacement of planning.json.
"""
from copy import deepcopy
import json
import os
import re
import uuid
from core.parent_library import store_path as library_store_path
from core.parent_local_route import normalize_local_route
SCHEMA = 'uma.parent_planning_state.v1'
_STATE_FIELDS = {'schema_version', 'revision', 'accounts', 'bindings', 'plans'}
_ACCOUNT_FIELDS = {'id', 'label', 'account_number'}
_BINDING_FIELDS = {'library_id', 'entry_id', 'owner_account_id', 'borrowable_by'}
_PLAN_FIELDS = {'id', 'name', 'account_id', 'library_id', 'target_character_id', 'selection_mode', 'parent_entry_ids', 'lineage', 'targets', 'raw_targets', 'candidate_entry_ids', 'include_unassigned', 'local_route'}

class PlanningConflict(ValueError):
    """The caller must refresh rather than overwrite another panel's changes."""

def _fields(value, allowed, label):
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(label + '字段不正确。')

def _id(value, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]{1,96}', value):
        raise ValueError('规划资料编号无效。')
    return value

def _text(value, *, blank=False):
    if not isinstance(value, str) or len(value) > 240 or (not blank and (not value.strip())):
        raise ValueError('规划名称或文字字段无效。')
    return value.strip()

def _ids(value):
    if not isinstance(value, list):
        raise ValueError('规划资料编号应为列表。')
    return list(dict.fromkeys((_id(item) for item in value)))

def _character(value):
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise ValueError('角色编号应为正整数，未知角色可留空。')
    return value

def _account(value):
    _fields(value, _ACCOUNT_FIELDS, '规划账号')
    result = {'id': _id(value.get('id')), 'label': _text(value.get('label'))}
    number = value.get('account_number')
    if number is not None:
        if type(number) is not int or number < 1:
            raise ValueError('日活账号编号应为正整数。')
        result['account_number'] = number
    return result

def _account_reference(value, accounts):
    identifier = _id(value, nullable=True)
    if identifier is not None and identifier not in accounts:
        raise ValueError('关联账号已不存在，请刷新账号清单后重试。')
    return identifier

def _binding(value, accounts):
    _fields(value, _BINDING_FIELDS, '成品账号关联')
    owner = _account_reference(value.get('owner_account_id'), accounts)
    borrowed = _ids(value.get('borrowable_by', []))
    if any((identifier not in accounts for identifier in borrowed)):
        raise ValueError('可借账号已不存在，请刷新账号清单后重试。')
    return {'library_id': _id(value.get('library_id')), 'entry_id': _id(value.get('entry_id')), 'owner_account_id': owner, 'borrowable_by': borrowed}

def _goals(value, *, raw=False):
    if not isinstance(value, list):
        raise ValueError('方案目标应为列表。')
    result = []
    for row in value:
        key = 'name' if raw else 'target_id'
        _fields(row, {key, 'min_stars', 'priority'}, '方案目标')
        normalized = {key: _text(row.get(key))}
        for field, default, upper in (('min_stars', 1, 9), ('priority', 3, 5)):
            number = row.get(field, default)
            if type(number) is not int or not 1 <= number <= upper:
                raise ValueError('目标星数或优先级超出有效范围。')
            normalized[field] = number
        result.append(normalized)
    return result

def _plan(value, accounts):
    _fields(value, _PLAN_FIELDS, '种马规划方案')
    parents = value.get('parent_entry_ids', [])
    if not isinstance(parents, list) or len(parents) not in (0, 2):
        raise ValueError('亲代草稿应为空列表或两个成品槽位，未选择的槽位可留空。')
    parents = [_id(identifier, nullable=True) for identifier in parents]
    if len(parents) == 2 and parents[0] is not None and (parents[0] == parents[1]):
        raise ValueError('两个亲代不能选择同一条成品记录。')
    lineage = value.get('lineage', [None] * 6)
    if not isinstance(lineage, list) or len(lineage) != 6:
        raise ValueError('手动家系草稿应保留六个节点，未知节点可留空。')
    selection_mode = value.get('selection_mode', 'entries')
    if not isinstance(selection_mode, str) or selection_mode not in {'entries', 'manual'}:
        raise ValueError('家系选择方式无效。')
    include = value.get('include_unassigned', False)
    if type(include) is not bool:
        raise ValueError('是否包含未关联资料应为开关。')
    targets = _goals(value.get('targets', []))
    raw_targets = _goals(value.get('raw_targets', []), raw=True)
    if len(targets) + len(raw_targets) > 200:
        raise ValueError('每份方案最多保存 200 个目标。')
    candidate_ids = value.get('candidate_entry_ids')
    return {'id': _id(value.get('id')), 'name': _text(value.get('name', ''), blank=True) or '未命名方案', 'account_id': _account_reference(value.get('account_id'), accounts), 'library_id': _id(value.get('library_id'), nullable=True), 'target_character_id': _character(value.get('target_character_id')), 'selection_mode': selection_mode, 'parent_entry_ids': parents, 'lineage': [_character(item) for item in lineage], 'targets': targets, 'raw_targets': raw_targets, 'candidate_entry_ids': None if candidate_ids is None else _ids(candidate_ids), 'include_unassigned': include, **({'local_route': normalize_local_route(value['local_route'])} if 'local_route' in value else {})}

def new_state():
    return {'schema_version': SCHEMA, 'revision': 0, 'accounts': [], 'bindings': [], 'plans': []}

def normalize_state(value):
    _fields(value, _STATE_FIELDS, '种马规划资料')
    revision = value.get('revision', 0)
    if value.get('schema_version') != SCHEMA or type(revision) is not int or revision < 0:
        raise ValueError('种马规划资料版本无效。')
    if any((not isinstance(value.get(field, []), list) for field in ('accounts', 'bindings', 'plans'))):
        raise ValueError('种马规划资料列表无效。')
    accounts = [_account(row) for row in value.get('accounts', [])]
    account_ids = {row['id'] for row in accounts}
    if len(account_ids) != len(accounts):
        raise ValueError('规划账号编号重复。')
    bindings = [_binding(row, account_ids) for row in value.get('bindings', [])]
    if len({(row['library_id'], row['entry_id']) for row in bindings}) != len(bindings):
        raise ValueError('同一成品的账号关联重复。')
    plans = [_plan(row, account_ids) for row in value.get('plans', [])]
    if len({row['id'] for row in plans}) != len(plans):
        raise ValueError('规划方案编号重复。')
    return {'schema_version': SCHEMA, 'revision': revision, 'accounts': accounts, 'bindings': bindings, 'plans': plans}

def state_path(root=None):
    return library_store_path(root).with_name('planning.json')

def load_state(root=None):
    path = state_path(root)
    if not path.exists():
        return new_state()
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError('本地种马规划资料格式损坏，请保留原文件后检查。') from None
    return normalize_state(value)

def save_state(value, root=None, *, expected_revision):
    result = normalize_state(value)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('保存规划前请先读取当前版本。')
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix('.lock')
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 384)
    except FileExistsError:
        raise PlanningConflict('种马规划正在另一面板保存，请稍后刷新。') from None
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        os.close(descriptor)
        current = load_state(root)
        if current['revision'] != expected_revision:
            raise PlanningConflict('种马规划已被另一面板修改，请刷新后重新保存。')
        result['revision'] = current['revision'] + 1
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        temporary.replace(path)
        return result
    finally:
        if temporary.exists():
            temporary.unlink()
        lock.unlink()

def upsert_account(state, account):
    result = normalize_state(state)
    _fields(account, _ACCOUNT_FIELDS, '规划账号')
    value = deepcopy(account)
    if not value.get('id'):
        value['id'] = 'manual-' + uuid.uuid4().hex
    index = next((i for i, row in enumerate(result['accounts']) if row['id'] == value['id']), None)
    prior = result['accounts'][index] if index is not None else {}
    value = _account({**prior, **value})
    if index is None:
        result['accounts'].append(value)
    else:
        result['accounts'][index] = value
    return result

def bind_entries(state, library_id, entry_ids, *, owner_account_id=None, borrowable_by=()):
    result = normalize_state(state)
    library_id = _id(library_id)
    identifiers = _ids(entry_ids)
    if not identifiers:
        raise ValueError('请至少选择一条需要关联账号的成品记录。')
    if not isinstance(borrowable_by, (list, tuple)):
        raise ValueError('可借账号应为列表。')
    account_ids = {row['id'] for row in result['accounts']}
    for identifier in identifiers:
        value = _binding({'library_id': library_id, 'entry_id': identifier, 'owner_account_id': owner_account_id, 'borrowable_by': list(borrowable_by)}, account_ids)
        index = next((i for i, row in enumerate(result['bindings']) if row['library_id'] == library_id and row['entry_id'] == identifier), None)
        if index is None:
            result['bindings'].append(value)
        else:
            result['bindings'][index] = value
    return result

def upsert_plan(state, plan):
    result = normalize_state(state)
    _fields(plan, _PLAN_FIELDS, '种马规划方案')
    value = deepcopy(plan)
    if not value.get('id'):
        value['id'] = 'plan-' + uuid.uuid4().hex
    index = next((i for i, row in enumerate(result['plans']) if row['id'] == value['id']), None)
    prior = result['plans'][index] if index is not None else {}
    value = _plan({**prior, **value}, {row['id'] for row in result['accounts']})
    if index is None:
        result['plans'].append(value)
    else:
        result['plans'][index] = value
    return result

def delete_plan(state, plan_id):
    result = normalize_state(state)
    identifier = _id(plan_id)
    if not any((row['id'] == identifier for row in result['plans'])):
        raise ValueError('该方案已不存在，请刷新方案清单。')
    result['plans'] = [row for row in result['plans'] if row['id'] != identifier]
    return result

def project_entries(state, library_id, entries, *, account_id=None, include_unassigned=False):
    state = normalize_state(state)
    library_id = _id(library_id, nullable=True)
    account_id = _id(account_id, nullable=True)
    if type(include_unassigned) is not bool or not isinstance(entries, list):
        raise ValueError('账号资料范围或成品列表无效。')
    bindings = {(row['library_id'], row['entry_id']): row for row in state['bindings']}
    accounts = {row['id']: row['label'] for row in state['accounts']}
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('成品列表无效。')
        identifier = _id(entry.get('entry_id'))
        binding = bindings.get((library_id, identifier), {})
        owner = binding.get('owner_account_id')
        borrowed = list(binding.get('borrowable_by', []))
        if account_id is None:
            availability, eligible, available, reference = ('reference', True, False, True)
            label = '未关联 · 仅作参考' if owner is None else accounts.get(owner, owner) + ' 的成品 · 全部资料参考'
        elif owner == account_id:
            availability, eligible, available, reference = ('owned', True, True, False)
            label = '本账号自有'
        elif account_id in borrowed:
            availability, eligible, available, reference = ('borrowed', True, True, False)
            label = '已明确可借'
        elif owner is None:
            availability = 'unassigned_reference' if include_unassigned else 'unassigned'
            eligible, available, reference = (include_unassigned, False, True)
            label = '未关联 · 纳入参考（不代表已拥有）' if include_unassigned else '未关联 · 当前未纳入'
        else:
            availability, eligible, available, reference = ('other_account', False, False, True)
            label = '其他账号所有 · 未开放借用'
        result.append({**deepcopy(entry), 'owner_account_id': owner, 'borrowable_by': borrowed, 'availability': availability, 'availability_label': label, 'eligible': eligible, 'available': available, 'reference_only': reference})
    return result

def eligible_entry_ids(state, library_id, entries, *, account_id=None, include_unassigned=False):
    return [row['entry_id'] for row in project_entries(state, library_id, entries, account_id=account_id, include_unassigned=include_unassigned) if row['eligible']]
