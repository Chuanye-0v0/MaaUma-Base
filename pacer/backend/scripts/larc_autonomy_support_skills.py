"""Bounded mid-career support-skill bridge; no training-turn consumption."""
from pathlib import Path
import hashlib,json,os,sys,time
import cv2
from core.auto_trainer.config import load_goal_config
from core.auto_trainer.larc_support_skill_startup import (startup_target,due_reasons,held_speed_skills,
    execution_options,normalize_mid_skill_learning,learning_completion,waiting_status,learning_mode)
from core.auto_trainer.larc_outing_route import menu_identity
from core.auto_trainer.route_transition_session import RouteTransitionSession
from core.auto_trainer.frame_safety import FrameSafetyAnalyzer,FormalActionSafetyGate
from core.auto_trainer.main_menu_state_provider import provide_main_menu_state,_CALENDAR_REGION_READER
from core.auto_trainer.safe_advance import SafeAdvancePlanner
from core.skill_acquisition_parser import resolve_tesseract_runtime,inspect_skill_acquisition_page
from core.native_capture_adb import adb_for_capture
from core.execution_frames import ExecutionFrames
from core.auto_trainer.controlled_process import run_controlled_process
from scripts.larc_d1_prerace_skill_worker import skill_ocr_environment
from scripts.larc_skill_completion_frames import success_close_box
from scripts.ug_parent_one_turn_pipeline import _atomic_write_json

def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _notice_once(ctx,status,**fields):
    """Static misconfiguration is a small one-time notice, not per-turn logs."""
    seen=getattr(ctx,'_support_skill_notices',None)
    if seen is None:
        seen=set();ctx._support_skill_notices=seen
    if status in seen:return None
    seen.add(status)
    return {'visited':False,'turn_consumed':False,'status':status,**fields}


def _learning_status(ctx,status,reason,message,**fields):
    value={"schema_version":"uma.mid_skill_learning_status.v1","run_session_id":ctx.run_id,
        "turn_index":ctx.progress["next_turn"],"status":status,"reason":reason,"message":message,**fields}
    path=ctx.root/"session/mid_skill_learning_status.json"
    try:
        if _read_learning_status(ctx)!=value:_atomic_write_json(path,value)
    except (OSError,ValueError,TypeError):pass # Display telemetry never blocks a career.
    return value


def _read_learning_status(ctx):
    try:
        value=read(ctx.root/"session/mid_skill_learning_status.json")
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError,TypeError):return {}


def _consume_trigger_dates(ledger):
    checked=set(ledger.get('checked_dates',[]))
    checked.update(reason.removeprefix('date:') for reason in ledger.get('trigger_reasons',[])
        if reason.startswith('date:'))
    ledger['checked_dates']=sorted(checked)

