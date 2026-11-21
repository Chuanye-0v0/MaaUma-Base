"""One editable workflow document; graph decisions never perform game input."""
from copy import deepcopy
import json
import math

VERSION = 'uma.career_workflow.v1'
KINDS = {'entry', 'career', 'cut', 'condition', 'continue_current', 'restart', 'hold', 'finish'}
CHECKS = ('home', 'junior', 'inheritance', 'cut_checkpoint')
SCREEN_CHECKS = (*CHECKS, 'game_date')
FIELDS = ('vitality', 'speed', 'stamina', 'power', 'guts', 'wisdom', 'skill_pt',
          'stat_sum', 'calendar_year', 'calendar_month', 'calendar_turn', 'remaining_turns')
OPERATORS = ('lt', 'lte', 'eq', 'gte', 'gt')
SS_SIEVE_MODE = 'ss_sieve_cycle'
ACE_CYCLE_MODE = 'ace_career_cycle'


def normalize_ss_sieve(value):
    if (not isinstance(value, dict) or set(value) != {'pre_debut', 'year1_min', 'allow_ready_five'}
            or type(value['pre_debut']) is not bool or type(value['allow_ready_five']) is not bool
            or type(value['year1_min']) is not int or not 1 <= value['year1_min'] <= 24):
        raise ValueError('SS筛局需要出道开关、1～24次五人SS门槛及允许满五的开关。')
    return dict(value)


def ss_sieve_node(document):
    if document.get('template_id') != SS_SIEVE_MODE:
        return None
    return next((node for node in document['run_graph']['nodes'] if 'ss_sieve' in node), None)


def ace_cycle_node(document):
    if document.get('template_id') != ACE_CYCLE_MODE:
        return None
    return next((node for node in document['run_graph']['nodes'] if 'ace_cycle' in node), None)


def cycle_sieve_config(document):
    sieve = ss_sieve_node(document)
    if sieve is not None:
        return dict(sieve['ss_sieve'])
    ace = ace_cycle_node(document)
    if ace is not None and (ace.get('ace_cycle') or {}).get('ss_sieve'):
        return dict(ace['ace_cycle']['ss_sieve'])
    return None


