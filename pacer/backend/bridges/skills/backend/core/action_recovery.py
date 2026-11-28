"""Common action boundary: observe freely, never replay an uncertain input.

The executor still owns authorization and the task owns settlement. This layer
does not infer transaction success from a page, error string or ADB return code.
"""
import functools
import json
import os
from pathlib import Path
import time

class ReobserveRequired(RuntimeError):

    def __init__(self, action, observation):
        super().__init__('action_page_changed:' + action)
        self.observation = observation

class ObservationTimeout(RuntimeError):
    pass

def require_fresh_observation(observed, action, *, reason='stale_frame'):
    """Expired neutral candidates yield for reobservation before any input.

    The domain authorizer still verifies the newly offered action. Missing or
    dishonest clocks and resource/unknown actions never gain retry authority.
    action_boundary rejects this handoff if capture already dispatched input.
    """
    from core.observation_timing import action_kind, observation_age, maximum_age
    age = observation_age(observed)
    if age is not None and age <= maximum_age(observed, action):
        return
    if age is not None and observed.get('page') not in {'unknown', 'loading', None} and (action_kind(observed, action) != 'transaction'):
        error = ReobserveRequired(action, observed)
        error.reason = reason
        raise error
    raise RuntimeError(reason)

def recovery_decision(status, page, stopped=False):
    if stopped:
        return 'stop'
    if page == 'connection_conflict':
        return 'pause_account'
    if status == 'uncertain':
        return 'reconcile_original_progress'
    if page in {'connection_interrupted', 'disconnected'}:
        return 'reconnect_then_reconcile'
    if page in {'auth_blocked', 'password', 'recent', 'records'}:
        return 'identity_required'
    return 'reobserve' if status == 'not_executed' else 'verify_postcondition'

def failure_details(error):
    details = getattr(error, 'action_outcome', None)
    return {'action_outcome': details} if isinstance(details, dict) else {}

def receipt_replan(receipt, budget):
    """Normalize existing explicit zero-input handoffs without running hooks."""
    if receipt.get('execution_status') != 'not_executed' or not (receipt.get('performed') is False or ('action' in receipt and receipt['action'] is None)):
        return None
    error = ReobserveRequired(receipt.get('action_id') or receipt.get('action') or 'handoff', receipt['observation'])
    error.action_outcome = receipt['action_outcome']
    return budget.accept(error)

