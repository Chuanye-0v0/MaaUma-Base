"""Private stdio control host for the native UMAA Pacer page.

Opening the page/listing/snapshotting never connects to or captures a device.
Only an explicit start request enters the existing controlled career runner.
"""
import argparse
from contextlib import contextmanager
import json
from copy import deepcopy
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.larc_panel_setup import (DEFAULT_TARGETS, create_panel_run, parse_targets,
    parse_strategy_options, parse_mei_breaks, parse_early_skill_allowlist,
    update_goal, save_mei_breaks, save_ramonu_present)
from scripts.larc_panel_runner import create_larc_control_service, submit_button
from scripts.larc_umaa_common import occupancy

ACTIVE = {'starting', 'running', 'pause_requested', 'paused', 'resume_requested',
          'stop_requested', 'unresponsive'}
from core.cut_portable_runtime import device_ports
ENDPOINTS = {f'127.0.0.1:{port}' for port in device_ports()}


_JSON_CACHE = {}


def read_json(path):
    try:
        p = Path(path)
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
        cached = _JSON_CACHE.get(key)
        if cached is not None:
            return dict(cached)
        value = json.loads(p.read_text(encoding='utf-8-sig'))
        if isinstance(value, dict):
            if len(_JSON_CACHE) > 256:
                _JSON_CACHE.clear()
            _JSON_CACHE[key] = value
            return dict(value)
        return {}
    except (OSError, ValueError):
        return {}



def entry_diagnostics(summary):
    """Small existing observation fields only; never config, OCR text or paths."""
    if not isinstance(summary, dict):
        return {}
    takeover = summary.get('live_takeover')
    scene = takeover.get('current_scene') if isinstance(takeover, dict) else None
    if not isinstance(scene, dict):
        return {}
    result = {}
    for key in ('owner', 'state', 'safe_reason', 'race_reason'):
        value = scene.get(key)
        if isinstance(value, str) and len(value) <= 160 and all(char.isalnum() or char in '_-.: ' for char in value):
            result[key] = value
    for key in ('entered', 'cut_debut_race', 'ordinary_clock_retry'):
        if isinstance(scene.get(key), bool):
            result[key] = scene[key]
    return result


