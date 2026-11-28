"""One account/day ledger for Racing Carnival, with durable intent and no unknown replay."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import uuid

from .options import normalize_options

ZONE = timezone(timedelta(hours=8))
RESOURCES = {
    'enter_race': 'race',
    'confirm_refill': 'jelly',
    'claim_all': 'claim',
}


def game_day(timestamp=None):
    value = datetime.now(ZONE) if timestamp is None else datetime.fromtimestamp(timestamp, ZONE)
    return (value - timedelta(hours=5)).date().isoformat()


def atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
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
        raise ValueError('racing_carnival_account_scope_required')
    i = indices[0]
    batch = Path(*parts[:i])
    if batch.parent.name != 'mfa-batches' or not (batch / 'selection.json').is_file():
        raise ValueError('racing_carnival_batch_scope_invalid')
    number = int(parts[i].split('-')[1])
    selection = json.loads((batch / 'selection.json').read_text(encoding='utf8'))
    accounts = selection['accounts']
    accounts = [int(x) for x in accounts.split(',')] if isinstance(accounts, str) else accounts
    if number not in accounts:
        raise ValueError('racing_carnival_account_not_selected')
    day = day or game_day()
    scope = dict(account='account-' + str(number), day=day, event='racing_carnival')
    ledger = batch.parent.parent / 'module-ledgers/racing_carnival' / scope['account'] / (day + '.json')
    prior_day_guard(ledger)
    return ledger, scope


def prior_day_guard(ledger):
    ledger = Path(ledger)
    if ledger.parent.exists():
        for old in ledger.parent.glob('*.json'):
            if old == ledger or not re.fullmatch(r'\d{4}-\d{2}-\d{2}\.json', old.name):
                continue
            prior = json.loads(old.read_text(encoding='utf8'))
            if prior.get('pending_entry') or prior.get('pending_operation') or prior.get('navigation_pending'):
                error = RuntimeError('racing_carnival_prior_day_pending')
                error.journal_path = str(old)
                raise error


def submitted_admission_loading(data):
    """Only the acknowledged original entry, before any progress."""
    entry = data.get('pending_entry') or {}
    op = data.get('pending_operation') or {}
    action = data.get('last_action') or {}
    ack = action.get('action_outcome') or {}
    kind = entry.get('kind')
    return (
        data.get('context_verified') is True
        and bool(entry.get('id'))
        and entry.get('id') == op.get('id') == action.get('id')
        and kind in {'normal', 'challenge'}
        and entry.get('status') == 'submitted'
        and entry.get('progress_seen') is False
        and entry.get('result_seen') is False
        and op.get('status') == 'submitted'
        and op.get('resource') == kind
        and op.get('action') == action.get('action') == 'enter_race'
        and op.get('source_page') == action.get('source_page') == 'carnival_lineup'
        and op.get('quantity') == 1
        and type(op.get('rp_before')) is int
        and op['rp_before'] > 0
        and ack.get('action_id') == 'enter_race'
        and ack.get('source_page') == 'carnival_lineup'
        and ack.get('execution_status') == 'executed'
        and ack.get('observed_page') in {'loading', 'carnival_playback_confirm', 'carnival_race_ready'}
        and ack.get('input_attempts') == 1
        and ack.get('acknowledged_inputs') == 1
        and bool(ack.get('source_frame_sha256'))
    )


class Journal:
    def __init__(self, path, scope, endpoint, options=None):
        self.path = Path(path).resolve()
        self.options = normalize_options(options)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding='utf8'))
            if (
                self.data.get('schema') != 'uma.racing_carnival.day.v1'
                or self.data.get('scope') != scope
                or self.data.get('endpoint') != endpoint
            ):
                raise ValueError('racing_carnival_journal_scope_mismatch')
            # Same-day catch-up with a higher max_races cap clears clean cap completion flags.
            if (
                (self.data.get('done') or self.data.get('exit_requested'))
                and self.data.get('races_reserved', 0) > 0
                and not self.capped()
                and not self.data.get('resources_exhausted')
                and not self.data.get('event_unavailable')
                and not self.data.get('exit_error')
                and not self.data.get('day_changed')
                and not self.pending
            ):
                self.data['done'] = False
                self.data['exit_requested'] = False
                self.save()
        else:
            self.data = dict(
                schema='uma.racing_carnival.day.v1',
                scope=deepcopy(scope),
                endpoint=endpoint,
                context_verified=False,
                pending_entry=None,
                pending_operation=None,
                races_reserved=0,
                races_submitted=0,
                races_completed=0,
                challenge_reserved=0,
                challenge_submitted=0,
                challenge_completed=0,
                jelly_reserved=0,
                jelly_confirmed=0,
                claims_confirmed=0,
                rotation_index=0,
                probed_tracks={},
                first_win_sweep_done=False,
                active_track=None,
                active_difficulty=None,
                current_is_challenge=False,
                mission_scan_at=-1,
                period_scan_at=-1,
                daily_claimed=False,
                period_claimed=False,
                races=[],
                records=[],
                done=False,
                event_unavailable=False,
                resources_exhausted=False,
                navigation_pending=False,
                last_observation={},
            )
            self.save()

    def save(self):
        atomic_write(self.path, self.data)

    def cap_remaining(self):
        max_races = self.options['max_races']
        if max_races == 0:
            return None
        return max(0, max_races - self.data['races_reserved'])

    def capped(self):
        max_races = self.options['max_races']
        return max_races > 0 and self.data['races_reserved'] >= max_races

    def record(self, kind, **fields):
        self.data['records'].append(dict(kind=kind, **deepcopy(fields)))

    def reserve(self, action, frame):
        candidate = frame['actions'][action]
        resource = RESOURCES.get(action)
        identity = uuid.uuid4().hex
        intent = dict(
            id=identity,
            action=action,
            source_page=frame['page'],
            source_frame_sha256=frame['frame_sha256'],
            resource=resource,
            status='reserved',
            quantity=1,
            basis=candidate.get('basis'),
        )
        if resource:
            if self.data['pending_operation']:
                raise RuntimeError('racing_carnival_operation_pending')
            if not self.data['context_verified']:
                raise RuntimeError('racing_carnival_context_unverified')
            if resource == 'race':
                if self.data['pending_entry']:
                    raise RuntimeError('racing_carnival_entry_pending')
                if self.capped():
                    raise RuntimeError('racing_carnival_daily_cap')
                if frame.get('items_empty') is not True:
                    raise RuntimeError('racing_carnival_boost_items_must_be_empty')
                rp = frame.get('rp')
                if type(rp) is not int or rp <= 0:
                    raise RuntimeError('racing_carnival_rp_unverified')
                is_challenge = bool(self.data.get('current_is_challenge'))
                kind = 'challenge' if is_challenge else 'normal'
                intent['resource'] = kind
                self.data['races_reserved'] += 1
                if is_challenge:
                    self.data['challenge_reserved'] += 1
                self.data['pending_entry'] = dict(
                    id=identity,
                    kind=kind,
                    track=self.data.get('active_track'),
                    difficulty=self.data.get('active_difficulty'),
                    status='reserved',
                    progress_seen=False,
                    result_seen=False,
                    outcome=None,
                    points_gained=None,
                )
            elif resource == 'jelly':
                quantity = candidate.get('quantity')
                if (
                    type(quantity) is not int
                    or quantity <= 0
                    or self.data['jelly_reserved'] + quantity > self.options['max_jellies']
                    or not self.options['allow_existing_jelly']
                ):
                    raise RuntimeError('racing_carnival_jelly_cap')
                intent['quantity'] = quantity
                self.data['jelly_reserved'] += quantity
            intent.update(
                stock_before=candidate.get('stock_before'),
                stock_after=candidate.get('stock_after'),
                rp_before=frame.get('rp') if frame.get('rp') is not None else self.data.get('refill_rp_before'),
                mission_tab=frame.get('mission_tab'),
            )
            self.data['pending_operation'] = deepcopy(intent)
        self.data['last_action'] = deepcopy(intent)
        self.record('intent', **intent)
        self.save()
        return identity

    def _release_zero_input(self, operation):
        resource = operation['resource']
        quantity = operation['quantity']
        if resource in {'normal', 'challenge'}:
            entry = self.data['pending_entry']
            if not entry or entry['id'] != operation['id'] or entry['status'] != 'reserved':
                raise RuntimeError('racing_carnival_zero_input_owner_mismatch')
            self.data['races_reserved'] -= 1
            if resource == 'challenge':
                self.data['challenge_reserved'] -= 1
            self.data['pending_entry'] = None
        elif resource == 'jelly':
            self.data['jelly_reserved'] -= quantity
        self.record('zero_input_release', operation=operation)
        self.data['pending_operation'] = None

    def receipt(self, receipt):
        outcome = receipt.get('action_outcome') or {}
        action = self.data.get('last_action') or {}
        if action:
            self.data['last_action']['action_outcome'] = deepcopy(outcome)
        if (
            action.get('action') == 'open_carnival'
            and action.get('basis') == 'named_carnival_event_card'
            and outcome.get('execution_status') == 'executed'
            and outcome.get('input_attempts') == outcome.get('acknowledged_inputs') == 1
        ):
            self.data['context_verified'] = True
        compact = {k: v for k, v in receipt.items() if k != 'observation'}
        observed = receipt.get('observation') or {}
        compact['observed'] = {
            k: observed[k]
            for k in [
                'page', 'reason', 'endpoint', 'frame_sha256', 'screenshot_path',
                'rp', 'outcome', 'points_gained', 'stock_before', 'stock_after',
                'quantity', 'mission_tab', 'claimable', 'first_win_claimed',
                'selected_difficulty', 'is_challenge', 'items_empty',
            ]
            if k in observed
        }
        self.record('receipt', intent_id=action.get('id'), receipt=compact)
        operation = self.data['pending_operation']
        if (
            action.get('action') == 'resume_race'
            and not self.data['pending_entry']
            and outcome.get('execution_status') == 'executed'
            and outcome.get('input_attempts') == outcome.get('acknowledged_inputs') == 1
        ):
            self.data['pending_entry'] = dict(
                id=action['id'],
                kind='challenge' if self.data.get('current_is_challenge') else 'normal',
                track=self.data.get('active_track'),
                difficulty=self.data.get('active_difficulty'),
                status='submitted',
                progress_seen=False,
                result_seen=False,
                outcome=None,
                points_gained=None,
                resumed_existing=True,
            )
        if operation and operation['id'] == action.get('id'):
            if (
                outcome.get('execution_status') == 'not_executed'
                and outcome.get('input_attempts') == outcome.get('acknowledged_inputs') == 0
            ):
                self._release_zero_input(operation)
            elif (
                outcome.get('execution_status') == 'executed'
                and outcome.get('input_attempts') == outcome.get('acknowledged_inputs') == 1
            ):
                operation['status'] = 'submitted'
                if operation['resource'] in {'normal', 'challenge'}:
                    self.data['pending_entry']['status'] = 'submitted'
                    self.data['races_submitted'] += 1
                    if operation['resource'] == 'challenge':
                        self.data['challenge_submitted'] += 1
            else:
                operation['status'] = 'uncertain'
                if self.data['pending_entry'] and operation['resource'] in {'normal', 'challenge'}:
                    self.data['pending_entry']['status'] = 'uncertain'
        self.save()
        if isinstance(receipt.get('observation'), dict):
            self.observe(receipt['observation'])

    def _confirm_operation(self, reason):
        operation = self.data['pending_operation']
        if not operation:
            return
        resource = operation['resource']
        if resource == 'jelly':
            self.data['jelly_confirmed'] += operation['quantity']
        elif resource == 'claim':
            self.data['claims_confirmed'] += 1
        elif resource in {'normal', 'challenge'} and operation['status'] == 'uncertain':
            self.data['races_submitted'] += 1
            if resource == 'challenge':
                self.data['challenge_submitted'] += 1
            if self.data['pending_entry']:
                self.data['pending_entry']['status'] = 'submitted'
        self.record('operation_verified', operation=operation, reason=reason)
        self.data['pending_operation'] = None

    def observe(self, frame):
        if frame.get('endpoint') != self.data['endpoint']:
            raise ValueError('racing_carnival_observation_endpoint_mismatch')
        page = frame.get('page')
        if frame.get('event_verified'):
            self.data['context_verified'] = True
        operation = self.data['pending_operation']
        entry = self.data['pending_entry']
        owned = self.data['context_verified']
        progress_pages = {
            'carnival_playback_confirm',
            'carnival_race_ready',
            'carnival_race',
            'carnival_result',
            'carnival_points',
            'carnival_rewards',
            'carnival_factor',
            'carnival_race_end',
        }
        result_pages = {
            'carnival_result',
            'carnival_points',
            'carnival_rewards',
            'carnival_factor',
            'carnival_race_end',
        }
        if owned and operation and operation['status'] in {'submitted', 'uncertain'}:
            resource = operation['resource']
            if resource in {'normal', 'challenge'} and page in progress_pages:
                self._confirm_operation('same_owned_entry_progress')
            elif resource == 'jelly' and (
                (
                    type(frame.get('rp')) is int
                    and type(operation.get('rp_before')) is int
                    and frame['rp'] >= operation['rp_before'] + operation['quantity']
                )
                or page == 'carnival_refill_done'
                or (
                    page == 'carnival_refill_qty'
                    and frame.get('stock_before') is not None
                    and frame.get('stock_before') == operation.get('stock_after')
                )
            ):
                self._confirm_operation('native_rp_refill_verified')
            elif resource == 'claim' and (
                page == 'carnival_claimed'
                or (page == 'carnival_missions' and frame.get('claimable') == 0 and operation['status'] == 'submitted')
            ):
                self._confirm_operation('native_claimed_rewards')

        entry = self.data['pending_entry']
        if owned and entry and entry['status'] == 'submitted':
            if page in progress_pages:
                entry['progress_seen'] = True
            if page in result_pages:
                entry['progress_seen'] = True
                entry['result_seen'] = True
            if page == 'carnival_result' and frame.get('outcome') in {'win', 'loss'}:
                entry['outcome'] = frame['outcome']
            if page == 'carnival_points' and type(frame.get('points_gained')) is int:
                entry['points_gained'] = frame['points_gained']
            if (
                entry['result_seen']
                and page in {'carnival_hub'}
                and 'resume_race' not in (frame.get('actions') or {})
            ):
                if entry.get('resumed_existing'):
                    self.record('resumed_entry_settled', entry=entry, frame_sha256=frame.get('frame_sha256'))
                else:
                    self.data['races_completed'] += 1
                    if entry['kind'] == 'challenge':
                        self.data['challenge_completed'] += 1
                    else:
                        self.data['rotation_index'] = (self.data.get('rotation_index', 0) + 1) % 4
                        track = entry.get('track')
                        if (
                            self.options['track_strategy'] == 'first_win_then_target'
                            and entry.get('outcome') == 'win'
                            and track in (1, 2, 3, 4)
                        ):
                            probed = dict(self.data.get('probed_tracks') or {})
                            probed[str(track)] = True
                            self.data['probed_tracks'] = probed
                            if all(probed.get(str(t)) is True for t in (1, 2, 3, 4)):
                                self.data['first_win_sweep_done'] = True
                    summary = dict(
                        id=entry['id'],
                        kind=entry['kind'],
                        track=entry.get('track'),
                        difficulty=entry.get('difficulty'),
                        outcome=entry.get('outcome'),
                        points_gained=entry.get('points_gained'),
                    )
                    self.data['races'].append(summary)
                    self.record('entry_completed', entry=entry, frame_sha256=frame.get('frame_sha256'))
                self.data['pending_entry'] = None
                self.data['active_track'] = None
                self.data['active_difficulty'] = None
                self.data['current_is_challenge'] = False

        if owned and page == 'carnival_detail':
            is_ch = bool(frame.get('is_challenge'))
            self.data['current_is_challenge'] = is_ch
            if frame.get('selected_difficulty') in {'困难', '普通', '简单'}:
                self.data['active_difficulty'] = frame['selected_difficulty']
            if not is_ch and self.data.get('active_track') in (1, 2, 3, 4):
                fw = frame.get('first_win_claimed')
                if fw is not None:
                    probed = dict(self.data.get('probed_tracks') or {})
                    probed[str(self.data['active_track'])] = bool(fw)
                    self.data['probed_tracks'] = probed
                    if all(probed.get(str(t)) is True for t in (1, 2, 3, 4)):
                        self.data['first_win_sweep_done'] = True

        if owned and page == 'carnival_missions':
            if frame.get('mission_tab') == 'daily':
                self.data['mission_scan_at'] = self.data['races_completed']
                if frame.get('claimable') == 0:
                    self.data['daily_claimed'] = True
            elif frame.get('mission_tab') == 'period':
                self.data['period_scan_at'] = self.data['races_completed']
                if frame.get('claimable') == 0:
                    self.data['period_claimed'] = True

        if page == 'carnival_closed' and frame.get('event_verified'):
            self.data['event_unavailable'] = True

        # Reject/rollback unstarted admission if native RP recovery popped before race began.
        if (
            operation
            and operation.get('resource') in {'normal', 'challenge'}
            and operation.get('status') == 'submitted'
            and entry
            and not entry['progress_seen']
            and (
                (page == 'carnival_refill_select' and frame.get('item') == 'mini_jelly')
                or (page == 'carnival_refill_prompt' and frame.get('recovery_needed_verified') is True)
            )
        ):
            self.record('entry_not_started', entry=entry, reason='native_rp_recovery_before_race')
            self.data['races_reserved'] -= 1
            self.data['races_submitted'] -= 1
            if operation.get('resource') == 'challenge':
                self.data['challenge_reserved'] -= 1
                self.data['challenge_submitted'] -= 1
            self.data['pending_entry'] = None
            self.data['pending_operation'] = None
            self.data['refill_rp_before'] = 0

        self.data['last_observation'] = deepcopy(frame)
        self.save()
        return self.data

    def failed(self, error):
        self.record('failure', error=str(error), action_outcome=getattr(error, 'action_outcome', None))
        outcome = getattr(error, 'action_outcome', None)
        if outcome:
            self.receipt(dict(action_outcome=outcome))
        else:
            self.save()

    @property
    def pending(self):
        return bool(self.data['pending_entry'] or self.data['pending_operation'])
