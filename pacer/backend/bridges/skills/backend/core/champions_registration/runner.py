"""One registration, explicit division, no automatic change or match entry."""
import json
import time
from pathlib import Path
from core.legend_race.runner import LegendRaceRunner
from core.champions_registration.observer import DIVISIONS
from core.champions_registration.resume import SUBMISSION_TRANSIENT_PAGES

class ChampionsRegistrationRunner(LegendRaceRunner):

    def __init__(self, device, emit, stop_event, *, prepare_entry=None, preserve_existing=False):
        super().__init__(device, emit, stop_event)
        self.prepare_entry = prepare_entry
        self.preserve_existing = preserve_existing is True
        self.registration = None
        self.already_registered = False
        self.resume_from = None

    def _acquire(self):
        if self.resume_from is None:
            return super()._acquire()
        from core.champions_registration.resume import load_pending
        self.pending = True
        if getattr(self, 'restart_verified', False):
            from core.champions_registration.resume import load_restart_pending
            journal, actions = load_restart_pending(self.resume_from, self.device.endpoint, self.device.division)
        else:
            journal, actions = load_pending(self.resume_from, self.device.endpoint, self.device.division)
        self.output = self.device.output_dir = Path(self.resume_from).resolve()
        self.journal = self.output.parent / 'active.json'
        self.event = self.device.event = journal['event']
        self.actions = actions
        self.stage = journal['stage']
        self.owns_journal = True

    def _remember_event(self):
        period = self.current.get('event_period')
        if period:
            if self.event and self.event != {'period': period}:
                raise RuntimeError('champions_event_changed')
            self.event = self.device.event = {'period': period}

    def _journal_data(self):
        return dict(super()._journal_data(), division=self.device.division, registration=self.registration, preserve_existing=self.preserve_existing, already_registered=self.already_registered)

    def _act(self, action):
        self._check_stop()
        if action not in self.current.get('actions', {}):
            raise RuntimeError('action_unavailable:' + action)
        self.stage = action
        self._persist()
        self.emit(dict(type='progress', phase='大赛报名：' + DIVISIONS[self.device.division][0], message=action))
        self.previous_page = self.current.get('page')
        receipt = self._step_with_reobserve(action)
        if receipt is None:
            return
        self.current = receipt['observation']
        self.actions += 1
        self._remember_event()

    def _run(self):
        if self.device.division not in DIVISIONS:
            raise ValueError('请先选择大赛报名组别：中级或高级')
        if self.resume_from is not None:
            rewards_closed = (self.output / 'close_registration_rewards-submitted.json').exists()
            self._capture()
            if getattr(self, 'restart_verified', False) and self.current.get('page') == 'home':
                for action, pages in [('open_races', {'races'}), ('open_events', {'events'}), ('open_champions', {'champions_registered', 'champions_rewards', 'champions_choose'})]:
                    self._act(action)
                    self._wait(pages, 'restart_registration_navigation_unverified')
                if self.current.get('page') == 'champions_choose':
                    raise RuntimeError('restart_registration_not_confirmed')
            self._wait({'champions_registered'} if rewards_closed else {'champions_rewards', 'champions_registered'}, 'champions_resume_page_unverified', transient_pages=() if rewards_closed else SUBMISSION_TRANSIENT_PAGES)
            if self.current['page'] == 'champions_rewards':
                self._act('close_registration_rewards')
                self._wait({'champions_registered'}, 'champions_registration_unverified')
            return self._finish_registration()
        if self.prepare_entry:
            self.prepare_entry()
        self._capture()
        self._wait({'home', 'races', 'events', 'champions_choose', 'champions_registered'}, 'champions_page_unverified')
        if self.current['page'] == 'champions_choose':
            self._act('close_league_choice')
            self._wait({'home', 'races', 'events', 'champions_registered'}, 'champions_existing_state_unverified')
        routes = {'home': ('open_races', {'races'}), 'races': ('open_events', {'events'}), 'events': ('open_champions', {'champions_choose', 'champions_registered'})}
        for _ in range(3):
            route = routes.get(self.current.get('page'))
            if not route:
                break
            self._act(route[0])
            self._wait(route[1], 'champions_entry_unverified')
        self.already_registered = self.current.get('page') == 'champions_registered'
        if self.current.get('page') == 'champions_choose':
            self._act('choose_' + self.device.division)
            self._wait({'champions_confirm'}, 'champions_confirmation_missing')
            if self.current.get('division') != self.device.division:
                raise RuntimeError('champions_wrong_division')
            self.pending = True
            self._act('confirm_registration')
            self._wait({'champions_rewards', 'champions_registered'}, 'champions_registration_unverified', transient_pages=SUBMISSION_TRANSIENT_PAGES)
            if self.current['page'] == 'champions_rewards':
                self._act('close_registration_rewards')
                self._wait({'champions_registered'}, 'champions_registration_unverified')
        return self._finish_registration()

    def _finish_registration(self):
        deadline = time.monotonic() + 60
        missing_home_reads = 0
        missing_identity_reads = 0
        for _ in range(30):
            self._check_stop()
            page = self.current.get('page')
            if page == 'champions_registered':
                if not self._verify_registration():
                    missing_identity_reads += 1
                    if missing_identity_reads >= 15:
                        raise RuntimeError('champions_registration_identity_unavailable')
                else:
                    if 'registration_home' in self.current.get('actions', {}):
                        break
                    missing_home_reads += 1
                    if missing_home_reads >= 15:
                        raise RuntimeError('champions_home_control_unavailable')
            elif page not in {'unknown', 'loading'}:
                raise RuntimeError('champions_post_registration_unexpected_page:' + str(page))
            if time.monotonic() >= deadline:
                raise RuntimeError('champions_post_registration_transition_timeout')
            if self.stop.wait(1):
                raise RuntimeError('operator_stop')
            self._capture()
        else:
            raise RuntimeError('champions_post_registration_transition_timeout')
        self.registration = dict(division=self.device.division, period=(self.event or {}).get('period'), identity_basis='visible_registered_league', frame_sha256=self.current['frame_sha256'], endpoint=self.current.get('endpoint'), screenshot_path=self.current.get('screenshot_path'))
        self._act('registration_home')
        self._wait({'home'}, 'home_not_observed')
        self.pending = False
        return 'champions_registration_home_verified'

    def _verify_registration(self):
        division, period = (self.current.get('division'), self.current.get('event_period'))
        if self.preserve_existing and self.already_registered and (division in DIVISIONS):
            self.device.division = division
        if self.current.get('page') != 'champions_registered' or (division is not None and division != self.device.division) or (period is not None and self.event and (self.event.get('period') is not None) and (period != self.event['period'])):
            raise RuntimeError('大赛报名组别未确认或与设置不同，不自动更改联赛')
        return bool(division)

    def run(self, *, resume_from=None, restart_verified=False):
        self.restart_verified = restart_verified
        self.resume_from = resume_from
        result = super().run()
        result.update(kind='champions_registration', registration=self.registration, already_registered=self.already_registered, last_observation=self.current)
        if resume_from is not None:
            result['resumed_from'] = str(Path(resume_from).resolve())
        if self.owns_journal:
            (self.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
        return result