def validate_start_document(document):
    try:
        doc = normalize(document)
    except ValueError as error:
        return {'ok': False, 'errors': [str(error)], 'warnings': []}
    errors = []
    warnings = []
    nodes = doc['run_graph']['nodes']
    by_id = {n['id']: n for n in nodes}
    edges = {(e['from'], e['outcome']): e['to'] for e in doc['run_graph']['edges']}
    sieve = ss_sieve_node(doc)
    ace = ace_cycle_node(doc)
    batch_max = (doc.get('career_batch') or {}).get('max_careers', 1)
    has_cut = any(n['kind'] in {'cut', 'restart'} for n in nodes)

    primary = sieve or ace
    if primary is None:
        curr = doc['run_graph']['entry']
        seen = set()
        while curr in by_id and curr not in seen:
            seen.add(curr)
            if by_id[curr]['kind'] in {'cut', 'career'}:
                primary = by_id[curr]
                break
            if (curr, 'next') not in edges:
                break
            curr = edges[(curr, 'next')]
        if primary is None:
            primary = next((n for n in nodes if n['kind'] in {'cut', 'career'}), None)

    policy = doc['policies'].get(primary.get('policy', 'default'), {}) if primary else doc['policies'].get('default', {})
    career_opts = policy.get('career_options') or {}
    cut_opts = policy.get('cut_options') or {}
    cut_inner = cut_opts.get('cut') if isinstance(cut_opts.get('cut'), dict) else {}

    if sieve is not None or ace is not None or batch_max > 1:
        setup = career_opts.get('setup') or {}
        char_id = str(setup.get('character_id') or '').strip()
        if char_id in {'', 'current', 'validation'}:
            errors.append('循环/筛局/成品模式必须选择具体马娘与衣装，不能使用「沿用当前角色」。')
    elif has_cut and primary and primary['kind'] == 'cut':
        char_id = str(cut_inner.get('character_id') or '').strip()
        if char_id == 'current':
            errors.append('刷切模式必须选择具体马娘与衣装，不能使用「沿用当前角色」。')

    if sieve is not None or ace is not None:
        if 'max_carrots' in cut_inner and cut_inner['max_carrots'] is None:
            errors.append('日常循环模式禁止无限萝卜预算，请填写明确整数上限（如 0）。')
        raw_attempts = cut_inner.get('max_attempts', 20 if sieve is not None else 10)
        if isinstance(raw_attempts, str) and raw_attempts.strip().isdigit():
            max_attempts = int(raw_attempts.strip())
        elif type(raw_attempts) is int:
            max_attempts = raw_attempts
        else:
            max_attempts = None
        if max_attempts is None or max_attempts < 1:
            errors.append('最多尝试局数必须为至少 1 的整数。')
        elif ace is not None:
            target_grad = ace['ace_cycle']['target_graduates']
            if max_attempts < target_grad:
                errors.append(f'最多尝试局数（{max_attempts}）小于目标成品数（{target_grad}），无法完成目标。')
    elif has_cut:
        if 'max_carrots' in cut_inner and cut_inner['max_carrots'] is None:
            warnings.append('当前刷切配置启用了无限萝卜（不封顶），请确认后再启动。')

    cut_restart_active = has_cut and sieve is None and ace is None and not any(
        (n.get('screening') or {}).get('enabled') is True for n in nodes if n['kind'] == 'cut'
    )
    mei_gamble_active = (sieve is None and ace is None) and (not has_cut or cut_restart_active)
    checked_prefs = [
        container['event_preferences']
        for container in (career_opts, cut_opts, cut_inner)
        if isinstance(container, dict) and isinstance(container.get('event_preferences'), dict)
    ]
    seen_conflict = False
    seen_inactive_warn = False
    for prefs in checked_prefs:
        mei = prefs.get('mei_unlock_policy') or {}
        if isinstance(mei, dict) and mei.get('mode') == 'gamble_when_low':
            manual = prefs.get('manual_choices') or {}
            if not seen_conflict and any(
                isinstance(k, str) and k.endswith(':story:809043004') and v == 1
                for k, v in manual.items()
            ):
                errors.append('事件设置冲突：黄帽解锁已手动锁定选 1，同时又开启了低体力选 2 失败重开，请先取消其中一项。')
                seen_conflict = True
            if not mei_gamble_active and not seen_inactive_warn:
                warnings.append('当前模式下「黄帽低体选2失败重开」不生效，将稳妥选1。')
                seen_inactive_warn = True

    return {'ok': len(errors) == 0, 'errors': errors, 'warnings': warnings}


def condition_catalog():
    from core.auto_trainer.larc_continuation import KINDS as continuation_kinds
    labels = {'vitality': '体力', 'speed': '速度', 'stamina': '耐力', 'power': '力量',
        'guts': '毅力', 'wisdom': '智力', 'skill_pt': '技能Pt', 'stat_sum': '五维总和',
        'calendar_year': '年份', 'calendar_month': '月份', 'calendar_turn': '日历回合',
        'remaining_turns': '距目标剩余回合', 'skill_count_min': '目标技能命中数（已学或当前可学）',
        'distance_min': '距离适性最低等级'}
    rows = [{'kind': field, 'label': labels[field], 'fields': ['op', 'value'],
             'supported_check_at': list(CHECKS)} for field in FIELDS]
    for kind in sorted(continuation_kinds):
        timing = ['junior', 'inheritance', 'cut_checkpoint']
        if kind in {'stat_min', 'stat_sum_min', 'adaptation_points_min'}:
            timing.append('home')
        if kind in {'supporter_points_min','bond_ready_min','bond_card_min','friend_outing_unlocked'}:
            timing=['junior']
        rows.append({'kind': kind, 'label': kind, 'fields': ['minimum', 'stat', 'card_id',
            'chain_id', 'bond_threshold', 'event_kind'], 'supported_check_at': timing})
    rows += [{'kind': 'skill_count_min', 'label': labels['skill_count_min'],
              'fields': ['skills', 'minimum'], 'supported_check_at': ['inheritance']},
             {'kind': 'distance_min', 'label': labels['distance_min'],
              'fields': ['distance', 'rank'], 'supported_check_at': ['inheritance']}]
    return rows


