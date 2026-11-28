from desktop.umaa_public_context import endpoint as _public_endpoint
from desktop.umaa_public_context import lock_root as _public_lock_root
'Current-account MFA routes. No account switching or automatic task replay.'
import json
import time
from pathlib import Path
from desktop.mfa_preflight import check_device_idle
from core.action_recovery import failure_details

def _wait_training_home(device, stop, observed, endpoint):
    """Read a cancelled training status without repeating its navigation input."""
    from core.location_navigation import wait_for_transition, NavigationFailure
    try:
        return (wait_for_transition(device, stop, observed, lambda state: state.get('page') == 'home', waiting_pages={'training_status', 'home', 'unknown', 'loading'}, endpoint=endpoint), None)
    except NavigationFailure as error:
        return (error.observation, error)

def retryable_failure(task, result, output, endpoint):
    """Only evidenced non-consuming navigation may be retried."""
    if task == 'limited_legend':
        from core.limited_legend.recovery import navigation_only_failure
        return result.get('pending') is False and navigation_only_failure(result, output, endpoint)
    if result.get('action_outcome', {}).get('execution_status') == 'uncertain':
        return False
    try:
        if any((json.loads(path.read_text(encoding='utf8')).get('execution_status') == 'uncertain' for path in Path(output).rglob('recovery/last-action.json'))):
            return False
    except (OSError, ValueError):
        return False
    if task == 'plant':
        if result.get('ok') is not False or result.get('start_submitted') is not False or result.get('start_confirmed') is not False or (result.get('last_observation', {}).get('page') not in ('trainee', 'plan_races')) or (result.get('last_observation', {}).get('endpoint') != endpoint):
            return False
        allowed = {'open_training', 'close_notice', 'continue_scenario', 'scenario_next', 'scroll_trainees', 'scroll_trainees_up', *('inspect_trainee_' + str(i) for i in range(15))}
        if result.get('last_observation', {}).get('page') == 'plan_races':
            allowed.update({'continue_trainee', 'auto_inheritance', 'continue_inheritance', 'toggle_borrow', 'confirm_inheritance', 'auto_support', 'confirm_auto_support', 'continue_support', 'select_autonomous', 'continue_autonomous_tutorial', 'plan_edit', 'plan_open_date', 'plan_select_race', 'plan_save_races', 'plan_cancel_races', 'plan_races_down', 'plan_dismiss_consecutive', 'plan_next_half', 'plan_deselect_race', 'plan_close_calendar', 'plan_cancel_final', 'plan_back_support_deck', 'plan_back_inheritance', 'plan_back_trainee', 'plan_back_exam_mode', 'select_scarlet', 'select_bakushin', 'select_urara', 'select_vodka', 'select_gold_ship', 'plan_year_0', 'plan_year_1', 'plan_year_2'})
        try:
            receipts = [json.loads(p.read_text(encoding='utf8')) for p in output.glob('*-receipt.json')]
            if result.get('last_observation', {}).get('page') == 'plan_races':
                receipts = [r for r in receipts if not (r.get('status') == 'dispatch_intent' and r.get('action_id') == 'plan_races_down' and r.get('source_frame_sha256') and (r.get('endpoint') == endpoint))]
            recorded = [r for r in result.get('records', []) if r.get('performed') is True]
            return bool(receipts) and len(receipts) == len(recorded) and ({r.get('receipt_path') for r in receipts} == {r.get('receipt_path') for r in recorded}) and all((r.get('action_id') in allowed and r.get('performed') is True and r.get('source_frame_sha256') and (r.get('endpoint') == endpoint) and (r.get('observation', {}).get('endpoint') == endpoint) for r in receipts)) and (not any(output.glob('*-submitted.json')))
        except (OSError, ValueError, TypeError, AttributeError):
            return False
    if task != 'daily_legend' or result.get('ok') is not False or result.get('pending') is not False or (result.get('completed_races') != 0) or (result.get('races') != []) or (result.get('device_action_count_complete') is False):
        return False
    observed = result.get('last_observation') or {}
    if observed.get('endpoint') != endpoint or observed.get('page') not in ('home', 'races', 'events', 'legend', 'daily_legend_list'):
        return False
    allowed = {'open_races', 'open_events', 'open_legend', 'back_to_opponents', 'scroll_opponents', 'choose_opponent', 'open_details'}
    try:
        if any(output.rglob('*-submitted.json')):
            return False
        receipts = [json.loads(p.read_text(encoding='utf8')) for p in output.rglob('*-receipt.json')]
        return type(result.get('device_actions')) is int and len(receipts) == result['device_actions'] and all((r.get('action_id') in allowed and r.get('source_frame_sha256') and (r.get('observation', {}).get('endpoint') == endpoint) for r in receipts))
    except (OSError, ValueError, TypeError, AttributeError):
        return False