class StartupNavigation:
    def __init__(self,ctx,*,adb=None,safety=None,gate=None,provider=None,sleep=time.sleep):
        self.ctx=ctx;self.frames=ExecutionFrames(adb or adb_for_capture(ctx.port));self.safety=safety or FrameSafetyAnalyzer()
        self.gate=gate or FormalActionSafetyGate();self.provider=provider or provide_main_menu_state;self.sleep=sleep
        self.route=RouteTransitionSession.load(ctx.route,expected_run_session_id=ctx.run_id)
        self.sequence=0
    def observe(self,path,screen,*,resync=False):
        sha=hashlib.sha256(Path(path).read_bytes()).hexdigest()
        self.route.observe(screen,source_sha256=sha,resync=resync);self.route.save(self.ctx.route)
        return sha
    def tap(self,path,point,screen,action,next_states):
        if not self.frames.connect():raise ValueError('startup_device_connect_failed')
        sha=self.observe(path,screen);decision={'action':'tap','reason':action,'route_state':screen,
            'tap':{'coordinate_name':action,'x':point[0],'y':point[1],'scaled':False}}
        if screen=='skill_acquisition' and action in {'startup.skills.back','arc.skills.back'}:
            image=cv2.imread(str(path))
            if image is not None and inspect_skill_acquisition_page(image).get('accepted'):
                back=SafeAdvancePlanner._skill_acquisition_back_button(image) or SafeAdvancePlanner._skill_acquisition_back_control(image)
                if back is not None and tuple(point)==tuple(back):
                    decision['evidence']={'skill_page_return':{
                        'source_image_sha256':sha,'skill_page_accepted':True,
                        'confirm_modal_visible':SafeAdvancePlanner._looks_like_confirm_modal_panel(image),
                        'back_point':list(back)}}
        packet=self.safety.analyze(path);verdict=self.gate.evaluate(packet,decision)
        if not verdict.allowed or packet.source_image_sha256!=sha:raise ValueError('startup_input_frame_unverified')
        if self.frames.tap(*point,scaled=False) is not True:raise ValueError('startup_input_unconfirmed')
        self.route.commit_action(action,expected_next_states=next_states,source_sha256=sha);self.route.save(self.ctx.route)
        self.sleep(.25)
    def capture(self,folder):
        if not self.frames.connect():raise ValueError('startup_device_connect_failed')
        path=Path(folder)/f'frame_{self.sequence:02d}.png';self.sequence+=1
        image=self.frames.screenshot(str(path))
        if image is None:raise ValueError('startup_capture_failed')
        return path,image
    def open(self,source,folder):
        image=cv2.imread(str(source))
        if image is None or image.shape[:2] not in {(1280,720),(2560,1440)}:return False
        small=cv2.resize(image,(720,1280));label,confidence=_CALENDAR_REGION_READER.read_line(small[993:1040,560:650])
        if label!='技能' or confidence<.95:return False
        scale=image.shape[1]/720
        self.tap(source,(round(605*scale),round(1000*scale)),'main_menu','startup.skills.open',('skill_acquisition','loading'))
        for _ in range(8):
            path,image=self.capture(folder)
            if inspect_skill_acquisition_page(image).get('accepted'):
                sha=self.observe(path,'skill_acquisition')
                # The isolated owner supplies all individual purchase receipts.
                self.route.commit_action('startup.skills.worker_handoff',expected_next_states=('main_menu','skill_acquisition','loading'),source_sha256=sha)
                self.route.save(self.ctx.route);return True
            self.sleep(.2)
        raise ValueError('startup_skill_entry_not_settled')
    def return_menu(self,folder,*,worker_returned=False):
        if not worker_returned:
            path,image=self.capture(folder)
            if not inspect_skill_acquisition_page(image).get('accepted'):raise ValueError('startup_return_not_skill_page')
            point=SafeAdvancePlanner._skill_acquisition_back_button(image) or SafeAdvancePlanner._skill_acquisition_back_control(image)
            if point is None:raise ValueError('startup_back_control_unknown')
            self.tap(path,point,'skill_acquisition','startup.skills.back',('main_menu','loading'))
        for _ in range(8):
            path,image=self.capture(folder)
            provider=self.provider(path,path.with_suffix('.state.json'),turn_index=self.ctx.progress['next_turn'],scenario='larc')
            if provider.get('valid_for_training') is True:
                self.observe(path,'main_menu');return str(path),provider
            self.sleep(.2)
        raise ValueError('startup_return_main_menu_unverified')

    def finish_confirmed_return(self,folder):
        """After a proven learning confirmation, only close success or go back."""
        from scripts.larc_transition_probe import actionable_surface
        closed=False
        backed=False
        for _ in range(40):
            path,image=self.capture(folder)
            box=success_close_box(image)
            if box is not None:
                if not closed:
                    self.observe(path,'modal',resync=True)
                    self.tap(path,((box[0]+box[2])//2,(box[1]+box[3])//2),
                        'modal','startup.skills.close_success',('skill_acquisition','main_menu','loading','modal'))
                    closed=True
                self.sleep(.25)
                continue
            if inspect_skill_acquisition_page(image).get('accepted'):
                packet=self.safety.analyze(path)
                if '连接中' in str(packet.visible_text):
                    self.sleep(.25)
                    continue
                if not backed:
                    self.observe(path,'skill_acquisition',resync=True)
                    point=SafeAdvancePlanner._skill_acquisition_back_button(image) or SafeAdvancePlanner._skill_acquisition_back_control(image)
                    if point is None:raise ValueError('startup_back_control_unknown')
                    self.tap(path,point,'skill_acquisition','startup.skills.back',('main_menu','loading'))
                    backed=True
                self.sleep(.25)
                continue
            if backed or actionable_surface(image,skip_enabled=True)=='main_menu':
                provider=self.provider(path,path.with_suffix('.state.json'),turn_index=self.ctx.progress['next_turn'],scenario='larc')
                if provider.get('valid_for_training') is True:
                    self.observe(path,'main_menu',resync=True)
                    return str(path),provider
            self.sleep(.25)
        raise ValueError('startup_confirmed_return_not_settled')


def confirmed_startup_learning(ctx,worker_root):
    """Reuse the existing final-confirmation receipt; never infer a purchase."""
    try:
        worker_root=Path(worker_root).resolve()
        if not worker_root.is_relative_to(ctx.root.resolve()):return None
        report=read(worker_root/'worker_report.json')
        if (report.get('run_session_id')!=ctx.run_id or report.get('turn_index')!=ctx.progress['next_turn']
                or report.get('purpose')!='support_startup' or report.get('endpoint')!=ctx.progress['device']):return None
        final=read(worker_root/'confirmation/final_confirmation_report.json')
        child_path=Path(final.get('promotion_plan_path','')).resolve()
        if not child_path.is_relative_to(worker_root/'selection') or not child_path.is_file():return None
        child=read(child_path)
        points=report.get('remaining_skill_points')
        ids=[row['skill_id'] for row in report.get('selected_skills',[]) if row.get('skill_id')]
        child_ids=[row.get('item_id') for row in child.get('purchases',[])]
        if (final.get('final_confirmation_performed') is not True
                or final.get('stop_reason') not in {'final_confirmation_probe_complete','batch_final_confirmation_probe_complete'}
                or final.get('actions')!=['skill_purchase.final_confirm']
                or type(final.get('device_action_count')) is not int or final['device_action_count']!=1
                or final.get('promotion_plan_sha256')!=hashlib.sha256(child_path.read_bytes()).hexdigest()
                or type(points) is not int or points<0 or child.get('expected_remaining_skill_points')!=points
                or not ids or sorted(ids)!=sorted(child_ids)):
            return None
        before=report.get('startup',{}).get('held',{})
        catalog=read(report['context']['planner_catalog'])
        held=held_speed_skills(before.get('owned_skill_ids',[])+ids,catalog,
            unknown_owned=max(0,before.get('unknown_count',0)-len(before.get('unknown_skill_ids',[]))-(0 if before.get('native_observation_covered') else 1)),
            native_known=before.get('native_observation_covered') is True)
        return {'worker_root':str(worker_root),'remaining_skill_points':points,'ownership':held,
            'learned_skill_ids':ids,'final_confirmation_report_path':str(worker_root/'confirmation/final_confirmation_report.json')}
    except (OSError,ValueError,KeyError,TypeError):
        return None


def _record_support_purchase_history(ledger, *, turn_index, before_pt, after_pt, action_ids, worker_report_path):
    if type(before_pt) is not int or type(after_pt) is not int or not 0 <= after_pt < before_pt:
        return
    history = ledger.setdefault('purchase_history', [])
    wpath_str = str(worker_report_path)
    try:
        wkey = str(Path(wpath_str).resolve())
    except OSError:
        wkey = wpath_str
    for row in history:
        if isinstance(row, dict) and row.get('worker_report_path'):
            try:
                existing_key = str(Path(row['worker_report_path']).resolve())
            except OSError:
                existing_key = str(row['worker_report_path'])
            if existing_key == wkey:
                return
    history.append({
        'turn_index': turn_index,
        'before_pt': before_pt,
        'after_pt': after_pt,
        'spent_pt': before_pt - after_pt,
        'selected_action_ids': list(action_ids or []),
        'worker_report_path': wpath_str,
    })


def finish_startup_learning(ctx,ledger,proof,output,*,navigation_factory=StartupNavigation):
    navigation=navigation_factory(ctx)
    source,provider=navigation.finish_confirmed_return(output)
    state=provider['state_context']
    diagnostics={}
    if state.get('current_stats',{}).get('skill_pt')!=proof['remaining_skill_points']:
        diagnostics['skill_pt_reading_discrepancy']={'menu':state.get('current_stats',{}).get('skill_pt'),
            'verified_worker':proof['remaining_skill_points']}
    if (ledger.get('before_remaining_turns') is not None and state.get('remaining_turns')!=ledger['before_remaining_turns']
            or ledger.get('before_period') is not None and request_period(state)!=ledger['before_period']):
        diagnostics['calendar_reading_discrepancy']={'before_remaining_turns':ledger.get('before_remaining_turns'),
            'after_remaining_turns':state.get('remaining_turns'),'before_period':ledger.get('before_period'),
            'after_period':request_period(state)}
    held=proof['ownership'];ledger.update(held)
    ledger.update(learning_completion(ledger.get("target",{}),held))
    ledger.update(last_checked_pt=proof['remaining_skill_points'],last_checked_turn=ctx.progress['next_turn'])
    ledger['status']='complete' if ledger['completed'] else 'deferred'
    _consume_trigger_dates(ledger)
    ledger['completion_recovery']={**proof,'returned_main_menu_path':source,'turn_consumed':False}
    try:
        wr_path = Path(proof['worker_root']) / 'worker_report.json'
        wr = read(wr_path) if wr_path.is_file() else {}
        _record_support_purchase_history(
            ledger,
            turn_index=ctx.progress['next_turn'],
            before_pt=(wr.get('before') or {}).get('current_skill_points'),
            after_pt=proof.get('remaining_skill_points'),
            action_ids=wr.get('selected_action_ids') or proof.get('learned_skill_ids'),
            worker_report_path=wr_path,
        )
    except (OSError, ValueError, TypeError, KeyError):
        pass
    ledger.pop('pending_worker_root',None)
    _learning_status(ctx,"learned","purchase_recovered","已完成上次技能学习收尾",mode=ledger.get("target",{}).get("mode","legacy_speed_startup"),selected_count=len(proof["learned_skill_ids"]))
    _atomic_write_json(ctx.root/'session/larc_support_skill_startup.json',ledger)
    ctx.menu_observation={'screenshot_path':source,'result':{'performed':False,'reason':'ready_main_menu',
        'frame_safety':{'source_image_sha256':hashlib.sha256(Path(source).read_bytes()).hexdigest()},
        'decision':{'evidence':{'screen_text':{'normalized_text':state.get('target_text','')}}}}}
    report={'status':ledger['status'],'turn_consumed':False,'confirmed_learning_recovery':proof,
        'after_skill_pt':proof['remaining_skill_points'],'points_basis':'confirmed_learning_receipt',**diagnostics}
    _atomic_write_json(Path(output)/'recovery_report.json',report)
    return report,source,provider


def resume_support_skill_completion(ctx,output,*,navigation_factory=StartupNavigation):
    path=ctx.root/'session/larc_support_skill_startup.json'
    if not path.is_file():return None
    ledger=read(path)
    if ledger.get('status')!='pending_purchase':return None
    worker_root=ledger.get('pending_worker_root')
    proof=confirmed_startup_learning(ctx,worker_root) if worker_root else None
    if proof is None:raise ValueError('support_skill_startup_pending_purchase')
    return finish_startup_learning(ctx,ledger,proof,output,navigation_factory=navigation_factory)

def launch_worker(ctx,output,request):
    catalogs=Path(getattr(ctx,'startup_skill_catalog_dir',None) or Path(ctx.d1_skill_root)/'data/skills/cn-20260916')
    runtime=resolve_tesseract_runtime();env=skill_ocr_environment(os.environ,executable=runtime.executable)
    env['PYTHONDONTWRITEBYTECODE']='1'
    out_path = Path(output)
    argv=[sys.executable,'-B',str(Path(__file__).with_name('larc_d1_prerace_skill_worker.py')),
        '--purpose','support_startup','--year',str(request['year']), '--startup-required-count',str(request['required']),
        '--max-purchase-count',str(request.get("max_purchase_count",5)),
        '--startup-owned-json',str(request['ledger_path']), '--d1-root',str(ctx.d1_skill_root),'--run-root',str(ctx.root),
        '--output-dir',str(output),'--resolver-catalog',str(catalogs/'cn_skill_resolver_catalog.json'),
        '--planner-catalog',str(catalogs/'cn_skill_planner_catalog.json'),'--external-skill-db-path',str(ctx.external_skill_db_path),
        '--endpoint',ctx.progress['device'],'--execute']
    probe = lambda: (out_path / 'worker_report.json').is_file() or (out_path.is_dir() and any(out_path.iterdir()))
    result=run_controlled_process(
        argv,
        cwd=str(ctx.root),
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        env=env,
        startup_timeout=35.0,
        startup_probe=probe,
        total_timeout=240.0,
        diagnostics_dir=out_path.parent,
    )
    stalled = bool(getattr(result, 'startup_stalled', False))
    diag_path = getattr(result, 'diagnostics_path', None)
    _atomic_write_json(out_path.parent/'worker_process.json',{
        'returncode':result.returncode,
        'stdout':result.stdout,
        'stderr':result.stderr,
        'startup_stalled':stalled,
        'timed_out':bool(getattr(result,'timed_out',False)),
        'diagnostics_path':diag_path,
    })
    report=out_path/'worker_report.json'
    if report.is_file():
        return read(report)
    zero_output = (not out_path.exists()) or (not any(out_path.iterdir()))
    return {
        'ok':False,
        'purchase_may_have_occurred':not zero_output,
        'zero_output_startup_failure':zero_output,
        'reason':'worker_startup_stalled' if stalled else 'worker_report_missing',
        'returncode':result.returncode,
        'startup_stalled':stalled,
        'diagnostics_path':diag_path,
    }

def prepare_support_skills(ctx,source,provider,output_dir,*,cut_mode=False,navigation_factory=StartupNavigation,worker=launch_worker):
    goal=load_goal_config(ctx.root/'goal.json')
    configured=goal.skill_timing.mid_skill_learning
    # Old cut templates must not opt in by carrying a legacy card fact/list.
    if cut_mode and configured is None:
        _learning_status(ctx,"disabled","legacy_cut_disabled","旧刷切批次未启用中途学习",mode=learning_mode(goal.skill_timing))
        return None,source,provider
    profile=getattr(ctx,'support_profile',None)
    if (ctx.root/'session/support_card_profile.json').is_file():
        from core.auto_trainer.run_support_profile import load_profile
        try:profile=load_profile(ctx.root,ctx.run_id)
        except (OSError,ValueError):profile=None
    target=startup_target(profile,ctx.run_id,goal.skill_timing)
    if not target['enabled']:
        _learning_status(ctx,**waiting_status({},target,run_id=ctx.run_id,turn=ctx.progress["next_turn"],points=None,timing=goal.skill_timing))
        if (configured is not None and normalize_mid_skill_learning(configured)['enabled']
                and target.get('reason')=='early_skill_allowlist_empty'):
            return _notice_once(ctx,'early_skill_allowlist_empty',
                message='中途学习已启用，请先选择允许提前学习的技能。'),source,provider
        return None,source,provider
    ledger_path=ctx.root/'session/larc_support_skill_startup.json'
    ledger=read(ledger_path) if ledger_path.is_file() else {'schema_version':'larc_support_skill_startup.v1','run_session_id':ctx.run_id,'owned_skill_ids':[]}
    state=provider['state_context'];points=state.get('current_stats',{}).get('skill_pt');turn=ctx.progress['next_turn']
    if ledger.get('status')=='pending_purchase':raise ValueError('support_skill_startup_pending_purchase')
    revision=state.get('scenario_state',{}).get('skill_hint_revision')
    period=provider.get('larc_period',{}).get('value') or request_period(state)
    reasons=due_reasons(ledger,target,run_id=ctx.run_id,turn=turn,points=points,
        timing=goal.skill_timing,information_revision=revision,period=period)
    if not reasons:
        keep=_read_learning_status(ctx)
        if not (ledger.get("last_checked_turn")==turn and keep.get("turn_index")==turn and keep.get("status") in {"learned","skipped"}):
            _learning_status(ctx,**waiting_status(ledger,target,run_id=ctx.run_id,turn=turn,points=points,timing=goal.skill_timing,period=period))
        return None,source,provider
    options=execution_options(goal.skill_timing,period=period)
    if options['max_cost']==0 or options['spend_fraction']==0:
        _learning_status(ctx,"skipped","no_purchase_budget","中途技能预算为0，暂不购买",mode=learning_mode(goal.skill_timing))
        return _notice_once(ctx,'no_purchase_budget'),source,provider
    if not getattr(ctx,'d1_skill_root',None) or not getattr(ctx,'external_skill_db_path',None):
        _learning_status(ctx,"skipped","driver_not_configured","中途技能执行器尚未配置",mode=learning_mode(goal.skill_timing))
        return _notice_once(ctx,'driver_not_configured',target=target),source,provider
    output=Path(output_dir);output.mkdir(parents=True,exist_ok=True)
    ledger.update(last_checked_turn=turn,last_checked_pt=points,information_revision=revision,status='checking',target=target,
        options=options,early_skill_allowlist=goal.skill_timing.early_skill_allowlist,trigger_reasons=reasons)
    _atomic_write_json(ledger_path,ledger)
    _learning_status(ctx,"checking","checking","正在检查允许中途学习的技能",mode=learning_mode(goal.skill_timing),trigger_reasons=reasons)
    navigation=navigation_factory(ctx)
    report={'visited':False,'turn_consumed':False,'target':target,'before_skill_pt':points,'trigger_reasons':reasons}
    if not navigation.open(source,output):
        ledger['status']='entry_unverified';_atomic_write_json(ledger_path,ledger)
        _learning_status(ctx,"skipped","entry_unverified","技能入口未确认，本次未学习",mode=learning_mode(goal.skill_timing))
        return {**report,'status':'entry_unverified'},source,provider
    report['visited']=True
    request={'required':target['required'],'year':provider['larc_period']['value']['year_index'],'ledger_path':ledger_path,
        'max_purchase_count':min(48,len(goal.skill_timing.early_skill_allowlist)) if learning_mode(goal.skill_timing)=="allowlist_v1" else 5}
    result=worker(ctx,output/'worker',request);report['worker']=result
    if result.get('purchase_may_have_occurred') and (result.get('ok') is not True or result.get('stage')!='complete'):
        ledger.update(status='pending_purchase',pending_worker_root=str(output/'worker'),
            before_remaining_turns=state.get('remaining_turns'),before_period=request_period(state))
        _atomic_write_json(ledger_path,ledger)
        _learning_status(ctx,"pending","pending_purchase","等待已提交技能学习的确认回执",mode=learning_mode(goal.skill_timing))
        proof=confirmed_startup_learning(ctx,output/'worker')
        if proof is not None:
            return finish_startup_learning(ctx,ledger,proof,output/'confirmed_return',navigation_factory=navigation_factory)
        raise ValueError('support_skill_learning_pending')
    if result.get('ok') is True and result.get('stage')=='complete':
        # Merely opening the page is not a completed inspection. Failed
        # no-purchase scans can retry on this date after the current action.
        _consume_trigger_dates(ledger)
        _atomic_write_json(ledger_path,ledger)
    source,provider=navigation.return_menu(output,worker_returned=result.get('returned') is True)
    fresh=provider['state_context'];after_points=fresh.get('current_stats',{}).get('skill_pt')
    if fresh.get('remaining_turns')!=state.get('remaining_turns') or request_period(fresh)!=request_period(state):
        report['calendar_reading_discrepancy']={'before_remaining_turns':state.get('remaining_turns'),
            'after_remaining_turns':fresh.get('remaining_turns'),'before_period':request_period(state),
            'after_period':request_period(fresh)}
    if result.get('ok') is True and after_points!=result.get('remaining_skill_points'):
        report['skill_pt_reading_discrepancy']={'menu':after_points,'verified_worker':result.get('remaining_skill_points')}
    diagnostic=result.get('startup',{});held=diagnostic.get('after') if result.get('learning_result',{}).get('ok') is True else diagnostic.get('held')
    if isinstance(held,dict):
        ledger.update(held);ledger.update(learning_completion(target,held))
    if result.get('learning_result',{}).get('ok') is True:
        _record_support_purchase_history(
            ledger,
            turn_index=turn,
            before_pt=(result.get('before') or {}).get('current_skill_points', points),
            after_pt=result.get('remaining_skill_points') if type(result.get('remaining_skill_points')) is int else after_points,
            action_ids=result.get('selected_action_ids'),
            worker_report_path=output/'worker/worker_report.json',
        )
    ledger.update(status='complete' if ledger.get('completed') else 'deferred',
        last_checked_pt=after_points if type(after_points) is int else result.get('remaining_skill_points',points))
    _atomic_write_json(ledger_path,ledger)
    safety=FrameSafetyAnalyzer().analyze(Path(source))
    ctx.menu_observation={'screenshot_path':source,'result':{'frame_safety':safety.to_dict(),'decision':{'evidence':{'screen_text':{'normalized_text':safety.visible_text}}}}}
    learned=result.get("learning_result",{}).get("ok") is True
    selected_count=len(result.get("selected_action_ids",[])) if learned else 0
    status="learned" if learned else "complete" if ledger.get("completed") else "skipped"
    reason="skills_learned" if learned else "legacy_speed_target_complete" if ledger.get("completed") else result.get("reason","no_suitable_skills")
    message=f"已学习{selected_count}个名单技能" if learned else "旧版高峰启动目标已完成" if ledger.get("completed") else "本次没有符合名单、折扣和预算的可学习技能"
    if learned and diagnostic.get("deferred_by_batch_limit",0):message+="；执行器单次最多48个，其余等待下一次设置的检查时间"
    _learning_status(ctx,status,reason,message,mode=learning_mode(goal.skill_timing),selected_count=selected_count,
        next_check_turn=turn+normalize_mid_skill_learning(configured)["interval_turns"] if configured is not None and normalize_mid_skill_learning(configured)["interval_enabled"] else None)
    report.update(status=ledger['status'],ownership=held,after_skill_pt=after_points)
    _atomic_write_json(output/'report.json',report)
    return report,source,provider

def request_period(state):return state.get('scenario_state',{}).get('larc_period',{}).get('value')