def templates():
    def make(identifier, label, nodes, edges):
        # Only the first execution step starts a new setup. Later steps inherit
        # the same run unless the user explicitly selects another strategy.
        initial_phase = 'cut' if nodes[0]['kind'] == 'cut' else 'career'
        return {'schema_version': VERSION, 'id': identifier, 'label': label, 'template_id': identifier,
            'run_graph': {'entry': 'start', 'nodes': [{'id': 'start', 'kind': 'entry'}, *nodes],
                          'edges': [{'from': 'start', 'outcome': 'next', 'to': nodes[0]['id']}, *edges]},
            'policies': {'default': {'career_options': {'larc_policy': None} if initial_phase == 'career' else {},
                                     'cut_options': {'larc_policy': None} if initial_phase == 'cut' else {}}},
            'editor': {'view': 'standard', 'positions': {}}}
    direct = make('direct_career', '直接自动育成',
        [{'id': 'grow', 'kind': 'career', 'policy': 'default'}, {'id': 'done', 'kind': 'finish'}],
        [{'from': 'grow', 'outcome': 'completed', 'to': 'done'}])
    result = [direct]
    for auto in (False, True):
        nodes = [{'id': 'screen', 'kind': 'cut', 'policy': 'default', 'check_at': 'cut_checkpoint'},
                 {'id': 'retry', 'kind': 'restart'}, {'id': 'wait', 'kind': 'hold'}]
        edges = [{'from': 'screen', 'outcome': 'not_matched', 'to': 'retry'},
                 {'from': 'screen', 'outcome': 'unknown', 'to': 'wait'},
                 {'from': 'retry', 'outcome': 'next', 'to': 'screen'}]
        if auto:
            nodes += [{'id': 'same', 'kind': 'continue_current'},
                      {'id': 'grow', 'kind': 'career', 'policy': 'default'}, {'id': 'done', 'kind': 'finish'}]
            edges += [{'from': 'screen', 'outcome': 'matched', 'to': 'same'},
                      {'from': 'same', 'outcome': 'next', 'to': 'grow'},
                      {'from': 'grow', 'outcome': 'completed', 'to': 'done'}]
        else:
            edges += [{'from': 'screen', 'outcome': 'matched', 'to': 'wait'}]
        result.append(make('cut_then_career' if auto else 'cut_preserve',
            '出切先停，确认后同局养完' if auto else '刷切并保留', nodes, edges))
    sieve = make(SS_SIEVE_MODE, 'SS筛局循环',
        [{'id': 'grow', 'kind': 'career', 'policy': 'default',
          'ss_sieve': {'pre_debut': False, 'year1_min': 4, 'allow_ready_five': False}},
         {'id': 'done', 'kind': 'finish'}],
        [{'from': 'grow', 'outcome': 'completed', 'to': 'done'}])
    sieve['policies']['default']['cut_options'] = {'cut': {
        'max_attempts': 20, 'max_tp_items': 0, 'max_carrots': 0}}
    result.append(sieve)
    from core.auto_trainer.ace_graduation import normalize_ace_cycle
    ace = make(ACE_CYCLE_MODE, '大赛成品马连开循环',
        [{'id': 'grow', 'kind': 'career', 'policy': 'default', 'ace_cycle': normalize_ace_cycle(None)},
         {'id': 'done', 'kind': 'finish'}],
        [{'from': 'grow', 'outcome': 'completed', 'to': 'done'}])
    ace['policies']['default']['cut_options'] = {'cut': {
        'max_attempts': 10, 'max_tp_items': 10, 'max_carrots': 0}}
    result.append(ace)
    return result