class PacerHost:
    def __init__(self, umaa_root, *, root=ROOT, service_factory=create_larc_control_service, lease=None,
                 cut_service_factory=None, immutable_career_settings=False):
        self.root = Path(root).resolve()
        self.umaa_root = Path(umaa_root).resolve(strict=True)
        self.lease = lease or occupancy(self.umaa_root)
        self.service_factory = service_factory
        self.cut_service_factory = cut_service_factory
        self.immutable_career_settings = immutable_career_settings
        self.service = None
        self.run_root = None
        self.endpoint = None
        self.mode = 'career'
        self.closing = False
        self.responses = {}

    def checked_run(self, value, endpoint=None):
        path = Path(value).resolve(strict=True)
        if path.parent != self.root / 'runs' or not path.name.startswith('larc_panel_'):
            raise ValueError('请选择本面板列出的育成记录。')
        progress = read_json(path / 'progress.json')
        if progress.get('run_session_id') != path.name or (endpoint and progress.get('device') != endpoint):
            raise ValueError('所选记录与当前设备或局号不一致。')
        settlement = read_json(path / 'session/larc_terminal_settlement.json')
        if settlement.get('status') == 'complete' or settlement.get('phase') == 'complete':
            raise ValueError('这局已正常结算，不会重复启动。')
        return path

    def records(self, endpoint, mode='career'):
        if mode == 'cut':
            from scripts.larc_umaa_cut import batch_records
            return batch_records(self.root, endpoint)
        rows = []
        for path in self.root.joinpath('runs').glob('larc_panel_*'):
            try:
                self.checked_run(path, endpoint)
            except (OSError, ValueError):
                continue
            progress = read_json(path / 'progress.json')
            rows.append(dict(path=str(path), mode='career', label=f"{path.name[11:26]} · 已推进 {progress.get('actions_completed', 0)} 步",
                             settings=self.settings(path), modified=path.joinpath('progress.json').stat().st_mtime))
        return sorted(rows, key=lambda row: row['modified'], reverse=True)

    def settings(self, path):
        goal = read_json(path / 'goal.json')
        support = read_json(path / 'session/support_card_profile.json')
        cards = support.get('cards', [])
        mei = next((card for card in cards if card.get('character_id') == 'satake_mei'), {})
        from core.auto_trainer.larc_policy import VARIANT_BRANCHES
        result = dict(targets=goal.get('target_stats', DEFAULT_TARGETS),
            strategy=VARIANT_BRANCHES.get(goal.get('larc_policy', {}).get('variant', 'alpha'), 'alpha'),
            larc_policy=deepcopy(goal.get('larc_policy', {})),
            failure_limit=goal.get('training_failure_rate_limit', 25),
            mei_breaks=mei.get('limit_breaks'),
            with_ramonu=any(str(card.get('card_id')) == '30134' for card in cards),
            early_skill_allowlist=goal.get('skill_timing', {}).get('early_skill_allowlist', []),
            arc_policy=goal.get('skill_timing', {}).get('arc_policy', {}),
            support=cards)
        from core.career_panel_presets import settings_from_storage
        result.update(settings_from_storage(read_json(path / 'session/umaa_panel_options.json')))
        # An old form snapshot is not authority over the actual running goal.
        result['larc_policy']=deepcopy(goal.get('larc_policy',{}))
        result['strategy']=VARIANT_BRANCHES.get(result['larc_policy'].get('variant','alpha'),'alpha')
        from core.auto_trainer.larc_support_skill_startup import normalize_mid_skill_learning
        timing=goal.get('skill_timing',{})
        result['early_skill_allowlist']=timing.get('early_skill_allowlist',[])
        result['mid_skill_learning']=normalize_mid_skill_learning(timing.get('mid_skill_learning'),
            legacy_enabled=result.get('with_ramonu') is True and bool(result['early_skill_allowlist']))
        from core.auto_trainer.larc_manual_handoff import load_options
        # Initial preset is immutable. The editable current-run pause plan is
        # exposed separately by snapshot(), never folded into resume options.
        result['manual_handoff'] = load_options(path)
        if goal.get('race_retry') is not None:result['race_retry']=deepcopy(goal['race_retry'])
        if goal.get('running_style') is not None:result['running_style']=goal['running_style']
        if goal.get('race_running_styles'):result['race_running_styles']=deepcopy(goal['race_running_styles'])
        if goal.get('custom_races'):result['custom_races']=list(goal['custom_races'])
        continuation = read_json(path / 'session/larc_continuation_options.json')
        if continuation.get('run_session_id') == path.name:
            result['continuation'] = continuation.get('config')
        return result

    def snapshot(self, endpoint=None, mode=None, selected_run_root=None):
        state = self.service.snapshot() if self.service else {'state': 'idle', 'runtime': None}
        mode = self.mode if state['state'] in ACTIVE else mode or self.mode
        endpoint = self.endpoint if state['state'] in ACTIVE else endpoint or self.endpoint
        busy = self.lease.probe(self.umaa_root / 'runs/device-occupancy', endpoint) if endpoint in ENDPOINTS else None
        record = {}
        cut = None
        selected_root = self.run_root if mode == self.mode else None
        if selected_root and mode == 'career' and not selected_run_root:
            from scripts.career_batch_runtime import current_run
            selected_root = current_run(selected_root, endpoint)
        if mode=='cut' and state['state'] not in ACTIVE and selected_run_root:
            from scripts.larc_umaa_cut import checked_batch
            selected_root,_,_=checked_batch(selected_run_root,root=self.root,endpoint=endpoint)
        elif mode == 'career' and state['state'] not in ACTIVE and selected_run_root:
            if self.immutable_career_settings:
                # Viewing an ended/handed-off record is not permission to resume it.
                from core.career_panel_presets import record_for_path
                row = record_for_path(selected_run_root, root=self.root,
                    endpoint=endpoint, review_only=True)
                selected_root = Path(row['path'])
            else:
                selected_root = self.checked_run(selected_run_root, endpoint)
        record_root = selected_root
        if selected_root and mode == 'cut':
            from scripts.larc_umaa_cut import checked_batch, cut_snapshot
            try:
                path, data, config = checked_batch(selected_root, root=self.root, endpoint=endpoint)
                cut = cut_snapshot(path, data, config)
                candidate = cut.get('current_attempt_root')
                record_root = Path(candidate).resolve() if candidate else None
                if record_root and not record_root.is_relative_to(selected_root / 'attempts'):
                    record_root = None
            except (OSError, ValueError, KeyError, TypeError) as error:
                cut = dict(status='unreadable', can_resume=False, resume_reason=str(error))
                record_root = None
        if record_root:
            record = read_json(record_root / 'progress.json')
            from core.auto_trainer.larc_support_skill_startup import learning_status_snapshot
            record["mid_skill_learning_status"]=learning_status_snapshot(record_root)
            from core.auto_trainer.larc_manual_handoff import snapshot as manual_snapshot
            record['manual_handoff'] = manual_snapshot(record_root)
            continuation = read_json(record_root / 'session/larc_continuation_state.json')
            if continuation.get('run_session_id') == record_root.name:
                record['continuation'] = continuation
            summaries = list(record_root.glob('autonomous_*/summary.json'))
            if summaries:
                summary_path = max(summaries, key=lambda path: path.stat().st_mtime)
                summary = read_json(summary_path)
                turns = summary.get('turns') or []
                record['last_turn'] = turns[-1] if turns else {}
                record['summary_status'] = summary.get('status')
                record['stop_reason'] = summary.get('stop_reason')
                record['error'] = summary.get('error')
                record['elapsed_seconds'] = summary.get('elapsed_seconds')
                record['entry_diagnostics'] = entry_diagnostics(summary)
                try:
                    from scripts.pacer_stop_diagnostics import extract_stop_diagnostics
                    record['stop_diagnostics'] = extract_stop_diagnostics(summary, summary_path)
                except Exception:
                    record['stop_diagnostics'] = {'not_recorded': True}
        if record_root and summaries:
            try:
                from desktop.native_stop_diagnostics import augment_stop_record
                augment_stop_record(record,summary,summary_path)
            except Exception:
                pass  # Diagnostic reads never affect the task or its ordinary snapshot.
        pending_path = self.umaa_root / 'runs/mfa-live/active.json'
        pending = read_json(pending_path)
        luck_session = getattr(self, '_realtime_luck', None)
        luck = None
        live_snapshot = state['state'] in ACTIVE
        # Active polling carries the owned run selector. Historical selection is idle-only.
        if luck_session is not None:
            try:
                target_root = record_root if (live_snapshot or selected_run_root is None or mode == self.mode) else None
                luck = luck_session.snapshot(target_root, record, mode, live=live_snapshot)
            except Exception:
                pass  # A display-only observer must not change a runtime snapshot.
        return dict(mode=mode, control=state, endpoint=endpoint, run_root=str(selected_root) if selected_root else None, cut=cut,
            manual_handoff=record.get('manual_handoff'),
            mid_skill_learning_status=record.get("mid_skill_learning_status"),
            device_owner=busy, daily_pending=pending_path.exists() and pending.get('endpoint') in {None, endpoint},
            account={'status': 'unconfirmed', 'label': '未从游戏确认（不登录、不切号）'},
            record=record, closing=self.closing, realtime_luck=luck)

    def start(self, request, *, sieve_document=None):
        state = self.service.snapshot()['state'] if self.service else 'idle'
        mode = request.get('mode', self.mode if state in ACTIVE else 'career')
        if self.run_root and self.mode == 'career':
            from scripts.career_batch_runtime import current_run
            self.run_root = current_run(self.run_root, self.endpoint)
        if self.closing:
            raise ValueError('面板正在关闭，不能启动。')
        if mode == 'career' and request.get('new_career') is True and state in ACTIVE:
            raise ValueError('旧任务尚未结束或已切换到其他任务，未开始新一轮；不会自动续跑旧局。')
        selected = request.get('run_root') or (self.run_root if state == 'paused' and self.mode == 'career' else None)
        if mode == 'career' and selected:
            from core.auto_trainer.larc_manual_handoff import snapshot as manual_snapshot
            manual = manual_snapshot(self.checked_run(selected, request.get('endpoint')))
            if manual['terminal']:
                raise ValueError('本局已交还用户收尾，不会自动恢复。')
            if manual['status'] == 'waiting':
                raise ValueError('请完成人工处理后点击“继续本局”，普通开始不会确认人工交接。')
        if state == 'paused':
            if (mode != self.mode or request.get('endpoint') not in (None, self.endpoint)
                    or request.get('run_root') not in (None, str(self.run_root))):
                raise ValueError('当前任务已暂停，请继续原任务；不能更换模式、设备或记录。')
            submit_button(self.service, 'start', request_id=request['request_id'])
            return self.snapshot()
        if state in ACTIVE:
            raise ValueError('原任务仍在运行或收尾，不能重复启动。')
        endpoint = request.get('endpoint')
        if endpoint not in ENDPOINTS:
            raise ValueError('请先在日活页选定支持的 MuMu 设备。')
        busy = self.snapshot(endpoint)
        if busy['device_owner'] or busy['daily_pending'] or request.get('daily_running') is True:
            raise ValueError('同设备仍被日活或其他任务占用，请停止原任务后再开始。')
        options = request.get('options') or {}
        career_resume=mode=='career' and self.immutable_career_settings and bool(request.get('run_root'))
        if career_resume:
            # Explicit reattachment owns this saved run, never the currently
            # edited preset or a newly supplied AI/model/weight configuration.
            options=self.settings(self.checked_run(request['run_root'],endpoint))
        if mode=='career' and self.immutable_career_settings:
            from core.career_panel_presets import sanitize_settings
            options=sanitize_settings(options,self.root,new_configuration=not career_resume)
        if mode == 'career':
            from core.auto_trainer.larc_manual_handoff import normalize as normalize_manual
            options = {**options, 'manual_handoff': normalize_manual(options.get('manual_handoff'))}
        continuation = options.get('continuation') or (options.get('cut') or {}).get('continuation')
        if continuation is not None:
            try:
                from core.auto_trainer.larc_continuation import normalize, feature_available
            except ImportError:
                raise ValueError('当前安装版本未包含续养执行功能。')
            continuation = normalize(continuation)
            if continuation['enabled'] and not feature_available(self.root):
                raise ValueError('当前安装版本未包含续养执行功能；稳定版不会自动启用。')
            if mode == 'cut':
                options = {**options, 'cut': {**(options.get('cut') or {}),
                    'continuation': continuation if continuation['enabled'] else None}}
            elif continuation['enabled'] and any(continuation[key]['on_fail'] == 'restart'
                    for key in ('junior', 'inheritance')):
                raise ValueError('自动回刷切需要已有刷切批次及次数预算；普通育成请选择暂停保留。')
        if mode == 'cut':
            if sieve_document is not None:
                return PacerHost.start_cut(self, request, endpoint, options, sieve_document=sieve_document)
            return self.start_cut(request, endpoint, options)
        connection={}
        if self.immutable_career_settings:
            if career_resume:
                from core.auto_trainer.larc_ai_options import load_options
                load_options(request['run_root'])  # Validate ownership without changing the snapshot.
            else:
                from core.career_panel_presets import ai_connection
                connection=ai_connection(self.root,options)
        targets = parse_targets(options.get('targets', DEFAULT_TARGETS))
        strategy, failure = parse_strategy_options(options.get('strategy', 'baseline'), options.get('failure_limit', 25))
        if not career_resume:
            strategy = 'baseline'  # Explicit new/save request; saved-run resume above remains immutable.
        breaks = parse_mei_breaks(options.get('mei_breaks'))
        limit = int(options.get('max_actions', 72))
        if not 1 <= limit <= 72:
            raise ValueError('最多推进回合数请填 1～72。')
        early = parse_early_skill_allowlist(options.get('early_skill_allowlist', []))
        from scripts.larc_ordinary_setup import parse_ordinary_setup, save_ordinary_setup
        setup = parse_ordinary_setup(options.get('setup'))
        from scripts.larc_panel_setup import parse_arc_skill_policy
        arc_policy = parse_arc_skill_policy(options['arc_policy']) if 'arc_policy' in options else None
        port = int(endpoint.rsplit(':', 1)[1])
        # Reject invalid input before creating a record or changing an old goal.
        if request.get('run_root'):
            path = self.checked_run(request['run_root'], endpoint)
        else:
            path = create_panel_run(device_port=port, targets=targets, runs_dir=self.root / 'runs',
                strategy=strategy, failure_limit=failure, mei_breaks=breaks,
                with_ramonu=options.get('with_ramonu') is True, early_skill_allowlist=early, arc_policy=arc_policy,
                larc_policy=options.get('larc_policy'),task_mode='career',mid_skill_learning=options.get('mid_skill_learning'),
                support_profile=options.get('support_profile'),race_retry=options.get('race_retry'),
                running_style=options.get('running_style'),race_running_styles=options.get('race_running_styles'),
                custom_races=options.get('custom_races'))
            if (options.get('larc_policy') or {}).get('revision') in ('1.9.5', '1.9.6', '1.9.7', '1.9.8', '1.9.9', '1.9.10', '1.9.11', '1.9.12', '2.0', '2.1', '2.2'):
                # The factory already combined the selected weights and its
                # defaults. Reuse that policy for the ordinary setup save.
                options = {**options, 'larc_policy': read_json(path / 'goal.json')['larc_policy']}
        from scripts.larc_control_window import build_argv
        argv = build_argv(path, limit, device_port=port, retain_training=options.get('retain_training') is True)
        argv += ['--ordinary-career-opening', '--same-account-reconnect']
        if '--d1-startup-only' in argv:
            argv.remove('--d1-startup-only')
        if options.get('terminal_skills', True) is not True:
            argv.append('--d1-startup-only')
        if not career_resume:
            update_goal(path, targets, strategy=strategy, failure_limit=failure, early_skill_allowlist=early,
                arc_policy=arc_policy,larc_policy=options.get('larc_policy'),task_mode='career',
                mid_skill_learning=options.get('mid_skill_learning'),race_retry=options.get('race_retry'),
                running_style=options.get('running_style'),race_running_styles=options.get('race_running_styles'),
                custom_races=options.get('custom_races'))
            save_mei_breaks(path, breaks)
            save_ramonu_present(path, options.get('with_ramonu') is True)
            save_ordinary_setup(path, read_json(path/'progress.json'), setup)
        if continuation is not None:
            from scripts.larc_continuation_runtime import save_options, acknowledge_pending
            if not career_resume:save_options(path, continuation)
            if request.get('run_root'):
                acknowledge_pending(path)
        options = {**options, 'setup':setup}
        if not career_resume:
            options['strategy']='baseline'
            options['larc_policy']=deepcopy(read_json(path/'goal.json')['larc_policy'])
        from scripts.ug_parent_one_turn_pipeline import _atomic_write_json
        if not career_resume:
            if self.immutable_career_settings:
                from core.career_panel_presets import save_new_run
                save_new_run(path,endpoint,options,connection)
            else:
                from core.auto_trainer.larc_manual_handoff import save_options as save_manual
                from core.career_panel_presets import settings_for_storage
                save_manual(path, options['manual_handoff'])
                _atomic_write_json(path / 'session/umaa_panel_options.json', settings_for_storage(options))
        if self.service:
            self.service.close()
        self.run_root, self.endpoint, self.mode = path, endpoint, 'career'
        self.service = self.service_factory(self.root / 'runs/umaa-panel-control' / str(port),
            {'current': argv}, umaa_root=self.umaa_root)
        submit_button(self.service, 'start', request_id=request['request_id'], profile_id='current')
        return self.snapshot()

    def update_manual_handoff_plan(self, request):
        """Edit only the owned run's pause plan; never interrupt live input."""
        if self.run_root and self.mode == 'career':
            from scripts.career_batch_runtime import current_run
            self.run_root = current_run(self.run_root, self.endpoint)
        if request.get('mode', 'career') != 'career':
            raise ValueError('暂停计划仅用于当前普通育成。')
        endpoint = request.get('endpoint')
        if endpoint not in ENDPOINTS or not request.get('run_root'):
            raise ValueError('请指定原局记录和同一设备。')
        if self.immutable_career_settings:
            from core.career_panel_presets import record_for_path
            row = record_for_path(request['run_root'], root=self.root,
                endpoint=endpoint, review_only=True)
            path = Path(row['path'])
        else:
            path = self.checked_run(request['run_root'], endpoint)
        state = self.service.snapshot() if self.service else {'state': 'idle'}
        if state['state'] in ACTIVE and (self.mode != 'career'
                or self.run_root != path or self.endpoint != endpoint):
            raise ValueError('当前任务仍在运行或暂停，请修改原来的同局同设备暂停计划。')
        from core.auto_trainer.larc_manual_handoff import update_plan
        receipt = update_plan(path, endpoint, request.get('plan'),
            request_id=request['request_id'], expected_revision=request.get('expected_revision'))
        # No submit_button, ADB, lease acquisition or implicit acknowledgement:
        # changes take effect only at the runner's next safe checkpoint.
        result = self.snapshot(endpoint, 'career', str(path))
        result['plan_receipt'] = receipt
        return result

    def end_manual_handoff_task(self, request):
        """Stop only the explicitly named old controller; never input to game.

        The next start is a separate request with the newly selected document.
        Keep old Goal/progress/handoff records, including any unconfirmed
        transaction, as history rather than acknowledging or replaying them.
        """
        endpoint = request.get('endpoint')
        if request.get('mode', 'career') != 'career' or endpoint not in ENDPOINTS or not request.get('run_root'):
            raise ValueError('请指定已手动结束的原育成记录和同一设备。')
        path = Path(request['run_root']).resolve(strict=True)
        progress = read_json(path / 'progress.json')
        if (path.parent != self.root / 'runs' or not path.name.startswith('larc_panel_')
                or progress.get('run_session_id') != path.name or progress.get('device') != endpoint
                or self.mode != 'career' or self.run_root != path or self.endpoint != endpoint):
            raise ValueError('原任务或设备已改变，请刷新后重新选择。')
        from core.auto_trainer.larc_manual_handoff import snapshot as manual_snapshot
        manual = manual_snapshot(path)
        if manual.get('status') != 'waiting' and manual.get('terminal') is not True:
            raise ValueError('当前记录没有待结束的人工交接。')
        control = self.service.snapshot() if self.service else {'state': 'idle', 'tap_in_flight': False}
        if (control['state'] not in {'paused', 'idle', 'stopped'}
                or control.get('tap_in_flight') is True
                or control['state'] == 'paused' and control.get('tap_in_flight') is not False):
            raise ValueError('请等待后台确认安全暂停或停止，再开始新一轮。')
        if control['state'] == 'paused':
            submit_button(self.service, 'stop', request_id=request['request_id'])
        return self.snapshot(endpoint, 'career')

    def resume_manual_handoff(self, request):
        """An explicit human handoff confirmation, never an implicit Start."""
        if self.run_root and self.mode == 'career':
            from scripts.career_batch_runtime import current_run
            self.run_root = current_run(self.run_root, self.endpoint)
        if request.get('mode', 'career') != 'career':
            raise ValueError('人工交接仅用于当前普通育成。')
        endpoint = request.get('endpoint')
        if endpoint not in ENDPOINTS or not request.get('run_root'):
            raise ValueError('请指定原局记录和同一设备。')
        from scripts.career_sieve_cycle import current_sieve_attempt
        sieve = current_sieve_attempt(request['run_root'], root=self.root, endpoint=endpoint)
        path = sieve[0] if sieve is not None else self.checked_run(request['run_root'], endpoint)
        from core.auto_trainer.larc_manual_handoff import snapshot as manual_snapshot, acknowledge
        manual = manual_snapshot(path)
        if manual['terminal'] or manual['status'] != 'waiting' or manual['stage'] not in {'first_arc', 'date'}:
            raise ValueError('仅首次凯旋门或育成日期的待确认交接可以继续。')
        state = self.service.snapshot() if self.service else {'state': 'idle', 'tap_in_flight': False}
        if state['state'] == 'paused':
            mode, owner_root = ('cut', sieve[1]) if sieve is not None else ('career', path)
            if self.mode != mode or self.run_root != owner_root or self.endpoint != endpoint:
                raise ValueError('请继续原来的同局同设备任务。')
            if state.get('tap_in_flight') is not False:
                raise ValueError('请等待后台确认暂停且没有输入在途。')
            acknowledge(path, endpoint, explicit=True, pause_id=request.get('pause_id'))
            submit_button(self.service, 'start', request_id=request['request_id'])
            return self.snapshot()
        if state['state'] not in {'idle', 'stopped'}:
            raise ValueError('请等待原后台完成暂停或停止。')
        if state.get('tap_in_flight') is not False:
            raise ValueError('请等待后台确认停止且没有输入在途。')
        busy = self.snapshot(endpoint)
        if busy['device_owner'] or busy['daily_pending'] or request.get('daily_running') is True:
            raise ValueError('同设备仍被其他任务占用，请先等待原任务停止。')
        acknowledge(path, endpoint, explicit=True, pause_id=request.get('pause_id'))
        if sieve is not None:
            from scripts.career_workflow_host import _start_sieve
            return _start_sieve(self, {'action': 'start', 'mode': 'cut', 'endpoint': endpoint,
                'run_root': str(sieve[1]), 'request_id': request['request_id']}, sieve[2])
        return self.start({**request, 'action': 'start', 'mode': 'career',
                           'run_root': str(path), 'options': self.settings(path)})

    def start_cut(self, request, endpoint, options, *, sieve_document=None):
        from scripts.larc_umaa_cut import (checked_batch, create_batch,
            create_cut_control_service, cut_options, resume_status)
        strategy, _ = parse_strategy_options(options.get('strategy', 'baseline'), 25)
        if request.get('run_root'):
            path, data, config = checked_batch(request['run_root'], root=self.root, endpoint=endpoint)
            if bool(data.get('ss_sieve_cycle')) != (sieve_document is not None):
                raise ValueError('请在对应的原工作流中续跑SS筛局记录。')
            can_resume, reason = resume_status(path, data, config)
            if not can_resume:
                raise ValueError('这批刷切不能续跑：' + str(reason))
            # Resuming uses the saved batch configuration, never edits its
            # resource budgets, character or target while reusing old progress.
            template = read_json(path / 'cut_panel_options.json').get('template_root')
            resume = True
        elif sieve_document is not None:
            from scripts.career_sieve_cycle import create_sieve_batch
            path, config = create_sieve_batch(self.root, endpoint, sieve_document)
            template, resume = None, False
        else:
            requested_policy=options.get('larc_policy')
            if requested_policy is None and strategy!='baseline':requested_policy={'variant':strategy}
            strategy='baseline'
            config, template = cut_options(options.get('cut'), endpoint=endpoint, strategy=strategy, root=self.root)
            path = create_batch(self.root, config, template=template,
                decision_preferences=(options.get('cut') or {}).get('decision_preferences'),
                ai_options=(options.get('cut') or {}).get('ai_options'),larc_policy=requested_policy,support_profile=options.get('support_profile'),
                mid_skill_learning=options.get('mid_skill_learning'),
                early_skill_allowlist=options.get('early_skill_allowlist',[]),race_retry=options.get('race_retry'),
                running_style=options.get('running_style'),race_running_styles=options.get('race_running_styles'),
                custom_races=options.get('custom_races'))
            resume = False
        if self.service:
            self.service.close()
        self.run_root, self.endpoint, self.mode = path, endpoint, 'cut'
        port = int(endpoint.rsplit(':', 1)[1])
        self.service = (self.cut_service_factory or create_cut_control_service)(
            self.root / 'runs/umaa-panel-control' / str(port), {'current': {
                'batch_root': str(path), 'config': config.public_dict(),
                'template_root': template, 'resume': resume}}, umaa_root=self.umaa_root, lease=self.lease)
        submit_button(self.service, 'start', request_id=request['request_id'], profile_id='current')
        return self.snapshot()

    def dispatch(self, request):
        action = request.get('action')
        if self.run_root and self.mode == 'career':
            from scripts.career_batch_runtime import current_run
            self.run_root = current_run(self.run_root, self.endpoint)
        if action == 'preview_decision_preferences':
            from desktop.scoring_preview import preview
            return preview(request)
        if action in {'runtime_stop_evidence','prepare_runtime_stop_report','submit_runtime_stop_report'}:
            from desktop.runtime_stop_reports import dispatch_for_host
            return dispatch_for_host(self,request)
        if action == 'export_stop_diagnostics':
            from desktop.native_stop_diagnostics import export_for_host
            return export_for_host(self,request)
        endpoint = request.get('endpoint')
        mode = request.get('mode', self.mode if self.service and self.service.snapshot()['state'] in ACTIVE else 'career')
        if mode not in {'career', 'cut'}:
            raise ValueError('panel_mode_invalid')
        if endpoint is not None and endpoint not in ENDPOINTS:
            raise ValueError('device_endpoint_not_allowed')
        if action == 'list':
            records = self.records(endpoint, mode)
            result = dict(mode=mode, records=records, snapshot=self.snapshot(endpoint, mode))
            if mode == 'cut':
                from scripts.larc_umaa_cut import new_batch_defaults
                result['defaults'] = new_batch_defaults(records)
            return result
        if action == 'snapshot':
            return self.snapshot(endpoint, mode, request.get('run_root'))
        if action == 'skill_choices':
            from scripts.larc_skill_choices import search_choices
            return search_choices(request.get('query', ''), target=request.get('final_target'),
                                  limit=request.get('limit', 100))
        if action == 'decision_preferences_schema':
            from core.auto_trainer.larc_user_preferences import feature_available, DEFAULTS
            from core.auto_trainer.larc_score_tuning import schema
            return {'available':feature_available(), 'defaults':DEFAULTS,'tuning_schema':schema()}
        if action == 'validate_decision_preferences':
            from core.auto_trainer.larc_user_preferences import feature_available,normalize
            if not feature_available():raise ValueError('高级评分设置仅在测试版可用。')
            if 'profile' in request:
                from desktop.scoring_exchange import checked_profile
                if 'preferences' in request:raise ValueError('请提供完整评分方案或本机设置，不要同时提供两份。')
                profile=checked_profile(request['profile'])
                return {'profile':profile,'preferences':profile['preferences'],'device_actions':0}
            return {'preferences':normalize(request.get('preferences')),'device_actions':0}
        if action == 'continuation_schema':
            try:
                from core.auto_trainer.larc_continuation import defaults, feature_available, KINDS
            except ImportError:
                return {'available': False}
            return {'available': feature_available(self.root), 'defaults': defaults(),
                    'condition_kinds': sorted(KINDS), 'automatic_limit': 'first_expedition_before_departure'}
        if action in {'start', 'resume', 'resume_manual_handoff', 'end_manual_handoff_task', 'update_manual_handoff_plan', 'pause', 'stop', 'close'}:
            identifier = request.get('request_id')
            if not isinstance(identifier, str) or not 8 <= len(identifier) <= 128:
                raise ValueError('request_id_required')
            canonical = json.dumps(request, sort_keys=True, ensure_ascii=False)
            if identifier in self.responses:
                previous, result = self.responses[identifier]
                if canonical != previous:
                    raise ValueError('request_id_conflict')
                return result
            if action == 'start':
                result = self.start(request)
            elif action == 'resume_manual_handoff':
                result = self.resume_manual_handoff(request)
            elif action == 'end_manual_handoff_task':
                result = self.end_manual_handoff_task(request)
            elif action == 'update_manual_handoff_plan':
                result = self.update_manual_handoff_plan(request)
            elif action == 'resume':
                if not self.service or self.service.snapshot()['state'] != 'paused':
                    raise ValueError('请先暂停当前任务，再继续。')
                result = self.start(request)
            else:
                if action == 'close':
                    self.closing = True
                    if self.service:
                        self.service.close()
                elif self.service:
                    if action == 'stop' and self.service.snapshot()['state'] == 'unresponsive':
                        self.service.close()  # Request the owned runtime; never release an uncertain lock.
                    else:
                        submit_button(self.service, action, request_id=identifier)
                result = self.snapshot()
            self.responses[identifier] = canonical, result
            return result
        raise ValueError('unknown_panel_action')

    def close(self, timeout=5.0):
        self.closing = True
        if self.service:
            self.service.close()
            deadline = time.monotonic() + max(0., timeout)
            while True:
                state = self.service.snapshot()['state']
                if state not in ACTIVE:
                    return {'stopped': True, 'state': state}
                if time.monotonic() >= deadline:
                    # The caller may close its view; uncertain worker ownership
                    # and locks remain intact until the original runner settles.
                    return {'stopped': False, 'state': state, 'pending': True}
                time.sleep(min(.1, max(0., deadline-time.monotonic())))
        return {'stopped': True, 'state': 'idle'}


