"""Bounded harvest coordination; device actions remain fresh-frame gated."""
from desktop.umaa_public_context import endpoint as _public_endpoint
import hashlib
import json
from pathlib import Path
import subprocess
import time
from core.action_recovery import ReobserveRequired, ReobserveBudget, failure_details
LOW_SKILL_PT_LIMIT = 60

def verify_score_record_resume(previous_path, device, stop):
    previous_path = Path(previous_path).resolve()
    prior = json.loads(previous_path.read_text(encoding='utf8'))
    records = prior.get('records', [])
    factor_transition = prior.get('reason') == 'unexpected_transition:confirm_factor_choice->factor_result'
    follow_transition = prior.get('reason') == 'unexpected_transition:next_training_rewards->unknown'
    activity_transition = prior.get('reason') == 'unexpected_transition:next_activity_points->unknown'
    expected = ['complete_training', 'confirm_training_rating', 'confirm_factors', 'confirm_factor_choice']
    if not factor_transition:
        expected.append('close_trained_detail')
    if follow_transition:
        expected = ['forward_harvest', 'history_next', 'history_scroll', 'confirm_history', 'open_skills'] + ['continue_skill_tutorial'] * 4 + expected + ['confirm_title_notice', 'next_training_summary', 'next_affinity_result', 'next_training_rewards']
    actions = [r.get('action_id') for r in records]
    if activity_transition:
        expected = ['open_training', 'forward_harvest', 'history_next'] + ['history_scroll'] * actions.count('history_scroll') + ['confirm_history'] + expected + ['confirm_title_notice', 'next_training_summary', 'next_affinity_result', 'next_training_rewards', 'next_activity_points']
    if prior.get('reason') not in {'unexpected_transition:close_trained_detail->unknown', 'action_not_allowed:advance_settlement', 'unexpected_transition:confirm_factor_choice->factor_result', 'unexpected_transition:next_training_rewards->unknown', 'unexpected_transition:next_activity_points->unknown'} or (actions != expected and (not (factor_transition and actions == ['forward_harvest', 'history_next', 'history_scroll', 'confirm_history', 'open_skills'] + expected))):
        raise RuntimeError('score_resume_chain_invalid')
    for record in records:
        path = Path(record['receipt_path']).resolve()
        if not path.is_relative_to(previous_path.parent):
            raise RuntimeError('score_resume_receipt_outside_run')
        receipt = json.loads(path.read_text(encoding='utf8'))
        if receipt.get('performed') is not True or device.endpoint != _public_endpoint() or receipt.get('endpoint') != device.endpoint or any((receipt.get(k) != record.get(k) for k in ('action_id', 'source_frame_sha256'))):
            raise RuntimeError('score_resume_receipt_invalid')
        sources = [json.loads(p.read_text(encoding='utf8')) for p in path.parent.glob('*.json')]
        sources = [s for s in sources if s.get('frame_sha256') == record['source_frame_sha256']]
        if len(sources) != 1:
            raise RuntimeError('score_resume_source_invalid')
        source = Path(sources[0]['screenshot_path']).resolve()
        if not source.is_relative_to(previous_path.parent) or hashlib.sha256(source.read_bytes()).hexdigest() != sources[0]['frame_sha256']:
            raise RuntimeError('score_resume_source_invalid')
    if stop.is_set():
        raise RuntimeError('operator_stop')
    device.context.update(completion_submitted=True, factors_requested=True, factor_choice_submitted=True)
    observed = device.capture()
    valid = observed.get('page') in {'activity_points_result', 'activity_points_claim'} and set(observed.get('actions', {})) == {RESULT_ACTIONS[observed['page']]} if activity_transition else observed.get('page') == 'follow_trainer' and set(observed.get('actions', {})) == {'cancel_follow_trainer'} if follow_transition else observed.get('page') == 'trained_detail' and set(observed.get('actions', {})) == {'close_trained_detail'} if factor_transition else observed.get('page') in {'score_record', 'settlement_navigation'} and set(observed.get('actions', {})) in ({'continue_score_record'}, {'advance_settlement'})
    if not valid:
        raise RuntimeError('score_resume_fresh_page_invalid')

def verify_finish_confirm_resume(previous_path, device, stop):
    previous_path = Path(previous_path).resolve()
    prior = json.loads(previous_path.read_text(encoding='utf8'))
    finish = prior.get('finish_result', {})
    if prior.get('reason') != 'no_safe_action:finish_confirm' or finish.get('stop_reason') != 'finish_entry_probe_complete' or finish.get('finish_entry_submitted') is not True or (finish.get('accepted_remaining_skill_points') != 0):
        raise RuntimeError('finish_resume_evidence_invalid')
    intent_path = Path(finish['intent_path'])
    if hashlib.sha256(intent_path.read_bytes()).hexdigest() != finish.get('intent_sha256'):
        raise RuntimeError('finish_resume_intent_changed')
    intent = json.loads(intent_path.read_text(encoding='utf8'))
    if any((intent.get(k) != finish.get(k) for k in ('plan_id', 'session_id', 'accepted_remaining_skill_points', 'child_plan_sha256', 'return_after_learning_report_sha256'))):
        raise RuntimeError('finish_resume_intent_mismatch')
    batches = prior.get('skill_batches', [])
    if len(batches) != 1 or batches[0].get('ok') is not True or batches[0].get('remaining_skill_points') != 0:
        raise RuntimeError('finish_resume_skills_invalid')
    for key, digest in [('child_plan_path', 'child_plan_sha256'), ('return_after_learning_report_path', 'return_after_learning_report_sha256')]:
        path = Path(batches[0][key]).resolve()
        if not path.is_relative_to(previous_path.parent) or hashlib.sha256(path.read_bytes()).hexdigest() != finish[digest]:
            raise RuntimeError('finish_resume_chain_changed')
    directory = Path(prior['last_observation']['screenshot_path']).resolve().parent
    if not directory.is_relative_to(previous_path.parent):
        raise RuntimeError('finish_resume_source_outside_run')
    receipts = [json.loads(p.read_text(encoding='utf8')) for p in sorted(directory.glob('*-receipt.json'))]
    entries = [r for r in receipts if r.get('action_id') == 'finish_training']
    if len(entries) != 1 or receipts[-1] != entries[0] or entries[0].get('performed') is not True or (entries[0].get('endpoint') != device.endpoint) or (device.endpoint != _public_endpoint()) or (entries[0].get('finish_intent_path') != str(intent_path)):
        raise RuntimeError('finish_resume_receipt_invalid')
    sources = [json.loads(p.read_text(encoding='utf8')) for p in directory.glob('*.json')]
    sources = [s for s in sources if s.get('frame_sha256') == entries[0].get('source_frame_sha256')]
    if len(sources) != 1 or sources[0].get('page') != 'finish_ready' or sources[0].get('current_skill_points') != 0:
        raise RuntimeError('finish_resume_source_invalid')
    source = Path(sources[0]['screenshot_path']).resolve()
    if not source.is_relative_to(directory) or hashlib.sha256(source.read_bytes()).hexdigest() != sources[0]['frame_sha256']:
        raise RuntimeError('finish_resume_source_invalid')
    if stop.is_set():
        raise RuntimeError('operator_stop')
    observed = device.capture()
    if observed.get('page') != 'finish_confirm' or observed.get('current_skill_points') != 0 or set(observed.get('actions', {})) != {'complete_training'}:
        raise RuntimeError('finish_resume_fresh_page_invalid')
    device.context.update(finish_submitted=True, finish_entry_verified=True, finish_submission={**intent, 'intent_path': str(intent_path), 'intent_sha256': finish['intent_sha256']}, harvest_summary=sources[0]['finish_summary'])

def verify_history_resume(previous_path, device):
    previous_path = Path(previous_path).resolve()
    prior = json.loads(previous_path.read_text(encoding='utf8'))
    records = prior.get('records', [])
    if prior.get('reason') != 'no_safe_action:history_summary' or prior.get('device_actions') != 1 or len(records) != 1 or (records[0].get('action_id') != 'forward_harvest') or prior.get('skill_batches'):
        raise RuntimeError('history_resume_evidence_invalid')
    record = records[0]
    receipt_path = Path(record['receipt_path']).resolve()
    if not receipt_path.is_relative_to(previous_path.parent):
        raise RuntimeError('history_resume_receipt_outside_run')
    receipt = json.loads(receipt_path.read_text(encoding='utf8'))
    if receipt.get('performed') is not True or receipt.get('endpoint') != device.endpoint or device.endpoint != _public_endpoint() or any((receipt.get(k) != record.get(k) for k in ('action_id', 'source_frame_sha256'))) or (receipt.get('observation', {}).get('frame_sha256') != prior.get('last_observation', {}).get('frame_sha256')):
        raise RuntimeError('history_resume_receipt_mismatch')
    sources = [json.loads(p.read_text(encoding='utf8')) for p in receipt_path.parent.glob('*.json')]
    sources = [s for s in sources if s.get('frame_sha256') == record.get('source_frame_sha256')]
    if len(sources) != 1 or sources[0].get('page') != 'training_status' or sources[0].get('remaining_seconds') != 0:
        raise RuntimeError('history_resume_source_invalid')
    source_path = Path(sources[0]['screenshot_path']).resolve()
    if not source_path.is_relative_to(previous_path.parent) or hashlib.sha256(source_path.read_bytes()).hexdigest() != sources[0]['frame_sha256']:
        raise RuntimeError('history_resume_source_invalid')
    observed = device.capture()
    if observed.get('page') != 'history_summary' or set(observed.get('actions', {})) != {'history_next'}:
        raise RuntimeError('history_resume_fresh_page_invalid')