def validate_when(when, timing, *, explicit_collectors=False):
    catalog = {row['kind']: row for row in condition_catalog()}
    if (timing not in (SCREEN_CHECKS if explicit_collectors else CHECKS)
            or not isinstance(when, dict) or len(when) != 1
            or next(iter(when)) not in {'all', 'any'}):
        raise ValueError('条件节点需要检查时机及all/any条件组。')
    rows = next(iter(when.values()))
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
        raise ValueError('条件组需要1～32项条件。')
    for row in rows:
        if not isinstance(row, dict) or row.get('kind') not in catalog:
            raise ValueError('条件类型不受支持。')
        if not explicit_collectors and timing not in catalog[row['kind']]['supported_check_at']:
            raise ValueError('此条件不能在所选时机检查，技能/羁绊页面不会每回合采集。')
        if row['kind'] in FIELDS and (row.get('op', 'gte') not in OPERATORS
                or type(row.get('value')) not in {int, float} or not math.isfinite(row['value'])):
            raise ValueError('数值条件需要比较符及有限数值。')
        if row['kind'] == 'skill_count_min':
            skills = row.get('skills')
            if (not isinstance(skills, list) or not skills or any(not isinstance(s, str) or not s.strip() for s in skills)
                    or type(row.get('minimum')) is not int or not 1 <= row['minimum'] <= len(set(skills))):
                raise ValueError('技能条件需要有效名单及命中数。')
        elif row['kind'] == 'distance_min':
            if row.get('distance') not in {'short', 'mile', 'medium', 'long'} or row.get('rank') not in tuple('GFEDCBAS'):
                raise ValueError('距离条件需要有效距离与适性等级。')
        elif row['kind'] not in FIELDS:
            from core.auto_trainer.larc_continuation import defaults, normalize as normalize_continuation
            config = defaults(); config['enabled'] = True; config['junior']['conditions'] = [row]
            config['inheritance']['mode'] = 'observe'
            normalize_continuation(config)


def normalize_screening(value):
    if not isinstance(value, dict) or type(value.get('enabled', False)) is not bool:
        raise ValueError('组合筛选需要参数对象及启用开关。')
    result = deepcopy(value)
    if not result.get('enabled'):
        return result  # Disabled editor drafts never change the legacy cut owner.
    points = result.get('checkpoints')
    if not isinstance(points, list) or not 1 <= len(points) <= 72:
        raise ValueError('组合筛选需要1～72个顺序检查点。')
    identifiers = set()
    for point in points:
        if (not isinstance(point, dict) or not isinstance(point.get('id'), str) or not point['id'].strip()
                or point['id'] in identifiers):
            raise ValueError('筛选检查点标识不能为空或重复。')
        identifiers.add(point['id'])
        timing = point.get('check_at')
        validate_when(point.get('when'), timing, explicit_collectors=True)
        for field, allowed in (('on_pass', {'next_checkpoint', 'matched_branch', 'hold'}),
                               ('on_fail', {'next_checkpoint', 'not_matched_branch', 'hold'})):
            if field in point and point[field] not in allowed:
                raise ValueError('检查点通过/不通过动作不受支持。')
        if timing == 'game_date':
            date = point.get('game_date')
            if (not isinstance(date, dict) or type(date.get('year')) is not int or date['year'] not in {1, 2, 3}
                    or type(date.get('month')) is not int or not 1 <= date['month'] <= 12
                    or date.get('half') not in {'first', 'second'}):
                raise ValueError('筛选日期需要游戏年份1～3、月份1～12及上/下旬。')
    return result


def screening_capabilities():
    rows = deepcopy(condition_catalog())
    for row in rows:
        row['supported_check_at'] = list(SCREEN_CHECKS)
    return {'available': True, 'supported_check_at': list(SCREEN_CHECKS), 'condition_kinds': rows,
        'max_checkpoints': 72, 'max_conditions': 32,
        'allowed_date_range': {'years': [1, 2, 3], 'months': list(range(1, 13)), 'halves': ['first', 'second']},
        'date_semantics': 'first_safe_home_on_or_after', 'sequence': 'all_in_order',
        'confirmed_cut_failure': 'preserve_unless_explicit_not_matched_restart',
        'checkpoint_actions': {'on_pass': ['next_checkpoint', 'matched_branch', 'hold'],
            'on_fail': ['next_checkpoint', 'not_matched_branch', 'hold'],
            'defaults': {'on_pass': 'next_checkpoint_or_matched_at_last', 'on_fail': 'legacy_not_matched_with_cut_protection'},
            'last_next': 'hold', 'unknown': 'hold', 'restart_requires': 'explicit_on_fail_not_matched_branch_and_actual_restart_target'},
        'collection': 'explicit_current_home_only; no per-turn skill scanning'}


