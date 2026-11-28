"""Read-only pilot for the account-scoped Kawasaki plan and optional AI ranking."""
from core import parent_library as store
from core import parent_planning_state as planning
from core.parent_own_career import (CORE_WHITE, GREEN_PASSIVES, OPENING_REFERENCE_RANKS,
    AncestorLockError, body_green_stars, body_scenario, green_dye_count, own_career_lock_options,
    plan_own_career)
from desktop.parent_planning_ai_api import OpenAIPlanningTransport, TIMEOUT_SECONDS, _home, _profile, _provider


TEMPLATE = 'kawasaki_2100_front'
PROFILE = 'nox-account'
RANKING_PROMPT = (
    '复排川崎2100米逃马系谱草稿。给定候选均由本地程序生成并保留固定父辈、'
    '正式祖辈自养、借马只染色、本体26胜门槛与27胜目标。'
    '保留locked_ancestors。共同G1未确认就视为未知。'
    '同一技能的本体和家系星数分别判断，染色素材只提供来源，不能视为正式因子。'
    '小林固有只计比赛中实际触发的绿技能。候选绿技能是规划目标，原生和条件技能不作为必须染色的因子。'
    '优先考虑可确认的共同G1、改造难度和相性。'
    '候选中的事实来自本地资料。不得增加、删除或修改候选及其中的槽位、胜场、因子、装备和状态。'
    '仅返回JSON对象，含order和priority两个字段。order是给定route_id按推荐顺序排列的完整列表；'
    'priority只能是common_g1、transformation_cost、affinity、balanced之一。'
)
PRIORITIES = {
    'common_g1': '优先比较已确认的共同 G1',
    'transformation_cost': '优先比较适性改造需求',
    'affinity': '优先比较基础相性',
    'balanced': '综合比较相性、改造需求和胜鞍证据',
}


def _body_white_stars(node):
    return {row['factor_key']: row['stars'] for row in node.get('factors', [])
            if row.get('factor_key') in CORE_WHITE and (row.get('stars') or 0) > 0}


def _external_dye_references(state):
    """Read already imported snapshots; their totals never enter formal inheritance."""
    references = {}
    for plan in state['plans']:
        route = plan.get('local_route')
        if plan['account_id'] != PROFILE or plan['library_id'] != PROFILE or not route:
            continue
        for slot in route['nodes']:
            if not slot or slot['kind'] != 'external':
                continue
            entry = slot['entry']
            if not entry['entry_id'].startswith('mali-'):
                continue
            totals = {row['factor_key']: row['family_stars'] for row in slot['reference_totals']}
            whites = [key for key in CORE_WHITE if key in totals]
            greens = [key for key in GREEN_PASSIVES if (totals.get(key) or 0) > 0]
            dirt, middle = totals.get('red:泥地'), totals.get('red:中距离')
            if dirt or middle or whites or greens:
                references[entry['entry_id']] = {
                    'label': entry['character_name'] or '吗哩吗哩候选',
                    'variant_id': entry['variant_id'],
                    'body_scenario': 'grand_masters' if body_scenario(entry['self']) else None,
                    'dirt_family_stars': dirt,
                    'middle_family_stars': middle,
                    'white_factor_keys': whites,
                    'green_factor_keys': greens,
                    'body_white_stars': _body_white_stars(entry['self']),
                    'body_green_stars': body_green_stars(entry['self']),
                    'family_white_stars': {key: totals[key] for key in whites},
                    'borrow_confirmed': False,
                }
    return sorted(references.values(),
        key=lambda row: (-(row['dirt_family_stars'] or 0), -len(row['white_factor_keys']),
            -len(row['body_white_stars']), -green_dye_count(row['green_factor_keys']),
            -(row['middle_family_stars'] or 0), row['label']))[:6]


