"""Read-only continuation of the exact completed skill-tutorial navigation.

No purchase/finish flags or success status are imported. Original acknowledged
auxiliary inputs remain recorded as history; this only restores planning data.
"""
from desktop.umaa_public_context import endpoint as _public_endpoint
import hashlib
import json
from pathlib import Path
import cv2

def read(path):
    value = json.loads(Path(path).read_text(encoding='utf8'))
    if not isinstance(value, dict):
        raise ValueError('tutorial_resume_object_invalid')
    return value

def verify_tutorial_summary(source, device, stop, preset):
    source = Path(source).resolve()
    prior = read(source / 'adapter_result.json')
    context = read(source / 'context.json')
    outcome = prior.get('action_outcome') or {}
    last = prior.get('last_observation') or {}
    records = prior.get('records') or []
    aux = prior.get('passive_popup_receipts') or []
    if device.endpoint != _public_endpoint() or prior.get('ok') is not False or prior.get('current_account_only') is not True or (prior.get('reason') != 'action_page_changed:continue_skill_tutorial') or (outcome.get('action_id') != 'continue_skill_tutorial') or (type(outcome.get('input_attempts')) is not int) or (not 0 < outcome['input_attempts'] == outcome.get('acknowledged_inputs') <= 12) or (outcome.get('source_page') is not None) or (outcome.get('source_frame_sha256') is not None) or (last.get('page') != 'skills') or (last.get('endpoint') != device.endpoint) or (type(last.get('current_skill_points')) is not int) or ('open_skill_confirmation' in last.get('actions', {})) or (context.get('mode') != 'harvest') or (context.get('harvest_preset') != preset) or any((context.get(k) for k in ('harvest_skill_attempted', 'finish_submitted', 'completion_submitted'))) or prior.get('skill_batches') or prior.get('skill_result') or list(source.glob('harvest-skills-*')) or (not records) or (not aux):
        raise RuntimeError('tutorial_resume_not_navigation_only')
    snapshots = {r['frame_sha256']: r for p in source.glob('*.json') if p.stem.isdigit() for r in [read(p)]}

    def frame(value):
        path = Path(value['screenshot_path']).resolve()
        if path.parent != source or value.get('endpoint') != device.endpoint or hashlib.sha256(path.read_bytes()).hexdigest() != value.get('frame_sha256'):
            raise RuntimeError('tutorial_resume_frame_unbound')
        return path
    allowed = {'open_training', 'forward_harvest', 'history_next', 'history_scroll', 'confirm_history', 'open_skills'}
    summary = None
    for record in records:
        path = Path(record['receipt_path']).resolve()
        actual = read(path)
        if path.parent != source or actual.get('performed') is not True or actual.get('endpoint') != device.endpoint or (actual.get('action_id') not in allowed) or any((actual.get(k) != record.get(k) for k in ('action_id', 'source_frame_sha256', 'performed'))):
            raise RuntimeError('tutorial_resume_receipt_invalid')
        if actual['action_id'] == 'open_skills':
            before = snapshots.get(actual.get('source_frame_sha256'), {})
            frame(before)
            if before.get('page') != 'finish_ready' or before.get('current_skill_points') != last['current_skill_points']:
                raise RuntimeError('tutorial_resume_points_changed')
            summary = before.get('finish_summary')
    tutorials = []
    from core.passive_tutorial import detect
    for p in aux:
        path = Path(p).resolve()
        receipt = read(path)
        if path.parent != source / 'popup-actions' or receipt.get('kind') != 'tutorial_pager' or receipt.get('action_id') != 'dismiss_passive_tutorial' or (receipt.get('ok') is not True) or (receipt.get('input_status') != 'dispatched') or (receipt.get('tap_count') != 1) or (receipt.get('endpoint') != device.endpoint) or (receipt.get('reason') != 'passive_popup_transition_observed'):
            raise RuntimeError('tutorial_resume_auxiliary_unverified')
        original = snapshots.get(receipt.get('source_frame_sha256'), {})
        png = frame(original)
        found = detect(cv2.imread(str(png)), original.get('tokens', []))
        if not found or any((found.get(k) != receipt.get(k) for k in ('page_index', 'page_count', 'button'))) or max((abs(a - b) for a, b in zip(found['point'], receipt.get('point', [])))) > 12:
            raise RuntimeError('tutorial_resume_auxiliary_frame_invalid')
        tutorials.append(receipt)
    if not summary or context.get('harvest_summary') != summary or [r['page_index'] for r in tutorials] != list(range(1, len(tutorials) + 1)) or any((r['page_count'] != len(tutorials) for r in tutorials)) or (tutorials[-1].get('button') != '关闭') or (tutorials[-1].get('after_frame_sha256') != last.get('frame_sha256')):
        raise RuntimeError('tutorial_resume_incomplete')
    frame(last)
    if stop.is_set():
        raise RuntimeError('operator_stop')
    fresh = device.capture()
    if fresh.get('page') != 'skills' or fresh.get('endpoint') != device.endpoint or fresh.get('current_skill_points') != last['current_skill_points'] or ('open_skill_confirmation' in fresh.get('actions', {})):
        raise RuntimeError('tutorial_resume_current_skills_changed')
    device.context.update(harvest_summary=summary, tutorial_summary_from=str(source))
    return source

def restore_latest_tutorial(output, device, stop, preset):
    root = Path(output).resolve().parent.parent
    candidates = sorted((p for p in root.glob('*/harvest/adapter_result.json') if p.parent != Path(output).resolve()), key=lambda p: p.parent.parent.name, reverse=True)
    if not candidates:
        return None
    previous = candidates[0]
    if read(previous).get('reason') != 'action_page_changed:continue_skill_tutorial':
        return None
    return verify_tutorial_summary(previous.parent, device, stop, preset)
