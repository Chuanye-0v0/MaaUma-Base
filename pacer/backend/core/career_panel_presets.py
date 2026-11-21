"""Ordinary Pacer slots and immutable standalone career records; no device I/O."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

from core.auto_trainer.cut_farming import atomic_json
from core.cut_panel_presets import slot_id
from scripts.larc_panel_setup import (DEFAULT_TARGETS,parse_targets,parse_strategy_options,
    parse_mei_breaks,parse_early_skill_allowlist,parse_arc_skill_policy)
from scripts.larc_ordinary_setup import parse_ordinary_setup

SCHEMA='uma.career_panel_presets.v1'
RUN_SCHEMA='uma.pacer_center_run.v1'
RUN_MARKER='session/pacer_center_run.json'


def _read(path,default=None):
    path=Path(path)
    if not path.is_file():return {} if default is None else deepcopy(default)
    try:value=json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError,ValueError) as exc:raise ValueError('普通育成配置或记录无法读取。') from exc
    if not isinstance(value,dict):raise ValueError('普通育成配置或记录格式不正确。')
    return value


def settings_from_storage(value):
    """Expose date stops without teaching old 049 readers a new bool field."""
    result=deepcopy(value)
    dates=result.pop('manual_pause_dates',None)
    config=result.get('manual_handoff')
    if dates is not None and (config is None or isinstance(config,dict) and 'dates' not in config):
        config={**(config or {}),'dates':dates}
    from core.auto_trainer.larc_manual_handoff import normalize
    result['manual_handoff']=normalize(config)
    return result


def settings_for_storage(value):
    """049 sanitizes manual_handoff strictly; keep its two-bool disk shape."""
    result=settings_from_storage(value)
    config=result['manual_handoff']
    result['manual_handoff']={key:config[key] for key in ('first_arc','final_arc')}
    result['manual_pause_dates']=deepcopy(config['dates'])
    return result


def sanitize_settings(value,root=None,*,new_configuration=False,allow_continuation_restart=False):
    if not isinstance(value,dict):raise ValueError('普通育成配置应为参数对象。')
    value=settings_from_storage(value)
    strategy,failure=parse_strategy_options(value.get('strategy','baseline'),value.get('failure_limit',25))
    limit=value.get('max_actions',72)
    if isinstance(limit,str) and limit.strip().isdigit():limit=int(limit)
    if type(limit) is not int or not 1<=limit<=72:raise ValueError('最多推进回合数请填 1～72。')
    result={'targets':parse_targets(value.get('targets',DEFAULT_TARGETS)),'strategy':strategy,
        'failure_limit':failure,'max_actions':limit,'mei_breaks':parse_mei_breaks(value.get('mei_breaks')),
        'with_ramonu':value.get('with_ramonu') is True,'terminal_skills':value.get('terminal_skills',True) is True,
        'retain_training':value.get('retain_training') is True,'early_skill_allowlist':parse_early_skill_allowlist(value.get('early_skill_allowlist',[])),
        'setup':parse_ordinary_setup(value.get('setup'))}
    from core.auto_trainer.larc_support_skill_startup import normalize_mid_skill_learning
    result['mid_skill_learning']=normalize_mid_skill_learning(value.get('mid_skill_learning'),
        legacy_enabled=result['with_ramonu'] and bool(result['early_skill_allowlist']))
    if 'support_profile' in value:
        from core.auto_trainer.run_support_profile import normalize_declaration
        result['support_profile']=normalize_declaration(value['support_profile'])
    if 'arc_policy' in value:result['arc_policy']=parse_arc_skill_policy(value['arc_policy'])
    if 'larc_policy' in value:
        from core.auto_trainer.larc_policy import normalize_larc_policy
        result['larc_policy']=normalize_larc_policy(value['larc_policy'],'career')
    elif new_configuration:
        from core.auto_trainer.larc_policy import normalize_larc_policy
        result['larc_policy']=normalize_larc_policy(None,'career')
    else:
        from core.auto_trainer.larc_policy import normalize_larc_policy
        result['larc_policy']=normalize_larc_policy({'variant':strategy},'career')
    from core.auto_trainer.larc_manual_handoff import normalize as normalize_manual
    # This is the next-run preset, including optional game-date stops. Live
    # plan edits use update_manual_handoff_plan and never rewrite this snapshot.
    result['manual_handoff']=normalize_manual(value.get('manual_handoff'))
    from core.auto_trainer import larc_ai_options,larc_user_preferences
    package=Path(root).parent if root is not None and Path(root).name.casefold()=='backend' else root
    if larc_user_preferences.feature_available(package):
        preferences = value.get('decision_preferences')
        if 'decision_preferences' not in value and result['larc_policy'].get('revision') in ('1.9.5', '1.9.6', '1.9.7', '1.9.8', '1.9.9', '1.9.10', '1.9.11', '1.9.12', '2.0', '2.1', '2.2'):
            preferences = result['larc_policy'].get('decision_preferences')
        result['decision_preferences'] = larc_user_preferences.normalize(preferences)
    ai=larc_ai_options.normalize(value.get('ai_options'))
    if ai['mode']!='local' and not larc_ai_options.feature_available(root):raise ValueError('当前版本未启用AI决策。')
    if larc_ai_options.feature_available(root):result['ai_options']=ai
    if value.get('continuation') is not None:
        from core.auto_trainer.larc_continuation import normalize,feature_available
        continuation=normalize(value['continuation'])
        if continuation['enabled']:
            if not feature_available(root):raise ValueError('当前版本未包含续养执行功能。')
            if not allow_continuation_restart and any(continuation[key]['on_fail']=='restart' for key in ('junior','inheritance')):
                raise ValueError('普通育成没有刷切批次预算，续养条件未达时请选择暂停保留。')
        result['continuation']=continuation
    from core.auto_trainer.race_retry_budget import normalize as normalize_retry
    if value.get('race_retry') is not None:result['race_retry']=normalize_retry(value['race_retry'])
    from core.auto_trainer.larc_custom_race_catalog import normalize_custom_races,normalize_race_running_styles,normalize_running_style
    if value.get('running_style') is not None:result['running_style']=normalize_running_style(value['running_style'])
    if value.get('race_running_styles') is not None:result['race_running_styles']=normalize_race_running_styles(value['race_running_styles'])
    if value.get('custom_races') is not None:result['custom_races']=normalize_custom_races(value['custom_races'])
    if value.get('event_preferences') is not None:
        from core.auto_trainer.larc_event_library import normalize_preferences as normalize_event_preferences
        result['event_preferences']=normalize_event_preferences(value['event_preferences'])
    return result


def ai_connection(root,options):
    from core.auto_trainer.larc_ai_options import normalize
    if normalize(options.get('ai_options'))['mode']=='local':return {}
    from desktop.larc_ai_client import provider_status
    value=provider_status(root)
    if value.get('configured') is not True:raise ValueError('请先保存AI接口地址、模型和密钥，再使用AI决策。')
    return {key:value[key] for key in ('base_url','model')}


def save_new_run(path,endpoint,options,connection):
    from core.auto_trainer.larc_ai_options import save_options
    from core.auto_trainer.larc_manual_handoff import save_options as save_manual
    path=Path(path)
    save_manual(path,options.get('manual_handoff'))
    goal=_read(path/'goal.json')
    goal_dirty=False
    if 'decision_preferences' in options:
        goal.setdefault('larc_policy',{})['decision_preferences']=deepcopy(options['decision_preferences'])
        goal_dirty=True
    if options.get('event_preferences') is not None:
        goal['event_preferences']=deepcopy(options['event_preferences'])
        goal_dirty=True
    if goal_dirty:
        atomic_json(path/'goal.json',goal)
    save_options(path,options.get('ai_options'),connection)
    atomic_json(path/'session/umaa_panel_options.json',settings_for_storage(options))
    atomic_json(path/RUN_MARKER,{'schema_version':RUN_SCHEMA,'run_id':path.name,'mode':'career',
        'endpoint':endpoint,'source':'private_pacer_center','started_at':datetime.now().isoformat(timespec='seconds'),
        'setup':deepcopy(options['setup'])})


def checked_cut_origin(path,origin,*,root,endpoint,review_only=False):
    """A handoff remains tied to the original preserved cut attempt."""
    from scripts.larc_umaa_cut import checked_batch
    if not isinstance(origin,dict) or origin.get('status_at_handoff') not in {'found','handoff'}:
        raise ValueError('续养来源记录不正确。')
    if review_only:
        # A historical review is bound to its recorded device, not today's
        # portable connection or executable support-card setup.
        batch=Path(origin.get('batch_root')).resolve(strict=True);runs=Path(root).resolve()/'runs'
        if batch==runs or not batch.is_relative_to(runs):raise ValueError('续养来源不在本程序记录目录。')
        data=_read(batch/'cut_farming.json');config=data.get('config') or {}
        if (data.get('schema_version')!='uma.cut_farming_session.v1'
                or type(config.get('device_port')) is not int
                or endpoint!=f"127.0.0.1:{config['device_port']}"):
            raise ValueError('续养来源与记录设备不一致。')
    else:batch,data,config=checked_batch(origin.get('batch_root'),root=root,endpoint=endpoint)
    attempt=(data.get('attempts') or [{}])[-1]
    found=(origin['status_at_handoff']=='found' and data.get('status')=='found' and attempt.get('status')=='found')
    handed=(origin['status_at_handoff']=='handoff' and data.get('status')=='handoff'
        and data.get('stop',{}).get('reason')=='continuation_handoff'
        and attempt.get('stop_reason')=='continuation_handoff'
        and attempt.get('status') not in {'abandoned','abandoned_and_restarted'})
    if (not (found or handed) or data.get('pending_action')
            or origin.get('attempt_id')!=path.name or attempt.get('run_id')!=path.name
            or Path(attempt.get('run_root','')).resolve()!=path or path.parent!=batch/'attempts'):
        raise ValueError('续养记录与已保留的刷切局不一致。')
    return batch,data,config,attempt


def handoff_candidate(root,endpoint,*,batch_root=None):
    """Only the latest confirmed, fully stopped cut may be explicitly handed off."""
    from scripts.larc_umaa_cut import batch_records
    from core.auto_trainer.larc_continuation import feature_available
    if not feature_available(root):raise ValueError('当前版本缺少首次远征前的续养交接功能。')
    rows=batch_records(root,endpoint)
    if not rows:raise ValueError('没有可继续养成的刷切记录。')
    latest=Path(rows[0]['path']).resolve()
    if batch_root is not None and Path(batch_root).resolve()!=latest:raise ValueError('仅能继续当前最新保留局。')
    data=_read(latest/'cut_farming.json');attempt=(data.get('attempts') or [{}])[-1]
    path=Path(attempt.get('run_root','')).resolve(strict=True)
    origin={'batch_root':str(latest),'attempt_id':path.name,'status_at_handoff':data.get('status')}
    batch,data,config,attempt=checked_cut_origin(path,origin,root=root,endpoint=endpoint)
    progress=_read(path/'progress.json');probe=attempt.get('last_probe') or {}
    if (progress.get('run_session_id')!=path.name or progress.get('device')!=endpoint
            or origin['status_at_handoff']=='found' and (probe.get('state')!='present' or probe.get('returned') is not True)
            or data.get('stop',{}).get('run_id')!=path.name
            or not data.get('stopped_at_unix_ms') or not attempt.get('stopped_at_unix_ms')):
        raise ValueError('本局尚未确认找到切并完成停止，不能交接。')
    if (path/RUN_MARKER).exists():raise ValueError('本局已转入普通育成，请从普通育成继续。')
    if ((path/'session/larc_autonomous_pending.json').exists()
            or _read(path/'session/larc_continuation_state.json').get('pending')):
        raise ValueError('本局还有待确认步骤，请先处理原任务。')
    settlement=_read(path/'session/larc_terminal_settlement.json')
    if settlement.get('status')=='complete' or settlement.get('phase')=='complete':
        raise ValueError('这局已正常结算，不能重新交接。')
    return path,origin,config,attempt


def save_cut_handoff(path,endpoint,origin,config,attempt,options,*,root,workflow_internal=False):
    """Explicit task handoff; retain custom goals/AI and transition default SS horizon."""
    from core.auto_trainer.cut_farming import expected_character_name
    from core.auto_trainer.larc_ai_options import load_options
    from core.auto_trainer.larc_continuation import defaults
    from scripts.larc_continuation_runtime import save_options
    options=deepcopy(options)
    options['setup']={'character_id':config.character_id,'character_name':expected_character_name(config),
        **{key:getattr(config,key) for key in ('support_deck','borrow_card_id','borrow_min_level','borrow_min_break')}}
    options['ai_options']=load_options(path)
    goal=_read(path/'goal.json')
    task_prepared=None
    if goal.get('larc_policy',{}).get('variant')=='baseline':
        from scripts.larc_panel_setup import prepare_panel_policy,save_policy_migration
        from core.auto_trainer.config import UGParentGoal,validate_goal_config
        original=deepcopy(goal['larc_policy']);source=deepcopy(original)
        task=_read(path/'session/larc_task_goal.json')
        if task.get('horizon_source')=='task_default':source.pop('ss_planning_horizon',None)
        prepared=prepare_panel_policy(source,strategy='baseline',task_mode='career')
        if original.get('ss_planning_horizon')!=prepared['policy'].get('ss_planning_horizon'):
            prepared['migration']['original_policy']=original
            for change in prepared['migration']['changes']:
                if change['field']=='ss_planning_horizon':
                    change.update(before_present=True,before=original.get('ss_planning_horizon'),
                        reason='explicit_cut_to_career_task_goal_transition')
        goal['larc_policy']=prepared['policy']
        validate_goal_config(UGParentGoal.from_dict(goal))
        task_prepared=prepared
        options.update(strategy='baseline',larc_policy=deepcopy(prepared['policy']))
    options['decision_preferences']=deepcopy(goal.get('larc_policy',{}).get('decision_preferences'))
    continuation=defaults();continuation['enabled']=True
    for node in ('junior','inheritance'):continuation[node].update(mode='observe',on_fail='pause')
    options['continuation']=continuation
    options=sanitize_settings(options,root)
    if task_prepared is not None:
        save_policy_migration(path/'session',task_prepared,label='task-career')
        atomic_json(path/'goal.json',goal)
        atomic_json(path/'session/larc_task_goal.json',task_prepared['task_goal'])
    from core.auto_trainer.larc_manual_handoff import save_options as save_manual
    from core.auto_trainer.larc_manual_handoff import PLAN,OPTIONS,snapshot as manual_snapshot
    if workflow_internal and ((path/PLAN).is_file() or (path/OPTIONS).is_file()):
        options['manual_handoff']=deepcopy(manual_snapshot(path)['plan']['config'])
    else:save_manual(path,options.get('manual_handoff'))
    save_options(path,continuation)
    atomic_json(path/'session/umaa_panel_options.json',settings_for_storage(options))
    now=datetime.now().isoformat(timespec='seconds')
    started=attempt.get('started_at_unix_ms')
    atomic_json(path/RUN_MARKER,{'schema_version':RUN_SCHEMA,'run_id':path.name,'mode':'career',
        'endpoint':endpoint,'source':'private_pacer_center','started_at':
            datetime.fromtimestamp(started/1000).isoformat(timespec='seconds') if type(started) is int else now,
        'handed_off_at':now,'setup':deepcopy(options['setup']),'cut_origin':deepcopy(origin)})
    if not workflow_internal and (path/'session/career_workflow_context.json').is_file():
        from scripts.career_workflow_runtime import load_for_run
        workflow=load_for_run(path)
        if workflow.node['kind'] not in {'continue_current','career'}:
            workflow.data.update(status='manual_career',manual_continue_current=True)
            workflow.save()


def _sieve_review_attempt(path, root, endpoint):
    batch = path.parent.parent
    if (path.parent.name != 'attempts' or batch.parent != root / 'runs'
            or not batch.name.startswith('cut_pacer_') or not path.name.startswith('larc_panel_')):
        return None
    data = _read(batch / 'cut_farming.json')
    config = data.get('config') or {}
    if (data.get('schema_version') != 'uma.cut_farming_session.v1' or not data.get('ss_sieve_cycle')
            or type(config.get('device_port')) is not int or endpoint != f"127.0.0.1:{config['device_port']}"):
        return None
    return next((attempt for attempt in data.get('attempts', []) if attempt.get('run_id') == path.name
                 and attempt.get('run_root') == str(path) and attempt.get('ss_sieve_outcome') == 'completed'), None)


def record_for_path(value,*,root,endpoint,include_settings=True,review_only=False):
    root=Path(root).resolve();path=Path(value).resolve(strict=True)
    marker=_read(path/RUN_MARKER);progress=_read(path/'progress.json')
    sieve_review = False
    if 'cut_origin' in marker:
        checked_cut_origin(path,marker['cut_origin'],root=root,endpoint=endpoint,review_only=review_only)
    elif path.parent!=root/'runs' or not path.name.startswith('larc_panel_'):
        sieve_review = _sieve_review_attempt(path, root, endpoint) is not None
        if not sieve_review:
            raise ValueError('普通育成记录不在本程序目录。')
    if (marker.get('schema_version')!=RUN_SCHEMA or marker.get('source')!='private_pacer_center'
            or marker.get('mode')!='career' or marker.get('run_id')!=path.name
            or progress.get('run_session_id')!=path.name or marker.get('endpoint')!=progress.get('device')
            or marker.get('endpoint')!=endpoint):raise ValueError('普通育成记录与当前模式、设备或局号不一致。')
    settings=settings_from_storage(_read(path/'session/umaa_panel_options.json')) if include_settings else {}
    terminal=_read(path/'session/larc_terminal_settlement.json')
    complete=sieve_review or terminal.get('status')=='complete' or terminal.get('phase')=='complete'
    from core.auto_trainer.larc_manual_handoff import snapshot as manual_snapshot
    manual=manual_snapshot(path)
    return {'path':str(path),'mode':'career','label':f"{marker.get('started_at',path.name)} · 已推进 {progress.get('actions_completed',0)} 步",
        'settings':settings,'manual_handoff':manual,'can_resume':not complete and not manual['terminal'],
        'resume_reason':'本局已交还用户收尾。' if manual['terminal'] else '这局已正常结算。' if complete else None,
        'modified':(path/'progress.json').stat().st_mtime}


def records(root,endpoint):
    rows=[]
    paths=set((Path(root)/'runs').glob('larc_panel_*'))
    from scripts.larc_umaa_cut import batch_records
    for batch in batch_records(root,endpoint):
        attempt=batch['cut'].get('current_attempt_root')
        if attempt:paths.add(Path(attempt))
    for path in paths:
        try:rows.append(record_for_path(path,root=root,endpoint=endpoint))
        except (OSError,ValueError,TypeError,KeyError):continue
    return sorted(rows,key=lambda row:row['modified'],reverse=True)


class CareerPanelPresets:
    def __init__(self,root,endpoint,cut_presets):
        self.root=Path(root).resolve();self.endpoint=endpoint;self.cut_presets=cut_presets
        self.path=self.root/'runs/standalone-panel/career-settings.json'
        self.preferences_path=self.path.with_name('decision-preferences.json')
        self.ai_options_path=self.path.with_name('ai-options.json')

    def _seed(self):
        from core.auto_trainer.cut_farming import CutFarmingConfig,expected_character_name
        old=self.cut_presets.payload();slots={}
        for row in old['slots']:
            saved=row['settings'];cut=saved.get('cut') or {}
            setup={key:cut.get(key) for key in ('support_deck','borrow_card_id','borrow_min_level','borrow_min_break') if key in cut}
            try:
                config=CutFarmingConfig(cut.get('character_id','101001'),validation_mode=cut.get('validation_mode',False),
                    validation_character_name=cut.get('validation_character_name'))
                setup.update(character_id=config.character_id,character_name=expected_character_name(config))
            except (ValueError,KeyError,AttributeError):setup.update(character_id='current',character_name='')
            settings=sanitize_settings({'strategy':saved.get('strategy','baseline'),'setup':setup,
                **{key:cut[key] for key in ('decision_preferences','ai_options') if key in cut}},self.root)
            from core.auto_trainer.larc_policy import normalize_larc_policy
            settings['larc_policy']=normalize_larc_policy(None,'career')
            slots[str(row['id'])]={'settings':settings,'saved':False,'decision_preferences_saved':False}
        return {'schema_version':SCHEMA,'selected_id':old['selected_id'],'slots':slots}

    def _read(self):
        if not self.path.is_file():return self._seed()
        data=_read(self.path)
        if data.get('schema_version')!=SCHEMA or not isinstance(data.get('slots'),dict):raise ValueError('普通育成配置记录格式不正确。')
        slot_id(data.get('selected_id'));return data

    def current_record(self,active_root=None):
        if active_root:
            try:return record_for_path(active_root,root=self.root,endpoint=self.endpoint)
            except (OSError,ValueError,TypeError,KeyError) as exc:
                return {'path':None,'mode':'career','label':'普通育成记录不可用','can_resume':False,'resume_reason':str(exc)}
        found=records(self.root,self.endpoint);return found[0] if found else None

    def payload(self,*,active_root=None):
        data=self._read();preferences=_read(self.preferences_path);ai=_read(self.ai_options_path);rows=[]
        for index in (1,2,3):
            entry=data['slots'].get(str(index),{});settings=deepcopy(entry.get('settings') or {})
            if str(index) not in data['slots']:
                from core.auto_trainer.larc_policy import normalize_larc_policy
                settings['larc_policy']=normalize_larc_policy(None,'career')
            if str(index) not in data['slots'] and str(index) not in preferences:
                from core.auto_trainer.larc_user_preferences import feature_available,normalize
                package=self.root.parent if self.root.name.casefold()=='backend' else self.root
                if feature_available(package):settings['decision_preferences']=normalize({'mei_bonus':5.})
            if str(index) in preferences:settings['decision_preferences']=preferences[str(index)]
            if str(index) in ai:settings['ai_options']=ai[str(index)]
            stored = entry.get('settings') or {}
            from core.auto_trainer.larc_user_preferences import panel_preferences_saved
            preferences_saved = panel_preferences_saved(self.preferences_path,index,preferences,
                embedded=self.path.is_file() and ('decision_preferences' in stored
                    or 'decision_preferences' in (stored.get('larc_policy') or {})))
            rows.append({'id':index,'label':f'配置{index}','settings':sanitize_settings(settings,self.root),
                         'saved':entry.get('saved') is True,'decision_preferences_saved':preferences_saved})
        return {'slots':rows,'selected_id':data['selected_id'],'current_record':self.current_record(active_root)}

    def _write(self,data):
        from core.auto_trainer.larc_user_preferences import feature_available,save_panel_preferences
        package=self.root.parent if self.root.name.casefold()=='backend' else self.root
        enabled=feature_available(package)
        preferences=_read(self.preferences_path) if enabled else {}
        for index,row in data['slots'].items():
            row['settings']=settings_for_storage(row['settings'])
            value=row['settings'].get('decision_preferences')
            if enabled and value is not None and index not in preferences:
                save_panel_preferences(self.preferences_path,index,value,preferences,
                    factory=row.get('decision_preferences_saved',self.path.is_file()) is False)
            row['settings'].pop('decision_preferences',None);row['settings'].pop('ai_options',None)
        atomic_json(self.path,data)

    def save(self,index,settings,*,active_root=None):
        from scripts.larc_panel_setup import upgrade_panel_settings,save_policy_migration
        index=slot_id(index);data=deepcopy(self._read())
        previous=deepcopy(data['slots'].get(str(index),{}).get('settings',{}))
        settings,prepared=upgrade_panel_settings(settings,task_mode='career',previous=previous)
        settings=sanitize_settings(settings,self.root)
        save_policy_migration(self.path.parent,prepared,label=f'career-slot-{index}',previous_settings=previous)
        data['slots'][str(index)]={'settings':deepcopy(settings),'saved':True,'decision_preferences_saved':True}
        for key,path in [('decision_preferences',self.preferences_path),('ai_options',self.ai_options_path)]:
            if key in settings:
                shared=_read(path)
                if key == 'decision_preferences':
                    from core.auto_trainer.larc_user_preferences import save_panel_preferences
                    save_panel_preferences(path,index,settings[key],shared)
                else:
                    shared[str(index)]=deepcopy(settings[key]);atomic_json(path,shared)
        self._write(data);return self.payload(active_root=active_root)

    def select(self,index,*,active_root=None):
        index=slot_id(index);data=deepcopy(self._read());data['selected_id']=index
        self._write(data);return self.payload(active_root=active_root)
