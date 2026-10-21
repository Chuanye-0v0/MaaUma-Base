"""Shared team workflow entry: requirements, research and explicit local saves."""
from copy import deepcopy
from pathlib import Path
from core import parent_library as library_store
from core import parent_planning_state as parent_store
from core import team_planning_state as team_store
from core.team_requirements import normalize_requirements, parse_requirements
ROOT = Path(__file__).resolve().parents[1]
FIELDS = {'team_requirements_parse': {'request_text', 'overrides'}, 'team_guides_retrieve': {'requirements', 'guide_urls', 'allow_network'}, 'parent_planning_team_state': set(), 'parent_planning_team_save': {'team', 'expected_revision'}, 'parent_planning_team_delete': {'team_id', 'expected_revision'}}

def _success(value):
    return {'ok': True, 'results': [value]}

def _error(code, message):
    return {'ok': False, 'code': code, 'message': message, 'results': [{'code': code, 'message': message}]}

def _project(state, root, account_book_path):
    from desktop.parent_planning_api import _account_catalog
    library = library_store.load_library(root=root)
    existing = library_store.store_path(root).is_file()
    parent = parent_store.load_state(root)
    value = deepcopy(state)
    value.update(accounts=_account_catalog(parent, account_book_path or ROOT / 'runs/mfa-accounts.json'), library_id=library['library_id'] if existing else None, library_revision=library['revision'])
    for team in value['teams']:
        team['library_matches'] = team['library_id'] in (None, value['library_id'])
        team['warnings'] = [] if team['library_matches'] else ['原团队引用另一份种马库，成品编号保留，匹配前请重新关联。']
    return value

def dispatch_team(request, *, root=None, account_book_path=None, guide_retriever=None):
    if not isinstance(request, dict) or request.get('action') not in FIELDS or set(request) - FIELDS[request['action']] - {'action', 'request_id', 'mode', 'endpoint'}:
        return _error('team_planning_invalid_request', '团队规划请求字段无效，不接受文件路径或密钥。')
    action = request['action']
    try:
        if action == 'team_requirements_parse':
            return _success(parse_requirements(request.get('request_text', ''), request.get('overrides')))
        if action == 'team_guides_retrieve':
            from desktop.team_guide_retrieval import retrieve_guides
            requirements = normalize_requirements(request.get('requirements'))
            allowed = request.get('allow_network', False)
            if type(allowed) is not bool:
                raise ValueError('联网检索应为明确的开关。')
            retriever = guide_retriever or retrieve_guides
            result = retriever(requirements, guide_urls=request.get('guide_urls', []), allow_network=allowed)
            saved = team_store.save_research(root, requirements, result)
            return _success({**deepcopy(result), 'research_id': saved['research_id'], 'requirements': requirements, 'retrieved_at': saved['created_at']})
        state = team_store.load_state(root)
        if action == 'parent_planning_team_state':
            return _success(_project(state, root, account_book_path))
        expected = request.get('expected_revision')
        if type(expected) is not int or expected < 0:
            raise ValueError('保存团队前请先读取当前版本。')
        saved_id = None
        if action == 'parent_planning_team_save':
            team = deepcopy(request.get('team'))
            if not isinstance(team, dict):
                raise ValueError('团队草稿格式无效。')
            current = _project(state, root, account_book_path)
            selected = team.get('account_id')
            if selected is not None and selected not in {row['id'] for row in current['accounts']}:
                raise ValueError('所选账号已不存在，请刷新账号列表。')
            team.setdefault('library_id', current['library_id'])
            state, saved_id = team_store.upsert_team(state, team)
        else:
            state = team_store.delete_team(state, request.get('team_id'))
        state = team_store.save_state(state, root, expected_revision=expected)
        result = _project(state, root, account_book_path)
        if saved_id is not None:
            result['saved_team'] = next((row for row in result['teams'] if row['id'] == saved_id))
        return _success(result)
    except team_store.TeamPlanningConflict:
        return _error('team_planning_revision_conflict', '团队方案已被另一窗口修改，本次未覆盖；请刷新后再保存。')
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
        message = str(error) if isinstance(error, ValueError) and any(('一' <= c <= '鿿' for c in str(error))) else '团队资料暂未匹配，请检查需求卡或重新检索。'
        return _error('team_planning_invalid_data', message)
    except OSError:
        return _error('team_planning_storage_unavailable', '本地团队资料暂不可读写，请检查保存位置。')
