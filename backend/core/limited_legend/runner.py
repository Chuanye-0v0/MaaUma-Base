"""Limited tickets and two mission tabs; no ticket purchase or alarm retries."""
from core.legend_race.runner import LegendRaceRunner
from core.daily_legend.runner import DailyLegendRunner
from pathlib import Path
import json

class LimitedLegendRunner(DailyLegendRunner):
    settlement_pages = {'legend'}

    def __init__(self, device, emit, stop_event, *, prepare_entry=None, verify_resume_account=None, skip_when_unavailable=False):
        super().__init__(device, emit, stop_event, prepare_entry=prepare_entry)
        self.missions_checked = []
        self.mission_results = {}
        self.claim_skipped = []
        self.verify_resume_account = verify_resume_account
        self.manual_reconciliation = None
        self.skip_when_unavailable = skip_when_unavailable
        self.unavailable_skipped = False

    def _read_tickets(self):
        if getattr(self, '_checking_manual_completion', False):
            if type(self.current.get('tickets')) is not int or self.current['tickets'] != 0:
                raise RuntimeError('limited_manual_completion_tickets_not_zero')
            return
        return super()._read_tickets()

    def _reconcile_manual_completion(self):
        if self.current['page'] == 'limited_missions':
            self._act('close_missions')
            self._wait({'legend'}, 'limited_missions_not_closed')
        if self.current['page'] == 'legend':
            self._act('return_home')
            self._wait({'home'}, 'home_not_observed')
        if self.verify_resume_account is None or self.verify_resume_account() is not True:
            raise RuntimeError('limited_manual_completion_account_unverified')
        self.emit(dict(type='progress', message='核实人工完成的限时传奇赛：检查剩余券和任务领奖，不重打比赛'))
        self._capture()
        if self.current.get('page') != 'home':
            raise RuntimeError('limited_manual_completion_home_unverified')
        self._navigate()
        if self.current.get('page') != 'legend' or self.current.get('event', {}).get('course') != self.event['course']:
            raise RuntimeError('limited_manual_completion_event_unverified')
        self._checking_manual_completion = True
        self._read_tickets()
        proof = dict(tickets_before=self.remaining, tickets_after=0, externally_completed_attempts=self.remaining, observation=self.current, account_verified=True)
        self._claim_missions()
        self._act('return_home')
        self._wait({'home'}, 'home_not_observed')
        proof['missions_checked'] = list(self.missions_checked)
        (self.output / 'manual-completion.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf8')
        self.manual_reconciliation = proof
        self.remaining = 0
        self.pending = False
        return 'limited_legend_manual_completion_verified'

    def _remember_event(self):
        LegendRaceRunner._remember_event(self)
        if self.event:
            self.device.target = dict(name=self.event['opponent'])
            self.device.target_verified = True

    def _acquire(self):
        if not getattr(self, 'resume_from', None):
            return super()._acquire()
        from core.limited_legend.mission_resume import load_mission_resume
        mission = load_mission_resume(self.resume_from, self.device.endpoint)
        if mission is not None:
            self.mission_resume = True
            self.output = Path(self.resume_from).resolve()
            self.journal = self.output.parent / 'active.json'
            self.event = self.device.event = mission['event']
            self.seen_event = self.event.copy()
            self.races = mission['races']
            self.initial_tickets, self.remaining = (mission['tickets_before'], 0)
            self.mission_tabs_remaining = mission.get('mission_tabs_remaining') is True
            self.missions_checked = list(mission.get('missions_checked', [])) if self.mission_tabs_remaining else ['daily', 'period']
            self.device.output_dir = self.output / 'missions'
            self.pending = True
            self.owns_journal = True
            self.stage = 'mission_resume'
            self._persist()
            return
        from core.daily_legend.resume import load_submitted_race
        self.pending = True
        from core.daily_legend.resume import load_unsubmitted_entry
        unsubmitted = load_unsubmitted_entry(self.resume_from, self.device.endpoint)
        self.resume_entry_unsubmitted = unsubmitted is not None
        if unsubmitted:
            journal, race = unsubmitted
            self.resume_outcome, self.resume_rewards_submitted = (None, False)
        else:
            journal, race, self.resume_outcome, self.resume_rewards_submitted = load_submitted_race(self.resume_from, self.device.endpoint)
        if not unsubmitted:
            self.output = Path(self.resume_from).resolve()
            self.journal = self.output.parent / 'active.json'
        self.event = self.device.event = journal['event']
        self.seen_event = self.event.copy()
        self.device.horse = journal['horse']
        self.device.target = dict(name=self.event['opponent'])
        self.device.target_verified = self.device.pending_resume_verified = True
        self.initial_tickets, self.remaining = (journal['tickets_before'], journal['tickets_after'])
        self.races, self.stage = (journal['races'], journal['stage'])
        if unsubmitted:
            self._acquire_unsubmitted_attempt()
        else:
            self.device.output_dir = race
            self.owns_journal = True

    def _navigate(self):
        from core.event_menu import unavailable
        if not self.skip_when_unavailable:
            return LegendRaceRunner._navigate(self)
        for _ in range(3):
            page = self.current.get('page')
            if page == 'events' and (not self.pending) and (not self.races) and (not getattr(self, 'resume_from', None)) and unavailable(self.current, 'legend', self.device.endpoint):
                confirmed = True
                for _ in range(2):
                    if self.stop.wait(0.25):
                        raise RuntimeError('operator_stop')
                    self._capture()
                    if not unavailable(self.current, 'legend', self.device.endpoint):
                        confirmed = False
                        break
                if confirmed:
                    self.unavailable_skipped = True
                    return
            route = {'home': ('open_races', 'races'), 'races': ('open_events', 'events'), 'events': ('open_legend', 'legend')}.get(self.current.get('page'))
            if route is None:
                break
            self._act(route[0])
            self._wait({route[1]}, 'navigation_not_observed')

    def _choose(self):
        if self.current.get('page') == 'legend':
            self._read_tickets()
            self._act('open_details')
            self._wait({'details', 'selection'}, 'selection_start_unverified')
        LegendRaceRunner._choose(self)

    def _missions_ready(self, tab):
        for attempt in range(4):
            self._wait({'limited_missions'}, 'limited_missions_not_observed')
            if self.current.get('mission_tab') == tab and self.current.get('mission_state_verified') is True and (self.current.get('claim_present') is True) and (type(self.current.get('claim_enabled')) is bool) and (self.current.get('claim_enabled') is True or self.current.get('claimable_rows') == 0):
                return
            if attempt == 3:
                break
            if self.stop.wait(0.4):
                raise RuntimeError('operator_stop')
            self._capture()
        if self.current.get('mission_tab') != tab:
            raise RuntimeError('limited_mission_tab_unverified')
        reason = 'claim_button_missing' if self.current.get('claim_present') is False else 'claim_state_unverified'
        self.claim_skipped.append(dict(tab=tab, reason=reason))
        raise RuntimeError('limited_' + reason)

    def _claim_missions(self):
        self.device.output_dir = self.output / 'missions'
        self.device.output_dir.mkdir(exist_ok=True)
        if self.current.get('page') != 'limited_missions':
            for _ in range(12):
                if 'open_missions' in self.current.get('actions', {}):
                    break
                if self.stop.wait(0.4):
                    raise RuntimeError('operator_stop')
                self._capture()
            self._act('open_missions')
            self._wait({'limited_missions'}, 'limited_missions_not_observed')
        for tab in ('daily', 'period'):
            if self.current.get('mission_tab') != tab:
                self._act('missions_' + tab)
            self._missions_ready(tab)
            if self.current['claim_enabled'] is True:
                from core.passive_popup_runtime import owned_reward
                with owned_reward(self.device, 'limited_mission_rewards'):
                    self._act('claim_' + tab)
                    self._wait({'limited_mission_rewards'}, 'limited_claim_receipt_missing')
                    self.mission_results[tab] = dict(status='claimed_pending_reconciliation', receipt_page=self.current['page'], receipt_frame_sha256=self.current.get('frame_sha256'))
                    self._act('close_mission_rewards')
                self._wait({'limited_missions'}, 'limited_missions_not_observed')
                for _ in range(4):
                    self._missions_ready(tab)
                    if self.current['claim_enabled'] is False:
                        break
                    if self.stop.wait(0.4):
                        raise RuntimeError('operator_stop')
                    self._capture()
                else:
                    raise RuntimeError('limited_claim_not_reconciled')
                self.mission_results[tab]['status'] = 'claimed'
            elif self.current['claim_enabled'] is False:
                self.mission_results[tab] = dict(status=self.current.get('reward_state') or 'no_claimable_rewards', row_states=self.current.get('mission_row_states', []))
            if tab not in self.missions_checked:
                self.missions_checked.append(tab)
        self._act('close_missions')
        self._wait({'legend'}, 'limited_missions_not_closed')
        self._read_tickets()

    def _run(self):
        if self.prepare_entry and (not getattr(self, 'resume_from', None)):
            self.prepare_entry()
        self._capture()
        if self.prepare_entry and getattr(self, 'resume_from', None):
            from core.task_entry import ORDINARY_ENTRY_PAGES
            if self.current.get('page') in {'unknown', 'loading'} | ORDINARY_ENTRY_PAGES:
                self.prepare_entry()
                self._capture()
        if getattr(self, 'mission_resume', False):
            if getattr(self, 'mission_tabs_remaining', False):
                if self.current.get('page') in {'home', 'races', 'events'}:
                    if self.verify_resume_account is None or self.verify_resume_account() is not True:
                        raise RuntimeError('limited_mission_resume_account_unverified')
                    self._capture()
                    self._navigate()
                self._wait({'limited_missions', 'legend'}, 'limited_mission_resume_page_unverified')
                self._claim_missions()
                self._act('return_home')
                self._wait({'home'}, 'home_not_observed')
                self.pending = False
                return 'limited_legend_home_verified'
            self._missions_ready('period')
            if self.current.get('claim_enabled') is not False:
                raise RuntimeError('limited_claim_resume_state_changed')
            self._act('close_missions')
            self._wait({'legend'}, 'limited_missions_not_closed')
            self._read_tickets()
            if self.remaining != 0:
                raise RuntimeError('limited_claim_resume_tickets_changed')
            self._act('return_home')
            self._wait({'home'}, 'home_not_observed')
            self.pending = False
            return 'limited_legend_home_verified'
        if getattr(self, 'resume_entry_unsubmitted', False):
            self._resume_unsubmitted_entry()
        elif getattr(self, 'resume_from', None):
            if self.current.get('page') in {'home', 'limited_missions'} or (self.current.get('page') == 'legend' and (not self.resume_rewards_submitted or self.current.get('tickets') != self.remaining - 1)):
                return self._reconcile_manual_completion()
            if self.resume_rewards_submitted:
                self._resume_sale_return()
                self._wait(self.settlement_pages, 'settlement_not_observed', transient_pages={'victory', 'defeat', 'rewards'})
                self._record_settlement(self.resume_outcome)
            else:
                self._wait({'race_ready', 'rank_overlay', 'victory', 'defeat', 'trophy', 'rewards'}, 'limited_legend_resume_result_unverified', transient_pages={'lineup'})
                if self.current['page'] == 'race_ready':
                    if self.current.get('race_horse') != self.device.horse['name'] or self.current.get('event', {}).get('course') != self.event['course']:
                        raise RuntimeError('limited_legend_resume_race_not_bound')
                    self._play_race()
                self._settle(self.resume_outcome)
        else:
            self._wait({'home', 'races', 'events', 'legend', 'details', 'selection'}, 'page_unverified')
            self._navigate()
            if self.unavailable_skipped:
                self.emit(dict(type='progress', message='限时传奇：新鲜画面已确认活动入口不可用，按用户授权跳过；未参赛'))
                return 'limited_legend_unavailable_skipped'
        if self.current.get('page') not in {'legend', 'details', 'selection'}:
            raise RuntimeError('limited_event_entry_unverified')
        if self.current['page'] == 'legend':
            self._read_tickets()
        while self.remaining is None or self.remaining:
            self.device.output_dir = self.output / f'race-{len(self.races) + 1:03d}'
            self.device.output_dir.mkdir(exist_ok=True)
            self.device.horse = None
            self._choose()
            self._enter()
            self._settle()
        self._claim_missions()
        self._act('return_home')
        self._wait({'home'}, 'home_not_observed')
        return 'limited_legend_home_verified'

    def run(self, resume_from=None):
        self.resume_from = resume_from
        result = LegendRaceRunner.run(self)
        result.update(kind='limited_legend', event=self.event, missions_checked=self.missions_checked, mission_results=self.mission_results, claim_skipped=self.claim_skipped, last_observation=self.current)
        if self.claim_skipped:
            result.update(complete=False, activity_completed=False)
        if result['ok'] and self.unavailable_skipped:
            result.update(skipped=True, skip_reason='event_unavailable', activity_completed=False, complete=False)
            if self.owns_journal:
                (self.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
        if self.manual_reconciliation is not None:
            result['manual_reconciliation'] = self.manual_reconciliation
        if resume_from is not None:
            result.update(resumed_from=str(Path(resume_from).resolve()), legend_output_dir=str(self.output.resolve()))
        if not result['ok']:
            from core.limited_legend.recovery import navigation_only_failure
            result['pre_entry_verified'] = navigation_only_failure(result, self.output, self.device.endpoint)
            result['pending'] = not result['pre_entry_verified']
            if self.remaining == 0 and self.races and (not self.pending or getattr(self, 'mission_resume', False)):
                result['pending_kind'] = 'mission_cleanup'
        if self.owns_journal:
            (self.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
        return result