def action_boundary(method):
    """Record input intent before transport; keep the legacy receipt contract.

Wrappers are local to one synchronous step, restored even on failure. Tracking
the actual command boundary also covers compound gestures and popup inputs.
No command arguments, OCR text, account names or credentials are persisted.
"""

    @functools.wraps(method)
    def wrapped(device, action, *args, **kwargs):
        if getattr(device, '_action_boundary_active', False):
            return method(device, action, *args, **kwargs)
        original_command, original_capture = (device.command, device.capture)
        saved = {key: device.__dict__.get(key) for key in ('command', 'capture')}
        present = {key: key in device.__dict__ for key in saved}
        device._action_boundary_active = True
        state = dict(action_id=action, execution_status='not_executed', input_attempts=0, acknowledged_inputs=0, source_page=None, source_frame_sha256=None)
        latest = getattr(device, '_latest', None) or {}
        directory = getattr(device, 'output_dir', None)
        path = Path(directory) / 'recovery' / 'last-action.json' if directory is not None else None

        def checkpoint():
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix('.tmp')
                with temporary.open('w', encoding='utf8') as stream:
                    json.dump(state, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)

        def capture(*values, **options):
            nonlocal latest
            latest = original_capture(*values, **options)
            return latest

        def command(*values, **options):
            mutation = 'shell' in values and (not any((name in values for name in ('dumpsys', 'resolve-activity', 'pidof'))))
            if mutation:
                state.update(execution_status='uncertain', input_attempts=state['input_attempts'] + 1, source_page=latest.get('page'), source_frame_sha256=latest.get('frame_sha256'))
                checkpoint()
            result = original_command(*values, **options)
            if mutation:
                state['acknowledged_inputs'] += 1
            return result
        device.command, device.capture = (command, capture)
        pause_signal = next((value for value in (*args, *kwargs.values()) if callable(getattr(value, 'begin_action', None)) and callable(getattr(value, 'end_action', None))), None)
        pause_action_started = False
        try:
            if pause_signal is not None:
                pause_signal.begin_action()
                pause_action_started = True
            receipt = method(device, action, *args, **kwargs)
            if state['input_attempts'] and receipt.get('ok') is not False and receipt.get('observation'):
                state['execution_status'] = 'executed'
            state['observed_page'] = (receipt.get('observation') or latest).get('page')
            state['decision'] = recovery_decision(state['execution_status'], state['observed_page'], str(receipt.get('reason', '')).startswith('operator_stop'))
            checkpoint()
            device.last_action_outcome = dict(state)
            return dict(receipt, execution_status=state['execution_status'], action_outcome=dict(state))
        except Exception as error:
            if state['input_attempts'] == 0 and type(error) is RuntimeError and (str(error) in {'stale_frame', 'stale_observation'}) and (action in latest.get('actions', {})):
                try:
                    require_fresh_observation(latest, action, reason=str(error))
                except ReobserveRequired as reobserve:
                    error = reobserve
                except RuntimeError:
                    pass
            state['observed_page'] = getattr(error, 'observation', latest).get('page')
            state['decision'] = recovery_decision(state['execution_status'], state['observed_page'], str(error).startswith('operator_stop'))
            if state['decision'] == 'reobserve' and (not isinstance(error, ReobserveRequired)):
                state['decision'] = 'wait_exhausted' if isinstance(error, ObservationTimeout) else 'stop'
            device.last_action_outcome = dict(state)
            try:
                checkpoint()
            except OSError:
                pass
            if isinstance(error, ReobserveRequired) and state['input_attempts']:
                error = RuntimeError(str(error))
            error.action_outcome = dict(state)
            raise error
        finally:
            if pause_action_started:
                pause_signal.end_action()
            for key in saved:
                if present[key]:
                    setattr(device, key, saved[key])
                else:
                    device.__dict__.pop(key, None)
            device._action_boundary_active = False
    return wrapped

def await_candidate(capture, stop, action, observed, source_page, *, seconds=8, reads=16, reason=None, alternatives=()):
    """Only read while a source control is obscured; never move the old point.

Return a fresh offered candidate to the ORIGINAL domain authorization. A
different recognized page or an explicitly reviewed replacement control is a
typed zero-input handoff, not action success. No alternative is selected here.
"""
    deadline = time.monotonic() + seconds
    for attempt in range(reads + 1):
        if stop.is_set():
            raise RuntimeError('operator_stop')
        page = observed.get('page')
        if page in {'connection_conflict', 'connection_interrupted', 'disconnected', 'auth_blocked'}:
            raise ReobserveRequired(action, observed)
        if page == source_page and (action in observed.get('actions', {}) or action in observed.get('navigation_actions', {})):
            return observed
        if page == source_page and any((a in observed.get('actions', {}) for a in alternatives)):
            raise ReobserveRequired(action, observed)
        if page not in {source_page, 'unknown', 'loading'}:
            raise ReobserveRequired(action, observed)
        if attempt == reads or time.monotonic() >= deadline:
            error = ObservationTimeout(reason or 'action_readiness_timeout:' + action)
            error.observation = observed
            raise error
        if stop.wait(0.3):
            raise RuntimeError('operator_stop')
        observed = capture()

class ReobserveBudget:
    """Consecutive no-input replans cannot reset a deadline or grow unbounded."""

    def __init__(self, limit=6, seconds=30):
        self.limit, self.seconds = (limit, seconds)
        self.count, self.started = (0, None)

    def accept(self, error):
        outcome = getattr(error, 'action_outcome', {})
        if not isinstance(error, ReobserveRequired) or outcome.get('execution_status') != 'not_executed':
            raise error
        observed = error.observation
        if observed.get('page') in {'connection_conflict', 'connection_interrupted', 'disconnected', 'auth_blocked', 'password', 'recent', 'records'}:
            raise error
        now = time.monotonic()
        if self.started is None:
            self.started = now
        self.count += 1
        if self.count > self.limit or now - self.started >= self.seconds:
            exhausted = RuntimeError('action_reobserve_limit')
            exhausted.action_outcome = dict(outcome, decision='wait_exhausted')
            raise exhausted from error
        return observed

    def executed(self):
        self.count, self.started = (0, None)
