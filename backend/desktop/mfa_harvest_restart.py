"""Reconcile an already finished training, without another finish or skill spend."""
import hashlib
import json
from pathlib import Path
POST_COMPLETION = {'training_rating', 'factor_result', 'factor_confirm', 'trained_detail', 'training_complete', 'score_record', 'title_notice', 'training_summary', 'affinity_result', 'training_rewards', 'activity_points_result', 'activity_points_claim', 'team_rank_update', 'team_rank_reward'}

def completion_receipt(prior, root, endpoint):
    for record in prior.get('records', []):
        if record.get('action_id') != 'complete_training' or record.get('performed') is not True:
            continue
        path = Path(record.get('receipt_path', '')).resolve()
        if not path.is_relative_to(root):
            continue
        receipt = json.loads(path.read_text(encoding='utf8'))
        after = receipt.get('observation') or {}
        if receipt.get('performed') is not True or receipt.get('endpoint') != endpoint or after.get('endpoint') != endpoint or (after.get('page') not in POST_COMPLETION) or (receipt.get('source_frame_sha256') != record.get('source_frame_sha256')):
            continue
        image = Path(after.get('screenshot_path', '')).resolve()
        if image.is_relative_to(root) and hashlib.sha256(image.read_bytes()).hexdigest() == after.get('frame_sha256'):
            return str(path)
    raise RuntimeError('restart_training_completion_unverified')

def completion_submission_receipt(prior, root, endpoint):
    """Prove the one original dispatch, not that its server result completed."""
    root = Path(root).resolve()
    records = [r for r in prior.get('records', []) if r.get('action_id') == 'complete_training']
    if len(records) != 1:
        raise RuntimeError('restart_training_submission_unverified')
    record = records[0]
    path = Path(record.get('receipt_path', '')).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise RuntimeError('restart_training_submission_unverified')
    receipt = json.loads(path.read_text(encoding='utf8'))
    outcome = record.get('action_outcome') or {}
    after = receipt.get('observation') or {}
    if record.get('performed') is not True or record.get('endpoint') != endpoint or record.get('execution_status') != 'executed' or (outcome.get('action_id') != 'complete_training') or (outcome.get('execution_status') != 'executed') or (type(outcome.get('input_attempts')) is not int) or (outcome.get('input_attempts') != 1) or (type(outcome.get('acknowledged_inputs')) is not int) or (outcome.get('acknowledged_inputs') != 1) or (outcome.get('source_page') != 'finish_confirm') or (outcome.get('source_frame_sha256') != record.get('source_frame_sha256')) or (receipt.get('action_id') != 'complete_training') or (receipt.get('performed') is not True) or (receipt.get('endpoint') != endpoint) or (after.get('endpoint') != endpoint) or (receipt.get('source_frame_sha256') != record.get('source_frame_sha256')) or (after.get('page') not in POST_COMPLETION | {'unknown', 'loading'}):
        raise RuntimeError('restart_training_submission_unverified')
    image = Path(after.get('screenshot_path', '')).resolve()
    if not image.is_relative_to(root) or not image.is_file() or hashlib.sha256(image.read_bytes()).hexdigest() != after.get('frame_sha256'):
        raise RuntimeError('restart_training_submission_unverified')
    return str(path)

def resume_context(prior, source):
    """Keep the old context snapshot; resume the newer owned child journal."""
    source = Path(source).resolve()
    owner = Path(prior.get('harvest_output_dir') or source).resolve()
    if owner != source and (len(source.parents) < 4 or not owner.is_relative_to(source.parents[3])):
        raise RuntimeError('harvest_resume_context_unverified')
    path = Path(prior.get('harvest_continuation_context') or source / 'context.json').resolve()
    if not path.is_relative_to(owner) or path.name != 'context.json' or (not path.is_file()):
        raise RuntimeError('harvest_resume_context_unverified')
    return json.loads(path.read_text(encoding='utf8'))