def _factor_evidence(library, references):
    local = [_body_white_stars(entry['self']) for entry in library['entries']]
    local_greens = [body_green_stars(entry['self']) for entry in library['entries']]
    external = [row['body_white_stars'] for row in references]
    return {
        'local_entry_count': len(local),
        'local_body_all_three_count': sum(all(key in row for key in CORE_WHITE[:3]) for row in local),
        'local_body_all_four_count': sum(all(key in row for key in CORE_WHITE) for row in local),
        'local_body_counts_by_factor': {key: sum(key in row for row in local) for key in CORE_WHITE},
        'local_body_green_counts_by_factor': {key: sum(key in row for row in local_greens) for key in GREEN_PASSIVES},
        'external_reference_count': len(external),
        'external_body_all_three_count': sum(all(key in row for key in CORE_WHITE[:3]) for row in external),
        'external_body_all_four_count': sum(all(key in row for key in CORE_WHITE) for row in external),
        'external_body_green_counts_by_factor': {
            key: sum(key in row['body_green_stars'] for row in references) for key in GREEN_PASSIVES},
    }


def _display_summary(route):
    route['affinity_detail'] = route['affinity']
    if route['affinity_detail'] is not None:
        route['affinity'] = route['affinity_detail']['total']
    if route.get('transformation_cost') is not None:
        route['transformation_detail'] = route['transformation_cost']
        route['transformation_cost'] = route['transformation_detail']['starting_red_star_total']


def _rank(value, *, profile_module, profile_root, transport_factory):
    profile = _profile(profile_module)
    home = _home(profile_root)
    provider = _provider(profile, home)
    if not provider['configured']:
        return {'status': 'not_configured', 'order': [], 'reason': provider['message']}

    candidates = [{
        'route_id': route['route_id'],
        'slots': [{key: slot[key] for key in ('slot', 'character_id', 'variant_id', 'red_target', 'status', 'locked')}
                  for slot in route['slots']],
        'affinity': route['affinity'],
        'transformation_cost': route['transformation_cost'],
        'common_g1': [
            edge['confirmed_count'] if edge.get('confirmed_count') is not None else None
            for edge in route['common_g1']
        ],
    } for route in value['routes']]
    payload = {'template_id': TEMPLATE, 'event': '川崎2100米 LoH · 逃马',
        'goals': ['小林历奇', '三走位', '拔得头筹', '开局改泥', '局中争取中距离S'],
        'green_targets': {'active_target': value['factor_targets']['green_active_target'],
                          'skills': value['factor_targets']['green_skills']},
        'locked_ancestors': value['locked_ancestors'],
        'upstream_dye_only': [{key: row[key] for key in ('variant_id', 'body_scenario',
            'dirt_family_stars', 'middle_family_stars', 'white_factor_keys', 'green_factor_keys',
            'body_white_stars', 'family_white_stars', 'body_green_stars')}
            for row in value['external_dye_references']],
        'candidates': candidates}
    try:
        transport = (transport_factory(profile, home) if transport_factory else
            OpenAIPlanningTransport(profile, home, system_prompt=RANKING_PROMPT, max_tokens=450))
        reply = transport.propose(payload, timeout_seconds=TIMEOUT_SECONDS)
    except Exception:
        return {'status': 'failed', 'order': [], 'reason': 'AI 接口暂不可用，已保留本地推荐。'}

    ids = {item['route_id'] for item in candidates}
    if isinstance(reply, dict) and set(reply) == {'order', 'priority'}:
        order, priority = reply['order'], reply['priority']
        if (isinstance(order, list) and all(isinstance(i, str) for i in order)
                and len(order) == len(ids) and set(order) == ids
                and isinstance(priority, str) and priority in PRIORITIES):
            return {'status': 'success', 'order': order, 'reason': PRIORITIES[priority],
                'model': provider['model']}
    return {'status': 'invalid_output', 'order': [], 'reason': 'AI 返回的候选顺序无效，已保留本地推荐。'}


