"""Private standalone Pacer host; legacy packages remain cut-only."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))


def configure(path):
    path=Path(path).resolve()
    data=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    if not isinstance(data,dict):raise ValueError('连接配置必须是JSON对象。')
    port=data.get('adb_port',16384)
    if type(port) is not int or not 1<=port<=65535:raise ValueError('ADB端口请填写1～65535的整数。')
    backend=data.get('capture_backend','auto')
    # R1-R5 only supported adb, so that old default was not an explicit
    # choice against a native reader. Schema2 can still force ADB if needed.
    if data.get('schema_version',1)==1 and backend=='adb':backend='auto'
    if backend not in {'auto','adb','ldplayer','mumu'}:raise ValueError('截图模式应为auto、adb、ldplayer或mumu。')
    adb=data.get('adb_path','')
    if not isinstance(adb,str):raise ValueError('ADB路径应为文本。')
    if adb:
        value=Path(adb).expanduser()
        adb=str((path.parent/value).resolve() if not value.is_absolute() else value.resolve())
    os.environ.update(UMA_CUT_PORTABLE_ROOT=str(ROOT),UMA_CUT_CAPTURE_BACKEND=backend,
        UMA_ADB_PORT=str(port),UMA_ADB_PATH=adb,UMA_CUT_CONFIG_PATH=str(path),PYTHONDONTWRITEBYTECODE='1')
    # Legacy action/coordinate readers use config/... relative to cwd. This is
    # a dedicated child process, so anchor it once before loading the runner.
    os.chdir(ROOT)
    scratch=ROOT/'runs/runtime-temp';scratch.mkdir(parents=True,exist_ok=True)
    os.environ['TEMP']=os.environ['TMP']=str(scratch)
    from core.cut_portable_runtime import enable_unicode_image_paths
    enable_unicode_image_paths()
    return {'adb_path':adb,'adb_port':port,'capture_backend':backend}


def capture_probe(config):
    """Read-only transport timing. No game navigation, login or saved images."""
    import time
    from statistics import mean
    from core.adb import ADB
    from core.portable_capture import reset_capture_probe
    from core.portable_adb_capture import reset_adb_frame_probe
    endpoint=f"127.0.0.1:{config['adb_port']}"
    reset_capture_probe(config['adb_path'], config['adb_port'])
    reset_adb_frame_probe((str(Path(config['adb_path']).resolve()),endpoint))
    device=ADB(port=config['adb_port'])
    try:
        if not device.connect():return {'ok':False,'endpoint':endpoint,'message':'ADB连接失败，请检查模拟器ADB开关和端口。'}
        started=time.perf_counter()
        frame=device.read_frame(deadline=time.monotonic()+10.)
        first_ms=round((time.perf_counter()-started)*1000,2)
        if frame is None:return {'ok':False,'endpoint':endpoint,'message':'ADB已连接，但未取得画面，请保持模拟器启动后重试。',**device.capture_diagnostics()}
        size=(frame.shape[1],frame.shape[0])
        samples=[]
        for _ in range(3):
            started=time.perf_counter();fresh=device.read_frame(deadline=time.monotonic()+3.)
            if fresh is None:break
            samples.append(round((time.perf_counter()-started)*1000,2))
        details=device.capture_diagnostics();backend=details['backend']
        label=('雷电高速截图' if backend.startswith('ldplayer-opengl') else
               'MuMu高速截图' if backend=='mumu-ipc' else 'ADB直传截图' if backend=='adb-raw' else 'ADB兼容截图')
        average=round(mean(samples),2) if samples else None
        supported=size==(720,1280)
        message=(f'连接成功 · {label}'+(f' · 约{average:.0f}毫秒/张' if average is not None else '')) if supported else '已连接；刷切需要竖屏720×1280，请调整后再开始。'
        return {'ok':supported,'endpoint':endpoint,'size':list(size),'capture_label':label,
                'first_frame_ms':first_ms,'capture_ms':samples,'mean_capture_ms':average,
                'message':message,'read_only':True,'screenshots_saved':False,**details}
    finally:
        device.close()


def runtime_capture_info(snapshot, root):
    """Show the last completed capture, using its text report without device I/O."""
    last=(snapshot.get('record') or {}).get('last_turn') or {}
    attempt=((snapshot.get('cut') or {}).get('current_attempt_root')
        or (snapshot.get('run_root') if snapshot.get('mode')=='career' else None))
    if not attempt or not last.get('output'):return None
    attempt=Path(attempt).resolve();turn=Path(last['output']).resolve()
    if not attempt.is_relative_to((Path(root)/'runs').resolve()) or not turn.is_relative_to(attempt):return None
    try:
        report=json.loads((turn/'pipeline_report.json').read_text(encoding='utf-8-sig'))
        capture=report.get('report',{}).get('stages',{}).get('capture',{})
        details=capture.get('capture_backend_details') or {}
        backend=details.get('backend') or capture.get('capture_transport')
        if not backend:return None
        label=('雷电高速截图' if backend.startswith('ldplayer-opengl') else
            'MuMu高速截图' if backend=='mumu-ipc' else 'ADB直传截图' if backend=='adb-raw' else 'ADB兼容截图')
        fallback=bool(details.get('fallback_reasons')) and backend.startswith('adb-')
        return {'backend':backend,'label':label,'fallback':fallback,
            'turn_index':last.get('turn_index'),'basis':'last_completed_training_capture',
            'dimensions':details.get('dimensions'),
            'message':f'最近训练截图：{label}'+(' · 原生通道未启用' if fallback else '')}
    except (OSError,ValueError,AttributeError,TypeError):
        return None


def runtime_race_error(snapshot, root):
    """Recover the concrete exception omitted by older summary writers."""
    record=snapshot.get('record') or {}
    if record.get('error') or record.get('stop_reason')!='race_executor_error':return None
    attempt=((snapshot.get('cut') or {}).get('current_attempt_root')
        or (snapshot.get('run_root') if snapshot.get('mode')=='career' else None))
    if not attempt:return None
    attempt=Path(attempt).resolve()
    if not attempt.is_relative_to((Path(root)/'runs').resolve()):return None
    try:
        saved=json.loads((attempt/'session/larc_race_checkpoint.json').read_text(encoding='utf-8-sig'))
        if (saved.get('run_session_id')!=attempt.name or saved.get('device')!=snapshot.get('endpoint')
                or saved.get('stop_reason')!='race_executor_error'):return None
        last=(saved.get('steps') or [{}])[-1].get('result') or {}
        error=last.get('error')
        return error[:1000] if isinstance(error,str) and error else None
    except (OSError,ValueError,AttributeError,TypeError):
        return None


def make_host(config):
    from scripts.larc_umaa_host import PacerHost
    from scripts.larc_umaa_cut import create_cut_control_service
    from core.auto_trainer.cut_runtime_license import LicensedRunControl
    from core import cut_portable_lease
    from core.cut_panel_presets import CutPanelPresets
    from desktop.cut_package_profile import pacer_center_enabled
    center_enabled=pacer_center_enabled(ROOT)
    endpoint=f"127.0.0.1:{config['adb_port']}"
    # Installed Python code lives under <application>/backend. Public builds run
    # open-source without online runtime gating via LicensedRunControl.
    application_root=ROOT.parent.resolve()
    # One restart retry for ready reports from an already-ended career.
    # Pending handoff pauses remain local; the client never uploads them.
    try:
        from core.auto_trainer.recognition_failure_archive import installed_application_root
        from desktop.recognition_failure_feedback import flush_async
        feedback_root=installed_application_root(application_root)
        if feedback_root is not None:
            flush_async(feedback_root)
    except Exception:
        pass
    def licensed_service(*args,**kwargs):
        return create_cut_control_service(*args,**kwargs,
            control_factory=lambda **control_kwargs:LicensedRunControl(application_root,**control_kwargs))
    def licensed_career_service(*args,**kwargs):
        from scripts.larc_panel_runner import create_larc_control_service
        return create_larc_control_service(*args,**kwargs,lease=cut_portable_lease,
            control_factory=lambda **control_kwargs:LicensedRunControl(application_root,**control_kwargs))
    def require_adb():
        if not config['adb_path'] or not Path(config['adb_path']).is_file():
            raise ValueError('请先选择本机模拟器自带的adb.exe，保存连接配置。')
    class CutHost(PacerHost):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self._panel_presets=CutPanelPresets(self.root,endpoint,lambda:self.records(endpoint,'cut'))
            self._successful_batch=None
            self._successful_career=None
            self._view_mode=self.mode='cut'
            self.pacer_center=center_enabled
            self._career_presets=None
            self._realtime_luck=None
            if center_enabled:
                from core.career_panel_presets import CareerPanelPresets
                self._career_presets=CareerPanelPresets(self.root,endpoint,self._panel_presets)
                try:
                    from core.pacer_luck_session import RealtimeLuckSession
                    self._realtime_luck=RealtimeLuckSession(self.root/'runs')
                except ImportError:
                    pass  # Existing runtimes may not ship the optional display adapter.

        def _sync_workflow_career(self):
            if self.run_root and self.mode == 'career':
                from scripts.career_batch_runtime import current_run
                self.run_root = current_run(self.run_root, self.endpoint)
                if self._successful_career:
                    self._successful_career = current_run(self._successful_career, self.endpoint)

        def records(self,endpoint,mode='career'):
            if mode=='career' and center_enabled:
                from core.career_panel_presets import records
                return records(self.root,endpoint)
            return super().records(endpoint,mode)

        def checked_run(self,value,endpoint=None):
            if center_enabled:
                from core.career_panel_presets import record_for_path
                row=record_for_path(value,root=self.root,endpoint=endpoint or self.endpoint)
                if not row['can_resume']:raise ValueError(row['resume_reason'])
                return Path(row['path'])
            return super().checked_run(value,endpoint)

        def _current_record(self,mode=None):
            state=self.service.snapshot()['state'] if self.service else 'idle'
            from scripts.larc_umaa_host import ACTIVE
            mode=self.mode if state in ACTIVE else mode or self._view_mode
            if mode=='career' and self._career_presets is not None:
                active=self.run_root if state in ACTIVE else self._successful_career
                return self._career_presets.current_record(active_root=active)
            active=self.run_root if state in ACTIVE and self.mode=='cut' else self._successful_batch
            row=self._panel_presets.current_record(active_root=active)
            if center_enabled and row:
                row={**row,'can_continue_as_career':False}
                try:
                    if state in ACTIVE:raise ValueError('请等待本局停止后再继续养成。')
                    from core.career_panel_presets import handoff_candidate
                    handoff_candidate(self.root,endpoint,batch_root=row.get('path'))
                    row['can_continue_as_career']=True
                except (OSError,ValueError,TypeError,KeyError) as exc:row['career_handoff_reason']=str(exc)
            return row

        def continue_cut_as_career(self,request):
            from scripts.larc_umaa_host import ACTIVE
            if not center_enabled:raise ValueError('当前版本只提供刷切。')
            if self.closing or self.service and self.service.snapshot()['state'] in ACTIVE:
                raise ValueError('请等待原任务完成停止，不能在运行或暂停中交接。')
            if request.get('options') is not None or request.get('run_root') is not None:
                raise ValueError('继续养这局沿用当前保留局及原设置，不接受另一局或新参数。')
            busy=self.snapshot(endpoint,'cut')
            if busy['device_owner'] or busy['daily_pending']:
                raise ValueError('同设备仍被日活或其他任务占用，请停止原任务后再继续。')
            from core.career_panel_presets import handoff_candidate,save_cut_handoff
            path,origin,config,attempt=handoff_candidate(self.root,endpoint)
            options=self.settings(path)
            from scripts.larc_control_window import build_argv
            build_argv(path,options.get('max_actions',72),device_port=int(endpoint.rsplit(':',1)[1]))
            save_cut_handoff(path,endpoint,origin,config,attempt,options,root=self.root)
            result=super().start({'action':'start','mode':'career','endpoint':endpoint,
                'run_root':str(path),'request_id':request['request_id']})
            self._view_mode='career';self._successful_career=path
            result['current_record']=self._current_record('career')
            return result

        def start_cut(self,request,endpoint,options):
            if request.get('run_root') or not request.get('new_cycle'):
                return super().start_cut(request,endpoint,options)
            if request.get('new_cycle') is not True or request.get('take_over_current') is not True:
                raise ValueError('开始新一轮需要明确接手当前育成。')
            from scripts.larc_panel_setup import parse_strategy_options
            from scripts.larc_umaa_cut import cut_options,create_batch,create_cut_control_service,read_json
            from scripts.larc_panel_runner import submit_button
            strategy,_=parse_strategy_options(options.get('strategy','baseline'),25)
            requested_policy=options.get('larc_policy')
            if requested_policy is None and strategy!='baseline':requested_policy={'variant':strategy}
            strategy='baseline'
            # The explicit new-cycle request authorizes current-page takeover;
            # this flag is not a claim that the game is on its first turn.
            config,template=cut_options({**(options.get('cut') or {}),'setup_confirmed':True},
                endpoint=endpoint,strategy=strategy,root=self.root)
            previous=self._current_record('cut')
            transaction=None
            if previous and previous.get('path'):
                old=read_json(Path(previous['path'])/'cut_farming.json')
                pending=old.get('pending_action') or {}
                if pending.get('state') in {'confirm_tp_refill','continue_support','confirm_start','confirm_career_start'}:
                    transaction={'batch_root':previous['path'],'pending_action':pending,
                        'status':'unconfirmed_previous_transaction; never_replayed'}
            path=create_batch(self.root,config,template=template,
                decision_preferences=(options.get('cut') or {}).get('decision_preferences'),
                ai_options=(options.get('cut') or {}).get('ai_options'),larc_policy=requested_policy,support_profile=options.get('support_profile'),
                mid_skill_learning=options.get('mid_skill_learning'),
                early_skill_allowlist=options.get('early_skill_allowlist',[]),race_retry=options.get('race_retry'))
            if self.service:self.service.close()
            self.run_root,self.endpoint,self.mode=path,endpoint,'cut'
            port=int(endpoint.rsplit(':',1)[1])
            self.service=(self.cut_service_factory or create_cut_control_service)(
                self.root/'runs/umaa-panel-control'/str(port),{'current':{
                    'batch_root':str(path),'config':config.public_dict(),'template_root':template,
                    'resume':False,'entry_mode':'current_career','previous_transaction':transaction}},
                umaa_root=self.umaa_root,lease=self.lease)
            submit_button(self.service,'start',request_id=request['request_id'],profile_id='current')
            return self.snapshot()

        def snapshot(self, endpoint=None,mode=None,selected_run_root=None):
            self._sync_workflow_career()
            # History is display-only. A new start never receives its run_root.
            current=self._current_record(mode or self._view_mode)
            result=super().snapshot(endpoint,mode or self._view_mode,selected_run_root)
            if result.get('mode')!=self.mode:
                # A stopped service belongs to its original mode. Its old
                # counters/error must not become the other mode's live status.
                result['control']={'state':'idle','runtime':None}
            # Restarted history supplies diagnostics only, never a start/resume root.
            from scripts.larc_umaa_host import ACTIVE
            if (result.get('mode')=='career' and result.get('control',{}).get('state') not in ACTIVE
                    and isinstance(result.get('record'),dict) and not result['record']
                    and isinstance(current,dict) and current.get('path')):
                try:
                    diagnostic_root=Path(current['path']).resolve(strict=True)
                    if (diagnostic_root.parent==(self.root/'runs').resolve()
                            and diagnostic_root.name.startswith('larc_panel_') and diagnostic_root.is_dir()):
                        summaries=[path for path in diagnostic_root.glob('autonomous_*/summary.json')
                            if path.resolve().parent.parent==diagnostic_root]
                        if summaries:
                            from scripts.larc_umaa_host import read_json,entry_diagnostics
                            from scripts.pacer_stop_diagnostics import extract_stop_diagnostics
                            summary_path=max(summaries,key=lambda path:path.stat().st_mtime)
                            summary=read_json(summary_path)
                            if isinstance(summary,dict) and summary:
                                diagnostic={'diagnostic_run_id':diagnostic_root.name}
                                if isinstance(summary.get('status'),str):diagnostic['summary_status']=summary['status']
                                if summary.get('status') not in {'running','dry_run','completed','batch_limit_reached',
                                        'year_checkpoint','continuation_checkpoint','cut_farming_checkpoint','reattach'}:
                                    for key in ('stop_reason','error','error_type'):
                                        if isinstance(summary.get(key),str):diagnostic[key]=summary[key]
                                diagnostic['entry_diagnostics']=entry_diagnostics(summary)
                                diagnostic['stop_diagnostics']=extract_stop_diagnostics(summary,summary_path)
                                try:
                                    from desktop.native_stop_diagnostics import augment_stop_record
                                    augment_stop_record(diagnostic,summary,summary_path)
                                except Exception:
                                    pass  # History remains display-only if a diagnostic is unavailable.
                                result['record']=diagnostic
                except Exception:
                    pass  # Display-only report reads must not affect the existing snapshot.
            if (self._realtime_luck is not None and selected_run_root is None
                    and result.get('control',{}).get('state') not in ACTIVE
                    and isinstance(current,dict) and current.get('path')):
                try:
                    from scripts.larc_umaa_host import read_json
                    idle_mode=result.get('mode')
                    idle_root=Path(current['path']).resolve(strict=True)
                    if idle_mode=='cut':
                        from scripts.larc_umaa_cut import checked_batch,cut_snapshot
                        b_path,b_data,b_cfg=checked_batch(idle_root,root=self.root,endpoint=endpoint)
                        cand=cut_snapshot(b_path,b_data,b_cfg).get('current_attempt_root')
                        idle_root=Path(cand).resolve(strict=True) if cand else None
                    if (idle_root is not None and idle_root.is_relative_to((self.root/'runs').resolve())
                            and (result.get('realtime_luck') or {}).get('run_id')!=idle_root.name):
                        idle_rec={**read_json(idle_root/'progress.json')}
                        if isinstance(result.get('record'),dict) and result['record'].get('summary_status'):
                            idle_rec['summary_status']=result['record']['summary_status']
                        result['realtime_luck']=self._realtime_luck.snapshot(idle_root,idle_rec,idle_mode,live=False)
                except Exception:
                    pass  # Display-only luck fallback never alters snapshot control.
            result['current_record']=current
            result['can_continue_as_career']=bool(current and current.get('can_continue_as_career'))
            if current and current.get('career_handoff_reason'):result['career_handoff_reason']=current['career_handoff_reason']
            result['pacer_center']=center_enabled
            result['allowed_modes']=['career','cut'] if center_enabled else ['cut']
            result['runtime_capture']=runtime_capture_info(result,ROOT)
            if error:=runtime_race_error(result,ROOT):result['record']['error']=error
            try:
                from desktop.larc_ai_status import for_host
                result['ai_status']=for_host(self,ROOT,snapshot=result)
            except Exception:pass  # Local display failure must not affect the runner.
            try:
                from desktop.pacer_stop_context import annotate
                annotate(result,ROOT)
            except Exception:pass  # Reporting never changes task control.
            from scripts.career_workflow_host import snapshot as workflow_snapshot
            result['workflow']=workflow_snapshot(self)
            return result

        def dispatch(self,request):
            self._sync_workflow_career()
            from scripts.larc_umaa_host import ACTIVE
            action=request.get('action')
            from scripts.career_workflow_host import ACTIONS as WORKFLOW_ACTIONS,dispatch as workflow_dispatch
            if action in WORKFLOW_ACTIONS:
                if not center_enabled:raise ValueError('工作流仅用于私有育成中心。')
                if request.get('endpoint',endpoint)!=endpoint:raise ValueError('设备与已保存连接配置不同。')
                if action=='start_workflow':
                    require_adb()
                    identifier=request.get('request_id')
                    canonical=json.dumps(request,sort_keys=True,ensure_ascii=False)
                    if identifier in self.responses:
                        old,result=self.responses[identifier]
                        if old!=canonical:raise ValueError('request_id_conflict')
                        return result
                result=workflow_dispatch(self,request,endpoint=endpoint)
                if action=='start_workflow':
                    self._view_mode = self.mode
                    self._sync_workflow_career()
                    if self.mode == 'career':self._successful_career = self.run_root
                    self.responses[identifier]=canonical,result
                return result
            from desktop.larc_panel_local_actions import LOCAL_ACTIONS,handle
            if action in LOCAL_ACTIONS:
                return handle(self,request,root=ROOT,center_enabled=center_enabled)
            if action in {'runtime_stop_evidence','prepare_runtime_stop_report','submit_runtime_stop_report'}:
                from desktop.runtime_stop_reports import dispatch_for_host
                return dispatch_for_host(self,request)
            if action=='export_stop_diagnostics':
                from desktop.native_stop_diagnostics import export_for_host
                return export_for_host(self,request)
            if action=='continue_cut_as_career':
                identifier=request.get('request_id')
                if not isinstance(identifier,str) or not 8<=len(identifier)<=128:raise ValueError('request_id_required')
                canonical=json.dumps(request,sort_keys=True,ensure_ascii=False)
                if identifier in self.responses:
                    old,result=self.responses[identifier]
                    if old!=canonical:raise ValueError('request_id_conflict')
                    return result
            state=self.service.snapshot()['state'] if self.service else 'idle'
            mode=request.get('mode',self.mode if state in ACTIVE else 'cut')
            if mode not in (('career','cut') if center_enabled else ('cut',)):raise ValueError('当前安装版本只提供刷切。')
            if action in {'list','snapshot'} and state in ACTIVE:
                # A newly opened view discovers the owned task; it cannot switch it.
                mode=self.mode;request={**request,'run_root':str(self.run_root)}
            sieve_manual = False
            if (center_enabled and state == 'paused' and action == 'resume_manual_handoff'
                    and mode == 'career' and self.mode == 'cut' and request.get('run_root')
                    and request.get('endpoint') == endpoint == self.endpoint):
                from scripts.career_sieve_cycle import current_sieve_attempt
                current = current_sieve_attempt(request['run_root'], root=self.root, endpoint=endpoint)
                if current is not None:
                    attempt, batch, _ = current
                    sieve_manual = batch == self.run_root and Path(request['run_root']).resolve() == attempt
            if state in ACTIVE and not sieve_manual and (mode!=self.mode or request.get('run_root') not in (None,str(self.run_root))):
                raise ValueError('当前任务运行或暂停中，请先结束本轮；不能更换模式或记录。')
            if request.get('endpoint',endpoint)!=endpoint:raise ValueError('设备与已保存连接配置不同。')
            request={**request,'mode':mode,'endpoint':endpoint}
            self._view_mode='cut' if sieve_manual else mode
            if action=='continue_cut_as_career':
                if mode!='cut':raise ValueError('请从刷切已保留局继续养成。')
                require_adb()
                result=self.continue_cut_as_career(request)
                self.responses[identifier]=canonical,result
                return result
            if action in {'pushplus_settings','save_pushplus_token','test_pushplus_notification'}:
                from desktop import pushplus_settings
                if action=='pushplus_settings':return pushplus_settings.status(ROOT)
                if action=='save_pushplus_token':return pushplus_settings.save(ROOT,request.get('token'))
                return pushplus_settings.test_notification(ROOT)
            if request.get('action')=='validate_event_preferences':
                from core.auto_trainer.larc_event_library import normalize_preferences
                return {'preferences':normalize_preferences(request.get('preferences',{}))}
            if request.get('action')=='search_events':
                from core.auto_trainer.larc_event_catalog import search_events
                return search_events(request.get('query',''),keys=request.get('keys'),limit=request.get('limit',40))
            if request.get('action')=='list_reviews':
                from core.auto_trainer.career_review_export import list_career_reviews
                return {'records':list_career_reviews(ROOT,mode=mode)}
            if request.get('action')=='export_review':
                from core.auto_trainer.career_review_export import export_career_review
                return export_career_review(ROOT,request.get('run_id'))
            if request.get('action')=='compare_review_preferences':
                from core.auto_trainer.larc_preferences_review import export_comparison
                preferences=request.get('preferences')
                if 'profile' in request:
                    from desktop.scoring_exchange import checked_profile
                    if 'preferences' in request:raise ValueError('请提供完整评分方案或本机设置，不要同时提供两份。')
                    preferences=checked_profile(request['profile'])['preferences']
                return export_comparison(ROOT,request.get('run_id'),preferences)
            if request.get('action')=='probe_device':
                require_adb()
                return capture_probe(config)
            if action in {'resume_manual_handoff','end_manual_handoff_task','update_manual_handoff_plan'} and (not center_enabled or mode!='career'):
                raise ValueError('人工交接及暂停计划仅用于育成中心的普通育成。')
            # Editing a pause plan is record-only, including while input is in
            # flight. Only an actual start/continue needs the existing ADB gate.
            if request.get('action') in {'start','resume','resume_manual_handoff'}:require_adb()
            if request.get('action') not in {'list','snapshot','start','resume','resume_manual_handoff','end_manual_handoff_task','update_manual_handoff_plan','pause','stop','close','continuation_schema','decision_preferences_schema','validate_decision_preferences','preview_decision_preferences'}:
                raise ValueError('独立刷切不支持此操作。')
            replayed=request.get('request_id') in self.responses
            result=super().dispatch(request)
            if action == 'resume_manual_handoff' and result.get('mode') == 'cut':
                mode = self._view_mode = 'cut'
            if action in {'start','resume','resume_manual_handoff'} and not replayed and result.get('run_root'):
                if mode=='career':self._successful_career=Path(result['run_root']).resolve()
                else:self._successful_batch=Path(result['run_root']).resolve()
            result['current_record']=self._current_record(mode)
            result['pacer_center']=center_enabled
            if action=='list':
                if mode=='career':result['career_presets']=self._career_presets.payload(active_root=self._successful_career)
                else:result['cut_presets']=self._panel_presets.payload(active_root=self._successful_batch)
            return result
    return CutHost(ROOT.parent.parent/"backend",root=ROOT,lease=cut_portable_lease,cut_service_factory=licensed_service,
        service_factory=licensed_career_service,immutable_career_settings=center_enabled)


def self_test():
    """CPU/import/data checks only. No ADB process or game input."""
    import cv2
    import numpy as np
    from core.auto_trainer.frame_safety import LocalRapidOCRScreenTextReader
    from core.auto_trainer.cut_farming import CutFarmingConfig,load_event_references
    from core.native_capture_adb import native_instance_id
    from core.training_parser import TrainingParser
    from scripts.larc_umaa_host import ENDPOINTS
    from core.main_menu_state import _load_assets
    from core.event_knowledge_lookup import load_reviewed_events
    from core.support_card_knowledge import default_library
    from core.lightweight_gain_reader import LightweightGainReader
    from core.coordinates import CoordinateConfig
    from core.auto_trainer.action_table import ActionTable
    from core.auto_trainer.screen_graph import ScreenGraph
    coordinates=CoordinateConfig()
    assert 'main_menu' in coordinates.data and ActionTable().data and ScreenGraph.load().nodes
    from core.auto_trainer.career_goal_perception import match_main_menu_named_goal
    catalog_goal=match_main_menu_named_goal('广播协会英里杯中进入前5')
    assert catalog_goal and catalog_goal['target_race_id']=='race.1007.classic.05.first'
    assert match_main_menu_named_goal('参加初级新人战') is None
    verified=0
    for section,items in coordinates.data.items():
        if not isinstance(items,dict):continue
        for name,row in items.items():
            if isinstance(row,dict) and row.get('verification',{}).get('status')=='verified':
                coordinates.require_verified(section,name);verified+=1
    assert native_instance_id(int(os.environ['UMA_ADB_PORT'])) is None
    assert ENDPOINTS=={f"127.0.0.1:{os.environ['UMA_ADB_PORT']}"}
    for char in ('101001','103102','106501'):
        load_event_references(CutFarmingConfig(char,device_port=int(os.environ['UMA_ADB_PORT'])))
    _load_assets();TrainingParser(use_mimo=False,recognition_mode='lightweight')
    LightweightGainReader();default_library();events=load_reviewed_events()
    import struct
    from core.adb_raw_capture import parse_adb_raw_screencap
    raw_frame,raw_info=parse_adb_raw_screencap(struct.pack('<IIII',1,1,1,1)+bytes((12,24,36,255)),header_bytes=16)
    assert raw_frame[0,0].tolist()==[36,24,12] and raw_info['header_bytes']==16
    from core.auto_trainer.larc_event_library import normalize_preferences
    assert normalize_preferences({})['mei_unlock_policy']=={'mode':'safe','below_vitality_percent':50.0}
    assert normalize_preferences({'mei_unlock_policy':{'mode':'gamble_when_low','below_vitality_percent':45}})['mei_unlock_policy']['below_vitality_percent']==45
    image=np.full((120,360,3),255,np.uint8)
    cv2.putText(image,'123456',(15,85),cv2.FONT_HERSHEY_SIMPLEX,2,(0,0,0),3)
    text=LocalRapidOCRScreenTextReader().read_regions(image)
    if not text:raise RuntimeError('OCR自检没有读到合成数字。')
    from core.auto_trainer.larc_policy import BALANCED_BASELINE,BALANCED_EXPERIMENTAL
    packaged=(ROOT.parent/'runtime/python.exe').is_file()
    if packaged:
        import onnxruntime
        from core.auto_trainer.cut_support_selection import support_api,support_options
        assert support_options(CutFarmingConfig('103102',device_port=int(os.environ['UMA_ADB_PORT']),support_deck=1))=={'support_deck':1}
        borrowed=support_options(CutFarmingConfig('103102',device_port=int(os.environ['UMA_ADB_PORT']),
            support_deck=1,borrow_card_id=30160,borrow_min_level=50,borrow_min_break=4))
        assert borrowed['borrow_card_id']==30160 and borrowed['borrow_min_level']==50
        assert len(support_api().matcher().templates)>0
        from core.auto_trainer.larc_event_library import resolve_library_event,score_library_event
        from core.auto_trainer.state import TrainingTurnState
        from core.auto_trainer.config import UGParentGoal
        state=TrainingTurnState.from_dict({'screen':'event_choice','turn':5,'vitality':20,
            'current_stats':dict(speed=150,stamina=150,power=150,guts=150,wisdom=150,skill_pt=100),
            'scenario_state':{'larc_period':{'status':'verified','value':{'year_index':1,'pre_debut':True}}}})
        from core.auto_trainer.larc_outing_session import _mei_unlock_event
        from core.auto_trainer.larc_mei_unlock_policy import select_mei_unlock
        gamble=select_mei_unlock(_mei_unlock_event(),('有时也要忘记工作',),state,
            {'cut_farming_active':True,'next_action_turn':5},
            {'mei_unlock_policy':{'mode':'gamble_when_low','below_vitality_percent':50}})
        assert gamble['choice_index']==2 and gamble['mei_unlock_decision']['gamble_selected']
        context={'trainee_variant_id':'103102','max_vitality':100,'event_preferences':{
            'target_skills':['斗志高昂'],'ignored_skills':['777','准备突围'],'conserve_energy':True}}
        for title,index in [('真的......没办法吗',2),('应该传达的话语是——',2),('尽管交给姐姐吧！',2),('Oh!今晚是派对☆',2)]:
            matched=resolve_library_event(title,2,context)
            selected=score_library_event(matched,state,UGParentGoal.default(),context,2)
            assert selected['status']=='selected' and selected['choice_index']==index
        from core.auto_trainer.larc_event_catalog import search_events,manual_event_choice
        found=search_events('根城 第二段')
        assert found['total']==1
        key=found['events'][0]['key']
        override=manual_event_choice('真的......没办法吗',2,context,{'manual_choices':{key:1}})
        assert override['choice_index']==1 and override['reason']=='user_event_choice'
        from dataclasses import replace
        from core.auto_trainer.larc_charge_unlock import charge_unlock_window
        opening=replace(state,scenario_state={**state.scenario_state,'scenario':'larc','larc_remaining_turns':10})
        assert charge_unlock_window(opening,replace(UGParentGoal.default(),scenario='larc'))['actions_until_unlock']==1
        extra=resolve_library_event('额外的自主训练',2,context)
        assert extra is not None
        selected=score_library_event(extra,state,UGParentGoal.default(),context,2)
        assert selected['choice_index']==2 and selected['event_policy_version']=='larc_events_v5'
        for module in (cv2,np,onnxruntime):
            assert Path(module.__file__).resolve().is_relative_to(ROOT.parent)
        assert Path(sys.executable).resolve().is_relative_to(ROOT.parent)
    return {'ok':True,'python':sys.version.split()[0],'opencv':cv2.__version__,
        'alpha':BALANCED_BASELINE,'beta':BALANCED_EXPERIMENTAL,'event_presets':3,'reviewed_events':len(events),
        'ocr':'ready','packaged_imports':packaged,'verified_coordinates':verified,
        'support_selection':'ready' if packaged else 'not_packaged',
        'event_library_decisions':'ready' if packaged else 'not_packaged','career_goal_catalog':'ready',
        'raw_frame_decoder':'ready','mei_unlock_policy':'ready','device_actions':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    check=parser.add_mutually_exclusive_group()
    check.add_argument('--self-test',action='store_true')
    check.add_argument('--capture-diagnostics',action='store_true')
    args=parser.parse_args();config=configure(args.config)
    from scripts.larc_umaa_host import protocol_output
    with protocol_output() as protocol:
        if args.self_test:
            protocol.write(json.dumps(self_test(),ensure_ascii=False)+'\n');return 0
        if args.capture_diagnostics:
            result=capture_probe(config)
            target=ROOT/'runs/capture-diagnostics.json'
            target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            protocol.write(json.dumps({**result,'report':str(target)},ensure_ascii=False)+'\n')
            return 0 if result['ok'] else 2
        host=make_host(config)
        try:
            for line in sys.stdin:
                identifier=None
                try:
                    request=json.loads(line);identifier=request.get('request_id')
                    result={'ok':True,'request_id':identifier,'result':host.dispatch(request)}
                except Exception as error:
                    result={'ok':False,'request_id':identifier,'error':str(error),'error_type':type(error).__name__}
                protocol.write(json.dumps(result,ensure_ascii=False)+'\n');protocol.flush()
        finally:host.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