def verify_rank_notice_resume(previous_path, device, stop):
    previous_path = Path(previous_path).resolve()
    prior = json.loads(previous_path.read_text(encoding='utf8'))
    records = prior.get('records', [])
    closing = []
    visited = {previous_path}
    while len(records) < 5:
        if prior.get('reason') not in {'no_safe_action:team_rank_update', 'no_safe_action:team_rank_reward', 'no_safe_action:team_formation'}:
            raise RuntimeError('rank_resume_chain_invalid')
        if any((r.get('action_id') not in {'continue_team_rank_reward', 'close_team_rank_update'} for r in records)):
            raise RuntimeError('rank_resume_chain_actions_invalid')
        closing = records + closing
        result = json.loads((previous_path.parent / 'result.json').read_text(encoding='utf8'))
        upstream = Path(result.get('resumed_from', '')).resolve() / 'harvest.json'
        if upstream in visited or upstream.parent.parent != previous_path.parent.parent:
            raise RuntimeError('rank_resume_chain_source_invalid')
        visited.add(upstream)
        previous_path = upstream
        prior = json.loads(previous_path.read_text(encoding='utf8'))
        records = prior.get('records', [])
    while records and records[-1].get('action_id') in {'continue_team_rank_reward', 'close_team_rank_update'}:
        closing.insert(0, records[-1])
        records = records[:-1]
    actions = ['open_completed_formation', 'auto_team_formation', 'confirm_auto_team_formation', 'save_team_formation', 'confirm_save_team_formation']
    tail = records[-5:]
    if (prior.get('reason'), prior.get('last_observation', {}).get('page')) not in {('no_safe_action:team_rank_update', 'team_rank_update'), ('no_safe_action:unknown', 'team_rank_update'), ('no_safe_action:team_formation', 'team_formation'), ('action_not_allowed:formation_home', 'team_formation')} or [r.get('action_id') for r in tail] != actions:
        raise RuntimeError('rank_resume_invalid')
    for record in tail + closing:
        path = Path(record['receipt_path']).resolve()
        if not path.is_relative_to(previous_path.parent.parent) or record.get('performed') is not True or record.get('endpoint') != device.endpoint:
            raise RuntimeError('rank_resume_receipt_invalid')
        receipt = json.loads(path.read_text(encoding='utf8'))
        if any((receipt.get(k) != record.get(k) for k in ('action_id', 'performed', 'endpoint', 'source_frame_sha256'))):
            raise RuntimeError('rank_resume_receipt_mismatch')
        sources = [json.loads(p.read_text(encoding='utf8')) for p in path.parent.glob('*.json')]
        sources = [s for s in sources if s.get('frame_sha256') == record['source_frame_sha256']]
        if len(sources) != 1:
            raise RuntimeError('rank_resume_source_missing')
        source = sources[0]
        png = Path(source['screenshot_path']).resolve()
        if not png.is_relative_to(previous_path.parent.parent) or hashlib.sha256(png.read_bytes()).hexdigest() != source['frame_sha256']:
            raise RuntimeError('rank_resume_source_invalid')
        mark_harvest_dispatch(record['action_id'], source, device.context, path)
    if stop.is_set():
        raise RuntimeError('operator_stop')
    if device.capture()['page'] not in {'team_rank_update', 'team_rank_reward', 'team_formation'}:
        raise RuntimeError('rank_resume_page_changed')

def verify_tied_style_resume(previous_path, device, stop):
    previous_path = Path(previous_path).resolve()
    prior = json.loads(previous_path.read_text(encoding='utf8'))
    old = prior.get('last_observation', {})
    records = prior.get('records', [])
    entry_transition = prior.get('reason') == 'unexpected_transition:open_skills->finish_ready'
    allowed = {'forward_harvest', 'history_next', 'history_scroll', 'confirm_history', 'open_skills', 'continue_skill_tutorial'}
    if prior.get('reason') not in {'run_style_tied', 'run_style_unknown', 'unexpected_transition:open_skills->finish_ready'} or prior.get('ok') is not False or prior.get('skill_result') or prior.get('skill_batches') or (old.get('page') != ('finish_ready' if entry_transition else 'skills')) or (not records) or list((previous_path.parent / 'training_status').glob('harvest-skills-*')):
        raise RuntimeError('harvest_tied_resume_invalid')
    summary = None
    for record in records:
        path = Path(record['receipt_path']).resolve()
        if not path.is_relative_to(previous_path.parent) or record.get('action_id') not in allowed or record.get('performed') is not True or (record.get('endpoint') != device.endpoint):
            raise RuntimeError('harvest_tied_receipt_invalid')
        receipt = json.loads(path.read_text(encoding='utf8'))
        if any((receipt.get(k) != record.get(k) for k in ('action_id', 'performed', 'endpoint', 'source_frame_sha256'))):
            raise RuntimeError('harvest_tied_receipt_mismatch')
        if record['action_id'] == 'open_skills':
            sources = [json.loads(p.read_text(encoding='utf8')) for p in path.parent.glob('*.json')]
            sources = [s for s in sources if s.get('frame_sha256') == record['source_frame_sha256']]
            if len(sources) != 1 and (not (entry_transition and sources and all((s.get('finish_summary') == sources[0].get('finish_summary') and s.get('current_skill_points') == sources[0].get('current_skill_points') and (s.get('page') == 'finish_ready') for s in sources)))):
                raise RuntimeError('harvest_tied_source_missing')
            source = sources[0]
            png = Path(source['screenshot_path']).resolve()
            if not png.is_relative_to(previous_path.parent) or source.get('page') != 'finish_ready' or hashlib.sha256(png.read_bytes()).hexdigest() != source['frame_sha256'] or (source.get('current_skill_points') != old.get('current_skill_points')):
                raise RuntimeError('harvest_tied_source_invalid')
            summary = source.get('finish_summary')
            if prior['reason'] == 'run_style_unknown':
                from core.autonomous_training.observer import observe, read_tokens
                from core.autonomous_training.harvest_skills import build_harvest_context
                from rapidocr_onnxruntime import RapidOCR
                import cv2
                corrected = observe(png.read_bytes(), read_tokens(cv2.imread(str(png)), RapidOCR()))
                updated = corrected.get('finish_summary') or {}
                if corrected.get('page') != 'finish_ready' or corrected.get('current_skill_points') != old.get('current_skill_points') or updated.get('name') != (summary or {}).get('name') or (updated.get('long_distance') != (summary or {}).get('long_distance')) or any((value is not None and updated.get('run_styles', {}).get(key) != value for key, value in (summary or {}).get('run_styles', {}).items())) or build_harvest_context(updated, 'resume-check').get('reason'):
                    raise RuntimeError('harvest_style_reparse_mismatch')
                summary = updated
    if stop.is_set():
        raise RuntimeError('operator_stop')
    fresh = device.capture()
    if not summary or fresh.get('page') not in ({'finish_ready', 'skills'} if entry_transition else {'skills'}) or fresh.get('endpoint') != device.endpoint or (fresh.get('current_skill_points') != old.get('current_skill_points')) or (entry_transition and fresh.get('page') == 'finish_ready' and (fresh.get('finish_summary') != summary)) or ('open_skill_confirmation' in fresh.get('actions', {})):
        raise RuntimeError('harvest_tied_fresh_mismatch')
    device.context.update(harvest_summary=summary, tied_resume_from=str(previous_path))

