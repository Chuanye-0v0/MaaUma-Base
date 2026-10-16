"""Continue an original plant from its current screen without starting another run."""
from desktop.umaa_public_context import endpoint as _public_endpoint
import json
import hashlib
import time
from pathlib import Path

def unsubmitted_setup_verified(prior, account_root, endpoint):
    """Prove a setup-only failure had no training submission, without editing it."""
    if prior.get('ok') is not False or prior.get('start_submitted') is not False or prior.get('start_confirmed') is not False or prior.get('account_conflict') or prior.get('prior_submission') or (prior.get('reason') == 'account_connection_conflict'):
        return False
    source = Path(prior.get('output_dir', '')).resolve()
    if not source.is_relative_to(Path(account_root).resolve()):
        return False
    safe = {'open_training', 'close_notice', 'continue_scenario', 'scroll_trainees', 'scroll_trainees_up', 'continue_trainee', 'auto_inheritance', 'toggle_borrow', 'confirm_inheritance'}
    try:
        saved = json.loads((source / 'backend_result.json').read_text(encoding='utf8'))
        context = json.loads((source / 'context.json').read_text(encoding='utf8'))
        last = json.loads((source / 'recovery/last-action.json').read_text(encoding='utf8'))
        if any((saved.get(k) != prior.get(k) for k in ('reason', 'start_submitted', 'start_confirmed', 'records', 'action_outcome'))) or context.get('mode') != 'start' or context.get('start_submitted') or context.get('request_id') or context.get('pending') or (saved.get('last_observation', {}).get('endpoint') != endpoint) or (last != saved.get('action_outcome')) or (last.get('execution_status') != 'not_executed') or (last.get('input_attempts') != 0):
            return False
        paths = []
        for record in saved.get('records', []):
            action = record.get('action_id', '')
            if action not in safe and (not (action.startswith('inspect_trainee_') and action[16:].isdigit())):
                return False
            path = Path(record['receipt_path']).resolve()
            if path.parent != source or path in paths:
                return False
            receipt = json.loads(path.read_text(encoding='utf8'))
            if receipt.get('performed') is not True or receipt.get('endpoint') != endpoint or (not receipt.get('source_frame_sha256')) or any((receipt.get(k) != record.get(k) for k in ('action_id', 'performed', 'endpoint', 'source_frame_sha256'))):
                return False
            paths.append(path)
        return bool(paths) and set(paths) == {p.resolve() for p in source.glob('*-receipt.json')}
    except (OSError, ValueError, KeyError, TypeError):
        return False

def acknowledged_confirmation(source, report, context, endpoint):
    """Original click acknowledged but its post-capture failed; not settled success."""
    try:
        outcome = report.get('action_outcome') or {}
        if report.get('account_conflict') or report.get('account_paused') or report.get('reason', '').startswith('operator_stop') or (outcome != json.loads((source.parent / 'recovery/last-action.json').read_text(encoding='utf8'))) or (outcome.get('action_id') != 'confirm_autonomous_start') or (outcome.get('execution_status') != 'uncertain') or (outcome.get('input_attempts') != 1) or (outcome.get('acknowledged_inputs') != 1) or (outcome.get('source_page') != 'autonomous_start_notice'):
            return False
        receipts = [json.loads(p.read_text(encoding='utf8')) for p in source.parent.glob('*-receipt.json')]
        confirms = [r for r in receipts if r.get('action_id') == 'confirm_autonomous_start']
        if len(confirms) != 1:
            return False
        receipt = confirms[0]
        if receipt.get('status') != 'dispatch_intent' or receipt.get('performed') is not None or receipt.get('endpoint') != endpoint or (receipt.get('request_id') != context['request_id']) or (receipt.get('source_frame_sha256') != outcome.get('source_frame_sha256')) or (Path(receipt.get('receipt_path', '')).resolve().parent != source.parent):
            return False
        frames = [json.loads(p.read_text(encoding='utf8')) for p in source.parent.glob('*.json')]
        frames = [f for f in frames if f.get('frame_sha256') == receipt['source_frame_sha256']]
        if not frames:
            return False
        for frame in frames:
            png = Path(frame['screenshot_path']).resolve()
            if frame.get('endpoint') != endpoint or frame.get('page') != 'autonomous_start_notice' or 'confirm_autonomous_start' not in frame.get('actions', {}) or (png.parent != source.parent) or (hashlib.sha256(png.read_bytes()).hexdigest() != frame['frame_sha256']):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False

