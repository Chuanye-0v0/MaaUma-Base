"""Bounded same-account restart/relogin handoffs to the original task's owner."""
from desktop.umaa_public_context import endpoint as _public_endpoint
from contextvars import ContextVar
from pathlib import Path
import json
import time
_scope = ContextVar('umaa_restart_scope', default=None)

class RestartScope:

    def __init__(self, task, kwargs):
        self.task, self.kwargs = (task, kwargs)
        self.verifier = kwargs.get('verify_restart_account')
        self.identity = None
        self.continuing = False

    def __enter__(self):
        self.token = _scope.set(self)
        return self

    def __exit__(self, *args):
        _scope.reset(self.token)

    def checkpoint(self):
        if self.verifier is not None or self.identity is not None or self.continuing:
            return
        if any((self.kwargs.get(k) is not None for k in ('resume_plant_from', 'resume_legend_from', 'resume_limited_from', 'resume_registration_from', 'resume_jjc_from', 'resume_activities_from', 'resume_finish_result', 'resume_harvest_summary', 'resume_settlement_from'))):
            return
        adb = self.kwargs.get('adb_path')
        if not adb or not Path(adb).is_file():
            return
        from core.account_inventory.live import InventoryDevice
        from core.account_inventory.public_navigation import to_profile, to_home
        device = InventoryDevice(self.kwargs['endpoint'], adb, self.kwargs['output'] / 'restart-identity')
        device.connect()
        try:
            frame = to_profile(device, self.kwargs['stop'])
            expected = frame.get('trainer_id')
            home = to_home(device, self.kwargs['stop'], observed=frame)
            if expected and home.get('page') == 'home':
                self.identity = str(expected)

                def verify():
                    probe = InventoryDevice(self.kwargs['endpoint'], adb, self.kwargs['output'] / 'restart-identity-after')
                    probe.connect()
                    try:
                        current = to_profile(probe, self.kwargs['stop'])
                        same = str(current.get('trainer_id')) == self.identity
                        return same and to_home(probe, self.kwargs['stop'], observed=current).get('page') == 'home'
                    finally:
                        probe.close()
                self.verifier = verify
        finally:
            device.close()

def checkpoint():
    active = _scope.get()
    if active is not None:
        active.checkpoint()

def continue_task(task, options, before, recovery, kwargs, invoke, scope):
    stop, endpoint = (kwargs['stop'], kwargs['endpoint'])
    if stop.is_set():
        return dict(before, ok=False, reason='operator_stop')
    if scope.verifier is None:
        return dict(before, restart_resume=dict(ok=False, reason='restart_account_identity_unavailable'))
    startup = recovery.get('startup') or {}
    frame = startup.get('last_observation') or {}
    receipt = recovery.get('receipt') or {}
    if recovery.get('ok') is not True or startup.get('ok') is not True or frame.get('page') != 'home' or (frame.get('endpoint') != endpoint) or (receipt.get('endpoint') != endpoint) or (receipt.get('launch_accepted') is not True):
        return dict(before, restart_resume=dict(ok=False, reason='restart_home_unverified'))
    if before.get('account_conflict') or before.get('account_paused'):
        return dict(before, restart_resume=dict(ok=False, reason='restart_account_held'))
    try:
        verified = scope.verifier() is True
    except (RuntimeError, OSError, ValueError) as error:
        return dict(before, restart_resume=dict(ok=False, reason='restart_account_unverified', detail=str(error)))
    if not verified or stop.is_set():
        return dict(before, restart_resume=dict(ok=False, reason='restart_account_unverified'))
    return _continue_owner(task, options, before, recovery, kwargs, invoke, scope, kind='restart')

def continue_session_task(task, options, before, recovery, kwargs, invoke, scope):
    """A same-account relogin hands progress to an owner, never declares success."""
    stop, endpoint = (kwargs['stop'], kwargs['endpoint'])
    result = dict(before, session_recovery=recovery)

    def refused(reason):
        return dict(result, session_resume=dict(ok=False, reason=reason))
    if stop.is_set():
        return dict(refused('operator_stop'), reason='operator_stop')
    if before.get('account_conflict') or before.get('account_paused'):
        return refused('session_account_held')
    fresh = recovery.get('last_observation') or {}
    if endpoint != _public_endpoint() or recovery.get('ok') is not True or recovery.get('reason') != 'same_account_session_restored' or (recovery.get('endpoint') != endpoint) or (fresh.get('page') != 'home') or (fresh.get('endpoint') != endpoint):
        return refused('session_home_unverified')
    if scope.verifier is None:
        return refused('session_account_identity_unavailable')
    try:
        verified = scope.verifier() is True
    except (RuntimeError, OSError, ValueError) as error:
        return dict(refused('session_account_unverified'), session_verification_error=str(error))
    if stop.is_set():
        return dict(refused('operator_stop'), reason='operator_stop')
    if not verified:
        return refused('session_account_unverified')
    return _continue_owner(task, options, before, recovery, kwargs, invoke, scope, kind='session')

def _continue_owner(task, options, before, recovery, kwargs, invoke, scope, *, kind):
    stop, endpoint = (kwargs['stop'], kwargs['endpoint'])
    output = Path(kwargs['output']).resolve()
    source = output / (kind + '-before-' + str(time.time_ns()) + '.json')
    source.write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding='utf8')
    peer = dict(kwargs, auto_restart_game=False, restart_from=source, restart_account_verified=True, verify_restart_account=scope.verifier)
    scope.continuing = True
    from core.stall_watch import StallWatch
    metadata = 'session_resume' if kind == 'session' else 'restart_resume'
    recovered = 'session_recovery' if kind == 'session' else 'stall_recovery'
    try:
        kwargs['emit'](dict(type='progress', message='重登后账号已核实，承接原任务未完成进度' if kind == 'session' else '重启后账号已核实，承接原任务未完成进度'))
        with StallWatch(endpoint, stop):
            resumed = invoke(task, options, **peer)
    except (RuntimeError, OSError, ValueError) as error:
        return dict(before, reason=str(error), pending=True, **{recovered: recovery, metadata: dict(ok=False, reason=str(error), source=str(source)), kind + '_prior_reason': before.get('reason')})
    if not isinstance(resumed, dict):
        return dict(before, pending=True, reason=kind + '_owner_result_invalid', **{recovered: recovery, metadata: dict(ok=False, reason=kind + '_owner_result_invalid')})
    result = dict(resumed, **{recovered: recovery, metadata: dict(ok=resumed.get('ok') is True, source=str(source), task=task)})
    result[kind + '_prior_reason'] = before.get('reason')
    return result
