"""Account-scoped local breeding drafts and joint material comparison."""
from copy import deepcopy
import json
from pathlib import Path
import uuid
from core import parent_library as library_store
from core import parent_planning_state as planning
from core.parent_compatibility import CompatibilityCatalog, characters_from_roster, entry_summaries, evaluate_entries, evaluate_lineage, suggest_entries_joint
from core.parent_pair_coverage import goal_groups, pair_coverage_resolver
from core.parent_planner_catalog import ParentPlannerCatalog
from desktop.parent_route_api import FIELDS as ROUTE_FIELDS, dispatch_route
from desktop.parent_planning_ai_api import FIELDS as AI_FIELDS, dispatch_ai
from desktop.team_planning_api import FIELDS as TEAM_FIELDS, dispatch_team
from desktop.team_planning_ai import FIELDS as TEAM_AI_FIELDS, dispatch_team as dispatch_team_ai
ROOT = Path(__file__).resolve().parents[1]
FIELDS = {'parent_planning_state': set(), 'parent_planning_account_save': {'id', 'label', 'expected_revision'}, 'parent_planning_bind': {'entry_ids', 'owner_account_id', 'borrowable_by', 'expected_revision'}, 'parent_planning_save': {'plan', 'expected_revision'}, 'parent_planning_delete': {'plan_id', 'expected_revision'}, 'parent_planning_suggest': {'target_character_id', 'candidate_entry_ids', 'account_id', 'include_unassigned', 'targets', 'raw_targets', 'limit'}, 'parent_planning_evaluate': {'target_character_id', 'parent_entry_ids', 'lineage', 'account_id', 'include_unassigned', 'targets', 'raw_targets'}}
FIELDS.update(ROUTE_FIELDS)
FIELDS.update(AI_FIELDS)
FIELDS.update(TEAM_FIELDS)
FIELDS.update(TEAM_AI_FIELDS)

def _success(value):
    return {'ok': True, 'results': [value]}

def _error(code, message):
    return {'ok': False, 'code': code, 'message': message, 'results': [{'code': code, 'message': message}]}

def _account_catalog(state, account_book_path):
    """Only account numbers and notes, never credentials or login usernames."""
    rows = {}
    path = Path(account_book_path)
    if path.is_file():
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('账号列表资料过大，请先检查本机账号设置。')
        book = json.loads(path.read_text('utf-8-sig'))
        for account in book.get('accounts', []):
            number = account.get('number')
            if type(number) is not int or number < 1:
                continue
            note = account.get('note')
            note = note.strip()[:100] if isinstance(note, str) else ''
            identifier = 'account-' + str(number)
            rows[identifier] = {'id': identifier, 'label': '账号 ' + str(number) + (' · ' + note if note else ''), 'account_number': number}
    for account in state['accounts']:
        rows[account['id']] = deepcopy(account)
    return sorted(rows.values(), key=lambda row: (row.get('account_number') is None, row.get('account_number') or 0, row['label'], row['id']))

def _include_accounts(state, accounts, identifiers):
    result = state
    catalog = {row['id']: row for row in accounts}
    for identifier in dict.fromkeys((i for i in identifiers if i is not None)):
        if not isinstance(identifier, str) or identifier not in catalog:
            raise ValueError('所选账号已不存在，请刷新账号列表或手动添加。')
        result = planning.upsert_account(result, catalog[identifier])
    return result

def _project(state, library, entries, accounts, library_exists):
    value = {key: deepcopy(state[key]) for key in ('revision', 'bindings', 'plans')}
    value.update(accounts=accounts, library_id=library['library_id'] if library_exists else None, library_revision=library['revision'], entries=planning.project_entries(state, library['library_id'], entries), message='账号关联和方案仅保存在本机；未关联成品可作参考，不代表当前账号持有。')
    known = {row['entry_id'] for row in entries}
    for plan in value['plans']:
        plan['library_matches'] = plan.get('library_id') in (None, library['library_id'])
        from core.parent_local_route import route_entry_ids
        references = [i for i in plan.get('parent_entry_ids', []) if i is not None]
        references.extend(route_entry_ids(plan.get('local_route')))
        plan['missing_entry_ids'] = [i for i in references if i not in known]
        plan['warnings'] = []
        if not plan['library_matches']:
            plan['warnings'].append('方案引用另一份种马库；原成品编号已保留，请重新选择可用成品。')
        if plan['missing_entry_ids']:
            plan['warnings'].append('方案中的部分成品已不在当前库，未自动替换为同角色成品。')
    return value

def _goals(request, catalog):
    targets, raw = (request.get('targets', []), request.get('raw_targets', []))
    if not isinstance(targets, list) or not isinstance(raw, list):
        raise ValueError('目标清单格式无效，请重新选择。')
    if targets or raw:
        goal_groups({'targets': targets, 'raw_targets': raw}, catalog)
    return (targets, raw)