def _set_native_stdout(descriptor):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.SetStdHandle.argtypes = (wintypes.DWORD, wintypes.HANDLE)
        kernel.SetStdHandle.restype = wintypes.BOOL
        if not kernel.SetStdHandle(-11, msvcrt.get_osfhandle(descriptor)):
            raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def protocol_output():
    """Keep RPC on a duplicate pipe, including C/Win32 stdout diagnostics."""
    original = sys.stdout
    descriptor = original.fileno()
    original.flush()
    saved = os.dup(descriptor)
    protocol = None
    try:
        os.dup2(sys.stderr.fileno(), descriptor)
        _set_native_stdout(descriptor)
        sys.stdout = sys.stderr
        protocol = os.fdopen(os.dup(saved), 'w', encoding='utf-8', newline='\n', buffering=1)
        yield protocol
    finally:
        try:
            if protocol is not None:
                protocol.close()
        finally:
            os.dup2(saved, descriptor)
            _set_native_stdout(descriptor)
            os.close(saved)
            sys.stdout = original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--umaa-root', type=Path, required=True)
    args = parser.parse_args()
    with protocol_output() as protocol:
        host = PacerHost(args.umaa_root)
        try:
            for line in sys.stdin:
                identifier = None
                try:
                    request = json.loads(line)
                    identifier = request.get('request_id')
                    result = dict(ok=True, request_id=identifier, result=host.dispatch(request))
                except Exception as error:
                    result = dict(ok=False, request_id=identifier, error=str(error), error_type=type(error).__name__)
                protocol.write(json.dumps(result, ensure_ascii=False) + '\n')
                protocol.flush()
        finally:
            host.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