def _completed_at_home(prior, root, endpoint):
    for record in prior.get('records', []):
        if record.get('action_id') not in {'close_completed_training', 'formation_home'} or record.get('performed') is not True:
            continue
        path = Path(record.get('receipt_path', '')).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        receipt = json.loads(path.read_text(encoding='utf8'))
        after = receipt.get('observation') or {}
        if receipt.get('action_id') != record['action_id'] or receipt.get('performed') is not True or receipt.get('endpoint') != endpoint or (after.get('endpoint') != endpoint) or (after.get('page') != 'home') or (receipt.get('source_frame_sha256') != record.get('source_frame_sha256')):
            continue
        image = Path(after.get('screenshot_path', '')).resolve()
        if image.is_relative_to(root) and image.is_file() and (hashlib.sha256(image.read_bytes()).hexdigest() == after.get('frame_sha256')):
            return str(path)
    return None

def _resume_result_pages(device, prior, emit, stop, options, proof, skill_database):
    from core.autonomous_training.harvest import HarvestRunner, RESULT_PAGES, HARVEST_ACTION_PAGES, FORMATION_ACTIONS
    from core.location_navigation import wait_for_transition
    from core.action_recovery import ReobserveRequired
    allowed_pages = POST_COMPLETION | RESULT_PAGES | {'factor_reroll_confirm', 'character_story_unlock'}
    if options['update_team']:
        allowed_pages |= {HARVEST_ACTION_PAGES[a] for a in FORMATION_ACTIONS}
    if (device.context.get('factor_selection') or {}).get('pending'):
        raise RuntimeError('restart_harvest_factor_consumption_pending')
    if device.context.get('completion_submitted') is not True:
        raise RuntimeError('restart_training_submission_unverified')
    current = device.capture()
    if stop.is_set():
        raise RuntimeError('operator_stop')
    if current.get('endpoint') != device.endpoint:
        raise RuntimeError('restart_harvest_home_unverified')
    records = []
    if current.get('page') == 'home':
        emit(dict(type='progress', message='重启收菜：重新进入育成，检查原评级／因子及收尾页；不重新结束育成'))
        try:
            receipt = device.step('open_training', stop)
            if receipt.get('action_id') != 'open_training' or receipt.get('endpoint') != device.endpoint or receipt.get('performed') is not True:
                raise RuntimeError('restart_training_entry_receipt_unverified')
            records.append({k: v for k, v in receipt.items() if k != 'observation'})
            current = receipt.get('observation') or {}
        except ReobserveRequired as error:
            current = error.observation
        if current.get('page') in {'home', 'unknown', 'loading'}:
            current = wait_for_transition(device, stop, current, lambda o: o.get('page') in allowed_pages, waiting_pages={'home', 'unknown', 'loading'} | allowed_pages, endpoint=device.endpoint)
    if stop.is_set() or current.get('endpoint') != device.endpoint or current.get('page') not in allowed_pages:
        raise RuntimeError('restart_training_result_page_unverified')
    proof_path = Path(device.output_dir) / 'restart-training-entry.json'
    proof_path.write_text(json.dumps(dict(schema_version='umaa.harvest_restart_entry.v1', endpoint=device.endpoint, source_submission_receipt=proof, entry_records=records, observed_result=current), ensure_ascii=False, indent=2), encoding='utf8')
    original_capture, original_step = (device.capture, device.step)
    final_home = False
    closing_home = False
    allowed_actions = {a for a, p in HARVEST_ACTION_PAGES.items() if p in allowed_pages}

    def guarded_capture(*args, **kwargs):
        observed = original_capture(*args, **kwargs)
        if observed.get('endpoint') != device.endpoint or observed.get('page') not in allowed_pages | {'unknown', 'loading'} | ({'home'} if final_home or closing_home else set()):
            raise RuntimeError('restart_training_result_page_changed')
        return observed

    def guarded_step(action, *args, **kwargs):
        nonlocal final_home, closing_home
        if action not in allowed_actions:
            raise RuntimeError('restart_training_repeated_submission_forbidden')
        closing_home = action in {'close_completed_training', 'formation_home'}
        try:
            result = original_step(action, *args, **kwargs)
        finally:
            closing_home = False
        if action in {'close_completed_training', 'formation_home'} and result.get('performed') is True and (result.get('endpoint') == device.endpoint) and ((result.get('observation') or {}).get('page') in {'home', 'character_story_unlock', 'activity_story_unlock'}):
            final_home = True
        return result
    device.capture, device.step = (guarded_capture, guarded_step)
    try:
        result = HarvestRunner(device, emit, stop, external_skill_db_path=skill_database, preset=options.get('preset'), refine_skills=options.get('refine_skills', False), skill_recheck_min_pt=options.get('skill_recheck_min_pt', 60)).run(update_team=options['update_team'], execute=True, learn_skills=options.get('learn_skills', False))
    finally:
        device.capture, device.step = (original_capture, original_step)
        context_path = Path(device.output_dir) / 'context.json'
        context_path.write_text(json.dumps(device.context, ensure_ascii=False, indent=2), encoding='utf8')
    result['records'] = [*prior.get('records', []), *records, *result.get('records', [])]
    result['device_actions'] = prior.get('device_actions', 0) + len(records) + result.get('device_actions', 0)
    for key in ('skill_result', 'skill_batches', 'finish_result'):
        if key in prior:
            result.setdefault(key, prior[key])
    result.update(harvest_output_dir=str(Path(device.output_dir).parent.resolve()), harvest_continuation_context=str(context_path), restart_training_entry_proof=str(proof_path), restart_completion_submission_receipt=proof)
    return result