def verify_remaining_skill_resume(previous_path, device, stop):
    """Resume a read-only remainder scan, retaining the successful purchase proof."""
    previous_path = Path(previous_path).resolve()
    prior = json.loads(previous_path.read_text(encoding='utf8'))
    batches = prior.get('skill_batches', [])
    if len(batches) == 1:
        return verify_initial_skill_scan_resume(previous_path, device, stop)
    if prior.get('reason') != 'affordable_skill_rows_unresolved' or len(batches) != 2 or batches[0].get('ok') is not True or (batches[1].get('child_plan_path') is not None) or (batches[1].get('reason') != prior['reason']):
        raise RuntimeError('remaining_skill_resume_invalid')
    points = batches[0].get('remaining_skill_points')
    if type(points) is not int or points != batches[1].get('remaining_skill_points'):
        raise RuntimeError('remaining_skill_points_mismatch')
    child = Path(batches[0]['child_plan_path']).resolve()
    returned = Path(batches[0]['return_after_learning_report_path']).resolve()
    if not child.is_relative_to(previous_path.parent) or not returned.is_relative_to(previous_path.parent):
        raise RuntimeError('remaining_skill_source_outside_run')
    proof = json.loads(returned.read_text(encoding='utf8'))
    if proof.get('return_performed') is not True or proof.get('device_endpoint') != device.endpoint or proof.get('verified_current_skill_points') != points or (proof.get('child_plan_sha256') != hashlib.sha256(child.read_bytes()).hexdigest()):
        raise RuntimeError('remaining_skill_purchase_proof_invalid')
    roots = list((previous_path.parent / 'training_status').glob('harvest-skills-*/batch-1'))
    if len(roots) != 1 or (roots[0] / 'selection').exists():
        raise RuntimeError('remaining_skill_scan_not_readonly')
    record = prior['records'][-1]
    receipt_path = Path(record['receipt_path']).resolve()
    if record.get('action_id') != 'open_skills' or record.get('performed') is not True or record.get('endpoint') != device.endpoint or (not receipt_path.is_relative_to(previous_path.parent)):
        raise RuntimeError('remaining_skill_entry_invalid')
    receipt = json.loads(receipt_path.read_text(encoding='utf8'))
    if any((receipt.get(k) != record.get(k) for k in ('action_id', 'performed', 'endpoint', 'source_frame_sha256'))):
        raise RuntimeError('remaining_skill_receipt_mismatch')
    sources = [json.loads(p.read_text(encoding='utf8')) for p in receipt_path.parent.glob('*.json')]
    sources = [s for s in sources if s.get('frame_sha256') == record['source_frame_sha256']]
    if len(sources) != 1 or sources[0].get('page') != 'finish_ready' or sources[0].get('current_skill_points') != points:
        raise RuntimeError('remaining_skill_source_invalid')
    source = sources[0]
    png = Path(source['screenshot_path']).resolve()
    if not png.is_relative_to(previous_path.parent) or hashlib.sha256(png.read_bytes()).hexdigest() != source['frame_sha256']:
        raise RuntimeError('remaining_skill_source_changed')
    if stop.is_set():
        raise RuntimeError('operator_stop')
    fresh = device.capture()
    if fresh.get('page') != 'skills' or fresh.get('current_skill_points') != points or fresh.get('endpoint') != device.endpoint or ('open_skill_confirmation' in fresh.get('actions', {})):
        raise RuntimeError('remaining_skill_fresh_mismatch')
    device.context.update(harvest_summary=source['finish_summary'], remaining_skill_resume_from=str(previous_path))

def verify_initial_skill_scan_resume(previous_path, device, stop):
    """Resume an initial optimizer failure only when every scan was read-only."""
    previous_path = Path(previous_path).resolve()
    prior = json.loads(previous_path.read_text(encoding='utf8'))
    root = previous_path.parent / 'training_status'
    batches = prior.get('skill_batches', [])
    scans = batches + prior.get('skill_scan_retries', [])
    if prior.get('ok') is not False or prior.get('reason') != 'affordable_skill_rows_unresolved' or len(batches) != 1 or any((s.get('child_plan_path') is not None or s.get('reason') != prior['reason'] for s in scans)) or list(root.rglob('selection')):
        raise RuntimeError('initial_skill_resume_invalid')
    prepares = list(root.glob('harvest-skills-*/*/prepare/skill_prepare.report.json'))
    if len(prepares) != len(scans):
        raise RuntimeError('initial_skill_scan_count_mismatch')
    allowed = {'open_training', 'forward_harvest', 'history_next', 'history_scroll', 'confirm_history', 'open_skills', 'scroll_skill_overlap', 'scroll_skill_up', 'scroll_skill_down', 'scroll_skill_reveal_bottom'}
    receipts = [json.loads(p.read_text(encoding='utf8')) for p in root.rglob('*-receipt.json')]
    if not receipts or any((r.get('action_id') not in allowed or r.get('performed') is not True or r.get('endpoint') != device.endpoint for r in receipts)):
        raise RuntimeError('initial_skill_actions_not_readonly')
    for path in prepares:
        prepared = json.loads(path.read_text(encoding='utf8'))
        plan = json.loads((path.parent / 'skill_batch.plan.json').read_text(encoding='utf8'))
        proposal = json.loads((path.parent / 'skill_proposal.json').read_text(encoding='utf8'))
        if prepared.get('purchase_executed') is not False or prepared.get('tap_count') != 0 or plan.get('selected_action_ids') != [] or (plan.get('items') != []) or (proposal.get('optimizer_status') != 'optimizer_state_limit_exceeded'):
            raise RuntimeError('initial_skill_scan_not_readonly')
    record = prior['records'][-1]
    path = Path(record['receipt_path']).resolve()
    if record.get('action_id') != 'open_skills' or record.get('performed') is not True or record.get('endpoint') != device.endpoint or (path.parent != root):
        raise RuntimeError('initial_skill_entry_invalid')
    receipt = json.loads(path.read_text(encoding='utf8'))
    if any((receipt.get(k) != record.get(k) for k in ('action_id', 'performed', 'endpoint', 'source_frame_sha256'))):
        raise RuntimeError('initial_skill_receipt_mismatch')
    sources = [json.loads(p.read_text(encoding='utf8')) for p in root.glob('*.json')]
    sources = [s for s in sources if s.get('frame_sha256') == record['source_frame_sha256']]
    if len(sources) != 1 or sources[0].get('page') != 'finish_ready':
        raise RuntimeError('initial_skill_source_invalid')
    source = sources[0]
    png = Path(source['screenshot_path']).resolve()
    points = source.get('current_skill_points')
    if type(points) is not int or any((s.get('remaining_skill_points') != points for s in scans)) or png.parent != root or (hashlib.sha256(png.read_bytes()).hexdigest() != source['frame_sha256']):
        raise RuntimeError('initial_skill_source_mismatch')
    if stop.is_set():
        raise RuntimeError('operator_stop')
    fresh = device.capture()
    if fresh.get('page') != 'skills' or fresh.get('endpoint') != device.endpoint or fresh.get('current_skill_points') != points or ('open_skill_confirmation' in fresh.get('actions', {})):
        raise RuntimeError('initial_skill_fresh_mismatch')
    device.context.update(harvest_summary=source['finish_summary'], remaining_skill_resume_from=str(previous_path))

def verify_unlearned_finish_resume(previous_path, device, stop):
    """Reparse a pre-skill identity failure and bind it to fresh finish state."""
    from core.autonomous_training.observer import observe, read_tokens
    import cv2
    from rapidocr_onnxruntime import RapidOCR
    previous_path = Path(previous_path).resolve()
    prior = json.loads(previous_path.read_text(encoding='utf8'))
    records = prior.get('records', [])
    old = prior.get('last_observation', {})
    if prior.get('ok') is not False or prior.get('reason') != 'harvest_trainee_changed' or prior.get('skill_result') or (old.get('page') != 'finish_ready') or (not records) or (records[-1].get('action_id') != 'confirm_history') or any((r.get('action_id') not in {'forward_harvest', 'history_next', 'history_scroll', 'confirm_history'} or r.get('performed') is not True or r.get('endpoint') != device.endpoint for r in records)):
        raise RuntimeError('harvest_unlearned_resume_invalid')
    for record in records:
        path = Path(record['receipt_path']).resolve()
        if not path.is_relative_to(previous_path.parent):
            raise RuntimeError('harvest_unlearned_receipt_outside_run')
        receipt = json.loads(path.read_text(encoding='utf8'))
        if any((receipt.get(k) != record.get(k) for k in ('action_id', 'performed', 'endpoint', 'source_frame_sha256'))):
            raise RuntimeError('harvest_unlearned_receipt_mismatch')
    image_path = Path(old['screenshot_path']).resolve()
    if not image_path.is_relative_to(previous_path.parent) or list(image_path.parent.glob('harvest-skills-*')):
        raise RuntimeError('harvest_unlearned_source_invalid')
    raw = image_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != old.get('frame_sha256'):
        raise RuntimeError('harvest_unlearned_source_hash_mismatch')
    corrected = observe(raw, read_tokens(cv2.imread(str(image_path)), RapidOCR()))
    if stop.is_set():
        raise RuntimeError('operator_stop')
    fresh = device.capture()
    if corrected.get('finish_summary', {}).get('name') != '大和赤骥' or corrected.get('current_skill_points') != old.get('current_skill_points') or fresh.get('page') != 'finish_ready' or (fresh.get('finish_summary') != corrected.get('finish_summary')) or (fresh.get('current_skill_points') != corrected.get('current_skill_points')):
        raise RuntimeError('harvest_unlearned_fresh_state_mismatch')
    device.context['harvest_summary'] = fresh['finish_summary']
    device.context['unlearned_resume_from'] = str(previous_path)

