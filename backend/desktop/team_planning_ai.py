"""Explicit, injectable team advice and local inventory; no automatic game action."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import time
from core import parent_library as library_store
from core import parent_planning_state as planning
from core import team_inventory as inventories
from core.parent_ai_planner import AIPlanningError, build_context, validate_suggestions, safe_text
from core.parent_aptitude import AptitudeCatalog
from core.parent_compatibility import CompatibilityCatalog, characters_from_roster
from core.parent_local_route import evaluate_local_route, normalize_local_route
from core.parent_planner_catalog import ParentPlannerCatalog
from core.parent_race_records import RaceCatalog
from core.parent_skill_reference import SkillReference
from core.team_planner import TeamCatalog, TEAM_SYSTEM_PROMPT, build_team_context, normalize_team_plan, validate_team_reply
from desktop import parent_planning_ai_api as pedigree
ROOT = Path(__file__).resolve().parents[1]
FIELDS = {'parent_planning_team_inventory_catalog': {'kind', 'query', 'limit'}, 'parent_planning_team_inventory_get': {'account_id'}, 'parent_planning_team_inventory_save': {'account_id', 'characters', 'support_cards', 'expected_revision'}, 'parent_planning_ai_team_propose': {'confirmed', 'requirements', 'research_id', 'selected_source_ids', 'account_id', 'library_id', 'expected_library_revision', 'expected_inventory_revision', 'expected_planning_revision', 'request_text'}, 'parent_planning_ai_team_member_seed': {'confirmed', 'team_plan', 'member_id', 'research_id', 'account_id', 'library_id', 'local_route'}}
TIMEOUT_SECONDS = 60
DATA_SCOPE = '仅在点击生成后发送已确认比赛需求、本次选中资料、角色/技能/支援卡目录和当前账号资源摘要；不发送账号ID/备注、成品编号、凭据、截图或本地路径。返回参考方案，本地核验，不自动保存或操作游戏。'

def _success(value):
    return {'ok': True, 'results': [value]}

def _error(code, message, **extra):
    return {'ok': False, 'code': code, 'message': message, 'results': [{'code': code, 'message': message, **extra}]}

def _load_library(root):
    exists = library_store.store_path(root).is_file()
    return (library_store.load_library(root=root) if exists else library_store.new_library(library_id='uninitialized-library'), exists)

def _accounts(state, path):
    from desktop.parent_planning_api import _account_catalog
    return _account_catalog(state, path)

def _account(account, state, account_book_path):
    accounts = _accounts(state, account_book_path)
    if account is not None and account not in {row['id'] for row in accounts}:
        raise AIPlanningError('team_planning_invalid_request', '当前规划账号已不存在，请刷新。')
    return accounts

def _research(root, identifier, requirements, selected, research_loader):
    from core.team_requirements import normalize_requirements, assess_requirements
    if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
        raise AIPlanningError('team_planning_invalid_sources', '请先检索并选择本次资料。')
    if research_loader is None:
        from core.team_planning_state import load_research
        research_loader = load_research
    saved = research_loader(root, identifier)
    if not isinstance(saved, dict) or saved.get('research_id') != identifier:
        raise AIPlanningError('team_planning_invalid_sources', '所选资料快照已变化，请重新检索。')
    req = normalize_requirements(requirements if requirements is not None else saved['requirements'])
    if req != normalize_requirements(saved['requirements']):
        raise AIPlanningError('team_planning_stale_research', '比赛需求与检索快照不同，请重新确认并检索。')
    assessment = assess_requirements(req)
    saved_conflicts = (saved.get('result', {}).get('requirements_assessment') or {}).get('conflicts', [])
    if assessment.get('conflicts') or saved_conflicts or not assessment.get('ready_for_planning'):
        raise AIPlanningError('team_planning_requirements_incomplete', '请先确认团队总人数或解决明确冲突；未知赛道和赛况可保留为条件化参考。')
    result = saved['result']
    rows = [*result.get('facts', []), *result.get('guides', [])]
    index = {row['source_id']: row for row in rows if isinstance(row, dict) and isinstance(row.get('source_id'), str)}
    if not isinstance(selected, list) or len(selected) > 32 or any((not isinstance(x, str) for x in selected)) or (len(set(selected)) != len(selected)) or any((x not in index for x in selected)):
        raise AIPlanningError('team_planning_invalid_sources', '所选来源不属于这次检索快照。')
    if any(((index[x].get('applicability') or {}).get('status') == 'out_of_scope' for x in selected)):
        raise AIPlanningError('team_planning_sources_out_of_scope', '所选资料含明确不属于本期比赛的来源，请取消勾选后再生成。')
    return (req, [deepcopy(index[x]) for x in selected])

def _inventory(root, account, loader):
    state = inventories.load_inventory(root)
    row = inventories.account_inventory(state, account)
    snapshot = loader(account) if loader is not None and account is not None else None
    if snapshot is not None:
        row = inventories.merge_explicit_snapshot(row, snapshot)
    token = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return (state, row, token)

def _revisions(library, exists, state, inventory, account):
    return {'library_id': library['library_id'] if exists else None, 'library_revision': library['revision'], 'planning_revision': state['revision'], 'inventory_revision': inventory['revision'], 'account_id': account}

def _check_current(root, account, expected, token, loader):
    library, exists = _load_library(root)
    state = planning.load_state(root)
    inventory, _, current_token = _inventory(root, account, loader)
    if _revisions(library, exists, state, inventory, account) != expected or token != current_token:
        raise AIPlanningError('team_planning_stale_context', '请求期间种马、账号关联或持有资料已变化，请刷新后重试。')

def _catalog(roster_path, relations_path, planner_catalog_path, skill_master_path, skill_sources_path, support_catalog_path, aptitude_catalog_path=None):
    compatibility = CompatibilityCatalog.load(relations_path or ROOT / 'data/parent_library/compatibility_relations.json')
    roster = json.loads(Path(roster_path or ROOT / 'core/autonomous_training/plant_roster.json').read_text('utf-8-sig'))
    characters = characters_from_roster(roster, compatibility)
    planner = ParentPlannerCatalog.load(planner_catalog_path or ROOT / 'data/parent_library/planner_catalog.json')
    skills = SkillReference.load(master_path=skill_master_path, sources_path=skill_sources_path, planner=planner)
    cards = json.loads(Path(support_catalog_path or ROOT / 'runs/support-card-library/catalog.json').read_text('utf-8-sig'))
    return (TeamCatalog(characters, skills, cards, AptitudeCatalog.load(aptitude_catalog_path)), planner)

def _catalog_result(catalog, request):
    kind, query, limit = (request.get('kind', 'all'), request.get('query', ''), request.get('limit', 100))
    if kind not in {'all', 'characters', 'support_cards'} or not isinstance(query, str) or len(query) > 240 or (type(limit) is not int) or (not 1 <= limit <= 500):
        raise ValueError('资源目录查询参数无效。')
    terms = query.casefold().split()
    characters = [deepcopy(row) for row in catalog.characters.values() if all((term in json.dumps(row, ensure_ascii=False).casefold() for term in terms))]
    cards = [{'card_id': cid, 'character_id': row.get('character_id'), 'rarity': row.get('rarity'), 'type': row.get('type'), 'name': row.get('name_cn') or '国服卡名待核对 #' + cid, 'title': row.get('title_cn') or ''} for cid, row in catalog.cards.items()]
    cards = [row for row in cards if all((term in json.dumps(row, ensure_ascii=False).casefold() for term in terms))]
    return {'characters': characters[:limit] if kind != 'support_cards' else [], 'support_cards': cards[:limit] if kind != 'characters' else [], 'character_total': len(characters), 'support_card_total': len(cards), 'message': '静态目录不代表当前账号持有；未收录名称可保留原名待核对。'}

def member_route_review(member, route, local_review, library, catalog, *, races, aptitudes):
    """Every aptitude/factor claim is computed locally, never read from the reply."""
    reviews = []
    characters = list(catalog.characters.values())
    for target in member['target_aptitudes']:
        trial = deepcopy(route)
        primary = route['aptitude_goal']
        baseline = primary['baseline_rank'] if primary['factor_key'] == target['factor_key'] else None
        trial['aptitude_goal'] = {'factor_key': target['factor_key'], 'target_rank': target['target_rank'], 'baseline_rank': baseline, 'target_variant_id': member['variant_id']}
        by_id = {row['entry_id']: row for row in library['entries']}
        available = {row['entry_id'] for row in local_review['route_nodes'] if row.get('usable') and row.get('kind') == 'entry'}
        projection = [{**row, 'available': row['entry_id'] in available, 'availability': 'owned' if row['entry_id'] in available else 'reference'} for row in by_id.values()]
        value = evaluate_local_route(library, projection, member['character_id'], trial, race_catalog=races, aptitude_catalog=aptitudes, characters=characters)
        reviews.append({'factor_key': target['factor_key'], 'target_rank': target['target_rank'], 'baseline': value['aptitude']['baseline'], 'plan': value['aptitude']['plan']})
    by_id = {row['entry_id']: row for row in library['entries']}
    nodes = []
    for row in local_review['route_nodes']:
        binding = row.get('record_binding') or {}
        entry = by_id.get(binding.get('entry_id'))
        if not row.get('usable') or entry is None:
            nodes.append(None)
        else:
            nodes.append(entry['self'] if binding['node'] == 'self' else entry['parents'][int(binding['node'][-1]) - 1])
    skills = []
    for skill in member['core_skills']:
        target = skill.get('seed_factor_target') or skill.get('lower_factor_target')
        known, unknown = ([], False)
        if target:
            for node in nodes:
                identifier = (node or {}).get('entry_id') or 'audit-node'
                evidence = library_store.assess_entry({'entry_id': identifier, 'self': node, 'parents': [None, None]}, [{'factor_key': target['factor_key'], 'min_stars': 1}], 'self')['checks'][0]
                known.append(0 if evidence['known_absent'] else evidence['known_stars'])
                unknown = unknown or evidence['uncertain']
        total = sum((x for x in known if x is not None)) if any((x is not None for x in known)) else None
        direct = skill.get('seed_factor_target') is not None
        skills.append({'skill_id': skill['skill_id'], 'name': skill['name'], 'acquisition': skill['acquisition'], 'factor_key': target['factor_key'] if target else None, 'known_stars': total, 'factor_available': total is not None and total >= 1, 'uncertain': unknown, 'direct_seed_target': direct, 'skill_obtained': False, 'status': 'factor_present' if direct and total is not None and (total >= 1) else 'factor_unknown' if direct and (total is None or unknown) else 'factor_missing' if direct else 'skill_unverified' if skill['skill_id'] is None else 'training_required', 'message': '因子素材存在不表示技能已继承；金技能的下位白因子不等于金技能本体。'})
    return {'aptitudes': reviews, 'skills': skills, 'training_required': [row['name'] for row in skills if row['status'] == 'training_required'], 'missing_data': [row['factor_key'] + '：' + row['plan']['status'] for row in reviews if row['plan']['status'] != 'satisfied'] + [row['name'] + '：' + row['status'] for row in skills if row['status'] != 'factor_present']}

def dispatch_team(request, *, root=None, account_book_path=None, relations_path=None, roster_path=None, race_catalog_path=None, aptitude_catalog_path=None, planner_catalog_path=None, skill_master_path=None, skill_sources_path=None, support_catalog_path=None, profile_root=None, profile_module=None, transport_factory=None, seed_transport_factory=None, research_loader=None, inventory_loader=None):
    if not isinstance(request, dict) or request.get('action') not in FIELDS or set(request) - FIELDS.get(request.get('action'), set()) - {'action', 'request_id', 'mode', 'endpoint'}:
        return _error('team_planning_invalid_request', '团队规划请求字段无效，不接受路径、密钥或前端伪造来源实体。')
    started = time.monotonic()
    try:
        action = request['action']
        state = planning.load_state(root)
        account = request.get('account_id')
        accounts = _account(account, state, account_book_path or ROOT / 'runs/mfa-accounts.json')
        inventory, resource, token = _inventory(root, account, inventory_loader)
        if action == 'parent_planning_team_inventory_save':
            if account is None or not any((k in request for k in ('characters', 'support_cards'))):
                raise ValueError('请选择明确账号，并提供角色或支援卡清单。')
            updated = inventories.upsert_account_inventory(inventory, account, characters=request.get('characters'), support_cards=request.get('support_cards'))
            inventory = inventories.save_inventory(updated, root, expected_revision=request.get('expected_revision'))
            resource = inventories.account_inventory(inventory, account)
        if action in {'parent_planning_team_inventory_get', 'parent_planning_team_inventory_save'}:
            return _success({**resource, 'inventory_revision': inventory['revision'], 'message': '未知持有、等级和突破保持未知；完整清单未列出才可判为确无。'})
        catalog, planner = _catalog(roster_path, relations_path, planner_catalog_path, skill_master_path, skill_sources_path, support_catalog_path, aptitude_catalog_path)
        if action == 'parent_planning_team_inventory_catalog':
            return _success(_catalog_result(catalog, request))
        if request.get('confirmed') is not True:
            raise ValueError('请先确认比赛需求或整套团队方案，再发起AI请求。')
        team = normalize_team_plan(request['team_plan']) if action.endswith('member_seed') else None
        research_id = team['research_id'] if team else request.get('research_id')
        if team and request.get('research_id') not in (None, research_id):
            raise ValueError('团队方案与检索快照不同。')
        req, evidence = _research(root, research_id, team['requirements'] if team else request.get('requirements'), team['selected_source_ids'] if team else request.get('selected_source_ids', []), research_loader)
        library, exists = _load_library(root)
        revisions = _revisions(library, exists, state, inventory, account)
        if request.get('library_id') not in (None, revisions['library_id']):
            raise ValueError('所选种马库已变化，请刷新。')
        for field, key in (('expected_library_revision', 'library_revision'), ('expected_inventory_revision', 'inventory_revision'), ('expected_planning_revision', 'planning_revision')):
            if field in request and (type(request[field]) is not int or request[field] != revisions[key]):
                raise AIPlanningError('team_planning_stale_context', '资料版本已变化，请刷新后再规划。')
        if team and team['context_revisions'] != revisions:
            raise AIPlanningError('team_planning_stale_context', '团队方案采用的种马或持有资料版本已变化，请重新核对团队。')
        profile, home = (pedigree._profile(profile_module), pedigree._home(profile_root))
        provider = pedigree._provider(profile, home, check_credentials=False)
        if not provider['configured']:
            raise AIPlanningError('parent_ai_not_configured', provider['message'])
        private = [library['library_id'], library.get('name', '')]
        private.extend((text for row in accounts for text in (row['id'], row['label'])))
        bundle = build_team_context(req, evidence, catalog, resource, private_values=private, request_text=team.get('request_text', '') if team else request.get('request_text', ''))
        if team is None:
            transport = transport_factory(profile, home) if transport_factory else pedigree.OpenAIPlanningTransport(profile, home, system_prompt=TEAM_SYSTEM_PROMPT, max_tokens=6000, max_timeout_seconds=TIMEOUT_SECONDS)
            reply = transport.propose(bundle['payload'], timeout_seconds=TIMEOUT_SECONDS)
            _check_current(root, account, revisions, token, inventory_loader)
            team = validate_team_reply(reply, bundle, research_id=research_id, context_revisions=revisions)
            return _success({'team_plan': team, 'member_routes': {}, 'missing_data': list(dict.fromkeys((text for member in team['members'] for text in member['missing_data']))), 'warnings': ['这是单套团队参考方案，不表示已拥有角色或可执行完整卡组。'], 'provider': {'model': provider['model'], 'status': 'success', 'elapsed_ms': round((time.monotonic() - started) * 1000)}, 'data_scope': DATA_SCOPE})
        proposal = {'team_plan': {'title': team['title'], 'summary': team['summary'], 'members': []}}
        for member in team['members']:
            proposal['team_plan']['members'].append({k: deepcopy(member[k]) for k in ('role', 'character_id', 'variant_id', 'raw_character_name', 'running_style', 'target_aptitudes', 'source_ids', 'notes')})
            proposal['team_plan']['members'][-1].update(core_skills=[{k: deepcopy(s[k]) for k in ('skill_id', 'raw_name', 'purpose', 'source_ids')} for s in member['core_skills']], support_cards=[{k: deepcopy(c[k]) for k in ('card_id', 'raw_name', 'role', 'borrowed')} for c in member['support_cards']])
        checked = validate_team_reply(proposal, bundle, research_id=research_id, context_revisions=revisions)
        member = next((row for row in checked['members'] if row['member_id'] == request.get('member_id')), None)
        if member is None or member['character_id'] is None or (not member['target_aptitudes']):
            raise ValueError('请先选择已对应本地角色和适性目标的团队成员。')
        primary = member['target_aptitudes'][0]
        route = normalize_local_route(request.get('local_route')) or normalize_local_route({'aptitude_goal': {**primary, 'baseline_rank': None, 'target_variant_id': member['variant_id']}})
        if route['aptitude_goal']['factor_key'] != primary['factor_key'] or route['aptitude_goal']['target_rank'] != primary['target_rank'] or route['aptitude_goal']['target_variant_id'] not in (None, member['variant_id']):
            raise ValueError('当前种马草稿与该团队成员目标不同，请先确认对应成员。')
        projected = planning.project_entries(state, library['library_id'], library['entries'], account_id=account)
        races, aptitudes = (RaceCatalog.load(race_catalog_path), catalog.aptitudes)
        review = evaluate_local_route(library, projected, member['character_id'], route, race_catalog=races, aptitude_catalog=aptitudes, characters=list(catalog.characters.values()))
        seed_bundle = build_context(library, projected, list(catalog.characters.values()), member['character_id'], route, '按已确认团队成员的适性和技能继承路线匹配种马；不能把金技能当父辈因子。', factor_keys={row['factor_key'] for row in planner.targets()}, race_catalog=races, private_values=private, local_goal_assessment=review['aptitude'], aptitude_catalog=aptitudes)
        seed_bundle['payload']['confirmed_team_member'] = {key: deepcopy(member[key]) for key in ('character_id', 'variant_id', 'running_style', 'target_aptitudes', 'source_ids')}
        seed_bundle['payload']['confirmed_team_member']['core_skills'] = [{key: deepcopy(s.get(key)) for key in ('skill_id', 'name', 'acquisition', 'seed_factor_target', 'lower_factor_target', 'source_ids')} for s in member['core_skills']]
        seed_bundle['payload']['team_evidence'] = [deepcopy(row) for row in bundle['payload']['evidence'] if row['source_id'] in member['source_ids']]
        seed_bundle['payload']['team_preferences'] = bundle['request_text']
        factory = seed_transport_factory or transport_factory
        transport = factory(profile, home) if factory else pedigree.OpenAIPlanningTransport(profile, home, system_prompt=pedigree.SYSTEM_PROMPT + '还要依据confirmed_team_member中的多适性与技能继承路线给出草稿，所有已知缺口由本地重新核验。', max_tokens=6000, max_timeout_seconds=TIMEOUT_SECONDS)
        reply = transport.propose(seed_bundle['payload'], timeout_seconds=TIMEOUT_SECONDS)
        _check_current(root, account, revisions, token, inventory_loader)
        evaluate = lambda candidate: evaluate_local_route(library, projected, member['character_id'], candidate, race_catalog=races, aptitude_catalog=aptitudes, characters=list(catalog.characters.values()))
        result = validate_suggestions(reply, seed_bundle, evaluate)
        for candidate in result['candidates']:
            candidate['member_review'] = member_route_review(member, candidate['local_route'], candidate['local_review'], library, catalog, races=races, aptitudes=aptitudes)
        result.update(member_id=member['member_id'], member_targets=deepcopy(member), context_revisions=revisions, provider={'model': provider['model'], 'status': 'success', 'elapsed_ms': round((time.monotonic() - started) * 1000)}, data_scope=DATA_SCOPE)
        return _success(result)
    except inventories.InventoryConflict as error:
        return _error('team_inventory_revision_conflict', str(error))
    except AIPlanningError as error:
        return _error(error.code, error.message)
    except ValueError as error:
        text = str(error)
        return _error('team_planning_invalid_request', text if any(('一' <= c <= '鿿' for c in text)) else '团队资料或参数无效，请刷新后重试。')
    except Exception:
        return _error('team_planning_request_failed', 'AI接口或本地资料暂不可用；未修改原团队、种马或资源资料。')
