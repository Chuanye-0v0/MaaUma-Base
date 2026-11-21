"""Bounded recovery for proved non-consuming training navigation freeze."""
from pathlib import Path
import hashlib
import json
import time

import cv2
import numpy as np

from core.adb import ADB
from core.auto_trainer.cut_farming import atomic_json
from core.cut_portable_runtime import portable_root,device_ports,screenshot_output_allowed
from core.auto_trainer.frame_safety import FrameSafetyAnalyzer
from core.auto_trainer.safe_advance import SafeAdvancePlanner,SafeAdvanceExecutor,ADVANCE_CONFIRM_TOKEN
from core.auto_trainer.route_transition_session import RouteTransitionSession
from core.auto_trainer.training_current_stats import read_training_current_stats
from core.auto_trainer.main_menu_state_provider import provide_main_menu_state
from core.training_menu_precheck import TrainingMenuPrechecker

PACKAGE='com.bilibili.umamusu'
ALLOWED={'tap_title_start','enter_training_from_home','continue_training_modal'}


class _ResumePlanner(SafeAdvancePlanner):
    def plan(self,*args,**kwargs):
        decision=super().plan(*args,**kwargs)
        if decision.get('reason') not in ALLOWED and decision.get('action') not in {'wait','stop'}:
            return {'action':'stop','reason':'freeze_resume_page_not_allowed','evidence':decision.get('evidence',{})}
        return decision


def _read(path):return json.loads(Path(path).read_text(encoding='utf-8'))


def _identity(state):
    return {key:state.get(key) for key in ('remaining_turns','target_text','current_stats')}|{
        'period':state.get('scenario_state',{}).get('larc_period',{}).get('value')}


