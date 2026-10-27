"""One account/day ledger, with durable intent and no unknown replay."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import uuid

from .options import normalize_options

ZONE = timezone(timedelta(hours=8))
RESOURCES = {'start_normal':'normal', 'start_special':'special',
             'confirm_retry':'clock', 'confirm_refill':'jelly',
             'confirm_recruit':'points', 'claim_all':'claim'}


def game_day(timestamp=None):
    value = datetime.now(ZONE) if timestamp is None else datetime.fromtimestamp(timestamp, ZONE)
    return (value-timedelta(hours=5)).date().isoformat()


def atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('w', encoding='utf8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def batch_scope(output, *, day=None):
    """Private scheduler scope; public current-account use supplies its own key."""
    output = Path(output).resolve()
    parts = output.parts
    indices = [i for i, part in enumerate(parts) if re.fullmatch(r'account-\d+', part)]
    if len(indices) != 1:
        raise ValueError('strongest_team_account_scope_required')
    i = indices[0]
    batch = Path(*parts[:i])
    if batch.parent.name != 'mfa-batches' or not (batch/'selection.json').is_file():
        raise ValueError('strongest_team_batch_scope_invalid')
    number = int(parts[i].split('-')[1])
    selection = json.loads((batch/'selection.json').read_text(encoding='utf8'))
    accounts = selection['accounts']
    accounts = [int(x) for x in accounts.split(',')] if isinstance(accounts,str) else accounts
    if number not in accounts:
        raise ValueError('strongest_team_account_not_selected')
    day = day or game_day()
    scope = dict(account='account-'+str(number), day=day, event='strongest_team')
    ledger = batch.parent.parent/'module-ledgers/strongest_team'/scope['account']/(day+'.json')
    prior_day_guard(ledger)
    return ledger, scope


def prior_day_guard(ledger):
    ledger=Path(ledger)
    if ledger.parent.exists():
        for old in ledger.parent.glob('*.json'):
            if old==ledger or not re.fullmatch(r'\d{4}-\d{2}-\d{2}\.json',old.name):continue
            prior=json.loads(old.read_text(encoding='utf8'))
            if prior.get('pending_entry') or prior.get('pending_operation') or prior.get('navigation_pending'):
                error=RuntimeError('strongest_team_prior_day_pending')
                error.journal_path=str(old)
                raise error


class Journal:
    def __init__(self, path, scope, endpoint, options=None):
        self.path = Path(path).resolve()
        self.options = normalize_options(options)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding='utf8'))
            if (self.data.get('schema') != 'uma.strongest_team.day.v1'
                    or self.data.get('scope') != scope or self.data.get('endpoint') != endpoint):
                raise ValueError('strongest_team_journal_scope_mismatch')
        else:
            self.data = dict(schema='uma.strongest_team.day.v1', scope=deepcopy(scope), endpoint=endpoint,
                context_verified=False, pending_entry=None, pending_operation=None,
                normal_reserved=0, normal_submitted=0, normal_completed=0,
                special_reserved=0, special_submitted=0, special_completed=0,
                jelly_reserved=0, jelly_confirmed=0, clock_reserved=0, clock_confirmed=0,
                free_retry_reserved=0,free_retry_confirmed=0,
                points_reserved=0, points_confirmed=0, records=[], done=False,
                mission_scan_at=-1, period_scan_at=-1, daily_tickets_claimed=False, last_observation={})
            self.save()

    def save(self):
        atomic_write(self.path, self.data)

    def cap_remaining(self):
        data = self.data
        used = data['normal_reserved'] + (0 if self.options['special_extra'] else data['special_reserved'])
        return max(0, self.options['max_entries']-used)

    def record(self, kind, **fields):
        self.data['records'].append(dict(kind=kind, **deepcopy(fields)))

    def reserve(self, action, frame):
        candidate = frame['actions'][action]
        if action=='confirm_adaptation':
            operation=self.data['pending_operation'];entry=self.data['pending_entry']
            if (not operation or not entry or operation['id']!=entry['id']
                    or operation['resource']!=entry['kind'] or operation['status']!='submitted'
                    or operation.get('continuation') or entry.get('progress_seen')
                    or entry['status']!='submitted' or candidate.get('entry_id')!=entry['id']):
                raise RuntimeError('strongest_team_original_admission_continuation_required')
            dispatch=uuid.uuid4().hex
            operation['status']='continuation_reserved'
            operation['continuation']=dict(dispatch_id=dispatch,status='reserved',parent_submission_counted=True)
            intent=dict(id=operation['id'],dispatch_id=dispatch,action=action,resource=operation['resource'],
                quantity=operation['quantity'],source_page=frame['page'],source_frame_sha256=frame['frame_sha256'],
                status='reserved',admission_continuation=True)
            self.data['last_action']=deepcopy(intent);self.record('intent',**intent);self.save()
            return dispatch
        resource = RESOURCES.get(action)
        identity = uuid.uuid4().hex
        intent = dict(id=identity, action=action, source_page=frame['page'],
                      source_frame_sha256=frame['frame_sha256'], resource=resource,
                      status='reserved', quantity=1, basis=candidate.get('basis'))
        if resource:
            if self.data['pending_operation']:
                raise RuntimeError('strongest_team_operation_pending')
            if not self.data['context_verified']:
                raise RuntimeError('strongest_team_context_unverified')
            if resource in {'normal','special'}:
                if self.data['pending_entry']:
                    raise RuntimeError('strongest_team_entry_pending')
                if resource == 'normal' and self.cap_remaining() <= 0:
                    raise RuntimeError('strongest_team_daily_cap')
                if resource == 'special' and (self.data['special_reserved'] >= 1
                        or not self.options['special_race']
                        or not self.options['special_extra'] and self.cap_remaining() <= 0):
                    raise RuntimeError('strongest_team_special_cap')
                self.data[resource+'_reserved'] += 1
                self.data['pending_entry'] = dict(id=identity, kind=resource, status='reserved',
                    progress_seen=False, result_seen=False, retries=0)
            elif resource == 'clock':
                entry = self.data['pending_entry']
                cost=candidate.get('cost',1)
                if cost not in (0,1):raise RuntimeError('strongest_team_retry_cost_unknown')
                if (not entry or not entry['result_seen']
                        or cost==1 and self.data['clock_reserved'] >= self.options['max_clocks']
                        or cost==0 and self.data['free_retry_reserved']>=5):
                    raise RuntimeError('strongest_team_clock_cap')
                intent['quantity']=cost
                intent['free_before']=candidate.get('free_remaining')
                self.data['clock_reserved'] += cost
                if cost==0:self.data['free_retry_reserved']+=1
                entry['retries'] += 1
                intent['before_entry'] = deepcopy(entry)
                entry.update(result_seen=False, progress_seen=False)
            elif resource == 'jelly':
                quantity = candidate.get('quantity')
                if (type(quantity) is not int or quantity <= 0
                        or self.data['jelly_reserved']+quantity > self.options['max_jellies']
                        or not self.options['allow_existing_jelly']):
                    raise RuntimeError('strongest_team_jelly_cap')
                intent['quantity'] = quantity
                self.data['jelly_reserved'] += quantity
            elif resource == 'points':
                cost = candidate.get('cost')
                if type(cost) is not int or cost <= 0:
                    raise RuntimeError('strongest_team_recruit_cost_unknown')
                intent['quantity'] = cost
                self.data['points_reserved'] += cost
            intent.update(stock_before=candidate.get('stock_before'),
                stock_after=candidate.get('stock_after'), tickets_before=frame.get('tickets') if frame.get('tickets') is not None else self.data.get('refill_tickets_before'),
                team_after=frame.get('team_after'), mission_tab=frame.get('mission_tab'))
            self.data['pending_operation'] = deepcopy(intent)
        self.data['last_action'] = deepcopy(intent)
        self.record('intent', **intent)
        self.save()  # Intent exists before the executor receives an action.
        return identity

    def _release_zero_input(self, operation):
        resource = operation['resource']
        quantity = operation['quantity']
        if resource in {'normal','special'}:
            entry = self.data['pending_entry']
            if not entry or entry['id'] != operation['id'] or entry['status'] != 'reserved':
                raise RuntimeError('strongest_team_zero_input_owner_mismatch')
            self.data[resource+'_reserved'] -= 1
            self.data['pending_entry'] = None
        elif resource in {'jelly','clock','points'}:
            self.data[resource+'_reserved'] -= quantity
            if resource == 'clock':
                prior = operation['before_entry']
                prior['retries'] -= 1
                self.data['pending_entry'] = prior
                if quantity==0:self.data['free_retry_reserved']-=1
        self.record('zero_input_release', operation=operation)
        self.data['pending_operation'] = None

    def receipt(self, receipt):
        outcome = receipt.get('action_outcome') or {}
        action = self.data.get('last_action') or {}
        if action:
            self.data['last_action']['action_outcome'] = deepcopy(outcome)
        if (action.get('action')=='open_event'
                and action.get('basis') in {'verified_strongest_team_banner_art','named_strongest_team_event_card'}
                and outcome.get('execution_status')=='executed'
                and outcome.get('input_attempts')==outcome.get('acknowledged_inputs')==1):
            self.data['context_verified'] = True
        compact={k:v for k,v in receipt.items() if k!='observation'}
        observed=receipt.get('observation') or {}
        compact['observed']={k:observed[k] for k in ['page','reason','endpoint','frame_sha256','screenshot_path',
            'tickets','entry_kind','outcome','stock_before','stock_after','quantity','recruit_cost','points',
            'team_before','team_after','mission_tab','daily_ticket_claims'] if k in observed}
        self.record('receipt', intent_id=action.get('id'), receipt=compact)
        operation = self.data['pending_operation']
        if operation and operation['id'] == action.get('id'):
            if action.get('admission_continuation'):
                continuation=operation.get('continuation') or {}
                if continuation.get('dispatch_id')!=action.get('dispatch_id'):
                    raise RuntimeError('strongest_team_continuation_receipt_owner_mismatch')
                if (outcome.get('execution_status')=='not_executed'
                        and outcome.get('input_attempts')==outcome.get('acknowledged_inputs')==0):
                    operation['status']='submitted';operation.pop('continuation',None)
                elif (outcome.get('execution_status')=='executed'
                        and outcome.get('input_attempts')==outcome.get('acknowledged_inputs')==1):
                    operation['status']='submitted';continuation['status']='submitted'
                else:
                    operation['status']='uncertain';continuation['status']='uncertain'
                    self.data['pending_entry']['status']='uncertain'
            elif (outcome.get('execution_status') == 'not_executed'
                    and outcome.get('input_attempts') == outcome.get('acknowledged_inputs') == 0):
                self._release_zero_input(operation)
            elif (outcome.get('execution_status') == 'executed'
                    and outcome.get('input_attempts') == outcome.get('acknowledged_inputs') == 1):
                operation['status'] = 'submitted'
                if operation['resource'] in {'normal','special'}:
                    self.data['pending_entry']['status'] = 'submitted'
                    self.data[operation['resource']+'_submitted'] += 1
            else:
                operation['status'] = 'uncertain'
                if self.data['pending_entry'] and operation['resource'] in {'normal','special'}:
                    self.data['pending_entry']['status'] = 'uncertain'
        self.save()
        if isinstance(receipt.get('observation'), dict):
            self.observe(receipt['observation'])

    def _confirm_operation(self, reason):
        operation = self.data['pending_operation']
        if not operation:
            return
        resource = operation['resource']
        if resource in {'jelly','clock','points'}:
            self.data[resource+'_confirmed'] += operation['quantity']
        if resource=='clock' and operation['quantity']==0:
            self.data['free_retry_confirmed']+=1
        if resource=='points':
            self.data['recruited_for']=(self.data.get('last_completed_entry') or {}).get('id')
        if resource in {'normal','special'} and operation['status'] == 'uncertain':
            if not (operation.get('continuation') or {}).get('parent_submission_counted'):
                self.data[resource+'_submitted'] += 1
            self.data['pending_entry']['status'] = 'submitted'
        self.record('operation_verified', operation=operation, reason=reason)
        self.data['pending_operation'] = None

    def observe(self, frame):
        if frame.get('endpoint') != self.data['endpoint']:
            raise ValueError('strongest_team_observation_endpoint_mismatch')
        page = frame.get('page')
        if frame.get('event_verified'):
            self.data['context_verified'] = True
        operation = self.data['pending_operation']
        entry = self.data['pending_entry']
        owned = self.data['context_verified']
        if owned and operation and operation['status'] in {'submitted','uncertain','continuation_reserved'}:
            resource = operation['resource']
            if resource in {'normal','special'} and page in {'st_race','st_result','st_race_detail'}:
                self._confirm_operation('same_owned_entry_progress')
            elif resource == 'clock' and (page in {'st_race','st_race_detail'}
                    or page == 'st_result' and (frame.get('outcome') == 'win' or operation['status']=='submitted')):
                self._confirm_operation('same_owned_retry_progress')
            elif (resource == 'clock' and operation['quantity']==1 and page == 'st_retry'
                    and frame.get('stock_before') == operation.get('stock_after')):
                self._confirm_operation('original_retry_stock_decrement_verified')
            elif (resource=='clock' and operation['quantity']==0 and page=='st_retry'
                    and type(operation.get('free_before')) is int
                    and frame.get('free_remaining')==operation['free_before']-1):
                self._confirm_operation('original_free_retry_count_verified')
            elif resource == 'jelly' and (type(frame.get('tickets')) is int
                    and type(operation.get('tickets_before')) is int
                    and frame['tickets'] >= operation['tickets_before']+operation['quantity']):
                self._confirm_operation('ticket_increment_verified')
            elif resource == 'jelly' and page == 'st_refill' and frame.get('stock_before') == operation.get('stock_after'):
                self._confirm_operation('original_jelly_stock_decrement_verified')
            elif resource == 'points' and (page == 'st_recruit_done' and frame.get('recruited') is True
                    or page=='st_grade_reward' and frame.get('team_total')==operation.get('team_after')
                       and type(frame.get('team_total')) is int):
                self._confirm_operation('native_recruit_completion')
            elif resource == 'claim' and page == 'st_claimed':
                self._confirm_operation('native_claimed_rewards')
        entry = self.data['pending_entry']
        if owned and entry and entry['status'] == 'submitted':
            if page in {'st_race','st_race_detail'}:
                entry['progress_seen'] = True
            if page == 'st_result' and frame.get('outcome') in {'win','loss'}:
                entry.update(progress_seen=True, result_seen=True, outcome=frame['outcome'])
            if entry['result_seen'] and page in {'st_scout','st_hub','st_opponents','st_recruit_done','st_special_done'}:
                self.data[entry['kind']+'_completed'] += 1
                self.data['last_completed_entry'] = deepcopy(entry)
                self.record('entry_completed', entry=entry, frame_sha256=frame.get('frame_sha256'))
                self.data['pending_entry'] = None
        if page == 'st_missions' and frame.get('mission_tab') == 'daily':
            self.data['mission_scan_at'] = self.data['normal_completed']
            if frame.get('daily_ticket_claims') == 5:
                self.data['daily_tickets_claimed'] = True
        if page == 'st_missions' and frame.get('mission_tab') == 'period':
            self.data['period_scan_at'] = self.data['normal_completed']
        if page=='st_closed' and frame.get('event_verified'):
            self.data['event_unavailable'] = True
        if (operation and operation.get('resource')=='normal' and operation.get('status')=='submitted'
                and entry and not entry['progress_seen']
                and (page=='st_refill' and frame.get('item')=='mini_jelly'
                     or page=='st_refill_prompt' and frame.get('recovery_needed_verified') is True)):
            # The native zero-ticket recovery dialog rejects this first entry.
            # Its ACK remains a real input; it is not rewritten as zero-input.
            self.record('entry_not_started', entry=entry, reason='native_ticket_recovery_before_race')
            self.data['normal_reserved'] -= 1
            self.data['normal_submitted'] -= 1
            self.data['pending_entry'] = None
            self.data['pending_operation'] = None
            self.data['refill_tickets_before']=0
        self.data['last_observation'] = deepcopy(frame)
        self.save()
        return self.data

    def failed(self, error):
        self.record('failure', error=str(error), action_outcome=getattr(error,'action_outcome',None))
        outcome = getattr(error,'action_outcome',None)
        if outcome:
            self.receipt(dict(action_outcome=outcome))
        else:
            self.save()

    @property
    def pending(self):
        return bool(self.data['pending_entry'] or self.data['pending_operation'])