def submission_source(prior, account_root, endpoint):
    if prior.get('ok') is True or not (prior.get('start_submitted') or prior.get('prior_submission')):
        return None
    source = Path(prior.get('prior_submission') or Path(prior['output_dir']) / 'backend_result.json').resolve()
    if not source.is_relative_to(Path(account_root).resolve()):
        raise ValueError('plant_resume_source_outside_account')
    from core.autonomous_training.runner import load_submission
    load_submission(source, endpoint)
    return source

def _latest_continuation(source):
    """Follow immutable handoffs within this original account, retaining history."""
    pointer = source.parent / 'home-recovery.intent.json'
    seen = set()
    for _ in range(32):
        if not pointer.exists():
            return (None, pointer)
        saved = json.loads(pointer.read_text(encoding='utf8'))
        attempt = Path(saved['output']).resolve()
        if saved.get('prior_submission') != str(source) or attempt in seen or (not attempt.is_relative_to(source.parents[4])) or (attempt == source.parent):
            raise RuntimeError('plant_continuation_owner_unverified')
        seen.add(attempt)
        pointer = attempt / 'submitted-continuation.intent.json'
        if not pointer.exists():
            return (attempt, pointer)
    raise RuntimeError('plant_continuation_chain_limit')

def resolve_continuation_owner(source, *, probe_output=None, probe_report=None):
    """An observation-only rejection is not a new submission/harvest owner.

    Keep the anchor and its complete receipt validation. A zero action counter
    alone cannot establish this: look for dispatch, consumption and popup intent
    artifacts as well as changed owner context. No journals are rewritten here.
    """
    source = Path(source).resolve()
    anchor, pointer = _latest_continuation(source)
    if probe_output is None or anchor == Path(probe_output).resolve():
        return (anchor, pointer)
    output = Path(probe_output).resolve()
    report = probe_report or {}
    outcome = report.get('action_outcome') or {}
    if anchor is None or not output.is_relative_to(source.parents[4]) or output == source.parent or (report.get('ok') is not False) or (report.get('kind') != 'autonomous_training') or (report.get('mode') != 'start') or (report.get('prior_submission') != str(source)) or (report.get('reason') != 'plant_recovery_page_unverified') or (report.get('records') != []) or (report.get('last_observation', {}).get('endpoint') != _public_endpoint()) or report.get('account_paused') or report.get('account_conflict') or (outcome.get('input_attempts', 0) != 0) or (outcome.get('acknowledged_inputs', 0) != 0) or (outcome.get('execution_status') in {'executed', 'uncertain'}) or report.get('passive_popup_receipts') or (report.get('device_actions') is not None and (type(report['device_actions']) is not int or report['device_actions'] != 0)) or any(output.rglob('*-receipt.json')) or any(output.rglob('*-submitted.json')) or any(output.rglob('*.intent.json')) or any(output.rglob('popup-actions/*.json')) or (output / 'submitted-recovery-result.json').exists():
        raise RuntimeError('restart_plant_harvest_owner_unverified')
    for path in output.rglob('last-action.json'):
        action = json.loads(path.read_text(encoding='utf8'))
        if action.get('input_attempts') != 0 or action.get('acknowledged_inputs') != 0 or action.get('execution_status') != 'not_executed':
            raise RuntimeError('restart_plant_harvest_owner_unverified')
    context = json.loads((output / 'context.json').read_text(encoding='utf8'))
    original_context = json.loads((anchor / 'context.json').read_text(encoding='utf8'))
    if not context or context != original_context or context.get('prior_submission') != str(source):
        raise RuntimeError('restart_plant_harvest_owner_unverified')
    return (anchor, pointer)