def release_retryable_attempt(root, endpoint):
    """Archive a finished failed attempt's occupancy marker, preserving evidence.

    Caller must first exclude other worker processes. Missing/uncertain results
    still retain occupancy; completed adapter output is required. A new request
    observes the current game state rather than replaying the previous actions.
    """
    root = Path(root).resolve()
    active = root / 'active.json'
    if not active.exists():
        return False
    from desktop.mfa_retry_lock import retry_cleanup_guard
    try:
        with retry_cleanup_guard(root) as acquired:
            if not acquired:
                return False
            original = active.read_bytes()
            attempt = json.loads(original)
            output = Path(attempt['output']).resolve()
            relative = output.relative_to(root)
            if len(relative.parts) != 2 or relative.parts[-1] != attempt.get('task') or attempt.get('endpoint') != endpoint:
                return False
            result = json.loads((output / 'adapter_result.json').read_text(encoding='utf8'))
            if attempt.get('task') in {'limited_legend', 'champions_registration', 'champions_race'} and result.get('pending') is not False:
                return False
            if result.get('game_task_complete') is not False or result.get('ok') is not False or active.read_bytes() != original:
                return False
            destination = output / 'retry-released.json'
            if destination.exists():
                return False
            active.rename(destination)
            return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
TASK_OPTIONS = {'story_event': {}, 'daily_rewards': {}, 'cleanup': {}, 'plant': {'trainee': 'scarlet'}, 'harvest': {'learn_skills': True, 'update_team': True, 'return_home': False, 'refine_skills': False, 'skill_recheck_min_pt': 60}, 'jjc': {'prefer_rematch': True, 'prefer_gift': False}, 'activities': {'races': True}, 'daily_legend': {'opponent_number': 0}, 'limited_legend': {}, 'champions_registration': {'division': None}, 'champions_race': {'rounds': 4, 'normal_only': False, 'allow_final_jewels': False, 'allow_jewels': False, 'division': None}, 'racing_carnival': {'opponent_mode': 'cycle_1234', 'prioritize_challenge': True, 'max_races': 10, 'allow_existing_jelly': True, 'max_jellies': 10}}

def resolve_daily_legend_options(options, account_number=None):
    number = options.get('opponent_number', 0)
    if account_number is not None:
        per_account = options.get('opponent_numbers')
        if isinstance(per_account, dict) and str(account_number) in per_account:
            number = per_account[str(account_number)]
    return {'opponent_number': number}


def validate_options(task, options):
    if task not in TASK_OPTIONS or not isinstance(options, dict):
        raise ValueError('mfa_task_invalid')
    defaults = TASK_OPTIONS[task]
    if task == 'champions_race':
        if set(options) - set(defaults) or type(options.get('rounds', 4)) is not int or options.get('rounds', 4) not in (1, 2, 3, 4) or (type(options.get('normal_only', True)) is not bool) or (type(options.get('allow_final_jewels', False)) is not bool) or (type(options.get('allow_jewels', False)) is not bool) or (options.get('division') not in (None, 'intermediate', 'advanced')):
            raise ValueError('大赛最多每日4轮；宝石需明确授权，每次报名最多30')
        return dict(defaults, **options)
    if task == 'champions_registration':
        if set(options) - {'division', 'preserve_existing'} or options.get('division') not in ('intermediate', 'advanced') or type(options.get('preserve_existing', False)) is not bool:
            raise ValueError('请先选择大赛报名组别：中级或高级')
        return dict(division=options['division'], **{'preserve_existing': True} if options.get('preserve_existing') else {})
    if task == 'racing_carnival':
        from core.racing_carnival.options import validate as validate_carnival
        return validate_carnival(options)
    if task == 'plant':
        from core.autonomous_training.plant_catalog import trainee_name
        from core.autonomous_training.plant_plan import DEFAULTS, validate
        from core.autonomous_training.plant_support import OPTION_KEYS, validate as validate_support
        if set(options) - set(defaults) - set(DEFAULTS) - OPTION_KEYS:
            raise ValueError('mfa_task_options_invalid')
        key = options.get('trainee', 'scarlet')
        trainee_name(key)
        return {'trainee': key, **validate(options), **validate_support(options)}
    if task == 'daily_legend':
        import re
        from core.daily_legend.catalog import opponent
        if set(options) - (set(defaults) | {'opponent_numbers'}):
            raise ValueError('mfa_task_options_invalid')
        number = options.get('opponent_number', 0)
        opponent(number)
        validated = {'opponent_number': number}
        raw_map = options.get('opponent_numbers')
        if raw_map is not None:
            if not isinstance(raw_map, dict):
                raise ValueError('mfa_task_options_invalid')
            normalized_map = {}
            for raw_account, opp in raw_map.items():
                if isinstance(raw_account, bool) or not isinstance(raw_account, (str, int)):
                    raise ValueError('mfa_task_options_invalid')
                key_text = str(raw_account)
                if not re.fullmatch(r'[1-9][0-9]*', key_text):
                    raise ValueError('mfa_task_options_invalid')
                opponent(opp)
                normalized_map[str(int(key_text))] = opp
            if normalized_map:
                validated['opponent_numbers'] = normalized_map
        return validated
    if task == 'harvest':
        threshold = options.get('skill_recheck_min_pt', 60)
        if type(threshold) is not int or not 0 <= threshold <= 9999:
            raise ValueError('mfa_skill_recheck_threshold_invalid')
        from core.harvest_presets import validate_snapshot
        if set(options) - (set(defaults) | {'preset'}):
            raise ValueError('mfa_task_options_invalid')
        if any((type(v) is not bool for k, v in options.items() if k not in {'skill_recheck_min_pt', 'preset'})):
            raise ValueError('mfa_task_options_invalid')
        result = {**defaults, **options}
        if options.get('preset') is not None:
            result['preset'] = validate_snapshot(options['preset'])
        return result
    if set(options) - set(defaults) or any((type(v) is not bool for k, v in options.items() if not (task == 'harvest' and k == 'skill_recheck_min_pt'))):
        raise ValueError('mfa_task_options_invalid')
    return {**defaults, **options}

