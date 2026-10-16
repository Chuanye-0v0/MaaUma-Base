"""Current-account setup continuation, only before a proven zero-cost start."""
import json
from pathlib import Path

def proven(source, endpoint):
    from core.autonomous_training.live import START_ACTIONS
    source = Path(source).resolve()
    if source.name != 'plant' or source.parent.parent.name != 'tasks' or (not source.parent.name.isdigit()):
        return False
    try:
        result = json.loads((source / 'adapter_result.json').read_text(encoding='utf8'))
        context = json.loads((source / 'context.json').read_text(encoding='utf8'))
        if result.get('ok') is not False or result.get('current_account_only') is not True or context.get('mode') != 'start' or context.get('start_submitted') or context.get('request_id') or context.get('start_cost') or context.get('pending') or (result.get('last_observation', {}).get('endpoint') != endpoint) or result.get('account_conflict') or result.get('account_paused') or (result.get('action_outcome', {}).get('execution_status') == 'uncertain') or (result.get('action_outcome', {}).get('decision') == 'pause_account'):
            return False
        allowed = START_ACTIONS - {'start_autonomous', 'confirm_autonomous_start', 'return_title_after_disconnect'}
        paths = []
        for row in result.get('records', []):
            path = Path(row.get('receipt_path', '')).resolve()
            if path.parent != source or path in paths:
                return False
            actual = json.loads(path.read_text(encoding='utf8'))
            if actual.get('action_id') not in allowed or actual.get('performed') is not True or actual.get('endpoint') != endpoint or (not actual.get('source_frame_sha256')) or any((actual.get(k) != row.get(k) for k in ('action_id', 'performed', 'endpoint', 'source_frame_sha256'))):
                return False
            paths.append(path)
        return bool(paths) and set(paths) == {path.resolve() for path in source.glob('*-receipt.json')}
    except (OSError, ValueError, KeyError, TypeError):
        return False

def resume(device, source, options, emit, stop):
    from core.autonomous_training.plant_plan import validate as plan
    from core.autonomous_training.plant_support import validate as support
    from core.autonomous_training.plant_catalog import trainee_name, INITIAL_VARIANTS
    from core.autonomous_training.runner import AutonomousRunner
    source = Path(source).resolve()
    if not proven(source, device.endpoint):
        raise RuntimeError('seed_unsubmitted_setup_not_proven')
    context = json.loads((source / 'context.json').read_text(encoding='utf8'))
    key = options['trainee']
    if context.get('plant_options') != plan(options) or context.get('support_options', {}) != support(options) or context.get('required_trainee') != trainee_name(key) or (context.get('required_variant') != INITIAL_VARIANTS.get(key, key)):
        raise RuntimeError('seed_setup_options_changed')
    original = json.loads((source / 'adapter_result.json').read_text(encoding='utf8'))
    device.context.update(context)
    emit(dict(type='progress', message='承接原开种准备与指定卡组／借卡；尚未提交开种，不重置已核实的准备状态'))
    result = AutonomousRunner(device, emit, stop).run('start')
    result['records'] = [*original.get('records', []), *result.get('records', [])]
    result['seed_resume_setup_from'] = str(source)
    return result
