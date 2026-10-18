"""Finish each team race and stop on a freshly observed empty RP bar."""
import time
from desktop.umaa_public_context import endpoint as _public_endpoint
from core.jjc.live import ACTION_PAGES
from core.action_recovery import ReobserveRequired, ReobserveBudget, failure_details
NEXT_PAGES = {'accelerate_weekly': {'weekly_settlement', 'arena'}, 'continue_weekly': {'arena'}, 'return_story_races': {'races'}, 'accelerate_summary': {'summary', 'rewards', 'story', 'limited_sale', 'high_score'}, 'open_races': {'races'}, 'open_arena': {'arena', 'weekly_settlement'}, 'open_opponents': {'opponents'}, 'choose_top': {'matchup', 'opponents'}, 'continue_matchup': {'items', 'matchup'}, 'enter_race': {'race_ready'}, 'choose_gift': {'matchup', 'opponents'}, 'enable_short': {'race_ready'}, 'view_results': {'animation', 'results'}, 'skip': {'animation', 'results'}, 'continue_results': {'story', 'summary', 'limited_sale', 'high_score'}, 'close_story': {'story', 'summary', 'story_detail'}, 'next': {'arena', 'rewards'}, 'continue_rewards': {'arena', 'story', 'summary', 'limited_sale'}, 'cancel_limited_sale': {'arena', 'story', 'summary'}, 'rematch': {'opponents', 'arena'}, 'dismiss_high_score': {'story', 'summary', 'limited_sale'}, 'resume_race': {'race_ready', 'animation', 'results'}, 'resume_matchup': {'matchup'}, 'open_pending_race': {'matchup', 'race_ready', 'animation', 'results', 'summary', 'rewards'}}