def verify_unpurchased_prepare_resume(previous_path, device, stop):
    """Recover a failed read-only parser before a selection plan was issued."""
    previous_path = Path(previous_path).resolve()
    prior = json.loads(previous_path.read_text(encoding='utf8'))
    root = previous_path.parent / 'training_status'
    prepares = list(root.glob('harvest-skills-*/batch-0/prepare/skill_prepare.report.json'))
    skill = prior.get('skill_result', {})
    old = prior.get('last_observation', {})
    promotion_failed = prior.get('reason') == 'selection_confirmation_not_opened'
    selections = list(root.glob('harvest-skills-*/batch-*/selection'))
    if promotion_failed:
        if len(selections) != 1 or sorted((p.name for p in selections[0].iterdir())) != ['purchase_preflight.json']:
            raise RuntimeError('harvest_promotion_resume_not_empty')
        preflight = json.loads((selections[0] / 'purchase_preflight.json').read_text(encoding='utf8'))
        plan = json.loads((selections[0].parent / 'prepare/skill_batch.plan.json').read_text(encoding='utf8'))
        if preflight.get('stop_reason') != 'promotion_blocked:parser_row_fingerprint_duplicate' or preflight.get('confirmation_opened') is not False or preflight.get('device_action_count') != 0 or (plan.get('initial_skill_points') != old.get('current_skill_points')) or (prior.get('skill_batches') != [skill]):
            raise RuntimeError('harvest_promotion_resume_invalid')
    if prior.get('ok') is not False or prior.get('reason') not in {'skill_prepare_not_ready', 'selection_confirmation_not_opened'} or skill.get('child_plan_path') is not None or (len(prepares) != 1) or (old.get('page') != 'skills') or (type(old.get('current_skill_points')) is not int) or (selections and (not promotion_failed)):
        raise RuntimeError('harvest_prepare_resume_invalid')
    prepared = json.loads(prepares[0].read_text(encoding='utf8'))
    failed_readonly = prepared.get('reason') == 'skill_plan_ready' if promotion_failed else prepared.get('reason') == 'skill_planner_failed' and prepared.get('detail') == 'OSError'
    if not failed_readonly or prepared.get('purchase_executed') is not False or prepared.get('tap_count') != 0:
        raise RuntimeError('harvest_prepare_failure_not_readonly')
    receipts = [json.loads(p.read_text(encoding='utf8')) for p in root.glob('*-receipt.json')]
    if not receipts or any((r.get('action_id') not in {'open_training', 'forward_harvest', 'history_next', 'history_scroll', 'confirm_history', 'continue_skill_tutorial', 'open_skills', 'scroll_skill_overlap', 'scroll_skill_up', 'scroll_skill_down'} or r.get('performed') is not True or r.get('endpoint') != device.endpoint for r in receipts)):
        raise RuntimeError('harvest_prepare_receipts_invalid')
    sources = [json.loads(p.read_text(encoding='utf8')) for p in root.glob('*.json')]
    finishes = [s for s in sources if s.get('page') == 'finish_ready' and (s.get('finish_summary') or {}).get('name') == '大和赤骥' and (s.get('current_skill_points') == old['current_skill_points'])]
    if not finishes:
        active_path = previous_path.parent.parent / 'active.json'
        if active_path.is_file():
            active = json.loads(active_path.read_text(encoding='utf8'))
            original = Path(active.get('previous_output', '')).resolve()
            if active.get('output') == str(previous_path.parent) and active.get('endpoint') == device.endpoint and (original.parent == previous_path.parent.parent) and (original != previous_path.parent) and (prior.get('records') == []):
                origin = json.loads((original / 'harvest.json').read_text(encoding='utf8'))
                if origin.get('last_observation', {}).get('current_skill_points') == old['current_skill_points']:
                    verify_initial_skill_scan_resume(original / 'harvest.json', device, stop)
                    return
        raise RuntimeError('harvest_prepare_identity_missing')
    original = finishes[-1]
    png = Path(original['screenshot_path']).resolve()
    if png.parent != root or hashlib.sha256(png.read_bytes()).hexdigest() != original['frame_sha256']:
        raise RuntimeError('harvest_prepare_identity_hash_mismatch')
    if stop.is_set():
        raise RuntimeError('operator_stop')
    fresh = device.capture()
    if fresh.get('page') != 'skills' or fresh.get('endpoint') != device.endpoint or fresh.get('current_skill_points') != old['current_skill_points'] or ('open_skill_confirmation' in fresh.get('actions', {})):
        raise RuntimeError('harvest_prepare_fresh_state_mismatch')
    device.context.update(harvest_summary=original['finish_summary'], prepare_resume_from=str(previous_path))

def verify_completed_harvest_home(previous_path, device, stop):
    """Finish a failed final home check, without replaying any training action."""
    previous_path = Path(previous_path).resolve()
    prior = json.loads(previous_path.read_text(encoding='utf8'))
    records = prior.get('records', [])
    if prior.get('reason') != 'stable_home_not_verified' or prior.get('ok') is not False or prior.get('formation_only') is not False or (not records) or (records[-1].get('action_id') != 'formation_home') or (not any((r.get('action_id') == 'confirm_save_team_formation' for r in records))) or any((r.get('performed') is not True or r.get('endpoint') != device.endpoint for r in records)):
        raise RuntimeError('harvest_home_resume_invalid')
    receipt_path = Path(records[-1]['receipt_path']).resolve()
    if not receipt_path.is_relative_to(previous_path.parent):
        raise RuntimeError('harvest_home_receipt_outside_run')
    receipt = json.loads(receipt_path.read_text(encoding='utf8'))
    if any((receipt.get(k) != records[-1].get(k) for k in ('action_id', 'performed', 'endpoint', 'source_frame_sha256'))) or receipt.get('observation', {}).get('page') != 'home':
        raise RuntimeError('harvest_home_receipt_mismatch')
    sources = [json.loads(p.read_text(encoding='utf8')) for p in receipt_path.parent.glob('*.json')]
    source = [s for s in sources if s.get('frame_sha256') == receipt['source_frame_sha256']]
    if len(source) != 1 or source[0].get('page') != 'team_formation':
        raise RuntimeError('harvest_home_formation_source_invalid')
    png = Path(source[0]['screenshot_path']).resolve()
    if png.parent != receipt_path.parent or hashlib.sha256(png.read_bytes()).hexdigest() != source[0]['frame_sha256']:
        raise RuntimeError('harvest_home_source_hash_mismatch')
    consecutive = 0
    closed = []
    for _ in range(10):
        if stop.is_set():
            raise RuntimeError('operator_stop')
        observed = device.capture()
        if observed['page'] == 'character_story_unlock' and len(closed) < 2:
            device.context.update(mode='harvest', harvest_active=True, formation_verified=True, formation_home_submitted=True, resumed_formation_home_receipt=str(receipt_path))
            closed.append(device.step('close_character_story_unlock', stop))
            consecutive = 0
            continue
        consecutive = consecutive + 1 if observed['page'] == 'home' else 0
        if consecutive == 2:
            return {**prior, 'ok': True, 'reason': 'harvest_and_formation_home_verified', 'team_score': source[0].get('team_score_after'), 'last_observation': observed, 'resumed_from': str(previous_path), 'resume_device_actions': len(closed), 'resume_receipts': closed}
        if observed['page'] not in {'home', 'unknown', 'loading'}:
            break
        stop.wait(0.5)
    raise RuntimeError('stable_home_not_verified')