def resume_completed_training(device, prior, emit, stop, options, *, operation_verified=False, skill_database=None):
    root = Path(device.output_dir).parent.resolve()
    if stop.is_set():
        raise RuntimeError('operator_stop')
    if prior.get('account_conflict') or prior.get('account_paused'):
        raise RuntimeError('restart_account_held')
    if operation_verified is True:
        try:
            proof = completion_receipt(prior, root, device.endpoint)
        except RuntimeError:
            proof = completion_submission_receipt(prior, root, device.endpoint)
    else:
        proof = completion_receipt(prior, root, device.endpoint)
    current = device.capture()
    if current.get('endpoint') != device.endpoint:
        raise RuntimeError('restart_harvest_home_unverified')
    context = device.context
    if (context.get('factor_selection') or {}).get('pending'):
        raise RuntimeError('restart_harvest_factor_consumption_pending')
    home_proof = _completed_at_home(prior, root, device.endpoint)
    if not home_proof or current.get('page') != 'home':
        if operation_verified is not True:
            raise RuntimeError('restart_training_entry_operation_unverified')
        return _resume_result_pages(device, prior, emit, stop, options, proof, skill_database)
    if not options['update_team']:
        verified = device.capture()
        if stop.is_set() or verified.get('page') != 'home' or verified.get('endpoint') != device.endpoint:
            raise RuntimeError('restart_harvest_home_unverified')
        return dict(prior, ok=True, pending=False, reason='harvest_home_verified', last_observation=verified, team_update_skipped=True, restart_completion_receipt=proof)
    if operation_verified is not True:
        raise RuntimeError('restart_training_entry_operation_unverified')
    from core.jjc.live import JjcDevice
    arena = JjcDevice(device.endpoint, device.adb, Path(device.output_dir) / 'formation-navigation')
    arena.connect()
    from core.location_navigation import navigate_location
    try:
        navigate_location(arena, stop, dict(home='open_races', races='open_arena', weekly_settlement='continue_weekly'), lambda o: o.get('page') == 'arena')
    finally:
        arena.close()
    from core.autonomous_training.harvest import HarvestRunner
    for key in list(context):
        if key.startswith('formation_') or key.startswith('team_'):
            context.pop(key)
    result = HarvestRunner(device, emit, stop).run(update_team=True, execute=True, formation_only=True, learn_skills=False)
    if result.get('ok') is True and result.get('reason') == 'formation_home_verified':
        result.update(reason='harvest_and_formation_home_verified', team_update_skipped=False, restart_completion_receipt=proof)
    return result
