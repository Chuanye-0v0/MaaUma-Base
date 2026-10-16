"""Dispatch restarted tasks through their existing progress/receipt owners."""
from desktop.umaa_public_context import endpoint as _public_endpoint
import json
import time
from pathlib import Path

def read(path):
    value = json.loads(Path(path).read_text(encoding='utf8'))
    if not isinstance(value, dict):
        raise RuntimeError('restart_object_required')
    return value

def navigation_only(prior, output, endpoint):
    """Only known non-consuming entry controls may be selected afresh."""
    allowed = {'open_races', 'open_events', 'open_legend', 'open_details', 'open_selection', 'open_champions', 'choose_advanced', 'choose_intermediate', 'close_league_choice', 'open_training', 'close_notice', 'continue_scenario', 'scenario_next', 'continue_trainee', 'auto_inheritance', 'toggle_borrow', 'confirm_inheritance', 'continue_inheritance', 'auto_support', 'confirm_auto_support', 'continue_support', 'select_autonomous', 'continue_autonomous_tutorial', 'scroll_trainees', 'scroll_trainees_up', 'select_scarlet', 'select_bakushin', 'select_gold_ship', 'select_vodka', 'select_urara'}
    if any((prior.get(k) is True for k in ('start_submitted', 'start_confirmed', 'pending', 'pending_race', 'pending_exchange'))):
        return False
    for path in output.rglob('*-submitted.json'):
        if path.name not in {'choose_advanced-submitted.json', 'choose_intermediate-submitted.json'}:
            return False
    for path in output.rglob('*-receipt.json'):
        value = read(path)
        action = value.get('action_id', '')
        selectable = any((action.startswith(prefix) and action[len(prefix):].isdigit() for prefix in ('horse_', 'inspect_trainee_')))
        if action not in allowed and (not selectable) or value.get('endpoint', (value.get('observation') or {}).get('endpoint')) != endpoint:
            return False
    outcome = prior.get('action_outcome') or {}
    return not outcome.get('input_attempts') or outcome.get('action_id') in allowed

