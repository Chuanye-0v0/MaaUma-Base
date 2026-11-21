"""Recover a confirmed post-race white-screen timeout without replaying entry."""
import json
from pathlib import Path
from types import SimpleNamespace
import time
import uuid

import numpy as np

from core.auto_trainer.cut_farming import atomic_json
from core.auto_trainer.runtime_control import current,RunStopped,ResumeFromCurrentPage
from core.auto_trainer.cut_training_freeze_recovery import PACKAGE
from core.cut_portable_runtime import portable_root,device_ports,screenshot_output_allowed
from core.native_capture_adb import adb_for_capture


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


def white_transition(image):
    """Allow a tiny tap sparkle; a static information/menu page is not a freeze."""
    if image is None or image.ndim!=3 or image.shape[1::-1] not in {(720,1280),(1440,2560)}:
        return False
    pixels=image.astype(np.int16)
    return float(((pixels.min(axis=2)>240)&(pixels.max(axis=2)-pixels.min(axis=2)<20)).mean())>.97


class CutRaceFreezeRecovery:
    def __init__(self,session,*,adb_factory=None,resume=None,sleep=time.sleep,clock=time.monotonic,max_recoveries=2):
        self.session=session;self.adb_factory=adb_factory or (lambda:adb_for_capture(session.config.device_port))
        self.resume=resume;self.sleep=sleep;self.clock=clock;self.max_recoveries=max_recoveries

    def __call__(self,root,report,*,career_run_index):
        root=Path(root).resolve();config=self.session.config
        def ignored(reason):return {'handled':False,'reason':reason}
        if report.get('stop_reason')!='race_transition_no_progress_timeout':return ignored('not_race_transition_timeout')
        portable=portable_root() is not None
        scope=(config.device_port in device_ports() and screenshot_output_allowed(root)) if portable else (
            config.validation_mode and config.device_port==16448 and root.drive.upper()=='E:')
        if not scope:return ignored('race_recovery_device_or_output_scope')
        if self.session.data.get('pending_action') or self.session.data.get('status')!='running':
            return ignored('race_recovery_session_protected')
        attempt=self.session.data['attempts'][-1]
        if attempt.get('run_id')!=root.name or attempt.get('cut_found'):return ignored('race_recovery_attempt_protected')
        history=attempt.setdefault('auto_recoveries',[])
        if len(history)>=self.max_recoveries:return ignored('race_freeze_recovery_limit')
        progress_path=root/'progress.json';checkpoint_path=root/'session/larc_race_checkpoint.json'
        try:progress=_read(progress_path);saved=_read(checkpoint_path)
        except (OSError,ValueError):return ignored('race_recovery_checkpoint_missing')
        address=f'127.0.0.1:{config.device_port}'
        if (progress.get('run_session_id')!=root.name or progress.get('device')!=address
                or saved.get('run_session_id')!=root.name or saved.get('device')!=address
                or saved.get('turn_index')!=progress.get('next_turn')
                or saved.get('status')!='stopped' or saved.get('stop_reason')!='race_transition_no_progress_timeout'
                or saved.get('current_input_receipts')):
            return ignored('race_recovery_checkpoint_not_owned')
        from scripts.larc_autonomy_race import _fixed_race_entry_confirmed
        steps=saved.get('steps') or []
        if not _fixed_race_entry_confirmed(steps):return ignored('race_entry_not_confirmed')
        if not steps or steps[-1].get('result',{}).get('decision',{}).get('action')!='wait':
            return ignored('race_recovery_not_loading')
        last_input=next((s.get('result',{}) for s in reversed(steps)
            if any(s.get('result',{}).get(k) for k in ('performed','tap_performed','input_receipts'))),{})
        if last_input.get('decision',{}).get('reason') not in {'view_race_result','race_result_continue'}:
            return ignored('race_recovery_not_post_result')
        if (last_input.get('performed') is not True or last_input.get('tap_performed') is not True
                or any(r.get('outcome')!='confirmed' for r in last_input.get('input_receipts') or [])):
            return ignored('race_result_input_unconfirmed')
        if (root/'session/larc_autonomous_pending.json').exists():return ignored('race_recovery_other_action_pending')
        for name in ('larc_friend_outing.json',):
            path=root/'session'/name
            if path.exists() and _read(path).get('pending'):return ignored('race_recovery_other_action_pending')
        for path in (root/'session').glob('larc_adaptation_*.json'):
            if _read(path).get('stage') in {'submission_pending','awaiting_result'}:
                return ignored('race_recovery_resource_pending')
        folder=root/'automatic_recovery'/f'race_{len(history)+1:02d}_{uuid.uuid4().hex[:6]}'
        folder.mkdir(parents=True,exist_ok=False)
        adb=self.adb_factory()
        if not adb.connect():return ignored('race_recovery_connection_failed')
        for index in range(3):
            if current() is not None:current().checkpoint('race_freeze_observation')
            image=adb.screenshot(str(folder/f'white_{index}.png'))
            if not white_transition(image):return ignored('race_white_transition_not_present')
            self.sleep(.5)
        record={'automatic':True,'kind':'post_race_white_screen','status':'started','ok':False,
            'run_id':root.name,'career_run_index':career_run_index,'started_at_unix_ms':int(time.time()*1000),
            'failed_stopped_at_unix_ms':attempt['career_runs'][career_run_index]['stopped_at_unix_ms'],
            'restart_performed':False,'commands':[],'race_entry_replayed':False,'new_career_started':False}
        history.append(record);started=self.clock()
        def save():atomic_json(folder/'report.json',record);self.session.save()
        try:
            save()
            for args in [('shell','am','force-stop',PACKAGE),('shell','am','start','-n',PACKAGE+'/com.uo.sdk.SplashActivity')]:
                if current() is not None:current().checkpoint('race_freeze_restart')
                record['commands'].append({'args':list(args),'status':'pending'});save()
                result,ok=adb._run(*args,timeout=20)
                record['commands'][-1].update(status='ok' if ok else 'failed',returncode=getattr(result,'returncode',None));save()
                if not ok:raise ValueError('race_restart_command_failed')
                record['restart_performed']=True
            from scripts.larc_same_account_reconnect import reconnect_existing_career
            resume=self.resume or reconnect_existing_career
            nav=resume(SimpleNamespace(root=root,run_id=root.name,port=config.device_port,progress=progress),
                folder/'resume',require_existing_career=True,allow_bilibili_sdk_terms=False,
                adb=adb,sleep_fn=self.sleep,clock_fn=self.clock)
            record['resume_report']=nav
            if not nav.get('ok') or nav.get('new_career_started') or nav.get('tp_start_submitted'):
                raise ValueError('race_existing_career_resume_unverified')
            source=Path(nav.get('current_source','')).resolve()
            if not source.is_relative_to(folder) or not source.is_file():raise ValueError('race_resume_source_unverified')
            # The existing race owner will reobserve its current result/menu.
            # Keep the confirmed entry, clocks and calendar receipts intact.
            if _read(progress_path)!=progress or _read(checkpoint_path)!=saved:
                raise ValueError('race_records_changed_during_recovery')
            record.update(ok=True,status='recovered',reason='same_career_ready_for_race_owner')
            return record
        except (RunStopped,ResumeFromCurrentPage):
            record.update(status='stopped',reason='race_recovery_interrupted');raise
        except Exception as exc:
            record.update(status='unknown',reason=str(exc));return record
        finally:
            record.update(ended_at_unix_ms=int(time.time()*1000),elapsed_seconds=max(0.,self.clock()-started));save()
