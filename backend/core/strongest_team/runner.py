"""One bounded strongest-team loop; original journals own all continuation."""
from copy import deepcopy
import json
from pathlib import Path
import time

from core.action_recovery import ReobserveRequired, ReobserveBudget

from .journal import atomic_write
from .journal import game_day
from .options import normalize_options
from .policy import choose


class Runner:
    def __init__(self, device, journal, emit, stop, *, options=None, clock=time.monotonic, screenshots=None, day_clock=None):
        self.device=device;self.journal=journal;self.emit=emit;self.stop=stop
        self.options=normalize_options(options);self.clock=clock
        device.journal=journal;device.options=self.options
        self.current={};self.wait_started=None;self.wait_reads=0
        self.screenshots=screenshots
        self.day_clock=day_clock or getattr(device,'day_clock',game_day)

    def consumed_checkpoint(self):
        # A settled race/recruit/claim is a durable consumption boundary even
        # while the outer daily task continues. Reuse the existing store; never
        # sweep a filesystem root or another task's frames.
        if (self.screenshots is not None and not self.journal.pending
                and self.current.get('page') in {'st_scout','st_recruit_done','st_hub','st_opponents','home'}):
            from .storage import consume
            consume(self.journal,self.screenshots,self.current,keep_current=True)

    def result(self,status,reason):
        frame=self.current
        pending=self.journal.pending
        navigation=(status in {'failed','cancelled'} and self.journal.data.get('context_verified')
                    and frame.get('page') not in {'home','races','events'})
        pending=pending or navigation
        self.journal.data['navigation_pending']=bool(navigation)
        if status in {'done','skipped'} and (pending or frame.get('page')!='home'):
            status='failed';reason='strongest_team_final_home_or_settlement_unverified'
        basis=('daily_entry_cap' if self.journal.cap_remaining()==0 else
               'available_resources_exhausted' if self.journal.data.get('resources_exhausted') else
               'event_unavailable_verified' if self.journal.data.get('event_unavailable') else
               'already_completed_today' if self.journal.data.get('done') else None)
        if status in {'done','skipped'} and basis is None:
            status='failed';reason='strongest_team_goal_not_verified'
        if status in {'done','skipped'}:
            self.journal.data['done']=True
        self.journal.data.update(status=status,reason=reason)
        self.journal.save()
        result=dict(kind='strongest_team',status=status,reason=reason,
            ok=status in {'done','skipped'},game_task_complete=status=='done',skip_verified=status=='skipped',
            skipped=status=='skipped',pending=bool(pending),entry_pending=self.journal.pending,
            journal_path=str(self.journal.path),scope=deepcopy(self.journal.data['scope']),
            last_observation=deepcopy(frame),output_dir=str(self.device.output_dir),
            effective_options=deepcopy(self.options),records=deepcopy(self.journal.data['records']))
        result['completion_basis']=basis
        for field in ['normal_reserved','normal_submitted','normal_completed','special_reserved','special_submitted','special_completed',
                      'clock_reserved','clock_confirmed','jelly_reserved','jelly_confirmed','points_confirmed']:
            result[field]=self.journal.data[field]
        path=Path(self.device.output_dir)/'strongest-team-result.json'
        atomic_write(path,result)
        return result

    def capture(self):
        if self.stop.is_set():raise RuntimeError('operator_stop')
        self.current=self.device.capture()
        self.journal.data['day_changed']=self.day_clock()!=self.journal.data['scope']['day']
        self.journal.observe(self.current)
        return self.current

    def run(self):
        budget=ReobserveBudget();repeated={};actions=0
        try:
            self.capture()
            while True:
                if self.stop.is_set():return self.result('cancelled','operator_stop')
                decision=choose(self.current,self.journal,self.options)
                if decision['kind']=='finish':return self.result(decision['status'],decision['reason'])
                if decision['kind']=='wait':
                    if self.wait_started is None:self.wait_started=self.clock()
                    self.wait_reads+=1
                    if self.clock()-self.wait_started>90 or self.wait_reads>180:
                        raise RuntimeError('strongest_team_wait_exhausted:'+decision['reason'])
                    if self.stop.wait(.2):return self.result('cancelled','operator_stop')
                    self.capture();continue
                self.wait_started=None;self.wait_reads=0
                action=decision['action'];before=self.current
                signature=(before['page'],action,before.get('frame_sha256'))
                repeated[signature]=repeated.get(signature,0)+1
                if repeated[signature]>3:raise RuntimeError('strongest_team_repeated_action_no_progress')
                actions+=1
                if actions>1200:raise RuntimeError('strongest_team_action_budget_exhausted')
                self.journal.data.update(decision.get('updates',{}))
                self.journal.reserve(action,before)
                self.emit(dict(type='progress',phase=before['page'],message='最强团队：'+action))
                try:
                    receipt=self.device.step(action,self.stop,before)
                except ReobserveRequired as error:
                    self.journal.failed(error)
                    self.current=budget.accept(error)
                    self.journal.observe(self.current)
                    repeated[signature]-=1
                    continue
                self.journal.receipt(receipt)
                if receipt.get('execution_status')=='executed':budget.executed()
                self.current=receipt.get('observation') or self.capture()
                self.consumed_checkpoint()
                if before.get('page')!=self.current.get('page'):repeated.clear()
        except Exception as error:
            self.journal.failed(error)
            return self.result('cancelled' if str(error).startswith('operator_stop') else 'failed',str(error))