def _restore_continuation(device, attempt, source, options):
    """Restore the original local action owner, never infer consumed steps from UI."""
    read = lambda path: json.loads(path.read_text(encoding='utf8'))
    result = read(attempt / 'submitted-recovery-result.json')
    context = read(attempt / 'context.json')
    if result.get('prior_submission') != str(source) or context.get('prior_submission') != str(source) or context.get('request_id') != device.context.get('request_id') or (context.get('mode') not in {'start', 'harvest'}) or (result.get('ok') is not False) or (context.get('start_submitted') is not True and (not (context.get('resume_unsubmitted_setup') is True and (not context.get('resume_start_open_attempted'))))) or (result.get('last_observation', {}).get('endpoint') != device.endpoint) or result.get('account_conflict') or result.get('account_paused'):
        raise RuntimeError('plant_continuation_owner_unverified')
    if (result.get('action_outcome') or {}).get('execution_status') == 'uncertain':
        raise RuntimeError('plant_continuation_input_uncertain')
    for path in attempt.rglob('last-action.json'):
        if read(path).get('execution_status') == 'uncertain':
            raise RuntimeError('plant_continuation_input_uncertain')
    if context['mode'] == 'harvest':
        if context.get('harvest_learn_skills') != options.get('learn_skills') or context.get('harvest_update_team') != options.get('update_team') or context.get('harvest_preset') != options.get('preset'):
            raise RuntimeError('plant_continuation_options_changed')
        flags = {'finish_submitted': 'finish_training', 'completion_submitted': 'complete_training', 'factors_requested': 'confirm_factors', 'factor_choice_submitted': 'confirm_factor_choice', 'harvest_home_submitted': 'close_completed_training'}
        paths = {Path(record['receipt_path']).resolve() for record in result.get('records', []) if record.get('receipt_path')}
        paths.update((path.resolve() for path in attempt.rglob('*-receipt.json')))
        receipts = []
        for path in paths:
            if not path.is_relative_to(source.parents[4]):
                raise RuntimeError('plant_continuation_receipt_outside_account')
            receipts.append(read(path))
        for flag, action in flags.items():
            if context.get(flag) is True and (not any((receipt.get('action_id') == action and receipt.get('performed') is True and (receipt.get('endpoint') == device.endpoint) for receipt in receipts))):
                raise RuntimeError('plant_continuation_receipt_missing:' + action)
    device.context.update(context)
    return result

def _pending_start_page(observed, context):
    return context.get('start_confirmed') is not True and context.get('mode') == 'start' and (observed.get('page') == 'autonomous_start_notice' or (observed.get('page') == 'final_autonomous' and context.get('resume_start_cancelled') is True and (not context.get('resume_start_open_attempted'))))

def _unsubmitted_setup_page(observed, context, original):
    outcome = original.get('action_outcome') or {}
    zero_confirmation = outcome.get('action_id') == 'confirm_autonomous_start' and outcome.get('execution_status') == 'not_executed' and (outcome.get('input_attempts') == 0) and (outcome.get('acknowledged_inputs') == 0)
    pages = {'new_scenario', 'scenario'}
    if context.get('resume_unsubmitted_setup') is True:
        pages |= {'trainee', 'inheritance', 'inherit_confirm', 'support_deck', 'auto_support_confirm', 'final_confirm', 'final_autonomous', 'autonomous_tutorial', 'exam_mode', 'exam_list', 'exam_detail'}
    return zero_confirmation and original.get('start_confirmed') is False and (context.get('start_confirmed') is not True) and (not context.get('resume_start_open_attempted')) and (observed.get('page') in pages)

def _continue_start_confirmation(device, observed, records, stop):
    """Finish the original request, rereading a stale budget on its clear setup page."""
    context = device.context
    if context.get('start_confirmed'):
        raise RuntimeError('start_confirmation_already_submitted')
    context['resume_start_confirmation'] = True
    budget = context.get('start_cost') or {}
    before, after, cost, captured = (budget.get('before'), budget.get('after'), budget.get('cost'), context.get('start_cost_at'))
    fresh_budget = all((type(n) is int for n in (before, after, cost, captured))) and 0 < cost <= before and (after >= 0) and (before - after == cost) and (0 <= int(time.time() * 1000) - captured <= 30 * 60 * 1000)

    def act(action):
        receipt = device.step(action, stop)
        records.append({key: value for key, value in receipt.items() if key != 'observation'})
        if receipt.get('performed') is not True:
            raise RuntimeError('plant_confirmation_input_unverified')
        return receipt.get('observation') or {}
    if not fresh_budget or observed.get('page') == 'final_autonomous':
        if observed.get('page') == 'autonomous_start_notice':
            observed = act('cancel_autonomous_start')
        if observed.get('page') != 'final_autonomous' or observed.get('endpoint') != device.endpoint:
            raise RuntimeError('plant_confirmation_budget_page_unverified')
        context['resume_start_cancelled'] = True
        observed = act('start_autonomous')
    if observed.get('page') == 'autonomous_start_notice':
        observed = act('confirm_autonomous_start')
    elif observed.get('page') not in {'autonomous_running', 'training_status'}:
        raise RuntimeError('plant_confirmation_current_page_unverified')
    context['start_confirmed'] = True
    return observed

