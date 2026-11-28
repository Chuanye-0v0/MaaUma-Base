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

def resume_completed_training(device, prior, emit, stop, options):
    root = Path(device.output_dir).parent.resolve()
    proof = completion_receipt(prior, root, device.endpoint)
    current = device.capture()
    if current.get('page') != 'home' or current.get('endpoint') != device.endpoint:
        raise RuntimeError('restart_harvest_home_unverified')
    context = device.context
    if not options['update_team']:
        verified = device.capture()
        if stop.is_set() or verified.get('page') != 'home' or verified.get('endpoint') != device.endpoint:
            raise RuntimeError('restart_harvest_home_unverified')
        return dict(prior, ok=True, reason='harvest_home_verified', last_observation=verified, team_update_skipped=True, restart_completion_receipt=proof)
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
