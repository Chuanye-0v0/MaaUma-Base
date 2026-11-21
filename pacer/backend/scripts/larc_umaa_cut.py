"""UMAA dashboard adapter for the existing independent cut controller."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
import uuid

from core.auto_trainer.cut_farming import CutFarmingConfig, CutSession, atomic_json, load_event_references
from core.auto_trainer.dashboard_control import ControlProfile, DashboardControlService
from core.auto_trainer.dashboard_runner import RunnerHeartbeatWorker
from core.auto_trainer.runtime_control import RunControl, RunStopped


CUT_OPTIONS = frozenset({'character_id', 'max_attempts', 'max_tp_items', 'max_carrots',
    'check_mode', 'fallback_interval_turns', 'debut_clock_retry', 'retain_screenshots', 'restart_after_character_cut_miss',
    'restart_after_training_failure',
    'pushplus_token_file', 'template_root', 'validation_mode', 'validation_character_name', 'setup_confirmed',
    'support_deck', 'borrow_card_id', 'borrow_min_level', 'borrow_min_break', 'discard_found_for_validation', 'continuation', 'decision_preferences', 'ai_options'})


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}


def cut_options(value, *, endpoint, strategy, root):
    if not isinstance(value, dict) or set(value) - CUT_OPTIONS:
        raise ValueError('刷切参数包含未知选项。')
    if value.get('setup_confirmed') is not True:
        raise ValueError('请先配好角色、卡组和种马，并确认当前已在该局第一回合。')
    from core.auto_trainer.larc_user_preferences import normalize, feature_available
    if feature_available() and 'decision_preferences' in value:normalize(value['decision_preferences'])
    from core.auto_trainer import larc_ai_options
    ai_options=larc_ai_options.normalize(value.get('ai_options'))
    if ai_options['mode']!='local' and not larc_ai_options.feature_available(root):
        raise ValueError('当前版本未启用AI接管，请使用已包含此功能的测试版。')
    fields = {key: item for key, item in value.items() if key not in {'template_root', 'setup_confirmed', 'decision_preferences','ai_options'}}
    for key in ('max_attempts', 'max_tp_items', 'max_carrots', 'fallback_interval_turns',
            'support_deck', 'borrow_card_id', 'borrow_min_level', 'borrow_min_break'):
        if key not in fields:
            continue
        item = fields[key]
        if isinstance(item, str) and re.fullmatch(r'[0-9]+', item.strip()):
            item = int(item.strip())
        if type(item) is not int or item < (1 if key == 'max_attempts' else 0):
            raise ValueError('尝试次数、药、萝卜和检查间隔请填合法整数。')
        fields[key] = item
    for key in ('pushplus_token_file', 'validation_character_name'):
        if key in fields:
            item = fields[key]
            if item is not None and not isinstance(item, str):
                raise ValueError('文件路径和角色名应为文本。')
            fields[key] = item.strip() or None if isinstance(item, str) else None
    fields.setdefault('character_id', 'validation' if fields.get('validation_mode') else None)
    fields.setdefault('retain_screenshots', False)
    fields.setdefault('discard_found_for_validation', False)
    fields.update(device_port=int(endpoint.rsplit(':', 1)[1]), strategy=strategy)
    config = CutFarmingConfig.from_dict(fields)
    # Check local event references once before creating a batch. No device or
    # notification is touched, and the existing controller remains the owner.
    load_event_references(config)
    template = value.get('template_root')
    if template is not None and not isinstance(template, str):
        raise ValueError('复用目标记录路径应为文本。')
    template = template.strip() or None if isinstance(template, str) else None
    if template:
        template = Path(template).resolve(strict=True)
        if not template.is_relative_to(Path(root) / 'runs') or not (template / 'goal.json').is_file():
            raise ValueError('复用目标记录必须是本项目的育成记录。')
        if read_json(template / 'progress.json').get('device') != endpoint:
            raise ValueError('复用目标记录与当前设备不一致。')
        template = str(template)
    return config, template


def checked_batch(value, *, root, endpoint=None):
    path = Path(value).resolve(strict=True)
    runs = Path(root).resolve() / 'runs'
    if path == runs or not path.is_relative_to(runs):
        raise ValueError('请选择本项目的刷切记录。')
    data = read_json(path / 'cut_farming.json')
    if data.get('schema_version') != 'uma.cut_farming_session.v1':
        raise ValueError('刷切记录无法读取。')
    config = CutFarmingConfig.from_dict(data.get('config'))
    if endpoint and endpoint != f'127.0.0.1:{config.device_port}':
        raise ValueError('所选刷切记录与当前设备不一致。')
    return path, data, config


def resume_status(path, data, config):
    """Use the business validator on an in-memory copy, without any writes."""
    try:
        if data.get('ss_sieve_cycle'):
            from scripts.career_sieve_cycle import sieve_resumable
            if not sieve_resumable(data):
                return False, '筛局开局或弃养阶段尚未确认，不能续跑或重复提交。'
        session = object.__new__(CutSession)
        session.root, session.path, session.config = path, path / 'cut_farming.json', config
        session.data = deepcopy(data)
        session.save = lambda: None
        attempt_root = session.resume_attempt()
        if not attempt_root.is_relative_to(path / 'attempts'):
            raise ValueError('续跑局不在本刷切记录内。')
    except (ValueError, OSError, KeyError, TypeError) as error:
        return False, str(error)
    return True, None


def cut_snapshot(path, data, config):
    attempts = data.get('attempts') or []
    completed = [a for a in attempts if a.get('status') in {
        'no_cut_at_cutoff', 'abandoned', 'abandoned_and_restarted', 'found', 'found_for_validation'}]
    times = [a['elapsed_seconds'] for a in completed if type(a.get('elapsed_seconds')) in {int, float}]
    current = attempts[-1] if attempts else {}
    can_resume, resume_reason = resume_status(path, data, config)
    probe = current.get('last_probe')
    check_text = None
    if isinstance(probe, dict):
        check_text = {'absent': '未发现才能出众', 'present': '已确认才能出众，本局已保留',
            'unknown': '状态未读清，保持停止'}.get(probe.get('state'), '状态尚未确认')
        if probe.get('state') == 'present' and config.discard_found_for_validation and data.get('status') != 'found':
            if current.get('status') in {'abandoned', 'abandoned_and_restarted'}:
                check_text = '已确认才能出众，此局已按耐久测试弃养'
            elif data.get('status') == 'running':
                check_text = '已确认才能出众，耐久测试将在通知后弃养并继续'
        reason = {'initial': '开局或恢复检查', 'event': '事件后检查', 'interval': '间隔补查',
            'capture_gap': '采样异常补查', 'cutoff': '12月下弃养前检查', 'every_turn': '每回合检查'}.get(probe.get('check_reason'))
        if reason:
            check_text += ' · ' + reason
    notification = data.get('notification')
    notification_text = {'accepted': '推送服务已受理', 'failed': '推送失败，本局仍已保留',
        'not_configured': '未配置微信通知'}.get((notification or {}).get('status'), '尚未通知')
    return dict(status=data.get('status'), attempts=len(attempts), max_attempts=config.max_attempts,
        max_tp_items=config.max_tp_items, max_carrots=config.max_carrots,
        completed_attempts=len(completed), avg_attempt_seconds=sum(times) / len(times) if times else None,
        last_attempt_seconds=current.get('elapsed_seconds'), current_attempt_root=current.get('run_root'),
        checks=sum(a.get('checks', 0) for a in attempts), last_check_result=check_text, last_check_details=probe,
        tp_items_reserved=data.get('tp_items_reserved', 0), carrots_reserved=data.get('carrots_reserved', 0),
        notification=notification_text, notification_details=notification, can_resume=can_resume, resume_reason=resume_reason,
        stop=data.get('stop'), stop_reason=(data.get('stop') or {}).get('reason'))


def batch_records(root, endpoint):
    runs = Path(root) / 'runs'
    # Native batches, the standalone window, and the existing CLI batch layout.
    paths = set(runs.glob('cut_*/cut_farming.json'))
    paths.update(runs.glob('cut_*/batch/cut_farming.json'))
    paths.update(runs.glob('cut_farming/*/cut_farming.json'))
    records = []
    for file in paths:
        try:
            path, data, config = checked_batch(file.parent, root=root, endpoint=endpoint)
        except (OSError, ValueError, TypeError):
            continue
        snapshot = cut_snapshot(path, data, config)
        settings = {'strategy': config.strategy, 'cut': {key: item for key, item in config.public_dict().items()
            if key in CUT_OPTIONS}}
        options=read_json(path / 'cut_panel_options.json')
        policy=options.pop('larc_policy',data.get('larc_policy'))
        declaration=options.pop('support_profile',data.get('support_profile'))
        from core.auto_trainer.larc_support_skill_startup import normalize_mid_skill_learning
        settings['mid_skill_learning']=normalize_mid_skill_learning(options.pop('mid_skill_learning',data.get('mid_skill_learning')))
        settings['early_skill_allowlist']=options.pop('early_skill_allowlist',data.get('early_skill_allowlist',[]))
        retry=options.pop('race_retry',data.get('race_retry'))
        if retry is not None:settings['race_retry']=deepcopy(retry)
        rstyle=options.pop('running_style',data.get('running_style'))
        if rstyle is not None:settings['running_style']=rstyle
        rstyles=options.pop('race_running_styles',data.get('race_running_styles'))
        if rstyles:settings['race_running_styles']=deepcopy(rstyles)
        craces=options.pop('custom_races',data.get('custom_races'))
        if craces is not None:settings['custom_races']=deepcopy(craces)
        settings['cut'].update(options)
        if policy is not None:settings['larc_policy']=policy
        if declaration is not None:settings['support_profile']=deepcopy(declaration)
        label = path.parent.name if path.name == 'batch' else path.name
        status_label = {'found': '已刷到切', 'unknown': '待检查', 'stopped': '已停止',
            'limit_reached': '已到次数上限', 'route_failed': '流程中断', 'running': '运行记录', 'ready': '待开始'}.get(data.get('status'), '')
        records.append(dict(path=str(path), mode='cut', label=f"{label} · {len(data.get('attempts', []))} 局 · {status_label}",
            settings=settings, modified=file.stat().st_mtime, can_resume=snapshot['can_resume'],
            resume_reason=snapshot['resume_reason'], cut=snapshot))
    return sorted(records, key=lambda row: row['modified'], reverse=True)


def new_batch_defaults(records):
    """Reuse only a previously configured token-file path; never its contents or budgets."""
    settings = {'strategy': 'baseline', 'cut': {'character_id': '101001', 'max_attempts': 20,
        'max_tp_items': 0, 'max_carrots': 0, 'check_mode': 'event_triggered',
        'fallback_interval_turns': 4, 'debut_clock_retry': True, 'retain_screenshots': False,
        'discard_found_for_validation': False, 'restart_after_training_failure': False}}
    from core.auto_trainer.larc_support_skill_startup import normalize_mid_skill_learning
    settings['mid_skill_learning']=normalize_mid_skill_learning()
    settings['early_skill_allowlist']=[]
    for record in records:
        path = record.get('settings', {}).get('cut', {}).get('pushplus_token_file')
        if isinstance(path, str) and Path(path).is_file():
            settings['cut']['pushplus_token_file'] = path
            break
    return settings


def create_batch(root, config, *, template=None, decision_preferences=None,ai_options=None,larc_policy=None,support_profile=None,
                 mid_skill_learning=None,early_skill_allowlist=None,race_retry=None,running_style=None,race_running_styles=None,custom_races=None):
    from core.auto_trainer.larc_support_skill_startup import normalize_mid_skill_learning
    from scripts.larc_panel_setup import parse_early_skill_allowlist
    from core.auto_trainer.larc_support_skill_startup import new_mid_skill_learning
    learning=new_mid_skill_learning(mid_skill_learning)
    early=parse_early_skill_allowlist(early_skill_allowlist or [])
    from core.auto_trainer.larc_user_preferences import normalize, feature_available
    preferences=normalize(decision_preferences) if feature_available() else None
    from core.auto_trainer import larc_ai_options
    ai_options=larc_ai_options.normalize(ai_options)
    if ai_options['mode']!='local' and not larc_ai_options.feature_available(root):
        raise ValueError('当前版本未启用AI接管，请使用已包含此功能的测试版。')
    ai_connection={}
    if ai_options['mode']!='local':
        from desktop.larc_ai_client import provider_status
        provider=provider_status(root)
        if provider.get('configured') is not True:raise ValueError('请先保存AI接口地址、模型和密钥，再使用AI决策。')
        ai_connection={key:provider[key] for key in ('base_url','model')}
    from core.auto_trainer.run_support_profile import normalize_declaration
    declaration=normalize_declaration(support_profile)
    prepared=None
    if config.strategy=='baseline':
        from scripts.larc_panel_setup import prepare_panel_policy,save_policy_migration
        source=larc_policy
        if source is None:
            if template:
                source = read_json(Path(template) / 'goal.json').get('larc_policy', {})
            else:
                from core.auto_trainer.larc_policy import normalize_larc_policy
                source = normalize_larc_policy(None, 'cut')
        if (preferences is not None and decision_preferences is None and isinstance(source, dict)
                and source.get('revision') in ('1.9.7', '1.9.8', '1.9.9', '1.9.10', '1.9.11', '1.9.12', '2.0', '2.1', '2.2')):
            preferences = normalize(source.get('decision_preferences'))
        if isinstance(source, dict) and source.get('revision') in ('1.9.5', '1.9.6', '1.9.7', '1.9.8', '1.9.9', '1.9.10', '1.9.11', '1.9.12', '2.0', '2.1', '2.2'):
            from core.auto_trainer.larc_strategy_versions import new_goal_policy_defaults
            # Freeze factory defaults for this new batch before a template
            # attempt copies the batch policy over its newly created Goal.
            source = new_goal_policy_defaults(source)
        prepared=prepare_panel_policy(source,strategy='baseline',task_mode='cut')
    directory = Path(root) / 'runs' / f'cut_pacer_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}'
    session = CutSession(directory, config)
    if prepared is not None:
        session.data['larc_policy']=prepared['policy']
        session.data['larc_task_goal']=prepared['task_goal']
        save_policy_migration(directory/'session',prepared,label='cut-batch')
        atomic_json(directory/'session/larc_task_goal.json',prepared['task_goal'])
    options={'template_root': template, 'setup_confirmed': True}
    if race_retry is not None:
        from core.auto_trainer.race_retry_budget import normalize as normalize_retry
        session.data['race_retry']=normalize_retry(race_retry);options['race_retry']=deepcopy(session.data['race_retry'])
    if running_style is not None:
        from core.auto_trainer.larc_custom_race_catalog import normalize_running_style
        norm_style=normalize_running_style(running_style)
        if norm_style is not None:
            session.data['running_style']=norm_style;options['running_style']=norm_style
    if race_running_styles is not None:
        from core.auto_trainer.larc_custom_race_catalog import normalize_race_running_styles
        norm_rstyles=normalize_race_running_styles(race_running_styles)
        if norm_rstyles:
            session.data['race_running_styles']=norm_rstyles;options['race_running_styles']=deepcopy(norm_rstyles)
    if custom_races is not None:
        from core.auto_trainer.larc_custom_race_catalog import normalize_custom_races
        norm_races=normalize_custom_races(custom_races)
        session.data['custom_races']=norm_races;options['custom_races']=deepcopy(norm_races)
    if declaration is not None:
        session.data['support_profile']=deepcopy(declaration);options['support_profile']=deepcopy(declaration)
    session.data.update(mid_skill_learning=learning,early_skill_allowlist=early)
    options.update(mid_skill_learning=learning,early_skill_allowlist=early)
    if 'larc_policy' in session.data:options['larc_policy']=session.data['larc_policy']
    session.data['ai_options']=ai_options;session.data['ai_connection']=ai_connection;session.save()
    options['ai_options']=ai_options
    options['ai_connection']=ai_connection
    if preferences is not None:
        session.data['decision_preferences']=preferences;session.save()
        options['decision_preferences']=preferences
    atomic_json(directory / 'cut_panel_options.json', options)
    return session.root


def create_cut_control_service(state_dir, profiles, *, umaa_root, lease=None, controller_factory=None,
                               control_factory=None):
    """Attach the same RunControl; Dashboard start while paused means resume."""
    def factory(profile, port):
        from scripts.larc_umaa_common import occupancy
        settings = profile.runner_config
        config = CutFarmingConfig.from_dict(settings['config'])
        control = (control_factory or RunControl)(changed=port.runtime_state)
        port.attach_runtime(control)
        heartbeat = None
        try:
            heartbeat = RunnerHeartbeatWorker(port=port)
            with heartbeat:
                if control_factory is not None:
                    # The standalone license gate runs on this worker, so the
                    # host can still process Stop/Close. No device lease,
                    # controller, attempt or resource accounting precedes it.
                    control.checkpoint('cut_license_start')
                device_lease = lease or occupancy(umaa_root)
                with device_lease.hold(Path(umaa_root) / 'runs/device-occupancy',
                        f'127.0.0.1:{config.device_port}', 'pacer'):
                    result = run_controller(settings, config, control)
            status = result.get('status', 'stopped')
            reason = 'cut_' + re.sub(r'[^a-z0-9_.:-]', '_', str(status).lower())[:80]
            port.finish(outcome='completed' if status in {'found', 'limit_reached', 'completed'} else 'stopped',
                reason=reason, evidence_sha256=None)
        except RunStopped as exc:
            reason='runner_heartbeat_failed' if heartbeat is not None and heartbeat.failure is not None else exc.reason
            port.finish(outcome='stopped', reason=reason, evidence_sha256=None)
        except Exception as error:
            reason=('runner_heartbeat_failed' if heartbeat is not None and heartbeat.failure is not None
                else 'device_busy' if str(error) == 'device_busy' else 'runner_error')
            port.finish(outcome='stopped', reason=reason, evidence_sha256=None)
            raise
        finally:
            control.finish()
            try:
                from core.auto_trainer.pacer_storage import maintain
                maintain(Path(__file__).resolve().parents[1], min_interval=0,
                         exclude_batch=settings['batch_root'])
            except Exception as error:
                import logging
                logging.getLogger(__name__).warning("截图维护未完成：%s", type(error).__name__)

    def run_controller(settings, config, control):
        from scripts.cut_farming import CutFarmingController
        # The host check precedes this worker's lease. A batch may expire in
        # between; reject its disappearance before CutSession can recreate it.
        batch_root, data, saved_config = checked_batch(settings['batch_root'],
            root=Path(__file__).resolve().parents[1],
            endpoint=f'127.0.0.1:{config.device_port}')
        if settings.get('resume') is True:
            can_resume, _ = resume_status(batch_root, data, saved_config)
            if not can_resume:
                return {'status':data.get('status', 'stopped'), 'reason':'cut_resume_not_allowed',
                        'session_root':str(batch_root)}
        session = CutSession(batch_root, config)
        if settings.get('entry_mode')=='current_career' and settings.get('resume') is not True:
            session.data['entry_context']={'mode':'current_career',
                'previous_transaction':settings.get('previous_transaction'),
                'old_actions_replayed':False,'previous_budgets_copied':False}
            session.save()
        controller = (controller_factory or CutFarmingController)(session, control=control)
        # The dashboard owns the final sweep after releasing its control port.
        controller._defer_storage_maintenance = True
        if data.get('ss_sieve_cycle'):
            return controller.run(execute=True, resume=settings.get('resume') is True)
        # Count only real calls to the existing state probe. This adds
        # display statistics, no sampling or alternate cut decisions.
        probe = controller.probe
        def counted_probe(*args, **kwargs):
            result = probe(*args, **kwargs)
            if session.data['attempts']:
                attempt = session.data['attempts'][-1]
                attempt['checks'] = attempt.get('checks', 0) + 1
                session.save()
            return result
        controller.probe = counted_probe
        entry_options={'take_over_current':True} if settings.get('entry_mode')=='current_career' else {}
        result = controller.run(execute=True, template_root=settings.get('template_root'),
            resume=settings.get('resume') is True,**entry_options)
        return result
    service = DashboardControlService(state_dir=Path(state_dir),
        profiles={name: ControlProfile(name, dict(settings)) for name, settings in profiles.items()},
        runner_factory=factory)
    if service.snapshot()['state'] not in {'idle','stopped'} or (Path(state_dir)/'runner.lock').exists():
        # A backend restart leaves the old advisory control marker on disk.
        # Obtain the actual device lease before retiring it, so a live U1/D1
        # or another Pacer worker can never be displaced by this recovery.
        from scripts.larc_umaa_common import occupancy
        if len(profiles)!=1:raise ValueError('cut_reconnect_profile_ambiguous')
        config=CutFarmingConfig.from_dict(next(iter(profiles.values()))['config'])
        device_lease=lease or occupancy(umaa_root)
        with device_lease.hold(Path(umaa_root)/'runs/device-occupancy',
                f'127.0.0.1:{config.device_port}','pacer'):
            service.recover_detached_runner()
    return service