HARVEST_ACTION_PAGES = {'factor_reroll': 'factor_result', 'confirm_factor_reroll': 'factor_reroll_confirm', 'cancel_factor_reroll': 'factor_reroll_confirm', 'select_original_factors': 'factor_result', 'select_new_factors': 'factor_result', 'close_activity_story_unlock': 'activity_story_unlock', 'advance_activity_reward': 'activity_reward_animation', 'next_activity_points': 'activity_points_result', 'close_activity_points_claim': 'activity_points_claim', 'advance_settlement': 'settlement_navigation', 'history_next': 'history_summary', 'history_scroll': 'history_races', 'continue_skill_tutorial': 'skill_tutorial', 'complete_training': 'finish_confirm', 'confirm_training_rating': 'training_rating', 'confirm_factors': 'factor_result', 'confirm_factor_choice': 'factor_confirm', 'close_trained_detail': 'trained_detail', 'continue_score_record': 'score_record', 'continue_scenario_record': 'scenario_record', 'confirm_title_notice': 'title_notice', 'next_training_summary': 'training_summary', 'next_affinity_result': 'affinity_result', 'next_training_rewards': 'training_rewards', 'cancel_follow_trainer': 'follow_trainer', 'open_completed_formation': 'training_complete', 'close_completed_training': 'training_complete', 'open_team_formation': 'team_arena', 'auto_team_formation': 'team_formation', 'confirm_auto_team_formation': 'team_formation_confirm', 'confirm_save_team_formation': 'team_formation_save_confirm', 'continue_team_rank_reward': 'team_rank_reward', 'save_team_formation': 'team_formation', 'close_team_rank_update': 'team_rank_update', 'close_team_unlock': 'team_unlock', 'formation_home': 'team_formation', 'close_character_story_unlock': 'character_story_unlock'}
RESULT_ACTIONS = {page: action for action, page in HARVEST_ACTION_PAGES.items() if action != 'close_completed_training' and page in {'trained_detail', 'score_record', 'scenario_record', 'title_notice', 'settlement_navigation', 'training_summary', 'affinity_result', 'training_rewards', 'activity_story_unlock', 'activity_reward_animation', 'activity_points_result', 'activity_points_claim', 'follow_trainer', 'training_complete'}}
RESULT_PAGES = set(RESULT_ACTIONS)
FORMATION_ACTIONS = {action for action, page in HARVEST_ACTION_PAGES.items() if page.startswith('team_')}
REPEATABLE_RESULT_ACTIONS = set(RESULT_ACTIONS.values()) | {'close_team_rank_update', 'close_team_unlock'}
NEXT_PAGES = {'factor_reroll': {'factor_reroll_confirm', 'factor_result'}, 'confirm_factor_reroll': {'factor_result'}, 'cancel_factor_reroll': {'factor_result'}, 'select_original_factors': {'factor_result'}, 'select_new_factors': {'factor_result'}, 'open_training': {'training_status', 'new_scenario', 'scenario'}, 'close_notice': {'new_scenario', 'training_status', 'scenario'}, 'forward_harvest': {'history_summary', 'history_races', 'finish_ready', 'training_rating', 'factor_result'}, 'history_next': {'history_races'}, 'history_scroll': {'history_races'}, 'confirm_history': {'finish_ready'}, 'open_skills': {'skills', 'skill_tutorial'}, 'continue_skill_tutorial': {'skill_tutorial', 'skills'}, 'complete_training': {'training_rating', 'factor_result'}, 'confirm_training_rating': {'factor_result'}, 'confirm_factors': {'factor_confirm'}, 'confirm_factor_choice': RESULT_PAGES, **{action: RESULT_PAGES for action in RESULT_ACTIONS.values()}, 'open_completed_formation': {'team_formation'}, 'open_team_formation': {'team_formation'}, 'auto_team_formation': {'team_formation_confirm', 'team_formation'}, 'confirm_auto_team_formation': {'team_formation'}, 'save_team_formation': {'team_formation_save_confirm', 'team_formation', 'team_rank_reward', 'team_rank_update', 'team_unlock'}, 'confirm_save_team_formation': {'team_formation', 'team_rank_reward', 'team_rank_update', 'team_unlock'}, 'continue_team_rank_reward': {'team_formation', 'team_rank_update', 'team_unlock'}, 'close_team_rank_update': {'team_formation', 'team_unlock'}, 'close_team_unlock': {'team_formation', 'team_unlock'}, 'formation_home': {'home', 'character_story_unlock', 'activity_story_unlock'}, 'close_character_story_unlock': {'home', 'activity_story_unlock'}, 'close_completed_training': {'home', 'character_story_unlock', 'activity_story_unlock'}, 'close_activity_story_unlock': RESULT_PAGES | {'home', 'character_story_unlock'}}

def formation_is_verified(observed, context):
    if not (observed.get('page') == 'team_formation' and context.get('formation_requested') is True and (context.get('formation_auto_confirmed') is True) and (observed.get('formation_save_enabled') is False)):
        return False
    if context.get('formation_save_submitted') is not True:
        return bool(context.get('formation_auto_receipt_id'))
    return context.get('formation_save_submitted') is True and bool(context.get('formation_save_receipt_id'))

def choose_harvest_action(observed, context):
    from core.autonomous_training.harvest_direct_settlement import completion_is_bound, ensure_settlement_scope
    ensure_settlement_scope(observed, context)
    page, actions = (observed['page'], observed['actions'])
    if page == 'home':
        if context.get('harvest_update_team') is False and context.get('harvest_home_submitted'):
            return None
        if context.get('formation_verified') and context.get('formation_home_submitted'):
            return None
        if context.get('formation_only') or completion_is_bound(context, observed.get('endpoint')):
            raise RuntimeError('harvest_home_before_formation_verified')
        action = 'open_training'
    elif page == 'new_scenario':
        action = 'close_notice'
    elif page == 'scenario':
        raise RuntimeError('no_completed_training_at_entry')
    elif page == 'training_status':
        if completion_is_bound(context, observed.get('endpoint')):
            raise RuntimeError('harvest_completed_training_changed')
        seconds = observed.get('remaining_seconds')
        if type(seconds) is not int or seconds < 0:
            raise RuntimeError('training_time_unknown')
        if seconds:
            raise RuntimeError('still_running')
        action = 'forward_harvest'
    elif page == 'history_summary':
        action = 'history_next'
    elif page == 'history':
        from core.autonomous_training.runner import choose_action
        return choose_action('harvest', observed, context)
    elif page == 'history_races':
        if observed.get('history_rewards_visible') is True:
            action = 'confirm_history'
        elif 'history_scroll' in actions:
            action = 'history_scroll'
        else:
            raise RuntimeError('history_rewards_not_verified')
    elif page == 'finish_ready':
        if context.get('harvest_learn_skills') is False:
            return 'finish_without_skills'
        action = 'open_skills'
    elif page == 'skills':
        if context.get('harvest_learn_skills') is False:
            raise RuntimeError('skip_skills_requires_finish_ready')
        return 'learn_harvest_skills'
    elif page == 'training_complete' and context.get('harvest_update_team') is False:
        action = 'close_completed_training'
    elif page.startswith('team_'):
        from core.jjc.formation import choose_formation_action
        action = choose_formation_action(observed, context)
    else:
        if page in {'factor_result', 'factor_reroll_confirm'}:
            from core.harvest_factors import next_factor_action
            choice = next_factor_action(observed, context)
            if choice is not None:
                if choice not in actions:
                    raise RuntimeError('factor_control_unavailable')
                return choice
        action = RESULT_ACTIONS.get(page) or {'skill_tutorial': 'continue_skill_tutorial', 'finish_confirm': 'complete_training', 'training_rating': 'confirm_training_rating', 'factor_result': 'confirm_factors', 'factor_confirm': 'confirm_factor_choice', 'character_story_unlock': 'close_character_story_unlock'}.get(page)
    if not action or action not in actions:
        raise RuntimeError('no_safe_action:' + page)
    return action

def authorize_harvest_action(action, observed, context):
    from core.autonomous_training.harvest_direct_settlement import completion_is_bound, ensure_settlement_scope
    ensure_settlement_scope(observed, context, action)
    completed = completion_is_bound(context, observed.get('endpoint'))
    if context.get('mode') != 'harvest' or context.get('harvest_active') is not True:
        raise RuntimeError('harvest_chain_not_requested')
    if action == 'advance_settlement' and (not (completed and context.get('factor_choice_submitted') is True and (not context.get('formation_requested')) and (set(observed.get('actions', {})) == {'advance_settlement'}))):
        raise RuntimeError('settlement_navigation_not_bound')
    if action == 'confirm_history':
        if observed.get('page') != 'history_races' or observed.get('history_rewards_visible') is not True:
            raise RuntimeError('history_rewards_not_verified')
        return
    if observed.get('page') != HARVEST_ACTION_PAGES.get(action):
        raise RuntimeError('harvest_action_forbidden')
    from core.harvest_factors import FACTOR_ACTIONS, authorize_factor_action
    if action in FACTOR_ACTIONS:
        authorize_factor_action(action, observed, context)
        return
    if action == 'close_character_story_unlock' and (not (context.get('formation_verified') is True and context.get('formation_home_submitted') is True or (context.get('harvest_update_team') is False and context.get('harvest_home_submitted') is True))):
        raise RuntimeError('formation_home_not_bound')
    if action == 'close_completed_training' and (not (context.get('harvest_update_team') is False and completed and (context.get('factor_choice_submitted') is True) and (not context.get('harvest_home_submitted')))):
        raise RuntimeError('harvest_completion_not_bound')
    if action == 'complete_training':
        submission = context.get('finish_submission') or {}
        remaining = submission.get('accepted_remaining_skill_points')
        if not (context.get('finish_submitted') is True and context.get('finish_entry_verified') is True and (not completed) and submission.get('intent_path') and (type(remaining) is int) and (remaining >= 0) and (type(observed.get('current_skill_points')) is int) and (observed['current_skill_points'] == remaining)):
            raise RuntimeError('finish_confirmation_not_bound')
    if action in ('confirm_training_rating', 'confirm_factors', 'confirm_factor_choice'):
        if action == 'confirm_factor_choice' and context.get('factor_handoff_dispatch'):
            from desktop.mfa_harvest_factor_handoff import verify_dispatch
            verify_dispatch(observed, context)
        if not completed:
            raise RuntimeError('training_completion_not_bound')
        if action == 'confirm_factors' and context.get('factors_requested'):
            raise RuntimeError('factors_already_requested')
        if action == 'confirm_factor_choice' and (not context.get('factors_requested') or context.get('factor_choice_submitted')):
            raise RuntimeError('factor_confirmation_not_bound')
    if action in RESULT_ACTIONS.values() and context.get('factor_choice_submitted') is not True:
        raise RuntimeError('factor_result_not_bound')
    if action in FORMATION_ACTIONS:
        from core.jjc.formation import choose_formation_action
        if action == 'formation_home' and (not formation_is_verified(observed, context)):
            raise RuntimeError('formation_save_not_verified')
        if choose_formation_action(observed, context) != action:
            raise RuntimeError('formation_action_not_bound')