def dispatch(request, *, root=None, account_book_path=None, relations_path=None, roster_path=None, planner_catalog_path=None, race_catalog_path=None, aptitude_catalog_path=None, ai_profile_module=None, ai_transport_factory=None, ai_profile_root=None, skill_master_path=None, skill_sources_path=None, support_catalog_path=None, team_guide_retriever=None, team_seed_transport_factory=None, team_research_loader=None, team_inventory_loader=None):
    if not isinstance(request, dict) or request.get('action') not in FIELDS:
        return _error('parent_planning_invalid_request', '不支持这项账号种马规划操作。')
    action = request['action']
    if set(request) - FIELDS[action] - {'action', 'request_id', 'mode', 'endpoint'}:
        return _error('parent_planning_invalid_request', '规划只接受对应操作参数，不接受文件路径。')
    if action in TEAM_FIELDS:
        return dispatch_team(request, root=root, account_book_path=account_book_path, guide_retriever=team_guide_retriever)
    if action in TEAM_AI_FIELDS:
        return dispatch_team_ai(request, root=root, account_book_path=account_book_path, relations_path=relations_path, roster_path=roster_path, race_catalog_path=race_catalog_path, aptitude_catalog_path=aptitude_catalog_path, planner_catalog_path=planner_catalog_path, skill_master_path=skill_master_path, skill_sources_path=skill_sources_path, support_catalog_path=support_catalog_path, profile_module=ai_profile_module, profile_root=ai_profile_root, transport_factory=ai_transport_factory, seed_transport_factory=team_seed_transport_factory, research_loader=team_research_loader, inventory_loader=team_inventory_loader)
    if action in AI_FIELDS:
        return dispatch_ai(request, root=root, account_book_path=account_book_path, relations_path=relations_path, roster_path=roster_path, race_catalog_path=race_catalog_path, aptitude_catalog_path=aptitude_catalog_path, planner_catalog_path=planner_catalog_path, profile_module=ai_profile_module, transport_factory=ai_transport_factory, profile_root=ai_profile_root)
    if action in ROUTE_FIELDS:
        return dispatch_route(request, root=root, account_book_path=account_book_path, relations_path=relations_path, roster_path=roster_path, race_catalog_path=race_catalog_path, aptitude_catalog_path=aptitude_catalog_path)
    try:
        catalog = CompatibilityCatalog.load(relations_path or ROOT / 'data/parent_library/compatibility_relations.json')
        roster = json.loads(Path(roster_path or ROOT / 'core/autonomous_training/plant_roster.json').read_text('utf-8-sig'))
        characters = characters_from_roster(roster, catalog)
        library = library_store.load_library(root=root)
        entries = entry_summaries(library, characters, catalog)
        state = planning.load_state(root)
        accounts = _account_catalog(state, account_book_path or ROOT / 'runs/mfa-accounts.json')
        exists = library_store.store_path(root).is_file()
        if action == 'parent_planning_state':
            return _success(_project(state, library, entries, accounts, exists))
        if action in {'parent_planning_account_save', 'parent_planning_bind', 'parent_planning_save', 'parent_planning_delete'}:
            revision = request.get('expected_revision')
            if type(revision) is not int or revision < 0:
                raise ValueError('保存前请先读取当前规划版本。')
            saved_id = None
            if action == 'parent_planning_account_save':
                identifier = request.get('id') or 'manual-' + uuid.uuid4().hex
                account = next((deepcopy(a) for a in accounts if a['id'] == identifier), {'id': identifier})
                account['label'] = request.get('label')
                state = planning.upsert_account(state, account)
            elif action == 'parent_planning_bind':
                ids = request.get('entry_ids')
                if not isinstance(ids, list) or not ids or any((i not in {e['entry_id'] for e in entries} for i in ids)):
                    raise ValueError('请勾选当前种马库中的成品，再保存归属。')
                owner, borrowers = (request.get('owner_account_id'), request.get('borrowable_by', []))
                if not isinstance(borrowers, list):
                    raise ValueError('可借账号列表格式无效。')
                state = _include_accounts(state, accounts, [owner, *borrowers])
                state = planning.bind_entries(state, library['library_id'], ids, owner_account_id=owner, borrowable_by=borrowers)
            elif action == 'parent_planning_save':
                plan = deepcopy(request.get('plan'))
                if not isinstance(plan, dict):
                    raise ValueError('方案草稿格式无效。')
                _goals(plan, ParentPlannerCatalog.load(planner_catalog_path or ROOT / 'data/parent_library/planner_catalog.json'))
                plan.setdefault('library_id', library['library_id'] if exists else None)
                if plan.get('id') is None:
                    plan['id'] = uuid.uuid4().hex
                saved_id = plan['id']
                state = _include_accounts(state, accounts, [plan.get('account_id')])
                state = planning.upsert_plan(state, plan)
            else:
                state = planning.delete_plan(state, request.get('plan_id'))
            state = planning.save_state(state, root, expected_revision=revision)
            value = _project(state, library, entries, _account_catalog(state, account_book_path or ROOT / 'runs/mfa-accounts.json'), exists)
            if saved_id is not None:
                value['saved_plan'] = next((deepcopy(p) for p in state['plans'] if p['id'] == saved_id))
            return _success(value)
        account_id = request.get('account_id')
        if account_id is not None and account_id not in {a['id'] for a in accounts}:
            raise ValueError('所选账号已不存在，请刷新账号列表。')
        include = request.get('include_unassigned', False)
        if type(include) is not bool:
            raise ValueError('包含未关联参考应为开关值。')
        allowed = set(planning.eligible_entry_ids(state, library['library_id'], entries, account_id=account_id, include_unassigned=include))
        factor_catalog = ParentPlannerCatalog.load(planner_catalog_path or ROOT / 'data/parent_library/planner_catalog.json')
        targets, raw = _goals(request, factor_catalog)
        coverage = pair_coverage_resolver(library, targets, raw, factor_catalog) if targets or raw else None
        if action == 'parent_planning_evaluate':
            if ('parent_entry_ids' in request) == ('lineage' in request):
                raise ValueError('请选择库内成品家系或手动家系中的一种。')
            if 'lineage' in request:
                value = evaluate_lineage(request.get('target_character_id'), request['lineage'], catalog, characters)
                value['resource_message'] = '手动家系仅作相性参考，不代表该账号拥有对应成品。'
                return _success(value)
            ids = request['parent_entry_ids']
            if not isinstance(ids, list) or any((i not in allowed for i in ids)):
                raise ValueError('亲代不在该账号的自有、可借或已选择参考范围内，请重新选择。')
            value = evaluate_entries(request.get('target_character_id'), ids, entries, catalog, characters)
            if targets or raw:
                value['factor_coverage'] = coverage(*ids)
        else:
            selected = request.get('candidate_entry_ids')
            if selected is None:
                selected = sorted(allowed)
            elif not isinstance(selected, list) or any((i not in allowed for i in selected)):
                raise ValueError('部分候选不在当前账号范围内，请刷新候选清单。')
            value = suggest_entries_joint(request.get('target_character_id'), entries, catalog, characters, candidate_entry_ids=selected, limit=request.get('limit', 20), coverage_resolver=coverage if targets or raw else None)
        projected = {e['entry_id']: e for e in planning.project_entries(state, library['library_id'], entries, account_id=account_id, include_unassigned=include)}

        def annotate(result):
            ids = result.get('parent_entry_ids', [])
            result['reference_entry_ids'] = [i for i in ids if projected[i].get('reference_only')]
            both_borrowed = len(ids) == 2 and all((projected[i].get('availability') == 'borrowed' for i in ids))
            result['resource_status'] = 'requires_owned_material' if both_borrowed else 'reference' if result['reference_entry_ids'] else 'available'
            result['resource_issues'] = ['两位直接亲代都为可借成品，同一次配种不能同时借用；请先持有或培养其中一位。'] if both_borrowed else []
            if both_borrowed:
                from core.parent_local_route import BORROW_PAIR_REFERENCE
                result['resource_rule_reference'] = deepcopy(BORROW_PAIR_REFERENCE)
        if 'suggestions' in value:
            for result in value['suggestions']:
                annotate(result)
        else:
            annotate(value)
        value.update(account_id=account_id, eligible_entry_count=len(allowed), resource_message='未关联或未选择账号的成品仅作参考；相性和因子覆盖不代表继承概率。')
        return _success(value)
    except planning.PlanningConflict:
        return _error('parent_planning_revision_conflict', '规划已被另一窗口修改，请重新读取后再保存；本次未覆盖。')
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        message = str(error) if isinstance(error, ValueError) and any(('一' <= c <= '鿿' for c in str(error))) else '规划资料或参数暂未匹配，请刷新后重试。'
        return _error('parent_planning_invalid_data', message)
    except OSError:
        return _error('parent_planning_storage_unavailable', '本地规划资料暂不可读写，请检查保存位置。')

def main():
    import sys
    try:
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError()
        value = dispatch(json.loads(raw))
    except (ValueError, TypeError):
        value = _error('parent_planning_invalid_request', '规划请求格式无效或过大。')
    print(json.dumps(value, ensure_ascii=False))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
