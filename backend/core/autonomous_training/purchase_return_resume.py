"""Continue an already-confirmed harvest purchase with close/return only."""
from desktop.umaa_public_context import endpoint as _public_endpoint
import hashlib
import json
from pathlib import Path

def verified_purchase_return(source, options, endpoint):
    source = Path(source).resolve()
    read = lambda p: json.loads(Path(p).read_text(encoding='utf8'))
    prior = read(source / 'adapter_result.json')
    context = read(source / 'context.json')
    batches = list(source.glob('harvest-skills-*/batch-0/confirmation/final_confirmation_report.json'))
    if endpoint != _public_endpoint() or prior.get('ok') is not False or prior.get('current_account_only') is not True or (prior.get('reason') != 'return_after_learning_not_verified') or (context.get('mode') != 'harvest') or (not context.get('harvest_skill_attempted')) or any((context.get(k) for k in ('finish_submitted', 'completion_submitted'))) or (context.get('harvest_preset') != options.get('preset')) or (context.get('harvest_learn_skills') != options['learn_skills']) or (context.get('harvest_update_team') != options['update_team']) or (len(batches) != 1):
        raise RuntimeError('purchase_return_owner_unverified')
    batch = batches[0].parent.parent
    child = batch / 'selection/issued/promotion_plan.final_child.json'
    final = batches[0]
    close = batch / 'success_close/success_close_report.json'
    returned = read(batch / 'return_after_learning/return_after_learning_report.json')
    child_value = read(child)
    from core.autonomous_training.success_close import _validate_final_report, _child_purchase_ids
    from core.autonomous_training.return_after_learning import _validate_success_close_report
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    final_value = read(final)
    close_value = read(close)
    if _child_purchase_ids(child_value) is None or _validate_final_report(final_value, child, child_value) or _validate_success_close_report(close_value, child_value, sha(child), sha(final)) or (returned.get('return_performed') is not False) or (returned.get('actions') != []) or (returned.get('device_action_count') != 0) or (returned.get('stop_reason') != 'direct_success_page_mismatch') or (returned.get('child_plan_sha256') != sha(child)) or (returned.get('final_confirmation_report_sha256') != sha(final)) or (returned.get('success_close_report_sha256') != sha(close)) or (not isinstance(context.get('harvest_summary'), dict)):
        raise RuntimeError('purchase_return_proofs_unverified')
    remaining = child_value.get('expected_remaining_skill_points')
    if type(remaining) is not int or remaining < 0:
        raise RuntimeError('purchase_return_points_unknown')
    return dict(source=source, batch=batch, child=child, final=final, close=close, remaining=remaining, summary=context['harvest_summary'])

def restore_latest_purchase_return(output, device, stop, options):
    output = Path(output).resolve()
    root = output.parent.parent
    paths = sorted((p for p in root.glob('*/harvest/adapter_result.json') if p.parent != output), key=lambda p: p.parent.parent.name, reverse=True)
    if not paths:
        return None
    latest = json.loads(paths[0].read_text(encoding='utf8'))
    if latest.get('reason') == 'low_pt_finish_not_verified':
        source = paths[0].parent
        context = json.loads((source / 'context.json').read_text(encoding='utf8'))
        original = Path(context.get('completed_purchase_return_from', '')).resolve()
        if not original.is_relative_to(root) or original == source or context.get('harvest_preset') != options.get('preset') or context.get('finish_submitted') or context.get('completion_submitted'):
            raise RuntimeError('purchase_return_finish_owner_unverified')
        evidence = verified_purchase_return(original, options, device.endpoint)
        skill = latest.get('skill_result') or {}
        child = evidence['child']
        returned = Path(skill.get('return_after_learning_report_path', '')).resolve()
        from core.autonomous_training.finish import _valid_return
        if not returned.is_relative_to(source) or skill.get('ok') is not True or Path(skill.get('child_plan_path', '')).resolve() != child or (not _valid_return(json.loads(returned.read_text(encoding='utf8')), json.loads(child.read_text(encoding='utf8')), hashlib.sha256(child.read_bytes()).hexdigest(), evidence['remaining'])):
            raise RuntimeError('purchase_return_finish_proof_unverified')
        device.context.update(harvest_summary=evidence['summary'], completed_purchase_return_from=str(original))
        return {**skill, 'device_action_count': 0}
    if latest.get('reason') != 'return_after_learning_not_verified':
        return None
    evidence = verified_purchase_return(paths[0].parent, options, device.endpoint)
    if stop.is_set():
        raise RuntimeError('operator_stop')
    from core.autonomous_training.harvest_skills import close_and_return_harvest_selection
    result = close_and_return_harvest_selection(device, output / 'completed-purchase-return', evidence['batch'] / 'selection', evidence['child'], evidence['final'], dict(device_action_count=0, device_action_count_complete=True), evidence['remaining'], stop, device.endpoint, previous_close_report=evidence['close'])
    if result.get('ok') is not True:
        raise RuntimeError('completed_purchase_return_not_verified:' + str(result.get('reason')))
    device.context.update(harvest_summary=evidence['summary'], completed_purchase_return_from=str(evidence['source']))
    return result