def execute_current(task, options, **kwargs):
    auto_restart = kwargs.get('auto_restart_game', True)
    if not auto_restart and kwargs.get('recover_connection') is None:
        return _execute_current(task, options, **kwargs)
    from core.stall_watch import StallWatch, recover_stall
    from desktop.mfa_restart_resume import RestartScope, continue_task, continue_session_task
    from core.game_runtime import GameRuntime
    endpoint, stop, output = (kwargs['endpoint'], kwargs['stop'], kwargs['output'])
    with RestartScope(task, kwargs) as restart_scope:
        if not auto_restart:
            result = _execute_current(task, options, **kwargs)
        else:
            with StallWatch(endpoint, stop) as watch:
                try:
                    result = _execute_current(task, options, **kwargs)
                except RuntimeError as error:
                    if watch.last is None:
                        raise
                    result = dict(ok=False, reason=str(error), last_observation=watch.last, device_action_count_complete=False, **failure_details(error))
        if auto_restart and (not result.get('ok')) and (not stop.is_set()):
            try:
                recovery = recover_stall(result, lambda: GameRuntime(endpoint, kwargs['adb_path'], output / 'stall-recovery', agree_startup=True, emit=kwargs['emit'], stop=stop), stop, watch.last)
                if recovery is not None:
                    result['stall_recovery'] = recovery
                    if task == 'daily_legend':
                        result.setdefault('legend_output_dir', str(output))
                    if recovery.get('ok') is True:
                        result = continue_task(task, options, result, recovery, kwargs, _execute_current, restart_scope)
                        if result.get('ok') is not True:
                            result['pending'] = True
                    else:
                        result['pending'] = True
            except (RuntimeError, OSError) as error:
                result.update(stall_recovery=dict(ok=False, reason=str(error)), pending=True)
        reconnect = kwargs.get('recover_connection')
        if not result.get('ok') and (not stop.is_set()) and (reconnect is not None) and (task != 'daily_legend') and ('restart_resume' not in result) and (result.get('reason') != 'account_connection_conflict') and (not result.get('account_conflict')) and (not result.get('account_paused')) and (result.get('action_outcome', {}).get('decision') != 'pause_account'):
            try:
                recovery = reconnect()
                if recovery is not None:
                    result = continue_session_task(task, options, result, recovery, kwargs, _execute_current, restart_scope)
            except (RuntimeError, OSError, ValueError) as error:
                result['session_recovery'] = dict(ok=False, reason=str(error))
    return result

