"""One guild visit, with durable ownership of each outgoing shoe transaction."""
import json
from pathlib import Path
import time
from uuid import uuid4
import numpy as np
from core.action_recovery import ReobserveRequired, ReobserveBudget, failure_details
from core.guild_daily import validate_options
MUTATIONS = {'confirm_donation', 'confirm_request', 'claim_received'}

def atomic_save(path, value):
    """Retry only Windows' transient replacement lock, never the game action."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
    for attempt in range(6):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))

class Journal:

    def __init__(self, path, endpoint, options):
        self.path = Path(path)
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding='utf8'))
            if self.state.get('version') != 1 or self.state.get('endpoint') != endpoint or self.state.get('options') != options:
                raise RuntimeError('guild_daily_source_mismatch')
        else:
            self.state = dict(version=1, endpoint=endpoint, options=options, status='running', entered=False, finished=False, pending=None, donated=[], claimed=False, donation_done=not options['donate'], request_done=options['request_item'] is None, request_status='disabled' if options['request_item'] is None else 'not_started', selected_request=None, selection_attempted=False, claims_checked=False, skip_reason=None, history=[])
            self.save()

    def save(self):
        atomic_save(self.path, self.state)

    def settle(self, observed):
        pending = self.state.get('pending')
        if not pending:
            return True
        action, page = (pending['action_id'], observed.get('page'))
        resolution = None
        if action == 'confirm_donation':
            before = pending.get('viewer_before')
            donated = any((r['key'] == pending.get('key') and (r.get('donated') or (type(before) is int and type(r.get('viewer_donated')) is int and (r['viewer_donated'] > before))) for r in observed.get('requests', [])))
            if page == 'guild_donation_success' or donated:
                self.state['donated'].append(pending['key'])
                resolution = 'donation_observed'
            elif page == 'guild_donation_limit':
                self.state['donation_done'] = True
                self.state['donation_count_complete'] = False
                resolution = 'donation_limit_observed'
        elif action == 'confirm_request' and (page in {'guild_request_success', 'guild_request_active'} or (page == 'guild_requests' and observed.get('request_active'))):
            if observed.get('item') not in (None, pending['item']):
                raise RuntimeError('guild_requested_item_changed')
            self.state.update(request_done=True, request_status='active' if page == 'guild_request_active' else 'requested')
            resolution = 'request_observed'
        elif action == 'claim_received' and page == 'guild_claim_success':
            self.state['claimed'] = True
            resolution = 'claim_observed'
        if resolution:
            self.state['history'].append(dict(**pending, resolution=resolution, observed_frame=observed.get('frame_sha256')))
            self.state['pending'] = None
            self.save()
            return True
        return False

class GuildDailyRunner:

    def __init__(self, device, emit, stop, *, options=None, journal=None):
        self.device, self.emit, self.stop = (device, emit, stop)
        self.options = validate_options(options or {})
        self.book = Journal(journal or Path(device.output_dir) / 'guild-state.json', device.endpoint, self.options)
        device.guild_state = self.book.state
        device.guild_options = self.options

    def run(self):
        state = self.book.state
        result = dict(kind='guild_daily', ok=False, game_task_complete=False, records=[], state_path=str(self.book.path.resolve()), pending=bool(state['pending']))
        observed = None
        reads = 0
        scrolls = 0
        last_scroll = None
        chat_seen = None
        filter_steps = 0
        pending_request_inspected = False
        top_verified = False
        top_previous = None
        top_scrolls = 0
        replans = ReobserveBudget()

        def request_signature(frame):
            return [(r['key'], round((r.get('position') or [0, 0])[1] / 10), r.get('viewer_donated'), r.get('donated')) for r in frame.get('requests', [])]

        def eligible(action, candidate):
            return action.startswith('donate_') and candidate['key'] not in state.get('unavailable_requests', []) and (candidate['item'] not in state.get('unavailable_items', []))

        def same_view(left, right):
            if left is None or right is None:
                return False
            left, right = (np.asarray(left), np.asarray(right))
            return left.shape == right.shape and left.size > 0 and (np.abs(left.astype(float) - right.astype(float)).mean() < 3 if left.dtype.kind in 'iuf' and right.dtype.kind in 'iuf' else np.array_equal(left, right))

        def step(action):
            nonlocal observed, reads
            candidate = observed['actions'][action]
            if action.startswith('donate_'):
                state['selected_request'] = {k: candidate.get(k) for k in ('key', 'item', 'viewer_donated')}
            if action in MUTATIONS:
                if state['pending']:
                    raise RuntimeError('guild_transaction_pending')
                state['pending'] = dict(action_id=action, status='intent', transaction_id=uuid4().hex, frame_sha256=observed['frame_sha256'], item=candidate.get('item'), key=(state.get('selected_request') or {}).get('key'), viewer_before=(state.get('selected_request') or {}).get('viewer_donated'))
            self.book.save()
            self.emit(dict(type='progress', phase='guild_daily', action_id=action, message={'confirm_donation': '社团：确认捐赠现有跑鞋', 'confirm_request': '社团：发起跑鞋请求', 'claim_received': '社团：领取收到的跑鞋', 'return_home': '社团日活：返回主页'}.get(action, '社团日活：读取并推进')))
            try:
                receipt = self.device.step(action, self.stop, observed)
            except Exception as error:
                outcome = getattr(error, 'action_outcome', {})
                if action in MUTATIONS and outcome.get('execution_status') == 'not_executed' and (outcome.get('input_attempts') == 0 and outcome.get('acknowledged_inputs') == 0):
                    state['history'].append(dict(**state['pending'], resolution='explicit_zero_input', outcome=outcome))
                    state['pending'] = None
                    self.book.save()
                elif action in MUTATIONS and state['pending']:
                    state['pending']['outcome'] = outcome
                    if outcome.get('execution_status') == 'uncertain':
                        state['pending']['status'] = 'uncertain'
                    self.book.save()
                raise
            result['records'].append(receipt)
            outcome = receipt.get('action_outcome') or {}
            if receipt.get('performed') is not True or outcome.get('execution_status') not in (None, 'executed'):
                raise RuntimeError('guild_input_not_acknowledged')
            if action in MUTATIONS:
                state['pending'].update(status='submitted', outcome=outcome)
            if action.startswith('select_'):
                state['selection_attempted'] = True
            if action == 'open_requests' and receipt['observation'].get('page') in {'guild_claim_success', 'guild_request_picker', 'guild_request_active', 'guild_requests'}:
                state['claims_checked'] = True
            state['history'].append(dict(action_id=action, receipt_path=receipt.get('receipt_path'), source_frame_sha256=receipt.get('source_frame_sha256')))
            self.book.save()
            observed = receipt['observation']
            reads = 0
        try:
            observed = self.device.capture()
            for _ in range(120):
                if self.stop.is_set():
                    raise RuntimeError('operator_stop')
                result['last_observation'] = observed
                page = observed.get('page')
                pending = state.get('pending') or {}
                if pending.get('action_id') == 'confirm_request' and page == 'guild_hub' and ('open_requests' in observed.get('actions', {})) and (not pending_request_inspected):
                    pending_request_inspected = True
                    try:
                        step('open_requests')
                        replans.executed()
                    except ReobserveRequired as error:
                        observed = replans.accept(error)
                    continue
                if page in {'unknown', 'loading'} or not self.book.settle(observed):
                    if reads >= 12 or self.stop.wait(0.3):
                        raise RuntimeError('operator_stop' if self.stop.is_set() else 'guild_state_not_observed')
                    reads += 1
                    observed = self.device.capture()
                    continue
                action = None
                if page == 'home':
                    if state['finished'] or state.get('status') == 'complete':
                        state['status'] = 'complete'
                        self.book.save()
                        skipped = bool(state['skip_reason'])
                        return dict(result, ok=True, game_task_complete=not skipped, skipped=skipped, skip_verified=skipped, skip_reason=state['skip_reason'], pending=False, donated_requests=len(state['donated']) if state.get('donation_count_complete', True) else None, verified_donated_requests=len(state['donated']), donation_count_complete=state.get('donation_count_complete', True), incoming_claim_observed=state['claimed'], request_status=state['request_status'], reason='guild_daily_skipped_home_verified' if skipped else 'guild_daily_home_verified', last_observation=observed)
                    if observed.get('guild_locked') is True:
                        state.update(finished=True, skip_reason='guild_locked')
                        self.book.save()
                        continue
                    action = 'open_guild'
                elif page == 'guild_unjoined':
                    state.update(entered=True, finished=True, skip_reason='not_joined')
                    action = next((a for a in ('return_home', 'continue_guild_intro', 'close_guild_intro', 'close_unjoined') if a in observed['actions']), None)
                elif page == 'guild_locked_dialog':
                    state.update(finished=True, skip_reason='guild_locked')
                    action = 'close'
                elif page == 'guild_menu':
                    action = 'close'
                elif page == 'guild_rank_reward':
                    if not state.get('rank_reward_observed'):
                        state['rank_reward_observed'] = True
                        state['history'].append(dict(action_id='rank_reward_observed', source_frame_sha256=observed.get('frame_sha256'), screenshot_path=observed.get('screenshot_path')))
                    action = 'close'
                elif page == 'guild_hub':
                    state['entered'] = True
                    if observed.get('chat_hub'):
                        current_filter = observed.get('chat_filter')
                        state.setdefault('original_chat_filter', current_filter)
                        if not state['donation_done']:
                            if 'open_donations' in observed['actions']:
                                action = 'open_donations'
                            elif current_filter not in {'请求', '捐赠请求', '道具请求', '道具捐赠请求'}:
                                if filter_steps >= 6:
                                    raise RuntimeError('guild_chat_filter_unverified')
                                filter_steps += 1
                                action = 'open_filter'
                            else:
                                action = next((a for a, c in observed['actions'].items() if eligible(a, c)), None)
                                if action is None:
                                    if observed.get('unresolved_requests'):
                                        raise RuntimeError('guild_donation_identity_unverified')
                                    signature = observed.get('list_signature')
                                    if same_view(signature, chat_seen):
                                        state.update(donation_done=True, donation_status='visible_requests_checked')
                                    elif observed.get('chat_empty'):
                                        chat_seen = signature
                                        if self.stop.wait(0.3):
                                            raise RuntimeError('operator_stop')
                                        observed = self.device.capture()
                                        continue
                                    elif scrolls < 12:
                                        chat_seen = signature
                                        scrolls += 1
                                        action = 'scroll_chat'
                                    else:
                                        raise RuntimeError('guild_chat_end_unverified')
                        if action is None and state['donation_done']:
                            if state['request_done'] and state.get('claims_checked'):
                                original = state.get('original_chat_filter')
                                if original is not None and current_filter != original:
                                    if filter_steps >= 9:
                                        raise RuntimeError('guild_chat_filter_restore_unverified')
                                    filter_steps += 1
                                    action = 'open_filter'
                                else:
                                    state['finished'] = True
                                    action = 'return_home'
                            else:
                                action = 'open_requests'
                        self.book.save()
                        if action not in observed['actions']:
                            if reads >= 12 or self.stop.wait(0.3):
                                raise RuntimeError('guild_action_not_observed:' + str(action))
                            reads += 1
                            observed = self.device.capture()
                            continue
                        try:
                            step(action)
                            replans.executed()
                        except ReobserveRequired as error:
                            observed = replans.accept(error)
                        continue
                    if state['donation_done'] and state['request_done'] and state.get('claims_checked'):
                        state['finished'] = True
                    action = 'return_home' if state['finished'] else 'open_requests'
                elif page == 'guild_requests':
                    donation_list = observed.get('view_kind') == 'donation_list'
                    if not donation_list:
                        state['claims_checked'] = True
                    if donation_list and observed.get('donation_exhausted'):
                        state['donation_done'] = True
                    if donation_list and (not state['donation_done']) and (not top_verified):
                        signature = request_signature(observed)
                        if observed.get('list_top') is True or (observed.get('list_top') is None and signature == top_previous):
                            top_verified = True
                            last_scroll = None
                        else:
                            if top_scrolls >= 12:
                                raise RuntimeError('guild_list_top_unverified')
                            top_previous = signature
                            top_scrolls += 1
                            step('scroll_requests_top')
                            continue
                    if observed.get('request_active') and (not state['request_done']):
                        state.update(request_done=True, request_status='active')
                    if 'claim_received' in observed['actions'] and (not state['claimed']):
                        action = 'claim_received'
                    elif not state['donation_done']:
                        action = next((a for a, c in observed['actions'].items() if eligible(a, c)), None)
                        if action is None:
                            if observed.get('unresolved_requests'):
                                raise RuntimeError('guild_donation_identity_unverified')
                            signature = request_signature(observed) if donation_list else observed.get('list_signature')
                            at_end = observed.get('list_bottom') is True or (observed.get('list_bottom') is None and same_view(signature, last_scroll))
                            if observed.get('donation_exhausted') or at_end:
                                state['donation_done'] = True
                            elif 'scroll_requests' in observed['actions'] and scrolls < 12:
                                last_scroll = signature
                                scrolls += 1
                                action = 'scroll_requests'
                            else:
                                raise RuntimeError('guild_request_list_end_unverified')
                    if action is None and state['donation_done']:
                        action = 'close' if donation_list or state['request_done'] else 'open_request'
                elif page == 'guild_donation_confirm':
                    selected = state.get('selected_request') or {}
                    if not self.options['donate'] or not selected.get('key') or observed.get('item') != selected.get('item'):
                        raise RuntimeError('guild_donation_not_bound')
                    if observed.get('remaining_donations') == 0:
                        state.update(donation_done=True, donation_status='daily_limit_observed')
                        action = 'close'
                    elif observed.get('owned') == 0:
                        state.setdefault('unavailable_items', []).append(observed['item'])
                        action = 'close'
                    elif observed.get('quantity') == 0:
                        state.setdefault('unavailable_requests', []).append(selected['key'])
                        action = 'close'
                    else:
                        action = 'confirm_donation'
                elif page == 'guild_request_picker':
                    state['claims_checked'] = True
                    item = self.options['request_item']
                    if item is None or state['request_done']:
                        action = 'close'
                    elif observed.get('selected_item') == item:
                        action = 'confirm_request'
                    elif not state['selection_attempted']:
                        action = 'select_' + item
                    else:
                        raise RuntimeError('guild_request_selection_unverified')
                elif page == 'guild_request_confirm':
                    if state['selection_attempted'] and observed.get('item') == self.options['request_item'] and (not state['request_done']):
                        action = 'confirm_request'
                    else:
                        action = 'close'
                elif page in {'guild_donation_success', 'guild_request_success', 'guild_claim_success'}:
                    if page == 'guild_claim_success' and (not state['claimed']):
                        state['claimed'] = True
                        state['history'].append(dict(action_id='incoming_claim_observed', item=observed.get('item'), quantity=observed.get('received_count'), source_frame_sha256=observed.get('frame_sha256')))
                    action = 'close'
                elif page == 'guild_request_active':
                    state.update(request_done=True, request_status='active')
                    action = 'close'
                elif page == 'guild_donation_limit':
                    state['donation_done'] = True
                    action = 'close'
                else:
                    raise RuntimeError('guild_page_not_supported:' + str(page))
                self.book.save()
                if action not in observed.get('actions', {}):
                    if reads >= 8 or self.stop.wait(0.3):
                        if page == 'guild_donation_confirm' and (not state['pending']) and (action == 'confirm_donation') and ('close' in observed.get('actions', {})):
                            state.setdefault('unavailable_requests', []).append(state['selected_request']['key'])
                            state['history'].append(dict(action_id='donation_submit_unavailable', key=state['selected_request']['key'], frame_sha256=observed.get('frame_sha256')))
                            self.book.save()
                            step('close')
                            continue
                        raise RuntimeError('guild_action_not_observed:' + str(action))
                    reads += 1
                    observed = self.device.capture()
                    continue
                try:
                    step(action)
                    replans.executed()
                except ReobserveRequired as error:
                    observed = replans.accept(error)
            raise RuntimeError('guild_daily_step_limit')
        except (RuntimeError, OSError, ValueError) as error:
            state['status'] = 'paused'
            self.book.save()
            return dict(result, reason=str(error), pending=bool(state['pending']) or (observed or {}).get('page') != 'home', pending_kind='guild_transaction' if state['pending'] else 'guild_navigation', last_observation=observed or {}, device_action_count_complete=False, **failure_details(error))