class JjcRunner:

    def __init__(self, device, emit, stop_event, *, prefer_rematch=False, prefer_gift=False, recover_stall=None, verify_resume_account=None):
        self.device, self.emit, self.stop = (device, emit, stop_event)
        self.prefer_rematch = prefer_rematch
        self.prefer_gift = prefer_gift
        self.recover_stall = recover_stall
        self.verify_resume_account = verify_resume_account

    def run(self, *, resume_from=None, restart_verified=False):
        report = dict(kind='jjc', ok=False, completed_races=0, remaining_rp=None, no_items=True, no_refill=True, records=[])
        pending_rp = None
        adopted_pending_rp = None
        previous = None
        opponent_attempts = 0
        matchup_attempts = 0
        story_return = False
        recovery_attempted = False
        verify_at_arena = resume_from is not None and (not restart_verified)
        if resume_from is not None:
            report.update(resumed_from=str(resume_from), resume_unstarted=True, resume_mode='current_scene')
            self.device.context.pop('recovery_entry_rp', None)
            self.device.context.pop('recovery_matchup_rp', None)
        try:
            observed = self.device.capture()
            replans = ReobserveBudget()
            for _ in range(180):
                report['last_observation'] = observed
                if self.stop.is_set():
                    raise RuntimeError('operator_stop')
                page = observed['page']
                if page == 'results' and not observed['actions']:
                    # Reopening a pending race can land on the results entrance
                    # animation before Continue appears. Observe only; never
                    # accelerate with an invented click or resubmit race entry.
                    deadline = time.monotonic() + 20
                    while observed['page'] == 'results' and not observed['actions']:
                        if time.monotonic() >= deadline:
                            raise RuntimeError('jjc_results_controls_timeout')
                        if self.stop.wait(.5):
                            raise RuntimeError('operator_stop')
                        observed = self.device.capture()
                        report['last_observation'] = observed
                    page = observed['page']
                if page == 'arena' and observed.get('arena_tallying'):
                    report.update(remaining_rp=observed.get('rp'), tally_period=observed.get('tally_period'))
                    raise RuntimeError('jjc_weekly_tally_in_progress')
                if verify_at_arena and page == 'arena':
                    if observed.get('endpoint') != _public_endpoint() or self.verify_resume_account is None or self.verify_resume_account() is not True:
                        raise RuntimeError('jjc_resume_account_unverified')
                    if self.stop.is_set():
                        raise RuntimeError('operator_stop')
                    verify_at_arena = False
                    observed = self.device.capture()
                    previous = None
                    continue
                if page in {'unknown', 'loading'} and (not observed['actions']) and (self.recover_stall is not None) and (not recovery_attempted):
                    recovery_attempted = True
                    recovery = self.recover_stall(observed)
                    if self.stop.is_set():
                        raise RuntimeError('operator_stop')
                    if recovery is not None:
                        report['restart_receipts'] = [recovery['receipt']]
                        report['restart_startup'] = recovery['startup']
                        if not recovery['startup']['ok']:
                            raise RuntimeError(recovery['startup']['reason'])
                        self.device.context = {'recovery_entry_rp': pending_rp} if pending_rp is not None else {}
                        observed = self.device.capture()
                        previous = None
                        continue
                if page == 'story_detail' and pending_rp is not None:
                    story_return = True
                    self.device.context['recovery_entry_rp'] = pending_rp
                if previous and page not in NEXT_PAGES[previous]:
                    raise RuntimeError('unexpected_transition:' + previous + '->' + page)
                if previous == 'open_pending_race':
                    if page == 'matchup':
                        adopted_pending_rp = None
                if previous == 'resume_matchup':
                    self.device.context.pop('recovery_matchup_rp', None)
                if page == 'opponents' and previous == 'rematch' and (pending_rp is not None):
                    report['completed_races'] += 1
                    report['records'].append(dict(settlement_verified=True, via='rematch'))
                    pending_rp = None
                if page == 'arena' and (not observed.get('race_in_progress')):
                    report['remaining_rp'] = observed['rp']
                    if adopted_pending_rp is not None:
                        if previous not in ('next', 'continue_rewards', 'cancel_limited_sale'):
                            raise RuntimeError('adopted_race_settlement_unverified')
                        report['recovered_existing_races'] = report.get('recovered_existing_races', 0) + 1
                        report['records'].append(dict(existing_race_settlement_verified=True, remaining_rp=observed['rp']))
                        adopted_pending_rp = None
                    if pending_rp is not None:
                        if not (story_return and previous == 'open_arena') and previous not in ('next', 'continue_rewards', 'cancel_limited_sale', 'rematch'):
                            raise RuntimeError('race_settlement_not_verified')
                        report['completed_races'] += 1
                        report['records'].append(dict(settlement_verified=True, remaining_rp=observed['rp']))
                        pending_rp = None
                        story_return = False
                    if observed['rp'] == 0:
                        report.update(ok=True, reason='rp_exhausted', last_observation=observed)
                        return report
                actions = observed['actions']
                if len(actions) != 1:
                    raise RuntimeError('no_safe_action:' + page)
                action = next(iter(actions))
                if action == 'resume_race' and pending_rp is None and (getattr(self.device, 'context', {}).get('recovery_matchup_rp') is not None):
                    action = 'resume_matchup'
                elif action == 'resume_race' and pending_rp is None and (getattr(self.device, 'context', {}).get('recovery_entry_rp') is None):
                    if 'open_pending_race' in observed.get('navigation_actions', {}):
                        action = 'open_pending_race'
                    adopted_pending_rp = observed.get('rp')
                if self.prefer_gift and page == 'opponents' and ('choose_gift' in observed.get('navigation_actions', {})):
                    action = 'choose_gift'
                if self.prefer_rematch and page == 'summary' and (type(pending_rp) is int) and (pending_rp > 1) and ('rematch' in observed.get('navigation_actions', {})):
                    action = 'rematch'
                if action not in ACTION_PAGES:
                    raise RuntimeError('jjc_action_forbidden')
                if action in {'choose_top', 'choose_gift'}:
                    opponent_attempts += 1
                    if opponent_attempts > 2:
                        raise RuntimeError('opponent_selection_not_received')
                else:
                    opponent_attempts = 0
                if action == 'continue_matchup':
                    matchup_attempts += 1
                    if matchup_attempts > 2:
                        raise RuntimeError('matchup_continue_not_received')
                else:
                    matchup_attempts = 0
                old_pending_rp = pending_rp
                if action == 'enter_race':
                    if verify_at_arena:
                        raise RuntimeError('jjc_resume_account_unverified')
                    if adopted_pending_rp is not None:
                        raise RuntimeError('existing_race_entry_forbidden')
                    pending_rp = observed['rp']
                self.emit(dict(type='progress', state='running', phase=page, message=f"JJC {page} → {action}；已结算 {report['completed_races']} 场"))
                if resume_from is not None:
                    report['resume_unstarted'] = False
                try:
                    receipt = self.device.step(action, self.stop)
                except ReobserveRequired as error:
                    observed = replans.accept(error)
                    pending_rp = old_pending_rp
                    continue
                if receipt.get('execution_status') == 'executed':
                    replans.executed()
                observed = receipt['observation']
                previous = action
            raise RuntimeError('jjc_step_limit')
        except RuntimeError as error:
            report.update(failure_details(error))
            report['reason'] = str(error)
            report['pending_race'] = pending_rp is not None or adopted_pending_rp is not None or report.get('resume_unstarted') is True
            return report
