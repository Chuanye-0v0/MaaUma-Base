"""At most four daily five-match rounds, with the original round owner retained."""
import json
import os
import time
from datetime import datetime
from pathlib import Path
from core.action_recovery import failure_details, ReobserveRequired, ReobserveBudget
from core.location_navigation import wait_for_transition, NavigationFailure
from core.champions_race.policy import admission_resource
from core.champions_race.task_restart import round_phase
OWNED_PAGES = {'champions_hub', 'champions_active_hub', 'champions_entry_confirm', 'champions_lineup', 'champions_auto_confirm', 'champions_team_confirm', 'champions_promotion', 'champions_board', 'champions_matching', 'champions_preview', 'champions_outcome', 'champions_standings', 'champions_match_result', 'champions_round_reward', 'champions_claim_receipt'}
TRANSITION_BACKDROPS = {'champions_entry_confirm': {'champions_hub'}, 'champions_auto_confirm': {'champions_lineup'}, 'champions_team_confirm': {'champions_lineup'}, 'champions_round_reward': {'champions_board'}, 'champions_claim_receipt': {'champions_board'}}

class ChampionsRaceRunner:

    def __init__(self, device, emit, stop_event, *, prepare_entry=None, max_rounds=4, allow_final_jewels=False, allow_jewels=False, verify_resume_account=None):
        if type(max_rounds) is not int or max_rounds not in (1, 2, 3, 4):
            raise ValueError('champions_round_limit_invalid')
        if type(allow_final_jewels) is not bool or type(allow_jewels) is not bool:
            raise ValueError('champions_jewel_permission_invalid')
        self.device, self.emit, self.stop = (device, emit, stop_event)
        self.prepare_entry = prepare_entry
        self.max_rounds = max_rounds
        self.allow_final_jewels = allow_final_jewels
        self.verify_resume_account = verify_resume_account
        self.allow_jewels = allow_jewels
        self.output = Path(device.output_dir).resolve()
        self.current = {}
        self.book = {}
        self.owns = False
        self.attempt_output = self.output
        self.restart_task = False
        self.at_task_entry = False
        self.unavailable_skipped = False
        self.resuming = False
        self.device.max_rounds = max_rounds
        self.device.allow_final_jewels = allow_final_jewels
        self.device.allow_jewels = allow_jewels

    def _persist(self):
        self.book.update(run=str(self.output), endpoint=self.device.endpoint, requested_rounds=self.max_rounds, state='pending' if self.book.get('started') else 'active')
        temporary = self.output / 'active-update.json'
        temporary.write_text(json.dumps(self.book, ensure_ascii=False, indent=2), encoding='utf8')
        os.replace(temporary, self.output / 'active.json')
        self.device.context = dict(self.book)
        (self.output / 'context.json').write_text(json.dumps(self.book, ensure_ascii=False, indent=2), encoding='utf8')

    def _capture(self):
        if self.stop.is_set():
            raise RuntimeError('operator_stop')
        self.current = self.device.capture()
        self._check_scope()

    def _check_scope(self):
        division = self.current.get('division')
        if division in {'intermediate', 'advanced'} and self.device.division is None:
            self.device.division = division
            self.book['division'] = division
            if not self.book.get('started'):
                self.device.event['division'] = division
        if self.device.division is not None and self.current.get('division') not in (None, self.device.division):
            raise RuntimeError('champions_race_division_changed')
        active = self.book.get('active_round') or {}
        phase = self.current.get('event_phase')
        prior_phase = round_phase(self.book)
        if phase is not None and self.book.get('event_phase') is None:
            self.book['event_phase'] = phase
        if prior_phase is not None and self.current.get('event_phase') not in (None, prior_phase):
            if self.restart_task and self.at_task_entry and (self.current.get('page') in {'champions_hub', 'champions_active_hub'}):
                self._restart_from_current_entry()
                return
            raise RuntimeError('champions_race_phase_changed')

    def _restart_from_current_entry(self):
        from core.champions_race.task_restart import phase_restart_record
        from core.action_recovery import require_fresh_observation
        if self.stop.is_set():
            raise RuntimeError('operator_stop')
        if self.attempt_output == self.output:
            if self.output.parent.name.isdigit() and self.output.parent.parent.name == 'tasks':
                self.attempt_output = self.output.parent.parent / str(time.time_ns()) / self.output.name
            else:
                raise RuntimeError('champions_new_task_attempt_required')
        require_fresh_observation(self.current, 'open_entry')
        source = self.output
        previous = self.book
        record = phase_restart_record(previous, self.current, source, self.device.endpoint)
        if self.verify_resume_account is None or self.verify_resume_account() is not True:
            raise RuntimeError('champions_task_restart_account_unverified')
        if self.stop.is_set():
            raise RuntimeError('operator_stop')
        self.current = self.device.capture()
        history = [*previous.get('task_history', []), record]
        self.output = self.attempt_output
        self.output.mkdir(parents=True, exist_ok=True)
        self.book = dict(kind='champions_race', run=str(self.output), endpoint=self.device.endpoint, entry_day=datetime.now().date().isoformat(), division=self.device.division, requested_rounds=self.max_rounds, rounds=[], resources_used=dict(free=0, tickets=0, jewels=0), started=False, pending_matches=0, active_round=None, stage='task_entry_replanned', device_actions=0, task_history=history, event_phase=record['current_phase'])
        with (self.output / 'active.json').open('x', encoding='utf8') as stream:
            json.dump(self.book, stream)
        (self.output / 'task-entry-replan.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf8')
        self._scope()
        self._persist()
        self.emit(dict(type='progress', message='大赛阶段已刷新：按当前入口重新执行未完成任务，保留旧记录'))

    def _completed_rounds(self):
        from core.champions_race.task_restart import carried_rounds
        return carried_rounds(self.book, self.device.endpoint) + len(self.book.get('rounds', []))

    def _wait(self, pages, reason, waiting=(), condition=None):
        started = time.monotonic()
        deadline = started + 30
        loading_deadline = started + 90
        waiting_pages = {'unknown', 'loading', 'champions_matching', self.book.get('source_page')} | set(waiting)
        if self.book.get('started'):
            waiting_pages.update(TRANSITION_BACKDROPS.get(self.book.get('source_page'), ()))
        trace = []
        while True:
            if self.stop.is_set():
                raise RuntimeError('operator_stop')
            if self.current.get('endpoint') != self.device.endpoint:
                raise RuntimeError('champions_wait_endpoint_unverified')
            page = self.current.get('page')
            if page == 'loading':
                deadline = loading_deadline
            if page == 'connection_conflict':
                raise RuntimeError('account_connection_conflict')
            self._check_scope()
            if page in pages and (condition is None or condition(self.current)):
                return
            trace.append(dict(page=page, frame_sha256=self.current.get('frame_sha256')))
            if page not in waiting_pages | set(pages) or time.monotonic() >= deadline:
                failure = RuntimeError('game_loading_timeout' if page == 'loading' and time.monotonic() >= deadline else reason)
                failure.navigation_trace = trace
                raise failure
            if self.stop.wait(0.3):
                raise RuntimeError('operator_stop')
            self._capture()

    def _scope(self, match_index=None):
        active = self.book.get('active_round')
        if active is None:
            directory = self.output
        else:
            directory = self.output / f"round-{active['number']:03d}"
            self.device.event = dict(active.get('event') or self.device.event)
            if match_index is not None:
                active['match_index'] = match_index
            else:
                match_index = active.get('match_index')
        directory.mkdir(parents=True, exist_ok=True)
        self.device.output_dir = directory
        self.device.configure_round(self.book, len(self.book['rounds']) + 1, match_index)
        if match_index:
            from core.champions_race.live import MATCH_ACTIONS
            for action in MATCH_ACTIONS & self.current.get('actions', {}).keys():
                self.current['actions'][f'{action}_{match_index}'] = self.current['actions'][action]
            if 'launch_match' in self.current.get('actions', {}):
                self.current['actions'][f'resume_match_{match_index}'] = self.current['actions']['launch_match']
            if 'continue_standings' in self.current.get('actions', {}):
                self.current['actions'][f'resume_continue_standings_{match_index}'] = self.current['actions']['continue_standings']
                self.current['actions'][f'continue_viewed_standings_{match_index}'] = self.current['actions']['continue_standings']
            if 'continue_match_result' in self.current.get('actions', {}):
                self.current['actions'][f'resume_continue_match_result_{match_index}'] = self.current['actions']['continue_match_result']
                self.current['actions'][f'retry_continue_match_result_{match_index}'] = self.current['actions']['continue_match_result']
            if 'view_match_result' in self.current.get('actions', {}):
                for attempt in range(1, 4):
                    self.current['actions'][f'retry_view_match_result_{attempt}_{match_index}'] = dict(self.current['actions']['view_match_result'])

    def _act(self, action):
        if self.stop.is_set():
            raise RuntimeError('operator_stop')
        source_page = self.current.get('page')
        if action not in self.current.get('actions', {}):
            self._wait({self.current.get('page')}, 'action_unavailable:' + action, condition=lambda state: action in state.get('actions', {}))
        self.book.update(stage=action, source_page=self.current.get('page'))
        self._persist()
        self.emit(dict(type='progress', message=f"大赛：第{len(self.book['rounds']) + 1}/{self.max_rounds}轮 · {action}"))
        budget = ReobserveBudget(limit=3, seconds=30)
        while True:
            if self.stop.is_set():
                raise RuntimeError('operator_stop')
            try:
                receipt = self.device.step(action, self.stop, self.current)
                break
            except ReobserveRequired as error:
                outcome = getattr(error, 'action_outcome', {})
                observed = error.observation
                replacement = any((candidate in observed.get('actions', {}) for candidate in getattr(self.device, 'candidate_replacements', {}).get(action, ())))
                if outcome.get('execution_status') != 'not_executed' or outcome.get('input_attempts') != 0 or outcome.get('acknowledged_inputs') != 0 or (observed.get('page') in {'unknown', 'loading', None}) or (observed.get('page') != source_page) or (observed.get('endpoint') != self.device.endpoint) or (action not in observed.get('actions', {}) and (not replacement)) or (self.device.output_dir / (action + '-submitted.json')).exists():
                    raise
                self.current = budget.accept(error)
                self._check_scope()
                if action not in self.current.get('actions', {}):
                    return None
        self.current = receipt['observation']
        self.book['device_actions'] += 1
        return receipt

    def _navigate(self):
        routes = {'home': ('open_races', {'races'}), 'races': ('open_events', {'events'}), 'events': ('open_champions', {'champions_hub', 'champions_active_hub', 'champions_board', 'champions_choose'}), 'champions_active_hub': ('open_current_round', {'champions_board', 'champions_lineup', 'champions_team_confirm', 'champions_preview', 'champions_outcome', 'champions_standings', 'champions_match_result'})}
        for _ in range(4):
            from core.event_menu import unavailable
            if not self.resuming and (not self.book.get('started')) and (not self.book.get('rounds')) and unavailable(self.current, 'champions', self.device.endpoint):
                for _ in range(2):
                    if self.stop.wait(0.25):
                        raise RuntimeError('operator_stop')
                    self._capture()
                    if not unavailable(self.current, 'champions', self.device.endpoint):
                        break
                else:
                    self.unavailable_skipped = True
                    raise RuntimeError('champions_event_unavailable_skipped')
            route = routes.get(self.current.get('page'))
            if route is None:
                return
            self._act(route[0])
            self._wait(route[1], 'champions_navigation_unverified')

    def _advance_preview(self, index):
        """Re-read and supplement an inert, non-consuming result button."""
        self._scope(index)
        directory = self.device.output_dir
        original = f'view_match_result_{index}'
        if not (directory / (original + '-submitted.json')).exists():
            self._act(original)
        destinations = {'champions_outcome', 'champions_standings', 'champions_match_result', 'champions_board'}
        for _ in range(4):
            try:
                self.current = wait_for_transition(self.device, self.stop, self.current, lambda state: state.get('page') in destinations, waiting_pages={'champions_preview', 'unknown', 'loading'}, timeout=1.5, endpoint=self.device.endpoint)
                return
            except NavigationFailure as error:
                self.current = error.observation
                if error.kind == 'operator_stop':
                    raise RuntimeError('operator_stop') from error
                if error.kind == 'transition_unverified' and self.current.get('page') in {'unknown', 'loading'}:
                    try:
                        self.current = wait_for_transition(self.device, self.stop, self.current, lambda state: state.get('page') in destinations | {'champions_preview'}, waiting_pages={'unknown', 'loading'}, timeout=30, endpoint=self.device.endpoint)
                    except NavigationFailure as arrival:
                        self.current = arrival.observation
                        raise RuntimeError('operator_stop' if arrival.kind == 'operator_stop' else 'champions_preview_result_missing') from arrival
                    if self.current.get('page') in destinations:
                        return
                if error.kind != 'transition_unverified' or self.current.get('page') != 'champions_preview' or self.current.get('match_index') != index or (self.current.get('preview', {}).get('view_result_locked') is not False) or ('view_match_result' not in self.current.get('actions', {})):
                    raise RuntimeError('champions_preview_result_missing') from error
                self._check_scope()
                self._scope(index)
                attempt = next((n for n in range(1, 4) if not (directory / f'retry_view_match_result_{n}_{index}-submitted.json').exists()), None)
                if attempt is None:
                    raise RuntimeError('champions_preview_retry_exhausted') from error
                self.emit(dict(type='progress', message=f'第{index}场查看结果未转场，补点{attempt}/3'))
                self._act(f'retry_view_match_result_{attempt}_{index}')
        raise RuntimeError('champions_preview_retry_exhausted')

    def _adopt(self):
        page = self.current.get('page')
        if page not in {'champions_board', 'champions_lineup', 'champions_team_confirm'}:
            raise RuntimeError('champions_existing_round_source_required')
        if page == 'champions_board' and self.current.get('table_verified') is not True:
            raise RuntimeError('champions_existing_table_unverified')
        active = dict(number=len(self.book['rounds']) + 1, resource=dict(kind='preexisting', cost=0), adopted=True, completed_before_adoption=self.current.get('completed_matches', 0) if page == 'champions_board' else 0, event_phase=self.current.get('event_phase') if self.current.get('event_phase') is not None else round_phase(self.book), event=dict(self.device.event))
        self.book.update(started=True, pending_matches=5 - active['completed_before_adoption'], active_round=active)
        self._scope()
        (self.device.output_dir / 'adopted.json').write_text(json.dumps(dict(read_only=True, endpoint=self.device.endpoint, source=self.current, resource=active['resource']), ensure_ascii=False, indent=2), encoding='utf8')
        self._persist()

    def _enter(self):
        if self.current['page'] == 'champions_hub':
            self._act('open_entry')
            self._wait({'champions_entry_confirm'}, 'champions_entry_confirmation_missing')
        quote = admission_resource(self.current, self.book, max_rounds=self.max_rounds, allow_final_jewels=self.allow_final_jewels, allow_jewels=self.allow_jewels)
        active = dict(number=len(self.book['rounds']) + 1, resource=quote, adopted=False, completed_before_adoption=0, event_phase=self.current.get('event_phase') if self.current.get('event_phase') is not None else round_phase(self.book), event=dict(self.device.event))
        self.book.update(started=True, pending_matches=5, active_round=active)
        self._scope()
        self._act('confirm_entry')
        key = {'free': 'free', 'ticket': 'tickets', 'jewels': 'jewels'}[quote['kind']]
        self.book['resources_used'][key] += 1 if key != 'jewels' else quote['cost']
        self._persist()
        self._wait({'champions_lineup', 'champions_board'}, 'champions_admission_not_observed', waiting={'champions_entry_confirm'})

    def _continue_round(self):
        self._scope()
        active = self.book['active_round']
        directory = self.device.output_dir
        if self.current['page'] == 'champions_lineup':
            self._wait({'champions_lineup'}, 'champions_lineup_missing')
            if self.current.get('lineup_count') is None:
                raise RuntimeError('champions_lineup_unverified')
            if self.current['lineup_count'] < 3:
                if not (directory / 'auto_lineup-submitted.json').exists():
                    self._act('auto_lineup')
                self._wait({'champions_auto_confirm', 'champions_lineup'}, 'champions_auto_lineup_incomplete', condition=lambda state: state.get('page') == 'champions_auto_confirm' or state.get('lineup_count') == 3)
        if self.current['page'] == 'champions_auto_confirm':
            if not (directory / 'confirm_auto_lineup-submitted.json').exists():
                self._act('confirm_auto_lineup')
            self._wait({'champions_lineup'}, 'champions_auto_lineup_incomplete', condition=lambda state: state.get('lineup_count') == 3)
        if self.current['page'] == 'champions_lineup':
            if self.current.get('lineup_count') != 3:
                raise RuntimeError('champions_lineup_not_complete')
            self._act('confirm_lineup')
            self._wait({'champions_team_confirm', 'champions_board'}, 'champions_team_confirmation_missing')
        if self.current['page'] == 'champions_team_confirm':
            self._act('confirm_team')
            self._wait({'champions_board'}, 'champions_round_not_observed')
        self._wait({'champions_board', 'champions_preview', 'champions_outcome', 'champions_promotion', 'champions_standings', 'champions_match_result', 'champions_round_reward', 'champions_claim_receipt', 'champions_hub'}, 'champions_pending_round_unverified')
        if self.current['page'] == 'champions_board' and self.current.get('unmatched'):
            if (directory / 'match_round-submitted.json').exists():
                if not (directory / 'resume_matching-submitted.json').exists():
                    self._act('resume_matching')
            else:
                self._act('match_round')
            self._wait({'champions_board'}, 'champions_matching_timeout', waiting={'champions_matching'}, condition=lambda state: state.get('unmatched') is False and state.get('next_match') == 1)
        for _ in range(39):
            page = self.current['page']
            if page == 'champions_board':
                if self.current.get('table_verified') is not True:
                    raise RuntimeError('champions_table_unverified')
                completed = self.current.get('completed_matches')
                if type(completed) is not int:
                    raise RuntimeError('champions_progress_unverified')
                self.book['pending_matches'] = 5 - completed
                self._persist()
                if completed == 5:
                    if not (directory / 'claim_round-submitted.json').exists():
                        self._act('claim_round')
                    self._wait({'champions_round_reward', 'champions_claim_receipt', 'champions_hub'}, 'champions_reward_missing')
                    continue
                index = self.current.get('next_match')
                if type(index) is not int or not 1 <= index <= 5:
                    raise RuntimeError('champions_next_match_unverified')
                self._scope(index)
                action = f'launch_match_{index}'
                if not (directory / (action + '-submitted.json')).exists():
                    self._act(action)
                elif not (directory / f'resume_match_{index}-submitted.json').exists():
                    self._act(f'resume_match_{index}')
                try:
                    self.current = wait_for_transition(self.device, self.stop, self.current, lambda state: state.get('page') in {'champions_preview', 'champions_outcome', 'champions_standings', 'champions_match_result'} or (state.get('page') == 'champions_board' and state.get('table_verified') is True and (type(state.get('completed_matches')) is int) and (state['completed_matches'] >= index)), waiting_pages={'champions_board', 'unknown', 'loading'}, timeout=30, endpoint=self.device.endpoint)
                except NavigationFailure as error:
                    self.current = error.observation
                    raise RuntimeError('operator_stop' if error.kind == 'operator_stop' else 'champions_match_result_missing') from error
            elif page == 'champions_preview':
                self._advance_preview(self.current['match_index'])
            elif page == 'champions_outcome':
                index = self.device.match_index
                if index is None:
                    raise RuntimeError('champions_result_owner_missing')
                self._scope(index)
                if not (directory / f'dismiss_match_outcome_{index}-submitted.json').exists():
                    self._act(f'dismiss_match_outcome_{index}')
                self._wait({'champions_standings', 'champions_match_result', 'champions_board', 'champions_promotion'}, 'champions_outcome_result_missing')
            elif page == 'champions_promotion':
                index = self.device.match_index
                if index is None:
                    raise RuntimeError('champions_promotion_owner_missing')
                self._scope(index)
                if not (directory / f'dismiss_group_promotion_{index}-submitted.json').exists():
                    self._act(f'dismiss_group_promotion_{index}')
                self._wait({'champions_standings', 'champions_match_result', 'champions_board'}, 'champions_promotion_result_missing')
            elif page == 'champions_standings':
                index = self.current['match_index']
                self._scope(index)
                if not (directory / f'continue_standings_{index}-submitted.json').exists():
                    self._act(f'continue_standings_{index}')
                elif (directory / f'resume_match_{index}-submitted.json').exists() and (not (directory / f'resume_continue_standings_{index}-submitted.json').exists()):
                    self._act(f'resume_continue_standings_{index}')
                elif (directory / f'view_match_result_{index}-submitted.json').exists() and (not (directory / f'continue_viewed_standings_{index}-submitted.json').exists()):
                    self._act(f'continue_viewed_standings_{index}')
                self._wait({'champions_preview', 'champions_match_result', 'champions_board'}, 'champions_team_result_missing')
            elif page == 'champions_match_result':
                index = self.device.match_index
                if index is None:
                    raise RuntimeError('champions_result_owner_missing')
                if not (directory / f'continue_match_result_{index}-submitted.json').exists():
                    self._act(f'continue_match_result_{index}')
                elif (directory / f'resume_match_{index}-submitted.json').exists() and (not (directory / f'resume_continue_match_result_{index}-submitted.json').exists()):
                    self._act(f'resume_continue_match_result_{index}')
                retry = f'retry_continue_match_result_{index}'
                if self.current.get('page') == 'champions_match_result' and (not (directory / (retry + '-submitted.json')).exists()):
                    try:
                        self.current = wait_for_transition(self.device, self.stop, self.current, lambda state: state.get('page') in {'champions_board', 'champions_round_reward', 'champions_claim_receipt'}, waiting_pages={'champions_match_result', 'unknown', 'loading'}, timeout=3, endpoint=self.device.endpoint)
                    except NavigationFailure as error:
                        self.current = error.observation
                        if error.kind == 'operator_stop':
                            raise RuntimeError('operator_stop') from error
                        if error.kind == 'transition_unverified' and self.current.get('page') == 'champions_match_result':
                            self._act(retry)
                try:
                    self._wait({'champions_board', 'champions_round_reward', 'champions_claim_receipt'}, 'champions_next_match_missing')
                except RuntimeError as error:
                    if str(error) != 'champions_next_match_missing' or self.current.get('page') != 'champions_match_result' or (directory / (retry + '-submitted.json')).exists():
                        raise
                    self._act(retry)
                    self._wait({'champions_board', 'champions_round_reward', 'champions_claim_receipt'}, 'champions_next_match_missing')
            elif page == 'champions_round_reward':
                self._wait({'champions_round_reward', 'champions_claim_receipt', 'champions_hub'}, 'champions_claim_receipt_missing', condition=lambda state: state.get('page') != 'champions_round_reward' or 'continue_round_reward' in state.get('actions', {}) or (not (directory / 'advance_round_reward-submitted.json').exists() and 'advance_round_reward' in state.get('actions', {})))
                if self.current['page'] == 'champions_round_reward' and 'continue_round_reward' not in self.current.get('actions', {}):
                    self._act('advance_round_reward')
                    self._wait({'champions_round_reward', 'champions_claim_receipt', 'champions_hub'}, 'champions_claim_receipt_missing', condition=lambda state: state.get('page') != 'champions_round_reward' or 'continue_round_reward' in state.get('actions', {}))
                if self.current['page'] == 'champions_round_reward':
                    if not (directory / 'continue_round_reward-submitted.json').exists():
                        self._act('continue_round_reward')
                    self._wait({'champions_claim_receipt', 'champions_hub'}, 'champions_claim_receipt_missing')
            elif page == 'champions_claim_receipt':
                if not (directory / 'close_claim-submitted.json').exists():
                    self._act('close_claim')
                self._wait({'champions_hub'}, 'champions_reward_close_unverified')
            elif page == 'champions_hub':
                if not (directory / 'claim_round-submitted.json').exists():
                    raise RuntimeError('champions_claim_owner_missing')
                self.book['rounds'].append(dict(active, evidence_dir=str(directory), matches=5))
                self.book.update(started=False, pending_matches=0, active_round=None, stage='round_complete')
                self._scope()
                self._persist()
                return
            else:
                raise RuntimeError('champions_unexpected_round_page:' + str(page))
        raise RuntimeError('champions_round_transition_limit')

    def run(self, resume_from=None, *, restart_task=False):
        self.resuming = resume_from is not None
        self.restart_task = restart_task is True
        self.at_task_entry = True
        ok = False
        reason = 'champions_race_incomplete'
        failure = {}
        limited = False
        try:
            if self.stop.is_set():
                raise RuntimeError('operator_stop')
            if resume_from is not None:
                from core.champions_race.resume import load_pending
                self.output = Path(resume_from).resolve()
                self.book = load_pending(self.output, self.device.endpoint, self.device.division)
                if self.device.division is None:
                    self.device.division = self.book.get('division')
                    self.device.event['division'] = self.device.division
            else:
                self.output.mkdir(parents=True, exist_ok=True)
                self.book = dict(kind='champions_race', run=str(self.output), endpoint=self.device.endpoint, entry_day=datetime.now().date().isoformat(), division=self.device.division, requested_rounds=self.max_rounds, rounds=[], resources_used=dict(free=0, tickets=0, jewels=0), started=False, pending_matches=0, active_round=None, stage='starting', device_actions=0)
                with (self.output / 'active.json').open('x', encoding='utf8') as stream:
                    json.dump(self.book, stream)
            self.owns = True
            self._scope()
            self._capture()
            self._wait(OWNED_PAGES | {'home', 'races', 'events', 'profile', 'menu', 'uma_list', 'uma_detail', 'champions_choose'}, 'champions_initial_page_unverified')
            if self.current['page'] not in OWNED_PAGES | {'champions_choose'} and self.prepare_entry is not None:
                self.prepare_entry()
                self._capture()
            if self.current['page'] in {'home', 'races', 'events', 'champions_active_hub'}:
                self._navigate()
            self.at_task_entry = False
            if self.book.get('started'):
                self._continue_round()
            elif self.current['page'] in {'champions_board', 'champions_lineup', 'champions_team_confirm'}:
                self._adopt()
                self._continue_round()
            else:
                self._navigate()
            if self.current.get('page') == 'champions_choose':
                raise RuntimeError('大赛尚未报名，请在大赛选项中选择中级或高级')
            while self._completed_rounds() < self.max_rounds:
                try:
                    self._enter()
                except RuntimeError as error:
                    if str(error) not in {'champions_paid_entry_forbidden', 'champions_daily_entries_exhausted'}:
                        raise
                    self._act('cancel_entry')
                    self._wait({'champions_hub'}, 'champions_cancel_unverified')
                    limited = True
                    break
                self._continue_round()
            if self.current['page'] != 'champions_hub':
                raise RuntimeError('champions_return_source_unverified')
            self._scope()
            self._act('return_home')
            self._wait({'home'}, 'champions_home_unverified')
            ok = True
            reason = 'champions_race_available_entries_finished' if limited else 'champions_race_home_verified'
        except (RuntimeError, OSError, ValueError) as error:
            reason = str(error)
            failure = failure_details(error)
            if self.unavailable_skipped and reason == 'champions_event_unavailable_skipped':
                ok = True
                failure = {}
        result = dict(kind='champions_race', ok=ok, complete=ok, reason=reason, **failure, requested_rounds=self.max_rounds, completed_rounds=self._completed_rounds(), requested_matches=self.max_rounds * 5, completed_matches=self._completed_rounds() * 5, rounds=self.book.get('rounds', []), started=self.book.get('started', False), pending_matches=self.book.get('pending_matches', 0), pending=self.book.get('started', False) or self.current.get('page') in OWNED_PAGES - {'champions_hub', 'champions_entry_confirm'}, resources_used=self.book.get('resources_used', {}), jewels_used=self.book.get('resources_used', {}).get('jewels', 0), refills=0, items_used=0, device_actions=self.book.get('device_actions', 0), last_observation=self.current, champions_race_output_dir=str(self.output), skipped=limited or self.unavailable_skipped, limit_reason='free_tickets_exhausted' if limited else None)
        if self.unavailable_skipped:
            result.update(complete=False, activity_completed=False, skip_reason='event_unavailable')
        if self.owns:
            self._persist()
            (self.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
            if ok:
                (self.output / 'active.json').unlink()
        return result
