"""Return from an interrupted read-only rescan after a verified low-PT purchase."""
from desktop.umaa_public_context import endpoint as _public_endpoint
import hashlib
import json
from pathlib import Path
from core.autonomous_training.finish import _valid_child, _valid_return
from core.autonomous_training.harvest import LOW_SKILL_PT_LIMIT

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def authorize_low_pt_return(observed, context):
    request = context.get('harvest_low_pt_return') or {}
    if request.get('submitted') is not False:
        raise RuntimeError('low_pt_return_not_pending')
    child_path = Path(request['child_plan_path'])
    returned_path = Path(request['return_after_learning_report_path'])
    child = json.loads(child_path.read_text(encoding='utf8'))
    returned = json.loads(returned_path.read_text(encoding='utf8'))
    remaining = _valid_child(child)
    if type(remaining) is not int or remaining < 0 or (remaining >= LOW_SKILL_PT_LIMIT and request.get('fast_finish') is not True) or (sha(child_path) != request['child_sha256']) or (sha(returned_path) != request['return_sha256']) or (not _valid_return(returned, child, request['child_sha256'], remaining)) or (observed.get('endpoint') != _public_endpoint()) or (observed.get('page') != 'skills') or (observed.get('current_skill_points') != remaining) or ('open_skill_confirmation' in observed.get('actions', {})) or ('return_harvest_skills' not in observed.get('actions', {})):
        raise RuntimeError('low_pt_return_unverified')

def resume_low_pt_purchase(previous_path, device, stop, *, fast_finish=False):
    previous_path = Path(previous_path).resolve()
    previous = json.loads(previous_path.read_text(encoding='utf8'))
    batches = previous.get('skill_batches', [])
    reasons = {'skill_prepare_not_ready', 'affordable_skill_rows_unresolved'} if fast_finish else {'skill_prepare_not_ready'}
    if previous.get('reason') not in reasons or len(batches) != 2 or batches[0].get('ok') is not True or (batches[1].get('reason') != previous.get('reason')) or (batches[1].get('child_plan_path') is not None) or previous.get('finish_result'):
        raise RuntimeError('low_pt_resume_shape_invalid')
    purchase = batches[0]
    child_path = Path(purchase['child_plan_path']).resolve()
    returned_path = Path(purchase['return_after_learning_report_path']).resolve()
    if not all((p.is_relative_to(previous_path.parent) for p in (child_path, returned_path))):
        raise RuntimeError('low_pt_resume_source_outside_run')
    batch_root = returned_path.parent.parent.parent
    scans = [p for p in batch_root.iterdir() if p.is_dir() and p.name != 'batch-0']
    if not scans or (returned_path.parent / '.finish_entry_intents').exists():
        raise RuntimeError('low_pt_rescan_not_readonly')
    for scan in scans:
        prepare = json.loads((scan / 'prepare/skill_prepare.report.json').read_text(encoding='utf8'))
        if prepare.get('reason') not in {'skill_scroll_overlap_not_verified', 'skill_plan_ready', 'skill_planner_failed'} or prepare.get('purchase_executed') is not False or prepare.get('tap_count') != 0 or (scan / 'selection').exists():
            raise RuntimeError('low_pt_rescan_not_readonly')
    reopens = [r for r in previous.get('records', []) if r.get('action_id') == 'open_skills']
    if not reopens:
        raise RuntimeError('low_pt_reopen_missing')
    last = reopens[-1]
    receipt_path = Path(last['receipt_path']).resolve()
    if not receipt_path.is_relative_to(previous_path.parent):
        raise RuntimeError('low_pt_reopen_outside_run')
    receipt = json.loads(receipt_path.read_text(encoding='utf8'))
    if receipt.get('performed') is not True or receipt.get('source_frame_sha256') != last['source_frame_sha256']:
        raise RuntimeError('low_pt_reopen_unverified')
    sources = [json.loads(p.read_text(encoding='utf8')) for p in receipt_path.parent.glob('*.json')]
    matches = [s for s in sources if s.get('frame_sha256') == last['source_frame_sha256'] and s.get('page') == 'finish_ready' and (s.get('current_skill_points') == purchase['remaining_skill_points'])]
    if len(matches) != 1 or sha(matches[0]['screenshot_path']) != last['source_frame_sha256']:
        raise RuntimeError('low_pt_identity_source_unverified')
    device.context.update(mode='harvest', harvest_active=True, harvest_summary=matches[0]['finish_summary'], harvest_low_pt_return={**purchase, 'child_sha256': sha(child_path), 'return_sha256': sha(returned_path), 'submitted': False, 'fast_finish': fast_finish})
    observed = device.capture()
    authorize_low_pt_return(observed, device.context)
    receipt = device.step('return_harvest_skills', stop)
    if receipt.get('performed') is not True:
        raise RuntimeError('low_pt_return_uncertain')
    observed = receipt.get('observation') or {}
    if observed.get('page') != 'finish_ready' or observed.get('finish_summary') != device.context['harvest_summary'] or observed.get('current_skill_points') != purchase['remaining_skill_points']:
        raise RuntimeError('low_pt_resume_identity_changed')
    return (purchase, observed, receipt)