def normalize(document):
    if not isinstance(document, dict) or document.get('schema_version') != VERSION:
        raise ValueError('育成工作流版本不受支持。')
    result = deepcopy(document)
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > 1024 * 1024:
        raise ValueError('育成工作流文件过大。')
    graph, policies = result.get('run_graph'), result.get('policies')
    if not isinstance(graph, dict) or not isinstance(policies, dict) or not policies:
        raise ValueError('工作流需要执行图及策略配置。')
    if 'default' not in policies:raise ValueError('工作流需要default初始策略快照。')
    for policy in policies.values():
        if (not isinstance(policy, dict) or not isinstance(policy.get('career_options', {}), dict)
                or not isinstance(policy.get('cut_options', {}), dict)):
            raise ValueError('策略需要career_options和cut_options参数对象。')
        for phase,options in (('career',policy.get('career_options', {})),('cut',policy.get('cut_options', {}))):
            from scripts.larc_panel_setup import parse_targets, parse_strategy_options
            if options.get('race_retry') is not None:
                from core.auto_trainer.race_retry_budget import normalize as normalize_retry
                normalize_retry(options['race_retry'])
            if options.get('running_style') is not None:
                from core.auto_trainer.larc_custom_race_catalog import normalize_running_style
                options['running_style'] = normalize_running_style(options['running_style'])
            if options.get('race_running_styles') is not None:
                from core.auto_trainer.larc_custom_race_catalog import normalize_race_running_styles
                options['race_running_styles'] = normalize_race_running_styles(options['race_running_styles'])
            if options.get('custom_races') is not None:
                from core.auto_trainer.larc_custom_race_catalog import normalize_custom_races
                options['custom_races'] = normalize_custom_races(options['custom_races'])
            if 'targets' in options:parse_targets(options['targets'])
            parse_strategy_options(options.get('strategy','baseline'), options.get('failure_limit',25))
            if 'larc_policy' in options:
                from core.auto_trainer.larc_policy import normalize_larc_policy
                normalize_larc_policy(options['larc_policy'],phase)
            from core.auto_trainer.larc_user_preferences import normalize as normalize_preferences
            preferences=options.get('decision_preferences', (options.get('cut') or {}).get('decision_preferences'))
            if preferences is not None:normalize_preferences(preferences)
            from core.auto_trainer.larc_event_library import normalize_preferences as normalize_event_preferences
            if options.get('event_preferences') is not None:
                options['event_preferences']=normalize_event_preferences(options['event_preferences'])
            if isinstance(options.get('cut'),dict) and options['cut'].get('event_preferences') is not None:
                options['cut']['event_preferences']=normalize_event_preferences(options['cut']['event_preferences'])
    nodes, edges = graph.get('nodes'), graph.get('edges')
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 48 or not isinstance(edges, list) or len(edges) > 144:
        raise ValueError('工作流最多48个节点和144条连线。')
    by_id, outlets = {}, {}
    catalog = {row['kind']: row for row in condition_catalog()}
    for node in nodes:
        if (not isinstance(node, dict) or not isinstance(node.get('id'), str) or not node['id']
                or node['id'] in by_id or node.get('kind') not in KINDS):
            raise ValueError('节点标识重复或节点类型不受支持。')
        kind = node['kind']
        if 'ss_sieve' in node:
            if kind != 'career' or result.get('template_id') != SS_SIEVE_MODE:
                raise ValueError('SS筛局只用于独立筛局模式的普通育成节点。')
            node['ss_sieve'] = normalize_ss_sieve(node['ss_sieve'])
        if 'ace_cycle' in node:
            if kind != 'career' or result.get('template_id') != ACE_CYCLE_MODE:
                raise ValueError('大赛成品马连开循环只用于独立成品循环模式的普通育成节点。')
            from core.auto_trainer.ace_graduation import normalize_ace_cycle
            node['ace_cycle'] = normalize_ace_cycle(node['ace_cycle'])
        if kind == 'cut' and 'screening' in node:
            node['screening'] = normalize_screening(node['screening'])
        if (kind in {'cut', 'career','condition'} or 'policy' in node) and node.get('policy', 'default') not in policies:
            raise ValueError('执行节点引用的策略不存在。')
        if kind == 'condition':
            when, timing = node.get('when'), node.get('check_at')
            if (timing not in CHECKS or not isinstance(when, dict) or len(when) != 1
                    or next(iter(when)) not in {'all', 'any'}):
                raise ValueError('条件节点需要检查时机及all/any条件组。')
            rows = next(iter(when.values()))
            if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
                raise ValueError('条件组需要1～32项条件。')
            for row in rows:
                if not isinstance(row, dict) or row.get('kind') not in catalog:
                    raise ValueError('条件类型不受支持。')
                if timing not in catalog[row['kind']]['supported_check_at']:
                    raise ValueError('此条件不能在所选时机检查，技能/羁绊页面不会每回合采集。')
                if row['kind'] in FIELDS and (row.get('op', 'gte') not in OPERATORS
                        or type(row.get('value')) not in {int, float} or not math.isfinite(row['value'])):
                    raise ValueError('数值条件需要比较符及有限数值。')
                if row['kind'] == 'skill_count_min':
                    skills=row.get('skills')
                    if (not isinstance(skills,list) or not skills or any(not isinstance(s,str) or not s.strip() for s in skills)
                            or type(row.get('minimum')) is not int or not 1<=row['minimum']<=len(set(skills))):
                        raise ValueError('技能条件需要有效名单及命中数。')
                elif row['kind'] == 'distance_min':
                    if row.get('distance') not in {'short','mile','medium','long'} or row.get('rank') not in tuple('GFEDCBAS'):
                        raise ValueError('距离条件需要有效距离与适性等级。')
                elif row['kind'] not in FIELDS:
                    from core.auto_trainer.larc_continuation import defaults, normalize as normalize_continuation
                    config=defaults();config['enabled']=True;config['junior']['conditions']=[row]
                    config['inheritance']['mode']='observe'
                    normalize_continuation(config)
        by_id[node['id']] = node
    if graph.get('entry') not in by_id or by_id[graph['entry']]['kind'] != 'entry':
        raise ValueError('工作流入口必须是entry节点。')
    required = {'entry': {'next'}, 'cut': {'matched', 'not_matched', 'unknown'},
        'condition': {'matched', 'not_matched', 'unknown'}, 'continue_current': {'next'},
        'restart': {'next'}, 'career': {'completed'}, 'hold': set(), 'finish': set()}
    for edge in edges:
        if (not isinstance(edge, dict) or edge.get('from') not in by_id or edge.get('to') not in by_id
                or edge.get('outcome') not in required[by_id[edge['from']]['kind']]):
            raise ValueError('连线端点或出口不受支持。')
        key = (edge['from'], edge['outcome'])
        if key in outlets:
            raise ValueError('同一出口只能连接一个后续节点。')
        outlets[key] = edge['to']
        if edge['outcome'] == 'unknown' and by_id[edge['to']]['kind'] != 'hold':
            raise ValueError('数据不足出口必须保留等待，不能作为不满足或进入重开。')
        source_kind,target_kind=by_id[edge['from']]['kind'],by_id[edge['to']]['kind']
        if source_kind=='career' and target_kind not in {'condition','hold','finish'}:
            raise ValueError('育成阶段后请连接检查条件、保留或结束节点。')
        if source_kind=='continue_current' and target_kind!='career':
            raise ValueError('同局继续后应进入育成阶段。')
        if source_kind=='restart' and target_kind!='cut':
            raise ValueError('重开完成后应进入刷切检查阶段。')
        if source_kind=='cut' and edge['outcome']=='matched' and target_kind not in {'continue_current','career','hold'}:
            raise ValueError('已找到切应同局续养或保留，不能再次筛选或重开。')
    for identifier, node in by_id.items():
        if {outcome for source, outcome in outlets if source == identifier} != required[node['kind']]:
            raise ValueError('节点存在缺失的必需出口。')
    reachable,remaining=set(),[graph['entry']]
    while remaining:
        identifier=remaining.pop()
        if identifier in reachable:continue
        reachable.add(identifier)
        remaining.extend(target for (source,_),target in outlets.items() if source==identifier)
    if reachable!=set(by_id):raise ValueError('执行图含有不能从入口到达的节点。')
    if result.get('template_id') == SS_SIEVE_MODE:
        if sorted(node['kind'] for node in nodes) != ['career', 'entry', 'finish'] or ss_sieve_node(result) is None:
            raise ValueError('SS筛局模式需要入口、一个普通育成筛局节点和结束节点。')
    if result.get('template_id') == ACE_CYCLE_MODE:
        if sorted(node['kind'] for node in nodes) != ['career', 'entry', 'finish'] or ace_cycle_node(result) is None:
            raise ValueError('大赛成品马连开循环需要入口、一个普通育成成品节点和结束节点。')
    if any(node['kind'] == 'restart' for node in nodes) and not any(node['kind'] == 'cut' for node in nodes):
        raise ValueError('重开节点必须属于有批次预算的刷切工作流。')
    if (any(node['kind']=='condition' and node['check_at']=='cut_checkpoint' for node in nodes)
            and not any(node['kind']=='cut' for node in nodes)):
        raise ValueError('切检查点条件需要图中的刷切检查执行器。')
    if (any(node['kind']=='condition' and any(row['kind']=='cut_present'
            for row in next(iter(node['when'].values()))) for node in nodes)
            and not any(node['kind']=='cut' for node in nodes)):
        raise ValueError('才能出众条件需要图中的刷切检查执行器。')
    # Only restart cycles may revisit input owners; empty control cycles cannot run.
    for identifier in by_id:
        seen, current = set(), identifier
        while by_id[current]['kind'] in {'entry', 'continue_current'}:
            if current in seen:
                raise ValueError('工作流存在不执行行动的循环。')
            seen.add(current)
            current = outlets[(current, 'next')]
    if 'career_batch' in result:
        from core.career_batch import validate_document
        result['career_batch'] = validate_document(result)
    return result


