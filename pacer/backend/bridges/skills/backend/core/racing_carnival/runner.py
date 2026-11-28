"""Bounded Racing Carnival loop; original journals own all continuation."""
from copy import deepcopy
from pathlib import Path
import time

from core.action_recovery import ReobserveRequired, ReobserveBudget
from core.legend_selection.runner import LegendSelectionRunner

from .journal import atomic_write, game_day
from .options import normalize_options
from .policy import choose


class Runner:
    def __init__(self, device, journal, emit, stop, *, options=None, clock=time.monotonic, screenshots=None, day_clock=None):
        self.device = device
        self.journal = journal
        self.emit = emit
        self.stop = stop
        self.options = normalize_options(options)
        self.clock = clock
        device.journal = journal
        device.options = self.options
        self.current = {}
        self.wait_started = None
        self.wait_reads = 0
        self.screenshots = screenshots
        self.day_clock = day_clock or getattr(device, 'day_clock', game_day)
        self._selection_verified_frame = None

    def consumed_checkpoint(self):
        if (
            self.screenshots is not None
            and not self.journal.pending
            and self.current.get('page') in {'carnival_hub', 'home'}
        ):
            from .storage import consume
            consume(self.journal, self.screenshots, self.current, keep_current=True)

    def result(self, status, reason):
        frame = self.current
        pending = bool(self.journal.pending)
        navigation = (
            status in {'failed', 'cancelled'}
            and bool(self.journal.data.get('context_verified'))
            and frame.get('page') not in {'home', 'races', 'events'}
        )
        pending = pending or navigation
        self.journal.data['navigation_pending'] = bool(navigation)
        if status in {'done', 'skipped'} and (pending or frame.get('page') != 'home'):
            status = 'failed'
            reason = 'racing_carnival_final_home_or_settlement_unverified'
        basis = (
            'daily_race_cap' if self.journal.capped() else
            'available_resources_exhausted' if self.journal.data.get('resources_exhausted') else
            'event_unavailable_verified' if self.journal.data.get('event_unavailable') else
            'already_completed_today' if self.journal.data.get('done') else None
        )
        if status in {'done', 'skipped'} and basis is None:
            status = 'failed'
            reason = 'racing_carnival_goal_not_verified'
        if status in {'done', 'skipped'}:
            self.journal.data['done'] = True
        self.journal.data.update(status=status, reason=reason)
        self.journal.save()
        result = dict(
            kind='racing_carnival',
            status=status,
            reason=reason,
            ok=status in {'done', 'skipped'},
            game_task_complete=status == 'done',
            skip_verified=status == 'skipped',
            skipped=status == 'skipped',
            pending=bool(pending),
            entry_pending=self.journal.pending,
            journal_path=str(self.journal.path),
            scope=deepcopy(self.journal.data['scope']),
            last_observation=deepcopy(frame),
            output_dir=str(self.device.output_dir),
            effective_options=deepcopy(self.options),
            records=deepcopy(self.journal.data['records']),
            races=deepcopy(self.journal.data.get('races', [])),
        )
        result['completion_basis'] = basis
        if (
            reason == 'racing_carnival_game_day_changed_checkpoint_yield'
            and not pending
            and frame.get('page') == 'home'
            and self.journal.data.get('day_changed')
        ):
            result['requirement_state'] = 'settled_unmet'
        for field in [
            'races_reserved', 'races_submitted', 'races_completed',
            'challenge_reserved', 'challenge_submitted', 'challenge_completed',
            'jelly_reserved', 'jelly_confirmed', 'claims_confirmed',
        ]:
            result[field] = self.journal.data[field]
        path = Path(self.device.output_dir) / 'racing-carnival-result.json'
        atomic_write(path, result)
        return result

    def capture(self):
        if self.stop.is_set():
            raise RuntimeError('operator_stop')
        self.current = self.device.capture()
        self.journal.data['day_changed'] = self.day_clock() != self.journal.data['scope']['day']
        self.journal.observe(self.current)
        return self.current

    def _run_shared_selection(self):
        sel_runner = LegendSelectionRunner(self.device, self.emit, self.stop)
        outcome = sel_runner.run()
        if sel_runner.current:
            self.current = sel_runner.current
            self.journal.data['day_changed'] = self.day_clock() != self.journal.data['scope']['day']
            self.journal.observe(self.current)
        if not outcome.get('ok'):
            reason = outcome.get('reason') or 'racing_carnival_selection_failed'
            if reason == 'selection_page_changed' and (self.current or {}).get('page') in {
                'carnival_notice', 'carnival_hub', 'carnival_detail', 'carnival_race_grid', 'carnival_race_end',
                'loading', 'unknown', 'connection_conflict', 'connection_interrupted',
            }:
                return
            raise RuntimeError(reason)
        self.journal.record(
            'selection_verified',
            selected=outcome.get('selected'),
            checked=outcome.get('checked'),
            rejected=outcome.get('rejected'),
            actions=outcome.get('device_actions'),
        )
        self._selection_verified_frame = self.current.get('frame_sha256')
        self.journal.save()

    def run(self):
        budget = ReobserveBudget()
        repeated = {}
        actions = 0
        try:
            self.capture()
            while True:
                if self.stop.is_set():
                    return self.result('cancelled', 'operator_stop')
                page = self.current.get('page')
                if page not in {'selection', 'sort'}:
                    self._selection_verified_frame = None
                has_grid_observation = (
                    'candidates' in self.current or 'grid_geometry_verified' in self.current
                )
                already_verified_horse = (
                    self.current.get('horse') is not None
                    and 'confirm_selected' in self.current.get('actions', {})
                    and (
                        self.current.get('frame_sha256') == self._selection_verified_frame
                        or (
                            self.current.get('at_top') is True
                            and self.current.get('selected') == 0
                        )
                    )
                )
                if (
                    self.journal.data.get('context_verified')
                    and not self.journal.pending
                    and not self.journal.data.get('return_to_hub')
                    and not self.journal.data.get('day_changed')
                    and not self.journal.data.get('resources_exhausted')
                    and not self.journal.capped()
                    and (
                        page == 'sort'
                        or (
                            page == 'selection'
                            and has_grid_observation
                            and not already_verified_horse
                        )
                    )
                ):
                    self._run_shared_selection()
                    repeated.clear()
                    continue
                decision = choose(self.current, self.journal, self.options)
                if decision['kind'] == 'finish':
                    return self.result(decision['status'], decision['reason'])
                if decision['kind'] == 'wait':
                    if self.wait_started is None:
                        self.wait_started = self.clock()
                    self.wait_reads += 1
                    if self.clock() - self.wait_started > 90 or self.wait_reads > 180:
                        raise RuntimeError('racing_carnival_wait_exhausted:' + decision['reason'])
                    if self.stop.wait(.2):
                        return self.result('cancelled', 'operator_stop')
                    self.capture()
                    continue
                self.wait_started = None
                self.wait_reads = 0
                action = decision['action']
                before = self.current
                signature = (before['page'], action, before.get('frame_sha256'))
                repeated[signature] = repeated.get(signature, 0) + 1
                if repeated[signature] > 3:
                    raise RuntimeError('racing_carnival_repeated_action_no_progress')
                actions += 1
                if actions > 1200:
                    raise RuntimeError('racing_carnival_action_budget_exhausted')
                self.journal.data.update(decision.get('updates', {}))
                self.journal.reserve(action, before)
                self.emit(dict(type='progress', phase=before['page'], message='竞速嘉年华：' + action))
                try:
                    receipt = self.device.step(action, self.stop, before)
                except ReobserveRequired as error:
                    self.journal.failed(error)
                    self.current = budget.accept(error)
                    self.journal.observe(self.current)
                    repeated[signature] -= 1
                    continue
                self.journal.receipt(receipt)
                if receipt.get('execution_status') == 'executed':
                    budget.executed()
                self.current = receipt.get('observation') or self.capture()
                outcome = receipt.get('action_outcome') or {}
                if (
                    action == 'open_carnival'
                    and not self.journal.data.get('context_verified')
                    and before.get('actions', {}).get(action, {}).get('basis') == 'named_carnival_event_card'
                    and outcome.get('execution_status') == 'executed'
                    and type(outcome.get('input_attempts')) is int
                    and outcome['input_attempts'] > 0
                    and outcome.get('acknowledged_inputs') == outcome['input_attempts']
                ):
                    self.current = self.capture()
                self.consumed_checkpoint()
                if before.get('page') != self.current.get('page'):
                    repeated.clear()
        except Exception as error:
            self.journal.failed(error)
            return self.result('cancelled' if str(error).startswith('operator_stop') else 'failed', str(error))
