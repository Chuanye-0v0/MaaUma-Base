"""Finite current-account seed farming, backed by the existing task owners.

No login, account switching, resource refill, or blind game inputs. A stopped
attempt is kept and explicitly resumed; a completed run never restarts itself.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
from core.factor_goals import seed_policy, targets_met, validate_targets
from core.harvest_presets import catalog, validate_snapshot
ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / 'runs/seed-cycle-settings.json'
DEFAULTS = dict(max_cycles=1, targets=[], total_tp_budget=0, total_jewel_budget=0, resume=False, adopt_existing=False, integrated=False)
REASONS = dict(operator_stop='已手动停止，进度已保存', seed_total_tp_budget_exhausted='本次TP总预算已用尽，未再次开种', seed_factor_result_incomplete='因子结果尚未完整核实，保留现场与进度', seed_crop_missing_not_a_completed_cycle='没有找到原育成，不计入已完成轮数', seed_current_account_changed='当前账号已改变，停止操作', seed_training_wait_timeout='等待成熟超过6小时，停止并保留进度', seed_uncertain_input_or_account_pause='存在未确认操作或账号暂停，需要先核实')

def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
    temporary.replace(path)

def settings(value, library=None, *, require_targets=True):
    library = library or catalog()
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('连续刷种马设置无效')
    result = {**DEFAULTS, **deepcopy(value)}
    for key, low, high in [('max_cycles', 1, 100), ('total_tp_budget', 0, 9999), ('total_jewel_budget', 0, 9999)]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('轮数或本次资源总上限无效')
    if any((type(result[key]) is not bool for key in ('resume', 'adopt_existing', 'integrated'))):
        raise ValueError('连续任务启动方式无效')
    if result['targets'] or (require_targets and (not result['integrated'])):
        result['targets'] = validate_targets(result['targets'], {r['key'] for r in library['factors']})
    elif result['targets'] != []:
        raise ValueError('目标因子无效')
    return result

def validate(options, library=None):
    from desktop.mfa_backend import validate_options
    if not isinstance(options, dict) or set(options) - {'cycle', 'plant', 'harvest'}:
        raise ValueError('seed_cycle_options_invalid')
    result = dict(cycle=settings(options.get('cycle', {}), library), plant=validate_options('plant', options.get('plant', {})), harvest=validate_options('harvest', options.get('harvest', {})))
    result['harvest']['return_home'] = False
    preset = result['harvest'].get('preset')
    if preset is None:
        preset = dict(version=1, skill_list=None, factor_profile=None, rerolls=0, tp_budget=0, jewel_budget=0)
    if preset['factor_profile'] is None:
        preset['factor_profile'] = dict(id='seed-goals', name='连续种马目标', star_weights=[1, 2, 3], weights=[dict(**row, weight=1) for row in result['cycle']['targets']])
    result['harvest']['preset'] = validate_snapshot(preset, library)
    return result

def editor_request(value):
    if not isinstance(value, dict) or value.get('action') not in {'load', 'save', 'preflight_start'}:
        raise ValueError('seed_editor_request_invalid')
    if value['action'] == 'preflight_start':
        return _public_preflight_start(value)
    library = catalog()
    stored = json.loads(SETTINGS.read_text(encoding='utf8')) if SETTINGS.exists() else DEFAULTS
    draft = settings(value['settings'] if value['action'] == 'save' else stored, library, require_targets=value['action'] == 'save')
    if value['action'] == 'save':
        _save(SETTINGS, draft)
    return dict(ok=True, results=[dict(settings=draft, factors=library['factors'])])

def _public_start_unresolved(value):
    if isinstance(value, list):
        return any((_public_start_unresolved(row) for row in value))
    if not isinstance(value, dict):
        return False
    flags = {'pending', 'pending_race', 'pending_exchange', 'pending_purchase', 'pending_confirmation', 'pending_refill', 'pending_spin', 'purchase_pending', 'await_selection', 'needs_comparison', 'account_conflict', 'account_paused', 'account_held', 'conflict_check_error', 'active_consumption', 'active_transaction', 'needs_reconciliation', 'unsettled'}
    protected = {'seed_account_requires_user', 'seed_uncertain_input_or_account_pause', 'account_connection_conflict'}
    for key, item in value.items():
        if key in flags and item is not None and (item is not False) and (item != {}):
            return True
        if key == 'execution_status' and item not in ('not_executed', 'executed', None):
            return True
        if key == 'decision' and (not isinstance(item, (str, type(None))) or item == 'pause_account'):
            return True
        if key == 'reason' and (not isinstance(item, (str, type(None))) or item in protected):
            return True
        if _public_start_unresolved(item):
            return True
    return False

def _public_start_input_evidence(value):
    if isinstance(value, list):
        return any((_public_start_input_evidence(row) for row in value))
    if not isinstance(value, dict):
        return False
    submitted = {'start_submitted', 'start_confirmed', 'finish_submitted', 'completion_submitted', 'factors_requested', 'factor_choice_submitted', 'purchase_submitted', 'confirmation_submitted', 'harvest_skill_attempted'}
    for key, item in value.items():
        if key in submitted and item:
            return True
        if key in {'input_attempts', 'acknowledged_inputs', 'device_actions', 'device_action_count', 'tp_spent', 'jewel_spent', 'carrots'}:
            if type(item) is not int or item != 0:
                return True
        if key == 'performed' and item is True:
            return True
        if key == 'status' and item == 'dispatch_intent':
            return True
        if key in {'request_id', 'finish_submission', 'start_cost'} and item:
            return True
        if _public_start_input_evidence(item):
            return True
    return False

def _public_start_navigation_proven(source, prior, context, documents, endpoint):
    from desktop.umaa_plant_setup_resume import proven
    if _public_start_input_evidence(context):
        return False
    records = prior.get('records')
    if not isinstance(records, list) or not records or any((not isinstance(row, dict) or not isinstance(row.get('receipt_path'), str) for row in records)):
        return False
    if not proven(source, endpoint):
        return False
    receipts = {Path(row['receipt_path']).resolve() for row in records}
    recognized = receipts | {source / name for name in ('adapter_result.json', 'backend_result.json', 'context.json', 'recovery/last-action.json')}
    for path, value in documents:
        if path.resolve() not in recognized and (_public_start_input_evidence(value) or any((word in path.name.lower() for word in ('intent', 'ledger', 'submission')))):
            return False
    return True

def _public_fresh_start_guard(engine, state, directory):
    """Read only the old current owner before admitting a different new plan."""
    reason = 'seed_new_start_previous_transaction_unverified'
    if not isinstance(state, dict) or type(state.get('version')) is not int or state['version'] != 1 or (state.get('endpoint') != engine.endpoint) or (state.get('status') not in {'running', 'paused', 'completed', 'ended'}) or (not isinstance(state.get('config'), dict)) or (not isinstance(state.get('history'), list)) or any((type(state.get(key)) is not int or state[key] < 0 for key in ('completed_cycles', 'tp_spent', 'jewel_spent'))):
        raise RuntimeError('seed_new_start_previous_journal_unverified')
    if _public_start_unresolved(state):
        raise RuntimeError(reason)
    current = state.get('current')
    if not isinstance(current, dict) or type(current.get('number')) is not int or current['number'] < 1 or (current.get('phase') not in {'plant', 'wait', 'harvest'}):
        raise RuntimeError('seed_new_start_previous_owner_unverified')
    attempt = current.get('attempt')
    if attempt is None:
        return
    if not isinstance(attempt, dict) or not isinstance(attempt.get('output'), str):
        raise RuntimeError('seed_new_start_previous_owner_unverified')
    task = 'plant' if current.get('phase') == 'plant' else 'harvest'
    source = Path(attempt['output']).resolve()
    if not source.is_relative_to(directory.resolve()) or source.name != task or source.parent.parent.name != 'tasks' or (not source.parent.name.isdigit()):
        raise RuntimeError('seed_new_start_previous_owner_unverified')

    def read(path):
        value = json.loads(path.read_text(encoding='utf8'))
        if not isinstance(value, dict):
            raise RuntimeError(reason)
        return value
    try:
        result_path = source / 'adapter_result.json'
        prior = read(result_path) if result_path.exists() else None
        context_path = source / 'context.json'
        context = read(context_path) if context_path.exists() else {}
        boundary_path = source / 'recovery/last-action.json'
        boundary = read(boundary_path) if boundary_path.exists() else {}
        if any((_public_start_unresolved(row) for row in (prior, context, boundary))):
            raise RuntimeError(reason)
        if prior is not None:
            if prior.get('current_account_only') is not True:
                raise RuntimeError('seed_new_start_previous_owner_unverified')
            observed = prior.get('last_observation') or {}
            if not isinstance(observed, dict) or observed.get('endpoint', engine.endpoint) != engine.endpoint:
                raise RuntimeError('seed_new_start_previous_owner_unverified')
            for key in ('action_outcome', 'factor_selection', 'tp_refill'):
                if key in prior and prior[key] is not None and (not isinstance(prior[key], dict)):
                    raise RuntimeError(reason)
        documents = []
        for path in source.rglob('*.json'):
            if not path.resolve().is_relative_to(source):
                raise RuntimeError('seed_new_start_previous_owner_unverified')
            value = json.loads(path.read_text(encoding='utf8'))
            if not isinstance(value, (dict, list)):
                raise RuntimeError(reason)
            documents.append((path, value))
        if any((_public_start_unresolved(row) for _, row in documents)):
            raise RuntimeError(reason)
        if prior is not None and engine._verified(task, prior):
            return
        evidence = any((_public_start_input_evidence(row) for _, row in documents))
        evidence = evidence or any((path.name.endswith('-receipt.json') or any((word in path.name.lower() for word in ('intent', 'ledger', 'submission'))) for path, _ in documents))
        if prior is None:
            if evidence:
                raise RuntimeError(reason)
            return
        if prior.get('device_actions') == 0 and type(prior.get('device_actions')) is int and (prior.get('device_action_count_complete') is True) and (prior.get('records') == []) and (not evidence):
            return
        if task == 'plant' and _public_start_navigation_proven(source, prior, context, documents, engine.endpoint):
            return
        raise RuntimeError(reason)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(reason) from error

def _public_preflight_start(value):
    """Selected-connection, file-only admission before any queued game task."""
    from desktop.umaa_public_context import bind, endpoint
    from types import SimpleNamespace, MethodType
    if set(value) != {'action', 'connection'}:
        raise ValueError('seed_start_preflight_request_invalid')
    bind(value['connection'])
    selected = endpoint()
    root = Path(ROOT).resolve()
    scope = (root / 'runs/seed-cycles' / selected.rpartition(':')[2]).resolve()
    if not scope.is_relative_to(root):
        raise RuntimeError('seed_journal_scope_invalid')
    latest = scope / 'latest.json'
    if not latest.exists():
        return dict(ok=True, results=[dict(ready=True)])
    if not latest.is_file() or not latest.resolve().is_relative_to(scope):
        raise RuntimeError('seed_journal_scope_invalid')
    saved = json.loads(latest.read_text(encoding='utf8'))
    if not isinstance(saved, dict) or set(saved) != {'directory'} or (not isinstance(saved.get('directory'), str)):
        raise RuntimeError('seed_journal_scope_invalid')
    supplied = Path(saved['directory'])
    directory = supplied.resolve()
    if not supplied.is_absolute() or directory.parent != scope or (not directory.name.isdigit()) or (not directory.is_dir()):
        raise RuntimeError('seed_journal_scope_invalid')
    state_path = directory / 'state.json'
    if not state_path.is_file() or not state_path.resolve().is_relative_to(directory):
        raise RuntimeError('seed_journal_scope_invalid')
    state = json.loads(state_path.read_text(encoding='utf8'))
    engine = SimpleNamespace(endpoint=selected)
    engine._verified = MethodType(SeedCycle._verified, engine)
    _public_fresh_start_guard(engine, state, directory)
    return dict(ok=True, results=[dict(ready=True)])

class SeedCycle:

    def __init__(self, root, *, endpoint, adb_path, skill_database, executor=None, identity=None, clock=time.time, sleeper=None):
        from desktop.mfa_backend import execute_current
        self.root, self.endpoint, self.adb_path = (Path(root), endpoint, adb_path)
        self.skill_database = skill_database
        self.executor = executor or execute_current
        self.clock, self.sleeper = (clock, sleeper)

    def run(self, options, emit, stop, *, execute=False):
        options = validate(options)
        if not execute:
            return dict(ok=True, dry_run=True, game_task_complete=False, device_actions=0, kind='seed_cycle', options=options)
        from core.device_occupancy import hold, DeviceBusy
        from desktop.umaa_public_context import lock_root, normalize_endpoint
        if normalize_endpoint(self.endpoint) != self.endpoint:
            raise ValueError('seed_selected_endpoint_not_canonical')
        try:
            with hold(lock_root(), self.endpoint, 'umaa_seed_cycle'):
                return self._run(options, emit, stop)
        except DeviceBusy:
            return dict(ok=False, kind='seed_cycle', game_task_complete=False, reason='device_busy')

    def _run(self, options, emit, stop):
        scope = self.root / self.endpoint.rpartition(':')[2]
        latest = scope / 'latest.json'
        saved = json.loads(latest.read_text(encoding='utf8')) if latest.exists() else None
        config = deepcopy(options)
        resume = config['cycle'].pop('resume')
        if saved:
            directory = Path(saved['directory']).resolve()
            if directory.parent != scope.resolve() or not directory.name.isdigit():
                raise RuntimeError('seed_journal_scope_invalid')
            state = json.loads((directory / 'state.json').read_text(encoding='utf8'))
            if state.get('endpoint') != self.endpoint:
                raise RuntimeError('seed_endpoint_changed')
            if resume:
                old = deepcopy(state['config'])
                old['cycle'].setdefault('integrated', False)
                compatible = deepcopy(old)
                compatible['cycle']['integrated'] = config['cycle']['integrated']
                compatible['cycle']['adopt_existing'] = config['cycle']['adopt_existing']
                if compatible == config:
                    state['config'] = config
                elif state['config'] != config:
                    raise ValueError('本次连续任务的设置已固定；继续时请沿用原设置')
                if state['status'] in {'completed', 'ended'}:
                    return self._summary(state, directory)
            else:
                _public_fresh_start_guard(self, state, directory)
        if not saved or not resume:
            if not config['cycle']['integrated'] and config['cycle']['total_tp_budget'] == 0 and (not config['cycle']['adopt_existing']):
                raise ValueError('本次TP总上限为0，尚未授权开种；请设置TP总上限并保存')
            directory = scope / str(time.time_ns())
            state = dict(version=1, endpoint=self.endpoint, config=config, status='running', completed_cycles=0, tp_spent=0, jewel_spent=0, history=[], current=dict(number=1, phase='plant'))
            _save(directory / 'state.json', state)
            _save(latest, dict(directory=str(directory)))
        cycle = state['config']['cycle']

        def save():
            _save(directory / 'state.json', state)

        def progress(message):
            emit(dict(type='progress', phase='seed_cycle', message=f"种马 {state['current']['number']}/{cycle['max_cycles']}：{message}"))
        try:
            state['status'] = 'running'
            save()
            while state['completed_cycles'] < cycle['max_cycles']:
                if stop.is_set():
                    raise RuntimeError('operator_stop')
                current = state['current']
                task = 'plant' if current['phase'] == 'plant' else 'harvest'
                if current['phase'] == 'wait':
                    due = current.get('due_at', 0)
                    while self.clock() < due:
                        delay = min(5, due - self.clock())
                        if self.sleeper:
                            self.sleeper(delay)
                        elif stop.wait(delay):
                            raise RuntimeError('operator_stop')
                        if stop.is_set():
                            raise RuntimeError('operator_stop')
                    if self.clock() > current['wait_deadline']:
                        raise RuntimeError('seed_training_wait_timeout')
                attempt = current.get('attempt')
                if attempt:
                    source = Path(attempt['output']).resolve()
                    if not source.is_relative_to(directory.resolve()) or source.name != task or source.parent.parent.name != 'tasks':
                        raise RuntimeError('seed_attempt_scope_invalid')
                    result_path = source / 'adapter_result.json'
                    prior = json.loads(result_path.read_text(encoding='utf8')) if result_path.exists() else None
                    if prior and self._verified(task, prior):
                        result = prior
                    else:
                        result = self._attempt(state, directory, task, attempt['options'], source, prior, emit, stop)
                else:
                    child = deepcopy(state['config'][task])
                    if task == 'harvest':
                        preset = child['preset']
                        if not cycle['integrated']:
                            preset['tp_budget'] = min(preset['tp_budget'], cycle['total_tp_budget'] - state['tp_spent'])
                            preset['jewel_budget'] = min(preset['jewel_budget'], cycle['total_jewel_budget'] - state['jewel_spent'])
                    source = directory / 'tasks' / str(time.time_ns()) / task
                    if task == 'harvest':
                        current['phase'] = 'harvest'
                    current['attempt'] = dict(output=str(source), options=child)
                    save()
                    progress('开种' if task == 'plant' else '检查成熟育成／收菜与因子')
                    result = self._attempt(state, directory, task, child, source, None, emit, stop)
                source = Path(result.get('output_dir', source))
                self._account_cost(state, task, source, result)
                collected_during_plant = task == 'plant' and result.get('submitted_training_collected') is True
                if collected_during_plant:
                    self._account_cost(state, 'harvest', source, result)
                save()
                if stop.is_set():
                    raise RuntimeError('operator_stop')
                if result.get('account_conflict') or result.get('account_paused') or result.get('action_outcome', {}).get('decision') == 'pause_account':
                    raise RuntimeError('seed_account_requires_user')
                if task == 'plant' and self._verified(task, result) and (not collected_during_plant):
                    if cycle['integrated'] and cycle['max_cycles'] == 1:
                        state.update(status='completed', reason='single_plant_complete', target_met=False)
                        save()
                        return self._summary(state, directory)
                    if result.get('skipped') and (not (cycle['adopt_existing'] and current['number'] == 1)):
                        raise RuntimeError('已有育成已保留；请先收菜或明确勾选使用现有育成')
                    current.update(phase='wait', due_at=self.clock(), wait_deadline=self.clock() + 6 * 3600)
                    current.pop('attempt')
                    save()
                    continue
                if collected_during_plant:
                    task = 'harvest'
                if task == 'harvest' and result.get('ok') and result.get('skipped'):
                    if result.get('skip_reason') != 'still_running':
                        raise RuntimeError('seed_crop_missing_not_a_completed_cycle')
                    seconds = result.get('remaining_seconds')
                    if type(seconds) is not int or seconds <= 0:
                        raise RuntimeError('seed_training_time_unverified')
                    current.update(phase='wait', due_at=self.clock() + min(seconds, 300))
                    current.pop('attempt')
                    save()
                    progress(f'育成仍在进行，约 {max(1, seconds // 60)} 分钟；到期再核实')
                    continue
                if not self._verified(task, result):
                    raise RuntimeError(result.get('reason') or 'seed_child_not_verified')
                factors = result.get('factor_selection') or {}
                retained = factors.get('retained') or {}
                if factors.get('done') is not True or factors.get('pending') or factors.get('await_selection') or factors.get('needs_comparison') or (not retained.get('rows')) or (not retained.get('frame_sha256')):
                    raise RuntimeError('seed_factor_result_incomplete')
                hit = bool(cycle['targets'] and targets_met(retained['rows'], cycle['targets']))
                state['history'].append(dict(number=current['number'], harvest_source=str(source), target_met=hit, factors=deepcopy(retained)))
                state['completed_cycles'] += 1
                current.pop('attempt')
                if hit or state['completed_cycles'] == cycle['max_cycles']:
                    state.update(status='completed', reason='target_factors_met' if hit else 'max_cycles_reached', target_met=hit)
                    save()
                    if hit:
                        self._notify_factor_hit(state, directory)
                    progress('目标因子全部达标，停止开种' if hit else '已达到最大轮数，停止开种')
                    return self._summary(state, directory)
                state['current'] = dict(number=state['completed_cycles'] + 1, phase='plant')
                save()
            raise RuntimeError('seed_progress_invalid')
        except (RuntimeError, ValueError, OSError, KeyError, TypeError) as error:
            state.update(status='paused', reason=str(error))
            if str(error) == 'seed_total_tp_budget_exhausted' and state['current']['phase'] == 'plant':
                from desktop.umaa_plant_setup_resume import proven
                attempt = state['current'].get('attempt')
                if attempt and proven(attempt['output'], self.endpoint):
                    state['status'] = 'ended'
            save()
            progress('已停止，进度和原回执保留：' + REASONS.get(str(error), str(error)))
            return self._summary(state, directory)

    def _verified(self, task, result):
        if result.get('ok') is not True or result.get('dry_run') or result.get('pending'):
            return False
        page = result.get('last_observation', {})
        if page.get('page') != 'home' or page.get('endpoint') != self.endpoint:
            return False
        if task == 'plant':
            return result.get('reason') in {'started_home_verified', 'existing_training_preserved'}
        regular = result.get('reason') in {'harvest_home_verified', 'harvest_and_formation_home_verified'}
        plant_collected = result.get('submitted_training_collected') is True and result.get('reason') == 'started_home_verified' and any((r.get('action_id') == 'close_completed_training' and r.get('performed') is True and (r.get('endpoint') == self.endpoint) for r in result.get('records', [])))
        return not result.get('skipped') and (regular or plant_collected)

    def _resume_operation_verifier(self, state, directory, task, source, prior, stop):
        """Selected-device operation evidence, never a Trainer-ID assertion."""
        directory, source = (Path(directory).resolve(), Path(source).resolve())
        endpoint = self.endpoint
        snapshot = deepcopy(state['current'].get('attempt'))
        prior = deepcopy(prior)
        context = json.loads((source / 'context.json').read_text(encoding='utf8'))

        def verify():
            try:
                if stop.is_set() or self.endpoint != endpoint or (not isinstance(snapshot, dict)) or (Path(snapshot.get('output', '')).resolve() != source) or (source.name != task) or (not source.parent.name.isdigit()) or (source.parent.parent != directory / 'tasks') or (json.loads((source / 'adapter_result.json').read_text(encoding='utf8')) != prior) or (json.loads((source / 'context.json').read_text(encoding='utf8')) != context):
                    return False
                if task == 'plant':
                    from desktop.mfa_plant_resume import submission_source
                    actual = submission_source(prior, directory, endpoint)
                    if actual is None or actual.resolve().parent.parent.parent != directory / 'tasks':
                        return False
                    from core.autonomous_training.runner import load_submission
                    operation = load_submission(actual, endpoint)
                    return context.get('request_id') in (None, operation['request_id']) and (not context.get('prior_submission') or Path(context['prior_submission']).resolve() == actual.resolve())
                from desktop.mfa_harvest_restart import completion_receipt, completion_submission_receipt
                owner = Path(prior.get('harvest_output_dir') or source).resolve()
                if owner.name != 'harvest' or not owner.parent.name.isdigit() or owner.parent.parent != directory / 'tasks':
                    return False
                try:
                    completion_receipt(prior, owner, endpoint)
                except RuntimeError:
                    completion_submission_receipt(prior, owner, endpoint)
                return True
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                return False
        return verify

    def _attempt(self, state, directory, task, options, source, prior, emit, stop, refill_depth=0):
        kwargs = {}
        if prior and prior.get('tp_refill', {}).get('pending'):
            raise RuntimeError('tp_refill_result_unverified')
        if source.exists():
            receipts = list(source.rglob('*-receipt.json'))
            if prior is None and receipts:
                raise RuntimeError('seed_input_without_result_requires_review')
            if prior:
                if prior.get('action_outcome', {}).get('execution_status') == 'uncertain' or prior.get('account_conflict') or prior.get('account_paused') or (prior.get('action_outcome', {}).get('decision') == 'pause_account') or (prior.get('reason') == 'account_connection_conflict'):
                    raise RuntimeError('seed_uncertain_input_or_account_pause')
                context_path = source / 'context.json'
                if not context_path.exists():
                    raise RuntimeError('seed_task_context_missing')
                context = json.loads(context_path.read_text(encoding='utf8'))
                if task == 'harvest':
                    kwargs['resume_harvest_from'] = source
                    kwargs['verify_resume_owner'] = self._resume_operation_verifier(state, directory, task, source, prior, stop)
                elif context.get('start_submitted'):
                    harvest = deepcopy(state['config']['harvest'])
                    if not state['config']['cycle']['integrated']:
                        harvest['preset']['tp_budget'] = min(harvest['preset']['tp_budget'], state['config']['cycle']['total_tp_budget'] - state['tp_spent'])
                        harvest['preset']['jewel_budget'] = min(harvest['preset']['jewel_budget'], state['config']['cycle']['total_jewel_budget'] - state['jewel_spent'])
                    submitted = Path(prior.get('prior_submission') or source / 'backend_result.json').resolve()
                    if submitted.name != 'backend_result.json' or not submitted.is_file() or submitted.parent.name != 'plant' or (not submitted.parent.parent.name.isdigit()) or (submitted.parent.parent.parent != Path(directory).resolve() / 'tasks'):
                        raise RuntimeError('seed_submitted_plant_source_unverified')
                    kwargs.update(resume_plant_from=submitted, recovery_harvest_options=harvest, verify_resume_owner=self._resume_operation_verifier(state, directory, task, source, prior, stop))
                elif receipts:
                    from desktop.umaa_plant_setup_resume import proven
                    if not proven(source, self.endpoint):
                        raise RuntimeError('seed_plant_preparation_requires_review')
                    kwargs['resume_plant_setup_from'] = source
                fresh = directory / 'tasks' / str(time.time_ns()) / task
                state['current']['attempt'].update(output=str(fresh), options=options)
                _save(directory / 'state.json', state)
                source = fresh
        source.mkdir(parents=True, exist_ok=True)
        cycle = state['config']['cycle']
        from core.passive_popup_runtime import PassivePopupSession
        from core.screenshot_retention import ScreenshotSession
        with ScreenshotSession() as screenshots:
            try:
                with seed_policy(cycle['targets'], None if cycle['integrated'] else cycle['total_tp_budget'] - state['tp_spent']), PassivePopupSession(self.endpoint, stop) as popups:
                    result = self.executor(task, options, endpoint=self.endpoint, adb_path=self.adb_path, output=source, skill_database=self.skill_database, emit=emit, stop=stop, auto_restart_game=False, **kwargs)
                    popups.annotate(result)
                    if popups.failure:
                        result.update(ok=False, reason=popups.failure)
            except Exception as error:
                result = dict(ok=False, reason=str(error), device_action_count_complete=False)
            context_path = source / 'context.json'
            if (task == 'harvest' or result.get('submitted_training_collected')) and 'factor_selection' not in result and context_path.exists():
                context = json.loads(context_path.read_text(encoding='utf8'))
                if context.get('factor_goal') == cycle['targets'] and context.get('factor_selection'):
                    result['factor_selection'] = deepcopy(context['factor_selection'])
            result.update(current_account_only=True, output_dir=str(source))
            if result.get('ok') is True and result.get('last_observation', {}).get('endpoint') != self.endpoint:
                result.update(ok=False, reason='seed_child_endpoint_unverified')
            _save(source / 'backend_result.json', result)
            _save(source / 'adapter_result.json', result)
            try:
                screenshots.finish(result, complete=not stop.is_set() and self._verified(task, result))
            except Exception:
                pass
        if task == 'plant' and cycle['integrated'] and (result.get('ok') is not True) and (not stop.is_set()):
            from desktop.umaa_plant_setup_resume import proven
            observed = result.get('last_observation', {})
            available = observed.get('tp_before')
            if available is None and observed.get('page') == 'final_autonomous' and observed.get('screenshot_path'):
                import cv2
                from core.autonomous_training.tp_refill import hud_plus
                frame = cv2.imread(observed['screenshot_path'])
                if frame is not None and frame.shape[1::-1] in {(720, 1280), (1440, 2560)}:
                    hud = hud_plus(cv2.resize(frame, (720, 1280)))
                    if hud:
                        available = hud['tp']
            shortage = type(available) is int and type(observed.get('tp_cost')) is int and (available < observed['tp_cost'])
            if shortage and refill_depth == 0 and proven(source, self.endpoint):
                from core.autonomous_training.live import AutonomousDevice
                from core.autonomous_training.tp_refill import TpRefill
                refill_dir = Path(prior.get('tp_refill_output')) if prior and prior.get('tp_refill_output') else source / 'tp-refill'
                if not refill_dir.resolve().is_relative_to(directory.resolve()):
                    raise RuntimeError('tp_refill_source_outside_cycle')
                device = AutonomousDevice(self.endpoint, self.adb_path, refill_dir)
                device.connect()
                refill = TpRefill(device, refill_dir).run(observed['tp_cost'], emit, stop)
                result.update(tp_refill=refill, tp_refill_output=str(refill_dir))
                _save(source / 'adapter_result.json', result)
                if not refill['ok']:
                    result.update(ok=False, reason=refill.get('reason', 'tp_refill_not_verified'))
                    _save(source / 'adapter_result.json', result)
                    return result
                restored = state.setdefault('tp_refills', [])
                if not any((row['source'] == str(refill_dir) for row in restored)):
                    restored.append(dict(source=str(refill_dir), tp_items=refill['tp_items'], carrots=refill['carrots']))
                    state['jewel_spent'] += refill['carrots']
                    state['tp_items_used'] = state.get('tp_items_used', 0) + refill['tp_items']
                    _save(directory / 'state.json', state)
                return self._attempt(state, directory, task, options, source, result, emit, stop, refill_depth=1)
        return result

    def _account_cost(self, state, task, source, result):
        current = state['current']
        if task == 'plant':
            context_path = Path(result.get('output_dir', source)) / 'context.json'
            context = json.loads(context_path.read_text(encoding='utf8')) if context_path.exists() else {}
            cost = context.get('start_cost', {}).get('cost', 0)
            if context.get('start_submitted') and (not current.get('plant_cost_accounted')):
                if type(cost) is not int or cost <= 0:
                    raise RuntimeError('seed_plant_cost_unverified')
                state['tp_spent'] += cost
                current['plant_cost_accounted'] = True
        else:
            factors = result.get('factor_selection') or {}
            for field, counter in [('tp_spent', 'factor_tp_accounted'), ('jewel_spent', 'factor_jewel_accounted')]:
                spent, old = (factors.get(field, 0), current.get(counter, 0))
                if type(spent) is not int or spent < old:
                    raise RuntimeError('seed_factor_budget_not_monotonic')
                state[field] += spent - old
                current[counter] = spent
        cycle = state['config']['cycle']
        if not cycle['integrated'] and (state['tp_spent'] > cycle['total_tp_budget'] or state['jewel_spent'] > cycle['total_jewel_budget']):
            raise RuntimeError('seed_total_budget_exceeded')

    @staticmethod
    def _notify_factor_hit(state, directory):
        """Best-effort notification after the confirmed result is committed."""
        marker = None
        try:
            if state.get('status') != 'completed' or state.get('target_met') is not True:
                return
            latest = state['history'][-1]
            retained = latest['factors']
            targets = state['config']['cycle']['targets']
            if latest.get('target_met') is not True or not retained.get('frame_sha256') or not targets_met(retained.get('rows'), targets):
                return
            notices = state.setdefault('notifications', {})
            if not isinstance(notices, dict) or 'seed_factor_hit' in notices:
                return
            key = f"seed:{Path(directory).name}:cycle:{latest['number']}:target_met"
            marker = dict(dedupe_key=key, status='attempted')
            notices['seed_factor_hit'] = marker
            # Never attempt external I/O unless the business dedupe is durable.
            _save(Path(directory) / 'state.json', state)
            rows = {row['key']: row for row in retained['rows']}
            summary = []
            for target in targets:
                row = rows[target['key']]
                name = row.get('name')
                name = name.strip() if isinstance(name, str) and name.strip() else row['key']
                summary.append(f"{name} {row['stars']}★")
            content = (
                f"第{latest['number']}轮已确认目标因子全部达标。\n"
                f"保留目标因子：{'、'.join(summary)}\n"
                f"本次累计消耗：TP {state['tp_spent']}，宝石 {state['jewel_spent']}。"
            )
            from desktop.notifications import send_event
            result = send_event(event_id='seed_factor_hit', title='MaaUma 种马因子命中', content=content, dedupe_key=key)
            status = result.get('status') if isinstance(result, dict) else None
            marker['status'] = status if status in {
                'accepted', 'rejected', 'failed', 'disabled', 'muted',
                'not_configured', 'saved_token_unreadable',
                'token_file_unreadable', 'duplicate',
            } else 'failed'
        except Exception:
            # Notification/storage failures cannot change a completed game task.
            if marker is None:
                return
            marker['status'] = 'failed'
        try:
            _save(Path(directory) / 'state.json', state)
        except Exception:
            pass

    @staticmethod
    def _summary(state, directory):
        complete = state['status'] == 'completed'
        return dict(kind='seed_cycle', ok=complete, game_task_complete=complete, stop_queue=state.get('reason') != 'single_plant_complete', reason=state.get('reason'), reason_text=REASONS.get(state.get('reason'), state.get('reason')), completed_cycles=state['completed_cycles'], target_met=state.get('target_met', False), tp_spent=state['tp_spent'], jewel_spent=state['jewel_spent'], output_dir=str(directory), history=state['history'])

def main():
    try:
        result = editor_request(json.loads(sys.stdin.read()))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        result = dict(ok=False, error=str(error))
    print(json.dumps(result, ensure_ascii=False))
if __name__ == '__main__':
    main()