def _execute_current(task, options, *, endpoint, adb_path, output, skill_database, emit, stop, resume_finish_result=None, resume_harvest_summary=None, resume_harvest_from=None, resume_activities_from=None, resume_plant_from=None, recovery_harvest_options=None, resume_jjc_from=None, resume_settlement_from=None, resume_legend_from=None, resume_registration_from=None, resume_limited_from=None, resume_champions_race_from=None, recover_connection=None, auto_restart_game=True, verify_limited_account=None, verify_restart_account=None, verify_resume_owner=None, restart_from=None, restart_account_verified=False, resume_racing_carnival_from=None):
    from core.task_entry import prepare_task_entry as prepare_entry

    def prepare_task_entry(*args, **values):
        result = prepare_entry(*args, **values)
        from desktop.mfa_restart_resume import checkpoint
        checkpoint()
        return result
    if restart_from is not None:
        if restart_account_verified is not True:
            raise RuntimeError('restart_account_unverified')
        from desktop.mfa_restart_owners import resume_owner
        return resume_owner(task, options, restart_from, endpoint=endpoint, adb_path=adb_path, output=output, skill_database=skill_database, emit=emit, stop=stop, verify_account=verify_restart_account, verify_resume_owner=verify_resume_owner, recovery_harvest_options=recovery_harvest_options, original_sources=dict(plant=resume_plant_from, daily_legend=resume_legend_from, limited_legend=resume_limited_from, champions_registration=resume_registration_from, champions_race=resume_champions_race_from, jjc=resume_jjc_from, activities=resume_activities_from))
    if task == 'plant':
        from core.autonomous_training.live import AutonomousDevice
        from core.autonomous_training.runner import AutonomousRunner
        device = AutonomousDevice(endpoint, adb_path, output)
        from core.autonomous_training.plant_plan import validate
        device.context['plant_options'] = validate(options)
        from core.autonomous_training.plant_support import validate as validate_support
        support = validate_support(options)
        if support:
            device.context['support_options'] = support
        from core.autonomous_training.plant_catalog import INITIAL_VARIANTS, trainee_name
        trainee = options.get('trainee', 'scarlet')
        device.context['required_trainee'] = trainee_name(trainee)
        device.context['required_variant'] = INITIAL_VARIANTS.get(trainee, trainee)
        emit(dict(type='progress', message='开种目标：' + device.context['required_trainee']))
        device.connect()
        if resume_plant_from is not None:
            from desktop.mfa_plant_resume import resume_submitted_plant
            result = resume_submitted_plant(device, resume_plant_from, recovery_harvest_options or {}, skill_database, emit, stop, verify_account=verify_restart_account, verify_owner=verify_resume_owner)
        else:
            prepare_task_entry(device, stop, emit, task=task)
            result = AutonomousRunner(device, emit, stop).run('start')
        if result.get('reason') in ('still_running', 'harvest_required') and (not result.get('start_submitted')):
            device.context['preserve_existing_training'] = True
            receipt = device.step('cancel_training_status', stop)
            result['records'].append({k: v for k, v in receipt.items() if k != 'observation'})
            arrival, navigation_failure = _wait_training_home(device, stop, receipt['observation'], endpoint)
            observed = arrival
            if navigation_failure is None:
                observed, navigation_failure = _wait_training_home(device, stop, device.capture(), endpoint)
            preserved = navigation_failure is None and arrival.get('page') == observed.get('page') == 'home'
            reason = 'operator_stop' if navigation_failure is not None and navigation_failure.kind == 'operator_stop' else 'existing_training_preserved' if preserved else 'home_not_verified'
            result.update(ok=preserved, reason=reason, last_observation=observed, skipped=True)
            if navigation_failure is not None:
                result['navigation_trace'] = navigation_failure.trace
            emit(dict(type='progress', message='保留已有育成，未重复开种'))
    elif task == 'harvest':
        from core.autonomous_training.live import AutonomousDevice
        from core.autonomous_training.harvest import HarvestRunner
        device = AutonomousDevice(endpoint, adb_path, output)
        capture = device.capture
        latest = None

        def capture_latest(*args, **kwargs):
            nonlocal latest
            observed = capture(*args, **kwargs)
            latest = observed
            return observed
        device.capture = capture_latest
        prior_harvest = None
        if resume_harvest_from is not None:
            source = Path(resume_harvest_from).resolve()
            prior_harvest = json.loads((source / 'adapter_result.json').read_text(encoding='utf8'))
            context = json.loads((source / 'context.json').read_text(encoding='utf8'))
            owner = Path(prior_harvest.get('harvest_output_dir') or source).resolve()
            parts = source.parts
            if len(parts) < 5 or source.name != 'harvest' or (not source.parent.name.isdigit()) or (source.parent.parent.name != 'tasks') or (not owner.is_relative_to(source.parents[3])) or (owner.name != 'harvest') or (source == Path(output).resolve()) or (prior_harvest.get('ok') is not False) or (prior_harvest.get('current_account_only') is not True) or (context.get('mode') != 'harvest') or (prior_harvest.get('last_observation', {}).get('endpoint') != endpoint) or (prior_harvest.get('action_outcome', {}).get('execution_status') == 'uncertain') or any((context.get(key) is not None and context[key] != options[value] for key, value in (('harvest_learn_skills', 'learn_skills'), ('harvest_update_team', 'update_team')))):
                raise RuntimeError('harvest_resume_owner_unverified')
            for path in owner.rglob('last-action.json'):
                if json.loads(path.read_text(encoding='utf8')).get('execution_status') == 'uncertain':
                    raise RuntimeError('harvest_pause_input_uncertain')
            device.context.update(context)
            device.output_dir = owner / ('pause-' + str(time.time_ns()))
            device.output_dir.mkdir()
        if resume_harvest_summary is not None:
            device.context['harvest_summary'] = dict(resume_harvest_summary)
        device.connect()
        original_tutorial = None
        completed_purchase_return = None
        if resume_harvest_from is None and resume_harvest_summary is None and (resume_finish_result is None) and (resume_settlement_from is None):
            from core.autonomous_training.purchase_return_resume import restore_latest_purchase_return
            completed_purchase_return = restore_latest_purchase_return(output, device, stop, options)
            if completed_purchase_return is not None:
                resume_finish_result = completed_purchase_return
                emit(dict(type='progress', message='收菜：原技能已确认购买，只继续关闭提示和结算，不重新学习'))
            else:
                from core.autonomous_training.tutorial_resume import restore_latest_tutorial
                original_tutorial = restore_latest_tutorial(output, device, stop, options.get('preset'))
            if original_tutorial is not None:
                emit(dict(type='progress', message='收菜：技能教程已完成；恢复角色元数据，按当前技能页继续，不重开育成或重放购买'))
        completed_at_home = False
        if prior_harvest is not None:
            observed = device.capture()
            from core.training_continuation import OWNED_HARVEST_PAGES, continuation_phase
            allowed = OWNED_HARVEST_PAGES | {'home', 'training_status'}
            if continuation_phase(observed, device.context) == 'harvest':
                allowed = allowed | {observed.get('page')}
            if observed.get('endpoint') != endpoint or observed.get('page') not in allowed:
                raise RuntimeError('harvest_pause_current_page_unverified')
            if device.context.get('completion_submitted'):
                from desktop.mfa_harvest_restart import completion_receipt
                completion_receipt(prior_harvest, owner, endpoint)
                if observed['page'] in {'skills', 'finish_ready', 'finish_confirm', 'history', 'history_summary', 'history_races', 'training_status'}:
                    raise RuntimeError('harvest_pause_training_changed')
                completed_at_home = observed['page'] == 'home'
            elif observed['page'] == 'home':
                unstarted = prior_harvest.get('last_observation', {}).get('page') == 'home' and prior_harvest.get('records') == [] and (prior_harvest.get('device_actions') == 0) and (not any((device.context.get(key) for key in ('harvest_skill_attempted', 'finish_submitted', 'formation_requested'))))
                if not unstarted:
                    raise RuntimeError('harvest_pause_original_training_unverified')
            skill = prior_harvest.get('skill_result') or {}
            if not device.context.get('finish_submitted') and skill.get('ok') is True and (observed['page'] == 'finish_ready'):
                resume_finish_result = skill
        if resume_settlement_from is not None:
            from core.autonomous_training.harvest import verify_score_record_resume
            verify_score_record_resume(resume_settlement_from, device, stop)
        if resume_settlement_from is None and resume_finish_result is None and (resume_harvest_summary is None) and (prior_harvest is None) and (original_tutorial is None):
            prepare_task_entry(device, stop, emit, task=task)
        runner = HarvestRunner(device, emit, stop, external_skill_db_path=skill_database, resume_finish_result=resume_finish_result, preset=options.get('preset'), refine_skills=options.get('refine_skills', False), skill_recheck_min_pt=options.get('skill_recheck_min_pt', 60))
        if completed_at_home:
            from desktop.mfa_harvest_restart import resume_completed_training
            result = resume_completed_training(device, prior_harvest, emit, stop, options)
        else:
            result = runner.run(execute=True, learn_skills=options['learn_skills'], update_team=options['update_team'])
        if completed_purchase_return is not None:
            result['completed_purchase_resume'] = completed_purchase_return
            result['device_actions'] = result.get('device_actions', 0) + completed_purchase_return.get('device_action_count', 0)
        if prior_harvest is not None:
            previous_records = prior_harvest.get('records', [])
            current_records = result.get('records', [])
            if previous_records and current_records[:len(previous_records)] != previous_records:
                result['records'] = [*previous_records, *current_records]
                result['device_actions'] = prior_harvest.get('device_actions', 0) + result.get('device_actions', 0)
            for key in ('skill_result', 'skill_batches', 'finish_result'):
                if key in prior_harvest:
                    result.setdefault(key, prior_harvest[key])
            result.update(harvest_output_dir=str(owner), harvest_resume_from=str(source))
        if result.get('ok') is not True and isinstance(latest, dict):
            result['last_observation'] = latest
        if resume_settlement_from is not None:
            result['resumed_from'] = str(resume_settlement_from)
        if result.get('reason') in ('no_completed_training_at_entry', 'still_running') and (not stop.is_set()):
            result['skip_reason'] = result['reason']
            observed = result.get('last_observation', {})
            result['remaining_seconds'] = observed.get('remaining_seconds')
            action = {'scenario': 'leave_empty_scenario', 'training_status': 'cancel_training_status'}.get(observed.get('page'))
            navigation_failure = None
            if action:
                device.context.update(empty_harvest=True, mode='harvest' if action == 'leave_empty_scenario' else 'start', preserve_existing_training=True)
                receipt = device.step(action, stop)
                result.setdefault('records', []).append(receipt)
                if isinstance(result.get('device_actions'), int) and receipt.get('performed'):
                    result['device_actions'] += 1
                observed = receipt.get('observation', {})
                if action == 'cancel_training_status':
                    observed, navigation_failure = _wait_training_home(device, stop, observed, endpoint)
                    if navigation_failure is not None:
                        result.update(ok=False, reason='operator_stop' if navigation_failure.kind == 'operator_stop' else 'home_not_verified', navigation_trace=navigation_failure.trace)
                result['last_observation'] = observed
            if navigation_failure is None and observed.get('page') == 'home' and (observed.get('endpoint') == endpoint) and (not stop.is_set()):
                result.update(ok=True, skipped=True, reason='harvest_empty_home_verified')
    elif task == 'jjc':
        from core.jjc.live import JjcDevice
        from core.jjc.runner import JjcRunner
        if resume_jjc_from is not None:
            try:
                if stop.is_set():
                    raise RuntimeError('operator_stop')
                from core.game_runtime import GameRuntime
                runtime = GameRuntime(endpoint, adb_path, output / 'resume-startup', agree_startup=True, emit=emit, stop=stop)
                launched = runtime.launch_if_exited()
                startup = runtime.prepare(after_restart=bool(launched and launched['process_absent']), allow_restart=False)
                runtime.save('startup-result.json', startup)
                if not startup['ok']:
                    raise RuntimeError(startup['reason'])
                if launched and launched.get('process_absent') and (verify_restart_account is None or verify_restart_account() is not True):
                    raise RuntimeError('restart_account_unverified')
            except RuntimeError as error:
                return dict(kind='jjc', ok=False, reason=str(error), pending_race=True, completed_races=0, records=[], resume_unstarted=True, resumed_from=str(resume_jjc_from))
        device = JjcDevice(endpoint, adb_path, output)
        device.connect()
        prepare_task_entry(device, stop, emit)
        result = JjcRunner(device, emit, stop, prefer_rematch=options['prefer_rematch'], prefer_gift=options['prefer_gift'], verify_resume_account=verify_restart_account).run(resume_from=resume_jjc_from)
    elif task == 'story_event':
        from core.story_event.runner import EventDevice, EventRunner
        device = EventDevice(endpoint, adb_path, output)
        device.connect()
        prepare_task_entry(device, stop, emit)
        result = EventRunner(device, emit, stop).run()
    elif task == 'champions_registration':
        from core.champions_registration.live import ChampionsRegistrationDevice
        from core.champions_registration.runner import ChampionsRegistrationRunner
        device = ChampionsRegistrationDevice(endpoint, adb_path, output, options['division'])
        device.connect()
        result = ChampionsRegistrationRunner(device, emit, stop, prepare_entry=lambda: prepare_task_entry(device, stop, emit), preserve_existing=options.get('preserve_existing', False)).run(resume_from=resume_registration_from)
    elif task == 'champions_race':
        from core.champions_race.live import ChampionsRaceDevice
        from core.champions_race.runner import ChampionsRaceRunner
        device = ChampionsRaceDevice(endpoint, adb_path, output)
        device.connect()
        result = ChampionsRaceRunner(device, emit, stop, prepare_entry=lambda: prepare_task_entry(device, stop, emit), max_rounds=options['rounds'], allow_final_jewels=options['allow_final_jewels'], allow_jewels=options['allow_jewels'], verify_resume_account=verify_restart_account).run(resume_from=resume_champions_race_from, restart_task=True)
    elif task == 'racing_carnival':
        from desktop.mfa_racing_carnival import execute as execute_carnival
        return execute_carnival(options, endpoint=endpoint, adb_path=adb_path, output=output, emit=emit, stop=stop, resume_from=resume_racing_carnival_from)
    (output / 'context.json').write_text(json.dumps(device.context, ensure_ascii=False, indent=2), encoding='utf8')
    return result

