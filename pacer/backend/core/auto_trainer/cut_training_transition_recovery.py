"""Pre-debut ordinary training freeze: restart, then reconcile one game turn."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import time
import uuid

import cv2
import numpy as np

from core.auto_trainer.cut_farming import atomic_json
from core.cut_portable_runtime import portable_root,device_ports,screenshot_output_allowed
from core.auto_trainer.cut_training_freeze_recovery import PACKAGE
from core.auto_trainer.frame_safety import FrameSafetyAnalyzer
from core.auto_trainer.larc_calendar import LAST_TRAINING_DAY,period_at,period_ordinal
from core.auto_trainer.main_menu_state_provider import provide_main_menu_state
from core.auto_trainer.route_transition_session import RouteTransitionSession
from core.auto_trainer.safe_advance import SafeAdvancePlanner,SafeAdvanceExecutor
from core.auto_trainer.safe_advance_transition import SafeAdvanceRoutePolicy
from core.native_capture_adb import adb_for_capture
from core.training_menu_precheck import TrainingMenuPrechecker
from core.main_menu_state import analyze_main_menu_state


def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))


STAGES={1:'junior',2:'classic',3:'senior'}


def valid_ordinary_before_period(period):
    if not isinstance(period,dict):return False
    if period.get('pre_debut') is True:
        return period.get('stage')=='junior' and period.get('year_index')==1
    day=period_ordinal(period)
    return (day is not None and 12<=day<=LAST_TRAINING_DAY
        and period.get('stage')==STAGES.get(period.get('year_index')))


def ordinary_period_scope(period):
    return 'pre_debut' if isinstance(period,dict) and period.get('pre_debut') is True else 'ordinary_career'


def same_or_next_ordinary_period(before,after):
    if not isinstance(before,dict) or not isinstance(after,dict):return False
    if after==before:return True
    if before.get('pre_debut') is True or after.get('pre_debut') is True:return False
    before_day=period_ordinal(before);after_day=period_ordinal(after)
    return (before_day is not None and after_day is not None
        and after_day==before_day+1<=71
        and before.get('stage')==STAGES.get(before.get('year_index'))
        and after.get('stage')==STAGES.get(after.get('year_index')))


def turn_identity(state):
    return {'remaining_turns':state.get('remaining_turns'),'target_text':state.get('target_text'),
        'period':state.get('period') or state.get('scenario_state',{}).get('larc_period',{}).get('value')}


def recovered_header(path):
    """A loaded event page can expose the authoritative same-career header."""
    from core.auto_trainer.main_menu_state_provider import _field_is_high_confidence,_CALENDAR_REGION_READER,_parse_header_date_value
    fields=analyze_main_menu_state(path.read_bytes(),scenario='larc').get('fields',{})
    if not _field_is_high_confidence(fields.get('remaining_turns')):return None
    image=cv2.imread(str(path))
    if image is None or image.shape[:2]!=(1280,720):return None
    if _field_is_high_confidence(fields.get('target_text')):
        target=fields['target_text']['value']
    else:
        target,confidence=_CALENDAR_REGION_READER.read_line(image[78:126,230:595])
        target=''.join(str(target).split())
        if not target or confidence<.95:return None
    text,confidence=_CALENDAR_REGION_READER.read_line(image[36:72,155:345])
    text=''.join(str(text).split());period=_parse_header_date_value(text)
    if period is None or confidence<.95:
        text,confidence=_CALENDAR_REGION_READER.read_line(image[36:72,155:280])
        if ''.join(str(text).split())!='初级出道前' or confidence<.95:return None
        period={'pre_debut':True,'stage':'junior','year_index':1}
    return {'remaining_turns':fields['remaining_turns']['value'],'target_text':target,'period':period}


def training_transition(path,image,packet,route):
    if packet.dangerous or packet.text_status!='ok' or SafeAdvanceExecutor._career_home_controls_ready(image):return False
    text=packet.visible_text
    if '训练' not in text or not ('还剩' in text or all(term in text for term in ('回合结束后','代表交流赛开始'))):return False
    check=TrainingMenuPrechecker(scenario='larc').check(path)
    policy=SafeAdvanceRoutePolicy.resolve(current_state=route.current_state,
        expected_next_states=route.expected_next_states,last_action_id=route.last_action_id,
        action_source_sha256=route.action_source_sha256,bootstrap_state=None)
    # The receipt owns this ordinary training wipe. The shared detector checks
    # its training HUD and pink/cyan scene geometry, not arbitrary unknown UI.
    return SafeAdvancePlanner._looks_like_training_action_animation(image,route_policy=policy,
        training_check=check,perception={'screen':'unknown','screen_text':{'normalized_text':text}},scenario='larc')


class CutTrainingTransitionFreezeRecovery:
    def __init__(self,session,*,adb_factory=None,safety=None,menu_reader=provide_main_menu_state,
            transition_check=training_transition,resume=None,header_reader=recovered_header,sleep=time.sleep,clock=time.monotonic,max_recoveries=2):
        self.session=session;self.adb_factory=adb_factory or (lambda:adb_for_capture(self.session.config.device_port))
        self.safety=safety or FrameSafetyAnalyzer();self.menu_reader=menu_reader
        self.transition_check=transition_check;self.resume=resume;self.header_reader=header_reader
        self.sleep=sleep;self.clock=clock;self.max_recoveries=max_recoveries

    def __call__(self,root,report,*,career_run_index):
        root=Path(root).resolve();config=self.session.config
        def ignored(reason):return {'handled':False,'reason':reason}
        portable=portable_root() is not None
        scope=(config.device_port in device_ports() and screenshot_output_allowed(root)) if portable else (root.drive.upper()=='E:' and config.device_port==16448)
        if not scope:return ignored('transition_recovery_device_or_output_scope')
        if self.session.data.get('pending_action') or self.session.data['status']!='running':return ignored('transition_recovery_protected')
        if report.get('stop_reason')!='career_transition_no_progress_timeout' or not report.get('turns'):return ignored('not_training_transition_timeout')
        turn=report['turns'][-1];output=Path(turn.get('output','')).resolve()
        if turn.get('kind')!='train' or turn.get('turn_consumed') is not True or turn.get('pipeline_code')!=0 or not output.is_relative_to(root):
            return ignored('not_ordinary_training_input')
        receipt_path=output/'action_receipt.json'
        if not receipt_path.is_file() or read(receipt_path)!=turn.get('execution'):return ignored('training_receipt_not_owned')
        if turn['execution'].get('performed') is not True or turn['execution'].get('target') not in {'speed','stamina','power','guts','wisdom'}:
            return ignored('not_ordinary_training_input')
        progress=read(root/'progress.json');index=turn.get('turn_index')
        if (progress.get('run_session_id')!=root.name or progress.get('device')!=f'127.0.0.1:{config.device_port}'
                or type(index) is not int or progress.get('next_turn')!=index+1 or progress.get('actions_completed')!=index):
            return ignored('training_progress_not_owned')
        if (root/'session/larc_autonomous_pending.json').exists():return ignored('transition_pending_input')
        for name in ('larc_friend_outing.json','larc_race_checkpoint.json'):
            path=root/'session'/name
            if path.exists() and (read(path).get('pending') or read(path).get('status') in {'in_flight','blocked'}):return ignored('transition_other_input_unresolved')
        for path in (root/'session').glob('larc_adaptation_*.json'):
            if read(path).get('stage') in {'submission_pending','awaiting_result'}:return ignored('transition_resource_unresolved')
        route_path=root/'session/transition_session.json';route=RouteTransitionSession.load(route_path,expected_run_session_id=root.name)
        if route.last_action_id!='training.execute' or not route.action_source_sha256 or not route.expected_next_states:return ignored('training_transition_owner_missing')
        before=turn_identity(turn.get('before',{}))
        if (type(before['remaining_turns']) is not int or before['remaining_turns']<2
                or not valid_ordinary_before_period(before['period']) or not before['target_text']):
            return ignored('training_before_turn_unverified')
        attempt=self.session.data['attempts'][-1];history=attempt.setdefault('auto_recoveries',[])
        if len(history)>=self.max_recoveries:return ignored('training_freeze_recovery_limit')
        folder=root/'automatic_recovery'/f'transition_{len(history)+1:02d}_{uuid.uuid4().hex[:6]}'
        folder.mkdir(parents=True,exist_ok=False);adb=self.adb_factory()
        if not adb.connect():return ignored('transition_capture_connection_failed')
        images=[];sources=[]
        for number in range(3):
            path=folder/f'freeze_{number}.png';image=adb.screenshot(str(path))
            if image is None:return ignored('transition_capture_failed')
            if number==0 and not self.transition_check(path,image,self.safety.analyze(path),route):return ignored('current_training_transition_unverified')
            images.append(image);sources.append(str(path));self.sleep(.5)
        if not all(np.array_equal(images[0],image) for image in images[1:]):return ignored('training_transition_moving')
        record={'automatic':True,'kind':'ordinary_training_transition_freeze','status':'started','ok':False,
            'run_id':root.name,'career_run_index':career_run_index,
            'failed_stopped_at_unix_ms':attempt['career_runs'][career_run_index]['stopped_at_unix_ms'],
            'started_at_unix_ms':int(time.time()*1000),'device':f'127.0.0.1:{config.device_port}','package':PACKAGE,
            'freeze_frames':sources,'before_identity':before,'scope':ordinary_period_scope(before['period']),'progress_before':progress,'commands':[],
            'restart_performed':False,'evidence':str(folder)}
        history.append(record);started=self.clock()
        def save():atomic_json(folder/'report.json',record);self.session.save()
        try:
            save()
            for args in [('shell','am','force-stop',PACKAGE),('shell','am','start','-n',PACKAGE+'/com.uo.sdk.SplashActivity')]:
                record['commands'].append({'args':list(args),'status':'pending'});save()
                result,ok=adb._run(*args,timeout=20)
                record['commands'][-1].update(status='ok' if ok else 'failed',returncode=getattr(result,'returncode',None));save()
                if not ok:raise ValueError('transition_restart_command_failed')
                record['restart_performed']=True
            from scripts.larc_same_account_reconnect import reconnect_existing_career
            resume=self.resume or reconnect_existing_career
            nav=resume(SimpleNamespace(root=root,run_id=root.name,port=config.device_port,progress=progress,screen_reader=self.safety),folder/'resume',
                require_existing_career=True,adb=adb,sleep_fn=self.sleep,clock_fn=self.clock)
            record['resume_report']=nav
            if not nav.get('ok'):raise ValueError('transition_same_career_resume_unverified')
            source=Path(nav['current_source']).resolve()
            if not source.is_relative_to(folder):raise ValueError('transition_resume_source_outside_recovery')
            observed=self.menu_reader(source,source.with_suffix('.state.json'),turn_index=progress['next_turn'],scenario='larc')
            after=(turn_identity(observed.get('state_context',{})) if observed.get('valid_for_training') is True else self.header_reader(source))
            record['after_identity']=after
            if after is None or after['target_text']!=before['target_text'] or not same_or_next_ordinary_period(before['period'],after['period']):
                raise ValueError('transition_restored_career_or_date_changed')
            if read(root/'progress.json')!=progress:raise ValueError('transition_progress_changed_during_recovery')
            if after['remaining_turns']==before['remaining_turns']-1:
                record['training_outcome']='applied'
            elif after['remaining_turns']==before['remaining_turns']:
                if after['period']!=before['period']:
                    raise ValueError('transition_restored_career_or_date_changed')
                if observed.get('valid_for_training') is not True:
                    raise ValueError('transition_event_training_outcome_not_final')
                corrected={**progress,'actions_completed':progress['actions_completed']-1,'next_turn':index}
                atomic_json(root/'progress.json',corrected)
                record.update(training_outcome='unapplied',progress_correction={'before':progress,'after':corrected,
                    'original_receipt':str(receipt_path),'original_receipt_preserved':True})
            else:raise ValueError('transition_restored_remaining_turns_unverified')
            if observed.get('valid_for_training') is True:
                route.observe('main_menu',source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),resync=True);route.save(route_path)
            record.update(status='recovered',ok=True,reason='same_career_training_outcome_observed');return record
        except Exception as exc:
            record.update(status='unknown',reason=str(exc));return record
        finally:
            record.update(ended_at_unix_ms=int(time.time()*1000),elapsed_seconds=max(0.,self.clock()-started));save()