def resume_owner(task, options, source, *, endpoint, adb_path, output, skill_database, emit, stop, verify_account, original_sources, recovery_harvest_options=None, verify_resume_owner=None, account_verified=False):
    source = Path(source).resolve()
    output = Path(output).resolve()
    if source.parent != output or endpoint != _public_endpoint():
        raise RuntimeError('restart_source_scope_invalid')
    prior = read(source)
    if prior.get('ok') is not False or stop.is_set():
        raise RuntimeError('restart_owner_not_failed')
    if task == 'harvest' and (output / 'context.json').exists():
        from desktop.mfa_harvest_restart import resume_context
        context = resume_context(prior, output)
    else:
        context = read(output / 'context.json') if (output / 'context.json').exists() else {}
    if task in {'plant', 'champions_registration', 'daily_legend', 'limited_legend'} and (not original_sources.get(task)) and navigation_only(prior, output, endpoint):
        from desktop.mfa_backend import _execute_current
        return _execute_current(task, options, endpoint=endpoint, adb_path=adb_path, output=output, skill_database=skill_database, emit=emit, stop=stop, auto_restart_game=False, verify_restart_account=verify_account, verify_limited_account=verify_account)
    if task == 'plant':
        from core.autonomous_training.live import AutonomousDevice
        from desktop.mfa_plant_resume import finish_restarted_plant
        origin = original_sources.get('plant') or source
        if context.get('mode') == 'harvest':
            from desktop.mfa_plant_resume import submission_source, resolve_continuation_owner, resume_submitted_plant
            origin = Path(original_sources.get('plant') or prior.get('prior_submission', '')).resolve()
            if not origin.is_file() or context.get('prior_submission') != str(origin) or submission_source(prior, origin.parents[4], endpoint) != origin:
                raise RuntimeError('restart_plant_harvest_owner_unverified')
            resolve_continuation_owner(origin, probe_output=output, probe_report=prior)
            device = AutonomousDevice(endpoint, adb_path, output / 'restart-harvest')
            device.connect()
            return resume_submitted_plant(device, origin, recovery_harvest_options or {}, skill_database, emit, stop, verify_owner=verify_resume_owner or (lambda: not stop.is_set()))
        device = AutonomousDevice(endpoint, adb_path, output / 'restart-return')
        device.connect()
        return finish_restarted_plant(device, origin, emit, stop, account_verified=True, recovery_output=output, harvest_options=recovery_harvest_options, skill_database=skill_database)
    if task == 'harvest':
        from core.autonomous_training.live import AutonomousDevice
        from core.autonomous_training.harvest import HarvestRunner
        if context.get('finish_submitted') and (not context.get('completion_submitted')) or (context.get('harvest_skill_attempted') and (not context.get('completion_submitted'))) or (prior.get('skill_result') and prior['skill_result'].get('ok') is not True):
            raise RuntimeError('restart_harvest_consumption_requires_reconciliation')
        harvest_owner = Path(prior.get('harvest_output_dir') or output).resolve()
        device = AutonomousDevice(endpoint, adb_path, harvest_owner / ('restart-harvest-' + str(time.time_ns())))
        device.context.update(context)
        device.connect()
        if context.get('completion_submitted'):
            from desktop.mfa_harvest_restart import resume_completed_training
            return resume_completed_training(device, prior, emit, stop, options, operation_verified=account_verified, skill_database=skill_database)
        result = HarvestRunner(device, emit, stop, external_skill_db_path=skill_database, refine_skills=options.get('refine_skills', False), skill_recheck_min_pt=options.get('skill_recheck_min_pt', 60)).run(execute=True, learn_skills=options['learn_skills'], update_team=options['update_team'])
        if result.get('reason') in {'no_completed_training_at_entry', 'still_running'}:
            observed = result.get('last_observation') or {}
            action = {'scenario': 'leave_empty_scenario', 'training_status': 'cancel_training_status'}.get(observed.get('page'))
            if action:
                device.context.update(empty_harvest=True, preserve_existing_training=True, mode='harvest' if action == 'leave_empty_scenario' else 'start')
                receipt = device.step(action, stop)
                result.setdefault('records', []).append(receipt)
                observed = receipt.get('observation') or {}
            verified = device.capture()
            if not stop.is_set() and all((o.get('page') == 'home' and o.get('endpoint') == endpoint for o in (observed, verified))):
                result.update(ok=True, skipped=True, reason='harvest_empty_home_verified', last_observation=verified)
        return result
    if task == 'jjc':
        from core.jjc.live import JjcDevice
        from core.jjc.runner import JjcRunner
        device = JjcDevice(endpoint, adb_path, output)
        device.connect()
        return JjcRunner(device, emit, stop, prefer_rematch=options['prefer_rematch'], prefer_gift=options['prefer_gift']).run(resume_from=source, restart_verified=True)
    if task == 'story_event':
        from core.story_event.runner import EventDevice, EventRunner, MultiEventRunner
        if context.get('pending_spin'):
            raise RuntimeError('restart_pending_spin_requires_reconciliation')
        if context.get('event_cycle'):
            from core.story_event.cycle import read_resume, pending
            context = read_resume(source, endpoint)
            if pending(context):
                raise RuntimeError('event_pending_requires_reconciliation')
            device = EventDevice(endpoint, adb_path, output)
            device.context.update(context)
            device.connect()
            return MultiEventRunner(device, emit, stop).run()
        device = EventDevice(endpoint, adb_path, output)
        device.context.update(context)
        device.context.update(stage='restart_entry', entry_pending=False, event_verified=False)
        device.context.pop('entry_banner_id', None)
        device.connect()
        return EventRunner(device, emit, stop).run()
    if task == 'champions_registration':
        from core.champions_registration.live import ChampionsRegistrationDevice
        from core.champions_registration.runner import ChampionsRegistrationRunner
        from core.task_entry import prepare_task_entry
        device = ChampionsRegistrationDevice(endpoint, adb_path, output, options['division'])
        device.connect()
        return ChampionsRegistrationRunner(device, emit, stop, prepare_entry=lambda: prepare_task_entry(device, stop, emit)).run(resume_from=original_sources.get(task) or output, restart_verified=True)
    if task == 'champions_race':
        from core.champions_race.live import ChampionsRaceDevice
        from core.champions_race.runner import ChampionsRaceRunner
        from core.task_entry import prepare_task_entry
        device = ChampionsRaceDevice(endpoint, adb_path, output)
        device.connect()
        return ChampionsRaceRunner(device, emit, stop, prepare_entry=lambda: prepare_task_entry(device, stop, emit), max_rounds=options['rounds'], allow_final_jewels=options['allow_final_jewels'], allow_jewels=options.get('allow_jewels', False), verify_resume_account=lambda: True).run(resume_from=original_sources.get(task) or prior.get('champions_race_output_dir') or output, restart_task=True)
    if task in {'daily_legend', 'limited_legend'}:
        from core.task_entry import prepare_task_entry
        if task == 'daily_legend':
            from core.daily_legend.live import DailyLegendDevice
            from core.daily_legend.runner import DailyLegendRunner
            device = DailyLegendDevice(endpoint, adb_path, output, options['opponent_number'])
            runner = DailyLegendRunner(device, emit, stop, prepare_entry=lambda: prepare_task_entry(device, stop, emit))
        else:
            from core.limited_legend.live import LimitedLegendDevice
            from core.limited_legend.runner import LimitedLegendRunner
            device = LimitedLegendDevice(endpoint, adb_path, output)
            runner = LimitedLegendRunner(device, emit, stop, verify_resume_account=lambda: True, prepare_entry=lambda: prepare_task_entry(device, stop, emit))
        device.connect()
        owner = original_sources.get(task) or output
        return runner.run(resume_from=owner)
    if task in {'activities', 'daily_rewards', 'cleanup'}:
        from core.daily_tasks.live import DailyDevice
        from core.daily_tasks.runner import DailyTasksRunner
        from core.task_entry import prepare_task_entry
        device = DailyDevice(endpoint, adb_path, output)
        device.connect()
        if task == 'cleanup':
            prepare_task_entry(device, stop, emit)
            return DailyTasksRunner(device, emit, stop).cleanup()
        owner = Path(original_sources.get(task) or prior.get('daily_output_dir') or output)
        device.output_dir = owner.parent / ('restart-daily-' + source.stem)
        device.output_dir.mkdir(parents=True, exist_ok=True)
        result = DailyTasksRunner(device, emit, stop).run(races=task == 'activities' and options['races'], rewards=task == 'daily_rewards', resume_from=owner, restart_verified=True)
        (device.output_dir / 'context.json').write_text(json.dumps(device.context, ensure_ascii=False), encoding='utf8')
        return dict(result, daily_output_dir=str(device.output_dir))
    raise RuntimeError('restart_owner_not_supported:' + task)
