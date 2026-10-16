"""Finish the initially remaining legend tickets, settling every race once."""
import json
import os
from pathlib import Path
import subprocess
import time
from core.legend_race.observer import selected_horse
from core.legend_selection.runner import LegendSelectionRunner
from core.action_recovery import ReobserveRequired, ReobserveBudget, failure_details

class LegendRaceRunner:
    settlement_pages = {'legend'}

    def __init__(self, device, emit, stop_event):
        self.device, self.emit, self.stop = (device, emit, stop_event)
        self.output = Path(device.output_dir)
        self.journal = self.output.parent / 'active.json'
        self.current, self.event, self.seen_event = ({}, None, {})
        self.races = []
        self.initial_tickets = self.remaining = None
        self.actions = 0
        self.pending = self.owns_journal = False
        self.previous_page = None
        self.stage = 'starting'

    def _check_stop(self):
        if self.stop.is_set():
            raise RuntimeError('operator_stop')

    def _journal_data(self):
        return dict(state='pending' if self.pending else 'active', run=str(self.output), endpoint=self.device.endpoint, stage=self.stage, event=self.event, horse=self.device.horse, completed_races=len(self.races), races=self.races, tickets_before=self.initial_tickets, tickets_after=self.remaining)

    def _persist(self):
        temporary = self.output / 'active-update.json'
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(self._journal_data(), stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.journal)

    def _acquire(self):
        self.output.mkdir(parents=True, exist_ok=True)
        try:
            stream = self.journal.open('x', encoding='utf-8')
        except FileExistsError:
            self.pending = True
            raise RuntimeError('unsettled_previous_run') from None
        self.owns_journal = True
        with stream:
            json.dump(self._journal_data(), stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())

    def _remember_event(self):
        if self.event and self.current.get('page') in {'selection', 'sort', 'lineup', 'items'}:
            return
        if self._bound_result() or (self.pending and self.event and self.device.horse and (self.current.get('page') in {'race_ready', 'race_grid'})):
            return
        observed = self.current.get('event') or {}
        for field in ('course', 'opponent'):
            value = observed.get(field)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise RuntimeError('event_unverified')
            if field in self.seen_event and self.seen_event[field] != value:
                raise RuntimeError('event_changed')
            self.seen_event[field] = value
        if self.event is None and all((observed.get(k) for k in ('course', 'opponent'))):
            self.event = {k: observed[k] for k in ('course', 'opponent')}
            self.device.event = self.event

    def _bound_result(self):
        return bool(self.pending and self.event and self.device.horse and (self.current.get('page') in {'victory', 'defeat', 'trophy', 'rewards'}))

    def _capture(self):
        self._check_stop()
        self.current = self.device.capture()
        self._remember_event()

    def _step_with_reobserve(self, action):
        source_page = self.current.get('page')
        budget = ReobserveBudget(limit=3, seconds=30)
        while True:
            self._check_stop()
            try:
                return self.device.step(action, self.stop, self.current)
            except ReobserveRequired as error:
                outcome = getattr(error, 'action_outcome', {})
                fresh = error.observation
                if outcome.get('execution_status') != 'not_executed' or outcome.get('input_attempts') != 0 or outcome.get('acknowledged_inputs') != 0:
                    raise
                destinations = {'open_races': 'races', 'open_events': 'events', 'open_legend': 'legend', 'return_home': 'home'}
                if action in destinations and fresh.get('page') == destinations[action] and (fresh.get('endpoint') == self.device.endpoint):
                    self.current = fresh
                    self._remember_event()
                    return None
                if fresh.get('page') in {'unknown', 'loading', None} or fresh.get('page') != source_page or fresh.get('endpoint') != self.device.endpoint or (action not in fresh.get('actions', {})) or (self.device.output_dir / (action + '-submitted.json')).exists():
                    raise
                self.current = budget.accept(error)
                self._remember_event()

    def _act(self, action):
        self._check_stop()
        if action not in self.current.get('actions', {}):
            raise RuntimeError('action_unavailable:' + action)
        self.stage = action
        self._persist()
        total = self.initial_tickets if self.initial_tickets is not None else '?'
        self.emit(dict(type='progress', phase=f'传奇赛事：已完成 {len(self.races)}/{total}；{action}', message=action))
        self.previous_page = self.current.get('page')
        receipt = self._step_with_reobserve(action)
        if receipt is None:
            return
        self.current = receipt['observation']
        self.actions += 1
        self._remember_event()

    def _wait(self, pages, reason, race=False, transient_pages=()):
        limit = 180 if race else 30
        deadline = time.monotonic() + limit
        for attempt in range(limit):
            self._check_stop()
            page = self.current.get('page')
            if page in pages:
                return
            if page not in ('unknown', 'loading', self.previous_page) and page not in transient_pages:
                raise RuntimeError('unexpected_page:' + str(page))
            if time.monotonic() >= deadline:
                break
            if race and attempt % 10 == 0:
                self.emit(dict(type='progress', phase='传奇赛事：等待比赛结果，超时将停止并保留记录', message='waiting_for_race_result'))
            if self.stop.wait(1):
                raise RuntimeError('operator_stop')
            self._capture()
        raise RuntimeError(reason)

    def _read_tickets(self):
        value = self.current.get('tickets')
        if type(value) is not int or not 0 <= value <= 3:
            raise RuntimeError('tickets_unverified')
        if self.initial_tickets is None:
            self.initial_tickets = value
        self.remaining = value

    def _navigate(self):
        routes = dict(home=('open_races', 'races'), races=('open_events', 'events'), events=('open_legend', 'legend'))
        for _ in range(3):
            route = routes.get(self.current.get('page'))
            if route is None:
                break
            action, target = route
            self._act(action)
            self._wait({target}, 'navigation_not_observed')

    def _choose(self):
        if self.current.get('page') == 'legend':
            self._read_tickets()
            self._act('open_details')
            self._wait({'details'}, 'details_not_observed')
        if self.current.get('page') not in ('details', 'selection'):
            raise RuntimeError('selection_start_unverified')
        selector = LegendSelectionRunner(self.device, self.emit, self.stop)
        result = selector.run()
        self.actions += selector.actions
        self.current = selector.current
        self._remember_event()
        if not result['ok']:
            raise RuntimeError(result['reason'])
        horse = selected_horse(self.current)
        if not horse or horse != result['selected']:
            raise RuntimeError('selected_horse_not_bound')
        if not self.event:
            raise RuntimeError('event_unverified')
        self.device.horse = horse

    def _play_ready_action(self, *, resumed=False):
        """Choose again only when a fresh preview proves zero playback input."""
        prefix = 'resume_' if resumed else ''
        budget = ReobserveBudget(limit=3, seconds=30)
        playback = ('start_race', 'view_result', 'resume_start_race', 'resume_view_result')
        while True:
            self._check_stop()
            action = 'view_result' if prefix + 'view_result' in self.current.get('actions', {}) else 'start_race'
            try:
                self._act(prefix + action)
                return action
            except ReobserveRequired as error:
                outcome = getattr(error, 'action_outcome', {})
                fresh = error.observation
                if outcome.get('execution_status') != 'not_executed' or outcome.get('input_attempts') != 0 or outcome.get('acknowledged_inputs') != 0 or (fresh.get('page') != 'race_ready') or (fresh.get('endpoint') != self.device.endpoint) or (not any((prefix + a in fresh.get('actions', {}) for a in ('start_race', 'view_result')))) or any(((self.device.output_dir / (a + '-submitted.json')).exists() for a in playback)):
                    raise
                self.current = budget.accept(error)

    def _enter(self):
        self._act('confirm_selected')
        self._wait({'lineup'}, 'lineup_not_observed')
        self._act('continue_lineup')
        self._wait({'items'}, 'items_not_observed')
        self._enter_from_items()

    def _enter_from_items(self):
        self._read_tickets()
        if self.remaining == 0 or self.current.get('items_empty') is not True or self.current.get('tickets_after') != self.remaining - 1:
            raise RuntimeError('ticket_cost_unverified')
        self.pending = True
        self._act('enter_race')
        self._wait({'race_ready'}, 'race_ready_not_observed', transient_pages={'lineup'})
        if self._play_ready_action() == 'start_race':
            self._wait({'race_grid'}, 'race_grid_not_observed')
            self._act('run_race')
        self._wait({'victory', 'defeat', 'trophy', 'rewards'}, 'race_result_unknown', race=True)

    def _settle(self, outcome=None):
        seen = set()
        while self.current.get('page') in ('victory', 'defeat', 'trophy', 'rewards'):
            page = self.current['page']
            if page in seen:
                raise RuntimeError('result_page_repeated')
            seen.add(page)
            if page == 'victory':
                if not self._bound_result() or self.current.get('victory') is not True:
                    raise RuntimeError('race_result_unknown')
                outcome = 'victory'
                self._act('continue_victory')
            elif page == 'defeat':
                if not self._bound_result():
                    raise RuntimeError('race_result_unknown')
                outcome = 'draw' if self.current.get('result_outcome') == 'draw' else 'defeat'
                self._act('continue_defeat')
            elif page == 'trophy':
                if not self._bound_result():
                    raise RuntimeError('race_result_unknown')
                outcome = 'victory'
                self._act('close_trophy')
            else:
                if outcome is None:
                    raise RuntimeError('race_result_unknown')
                self._act('continue_rewards')
                self._wait(self.settlement_pages, 'settlement_not_observed', transient_pages={'defeat' if outcome == 'draw' else outcome})
                break
            self._wait({'victory', 'defeat', 'trophy', 'rewards'} - seen, 'race_result_unknown')
        if outcome is None or 'rewards' not in seen or self.current.get('page') not in self.settlement_pages:
            raise RuntimeError('settlement_not_observed')
        self._record_settlement(outcome)

    def _record_settlement(self, outcome):
        for _ in range(6):
            if self.current.get('tickets') is not None:
                break
            if self.stop.wait(0.3):
                raise RuntimeError('operator_stop')
            self._capture()
            self._wait(self.settlement_pages, 'settlement_not_observed')
        after = self.current.get('tickets')
        if type(after) is not int or not 0 <= after <= 3:
            raise RuntimeError('ticket_delta_unverified')
        self.races.append(dict(number=len(self.races) + 1, horse=self.device.horse.copy(), event=self.event.copy(), result=outcome, tickets_before=self.remaining, tickets_after=after, evidence_dir=str(self.device.output_dir)))
        self.remaining = after
        self.pending = False
        self.stage = 'settled'
        self._persist()

    def _run(self):
        self._capture()
        self._wait({'home', 'races', 'events', 'legend', 'details', 'selection'}, 'page_unverified')
        self._navigate()
        if self.current.get('page') == 'legend':
            self._read_tickets()
        if self.remaining == 0:
            self._act('return_home')
            self._wait({'home'}, 'home_not_observed')
            return 'no_remaining_tickets'
        for _ in range(3):
            self._check_stop()
            self.device.output_dir = self.output / f'race-{len(self.races) + 1:03d}'
            self.device.output_dir.mkdir(exist_ok=True)
            self.device.horse = None
            self._choose()
            self._enter()
            self._settle()
            if self.remaining == 0:
                self._act('return_home')
                self._wait({'home'}, 'home_not_observed')
                return 'all_remaining_finished'
        raise RuntimeError('race_limit')

    def run(self):
        complete = False
        failure = {}
        try:
            self._check_stop()
            self._acquire()
            reason = self._run()
            complete = True
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
            reason = str(exc)
            failure = failure_details(exc)
        result = dict(kind='legend_race', ok=complete, complete=complete, reason=reason, **failure, completed_races=len(self.races), races=self.races, tickets_before=self.initial_tickets, tickets_after=self.remaining, pending=self.pending, device_actions=self.actions, items_used=0, refills=0, last_page=self.current.get('page'), screenshot_path=self.current.get('screenshot_path'))
        if self.owns_journal:
            (self.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            if not self.pending:
                self.journal.unlink()
        return result