def resume_submitted_plant(device, source, harvest_options, skill_database, emit, stop, *, verify_account=None, verify_owner=None):
    from core.autonomous_training.runner import load_submission
    from core.autonomous_training.harvest import HarvestRunner
    from core.training_continuation import continuation_phase, continue_training_navigation, MATURE_ENTRY_PAGES
    from core.location_navigation import wait_for_transition, NavigationFailure
    source = Path(source).resolve()
    original = json.loads(source.read_text(encoding='utf8'))
    device.context.update(load_submission(source, _public_endpoint()), mode='start')
    outcome = original.get('action_outcome') or {}
    if outcome.get('action_id') == 'confirm_autonomous_start' and (outcome.get('input_attempts', 0) > 0 or outcome.get('execution_status') in {'uncertain', 'executed'}):
        device.context['start_confirmed'] = True
    device.context['inheritance_options'] = {}
    context_path = source.parent / 'context.json'
    if context_path.is_file():
        original_context = json.loads(context_path.read_text(encoding='utf8'))
        for key in ('required_trainee', 'required_variant', 'verified_trainee', 'verified_variant', 'plant_options', 'verified_scenario', 'plan_done', 'support_options', 'verified_support', 'inheritance_options', 'inheritance_confirmation_submitted', 'inheritance_confirmed', 'inheritance_borrow_toggle', 'inheritance_requested'):
            if key in original_context:
                device.context[key] = original_context[key]
    base = dict(kind='autonomous_training', mode='start', start_submitted=True, start_confirmed=bool(device.context.get('start_confirmed')), prior_submission=str(source), no_refill=True, started_at=original.get('started_at'))
    report = dict(base, ok=False, records=[])
    prior_attempt, intent = resolve_continuation_owner(source)
    previous = None
    observed = device.capture()
    report['last_observation'] = observed
    if device.endpoint != _public_endpoint() or observed.get('endpoint') != device.endpoint:
        return dict(report, reason='plant_recovery_scope_unverified')
    home = lambda o: o.get('page') == 'home' and o.get('endpoint') == _public_endpoint()
    if stop.is_set():
        return dict(report, reason='operator_stop')
    if verify_owner is not None and verify_owner() is not True:
        return dict(report, reason='plant_confirmation_account_unverified')
    if prior_attempt is not None:
        result_path = prior_attempt / 'submitted-recovery-result.json'
        result = json.loads(result_path.read_text(encoding='utf8')) if result_path.exists() else {}
        if result.get('ok') is True and result.get('reason') == 'started_home_verified' and (result.get('prior_submission') == str(source)) and home(observed) and home((fresh := device.capture())) and (not stop.is_set()):
            return dict(result, last_observation=fresh)
        attempt = prior_attempt
        adapter_path = attempt / 'adapter_result.json'
        if adapter_path.is_file():
            adapter = json.loads(adapter_path.read_text(encoding='utf8'))
            recovery = adapter.get('stall_recovery') or {}
            restarted_home = (recovery.get('startup') or {}).get('last_observation') or {}
            if recovery.get('ok') is True and (recovery.get('startup') or {}).get('ok') is True and ((recovery.get('receipt') or {}).get('launch_accepted') is True) and ((recovery.get('receipt') or {}).get('endpoint') == device.endpoint) and (restarted_home.get('page') == 'home') and (restarted_home.get('endpoint') == device.endpoint) and navigation_only_recovery(attempt, source, device.endpoint) and (verify_account is not None) and (verify_account() is True):
                finished = finish_restarted_plant(device, source, emit, stop, account_verified=True, recovery_output=attempt, harvest_options=harvest_options, skill_database=skill_database)
                return finished
        if not result_path.is_file() or not (attempt / 'context.json').is_file():
            return dict(report, reason='plant_home_recovery_requires_review')
        previous = _restore_continuation(device, attempt, source, harvest_options)
    phase = continuation_phase(observed, device.context)
    active_before_check = phase == 'navigation' and observed.get('page') in {'autonomous_running', 'training_status'}
    if phase == 'wait':
        try:
            observed = wait_for_transition(device, stop, observed, lambda state: continuation_phase(state, device.context) != 'wait', waiting_pages={'unknown', 'loading', 'training_status', 'autonomous_running'}, endpoint=_public_endpoint())
        except NavigationFailure as error:
            return dict(report, reason='operator_stop' if error.kind == 'operator_stop' else 'plant_recovery_page_unverified', last_observation=error.observation, navigation_trace=error.trace)
        report['last_observation'] = observed
        phase = continuation_phase(observed, device.context)
    unsettled_confirmation = acknowledged_confirmation(source, original, device.context, _public_endpoint())
    if unsettled_confirmation or previous is not None:
        if observed.get('endpoint') != _public_endpoint() or (phase not in {'navigation', 'harvest', 'home'} and (not _pending_start_page(observed, device.context)) and (not _unsubmitted_setup_page(observed, device.context, original))):
            return dict(report, reason='plant_confirmation_current_page_unverified')
        verifier = verify_owner if phase == 'harvest' or previous is not None else verify_owner or verify_account
        if verifier is None or verifier() is not True or stop.is_set():
            return dict(report, reason='plant_confirmation_account_unverified')
        observed = device.capture()
        next_phase = continuation_phase(observed, device.context)
        if next_phase == 'wait':
            try:
                observed = wait_for_transition(device, stop, observed, lambda state: continuation_phase(state, device.context) != 'wait', waiting_pages={'unknown', 'loading', 'training_status', 'autonomous_running'}, endpoint=_public_endpoint())
                next_phase = continuation_phase(observed, device.context)
            except NavigationFailure as error:
                return dict(report, reason='operator_stop' if error.kind == 'operator_stop' else 'plant_recovery_page_unverified', last_observation=error.observation, navigation_trace=error.trace)
        settled_home = previous is not None and next_phase == 'home' and (device.context.get('harvest_home_submitted') is True or (device.context.get('formation_verified') is True and device.context.get('formation_home_submitted') is True))
        if observed.get('endpoint') != _public_endpoint() or (next_phase not in {'navigation', 'harvest', 'home'} and (not _pending_start_page(observed, device.context)) and (not _unsubmitted_setup_page(observed, device.context, original))) or (phase == 'harvest' and next_phase != 'harvest' and (not settled_home)):
            return dict(report, reason='plant_confirmation_post_identity_state_changed')
        report.update(last_observation=observed, confirmation_reconciled=True)
    phase = continuation_phase(observed, device.context)
    if device.context.get('start_confirmed') is not True and verify_owner is None:
        return dict(report, reason='plant_confirmation_account_unverified')
    if _unsubmitted_setup_page(observed, device.context, original):
        if verify_owner is None or verify_owner() is not True:
            return dict(report, reason='plant_confirmation_account_unverified')
        from core.autonomous_training.runner import AutonomousRunner
        with intent.open('x', encoding='utf8') as stream:
            json.dump(dict(output=str(Path(device.output_dir).resolve()), prior_submission=str(source)), stream)
        if device.context.get('resume_unsubmitted_setup') is not True:
            for key in ('verified_scenario', 'verified_variant', 'verified_trainee', 'verified_support', 'plan_done', 'plan_phase', 'support_requested'):
                device.context.pop(key, None)
        device.context.update(start_submitted=False, resume_unsubmitted_setup=True)
        emit(dict(type='progress', message='当前无进行中育成且原确认零输入，按原请求继续准备'))
        result = AutonomousRunner(device, emit, stop).run('start')
        result.update(prior_submission=str(source), resumed_unsubmitted_setup=True)
        (Path(device.output_dir) / 'context.json').write_text(json.dumps(device.context), encoding='utf8')
        (Path(device.output_dir) / 'submitted-recovery-result.json').write_text(json.dumps(result), encoding='utf8')
        return result
    harvest_from_home = previous is not None and phase == 'home' and (device.context.get('mode') == 'harvest') and (not device.context.get('completion_submitted'))
    if harvest_from_home and any((device.context.get(key) for key in ('finish_submitted', 'harvest_skill_attempted', 'formation_requested'))):
        return dict(report, reason='plant_continuation_partial_consumption_requires_reconciliation')
    mature = phase == 'harvest' or harvest_from_home
    if mature and (not {'learn_skills', 'update_team'} <= harvest_options.keys()):
        return dict(report, reason='plant_recovery_harvest_options_missing')
    if mature and harvest_options['learn_skills'] and (not (skill_database and Path(skill_database).is_file())):
        return dict(report, reason='external_skill_database_missing')
    if phase not in {'harvest', 'home', 'navigation'} and (not _pending_start_page(observed, device.context)):
        return dict(report, reason='plant_recovery_page_unverified')
    if mature and previous is None and (observed.get('page') not in MATURE_ENTRY_PAGES | {'training_status'}):
        return dict(report, reason='plant_harvest_context_required')
    with intent.open('x', encoding='utf8') as stream:
        json.dump(dict(output=str(Path(device.output_dir).resolve()), prior_submission=str(source)), stream)
    navigation_records = []
    pending_confirmation = device.context.get('start_confirmed') is not True
    if _pending_start_page(observed, device.context):
        try:
            if verify_owner is None or verify_owner() is not True:
                raise RuntimeError('plant_confirmation_account_unverified')
            observed = _continue_start_confirmation(device, observed, navigation_records, stop)
            report['last_observation'] = observed
            phase = continuation_phase(observed, device.context)
            base.update(start_confirmed=True, started_at=time.time())
            report.update(base)
            pending_confirmation = False
        except RuntimeError as error:
            from core.action_recovery import failure_details
            report.update(failure_details(error), reason=str(error), records=navigation_records, start_confirmed=bool(device.context.get('start_confirmed')))
            (Path(device.output_dir) / 'context.json').write_text(json.dumps(device.context), encoding='utf8')
            (Path(device.output_dir) / 'submitted-recovery-result.json').write_text(json.dumps(report), encoding='utf8')
            return report
    if pending_confirmation and phase in {'navigation', 'harvest'}:
        if verify_owner is None or verify_owner() is not True:
            return dict(report, reason='plant_confirmation_account_unverified')
        device.context['start_confirmed'] = True
        base['start_confirmed'] = report['start_confirmed'] = True
    emit(dict(type='progress', message='按当前画面续接原育成'))
    if phase == 'navigation' or (phase == 'home' and previous is None and (unsettled_confirmation or pending_confirmation)):
        try:
            observed, more_records = continue_training_navigation(device, observed, emit, stop, probe_home=phase == 'home' and (pending_confirmation or not active_before_check))
            navigation_records.extend(more_records)
            if pending_confirmation:
                device.context['start_confirmed'] = True
                base['start_confirmed'] = report['start_confirmed'] = True
        except RuntimeError as error:
            from core.action_recovery import failure_details
            report.update(failure_details(error), reason=str(error), records=navigation_records)
            (Path(device.output_dir) / 'context.json').write_text(json.dumps(device.context), encoding='utf8')
            (Path(device.output_dir) / 'submitted-recovery-result.json').write_text(json.dumps(report), encoding='utf8')
            return report
        report['last_observation'] = observed
        phase = continuation_phase(observed, device.context)
        mature = phase == 'harvest'
    if mature:
        if not {'learn_skills', 'update_team'} <= harvest_options.keys():
            raise RuntimeError('plant_recovery_harvest_options_missing')
        resumed_finish = None
        if previous is not None:
            if device.context.get('completion_submitted'):
                from desktop.mfa_harvest_restart import completion_receipt
                completion_receipt(previous, source.parents[4], device.endpoint)
                if observed['page'] in MATURE_ENTRY_PAGES | {'skills', 'finish_confirm', 'training_status'}:
                    return dict(report, reason='plant_continuation_training_changed')
            elif not device.context.get('finish_submitted') and observed['page'] == 'finish_ready':
                skill = previous.get('skill_result') or {}
                if skill.get('ok') is True:
                    resumed_finish = skill
        result = HarvestRunner(device, emit, stop, external_skill_db_path=skill_database, resume_finish_result=resumed_finish, preset=harvest_options.get('preset'), refine_skills=harvest_options.get('refine_skills', False), skill_recheck_min_pt=harvest_options.get('skill_recheck_min_pt', 60)).run(execute=True, learn_skills=harvest_options['learn_skills'], update_team=harvest_options['update_team'])
        expected = 'harvest_and_formation_home_verified' if harvest_options['update_team'] else 'harvest_home_verified'
        complete = result.get('ok') is True and result.get('reason') == expected and home(result.get('last_observation', {}))
        report.update(result, **base, submitted_training_collected=bool(complete))
        report['ok'] = complete
        if complete:
            report['reason'] = 'started_home_verified'
    elif phase == 'home':
        if previous and device.context.get('mode') == 'harvest':
            from desktop.mfa_harvest_restart import completion_receipt
            completion_receipt(previous, source.parents[4], device.endpoint)
            if not (device.context.get('harvest_home_submitted') is True or (device.context.get('formation_verified') is True and device.context.get('formation_home_submitted') is True)):
                raise RuntimeError('plant_continuation_home_unverified')
            report['submitted_training_collected'] = True
        fresh = device.capture()
        report.update(ok=home(fresh), reason='started_home_verified' if home(fresh) else 'home_not_verified', last_observation=fresh)
    else:
        report.update(ok=False, reason='plant_recovery_page_unverified')
    prior_records = previous.get('records', []) if previous else []
    report['records'] = [*prior_records, *navigation_records, *report.get('records', [])]
    if previous:
        report['device_actions'] = previous.get('device_actions', 0) + report.get('device_actions', 0)
        for key in ('skill_result', 'skill_batches', 'finish_result', 'factor_selection'):
            if key in previous:
                report.setdefault(key, previous[key])
    (Path(device.output_dir) / 'context.json').write_text(json.dumps(device.context, ensure_ascii=False, indent=2), encoding='utf8')
    if stop.is_set():
        report.update(ok=False, reason='operator_stop')
    (Path(device.output_dir) / 'submitted-recovery-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    return report

def navigation_only_recovery(attempt, source, endpoint):
    """The prior home-recovery may contain uncertain UI taps, never finish/spend."""
    try:
        attempt, source = (Path(attempt).resolve(), Path(source).resolve())
        if not attempt.is_relative_to(source.parents[4]):
            return False
        report = json.loads((attempt / 'submitted-recovery-result.json').read_text(encoding='utf8'))
        if report.get('prior_submission') != str(source) or report.get('start_confirmed') is not True or report.get('reason') != 'game_stalled':
            return False
        allowed = {'open_training_menu', 'return_home', 'open_training', 'cancel_training_status'}
        for path in attempt.rglob('*-receipt.json'):
            if 'stall-recovery' in path.relative_to(attempt).parts:
                continue
            receipt = json.loads(path.read_text(encoding='utf8'))
            if receipt.get('action_id') not in allowed or receipt.get('endpoint') != endpoint:
                return False
        outcome = json.loads((attempt / 'recovery/last-action.json').read_text(encoding='utf8'))
        return outcome.get('action_id') in allowed and outcome.get('acknowledged_inputs', 0) <= 1
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return False

def finish_restarted_plant(device, source, emit, stop, *, account_verified, recovery_output=None, harvest_options=None, skill_database=None):
    """Verify the server's existing training and home; never submit another start."""
    from core.autonomous_training.runner import load_submission
    from core.location_navigation import wait_for_transition, NavigationFailure
    source = Path(source).resolve()
    context = load_submission(source, device.endpoint)
    original = json.loads(source.read_text(encoding='utf8'))
    if account_verified is not True or stop.is_set():
        raise RuntimeError('restart_plant_submission_unverified')
    device.context.update(context, mode='start', restart_return_only=True)
    records = []
    observed = device.capture()
    target = Path(recovery_output or device.output_dir)

    def wait_ready(fresh, pages, waiting, reason):
        try:
            return wait_for_transition(device, stop, fresh, lambda state: state.get('page') in pages, waiting_pages=waiting, endpoint=device.endpoint)
        except NavigationFailure as error:
            result = dict(ok=False, kind='autonomous_training', mode='start', reason='operator_stop' if error.kind == 'operator_stop' else reason, start_submitted=True, start_confirmed=True, prior_submission=str(source), no_refill=True, records=records, last_observation=error.observation, navigation_trace=error.trace)
            target.mkdir(parents=True, exist_ok=True)
            (target / 'submitted-recovery-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
            failure = RuntimeError(result['reason'])
            failure.last_observation = error.observation
            failure.navigation_trace = error.trace
            raise failure from error
    from core.training_continuation import MATURE_ENTRY_PAGES, continuation_phase
    mature_pages = MATURE_ENTRY_PAGES if harvest_options is not None else set()
    observed = wait_ready(observed, {'home', 'autonomous_running', 'training_menu', 'training_status'} | mature_pages, {'unknown', 'loading'}, 'restart_existing_training_unverified')

    def act(action, pages):
        nonlocal observed
        source_page = observed.get('page')
        result = device.step(action, stop)
        records.append({k: v for k, v in result.items() if k != 'observation'})
        observed = wait_ready(result['observation'], pages, {source_page, 'unknown', 'loading'}, 'restart_plant_home_unverified' if pages == {'home'} else 'restart_existing_training_unverified')
    if observed.get('page') == 'home':
        act('open_training', {'autonomous_running', 'training_menu', 'training_status'} | mature_pages)
    if continuation_phase(observed, device.context) not in {'navigation', 'harvest'}:
        raise RuntimeError('restart_existing_training_unverified')
    device.context['start_confirmed'] = True
    if harvest_options is not None and continuation_phase(observed, device.context) == 'harvest':
        previous, pointer = _latest_continuation(source)
        if previous is not None:
            previous_context = previous / 'context.json'
            if not navigation_only_recovery(previous, source, device.endpoint) or (previous_context.is_file() and json.loads(previous_context.read_text(encoding='utf8')).get('mode') == 'harvest'):
                raise RuntimeError('restart_plant_harvest_owner_requires_reconciliation')
        from core.autonomous_training.harvest import HarvestRunner
        if not {'learn_skills', 'update_team'} <= harvest_options.keys():
            raise RuntimeError('plant_recovery_harvest_options_missing')
        if harvest_options['learn_skills'] and (not (skill_database and Path(skill_database).is_file())):
            raise RuntimeError('external_skill_database_missing')
        with pointer.open('x', encoding='utf8') as stream:
            json.dump(dict(output=str(Path(device.output_dir).resolve()), prior_submission=str(source)), stream)
        result = HarvestRunner(device, emit, stop, external_skill_db_path=skill_database, preset=harvest_options.get('preset'), refine_skills=harvest_options.get('refine_skills', False), skill_recheck_min_pt=harvest_options.get('skill_recheck_min_pt', 60)).run(execute=True, learn_skills=harvest_options['learn_skills'], update_team=harvest_options['update_team'])
        expected = 'harvest_and_formation_home_verified' if harvest_options['update_team'] else 'harvest_home_verified'
        complete = result.get('ok') is True and result.get('reason') == expected and (result.get('last_observation', {}).get('page') == 'home')
        result.update(kind='autonomous_training', mode='start', start_submitted=True, start_confirmed=True, prior_submission=str(source), started_at=original.get('started_at'), no_refill=True, ok=complete, submitted_training_collected=complete)
        if complete:
            result['reason'] = 'started_home_verified'
        (Path(device.output_dir) / 'context.json').write_text(json.dumps(device.context, ensure_ascii=False), encoding='utf8')
        (Path(device.output_dir) / 'submitted-recovery-result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf8')
        return result
    if observed.get('page') == 'autonomous_running':
        act('open_training_menu', {'training_menu'})
    if observed.get('page') == 'training_menu':
        act('return_home', {'home'})
    elif observed.get('page') == 'training_status' and type(observed.get('remaining_seconds')) is int:
        if observed['remaining_seconds'] < 0:
            raise RuntimeError('restart_training_time_invalid')
        act('cancel_training_status', {'home'})
    else:
        raise RuntimeError('restart_existing_training_unverified')
    verified = wait_ready(device.capture(), {'home'}, {'home', 'unknown', 'loading'}, 'restart_plant_home_unverified')
    if stop.is_set() or any((o.get('page') != 'home' or o.get('endpoint') != device.endpoint for o in (observed, verified))):
        raise RuntimeError('restart_plant_home_unverified')
    result = dict(ok=True, kind='autonomous_training', mode='start', reason='started_home_verified', start_submitted=True, start_confirmed=True, prior_submission=str(source), no_refill=True, records=records, last_observation=verified, restart_training_verified=True)
    target.mkdir(parents=True, exist_ok=True)
    (target / 'submitted-recovery-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
    emit(dict(type='progress', message='原育成状态及主页已核实，开种收尾完成'))
    return result
