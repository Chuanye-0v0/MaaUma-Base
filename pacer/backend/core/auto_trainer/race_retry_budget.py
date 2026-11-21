"""One career's clock-item budget, driven by the existing race input receipts."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import uuid

from core.auto_trainer.pacer_storage import _io, _rglob

DEFAULT_MAX_CLOCK_USES = 3
SCHEMA = 'larc.race_clock_budget.v1'


def normalize(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or type(value.get('enabled')) is not bool
            or type(value.get('max_clock_uses', DEFAULT_MAX_CLOCK_USES)) is not int
            or not 1 <= value.get('max_clock_uses', DEFAULT_MAX_CLOCK_USES) <= 5):
        raise ValueError('race_retry_requires_enabled_and_1_to_5_clock_uses')
    return {'enabled': value['enabled'],
            'max_clock_uses': value.get('max_clock_uses', DEFAULT_MAX_CLOCK_USES)}


def save_goal_option(run_root, value):
    """Only an explicit new option changes a goal; absent legacy options do not."""
    config = normalize(value)
    if config is None:
        return
    from core.auto_trainer.cut_farming import atomic_json
    path = Path(run_root) / 'goal.json'
    goal = json.loads(_io(path).read_text(encoding='utf-8-sig'))
    goal['race_retry'] = config
    atomic_json(path, goal)


def _prior_uses(root, endpoint):
    """Recover script-observed paid retries, deduplicating resumed reports."""
    paths = {root / 'session/larc_race_checkpoint.json'}
    for path in _rglob(root, 'summary.json'):
        try:
            summary = json.loads(_io(path).read_text(encoding='utf-8-sig'))
            if not isinstance(summary,dict) or not isinstance(summary.get('race_reports',[]),list):continue
            for row in summary.get('race_reports', []):
                if not isinstance(row,dict):continue
                item = Path(row.get('path', '')).resolve()
                if item.is_relative_to(root.resolve()):
                    paths.add(item)
        except (OSError, ValueError, TypeError):
            continue
    receipts = set()
    baseline = 0
    for path in paths:
        try:
            report = json.loads(_io(path).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            continue
        if not isinstance(report,dict) or report.get('run_session_id') != root.name or report.get('device') != endpoint:
            continue
        if path == root / 'session/larc_race_checkpoint.json':
            count = report.get('clock_items_committed', 0)
            if type(count) is int and count > 0:
                baseline = count
        steps=report.get('step_reports', report.get('steps', []))
        if not isinstance(steps,list):continue
        for step in steps:
            if not isinstance(step,dict):continue
            result = step.get('result', {})
            if not isinstance(result,dict):continue
            decision = result.get('decision', {})
            if not isinstance(decision,dict):continue
            proof = decision.get('evidence', {}).get('clock_retry', {})
            if (result.get('performed') is not True or decision.get('reason') != 'retry_lost_race'
                    or type(proof.get('before')) is not int or proof.get('after') != proof['before'] - 1):
                continue
            image = decision.get('evidence', {}).get('image')
            receipts.add((image, report.get('turn_index'), proof.get('remaining'), proof['before'], proof['after']))
    return max(len(receipts), baseline)


class ClockBudget:
    def __init__(self, run_root, config):
        self.root = Path(run_root)
        self.config = normalize(config)
        progress = json.loads(_io(self.root / 'progress.json').read_text(encoding='utf-8-sig'))
        self.run_id, self.endpoint = self.root.name, progress['device']
        self.turn_index = progress.get('next_turn')
        if progress.get('run_session_id') != self.run_id:
            raise ValueError('race_clock_budget_run_mismatch')
        self.path = self.root / 'session/race_clock_budget.json'
        self.tracking_warning = None
        self.writable = True
        self.data = None
        if _io(self.path).is_file():
            try:
                self.data = json.loads(_io(self.path).read_text(encoding='utf-8-sig'))
                if (not isinstance(self.data,dict) or self.data.get('schema_version') != SCHEMA or self.data.get('run_session_id') != self.run_id
                        or self.data.get('device') != self.endpoint or not isinstance(self.data.get('entries'), dict)
                        or type(self.data.get('initial_clock_uses')) is not int or self.data['initial_clock_uses'] < 0
                        or any(not isinstance(row,dict) or type(row.get('cost')) is not int or row['cost'] not in (0,1)
                            or row.get('outcome') not in {'pending','confirmed','unknown','failed'}
                            for row in self.data['entries'].values())):
                    raise ValueError('race_clock_budget_context_mismatch')
            except (OSError, ValueError):
                if self.config is not None:raise
                self.data=None;self.writable=False;self.tracking_warning='legacy_accounting_unavailable'
        if self.data is None:
            # A legacy race already in progress may carry a confirmed local
            # total. New runs start at zero; subsequent races share this file.
            count = _prior_uses(self.root, self.endpoint)
            self.data = {'schema_version': SCHEMA, 'run_session_id': self.run_id,
                         'device': self.endpoint, 'initial_clock_uses': count, 'entries': {}}

    @property
    def used(self):
        return self.data.get('initial_clock_uses', 0) + sum(
            row['cost'] for row in self.data['entries'].values()
            if row.get('outcome') in {'pending', 'unknown', 'failed', 'confirmed'})

    @property
    def remaining(self):
        if self.config is None:
            return None
        return max(0, self.config['max_clock_uses'] - self.used) if self.config['enabled'] else 0

    @property
    def unresolved(self):
        return any(row.get('outcome') in {'pending','unknown','failed'} and not row.get('resolved')
                   for row in self.data['entries'].values())

    @property
    def retry_pending(self):
        return self.config is not None and self.unresolved

    def observe_page(self, decision, packet):
        """Resolve uncertainty from an existing typed successor observation.

        A changed retry image or the same losing result is not a successor.
        Resolving the input never refunds its reserved item count.
        """
        if not self.unresolved or not isinstance(decision,dict) or not isinstance(packet,dict):
            return False
        from core.auto_trainer.race_clock_retry import retry_prompt_visible
        if (packet.get('text_status')!='ok' or packet.get('dangerous') is True
                or not packet.get('source_image_sha256') or retry_prompt_visible(packet.get('visible_text',''))):
            return False
        identity=(decision.get('reason'),decision.get('route_state'))
        successors={('ready_main_menu','main_menu'),('ready_main_menu','ready_main_menu'),
            ('ready_training_menu','training_menu'),('ready_training_menu','ready_training_menu'),
            ('race_day_main_menu','race_day_main_menu'),('enable_bottom_skip_toggle','main_menu'),
            ('race_scene_running','race_scene'),('skip_or_advance_race_scene','race_scene'),
            ('race_scene_start_or_continue','race_scene'),('view_race_result','race_preview'),
            ('go_to_race_from_detail','race_detail')}
        if identity not in successors:
            return False
        for row in self.data['entries'].values():
            if row.get('outcome') in {'pending','unknown','failed'} and not row.get('resolved'):
                row['resolved']=True
                row['resolution']={'reason':identity[0],'route_state':identity[1],
                    'source_sha256':packet['source_image_sha256']}
        self._save(strict=False)
        return True

    def observe(self, receipt, decision):
        if not isinstance(decision,dict) or decision.get('reason') != 'retry_lost_race':
            return
        tap=decision.get('tap') or {}
        if (tap.get('coordinate_name')!='race.clock_retry'
                or any(receipt.get(key)!=tap.get(key) for key in ('x','y','scaled'))):
            return
        proof = decision.get('evidence', {}).get('clock_retry', {})
        before, after = proof.get('before'), proof.get('after')
        if type(before) is not int or type(after) is not int or before - after not in {0, 1}:
            raise ValueError('race_clock_budget_cost_unverified')
        cost = before - after
        identity = {k: receipt.get(k) for k in ('source_sha256', 'x', 'y', 'scaled')}
        identity['turn_index'] = self.turn_index
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        prior = self.data['entries'].get(key)
        new_input=receipt['outcome']=='pending'
        if new_input and self.retry_pending:
            raise ValueError('race_clock_input_outcome_pending')
        if prior is not None and new_input and self.config is not None:
            raise ValueError('race_clock_input_already_reserved')
        if prior is None:
            if self.config is not None and (not self.config['enabled'] or cost > self.remaining):
                raise ValueError('race_clock_budget_exhausted')
            self.data['entries'][key] = {'cost': cost, 'outcome': receipt['outcome']}
        elif prior['outcome'] != 'confirmed':
            prior['outcome'] = receipt['outcome']
        # A pending/uncertain send reserves the item across a crash. It is
        # never submitted again merely to find out whether it was consumed.
        self._save()

    def _save(self, *, strict=True):
        if not self.writable:return
        _io(self.path.parent).mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            _io(temporary).write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
            _io(temporary).replace(_io(self.path))
        except OSError:
            if self.config is not None and strict:
                raise
            if self.config is None:self.writable=False
            self.tracking_warning='clock_accounting_not_saved'
        finally:
            try:_io(temporary).unlink(missing_ok=True)
            except OSError:pass

    def snapshot(self):
        return {'clock_items_used_or_reserved': self.used, 'remaining': self.remaining,
                'config': deepcopy(self.config), 'tracking_warning':self.tracking_warning,
                'input_outcome_pending':self.retry_pending}
