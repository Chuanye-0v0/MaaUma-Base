"""Finish the observed daily tickets, with the requested opponent bound first."""
import time
import json
from core.location_navigation import navigate_runner
from pathlib import Path
from core.legend_race.runner import LegendRaceRunner
from core.action_recovery import ReobserveRequired, ReobserveBudget

class _RaceReconciled(Exception):
    """Unwind the old result-page flow after reconnecting at home."""

class DailyLegendRunner(LegendRaceRunner):
    settlement_pages = {'legend', 'daily_legend_complete'}

    def __init__(self, device, emit, stop_event, *, recover_connection=None, prepare_entry=None):
        super().__init__(device, emit, stop_event)
        self.recover_connection = recover_connection
        self.connection_recovered = False
        self.prepare_entry = prepare_entry
        self.resumed_playback = False

    def _act(self, action):
        source_page = self.current.get('page')
        budget = ReobserveBudget(limit=3, seconds=30)
        while True:
            try:
                return super()._act(action)
            except ReobserveRequired as error:
                outcome = getattr(error, 'action_outcome', {})
                if action == 'close_legend_help' or error.observation.get('page') != 'daily_legend_help' or outcome.get('execution_status') != 'not_executed' or (outcome.get('input_attempts') != 0) or (outcome.get('acknowledged_inputs') != 0):
                    raise
                self.current = budget.accept(error)
                super()._act('close_legend_help')
                self._wait({source_page}, 'legend_help_return_unverified')

    def _resume_sale_return(self):
        directory = self.device.output_dir
        if self.current.get('page') != 'rewards' or (directory / 'continue_sale_rewards-submitted.json').exists():
            return
        paths = sorted(directory.glob('*-receipt.json'))
        if not paths:
            return
        last = json.loads(paths[-1].read_text(encoding='utf8'))
        after = last.get('observation', {})
        if last.get('action_id') == 'cancel_limited_sale' and after.get('page') == 'rewards' and (after.get('endpoint') == self.device.endpoint):
            self._act('continue_sale_rewards')

    def _acquire(self):
        if not getattr(self, 'resume_from', None):
            return super()._acquire()
        from core.daily_legend.resume import load_pending
        self.pending = True
        from core.daily_legend.resume import load_unsubmitted_entry
        unsubmitted = load_unsubmitted_entry(self.resume_from, self.device.endpoint, self.device.target)
        self.resume_entry_unsubmitted = unsubmitted is not None
        if unsubmitted:
            journal, race = unsubmitted
            self.resume_outcome, self.resume_rewards_submitted = (None, False)
        else:
            journal, race, self.resume_outcome, self.resume_rewards_submitted = load_pending(self.resume_from, self.device.endpoint, self.device.target['number'])
        if not unsubmitted:
            self.output = Path(self.resume_from).resolve()
            self.journal = self.output.parent / 'active.json'
        self.event = self.device.event = journal['event']
        self.seen_event = self.event.copy()
        self.device.horse = journal['horse']
        self.device.target_verified = True
        self.device.pending_resume_verified = True
        self.initial_tickets, self.remaining = (journal['tickets_before'], journal['tickets_after'])
        self.races, self.stage = (journal['races'], journal['stage'])
        if unsubmitted:
            self._acquire_unsubmitted_attempt()
        else:
            self.device.output_dir = race
            self.owns_journal = True

    def _acquire_unsubmitted_attempt(self):
        """Keep the source proof untouched; later entry belongs to this output."""
        source = Path(self.resume_from).resolve()
        self.output = self.output.resolve()
        if self.output == source or self.journal.resolve() == source.parent / 'active.json':
            raise RuntimeError('unsubmitted_entry_new_output_required')
        self.unsubmitted_entry_source = self.device.unsubmitted_entry_source = source
        self.stage = 'resume_unsubmitted_entry'
        LegendRaceRunner._acquire(self)
        (self.output / 'entry-origin.json').write_text(json.dumps(dict(source=str(source)), ensure_ascii=False, indent=2), encoding='utf8')
        self.device.output_dir = self.output / f'race-{len(self.races) + 1:03d}'
        self.device.output_dir.mkdir(exist_ok=True)
        (self.output / 'context.json').write_text(json.dumps(dict(target=self.device.target, event=self.event, target_verified=self.device.target_verified), ensure_ascii=False, indent=2), encoding='utf8')

    def _resume_unsubmitted_entry(self):
        if self.current.get('page') == 'items':
            self._enter_from_items()
            self._settle()
            return
        if self.current.get('page') not in {'home', 'races', 'events', 'legend', 'details', 'selection', 'daily_legend_list', 'daily_legend_complete'}:
            raise RuntimeError('unsubmitted_entry_location_changed')
        self.device.horse = None
        self.device.pending_resume_verified = False
        self.resumed_playback = False
        self.pending = False
        if self.current.get('page') not in {'details', 'selection'}:
            self._navigate()
        if self.current.get('page') in {'legend', 'daily_legend_list', 'daily_legend_complete'}:
            self._read_tickets()
        if self.remaining == 0:
            return
        self._choose()
        self._enter()
        self._settle()

    def _wait(self, pages, reason, race=False, transient_pages=()):
        if set(pages) == self.settlement_pages:
            transient_pages = set(transient_pages) | {'home', 'events'}
        overlays = {'daily_legend_help': 'close_legend_help', 'limited_sale': 'cancel_limited_sale'}
        for _ in range(2):
            super()._wait(set(pages) | set(overlays) | {'connection_interrupted'}, reason, race, transient_pages)
            if self.current.get('page') == 'connection_interrupted':
                if self.recover_connection is None or self.connection_recovered:
                    raise RuntimeError('session_recovery_unavailable')
                if not self._recover_session(self.current):
                    raise RuntimeError('session_recovery_unverified')
                continue
            action = overlays.get(self.current.get('page'))
            if action is None:
                return
            self._act(action)
            if action == 'cancel_limited_sale':
                super()._wait(set(pages) | {'rewards'}, reason, race, transient_pages)
                if self.current.get('page') == 'rewards':
                    self._act('continue_sale_rewards')
        super()._wait(pages, reason, race, transient_pages)

    def _recover_session(self, observed=None):
        recovery = self.recover_connection(observed)
        if not recovery or recovery.get('ok') is not True:
            return False
        self.connection_recovered = True
        self._capture()
        if self.pending and self.current.get('page') == 'race_resume':
            self._resume_inflight()
            raise _RaceReconciled()
        if self.pending and self.current.get('page') == 'home':
            self._reconcile_reconnected_race()
            raise _RaceReconciled()
        return True

    def _reconcile_reconnected_race(self):
        self._navigate_list('reconnected_race_navigation_unverified', pending=True)
        if self.current.get('page') == 'race_resume':
            self._resume_inflight()
            return
        tickets = self.current.get('tickets')
        if type(tickets) is not int or tickets != self.remaining - 1:
            raise RuntimeError('reconnected_race_ticket_state_unverified')
        if tickets:
            for _ in range(24):
                self._wait_list_ready()
                if 'choose_opponent' in self.current.get('actions', {}):
                    break
                self._act('scroll_opponents')
                self._wait({'daily_legend_list'}, 'daily_legend_list_not_observed')
            else:
                raise RuntimeError('daily_legend_scroll_limit')
            self._act('choose_opponent')
            self._wait({'legend'}, 'reconnected_race_opponent_unverified')
            self.device.target_verified = True
        self._record_settlement('unknown_after_reconnect')

    def _enter(self):
        self._act('confirm_selected')
        self._wait({'lineup'}, 'lineup_not_observed')
        self._enter_from_lineup()

    def _enter_from_lineup(self):
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
        self.device.pending_resume_verified = True
        self._play_race()

    def _playback_action(self, action):
        return 'resume_' + action if self.resumed_playback else action

    def _resume_inflight(self):
        self._act('resume_race')
        self.resumed_playback = True
        self._wait({'race_ready', 'orientation', 'race_grid', 'animation', 'rank_overlay', 'victory', 'defeat', 'trophy', 'rewards'}, 'daily_legend_resume_page_unverified')
        if self.current['page'] == 'race_ready':
            self._play_race()
        else:
            self._continue_playback()
        self._settle(getattr(self, 'resume_outcome', None))

    def _play_race(self):
        results = {'rank_overlay', 'victory', 'defeat', 'trophy', 'rewards'}
        if self._play_ready_action(resumed=self.resumed_playback) == 'view_result':
            self._wait(results, 'race_result_unknown', race=True)
            return
        self._wait({'orientation', 'race_grid', 'animation'} | results, 'race_grid_not_observed')
        self._continue_playback()

    def _continue_playback(self):
        results = {'rank_overlay', 'victory', 'defeat', 'trophy', 'rewards'}
        if self.current['page'] == 'orientation':
            if not self.current.get('portrait_selected'):
                self._act('select_portrait')
                self._wait({'orientation'}, 'orientation_not_observed')
            self._act('confirm_portrait')
            self._wait({'race_grid', 'animation'} | results, 'race_grid_not_observed', transient_pages={'race_ready'})
        self._finish_playback()

    def _finish_playback(self):
        results = {'rank_overlay', 'victory', 'defeat', 'trophy', 'rewards'}
        if self.current['page'] == 'race_grid':
            self._act(self._playback_action('run_race'))
            self._wait({'animation'} | results, 'race_animation_not_observed', race=True)
        for _ in range(20):
            if self.current['page'] in results:
                return
            if 'skip_race' in self.current.get('actions', {}):
                self._act('skip_race')
            else:
                if self.stop.wait(0.5):
                    raise RuntimeError('operator_stop')
                self._capture()
            self._wait({'animation'} | results, 'race_result_unknown', race=True)
        raise RuntimeError('race_result_unknown')

    def _settle(self, outcome=None):
        if self.current.get('page') == 'rank_overlay':
            self._act('dismiss_rank')
            self._wait({'victory', 'defeat', 'trophy', 'rewards'}, 'rank_overlay_not_closed', race=True)
        super()._settle(outcome)

    def _remember_event(self):
        if self.event and self.current.get('page') in {'selection', 'sort', 'lineup', 'items'}:
            return
        if self._bound_result() or (self.pending and self.event and self.device.horse and (self.current.get('page') in {'race_ready', 'race_grid'})):
            return
        visible = self.current.get('event', {}).get('opponent')
        if visible and visible != self.device.target['name']:
            raise RuntimeError('daily_legend_wrong_opponent')
        super()._remember_event()

    def _wait_list_ready(self):
        deadline = time.monotonic() + 30
        previous = None
        for attempt in range(31):
            self._check_stop()
            page = self.current.get('page')
            if page == 'daily_legend_help':
                self._wait({'daily_legend_list'}, 'daily_legend_list_not_ready')
                previous = None
                page = self.current.get('page')
            if page not in ('daily_legend_list', 'unknown', 'loading', 'legend'):
                raise RuntimeError('unexpected_page:' + str(page))
            actions = self.current.get('actions', {})
            action = next((name for name in ('choose_opponent', 'scroll_opponents') if page == 'daily_legend_list' and name in actions), None)
            ready = (action, actions[action]) if action else None
            if time.monotonic() >= deadline:
                break
            if ready is not None and ready == previous:
                return
            previous = ready
            if attempt == 30:
                break
            if attempt == 0:
                self.emit(dict(type='progress', message='等待传奇名单加载及定位稳定，暂不点击或滑动'))
            if self.stop.wait(min(1, max(0, deadline - time.monotonic()))):
                raise RuntimeError('operator_stop')
            self._capture()
        raise RuntimeError('daily_legend_list_not_ready')

    def _navigate_list(self, reason, *, pending=False):
        routes = dict(home='open_races', races='open_events', events='open_legend', legend='back_to_opponents')
        targets = {'daily_legend_list', 'daily_legend_complete'}
        if pending:
            targets.add('race_resume')
        interrupts = {'daily_legend_help', 'limited_sale', 'connection_interrupted'}
        for _ in range(3):
            navigate_runner(self, routes, targets | interrupts, reason)
            if self.current.get('page') in targets:
                return
            self._wait(set(routes) | targets, reason)
        raise RuntimeError(reason)

    def _navigate(self):
        if self.current.get('page') == 'daily_legend_complete':
            self._read_tickets()
            return
        self._navigate_list('daily_legend_navigation_not_observed')
        self._read_tickets()
        if self.remaining == 0:
            return
        for _ in range(24):
            self._wait_list_ready()
            self._read_tickets()
            if 'choose_opponent' in self.current.get('actions', {}):
                self.emit(dict(type='progress', message=f"已定位第 {self.device.target['number']} 号：{self.device.target['label']}"))
                self._act('choose_opponent')
                self._wait({'legend'}, 'daily_legend_opponent_not_observed')
                self.device.target_verified = True
                return
            self._act('scroll_opponents')
            self._wait({'daily_legend_list'}, 'daily_legend_list_not_observed')
        raise RuntimeError('daily_legend_scroll_limit')

    def _prepare_run(self):
        self._capture()
        if getattr(self, 'resume_entry_unsubmitted', False):
            if self.prepare_entry is not None:
                from core.task_entry import ORDINARY_ENTRY_PAGES
                if self.current.get('page') in {'unknown', 'loading'} | ORDINARY_ENTRY_PAGES:
                    self.prepare_entry()
                    self._capture()
            self._resume_unsubmitted_entry()
            return
        if getattr(self, 'resume_from', None) and self.recover_connection is not None and (self.current.get('page') in {'unknown', 'loading', 'connection_interrupted', 'title', 'recent'}):
            self._recover_session()
        if self.prepare_entry is not None:
            self.prepare_entry()
            self._capture()
        if getattr(self, 'resume_from', None):
            if self.resume_rewards_submitted and self.current.get('page') in {'victory', 'defeat', 'rewards', 'unknown', 'loading', *self.settlement_pages}:
                self._resume_sale_return()
                self._wait(self.settlement_pages, 'settlement_not_observed', transient_pages={'defeat' if self.resume_outcome == 'draw' else self.resume_outcome, 'rewards'})
                self._record_settlement(self.resume_outcome)
                raise _RaceReconciled()
            if self.current.get('page') == 'race_resume':
                self._resume_inflight()
                raise _RaceReconciled()
            if getattr(self.device, 'resumed_playback_bound', lambda: False)():
                self.resumed_playback = True
                self._wait({'race_ready', 'orientation', 'race_grid', 'animation', 'rank_overlay', 'victory', 'defeat', 'trophy', 'rewards', 'home', 'races', 'events', 'daily_legend_list', 'daily_legend_complete'}, 'daily_legend_resume_page_unverified')
                if self.current['page'] in {'race_ready', 'orientation', 'race_grid', 'animation'}:
                    if self.current['page'] == 'race_ready':
                        self._play_race()
                    else:
                        self._continue_playback()
                    self._settle(self.resume_outcome)
                    raise _RaceReconciled()
            if self.current.get('page') in {'home', 'races', 'events', 'daily_legend_list', 'daily_legend_complete'}:
                self._reconcile_reconnected_race()
                raise _RaceReconciled()
            self._wait({'race_ready', 'rank_overlay', 'victory', 'defeat', 'trophy', 'rewards'}, 'daily_legend_resume_page_unverified', transient_pages={'lineup'})
            if self.current['page'] == 'race_ready':
                if 'view_result' not in self.current.get('actions', {}):
                    if self.current.get('race_horse') != self.device.horse['name'] or self.current.get('event', {}).get('course') != self.event['course'] or any(((self.device.output_dir / (a + '-submitted.json')).exists() for a in ('start_race', 'view_result', 'run_race'))):
                        raise RuntimeError('daily_legend_resume_result_not_ready')
                    self._play_race()
                    self._settle(self.resume_outcome)
                    return
                self._act('view_result')
                self._wait({'rank_overlay', 'victory', 'defeat', 'trophy', 'rewards'}, 'race_result_unknown', race=True)
            self._settle(self.resume_outcome)
        else:
            self._wait({'home', 'races', 'events', 'legend', 'daily_legend_list', 'daily_legend_complete'}, 'page_unverified')
            self._navigate()

    def _return_home(self):
        if self.current.get('page') == 'daily_legend_complete' and self.remaining == 0 and (not self.pending) and ('return_home' not in self.current.get('actions', {})):
            for _ in range(6):
                if self.stop.wait(0.3):
                    raise RuntimeError('operator_stop')
                self._capture()
                page = self.current.get('page')
                if page == 'home':
                    return
                if page == 'daily_legend_complete' and 'return_home' in self.current.get('actions', {}):
                    break
                if page not in {'daily_legend_complete', 'events', 'unknown', 'loading'}:
                    raise RuntimeError('unexpected_page:' + str(page))
            if self.current.get('page') != 'daily_legend_complete':
                raise RuntimeError('action_unavailable:return_home')
        self._act('return_home')

    def _run(self):
        try:
            self._prepare_run()
        except _RaceReconciled:
            pass
        if self.remaining == 0:
            self._return_home()
            self._wait({'home'}, 'home_not_observed')
            return 'all_remaining_finished' if self.races else 'no_remaining_tickets'
        while self.remaining:
            self._check_stop()
            self.device.output_dir = self.output / f'race-{len(self.races) + 1:03d}'
            self.device.output_dir.mkdir(exist_ok=True)
            self.device.horse = None
            self.resumed_playback = False
            self.device.pending_resume_verified = False
            try:
                self._choose()
                self._enter()
                self._settle()
            except _RaceReconciled:
                pass
            self.emit(dict(type='progress', message=f"{self.device.target['label']}：已结算 {len(self.races)} 场，剩余 {self.remaining} 次"))
        self._return_home()
        self._wait({'home'}, 'home_not_observed')
        return 'all_remaining_finished'

    def run(self, resume_from=None):
        self.resume_from = resume_from
        result = super().run()
        result.update(kind='daily_legend', opponent=self.device.target, last_observation=self.current)
        if resume_from is not None:
            result.update(legend_output_dir=str(self.output.resolve()), resumed_from=str(resume_from))
        return result
