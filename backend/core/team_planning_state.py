"""Local team projects and traceable research snapshots, separate from horse facts."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import uuid
from core.parent_library import store_path
from core.parent_local_route import normalize_local_route
from core.team_requirements import normalize_requirements
SCHEMA = 'uma.team_planning_state.v1'
RESEARCH_SCHEMA = 'uma.team_research_snapshot.v1'
TEAM_FIELDS = {'id', 'name', 'request_text', 'account_id', 'library_id', 'requirements', 'research_id', 'selected_source_ids', 'team_plan', 'member_routes', 'created_at', 'updated_at', 'library_matches', 'warnings'}

class TeamPlanningConflict(ValueError):
    pass

def _id(value, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]{1,96}', value):
        raise ValueError('团队方案编号无效。')
    return value

def _text(value, limit=2000):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError('团队方案文字格式或长度无效。')
    return value.strip()

def _sources(value):
    if not isinstance(value, list) or len(value) > 40 or any((not isinstance(item, str) or not 1 <= len(item) <= 200 for item in value)):
        raise ValueError('请选择本次检索结果中的来源。')
    return list(dict.fromkeys(value))

def _research_id(value, nullable=True):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise ValueError('检索记录编号无效，请重新检索。')
    return value

def normalize_team(value):
    if not isinstance(value, dict) or set(value) - TEAM_FIELDS:
        raise ValueError('团队方案字段无效。')
    requirements = normalize_requirements(value.get('requirements'))
    plan = value.get('team_plan')
    if plan is not None:
        from core.team_planner import normalize_team_plan
        plan = normalize_team_plan(plan)
        if plan['requirements'] != requirements:
            raise ValueError('比赛条件已修改，请重新生成团队方案后再保存。')
    routes = value.get('member_routes', {})
    member_ids = {row['member_id'] for row in (plan or {}).get('members', [])}
    if not isinstance(routes, dict) or set(routes) - member_ids:
        raise ValueError('成员系谱不属于当前团队方案。')
    normalized_routes = {_id(key): normalize_local_route(route) for key, route in routes.items()}
    result = {'id': _id(value.get('id')), 'name': _text(value.get('name', ''), 160) or '未命名团队', 'request_text': _text(value.get('request_text', '')), 'account_id': _id(value.get('account_id'), True), 'library_id': _id(value.get('library_id'), True), 'requirements': requirements, 'research_id': _research_id(value.get('research_id')), 'selected_source_ids': _sources(value.get('selected_source_ids', [])), 'team_plan': plan, 'member_routes': normalized_routes, 'created_at': _text(value.get('created_at', ''), 64), 'updated_at': _text(value.get('updated_at', ''), 64)}
    if plan is not None and (plan.get('research_id') != result['research_id'] or set(plan.get('selected_source_ids', [])) != set(result['selected_source_ids'])):
        raise ValueError('团队方案的攻略来源已变化，请重新生成后再保存。')
    return result

def new_state():
    return {'schema_version': SCHEMA, 'revision': 0, 'teams': []}

def normalize_state(value):
    if not isinstance(value, dict) or set(value) - {'schema_version', 'revision', 'teams'}:
        raise ValueError('团队方案资料格式无效。')
    if value.get('schema_version') != SCHEMA or type(value.get('revision')) is not int or value['revision'] < 0:
        raise ValueError('团队方案资料版本无效。')
    teams = value.get('teams')
    if not isinstance(teams, list) or len(teams) > 100:
        raise ValueError('团队方案清单无效或超过100份。')
    rows = [normalize_team(row) for row in teams]
    if len({row['id'] for row in rows}) != len(rows):
        raise ValueError('团队方案编号重复。')
    return {'schema_version': SCHEMA, 'revision': value['revision'], 'teams': rows}

def state_path(root=None):
    return store_path(root).with_name('team-plans.json')

def load_state(root=None):
    path = state_path(root)
    if not path.is_file():
        return new_state()
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('团队方案资料过大，请先检查本机保存文件。')
    return normalize_state(json.loads(path.read_text('utf-8-sig')))

def upsert_team(state, team):
    result = normalize_state(state)
    if not isinstance(team, dict):
        raise ValueError('团队草稿格式无效。')
    value = deepcopy(team)
    value['id'] = value.get('id') or uuid.uuid4().hex
    prior = next((row for row in result['teams'] if row['id'] == value['id']), {})
    now = datetime.now(timezone.utc).isoformat()
    value = normalize_team({**prior, **value, 'created_at': prior.get('created_at') or now, 'updated_at': now})
    result['teams'] = [value if row['id'] == value['id'] else row for row in result['teams']]
    if not prior:
        result['teams'].append(value)
    return (normalize_state(result), value['id'])

def delete_team(state, team_id):
    result = normalize_state(state)
    team_id = _id(team_id)
    if not any((row['id'] == team_id for row in result['teams'])):
        raise ValueError('团队方案已不存在，请刷新后重试。')
    result['teams'] = [row for row in result['teams'] if row['id'] != team_id]
    return result

def save_state(value, root=None, *, expected_revision):
    result = normalize_state(value)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('保存团队前请先读取当前版本。')
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix('.lock')
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 384)
    except FileExistsError:
        raise TeamPlanningConflict('团队方案正在另一窗口保存，请稍后刷新。') from None
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        os.close(descriptor)
        current = load_state(root)
        if current['revision'] != expected_revision:
            raise TeamPlanningConflict('团队方案已被另一窗口修改，本次未覆盖。')
        result['revision'] = current['revision'] + 1
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), 'utf-8')
        temporary.replace(path)
        return result
    finally:
        if temporary.exists():
            temporary.unlink()
        lock.unlink()

def save_research(root, requirements, result):
    requirements = normalize_requirements(requirements)
    if not isinstance(result, dict) or not isinstance(result.get('facts', []), list) or (not isinstance(result.get('guides', []), list)):
        raise ValueError('检索结果格式无效。')
    body = json.dumps(result, ensure_ascii=False, allow_nan=False)
    if len(body.encode('utf-8')) > 1024 * 1024:
        raise ValueError('检索结果过大，本次未存档。')
    identifier = uuid.uuid4().hex
    snapshot = {'schema_version': RESEARCH_SCHEMA, 'research_id': identifier, 'requirements': requirements, 'result': deepcopy(result), 'created_at': datetime.now(timezone.utc).isoformat()}
    path = state_path(root).parent / 'team-research' / (identifier + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 384)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(snapshot, stream, ensure_ascii=False, allow_nan=False)
    return snapshot

def load_research(root, research_id):
    identifier = _research_id(research_id, False)
    path = state_path(root).parent / 'team-research' / (identifier + '.json')
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('检索记录已不存在，请重新检索攻略。')
    result = json.loads(path.read_text('utf-8-sig'))
    if not isinstance(result, dict) or result.get('schema_version') != RESEARCH_SCHEMA or result.get('research_id') != identifier:
        raise ValueError('检索记录版本不匹配，请重新检索。')
    result['requirements'] = normalize_requirements(result['requirements'])
    return result