def dispatch_own_career(request, *, root=None, profile_module=None, profile_root=None, transport_factory=None):
    if request.get('action') == 'parent_planning_own_career_lock_options':
        if request.get('template_id') != TEMPLATE:
            return {'ok': False, 'code': 'parent_own_career_invalid_request', 'message': '请选择川崎自养方案。'}
        try:
            return {'ok': True, 'results': [own_career_lock_options()]}
        except (OSError, ValueError):
            return {'ok': False, 'code': 'parent_own_career_unavailable', 'message': '本地衣装目录暂不可用。'}
    use_ai = request.get('use_ai', False)
    if request.get('template_id') != TEMPLATE or request.get('profile_key') != PROFILE or type(use_ai) is not bool:
        return {'ok': False, 'code': 'parent_own_career_invalid_request',
            'message': '请在川崎自养样例中选择夜神账号和本地或 AI 规划。'}
    if root is None:
        root = store.store_path().parent / 'accounts' / PROFILE
    try:
        library = store.load_library(root=root)
        if library['library_id'] != PROFILE:
            return {'ok': False, 'code': 'parent_own_career_library_missing',
                'message': '这台电脑未找到夜神的独立种马库。'}
        state = planning.load_state(root)
        projected = planning.project_entries(state, PROFILE, library['entries'], account_id=PROFILE)
        value = plan_own_career(library, projected_entries=projected,
            locked_ancestors=request.get('locked_ancestors'))
        references = _external_dye_references(state)
        evidence = _factor_evidence(library, references)
        green_observations = [f'{key.removeprefix("white:")} {count} 匹'
                              for key, count in evidence['local_body_green_counts_by_factor'].items() if count]
        for route in value['routes']:
            route['reasons'] = [
                f"与人工范本相同的槽位有 {route['reference_slot_matches']}/6 个。相性为基础值，胜鞍单列。",
                '改造成本为六个角色各自准备育成时的参考红星合计。',
                '小林历奇固有按比赛中实际触发的绿技能计数，最多计 6 个。冬季优先准备川崎赛场、急弯、逆时针、冬季优俊少女、交流重赏和擅长迈步。',
                '小林觉醒 4 级有交流重赏，按本期比赛标记计。觉醒 2 级有擅长迈步，泥地基础力量达到 1000 才触发。',
                '非冬季可用自信十足替换冬季优俊少女，川崎中距要求基础速度和力量各达到 1000。',
                '急弯可用 SR 毅力小林历奇（20064）的「总有某人在某处守护着你的幸福」选项 1 在种马阶段补。',
                '培养顺序为 ' + ' → '.join(value['training_order']) + '。',
            ]
            if green_observations:
                route['reasons'].append('夜神库本体已识别到' + '、'.join(green_observations) + '。')
            for slot in route['slots']:
                if slot['variant_id'] not in OPENING_REFERENCE_RANKS:
                    continue
                fit = slot['race_fit']
                ranks, stars = fit['starting_ranks_after_reference_red'], fit['starting_red_stars']
                route['reasons'].append(
                    f"{slot['label']}开局参考为泥地 {ranks['dirt']}（{stars['dirt']}★）和英里 {ranks['mile']}（{stars['mile']}★）。"
                    '赛程估算只计场地及距离适性达到 B 的目录回合，实际可跑场次另核。')
            _display_summary(route)
        _display_summary(value['reference'])
        value['qualification']['reason'] = (
            '正式父祖都须是女神杯自有成品。本体至少确认 26 胜，目标 27 胜，按槽位核对红因子。')
        if references:
            dye_note = f'上游染色有 {len(references)} 条吗哩吗哩候选，借用资格待游戏内核对。'
            confirmed = sum(row['body_scenario'] == 'grand_masters' for row in references)
            scenario_note = f'候选中有 {confirmed} 匹的本体女神杯特征已确认，其余待核。'
            for route in value['routes']:
                route.setdefault('reasons', []).extend([dye_note, scenario_note])
        value.update(profile_label='夜神的号', library_entry_count=len(library['entries']),
            external_dye_references=references,
            factor_evidence=evidence,
            ai={'status': 'not_requested', 'order': [], 'reason': ''})
        if use_ai and value['routes']:
            value['ai'] = _rank(value, profile_module=profile_module, profile_root=profile_root,
                transport_factory=transport_factory)
            if value['ai']['status'] == 'success':
                current = store.load_library(root=root)
                if (current['library_id'], current['revision'], planning.load_state(root)['revision']) != (
                        PROFILE, library['revision'], state['revision']):
                    return {'ok': False, 'code': 'parent_own_career_stale',
                        'message': '复排期间种马库或账号关联有变化，请重新生成。'}
        return {'ok': True, 'results': [value]}
    except AncestorLockError as error:
        return {'ok': False, 'code': 'parent_own_career_invalid_locks', 'message': str(error)}
    except (OSError, ValueError, KeyError, TypeError):
        return {'ok': False, 'code': 'parent_own_career_unavailable',
            'message': '川崎自养规划资料暂不可用，请检查独立种马库和本地目录。'}