class CutTrainingFreezeRecovery:
    def __init__(self,session,*,adb_factory=None,prechecker=None,menu_reader=None,safety=None,
                 sleep=time.sleep,clock=time.monotonic,max_recoveries=2):
        self.session=session;self.adb_factory=adb_factory or (lambda:ADB(port=self.session.config.device_port))
        self.prechecker=prechecker or TrainingMenuPrechecker(scenario='larc')
        self.menu_reader=menu_reader or provide_main_menu_state;self.safety=safety or FrameSafetyAnalyzer()
        self.sleep,self.clock=sleep,clock;self.max_recoveries=max_recoveries

    def __call__(self,root,report,*,career_run_index):
        root=Path(root).resolve();config=self.session.config
        portable=portable_root() is not None
        scope=(config.device_port in device_ports() and screenshot_output_allowed(root)) if portable else config.device_port==16448
        if not scope or (not portable and not (config.validation_mode or config.discard_found_for_validation)):
            return {'handled':False,'reason':'freeze_recovery_outside_validation_device'}
        if self.session.data.get('pending_action') or self.session.data['status'] in {'found','unknown','stopped'}:
            return {'handled':False,'reason':'freeze_recovery_session_protected'}
        if report.get('stop_reason')!='larc_ss_information_failed' or not report.get('turns'):
            return {'handled':False,'reason':'not_training_information_freeze'}
        attempt=self.session.data['attempts'][-1];history=attempt.setdefault('auto_recoveries',[])
        if len(history)>=self.max_recoveries:return {'handled':False,'reason':'training_freeze_recovery_limit'}
        turn=report['turns'][-1];output=Path(turn.get('output','')).resolve()
        if not output.is_relative_to(root) or turn.get('turn_consumed') is not False:
            return {'handled':False,'reason':'freeze_turn_outcome_not_safe'}
        pipeline=_read(output/'pipeline_report.json');pipeline=pipeline.get('report',pipeline)
        execution=pipeline.get('stages',{}).get('live_loop',{}).get('execution',{})
        if (pipeline.get('turn_consumed') is True or any(execution.get(key) for key in
                ('performed','tap_performed','input_may_have_occurred','input_receipts'))):
            return {'handled':False,'reason':'freeze_consuming_outcome_unknown'}
        for item in pipeline.get('stages',{}).get('larc_ss',{}).get('step_reports',[]):
            result=item.get('result',{})
            if result.get('decision',{}).get('reason')=='larc_ss_confirm' and any(result.get(key) for key in
                    ('performed','tap_performed','input_may_have_occurred','larc_ss_execution_receipt')):
                return {'handled':False,'reason':'freeze_ss_input_unresolved'}
        info=pipeline.get('stages',{}).get('capture',{}).get('larc_ss_information',{})
        if (info.get('reason')!='information_open_frozen' or info.get('turn_consuming_action_executed') is not False
                or len(info.get('steps',[]))!=1):return {'handled':False,'reason':'freeze_navigation_not_proved'}
        step=info['steps'][0]
        if (step.get('decision',{}).get('reason')!='larc_ss_information.open' or step.get('input_performed') is not True
                or step.get('safety_verdict',{}).get('allowed') is not True):
            return {'handled':False,'reason':'freeze_navigation_not_proved'}
        frozen=[Path(value).resolve() for value in info.get('freeze_frames',[])]
        if len(frozen)<3 or any(not p.is_relative_to(root) or not p.is_file() for p in frozen):
            return {'handled':False,'reason':'freeze_frames_missing'}
        images=[cv2.imread(str(p)) for p in frozen]
        if any(image is None for image in images) or not all(np.array_equal(images[0],image) for image in images[1:]):
            return {'handled':False,'reason':'training_frames_are_moving'}
        progress=_read(root/'progress.json')
        if (progress.get('run_session_id')!=root.name or progress.get('device')!=f'127.0.0.1:{config.device_port}'
                or turn.get('turn_index')!=progress.get('next_turn') or (output/'action_receipt.json').exists()):
            return {'handled':False,'reason':'freeze_progress_or_receipt_not_safe'}
        pending=root/'session/larc_autonomous_pending.json'
        if pending.exists():
            intent=_read(pending)
            if (set(intent)-{'run_session_id','turn_index','output'} or intent.get('run_session_id')!=root.name
                    or intent.get('turn_index')!=progress['next_turn'] or Path(intent.get('output','')).resolve()!=output):
                return {'handled':False,'reason':'freeze_consuming_intent_unknown'}
        for name in ('larc_friend_outing.json','larc_race_checkpoint.json'):
            path=root/'session'/name
            if path.exists():
                data=_read(path)
                if data.get('pending') or data.get('status') in {'in_flight','blocked'}:
                    return {'handled':False,'reason':'freeze_other_input_unresolved'}
        for path in (root/'session').glob('larc_adaptation_*.json'):
            if _read(path).get('stage') in {'submission_pending','awaiting_result'}:
                return {'handled':False,'reason':'freeze_resource_input_unresolved'}
        baseline=_read(output/'capture/state_context.json');baseline=baseline.get('state_context',baseline)
        stats=read_training_current_stats(images[0])
        if stats.get('status')!='ok':return {'handled':False,'reason':'freeze_stats_unverified'}
        baseline['current_stats']=stats['values'];expected=_identity(baseline)
        folder=root/'automatic_recovery'/f'freeze_{len(history)+1:02d}';folder.mkdir(parents=True,exist_ok=False)
        record={'automatic':True,'kind':'training_navigation_freeze','status':'started','run_id':root.name,
            'career_run_index':career_run_index,'failed_stopped_at_unix_ms':attempt['career_runs'][career_run_index]['stopped_at_unix_ms'],
            'started_at_unix_ms':int(time.time()*1000),'device':f'127.0.0.1:{config.device_port}','package':PACKAGE,
            'commands':[],'restart_performed':False,'expected_identity':expected,'evidence':str(folder)}
        history.append(record);self.session.save();started=self.clock()
        def save():atomic_json(folder/'report.json',record);self.session.save()
        def capture(name):
            path=folder/name;image=adb.screenshot(str(path))
            return path,image
        def verify(path):
            data=self.menu_reader(path,path.with_suffix('.state.json'),turn_index=progress['next_turn'],scenario='larc')
            state=data.get('state_context',{})
            if data.get('valid_for_training') is not True or _identity(state)!=expected:return False
            if _read(root/'progress.json')!=progress:return False
            route=RouteTransitionSession.load(root/'session/transition_session.json',expected_run_session_id=root.name)
            route.observe('main_menu',source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),resync=True)
            route.save(root/'session/transition_session.json')
            if pending.exists():pending.unlink();record['cleared_nonconsuming_intent']=True
            return True
        try:
            adb=self.adb_factory()
            if not adb.connect():raise ValueError('freeze_adb_connect_failed')
            path,image=capture('fresh_before.png')
            if image is None or not self.prechecker.check(path).accepted:raise ValueError('freeze_current_training_unverified')
            if not np.array_equal(image,images[0]):raise ValueError('freeze_current_frame_changed')
            # Probe a different, non-consuming navigation once; never repeat SS.
            decision=SafeAdvancePlanner().plan(path,scenario='larc',visible_text=self.safety.analyze(path).visible_text,
                allow_return_main_menu_from_training=True)
            if decision.get('reason')!='return_main_menu_from_training':raise ValueError('freeze_back_unverified')
            tap=decision['tap']
            if adb.tap(tap['x'],tap['y'],scaled=False) is not True:raise ValueError('freeze_back_input_unknown')
            record['diagnostic_back']=dict(tap);save();still=[]
            for index in range(3):
                self.sleep(.5);after,current=capture(f'back_{index}.png')
                if current is None:raise ValueError('freeze_capture_failed')
                if not np.array_equal(current,image):
                    if verify(after):record['reason']='nonconsuming_back_recovered';record['status']='recovered';record['ok']=True;return record
                    raise ValueError('freeze_back_destination_changed')
                still.append(after)
            record['frozen_after_back']=[str(p) for p in still]
            for args in [('shell','am','force-stop',PACKAGE),('shell','am','start','-n',PACKAGE+'/com.uo.sdk.SplashActivity')]:
                record['commands'].append({'args':list(args),'status':'pending'});save()
                result,ok=adb._run(*args,timeout=20)
                record['commands'][-1].update(status='ok' if ok else 'failed',returncode=getattr(result,'returncode',None));save()
                if not ok:raise ValueError('freeze_app_command_failed')
                record['restart_performed']=True
            executor=SafeAdvanceExecutor(adb_factory=lambda:adb,planner=_ResumePlanner(),safety_analyzer=self.safety,sleep_fn=self.sleep,monotonic_fn=self.clock)
            last=None;deadline=self.clock()+90
            for index in range(60):
                if self.clock()>deadline:break
                path,current=capture(f'relaunch_{index:02d}.png')
                if current is None:raise ValueError('freeze_restart_capture_failed')
                if verify(path):record.update(status='recovered',ok=True,reason='same_career_restored');return record
                packet=self.safety.analyze(path);planner=SafeAdvancePlanner()
                decision=planner.plan(path,scenario='larc',visible_text=packet.visible_text,allow_enter_training_from_global_home=True)
                if decision.get('reason') in {'global_home','resource_prompt'}:
                    decision=planner.plan(path,scenario='larc',allow_enter_training_from_global_home=True)
                reason=decision.get('reason')
                if reason in ALLOWED:
                    if reason==last:self.sleep(.3);continue
                    if reason=='enter_training_from_home' and not any(t in packet.visible_text for t in ('出道前','初级','经典','高级')):
                        raise ValueError('freeze_existing_career_not_visible')
                    result=executor.run_once(mode='live',execute=True,confirm_token=ADVANCE_CONFIRM_TOKEN,scenario='larc',
                        screenshot_path=path,initial_frame=current if callable(getattr(adb,'save_frame',None)) else None,post_wait_freezes=False,
                        allow_enter_training_from_global_home=reason=='enter_training_from_home',
                        expected_action_id='safe_advance.home_training' if reason=='enter_training_from_home' else None)
                    if not result.get('performed'):raise ValueError('freeze_resume_action_unverified')
                    record.setdefault('resume_actions',[]).append(result['decision']['reason']);last=reason;save()
                elif packet.dangerous:raise ValueError('freeze_resource_page_not_allowed')
                self.sleep(.3)
            raise ValueError('freeze_restart_destination_unknown')
        except Exception as exc:
            record.update(ok=False,status='unknown',reason=str(exc));return record
        finally:
            record['ended_at_unix_ms']=int(time.time()*1000);record['elapsed_seconds']=max(0.,self.clock()-started);save()