def mark_harvest_dispatch(action, observed, context, receipt_path):
    """Consume irreversible intents before transport, including uncertain sends."""
    from core.harvest_factors import FACTOR_ACTIONS, mark_factor_dispatch
    if action in FACTOR_ACTIONS:
        mark_factor_dispatch(action, observed, context)
    flags = {'complete_training': 'completion_submitted', 'confirm_factors': 'factors_requested', 'confirm_factor_choice': 'factor_choice_submitted', 'open_completed_formation': 'formation_requested', 'open_team_formation': 'formation_requested', 'formation_home': 'formation_home_submitted', 'close_completed_training': 'harvest_home_submitted'}
    if action in flags:
        context[flags[action]] = True
    if action == 'auto_team_formation':
        context.update(formation_auto_requested=True, formation_auto_score_before=observed.get('team_score_before'), formation_auto_receipt_id=str(receipt_path))
    if action == 'confirm_auto_team_formation':
        context.update(formation_auto_confirmed=True, formation_auto_score_after=observed.get('team_score_after'))
    if action == 'save_team_formation':
        context.update(formation_save_submitted=True, formation_save_receipt_id=str(receipt_path))
    if action == 'confirm_save_team_formation':
        context.update(formation_save_confirmed=True, formation_save_confirm_receipt_id=str(receipt_path))
    if action == 'continue_team_rank_reward':
        context['formation_rank_reward_acknowledged'] = True