def evaluate(node, facts):
    from core.auto_trainer.larc_continuation import condition_result, defaults, evaluate_inheritance
    rows = []
    for condition in next(iter(node['when'].values())):
        kind, value = condition['kind'], None
        if kind in FIELDS:
            actual = facts.get(kind)
            if type(actual) in {int, float} and math.isfinite(actual):
                expected, op = condition['value'], condition.get('op', 'gte')
                value = {'lt': actual < expected, 'lte': actual <= expected, 'eq': actual == expected,
                         'gte': actual >= expected, 'gt': actual > expected}[op]
        elif kind in {'skill_count_min', 'distance_min'}:
            config = defaults()
            section = config['inheritance']
            if kind == 'skill_count_min':
                section.update(skills=condition.get('skills', []), min_skills=condition.get('minimum', 1))
            else:
                section['distances'] = {condition.get('distance'): condition.get('rank')}
            status = evaluate_inheritance(config, facts)['status']
            value = {'passed': True, 'failed': False}.get(status)
        else:
            value = condition_result(condition, facts)['passed']
        rows.append({'condition': deepcopy(condition), 'result': value})
    values = [row['result'] for row in rows]
    if 'all' in node['when']:
        result = False if False in values else None if None in values else True
    else:
        result = True if True in values else None if None in values else False
    return {'outcome': {True: 'matched', False: 'not_matched', None: 'unknown'}[result], 'conditions': rows}


