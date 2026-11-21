"""Run-local continuation orchestration; only callers with active ownership act."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

from core.auto_trainer.cut_farming import atomic_json
from core.auto_trainer.larc_continuation import normalize, evaluate_junior, evaluate_inheritance, feature_available
from core.auto_trainer.runtime_control import current

OPTIONS = 'session/larc_continuation_options.json'
STATE = 'session/larc_continuation_state.json'
ACE_BINDING = 'session/ace_cycle_binding.json'
ACE_FAILURE_PACKET = 'session/ace_continuation_failure.json'
DONE = {'passed', 'observed', 'user_continued', 'historical_not_observed'}


def _ace_cycle_bound(ctx):
    binding = read(Path(ctx.root) / ACE_BINDING)
    return (
        isinstance(binding, dict)
        and binding.get('run_session_id') == ctx.run_id
        and binding.get('endpoint') == getattr(ctx, 'progress', {}).get('device')
        and binding.get('allow_continuation_restart') is True
    )


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig')) if Path(path).is_file() else {}


def save_options(root, value):
    root = Path(root)
    config = normalize(value)
    atomic_json(root / OPTIONS, {'run_session_id': root.name, 'config': config})
    return config


def acknowledge_pending(root):
    root = Path(root)
    state = read(root / STATE)
    if state.get('run_session_id') == root.name and state.get('pending'):
        state['continue_requested'] = True
        atomic_json(root / STATE, state)


def load_runtime(ctx, *, inspect=None, notify=None):
    if (Path(ctx.root) / 'session/career_workflow_context.json').is_file():
        from scripts.career_workflow_runtime import load_for_run, WorkflowRuntime
        return WorkflowRuntime(ctx, load_for_run(ctx.root))
    saved = read(Path(ctx.root) / OPTIONS)
    if not saved:
        return None
    if saved.get('run_session_id') != ctx.run_id:
        raise ValueError('continuation_options_run_mismatch')
    config = normalize(saved.get('config'))
    if config['enabled'] and not feature_available(Path(__file__).resolve().parents[1]):
        raise ValueError('installed_package_continuation_execution_disabled')
    return ContinuationRuntime(ctx, config, inspect=inspect, notify=notify) if config['enabled'] else None


class ContinuationRuntime:
    def __init__(self, ctx, config, *, inspect=None, notify=None):
        self.ctx, self.config = ctx, normalize(config)
        self.path = Path(ctx.root) / STATE
        self.state = read(self.path) or {'schema_version': 'larc_continuation_state.v1',
            'run_session_id': ctx.run_id, 'nodes': {}, 'notifications': {}, 'pending': None}
        if self.state.get('run_session_id') != ctx.run_id:
            raise ValueError('continuation_state_run_mismatch')
        self.inspect, self.notify = inspect, notify
        self.state.setdefault('inheritance_seen', False)
        self.sync_career_flow()

    def save(self):
        atomic_json(self.path, self.state)

    def observe_report(self, report):
        def walk(value):
            if not isinstance(value, dict):
                return False
            decision = value.get('decision') or {}
            if (value.get('performed') is True and
                    decision.get('reason') == 'inheritance_factor_confirm'):
                return True
            return any(walk(item) for item in value.values() if isinstance(item, dict)) or any(
                walk(item) for values in value.values() if isinstance(values, list)
                for item in values if isinstance(item, dict))
        if not self.state['inheritance_seen'] and walk(report):
            self.state['inheritance_seen'] = True
            self.save()

    def _cut_handoff_required(self):
        # Only CutFarmingController injects this owner. Ordinary PacerHost
        # runs, including an explicit cut-to-career handoff, have no cut hook.
        return getattr(self.ctx, 'cut_farming_hook', None) is not None

    def _resume_pending(self):
        pending = self.state.get('pending')
        if not pending:
            return None
        node = pending['node']
        ordinary_handoff = node == 'handoff' and not self._cut_handoff_required()
        control = current()
        resumed = self.state.pop('continue_requested', False) or (
            control is not None and control.epoch > pending['resume_epoch'])
        if not resumed and not ordinary_handoff:
            return None
        self.state['pending'] = None
        self.state['nodes'][node]['action'] = ('user_continued' if resumed
                                             else 'ordinary_career_continued')
        self.state['nodes'][node]['continued_at'] = time.time()
        self.save()
        if node == 'handoff' and not ordinary_handoff:
            return {'stop_reason': 'continuation_handoff', 'status': 'handed_off'}
        self.state['nodes'][node]['flow_status'] = self.state['nodes'][node]['action']
        self.save()
        self.sync_career_flow()
        return {'status': 'reobserve'}

    def can_continue_junior(self):
        entry = self.state['nodes'].get('junior', {})
        return entry.get('flow_status', entry.get('result', {}).get('status')) in DONE

    def sync_career_flow(self, period=None):
        """Keep the cut owner for preservation/budgets, not first-year routing."""
        if period and type(period.get('year_index')) is int and period['year_index'] >= 2:
            if 'junior' not in self.state['nodes']:
                self.state['nodes']['junior'] = {'flow_status': 'historical_not_observed',
                    'reason': 'attached_after_year1; earlier_snapshot_not_invented'}
        full_career = self.can_continue_junior()
        hook = getattr(self.ctx, 'cut_farming_hook', None)
        if hook is not None:
            hook.continuation_flow = full_career
        flow = 'full_career' if full_career else 'cut_screening'
        if self.state.get('career_flow') != flow:
            self.state['career_flow'] = flow
            self.save()
        return full_career

    def due(self, period):
        year, month, half = (period.get(key) for key in ('year_index', 'month', 'half'))
        if type(year) is not int or type(month) is not int:
            return None
        if self._cut_handoff_required() and (year > 2 or year == 2 and month >= 7):
            return 'handoff'  # Already outside this mode's supported automatic scope.
        nodes = self.state['nodes']
        if 'junior' not in nodes:
            if (year, month, half) == (1, 12, 'second'):
                return 'junior'
            if year >= 2:
                nodes['junior'] = {'flow_status': 'historical_not_observed',
                    'reason': 'attached_after_year1; earlier_snapshot_not_invented'}
                self.save()
        if 'inheritance' not in nodes and year >= 2 and (
                self.state['inheritance_seen'] or year > 2 or month >= 5):
            return 'inheritance'
        if self._cut_handoff_required() and (
                year > 2 or (year == 2 and (month > 6 or month == 6 and half == 'second'))):
            return 'handoff'
        return None

    def _notice(self, node, entry):
        if node in self.state['notifications']:
            return
        # Persist before external I/O. A notification is never a purchase retry.
        self.state['notifications'][node] = {'status': 'attempted', 'at': time.time()}
        self.save()
        result = {'status': 'not_configured'}
        if self.config['notify'] and self.notify is not None:
            try:
                result = self.notify(node, deepcopy(entry))
            except Exception:
                result = {'status': 'failed'}
        self.state['notifications'][node].update(result)
        self.save()

    def _hold(self, node, entry):
        control = current()
        if control is None:
            self.state['pending'] = {'node': node, 'resume_epoch': 0}
            self.save()
            self._notice(node, entry)
            return {'status': 'stopped', 'stop_reason': 'continuation_hold', 'node': node}
        self.state['pending'] = {'node': node, 'resume_epoch': control.epoch}
        control.note = {'kind': 'continuation_handoff' if node == 'handoff' else 'continuation_gate',
                        'node': node, 'result': entry['result']}
        self.save()
        control.request('pause')
        control.checkpoint('cut_before_turn', on_paused=lambda _snapshot: self._notice(node, entry))
        # In a panel, current-page resume normally unwinds this stack instead.
        return self._resume_pending() or {'status': 'reobserve'}

    def before_home(self, source, provider, output):
        if provider.get('larc_period', {}).get('status') == 'verified':
            self.sync_career_flow(provider['larc_period'].get('value', {}))
        resumed = self._resume_pending()
        if resumed:
            return resumed
        if (self._cut_handoff_required()
                and self.state['nodes'].get('handoff', {}).get('action') == 'user_continued'):
            return {'status': 'handed_off', 'stop_reason': 'continuation_handoff'}
        period = provider.get('larc_period', {}).get('value', {})
        node = (self.state.get('pending') or {}).get('node') or self.due(period)
        if not node:
            return {'status': 'not_due'}
        if node in self.state['nodes']:
            entry = self.state['nodes'][node]
            if entry.get('action') == 'needs_cut_check':
                hook = getattr(self.ctx, 'cut_farming_hook', None)
                if hook and hook.last_probe_turn == self.ctx.progress['next_turn']:
                    probe = hook.session.data['attempts'][-1].get('last_probe', {})
                    if probe.get('state') == 'absent' and probe.get('returned'):
                        entry['action'] = 'restart_authorized'
                        self.save()
                        return {'status': 'failed', 'stop_reason': 'continuation_gate_failed', 'result': entry['result']}
                return self._hold(node, entry)
            return self._hold(node, entry) if self.state.get('pending') else {'status': 'not_due'}
        if self.inspect is None:
            from scripts.larc_continuation_collect import inspect_node
            inspector = inspect_node
        else:
            inspector = self.inspect
        collected = inspector(self.ctx, node, source, provider, Path(output), self.config)
        if collected.get('ok') is not True:
            return {'status': 'collection_failed', 'stop_reason': 'continuation_collection_failed',
                    'detail': collected.get('reason')}
        facts = collected['facts']
        result = (evaluate_junior(self.config, facts) if node == 'junior' else
                  evaluate_inheritance(self.config, facts) if node == 'inheritance' else
                  {'node': 'handoff', 'status': 'handoff', 'on_fail': 'pause'})
        entry = {'result': result, 'facts': facts, 'config': deepcopy(self.config),
                 'period': deepcopy(period), 'turn_index': self.ctx.progress['next_turn'], 'at': time.time()}
        self.state['nodes'][node] = entry
        self.save()
        if result['status'] in {'passed', 'observed'}:
            self.sync_career_flow()
            return {'status': 'recollected' if collected.get('visited') else 'passed',
                    'source': collected.get('source', source), 'provider': collected.get('provider', provider),
                    'result': result}
        if result['status'] == 'failed' and result['on_fail'] == 'restart':
            if getattr(self.ctx, 'cut_farming_hook', None) is None and _ace_cycle_bound(self.ctx):
                try:
                    source_file = Path(collected.get('source', source)).resolve(strict=True)
                    in_run = source_file.is_relative_to(Path(self.ctx.root).resolve())
                except (OSError, TypeError, ValueError):
                    source_file, in_run = None, False
                if source_file is not None and in_run and source_file.is_file():
                    turn = self.ctx.progress['next_turn']
                    endpoint = self.ctx.progress.get('device')
                    packet = {
                        'schema_version': 'larc.ace_continuation_failure.v1',
                        'run_id': self.ctx.run_id,
                        'endpoint': endpoint,
                        'turn': turn,
                        'turn_consumed': False,
                        'node': node,
                        'decision': 'screen_out',
                        'known': True,
                        'result': deepcopy(result),
                        'source': {
                            'run_session_id': self.ctx.run_id,
                            'turn_index': turn,
                            'path': str(source_file),
                            'sha256': hashlib.sha256(source_file.read_bytes()).hexdigest(),
                        },
                    }
                    atomic_json(Path(self.ctx.root) / ACE_FAILURE_PACKET, packet)
                    entry['action'] = 'ace_restart_authorized'
                    self.save()
                    return {
                        'status': 'failed',
                        'stop_reason': 'continuation_gate_failed',
                        'result': result,
                        'ace_continuation': packet,
                    }
            if facts.get('cut_state') == 'absent' and getattr(self.ctx, 'cut_farming_hook', None) is not None:
                return {'status': 'failed', 'stop_reason': 'continuation_gate_failed', 'result': result}
            hook = getattr(self.ctx, 'cut_farming_hook', None)
            if hook is not None and facts.get('cut_state') != 'present':
                entry['action'] = 'needs_cut_check'
                hook.pending = True
                hook.pending_reasons.add('continuation_restart')
                self.save()
                return {'status': 'reobserve', 'source': collected.get('source', source)}
        return self._hold(node, entry)