class CurrentAccountBackend:
    """Internal adapter; distributed preview never enables this live route.

    active.json prevents overlapping MFA calls, not new requests after finished failures.
    The outer run additionally uses the UMAA/Pacer OS device lease. Unmodified
    external controllers are still checked separately and are not claimed locked.
    """

    def __init__(self, output_root, *, endpoint=_public_endpoint(), adb_path='', skill_database=None, live_enabled=False, executor=execute_current, device_check=check_device_idle, account_number=None):
        self.root = Path(output_root)
        self.endpoint, self.adb_path = (endpoint, adb_path)
        self.skill_database, self.live_enabled = (skill_database, live_enabled)
        self.executor = executor
        self.device_check = device_check
        self.account_number = account_number

    def run(self, task, options, emit, stop, *, execute=False):
        from core.device_occupancy import DeviceBusy, hold
        from contextlib import nullcontext
        from core.screenshot_retention import ScreenshotSession
        scope = hold(_public_lock_root(), self.endpoint, 'umaa_daily') if execute and self.live_enabled and (self.endpoint == _public_endpoint()) else nullcontext()
        try:
            with scope, ScreenshotSession() as screenshots:
                result = self._run(task, options, emit, stop, execute=execute)
                screenshots.finish(result, complete=result.get('game_task_complete') is True)
                return result
        except DeviceBusy as error:
            return dict(kind=task, ok=False, game_task_complete=False, reason='device_busy', device_actions=0, device_owner=error.owner)

    def _run(self, task, options, emit, stop, *, execute=False):
        options = validate_options(task, options)
        base = dict(kind=task, ok=False, game_task_complete=False, current_account_only=True)
        if stop.is_set():
            return {**base, 'reason': 'operator_stop', 'device_actions': 0}
        if not execute:
            emit(dict(type='progress', message='[预演] ' + json.dumps(options, ensure_ascii=False)))
            return {**base, 'ok': True, 'dry_run': True, 'device_actions': 0, 'reason': 'dry_run_plan_only', 'options': options, 'stop_queue': task == 'harvest' and options.get('return_home') is True}
        if self.live_enabled is not True:
            return {**base, 'reason': 'mfa_live_not_enabled', 'device_actions': 0}
        if self.endpoint != _public_endpoint():
            return {**base, 'reason': 'device_endpoint_not_allowed', 'device_actions': 0}
        if task == 'harvest' and options['learn_skills'] and (not (self.skill_database and Path(self.skill_database).is_file())):
            return {**base, 'reason': 'external_skill_database_missing', 'device_actions': 0}
        try:
            self.device_check()
        except RuntimeError as error:
            return {**base, 'reason': str(error), 'device_actions': 0}
        self.root.mkdir(parents=True, exist_ok=True)
        release_retryable_attempt(self.root, self.endpoint)
        output = self.root / str(time.time_ns()) / task
        active = self.root / 'active.json'
        try:
            with active.open('x', encoding='utf8') as stream:
                json.dump(dict(task=task, endpoint=self.endpoint, output=str(output), options=options), stream)
        except FileExistsError:
            return {**base, 'reason': 'mfa_active_attempt_requires_review', 'device_actions': 0}
        output.mkdir(parents=True)
        from contextlib import nullcontext
        from core.passive_popup_runtime import PassivePopupSession
        popup_scope = PassivePopupSession(self.endpoint, stop) if task != 'batch' else nullcontext()
        try:
            with popup_scope as popups:
                result = self.executor(task, options, endpoint=self.endpoint, adb_path=self.adb_path, output=output, skill_database=self.skill_database, emit=emit, stop=stop)
                if popups is not None:
                    popups.annotate(result)
            (output / 'backend_result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
            complete = result.get('ok') is True and (not result.get('dry_run')) and (not stop.is_set())
            accepted_skip = False
            if task == 'harvest':
                expected = 'harvest_and_formation_home_verified' if options['update_team'] else 'harvest_home_verified'
                observed = result.get('last_observation', {})
                complete = complete and result.get('reason') == expected and (observed.get('page') == 'home' and observed.get('endpoint') == self.endpoint)
                if result.get('skipped') is True and result.get('reason') == 'harvest_empty_home_verified':
                    complete = result.get('ok') is True and (not stop.is_set()) and (not result.get('dry_run')) and (observed.get('page') == 'home' and observed.get('endpoint') == self.endpoint)
            elif task == 'plant':
                observed = result.get('last_observation', {})
                complete = complete and result.get('reason') in ('started_home_verified', 'existing_training_preserved') and (observed.get('page') == 'home' and observed.get('endpoint') == self.endpoint)
            elif task == 'story_event':
                observed = result.get('last_observation', {})
                complete = complete and (not result.get('skipped')) and (result.get('reason') == 'event_home_verified') and (observed.get('page') == 'home' and observed.get('endpoint') == self.endpoint)
            elif task == 'daily_rewards':
                observed = result.get('last_observation', {})
                complete = complete and result.get('reason') == 'daily_rewards_home_verified' and (observed.get('page') == 'home' and observed.get('endpoint') == self.endpoint)
            elif task == 'cleanup':
                observed = result.get('last_observation', {})
                complete = complete and result.get('reason') == 'account_cleanup_home_verified' and (observed.get('page') == 'home' and observed.get('endpoint') == self.endpoint)
            elif task == 'champions_registration':
                observed = result.get('last_observation', {})
                registration = result.get('registration') or {}
                complete = complete and result.get('reason') == 'champions_registration_home_verified' and (result.get('pending') is False and (registration.get('division') == options['division'] or (options.get('preserve_existing') is True and result.get('already_registered') is True and (registration.get('division') in {'intermediate', 'advanced'}))) and (bool(registration.get('period')) or registration.get('identity_basis') == 'visible_registered_league') and bool(registration.get('frame_sha256')) and (registration.get('endpoint') == self.endpoint) and (observed.get('page') == 'home') and (observed.get('endpoint') == self.endpoint))
            elif task == 'champions_race':
                observed = result.get('last_observation', {})
                complete = complete and result.get('reason') in {'champions_race_home_verified', 'champions_race_available_entries_finished'} and (result.get('pending') is False and result.get('pending_matches') == 0 and (type(result.get('completed_rounds')) is int) and (0 <= result['completed_rounds'] <= 4) and (result['completed_rounds'] >= options['rounds'] or (result.get('reason') == 'champions_race_available_entries_finished' and result.get('skipped') is True and (result.get('limit_reason') == 'free_tickets_exhausted'))) and (observed.get('page') == 'home') and (observed.get('endpoint') == self.endpoint))
            elif task == 'limited_legend':
                from core.limited_legend.completion import completion_verified
                complete = complete and completion_verified(result, self.endpoint)
            elif task == 'daily_legend':
                observed = result.get('last_observation', {})
                complete = complete and result.get('reason') in ('all_remaining_finished', 'no_remaining_tickets') and (result.get('tickets_after') == 0 and (not result.get('pending')) and (observed.get('page') == 'home') and (observed.get('endpoint') == self.endpoint))
            elif task == 'racing_carnival':
                from desktop.mfa_racing_carnival import classify as classify_carnival
                proof = classify_carnival(result, self.endpoint)
                complete = complete and proof['game_task_complete']
                accepted_skip = proof['skip_verified']
                result.update(proof)
            if task in {'limited_legend', 'champions_race', 'racing_carnival'} and (not stop.is_set()) and (not result.get('dry_run')):
                from core.event_menu import skipped_result_verified
                accepted_skip = skipped_result_verified(result, task, self.endpoint)
            result = {**base, **result, 'ok': bool(complete or accepted_skip), 'game_task_complete': bool(complete), 'skip_verified': bool(accepted_skip), 'output_dir': str(output)}
            if stop.is_set():
                result['reason'] = 'operator_stop'
            elif not complete and result.get('ok') is False and (not result.get('reason')):
                result['reason'] = 'mfa_completion_not_verified'
            if task == 'harvest' and complete:
                result['stop_queue'] = options['return_home']
        except Exception as error:
            entry_failure = isinstance(error, RuntimeError) and str(error).startswith(('task_entry_page_unverified:', 'return_home_'))
            result = {**base, **failure_details(error), 'reason': str(error) if entry_failure else 'mfa_backend_exception', 'error_type': type(error).__name__, 'output_dir': str(output), 'device_action_count_complete': False}
        if isinstance(popup_scope, PassivePopupSession):
            popup_scope.annotate(result)
            if popup_scope.failure:
                result.update(ok=False, game_task_complete=False, reason=popup_scope.failure)
        outcome = result.get('action_outcome', {})
        if outcome.get('decision') == 'pause_account':
            result.update(ok=False, game_task_complete=False, reason='account_connection_conflict')
        if outcome.get('execution_status') == 'uncertain':
            result['device_action_count_complete'] = False
        if task == 'limited_legend' and (not result.get('game_task_complete')) and (not (result.get('ok') and result.get('skip_verified'))):
            from core.limited_legend.recovery import navigation_only_failure
            result['pre_entry_verified'] = navigation_only_failure(result, output, self.endpoint)
            result['pending'] = not result['pre_entry_verified']
        if task in {'champions_registration', 'champions_race'} and (not result.get('game_task_complete')):
            result.setdefault('pending', True)
        result['retry_allowed'] = retryable_failure(task, result, output, self.endpoint)
        if isinstance(popup_scope, PassivePopupSession) and popup_scope.failure:
            result['retry_allowed'] = False
        (output / 'adapter_result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
        if result['game_task_complete'] or (result.get('ok') and result.get('skip_verified')):
            active.unlink()
        else:
            release_retryable_attempt(self.root, self.endpoint)
        return result