def schema():
    from core.auto_trainer.larc_baseline import normalize_larc_policy
    from core.career_batch import MAX_CAREERS
    return {'available': True, 'schema_version': VERSION, 'templates': templates(),
        # This describes new runs with no inherited/template policy, not the
        # effective policy of an arbitrary saved goal or an earlier workflow step.
        'new_larc_policy_defaults': {mode: normalize_larc_policy(None, mode)
                                     for mode in ('career', 'cut')},
        'node_kinds': sorted(KINDS), 'condition_kinds': condition_catalog(),
        'supported_check_at': list(CHECKS), 'condition_outcomes': ['matched', 'not_matched', 'unknown'],
        'unknown_policy': 'hold', 'editor_metadata_preserved': True,
        'cut_deadline': 'existing_junior_december_second_half',
        'skill_sampling': 'inheritance_checkpoint_only; learned_or_currently_available',
        'condition_groups':['all','any'],
        'career_to_condition':'train_until_downstream_condition_check_at',
        'policy_switch_fields':['targets','failure_limit','larc_policy','decision_preferences',
                                'event_preferences','race_retry','running_style','race_running_styles',
                                'custom_races','manual_handoff'],
        'batch_budget':'initial immutable CutSession including reservations; never reset by a node',
        'career_batch': {'available': True, 'default_max_careers': 1, 'max_careers': [1, MAX_CAREERS],
            'scope': 'linear_ordinary_career_only', 'refill_policy': 'no_automatic_refill'},
        'screening_capabilities': screening_capabilities()}