class HarvestRunner:

    def __init__(self, device, emit, stop_event, skill_service=None, external_skill_db_path=None, resume_navigation_from=None, small_skill_batches=False, resume_finish_result=None, refine_skills=True, skill_recheck_min_pt=60, preset=None):
        self.device, self.emit, self.stop = (device, emit, stop_event)
        self.skill_service = skill_service
        self.external_skill_db_path = external_skill_db_path
        self.resume_navigation_from = resume_navigation_from
        self.small_skill_batches = small_skill_batches
        self.resume_finish_result = resume_finish_result
        self.refine_skills = refine_skills
        self.skill_recheck_min_pt = skill_recheck_min_pt
        self.preset = preset

    def _authorize_navigation_resume(self, observed):
        """Resume only a persisted first-batch stop that never dispatched a plus tap."""
        if self.resume_navigation_from is None:
            raise RuntimeError('harvest_skill_transaction_already_attempted')
        previous = Path(self.resume_navigation_from)
        read = lambda path: json.loads(path.read_text(encoding='utf-8'))
        report = read(previous / 'harvest_result.json')
        saved_context = read(previous / 'harvest_context.json')
        if report.get('reason') == 'harvest_large_batch_dialog_validation_pending':
            plans = list(previous.glob('**/harvest-skills-*/batch-0/prepare/skill_batch.plan.json'))
            plan = read(plans[0]) if len(plans) == 1 else {}
            skill = report.get('skill_result') or {}
            navigation = {'open_training', 'forward_harvest', 'history_next', 'history_scroll', 'confirm_history', 'open_skills', 'continue_skill_tutorial'}
            if self.small_skill_batches is not True or saved_context != self.device.context or report.get('skill_batches') != [skill] or (len(plans) != 1) or (skill.get('reason') != report['reason']) or (skill.get('child_plan_path') is not None) or (type(plan.get('initial_skill_points')) is not int) or (plan['initial_skill_points'] <= 0) or (not isinstance(plan.get('selected_action_ids'), list)) or (not len(plan['selected_action_ids']) == skill.get('planned_skill_count', 0) > 5) or (plans[0].parent.parent / 'selection').exists() or (observed.get('page') != 'skills') or (observed.get('endpoint') != _public_endpoint()) or (observed.get('current_skill_points') != plan['initial_skill_points']) or (skill.get('remaining_skill_points') != plan['initial_skill_points']) or ('open_skill_confirmation' in observed.get('actions', {})) or saved_context.get('completion_submitted') or (not report.get('records')) or any((r.get('action_id') not in navigation or r.get('performed') is not True or r.get('endpoint') != observed['endpoint'] for r in report['records'])):
                raise RuntimeError('harvest_prepared_resume_not_empty')
            intent = dict(previous_report=str(previous / 'harvest_result.json'), source_frame_sha256=observed['frame_sha256'], output_dir=str(self.device.output_dir))
            with (previous / 'navigation_resume.intent.json').open('x', encoding='utf-8') as handle:
                json.dump(intent, handle, ensure_ascii=False)
            self.resume_navigation_from = None
            return intent
        paths = list(previous.glob('harvest-skills-*/batch-0/selection/purchase_preflight.json'))
        if len(paths) != 1 or saved_context != self.device.context or report.get('reason') != 'selection_confirmation_not_opened' or (len(report.get('skill_batches') or []) != 1):
            raise RuntimeError('harvest_navigation_resume_evidence_invalid')
        preflight_path = paths[0]
        selection = preflight_path.parent
        preflight = read(preflight_path)
        plan = read(selection.parent / 'prepare/skill_batch.plan.json')
        if not str(preflight.get('stop_reason', '')).startswith('planned_row_identity_mismatch:') or preflight.get('confirmation_opened') is not False or preflight.get('pre_tap_receipts_path') is not None or (preflight.get('selection_ledger_path') is not None) or (selection / 'selection_checkpoint.json').exists() or (selection / 'selection_pre_tap_receipts.json').exists() or (preflight.get('plan_sha256') != plan.get('plan_sha256')) or (type(plan.get('initial_skill_points')) is not int) or (plan['initial_skill_points'] <= 0) or (observed.get('page') != 'skills') or (observed.get('endpoint') != _public_endpoint()) or (observed.get('current_skill_points') != plan.get('initial_skill_points')) or ('open_skill_confirmation' in observed.get('actions', {})) or saved_context.get('completion_submitted'):
            raise RuntimeError('harvest_navigation_resume_not_empty')
        intent = dict(previous_report=str(previous / 'harvest_result.json'), source_frame_sha256=observed['frame_sha256'], output_dir=str(self.device.output_dir))
        with (previous / 'navigation_resume.intent.json').open('x', encoding='utf-8') as handle:
            json.dump(intent, handle, ensure_ascii=False)
        self.resume_navigation_from = None
        return intent

    @staticmethod
    def _include_action_count(report, result):
        count = result.get('device_action_count')
        if type(count) is int and count >= 0:
            report['device_actions'] += count
        else:
            report['device_action_count_complete'] = False
        if result.get('device_action_count_complete') is False:
            report['device_action_count_complete'] = False

    def _learn_and_finish(self, observed, report):
        from core.skill_ocr_performance import harvest_skill_ocr
        started = time.monotonic()
        with harvest_skill_ocr() as performance:
            try:
                return self._learn_and_finish_impl(observed, report)
            finally:
                report['skill_learning_seconds'] = round(time.monotonic() - started, 3)
                report['skill_ocr_counts'] = {key: performance[key] for key in ('rapid_calls', 'cache_hits', 'tesseract_calls')}

    def _learn_and_finish_impl(self, observed, report):
        from core.autonomous_training.harvest_skills import build_harvest_context, learn_harvest_skills, return_after_no_purchase_skills
        from core.autonomous_training.finish import submit_finish_entry_once
        from core.skill_purchase_executor import SKILL_PURCHASE_SAFETY_TOKEN
        context = self.device.context
        if context.get('harvest_skill_attempted'):
            report['navigation_resume'] = self._authorize_navigation_resume(observed)
        build = build_harvest_context(context.get('harvest_summary') or {}, str(time.time_ns()))
        if build.get('reason'):
            raise RuntimeError(build['reason'])
        preset = context.get('harvest_preset') or self.preset or {}
        if preset.get('skill_list'):
            build['harvest_skill_list'] = preset['skill_list']
            report['skill_list'] = preset['skill_list']
        if self.external_skill_db_path is not None:
            build['external_skill_db_path'] = str(self.external_skill_db_path)
        previous_points = observed.get('current_skill_points')
        if self.small_skill_batches:
            if type(previous_points) is not int or previous_points < 0:
                raise RuntimeError('skill_points_unknown')
            build['policy']['max_purchase_count'] = 5
        context['harvest_skill_attempted'] = True
        root = Path(self.device.output_dir) / ('harvest-skills-' + str(time.time_ns()))
        service = self.skill_service or learn_harvest_skills
        report['skill_batches'] = []
        purchase_limit = previous_points if self.small_skill_batches else 2
        for batch_index in range(purchase_limit + 1):
            if self.stop.is_set():
                raise RuntimeError('operator_stop')
            purchase_allowed = batch_index < purchase_limit
            batch_label = str(batch_index + 1) if self.small_skill_batches else f'{batch_index + 1}/{(2 if self.refine_skills else 1)}'
            self.emit(dict(type='progress', phase='skills', message=('技能首批：整批规划并核对购买' if batch_index == 0 else '技能余量复查：仅有新增可购项时补学') if purchase_allowed else '技能余量核验：只扫描，不再购买'))
            batch_build = {**build, 'turn_id': build['turn_id'] + '-' + str(batch_index)}
            result = service(self.device, root / ('batch-' + str(batch_index)), batch_build, stop_event=self.stop, execute=True, purchase_allowed=purchase_allowed)
            for retry in range(2):
                if not isinstance(result, dict) or result.get('reason') != 'affordable_skill_rows_unresolved' or result.get('child_plan_path') is not None:
                    break
                fresh = self.device.capture()
                if fresh.get('page') != 'skills' or fresh.get('current_skill_points') != previous_points or 'open_skill_confirmation' in fresh.get('actions', {}):
                    break
                report.setdefault('skill_scan_retries', []).append(result)
                self._include_action_count(report, result)
                self.emit(dict(type='progress', phase='skills', message=f'技能识别自动重试 {retry + 1}/2；不重放购买'))
                result = service(self.device, root / f'r{batch_index}{retry + 1}', batch_build, stop_event=self.stop, execute=True, purchase_allowed=purchase_allowed)
            report['skill_result'] = result
            if not isinstance(result, dict):
                report['device_action_count_complete'] = False
                raise RuntimeError('skill_service_result_invalid')
            report['skill_batches'].append(result)
            self._include_action_count(report, result)
            if result.get('reason') == 'no_harvest_eligible_skills_return_required' and result.get('ok') is False:
                break
            if result.get('ok') is not True:
                raise RuntimeError(result.get('reason') or 'skill_learning_not_verified')
            if not purchase_allowed:
                raise RuntimeError('harvest_skill_purchase_limit_not_enforced')
            remaining = result.get('remaining_skill_points')
            if type(remaining) is not int or remaining < 0:
                raise RuntimeError('skill_remaining_points_not_verified')
            if self.small_skill_batches and remaining >= previous_points:
                raise RuntimeError('skill_points_did_not_decrease')
            previous_points = remaining
            self.emit(dict(type='progress', phase='skills', message=f'技能第 {batch_label} 批已核实购买，剩余 {remaining} PT'))
            for _ in range(6):
                if self.stop.is_set():
                    raise RuntimeError('operator_stop')
                observed = self.device.capture()
                if observed.get('page') not in ('unknown', 'loading') and (not (observed.get('page') == 'skills' and (not observed.get('actions')))):
                    break
                time.sleep(0.5)
            if observed.get('page') != 'finish_ready' or observed.get('current_skill_points') != remaining:
                raise RuntimeError('skill_return_page_not_verified')
            if not self.refine_skills or remaining < self.skill_recheck_min_pt or observed.get('skill_available_badge') is False:
                self.emit(dict(type='progress', phase='skills', message=f'剩余 {remaining} PT，已核实购买；不再复查技能，直接结束育成'))
                return self._finish_low_points(observed, result, root, report)
            receipt = self.device.step('open_skills', self.stop)
            report['records'].append({key: value for key, value in receipt.items() if key != 'observation'})
            if receipt.get('performed') is not True or receipt.get('action_id') != 'open_skills':
                report['device_action_count_complete'] = False
                raise RuntimeError('skill_reopen_not_verified')
            report['device_actions'] += 1
            observed = receipt.get('observation') or {}
            if observed.get('page') != 'skills':
                raise RuntimeError('skill_reopen_page_not_verified')
        else:
            raise RuntimeError('harvest_skill_batch_limit')
        if self.stop.is_set():
            raise RuntimeError('operator_stop')
        remaining = result.get('remaining_skill_points')
        if type(remaining) is not int or remaining < 0:
            raise RuntimeError('skill_remaining_points_not_verified')
        evidence = Path(result['no_purchase_evidence_path'])
        returned = return_after_no_purchase_skills(self.device, root / 'empty-return', evidence, result['no_purchase_evidence_sha256'], stop_event=self.stop, execute=True)
        report['no_purchase_return_result'] = returned
        if not isinstance(returned, dict):
            report['device_action_count_complete'] = False
            raise RuntimeError('no_purchase_return_result_invalid')
        self._include_action_count(report, returned)
        if returned.get('ok') is not True or returned.get('remaining_skill_points') != remaining:
            raise RuntimeError(returned.get('reason') or 'no_purchase_return_not_verified')
        observation_path = Path(returned['finish_ready_observation_path'])
        returned_path = Path(returned['no_purchase_return_report_path'])
        sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        finish = submit_finish_entry_once(device=self.device, no_purchase_evidence_path=evidence, expected_no_purchase_evidence_sha256=result['no_purchase_evidence_sha256'], no_purchase_return_report_path=returned_path, expected_no_purchase_return_report_sha256=sha(returned_path), finish_ready_observation_path=observation_path, expected_finish_ready_observation_sha256=sha(observation_path), completion_authorization={'recommended_skills_complete': True, 'discard_remaining_skill_points_accepted': True, 'accepted_remaining_skill_points': remaining}, output_dir=root / 'finish-entry', mode='live', execute=True, safety_token=SKILL_PURCHASE_SAFETY_TOKEN)
        report['finish_result'] = finish
        self._include_action_count(report, finish)
        if finish.get('stop_reason') != 'finish_entry_probe_complete':
            raise RuntimeError(finish.get('stop_reason') or 'finish_entry_not_verified')
        context['finish_entry_verified'] = True
        observed = self.device.capture()
        if observed.get('page') != 'finish_confirm':
            raise RuntimeError('finish_confirmation_page_not_verified')
        return observed

    def _finish_low_points(self, observed, result, root, report):
        from core.autonomous_training.finish import submit_finish_entry_once
        from core.skill_purchase_executor import SKILL_PURCHASE_SAFETY_TOKEN
        remaining = result.get('remaining_skill_points')
        if result.get('ok') is not True or type(remaining) is not int or remaining < 0 or (self.refine_skills and remaining >= self.skill_recheck_min_pt and (observed.get('skill_available_badge') is not False)) or (observed.get('page') != 'finish_ready') or (observed.get('current_skill_points') != remaining):
            raise RuntimeError('low_pt_finish_not_verified')
        if self.stop.is_set():
            raise RuntimeError('operator_stop')
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        observation_path = root / 'low-pt-finish-observation.json'
        observation_path.write_text(json.dumps(observed, ensure_ascii=False, indent=2), encoding='utf8')
        child = Path(result['child_plan_path'])
        returned = Path(result['return_after_learning_report_path'])
        sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        finish = submit_finish_entry_once(device=self.device, child_plan_path=child, expected_child_plan_sha256=sha(child), return_after_learning_report_path=returned, expected_return_after_learning_report_sha256=sha(returned), finish_ready_observation_path=observation_path, expected_finish_ready_observation_sha256=sha(observation_path), completion_authorization={'recommended_skills_complete': True, 'discard_remaining_skill_points_accepted': True, 'accepted_remaining_skill_points': remaining}, output_dir=root / 'finish-entry', mode='live', execute=True, safety_token=SKILL_PURCHASE_SAFETY_TOKEN)
        report.update(finish_result=finish, low_pt_scan_skipped=remaining < LOW_SKILL_PT_LIMIT, skill_learning_flow='refine' if self.refine_skills else 'single_batch')
        self._include_action_count(report, finish)
        if finish.get('stop_reason') != 'finish_entry_probe_complete':
            raise RuntimeError(finish.get('stop_reason') or 'finish_entry_not_verified')
        self.device.context['finish_entry_verified'] = True
        observed = self.device.capture()
        if observed.get('page') != 'finish_confirm':
            raise RuntimeError('finish_confirmation_page_not_verified')
        return observed

    def _finish_without_skills(self, observed, report):
        from core.autonomous_training.finish import submit_finish_entry_once
        from core.skill_purchase_executor import SKILL_PURCHASE_SAFETY_TOKEN
        root = Path(self.device.output_dir) / 'skip-skills-finish'
        root.mkdir(parents=True, exist_ok=True)
        observation_path = root / 'observation.json'
        with observation_path.open('x', encoding='utf8') as handle:
            json.dump(observed, handle, ensure_ascii=False, indent=2)
        if self.stop.is_set():
            raise RuntimeError('operator_stop')
        finish = submit_finish_entry_once(device=self.device, skip_skills=True, finish_ready_observation_path=observation_path, expected_finish_ready_observation_sha256=hashlib.sha256(observation_path.read_bytes()).hexdigest(), completion_authorization={'skip_skills': True, 'discard_remaining_skill_points_accepted': True, 'accepted_remaining_skill_points': observed.get('current_skill_points')}, output_dir=root, mode='live', execute=True, safety_token=SKILL_PURCHASE_SAFETY_TOKEN)
        report.update(finish_result=finish, skills_skipped=True)
        self._include_action_count(report, finish)
        if finish.get('stop_reason') != 'finish_entry_probe_complete':
            raise RuntimeError(finish.get('stop_reason') or 'finish_entry_not_verified')
        self.device.context['finish_entry_verified'] = True
        return self.device.capture()

    def run(self, update_team=True, *, execute=False, formation_only=False, learn_skills=True):
        report = dict(kind='autonomous_harvest', ok=False, dry_run=not execute, formation_only=formation_only, device_actions=0, records=[], carrot_reward={'status': 'unknown', 'amount': None})
        if not execute:
            return {**report, 'reason': 'dry_run_no_device_actions'}
        if type(update_team) is not bool or type(learn_skills) is not bool or (formation_only and (not update_team)):
            return {**report, 'reason': 'harvest_options_invalid'}
        context = self.device.context
        from core.factor_goals import current_targets
        goals = current_targets()
        if goals is not None:
            if context.get('factor_goal') not in (None, goals):
                return {**report, 'reason': 'harvest_resume_goal_changed'}
            context['factor_goal'] = goals
        context.update(mode='harvest', harvest_active=True, formation_only=formation_only, harvest_update_team=update_team, harvest_learn_skills=learn_skills)
        if self.preset is not None:
            from core.harvest_presets import validate_snapshot
            preset = validate_snapshot(self.preset)
            if context.get('harvest_preset') is not None and context['harvest_preset'] != preset:
                return {**report, 'reason': 'harvest_resume_preset_changed'}
            context['harvest_preset'] = preset
        counts = {}
        try:
            observed = self.device.capture()
            for _ in range(12):
                if observed['page'] not in {'unknown', 'loading'} or observed['actions']:
                    break
                if self.stop.wait(0.5):
                    raise RuntimeError('operator_stop')
                observed = self.device.capture()
            if self.resume_finish_result is not None:
                report['last_observation'] = observed
                report['skill_result'] = self.resume_finish_result
                self.emit(dict(type='progress', message='收菜：承接已学完技能的进度，直接结束育成'))
                if observed.get('page') in {'skills', 'unknown', 'loading'}:
                    from core.location_navigation import wait_for_transition
                    observed = wait_for_transition(self.device, self.stop, observed, lambda state: state.get('page') == 'finish_ready', waiting_pages={'skills', 'unknown', 'loading', 'finish_ready'}, endpoint=self.device.endpoint)
                observed = self._finish_low_points(observed, self.resume_finish_result, Path(self.device.output_dir) / 'resume-finish', report)
            if formation_only:
                if observed['page'] not in ('team_arena', 'team_formation'):
                    raise RuntimeError('formation_entry_page_required')
                context['formation_requested'] = True
            replans = ReobserveBudget()
            for _ in range(80):
                report['last_observation'] = observed
                if context.get('direct_settlement_entry'):
                    report['direct_settlement_entry'] = context['direct_settlement_entry']
                    if learn_skills and (not report.get('skill_result')):
                        report['skill_learning_skipped_reason'] = 'already_at_verified_settlement'
                        report['missed_skill_phase'] = True
                        if not getattr(self, '_direct_skill_warning_sent', False):
                            self.emit(dict(type='warning', message='原育成已直达评级，技能阶段已错过；只安全完成结算，不计本次技能学习成功，不额外开种补学。'))
                            self._direct_skill_warning_sent = True
                if self.stop.is_set():
                    raise RuntimeError('operator_stop')
                if observed.get('finish_summary'):
                    prior = context.get('harvest_summary')
                    summary = observed['finish_summary']
                    if not prior:
                        context['harvest_summary'] = summary
                if observed.get('history_rewards_visible') is True:
                    report['carrot_reward'] = observed.get('carrot_reward') or report['carrot_reward']
                if formation_is_verified(observed, context):
                    context.update(formation_verified=True, formation_saved_score=observed.get('team_score_after'))
                if (observed['page'] in {'team_rank_update', 'team_rank_reward', 'history_summary', 'training_rating'} or (observed['page'] in {'unknown', 'loading'} and context.get('formation_save_confirmed') is True)) and (not observed['actions']):
                    for _ in range(12):
                        if self.stop.wait(0.5):
                            raise RuntimeError('operator_stop')
                        observed = self.device.capture()
                        if observed['actions'] or (observed['page'] not in {'team_rank_update', 'team_rank_reward', 'history_summary', 'training_rating'} and (not (observed['page'] in {'unknown', 'loading'} and context.get('formation_save_confirmed') is True))):
                            break
                action = choose_harvest_action(observed, context)
                if context.get('factor_selection'):
                    report['factor_selection'] = context['factor_selection']
                    factor_state = context['factor_selection']
                    version = (factor_state.get('rolls'), factor_state.get('comparison_decision', {}).get('action'), factor_state.get('done'), factor_state.get('limit_reason'))
                    if version != getattr(self, '_factor_message_version', None):
                        self._factor_message_version = version
                        retained = factor_state.get('retained') or {}
                        message = f"因子方案评分：{retained['score']:g}；保留已识别的当前结果" if retained else '因子清单未读全：保留初次结果，不使用重抽资源'
                        comparison = factor_state.get('comparison_decision')
                        if comparison:
                            message = f"因子比较：原 {comparison['original_score']:g} / 新 {comparison['new_score']:g}；" + ('选择新因子' if comparison['action'] == 'select_new_factors' else '保留原因子')
                        self.emit(dict(type='progress', phase='factors', message=message))
                if action is None:
                    verified = self.device.capture()
                    if verified['page'] in {'character_story_unlock', 'activity_story_unlock'}:
                        observed = verified
                        continue
                    for _ in range(8):
                        if verified['page'] == 'home' or verified['page'] not in {'unknown', 'loading'}:
                            break
                        if self.stop.wait(0.5):
                            raise RuntimeError('operator_stop')
                        verified = self.device.capture()
                    if verified['page'] != 'home':
                        raise RuntimeError('stable_home_not_verified')
                    reason = 'formation_home_verified' if formation_only else 'harvest_and_formation_home_verified' if update_team else 'harvest_home_verified'
                    if report.get('missed_skill_phase') is True:
                        return {**report, 'ok': False, 'pending': False, 'game_task_complete': False, 'settlement_verified': True, 'requirement_state': 'settled_unmet', 'reason': 'harvest_settled_requirements_unmet', 'last_observation': verified, 'team_score': context.get('formation_saved_score'), 'team_update_skipped': not update_team}
                    return {**report, 'ok': True, 'pending': False, 'reason': reason, 'last_observation': verified, 'team_score': context.get('formation_saved_score'), 'team_update_skipped': not update_team}
                if action == 'finish_without_skills':
                    self.emit(dict(type='progress', phase='harvest', message='收菜：按设置跳过技能，结束育成并结算'))
                    observed = self._finish_without_skills(observed, report)
                    continue
                if action == 'learn_harvest_skills':
                    observed = self._learn_and_finish(observed, report)
                    continue
                counts[action] = counts.get(action, 0) + 1
                limit = 80 if action in REPEATABLE_RESULT_ACTIONS else 12 if action == 'history_scroll' else 6 if action == 'continue_skill_tutorial' else 3
                if counts[action] > limit:
                    raise RuntimeError('action_repeat_limit:' + action)
                if action in HARVEST_ACTION_PAGES or (action == 'confirm_history' and observed['page'] == 'history_races'):
                    authorize_harvest_action(action, observed, context)
                self.emit(dict(type='progress', phase=observed['page'], message='收菜: ' + action))
                try:
                    receipt = self.device.step(action, self.stop)
                except ReobserveRequired as error:
                    observed = replans.accept(error)
                    counts[action] -= 1
                    continue
                if receipt.get('execution_status') == 'executed':
                    replans.executed()
                report['records'].append({key: value for key, value in receipt.items() if key != 'observation'})
                if receipt.get('performed') is not True or receipt.get('action_id') != action:
                    raise RuntimeError('harvest_action_result_unknown')
                report['device_actions'] += 1
                from core.harvest_factors import FACTOR_ACTIONS, record_factor_receipt
                if action in FACTOR_ACTIONS:
                    record_factor_receipt(action, receipt, context)
                after = receipt.get('observation') or {}
                report['last_observation'] = after
                if action == 'forward_harvest' and after.get('page') in {'training_rating', 'factor_result'}:
                    report['pending'] = True
                    from core.autonomous_training.harvest_direct_settlement import bind_forward_settlement
                    bind_forward_settlement(self.device, receipt, self.stop)
                    report['direct_settlement_entry'] = context['direct_settlement_entry']
                if action == 'forward_harvest' and receipt.get('reason') == 'history_load_timeout':
                    raise RuntimeError('history_load_timeout')
                if action == 'confirm_factor_choice':
                    for _ in range(12):
                        if after.get('page') not in {'factor_result', 'unknown', 'loading'}:
                            break
                        if self.stop.wait(0.5):
                            raise RuntimeError('operator_stop')
                        after = self.device.capture()
                if after.get('page') not in NEXT_PAGES[action]:
                    raise RuntimeError('unexpected_transition:' + action + '->' + str(after.get('page')))
                self.emit(dict(type='progress', phase='harvest', action_id=action, receipt_path=receipt.get('receipt_path'), message='收菜: ' + action))
                observed = after
            raise RuntimeError('harvest_step_limit')
        except (RuntimeError, ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError) as error:
            report.update(failure_details(error))
            if context.get('direct_settlement_entry'):
                report.update(pending=True, direct_settlement_entry=context['direct_settlement_entry'])
            report['reason'] = str(error) if isinstance(error, (RuntimeError, ValueError)) else 'harvest_evidence_or_transport_failed'
            return report
